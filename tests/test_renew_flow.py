import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from box import Box

from app.schemas.acme import RenewedCert

import app  # noqa: F401  # 确保应用包可被导入


def _make_config(mode: str = "apply") -> Box:
    """构造最小化 deploy 配置，用于 renew_flow 单测。"""
    return Box(
        {
            "deploy": {
                "mode": mode,
                "renew_cooldown_days": 7,
                "max_renew_retries": 3,
            }
        },
        default_box=True,
    )


def _make_renewed_cert(domain: str = "vpn.gw6re98mzl.sqxs123.com") -> RenewedCert:
    now = datetime.now(timezone.utc)
    return RenewedCert(
        domain=domain,
        cert_path=f"/path/{domain}.pem",
        key_path=f"/path/{domain}.key",
        fullchain_path=f"/path/{domain}.pem",
        ca_path=None,
        cert_pem="CERT",
        key_pem="KEY",
        fullchain_pem="CERT",
        ca_pem="",
        issued_at=now,
        expires_at=now + timedelta(days=90),
        key_length="2048",
        sans=["*.vpn.gw6re98mzl.sqxs123.com"],
        issuer="TestCA",
        profile=None,
    )


def test_renew_and_plan_deploy_apply_success_sends_formatted_message(monkeypatch: pytest.MonkeyPatch):
    """续签 + 部署成功时，应按期望格式发送钉钉通知（包含域名、云账号、nginx 主机组）。"""
    from app.services import renew_flow

    cfg = _make_config(mode="apply")

    # 配置与状态存储
    monkeypatch.setattr(renew_flow, "load_raw_config", lambda: cfg)

    class FakeRenewStateStore:
        def __init__(self):
            self.success = []
            self.failure = []

        def get(self, domain_lower: str):
            # 不触发冷却窗口逻辑
            return None

        def record_success(self, domain: str, provider: str, message: str):
            self.success.append((domain, provider, message))

        def record_failure(self, domain: str, provider: str, message: str):
            self.failure.append((domain, provider, message))

    fake_state = FakeRenewStateStore()
    monkeypatch.setattr(renew_flow.store, "RenewStateStore", lambda: fake_state)

    # 续签结果
    renewed = _make_renewed_cert()

    async def fake_renew_certificate(domain: str, provider_name=None, force: bool = False):
        assert domain == renewed.domain
        return renewed

    monkeypatch.setattr(renew_flow, "renew_certificate", fake_renew_certificate)

    # 部署结果（包含阿里云、华为云和 nginx 一组）
    fake_result = {
        "mode": "apply",
        "cert": {
            "domain": renewed.domain,
            "sans": [renewed.domain, *renewed.sans],
            "expires_at": renewed.expires_at.isoformat(),
            "issuer": renewed.issuer,
        },
        "bind_results": [
            {
                "provider_type": "aliyun",
                "provider": "奇裕",
                "product_type": "cdn",
                "product_id": "img.vpn.gw6re98mzl.sqxs123.com",
                "domain": "img.vpn.gw6re98mzl.sqxs123.com",
                "status": "success",
            },
            {
                "provider_type": "huawei",
                "provider": "准游hw",
                "product_type": "elb",
                "product_id": "listener-id-1",
                "domain": renewed.domain,
                "status": "success",
            },
            {
                "provider_type": "nginx",
                "provider": "nginx-ssh",
                "product_type": "nginx",
                "product_id": "test-host",
                "domain": renewed.domain,
                "group": "test",
                "status": "success",
            },
        ],
        "summary": {
            "by_provider_type": {"aliyun": {"success": 1}, "huawei": {"success": 1}, "nginx": {"success": 1}},
            "by_provider": {
                "aliyun:奇裕": {"success": 1},
                "huawei:准游hw": {"success": 1},
                "nginx:nginx-ssh": {"success": 1},
            },
            "by_product_type": {"cdn": {"success": 1}, "elb": {"success": 1}, "nginx": {"success": 1}},
        },
        "failed_count": 0,
        "success_count": 3,
        "nginx_target_count": 1,
    }

    class FakeDeployService:
        def __init__(self):
            self.called_with = None

        async def apply_deploy(self, renewed_cert, provider_name=None):
            self.called_with = (renewed_cert, provider_name)
            return fake_result

        async def plan_dry_run(self, renewed_cert, provider_name=None):
            raise AssertionError("unexpected plan_dry_run call in apply mode")

    fake_deploy = FakeDeployService()
    monkeypatch.setattr(renew_flow, "DeployService", lambda: fake_deploy)

    # 钉钉通知
    class FakeNotifier:
        def __init__(self):
            self.calls = []

        async def notify_renew_result(self, domain: str, success: bool, message: str = "", new_expires_at=None):
            self.calls.append(
                {
                    "domain": domain,
                    "success": success,
                    "message": message,
                    "expires_at": new_expires_at,
                }
            )

        async def notify_deploy_summary(self, *a, **k):
            raise AssertionError("续签入口不得调用仅部署通知，否则会重复发消息")

    fake_notifier = FakeNotifier()
    monkeypatch.setattr(renew_flow.notifier, "DingDingNotifier", lambda: fake_notifier)

    deploy_only_calls = []

    async def fake_deploy_existing_only(*a, **k):
        deploy_only_calls.append(1)
        raise AssertionError("续签入口不得调用 deploy_existing_only")

    monkeypatch.setattr(renew_flow, "deploy_existing_only", fake_deploy_existing_only)

    # 执行完整流程
    result = asyncio.run(renew_flow.renew_and_plan_deploy(domain=renewed.domain, provider_name=None, force=False))

    # 返回结果校验
    assert result["mode"] == "apply"
    assert result["renew_status"] == "success"
    assert result["retry_count"] == 1

    # deploy 应该被调用一次
    assert fake_deploy.called_with is not None
    called_cert, called_provider = fake_deploy.called_with
    assert called_cert.domain == renewed.domain
    assert called_provider is None

    # 续期成功通知内容校验
    assert len(fake_notifier.calls) == 1
    call = fake_notifier.calls[0]
    assert call["domain"] == renewed.domain
    assert call["success"] is True
    # 正文格式：
    # 域名: <domain>
    # 部署目标:
    #   奇裕 阿里云 cdn
    #   准游hw 华为云 elb
    #   nginx 组 test
    body_lines = call["message"].splitlines()
    assert body_lines[0] == f"域名: {renewed.domain}"
    assert body_lines[1] == "部署目标:"
    # 顺序保持当前实现，但断言用包含关系更稳妥
    assert "  奇裕 阿里云 cdn" in body_lines
    assert "  准游hw 华为云 elb" in body_lines
    assert "  nginx 组 test" in body_lines
    assert deploy_only_calls == []


