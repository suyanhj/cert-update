import asyncio
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from box import Box

from app.deploys.base import DeployResult, DeployTarget
from app.schemas.acme import RenewedCert
from app.schemas.provider import DeployMatchResult
from app.services import collector, deploy, renew_flow


def _cert():
    now = datetime.now(timezone.utc)
    return RenewedCert(
        domain="example.com", sans=["*.example.com"],
        cert_path="/tmp/fullchain.pem", fullchain_path="/tmp/fullchain.pem",
        key_path="/tmp/privkey.pem", ca_path=None,
        cert_pem="CERT", fullchain_pem="CERT", key_pem="KEY", ca_pem="",
        issued_at=now, expires_at=now + timedelta(days=90),
        key_length="2048", issuer="TestCA", profile=None,
    )


@pytest.fixture
def renewal(monkeypatch):
    config = Box({"deploy": {"mode": "apply", "renew_cooldown_days": 7}}, default_box=True)
    state = SimpleNamespace(
        get=Mock(return_value=None), record_success=Mock(),
        record_failure=Mock(), record_deploy_failure=Mock(),
    )
    notifier = SimpleNamespace(
        notify_renew_result=AsyncMock(), notify_issue_result=AsyncMock(),
        notify_deploy_summary=AsyncMock(),
    )
    service = SimpleNamespace(
        apply_deploy=AsyncMock(return_value={"mode": "apply", "success_count": 1}),
        plan_dry_run=AsyncMock(return_value={"mode": "dry-run", "planned_bind_actions": []}),
    )
    renew = AsyncMock(return_value=_cert())
    monkeypatch.setattr(renew_flow, "load_raw_config", lambda: config)
    monkeypatch.setattr(renew_flow, "resolve_renew_domain", AsyncMock(return_value="example.com"))
    monkeypatch.setattr(renew_flow, "renew_certificate", renew)
    monkeypatch.setattr(renew_flow, "issue_certificate", AsyncMock(return_value=_cert()))
    monkeypatch.setattr(renew_flow, "load_existing_certificate", AsyncMock(return_value=_cert()))
    monkeypatch.setattr(renew_flow.store, "RenewStateStore", lambda: state)
    monkeypatch.setattr(renew_flow.notifier, "DingDingNotifier", lambda: notifier)
    monkeypatch.setattr(renew_flow, "DeployService", lambda: service)
    return config, state, notifier, service, renew


@pytest.mark.parametrize("automatic", [True, False])
@pytest.mark.parametrize("status", ["success", "deploy_failed"])
def test_force_renewal_respects_state_only_for_automatic_calls(renewal, automatic, status):
    _, state, _, service, renew = renewal
    state.get.return_value = {
        "last_status": status,
        "last_success_at": datetime.now(timezone.utc).isoformat(),
        "last_message": "原部署失败",
    }
    result = asyncio.run(renew_flow.renew_and_plan_deploy(
        "api.example.com", force=True, automatic=automatic,
    ))
    state.get.assert_called_once_with("example.com")
    if automatic:
        assert result["renew_status"] == ("skipped" if status == "success" else "deploy_failed_pending")
        renew.assert_not_awaited()
        service.apply_deploy.assert_not_awaited()
        state.record_success.assert_not_called()
    else:
        assert result["deploy_status"] == "success"
        assert renew.await_args.kwargs["force"] is True
        assert renew.await_args.kwargs["main_domain"] == "example.com"
        service.apply_deploy.assert_awaited_once()


def test_automatic_renewal_without_protection_still_forces_acme_and_deploys_all(renewal):
    _, state, _, service, renew = renewal
    result = asyncio.run(renew_flow.renew_and_plan_deploy("api.example.com", force=True, automatic=True))
    assert result["renew_status"] == "success"
    assert renew.await_args.kwargs["force"] is True
    assert service.apply_deploy.await_args.kwargs["provider_name"] is None
    state.record_success.assert_called_once()


def test_automatic_scan_deduplicates_actual_certificate(monkeypatch, renewal):
    monkeypatch.setattr(collector, "load_raw_config", lambda: Box(
        {"auto_renew_enabled": True, "auto_renew_days": 20}, default_box=True,
    ))
    _, state, _, service, renew = renewal
    resolve = AsyncMock(return_value="example.com")
    monkeypatch.setattr(renew_flow, "resolve_renew_domain", resolve)
    monkeypatch.setattr(collector, "renew_and_plan_deploy", renew_flow.renew_and_plan_deploy)
    asyncio.run(collector._auto_renew_and_deploy_expiring_certificates([
        {"domain": "api.example.com", "provider": "ali-1", "days": 5},
        {"domain": "www.example.com", "provider": "ali-1", "days": 6},
    ]))
    assert resolve.await_count == 2
    renew.assert_awaited_once_with(domain="api.example.com", provider_name=None, force=True, main_domain="example.com")
    service.apply_deploy.assert_awaited_once()
    state.record_success.assert_called_once()


