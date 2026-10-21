import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from box import Box
from cryptography import x509
from pydantic import ValidationError

import app.config as config_mod
from app.schemas.config import AppConfig
from app.schemas.provider import CloudProductBinding, DeployMatchResult
from app.services import config_runtime, deploy, scheduler
from app.utils import store, tls_probe


def test_runtime_config_uses_normalized_model_values(monkeypatch):
    raw = {"acme": {}, "auto_renew_enabled": "false", "tls_scan_parallel": "3"}
    monkeypatch.setattr(config_mod, "_read_raw_dict", lambda: raw)
    monkeypatch.setattr(config_mod, "_CONFIG_CACHE", None)
    monkeypatch.setattr(config_mod, "_apply_log_level", lambda conf: None)
    conf = config_mod.get_config()
    assert conf.auto_renew_enabled is False
    assert conf.tls_scan_parallel == 3
    assert conf.acme.command_timeout_seconds == 900
    assert conf.acme.dns_sleep_seconds == 60
    assert raw["acme"] == {}


@pytest.mark.parametrize("parallel", [0, -1])
def test_tls_invalid_parallel_is_rejected(parallel):
    with pytest.raises(ValidationError):
        AppConfig(acme={}, tls_scan_parallel=parallel)
    with pytest.raises(ValueError, match="并发数必须大于"):
        asyncio.run(tls_probe.probe_domains_batch(["example.com"], concurrency=parallel))


@pytest.mark.parametrize("domain, expected", [
    ("a.xx.hj.com", True), ("A.XX.HJ.COM.", True),
    ("a.b.xx.hj.com", False), ("hj.com", False),
    ("xx.hj.com", False), ("axx.hj.com", False),
])
def test_tls_wildcard_matches_only_one_label(domain, expected):
    cert = SimpleNamespace(subject=[], extensions=SimpleNamespace(
        get_extension_for_class=lambda _: SimpleNamespace(value=SimpleNamespace(
            get_values_for_type=lambda _: ["*.xx.hj.com"]))
    ))
    assert tls_probe._cert_match_domain(cert, domain) is expected


def certificate():
    return SimpleNamespace(domain="example.com", expires_at=datetime.now(timezone.utc) + timedelta(days=10),
                           issuer="test", cert_pem="CERT", key_pem="KEY")


@pytest.mark.parametrize("mode", ["apply", "dry-run"])
@pytest.mark.parametrize("offset", [-1, 0])
def test_expired_certificate_fails_before_cloud_queries(mode, offset, monkeypatch):
    now = datetime(2026, 10, 10, tzinfo=timezone.utc)
    monkeypatch.setattr(deploy.TimeUtil, "now_utc", lambda: now)
    service = deploy.DeployService.__new__(deploy.DeployService)
    service.conf = Box({"deploy": {"mode": mode}})
    service._collect_matches = AsyncMock()
    cert = certificate()
    cert.expires_at = now + timedelta(seconds=offset)
    operation = service.apply_deploy if mode == "apply" else service.plan_dry_run
    with pytest.raises(ValueError, match="已过期"):
        asyncio.run(operation(cert))
    service._collect_matches.assert_not_awaited()


def test_expiry_accepts_equivalent_future_timezone(monkeypatch):
    now = datetime(2026, 10, 10, tzinfo=timezone.utc)
    monkeypatch.setattr(deploy.TimeUtil, "now_utc", lambda: now)
    cert = certificate()
    cert.expires_at = (now + timedelta(seconds=1)).astimezone(timezone(timedelta(hours=8)))
    deploy.DeployService._check_certificate_expiry(cert)


@pytest.mark.parametrize("failure", ["construct", "upload", "deploy"])
@pytest.mark.parametrize("failed_account", ["a", "b"])
def test_account_failure_preserves_other_account_and_nginx(monkeypatch, failure, failed_account):
    service = deploy.DeployService.__new__(deploy.DeployService)
    service.conf = Box({"deploy": {"mode": "apply"}})
    providers = [SimpleNamespace(name=name, config=SimpleNamespace(type="aliyun")) for name in ["a", "b"]]
    matches = [DeployMatchResult("aliyun", p.name, "cdn", p.name, "example.com", "exact", None) for p in providers]
    service._collect_matches = AsyncMock(return_value=(providers, {"example.com"}, matches, []))
    service._binding_cache_settings = lambda: (False, 3600, False)
    service._augment_tencent_ssl_matches = AsyncMock(return_value=(matches, {}, {}))
    deployers = {name: SimpleNamespace(
        prepare_certificate=AsyncMock(return_value="id"),
        deploy=AsyncMock(return_value=SimpleNamespace(success=True, message="完成", cert_id="id")),
    ) for name in ["a", "b"]}
    if failure != "construct":
        action = deployers[failed_account].prepare_certificate if failure == "upload" else deployers[failed_account].deploy
        action.side_effect = RuntimeError("模拟账号失败")
    def build(provider):
        if failure == "construct" and provider.name == failed_account:
            raise RuntimeError("模拟账号失败")
        return deployers[provider.name]
    service._build_deployer = build
    service._apply_nginx_deploy = AsyncMock(return_value=[{
        "provider_type": "nginx", "provider": "nginx", "product_type": "nginx",
        "product_id": "host", "domain": "example.com", "status": "success", "message": "完成",
    }])
    monkeypatch.setattr(deploy, "DeployStateStore", lambda: Mock())
    with pytest.raises(deploy.DeploymentError) as exc:
        asyncio.run(service.apply_deploy(certificate()))
    result = exc.value.result
    assert result["deploy_status"] == "partial_failed"
    assert result["failed_count"] == 1
    assert result["success_count"] == 2
    successful = "b" if failed_account == "a" else "a"
    deployers[successful].deploy.assert_awaited_once()
    service._apply_nginx_deploy.assert_awaited_once()
    assert any(item["provider"] == failed_account and item["status"] == "failed" for item in result["bind_results"])