def test_renew_and_plan_deploy_retry_failure_notifies_and_returns_failed(monkeypatch: pytest.MonkeyPatch):
    """续签多次失败时，应发送失败通知，并返回 renew_status=failed。"""
    from app.services import renew_flow

    # 配置：apply 模式，最多重试 2 次
    cfg = Box(
        {
            "deploy": {
                "mode": "apply",
                "renew_cooldown_days": 7,
                "max_renew_retries": 2,
            }
        },
        default_box=True,
    )
    monkeypatch.setattr(renew_flow, "load_raw_config", lambda: cfg)

    class FakeRenewStateStore:
        def __init__(self):
            self.success = []
            self.failure = []

        def get(self, domain_lower: str):
            return None

        def record_success(self, domain: str, provider: str, message: str):
            self.success.append((domain, provider, message))

        def record_failure(self, domain: str, provider: str, message: str):
            self.failure.append((domain, provider, message))

    fake_state = FakeRenewStateStore()
    monkeypatch.setattr(renew_flow.store, "RenewStateStore", lambda: fake_state)

    # renew_certificate 始终抛错
    attempts = {"count": 0}

    async def fake_renew_certificate(domain: str, provider_name=None, force: bool = False):
        attempts["count"] += 1
        raise RuntimeError("renew failed in test")

    monkeypatch.setattr(renew_flow, "renew_certificate", fake_renew_certificate)

    class FakeNotifier:
        def __init__(self):
            self.calls = []

        async def notify_renew_result(self, domain: str, success: bool, message: str = "", new_expires_at=None):
            self.calls.append(
                {
                    "domain": domain,
                    "success": success,
                    "message": message,
                    "expires_at": new_expires_at,
                }
            )

        async def notify_deploy_summary(self, *a, **k):
            raise AssertionError("续签入口不得调用仅部署通知，否则会重复发消息")

    fake_notifier = FakeNotifier()
    monkeypatch.setattr(renew_flow.notifier, "DingDingNotifier", lambda: fake_notifier)

    # DeployService 不应被调用
    class FakeDeployService:
        async def apply_deploy(self, *a, **k):
            raise AssertionError("deploy should not be called when renew fails")

        async def plan_dry_run(self, *a, **k):
            raise AssertionError("plan_dry_run should not be called when renew fails")

    monkeypatch.setattr(renew_flow, "DeployService", lambda: FakeDeployService())

    domain = "vpn.gw6re98mzl.sqxs123.com"
    result = asyncio.run(renew_flow.renew_and_plan_deploy(domain=domain, provider_name=None, force=False))

    # 应该按 max_renew_retries 次数尝试
    assert attempts["count"] == 2
    assert len(fake_state.failure) == 2
    assert fake_state.success == []

    # 返回结果标记为 failed
    assert result["mode"] == "apply"
    assert result["renew_status"] == "failed"
    assert result["retry_count"] == 2
    assert "error" in result

    # 应发送一次失败通知
    assert len(fake_notifier.calls) == 1
    call = fake_notifier.calls[0]
    assert call["domain"] == domain
    assert call["success"] is False
    assert "证书续签失败" in call["message"]