@pytest.mark.parametrize("has_snapshot", [True, False])
def test_failed_domain_collection_returns_and_next_round_recovers(monkeypatch, has_snapshot):
    # 就绪事件不能让历史快照在失败轮次触发自动操作。
    monkeypatch.setattr(collector.state, "DOMAIN_READY", asyncio.Event())
    if has_snapshot:
        collector.state.DOMAIN_READY.set()
    monkeypatch.setattr(collector.state, "LAST_UPDATED", "OLD")
    domains = AsyncMock(side_effect=[RuntimeError("域名查询失败"), None])
    ecs = AsyncMock()
    bindings = AsyncMock()
    certs = AsyncMock()
    snapshot = Mock()
    monkeypatch.setattr(collector, "collect_domains", domains)
    monkeypatch.setattr(collector, "collect_ecs_instances_in_thread", ecs)
    monkeypatch.setattr(collector, "refresh_product_bindings_cache", bindings)
    monkeypatch.setattr(collector, "collect_certificates", certs)
    monkeypatch.setattr(collector, "persist_state_snapshot", snapshot)
    monkeypatch.setattr(collector.timeutils, "now", lambda: "NEW")

    async def run():
        with pytest.raises(RuntimeError, match="域名查询失败"):
            await asyncio.wait_for(collector.collect_all(), timeout=0.5)
        certs.assert_not_awaited()
        snapshot.assert_not_called()
        assert collector.state.LAST_UPDATED == "OLD"
        await asyncio.wait_for(collector.collect_all(), timeout=0.5)

    asyncio.run(run())
    assert ecs.await_count == bindings.await_count == 2
    certs.assert_awaited_once()
    snapshot.assert_called_once()
    assert collector.state.LAST_UPDATED == "NEW"


@pytest.mark.parametrize("operation", ["renew", "issue", "deploy_only"])
def test_dry_run_never_records_successful_deployment(renewal, operation):
    config, state, notifier, service, _ = renewal
    config.deploy.mode = "dry-run"
    # 即使已有待人工处理状态，手工预演也不能将它清除。
    state.get.return_value = {"last_status": "deploy_failed"}
    if operation == "renew":
        result = asyncio.run(renew_flow.renew_and_plan_deploy("example.com", force=True))
        notification = notifier.notify_renew_result
    elif operation == "issue":
        result = asyncio.run(renew_flow.issue_and_plan_deploy("example.com", ["example.com"]))
        notification = notifier.notify_issue_result
    else:
        result = asyncio.run(renew_flow.deploy_existing_only("example.com"))
        notification = notifier.notify_deploy_summary
    assert result["deploy_status"] == "planned"
    state.record_success.assert_not_called()
    state.record_deploy_failure.assert_not_called()
    service.apply_deploy.assert_not_awaited()
    assert "尚未部署" in notification.await_args.kwargs["message"]
    if operation == "deploy_only":
        assert notification.await_args.kwargs["dry_run"] is True


def test_failed_notification_does_not_replace_partial_deploy_result(renewal):
    _, state, notifier, service, _ = renewal
    failure = {
        "deploy_status": "partial_failed", "success_count": 1, "failed_count": 1,
        "bind_results": [{"provider": "nginx-ssh", "product_id": "host-1", "status": "failed", "message": "SSH 失败"}],
    }
    service.apply_deploy.side_effect = deploy.DeploymentError(failure)
    notifier.notify_renew_result.side_effect = RuntimeError("通知接口不可用")
    result = asyncio.run(renew_flow.renew_and_plan_deploy("example.com"))
    assert result["deploy_status"] == "partial_failed"
    assert result["bind_results"] == failure["bind_results"]
    assert "SSH 失败" in result["error"]
    state.record_success.assert_not_called()


def test_failed_dry_run_preserves_pending_deploy_state(renewal):
    config, state, notifier, service, _ = renewal
    config.deploy.mode = "dry-run"
    state.get.return_value = {"last_status": "deploy_failed"}
    service.plan_dry_run.side_effect = RuntimeError("Nginx 部署计划失败")
    result = asyncio.run(renew_flow.renew_and_plan_deploy("example.com", force=True))
    assert result["deploy_status"] == "failed"
    state.record_success.assert_not_called()
    state.record_deploy_failure.assert_not_called()
    assert "部署预演失败" in notifier.notify_renew_result.await_args.kwargs["message"]