@pytest.mark.parametrize("failure", ["construct", "upload", "discover"])
def test_tencent_context_failure_does_not_block_next_account(failure):
    service = deploy.DeployService.__new__(deploy.DeployService)
    service.conf = Box({"product_scan": {"tencent": {"cdn_scan": True}}}, default_box=True)
    providers = [SimpleNamespace(name=name, config=SimpleNamespace(type="tencent"),
        get_ssl_deploy_bindings=AsyncMock(return_value=[CloudProductBinding("cdn", name, "example.com")]))
        for name in ["a", "b"]]
    deployers = {name: SimpleNamespace(upload_certificate=AsyncMock(return_value="id-" + name)) for name in ["a", "b"]}
    if failure == "upload":
        deployers["a"].upload_certificate.side_effect = RuntimeError("上传失败")
    if failure == "discover":
        providers[0].get_ssl_deploy_bindings.side_effect = RuntimeError("发现失败")
    def build(provider):
        if failure == "construct" and provider.name == "a":
            raise RuntimeError("构造失败")
        return deployers[provider.name]
    service._build_deployer = build
    skipped = []
    matches, _, ids = asyncio.run(service._augment_tencent_ssl_matches(
        providers, certificate(), {"example.com"}, [], skipped))
    assert [item.provider_name for item in matches] == ["b"]
    assert ids["tencent:b"] == "id-b"
    assert skipped[0]["provider"] == "a"
    assert skipped[0]["status"] == "failed"
    if failure in {"construct", "upload"}:
        providers[0].get_ssl_deploy_bindings.assert_not_awaited()


def test_scheduler_stop_waits_for_active_round(monkeypatch):
    async def run():
        started, finish = asyncio.Event(), asyncio.Event()
        completed = []
        async def collect():
            started.set()
            await finish.wait()
            completed.append(True)
        monkeypatch.setattr(scheduler, "collect_all", collect)
        monkeypatch.setattr(scheduler, "_stop_event", asyncio.Event())
        monkeypatch.setattr(scheduler, "_scheduler_task", None)
        scheduler.start_scheduler(60)
        await asyncio.wait_for(started.wait(), 1)
        stop = asyncio.create_task(scheduler.stop_scheduler())
        await asyncio.sleep(0)
        assert not stop.done()
        finish.set()
        await asyncio.wait_for(stop, 1)
        assert completed == [True]
        assert scheduler._scheduler_task is None
    asyncio.run(run())


def test_scheduler_stop_wakes_interval_immediately(monkeypatch):
    async def run():
        collect = AsyncMock()
        monkeypatch.setattr(scheduler, "collect_all", collect)
        monkeypatch.setattr(scheduler, "_stop_event", asyncio.Event())
        monkeypatch.setattr(scheduler, "_scheduler_task", None)
        scheduler.start_scheduler(60, run_immediately=False)
        await asyncio.sleep(0)
        await asyncio.wait_for(scheduler.stop_scheduler(), 1)
        collect.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize("write_fails", [False, True])
def test_config_switch_waits_before_write_and_restores_scheduler(monkeypatch, write_fails):
    async def run():
        entered, finished = asyncio.Event(), asyncio.Event()
        async def stop():
            entered.set()
            await finished.wait()
        write = Mock(side_effect=RuntimeError("不可写") if write_fails else None)
        start = Mock()
        monkeypatch.setattr(config_runtime, "_apply_lock", asyncio.Lock())
        monkeypatch.setattr(config_runtime, "get_expanded_config_for_validation", lambda conf: conf)
        monkeypatch.setattr(config_runtime, "get_config", lambda: SimpleNamespace(check_interval_minutes=10))
        monkeypatch.setattr(config_runtime, "stop_scheduler", stop)
        monkeypatch.setattr(config_runtime, "update_config_atomic", write)
        monkeypatch.setattr(config_runtime, "reload_config", lambda: SimpleNamespace(check_interval_minutes=20))
        monkeypatch.setattr(config_runtime, "start_scheduler", start)
        task = asyncio.create_task(config_runtime.apply_config_dict({"acme": {}}))
        await asyncio.wait_for(entered.wait(), 1)
        write.assert_not_called()
        finished.set()
        if write_fails:
            with pytest.raises(RuntimeError, match="不可写"):
                await task
        else:
            await task
        start.assert_called_once_with(10 if write_fails else 20, run_immediately=False)
    asyncio.run(run())


@pytest.mark.parametrize("content", [b'{broken', b'', b'\xff\xfe'])
def test_corrupt_store_backs_up_bytes_and_can_persist(tmp_path, content):
    path = tmp_path / "state.json"
    path.write_bytes(content)
    state = store.DomainProviderStore(str(path))
    assert state.get_all() == {}
    backups = list(tmp_path.glob("state.json.corrupt-*.bak"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == content
    state._persist()
    assert path.read_text(encoding="utf-8") == "{}"
    assert store.DomainProviderStore(str(path)).get_all() == {}


def test_corrupt_store_backup_failure_keeps_original(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_bytes(b'{broken')
    monkeypatch.setattr(store.shutil, "copy2", Mock(side_effect=OSError("备份失败")))
    with pytest.raises(OSError, match="备份失败"):
        store.DomainProviderStore(str(path))
    assert path.read_bytes() == b'{broken'