def test_deploy_existing_only_apply_success_sends_deploy_summary(monkeypatch: pytest.MonkeyPatch):
    """点击「部署」成功时，应发送证书部署成功通知（含域名与部署目标）。"""
    from app.services import renew_flow

    cfg = _make_config(mode="apply")
    monkeypatch.setattr(renew_flow, "load_raw_config", lambda: cfg)

    renewed = _make_renewed_cert()

    async def fake_load_existing_certificate(domain: str, provider_name=None):
        assert domain == renewed.domain
        return renewed

    monkeypatch.setattr(renew_flow, "load_existing_certificate", fake_load_existing_certificate)
    monkeypatch.setattr(renew_flow, "_resolve_effective_renew_domain", lambda d: d)

    fake_result = {
        "mode": "apply",
        "bind_results": [
            {
                "provider_type": "aliyun",
                "provider": "奇裕",
                "product_type": "cdn",
                "product_id": "img.vpn.gw6re98mzl.sqxs123.com",
                "domain": renewed.domain,
                "status": "success",
            },
            {
                "provider_type": "nginx",
                "provider": "nginx-ssh",
                "product_type": "nginx",
                "product_id": "test-host",
                "domain": renewed.domain,
                "group": "test",
                "status": "success",
            },
        ],
        "summary": {
            "by_provider_type": {"aliyun": {"success": 1}, "nginx": {"success": 1}},
            "by_provider": {
                "aliyun:奇裕": {"success": 1},
                "nginx:nginx-ssh": {"success": 1},
            },
        },
        "failed_count": 0,
        "success_count": 2,
    }

    class FakeDeployService:
        async def apply_deploy(self, renewed_cert, provider_name=None):
            return fake_result

        async def plan_dry_run(self, renewed_cert, provider_name=None):
            raise AssertionError("unexpected plan_dry_run call in apply mode")

    monkeypatch.setattr(renew_flow, "DeployService", lambda: FakeDeployService())

    class FakeNotifier:
        def __init__(self):
            self.calls = []

        async def notify_deploy_summary(self, domain: str, success: bool, message: str = ""):
            self.calls.append(
                {
                    "domain": domain,
                    "success": success,
                    "message": message,
                }
            )

        async def notify_renew_result(self, *a, **k):
            raise AssertionError("仅部署不应走续签通知")

    fake_notifier = FakeNotifier()
    monkeypatch.setattr(renew_flow.notifier, "DingDingNotifier", lambda: fake_notifier)

    result = asyncio.run(
        renew_flow.deploy_existing_only(domain=renewed.domain, provider_name=None)
    )

    assert result["renew_status"] == "deploy_only"
    assert len(fake_notifier.calls) == 1
    call = fake_notifier.calls[0]
    assert call["domain"] == renewed.domain
    assert call["success"] is True
    body_lines = call["message"].splitlines()
    assert body_lines[0] == f"域名: {renewed.domain}"
    assert body_lines[1] == "部署目标:"
    assert "  奇裕 阿里云 cdn" in body_lines
    assert "  nginx 组 test" in body_lines