@pytest.mark.parametrize("success", [True, False])
def test_dry_run_notification_title_does_not_claim_deployment(success):
    from app.utils.notifier import DingDingNotifier

    notifier = object.__new__(DingDingNotifier)
    notifier.notify_alert = AsyncMock()
    asyncio.run(notifier.notify_deploy_summary("example.com", success, dry_run=True))
    event = notifier.notify_alert.await_args.args[0]
    assert event.title == ("证书部署预演完成" if success else "证书部署预演失败")


@pytest.mark.parametrize("operation", ["renew", "issue", "deploy_only"])
def test_partial_deployment_result_survives_flow_and_notifies_failure(renewal, operation):
    _, state, notifier, service, _ = renewal
    result = {
        "mode": "apply", "deploy_status": "partial_failed",
        "success_count": 1, "failed_count": 1,
        "bind_results": [
            {"provider": "ali-1", "product_id": "cdn-1", "status": "success"},
            {"provider": "nginx-ssh", "product_id": "host-1", "status": "failed", "message": "密码未设置"},
        ],
    }
    service.apply_deploy.side_effect = deploy.DeploymentError(result)
    if operation == "renew":
        actual = asyncio.run(renew_flow.renew_and_plan_deploy("example.com"))
        state.record_deploy_failure.assert_called_once()
        notification = notifier.notify_renew_result
    elif operation == "issue":
        actual = asyncio.run(renew_flow.issue_and_plan_deploy("example.com", ["example.com"]))
        notification = notifier.notify_issue_result
    else:
        with pytest.raises(deploy.DeploymentError) as exc:
            asyncio.run(renew_flow.deploy_existing_only("example.com"))
        actual = exc.value.result
        notification = notifier.notify_deploy_summary
    assert actual["deploy_status"] == "partial_failed"
    assert actual["bind_results"] == result["bind_results"]
    state.record_success.assert_not_called()
    assert notification.await_args.kwargs["success"] is False
    assert "密码未设置" in notification.await_args.kwargs["message"]


def _deployment(monkeypatch):
    config = Box({
        "deploy": {"mode": "apply", "binding_cache": {"enabled": True, "verify_before_apply": True, "ttl_seconds": 3600}},
        "product_scan": {"tencent": {}},
    }, default_box=True)
    monkeypatch.setattr(deploy, "load_raw_config", lambda: config)
    state = SimpleNamespace(record_success=Mock(), record_failure=Mock())
    monkeypatch.setattr(deploy, "DeployStateStore", lambda: state)
    monkeypatch.setattr(deploy, "DingDingNotifier", lambda: SimpleNamespace(notify_alert=AsyncMock()))
    return deploy.DeployService(), state


@pytest.mark.parametrize("query_error", [True, False])
@pytest.mark.parametrize("nginx_succeeds", [True, False])
def test_verification_error_fails_but_removed_target_skips(monkeypatch, query_error, nginx_succeeds):
    service, state = _deployment(monkeypatch)
    provider = SimpleNamespace(name="ali-1", config=Box({"name": "ali-1", "type": "aliyun"}, default_box=True))
    match = DeployMatchResult(
        provider_type="aliyun", provider_name="ali-1", product_type="cdn",
        product_id="cdn-1", domain="example.com", match_type="exact", current_cert_id=None,
    )
    monkeypatch.setattr(service, "_collect_matches", AsyncMock(return_value=([provider], {"example.com"}, [match], [])))
    bindings = AsyncMock(side_effect=RuntimeError("云接口超时")) if query_error else AsyncMock(return_value=[])
    monkeypatch.setattr(service, "_get_provider_bindings", bindings)
    nginx_result = [{
        "provider_type": "nginx", "provider": "nginx-ssh", "product_type": "nginx",
        "product_id": "host-1", "domain": "example.com", "status": "success", "message": "完成",
    }] if nginx_succeeds else []
    monkeypatch.setattr(service, "_apply_nginx_deploy", AsyncMock(return_value=nginx_result))
    if query_error:
        with pytest.raises(deploy.DeploymentError) as exc:
            asyncio.run(service.apply_deploy(_cert()))
        result = exc.value.result
        assert result["deploy_status"] == ("partial_failed" if nginx_succeeds else "failed")
        assert result["failed_count"] == 1
        assert result["success_count"] == int(nginx_succeeds)
        assert "云接口超时" in str(exc.value)
        state.record_failure.assert_called_once()
    elif nginx_succeeds:
        result = asyncio.run(service.apply_deploy(_cert()))
        assert result["deploy_status"] == "success"
        assert result["failed_count"] == 0
        assert len(result["skipped"]) == 1
    else:
        with pytest.raises(RuntimeError, match="未发现可部署"):
            asyncio.run(service.apply_deploy(_cert()))