def test_deploy_existing_only_apply_failure_notifies_and_reraises(monkeypatch: pytest.MonkeyPatch):
    """点击「部署」失败时，应发送证书部署失败通知，并继续抛出原异常。"""
    from app.services import renew_flow

    cfg = _make_config(mode="apply")
    monkeypatch.setattr(renew_flow, "load_raw_config", lambda: cfg)

    renewed = _make_renewed_cert()

    async def fake_load_existing_certificate(domain: str, provider_name=None):
        return renewed

    monkeypatch.setattr(renew_flow, "load_existing_certificate", fake_load_existing_certificate)
    monkeypatch.setattr(renew_flow, "_resolve_effective_renew_domain", lambda d: d)

    class FakeDeployService:
        async def apply_deploy(self, renewed_cert, provider_name=None):
            raise RuntimeError("证书部署存在失败: 2 个目标失败")

        async def plan_dry_run(self, renewed_cert, provider_name=None):
            raise AssertionError("unexpected plan_dry_run call in apply mode")

    monkeypatch.setattr(renew_flow, "DeployService", lambda: FakeDeployService())

    class FakeNotifier:
        def __init__(self):
            self.calls = []

        async def notify_deploy_summary(self, domain: str, success: bool, message: str = ""):
            self.calls.append(
                {
                    "domain": domain,
                    "success": success,
                    "message": message,
                }
            )

    fake_notifier = FakeNotifier()
    monkeypatch.setattr(renew_flow.notifier, "DingDingNotifier", lambda: fake_notifier)

    with pytest.raises(RuntimeError, match="证书部署存在失败"):
        asyncio.run(renew_flow.deploy_existing_only(domain=renewed.domain, provider_name=None))

    assert len(fake_notifier.calls) == 1
    call = fake_notifier.calls[0]
    assert call["domain"] == renewed.domain
    assert call["success"] is False
    assert "证书部署失败" in call["message"]
    assert "2 个目标失败" in call["message"]

def test_deploy_existing_only_keeps_deploy_error_when_failure_notification_fails(
    monkeypatch: pytest.MonkeyPatch,
):
    """部署和通知同时失败时，调用方必须拿到原始部署异常。"""
    from app.services import renew_flow

    cfg = _make_config(mode="apply")
    monkeypatch.setattr(renew_flow, "load_raw_config", lambda: cfg)
    monkeypatch.setattr(renew_flow, "_resolve_effective_renew_domain", lambda d: d)

    async def fake_load_existing_certificate(domain: str, provider_name=None):
        return _make_renewed_cert()

    class FakeDeployService:
        async def apply_deploy(self, renewed_cert, provider_name=None):
            raise RuntimeError("original deploy failure")

    class FakeNotifier:
        async def notify_deploy_summary(self, **kwargs):
            raise RuntimeError("notification failure")

    monkeypatch.setattr(renew_flow, "load_existing_certificate", fake_load_existing_certificate)
    monkeypatch.setattr(renew_flow, "DeployService", lambda: FakeDeployService())
    monkeypatch.setattr(renew_flow.notifier, "DingDingNotifier", lambda: FakeNotifier())

    with pytest.raises(RuntimeError, match="original deploy failure"):
        asyncio.run(renew_flow.deploy_existing_only(domain="vpn.example.com"))


def test_format_deploy_notify_message_keeps_legacy_layout():
    """抽出的正文函数必须保持续签成功通知的历史格式，避免两入口文案分叉。"""
    from app.services.renew_flow import format_deploy_notify_message

    result = {
        "bind_results": [
            {
                "provider_type": "aliyun",
                "provider": "奇裕",
                "product_type": "cdn",
                "status": "success",
            },
            {
                "provider_type": "huawei",
                "provider": "准游hw",
                "product_type": "elb",
                "status": "success",
            },
            {
                "provider_type": "nginx",
                "provider": "nginx-ssh",
                "product_type": "nginx",
                "group": "test",
                "status": "success",
            },
        ]
    }
    body = format_deploy_notify_message(result, "vpn.example.com")
    lines = body.splitlines()
    assert lines[0] == "域名: vpn.example.com"
    assert lines[1] == "部署目标:"
    assert "  奇裕 阿里云 cdn" in lines
    assert "  准游hw 华为云 elb" in lines
    assert "  nginx 组 test" in lines