@pytest.mark.parametrize("invalid", ["auth", "host", "missing_auth"])
def test_nginx_target_configuration_errors_are_failed_results(monkeypatch, invalid):
    service, state = _deployment(monkeypatch)
    target = {"host_name": "host-1", "host": "127.0.0.1", "target_group": "group-1"}
    if invalid == "host":
        target["host"] = ""
    monkeypatch.setattr(service, "_plan_nginx_deploy", AsyncMock(return_value={"targets": [target]}))
    if invalid == "auth":
        monkeypatch.setattr(service, "_build_nginx_target_auth_map", Mock(side_effect=RuntimeError("SSH 密码环境变量不存在")))
    else:
        monkeypatch.setattr(service, "_build_nginx_target_auth_map", Mock(return_value={}))
    monkeypatch.setattr(deploy, "NginxSSHDeployer", Mock(side_effect=AssertionError("配置错误时不连接 SSH")))
    results = asyncio.run(service._apply_nginx_deploy(_cert()))
    assert len(results) == 1
    assert results[0]["status"] == "failed"
    assert results[0]["product_id"] == "host-1"
    state.record_failure.assert_called_once()


def test_nginx_plan_failure_preserves_successful_cloud_result(monkeypatch):
    service, _ = _deployment(monkeypatch)
    provider = SimpleNamespace(name="ali-1", config=Box({"name": "ali-1", "type": "aliyun"}, default_box=True))
    match = DeployMatchResult(provider_type="aliyun", provider_name="ali-1", product_type="cdn", product_id="cdn-1", domain="example.com", match_type="exact", current_cert_id=None)
    service.conf.deploy.binding_cache.enabled = False
    monkeypatch.setattr(service, "_collect_matches", AsyncMock(return_value=([provider], {"example.com"}, [match], [])))
    target = DeployTarget(provider="aliyun", product_type="cdn", product_id="cdn-1", domain="example.com")
    cloud_deployer = SimpleNamespace(prepare_certificate=AsyncMock(return_value="new-cert"), deploy=AsyncMock(return_value=DeployResult(success=True, target=target, message="完成")))
    monkeypatch.setattr(service, "_build_deployer", Mock(return_value=cloud_deployer))
    monkeypatch.setattr(deploy, "NginxDeployPlanner", Mock(return_value=SimpleNamespace(plan_for_certificate=Mock(side_effect=ValueError("Nginx 目标组不存在")))))
    with pytest.raises(deploy.DeploymentError) as exc:
        asyncio.run(service.apply_deploy(_cert()))
    assert exc.value.result["deploy_status"] == "partial_failed"
    assert exc.value.result["success_count"] == exc.value.result["failed_count"] == 1
    assert "目标组不存在" in str(exc.value)


def test_failed_binding_query_does_not_produce_successful_dry_run(monkeypatch):
    service, _ = _deployment(monkeypatch)
    service.conf.deploy.mode = "dry-run"
    monkeypatch.setattr(service, "_collect_matches", AsyncMock(return_value=(
        [], {"example.com"}, [], [{"status": "failed", "reason": "绑定查询超时"}],
    )))
    with pytest.raises(RuntimeError, match="部署预演查询失败.*绑定查询超时"):
        asyncio.run(service.plan_dry_run(_cert()))


@pytest.mark.parametrize("old_latest", [True, False])
def test_build_script_tags_and_pushes_new_image(tmp_path, old_latest):
    # 使用模拟 Docker 函数运行原脚本，不构建、不连接镜像仓库。
    git = shutil.which("git")
    bash = Path(git).parent.parent / "bin" / "bash.exe" if os.name == "nt" and git else shutil.which("bash")
    assert bash and Path(bash).exists(), "镜像发布回归测试需要 Git Bash 或 Bash"
    script = Path(__file__).resolve().parents[1] / "build.sh"
    harness = r'''
declare -A images
if [[ "$OLD_LATEST" == 1 ]]; then images["repo/test:latest"]=old; fi
docker() {
    case "$1" in
        build) images["$3"]=new ;;
        tag) [[ -n "${images[$2]:-}" ]] || return 1; images["$3"]="${images[$2]}" ;;
        push) printf '%s=%s\n' "$2" "${images[$2]}" ;;
        *) return 1 ;;
    esac
}
source "$BUILD_SCRIPT" 1.2.3
'''
    env = os.environ.copy()
    env.update(OLD_LATEST=str(int(old_latest)), IMAGE_BASE="repo/test", BUILD_SCRIPT=script.as_posix())
    completed = subprocess.run([str(bash), "-c", harness], env=env, text=True, capture_output=True, timeout=20)
    assert completed.returncode == 0, completed.stderr
    assert "repo/test:1.2.3=new" in completed.stdout
    assert "repo/test:latest=new" in completed.stdout