def test_renew_and_plan_deploy_deploy_failed_sends_only_renew_notify(monkeypatch: pytest.MonkeyPatch):
    """续签成功但部署失败：只发续签失败汇总，不走仅部署通知，也不调用 deploy_existing_only。"""
    from app.services import renew_flow

    cfg = _make_config(mode="apply")
    monkeypatch.setattr(renew_flow, "load_raw_config", lambda: cfg)

    class FakeRenewStateStore:
        def __init__(self):
            self.success = []
            self.failure = []
            self.deploy_failure = []

        def get(self, domain_lower: str):
            return None

        def record_success(self, domain: str, provider: str, message: str):
            self.success.append((domain, provider, message))

        def record_failure(self, domain: str, provider: str, message: str):
            self.failure.append((domain, provider, message))

        def record_deploy_failure(self, domain: str, provider: str, message: str):
            self.deploy_failure.append((domain, provider, message))

    fake_state = FakeRenewStateStore()
    monkeypatch.setattr(renew_flow.store, "RenewStateStore", lambda: fake_state)

    renewed = _make_renewed_cert()

    async def fake_renew_certificate(domain: str, provider_name=None, force: bool = False):
        return renewed

    monkeypatch.setattr(renew_flow, "renew_certificate", fake_renew_certificate)

    class FakeDeployService:
        async def apply_deploy(self, renewed_cert, provider_name=None):
            raise RuntimeError("证书部署存在失败: 1 个目标失败")

        async def plan_dry_run(self, *a, **k):
            raise AssertionError("unexpected plan_dry_run call in apply mode")

    monkeypatch.setattr(renew_flow, "DeployService", lambda: FakeDeployService())

    class FakeNotifier:
        def __init__(self):
            self.renew_calls = []
            self.deploy_calls = []

        async def notify_renew_result(self, domain: str, success: bool, message: str = "", new_expires_at=None):
            self.renew_calls.append(
                {
                    "domain": domain,
                    "success": success,
                    "message": message,
                    "expires_at": new_expires_at,
                }
            )

        async def notify_deploy_summary(self, domain: str, success: bool, message: str = ""):
            self.deploy_calls.append(
                {
                    "domain": domain,
                    "success": success,
                    "message": message,
                }
            )

    fake_notifier = FakeNotifier()
    monkeypatch.setattr(renew_flow.notifier, "DingDingNotifier", lambda: fake_notifier)

    deploy_only_calls = []

    async def fake_deploy_existing_only(*a, **k):
        deploy_only_calls.append(1)
        raise AssertionError("续签入口不得调用 deploy_existing_only")

    monkeypatch.setattr(renew_flow, "deploy_existing_only", fake_deploy_existing_only)

    result = asyncio.run(
        renew_flow.renew_and_plan_deploy(domain=renewed.domain, provider_name=None, force=False)
    )

    assert result["renew_status"] == "deploy_failed"
    assert fake_state.success == []
    assert fake_state.failure == []
    assert len(fake_state.deploy_failure) == 1
    assert deploy_only_calls == []
    assert fake_notifier.deploy_calls == []
    assert len(fake_notifier.renew_calls) == 1
    call = fake_notifier.renew_calls[0]
    assert call["success"] is False


def test_deploy_failed_pending_waits_for_manual_action(monkeypatch: pytest.MonkeyPatch):
    from app.services import renew_flow

    monkeypatch.setattr(renew_flow, "load_raw_config", lambda: _make_config(mode="apply"))

    class FakeRenewStateStore:
        def get(self, domain_lower: str):
            return {
                "last_status": "deploy_failed",
                "last_message": "cdn deploy failed",
                "last_fail_at": "2026-09-10T01:00:00+00:00",
            }

    monkeypatch.setattr(renew_flow.store, "RenewStateStore", FakeRenewStateStore)

    async def unexpected_renew(*args, **kwargs):
        raise AssertionError("等待人工处理时不应再次续签")

    monkeypatch.setattr(renew_flow, "renew_certificate", unexpected_renew)
    result = asyncio.run(renew_flow.renew_and_plan_deploy("example.com"))

    assert result["renew_status"] == "deploy_failed_pending"
    assert result["retry_count"] == 0
    assert result["error"] == "cdn deploy failed"


def test_issue_and_plan_deploy_dry_run_success(monkeypatch: pytest.MonkeyPatch):
    from app.services import renew_flow

    monkeypatch.setattr(renew_flow, "load_raw_config", lambda: _make_config(mode="dry-run"))
    issued = _make_renewed_cert("example.com")

    async def fake_issue_certificate(domain, sans, provider_name=None):
        assert domain == "example.com"
        assert sans == ["*.example.com"]
        assert provider_name == "cf-main"
        return issued

    monkeypatch.setattr(renew_flow, "issue_certificate", fake_issue_certificate)

    class FakeDeployService:
        async def plan_dry_run(self, renewed_cert, provider_name=None):
            assert renewed_cert is issued
            return {
                "mode": "dry-run",
                "planned_bind_actions": [{"domain": "example.com"}],
                "summary": {},
            }

        async def apply_deploy(self, *args, **kwargs):
            raise AssertionError("dry-run 不应执行 apply")

    monkeypatch.setattr(renew_flow, "DeployService", FakeDeployService)

    calls = []

    class FakeNotifier:
        async def notify_issue_result(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(renew_flow.notifier, "DingDingNotifier", FakeNotifier)

    result = asyncio.run(
        renew_flow.issue_and_plan_deploy(
            "example.com",
            ["example.com", "*.example.com"],
            provider_name="cf-main",
        )
    )

    assert result["issue_status"] == "success"
    assert result["domains"] == ["example.com", "*.example.com"]
    assert len(calls) == 1
    assert calls[0]["success"] is True


def test_issue_and_plan_deploy_issue_failure_skips_deploy(monkeypatch):
    from app.services import renew_flow

    monkeypatch.setattr(renew_flow, "load_raw_config", lambda: _make_config(mode="apply"))

    async def fake_issue_certificate(*args, **kwargs):
        raise RuntimeError("acme order failed")

    monkeypatch.setattr(renew_flow, "issue_certificate", fake_issue_certificate)
    monkeypatch.setattr(
        renew_flow,
        "DeployService",
        lambda: pytest.fail("签发失败后不应构造部署服务"),
    )
    calls = []

    class FakeNotifier:
        async def notify_issue_result(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(renew_flow.notifier, "DingDingNotifier", FakeNotifier)

    result = asyncio.run(renew_flow.issue_and_plan_deploy("example.com", ["example.com"]))

    assert result["issue_status"] == "failed"
    assert result["error"] == "acme order failed"
    assert calls[0]["success"] is False


def test_issue_and_plan_deploy_deploy_failure_is_independent_from_renew_state(monkeypatch):
    from app.services import renew_flow

    monkeypatch.setattr(renew_flow, "load_raw_config", lambda: _make_config(mode="apply"))
    issued = _make_renewed_cert("example.com")

    async def fake_issue_certificate(*args, **kwargs):
        return issued

    monkeypatch.setattr(renew_flow, "issue_certificate", fake_issue_certificate)

    class FakeDeployService:
        async def apply_deploy(self, *args, **kwargs):
            raise RuntimeError("cdn failed")

    monkeypatch.setattr(renew_flow, "DeployService", FakeDeployService)
    calls = []

    class FakeNotifier:
        async def notify_issue_result(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(renew_flow.notifier, "DingDingNotifier", FakeNotifier)
    monkeypatch.setattr(
        renew_flow.store,
        "RenewStateStore",
        lambda: pytest.fail("签发流程不应读取或写入续签状态"),
    )

    result = asyncio.run(renew_flow.issue_and_plan_deploy("example.com", ["example.com"]))

    assert result["issue_status"] == "deploy_failed"
    assert result["error"] == "cdn failed"
    assert calls[0]["success"] is False

