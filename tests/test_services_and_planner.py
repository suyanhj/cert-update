import asyncio
import importlib
import sys
import types
from datetime import datetime, timedelta

import pytest
from box import Box

import app.config as app_config_module
import app.services.deploy as deploy_service
from app.deploys.base import CertificateDeployer, DeployResult, DeployTarget
from app.schemas.acme import RenewedCert
from app.schemas.alert import AlertLevel
from app.schemas.provider import CloudProductBinding, DeployMatchResult


def _import_certs_service_with_fake_nicegui():
    fake_nicegui = types.ModuleType("nicegui")
    fake_nicegui.ui = types.SimpleNamespace(notify=lambda *a, **k: None)
    sys.modules["nicegui"] = fake_nicegui
    return importlib.import_module("app.services.certs")


def test_certificates_alert_adds_event_for_critical(monkeypatch: pytest.MonkeyPatch):
    certs_service = _import_certs_service_with_fake_nicegui()

    class FakeStateStore:
        def is_firing(self, domain):
            return False

        def fire(self, domain):
            return True

        def recover(self, domain):
            return None

    class FakeBatcher:
        def __init__(self):
            self.events = []

        def add(self, event):
            self.events.append(event)

    monkeypatch.setattr(
        certs_service,
        "load_raw_config",
        lambda: Box(
            {
                "providers": [],
                "whitelist": [],
                "alert": {"cert": {"enabled": True, "cert_warn_days": 15, "cert_expiry_days": 28}},
            },
            default_box=True,
        ),
    )
    monkeypatch.setattr(certs_service.store, "DomainProviderStore", lambda: object())
    monkeypatch.setattr(certs_service.store, "AlertStateStore", lambda *a, **k: FakeStateStore())
    monkeypatch.setattr(certs_service.notifier, "DingDingNotifier", lambda *a, **k: object())
    monkeypatch.setattr(certs_service.notifier, "AlertBatcher", FakeBatcher)

    svc = certs_service.Certificates()
    asyncio.run(
        svc.cert_alert(
            {
                "domain": "a.example.com",
                "days": 5,
                "provider": "aliyun",
                "exp": "2026-12-31 00:00:00",
            }
        )
    )

    assert len(svc.alert_batch.events) == 1
    assert svc.alert_batch.events[0].level == AlertLevel.CRITICAL


def test_certificates_alert_recovers_when_not_expiring(monkeypatch: pytest.MonkeyPatch):
    certs_service = _import_certs_service_with_fake_nicegui()
    calls = {"recover": 0}

    class FakeStateStore:
        def is_firing(self, domain):
            return False

        def fire(self, domain):
            return True

        def recover(self, domain):
            calls["recover"] += 1

    class FakeBatcher:
        def __init__(self):
            self.events = []

        def add(self, event):
            self.events.append(event)

    monkeypatch.setattr(
        certs_service,
        "load_raw_config",
        lambda: Box(
            {
                "providers": [],
                "whitelist": [],
                "alert": {"cert": {"enabled": True, "cert_warn_days": 15, "cert_expiry_days": 28}},
            },
            default_box=True,
        ),
    )
    monkeypatch.setattr(certs_service.store, "DomainProviderStore", lambda: object())
    monkeypatch.setattr(certs_service.store, "AlertStateStore", lambda *a, **k: FakeStateStore())
    monkeypatch.setattr(certs_service.notifier, "DingDingNotifier", lambda *a, **k: object())
    monkeypatch.setattr(certs_service.notifier, "AlertBatcher", FakeBatcher)

    svc = certs_service.Certificates()
    asyncio.run(
        svc.cert_alert(
            {
                "domain": "a.example.com",
                "days": 120,
                "provider": "aliyun",
                "exp": "2027-12-31 00:00:00",
            }
        )
    )

    assert calls["recover"] == 1
    assert svc.alert_batch.events == []


def _renewed_cert() -> RenewedCert:
    now = datetime.now()
    return RenewedCert(
        domain="example.com",
        cert_path="/tmp/cert.pem",
        key_path="/tmp/key.pem",
        fullchain_path="/tmp/fullchain.pem",
        ca_path=None,
        cert_pem="CERT",
        key_pem="KEY",
        fullchain_pem="FULLCHAIN",
        ca_pem="",
        issued_at=now,
        expires_at=now + timedelta(days=90),
        key_length="ECC-256",
        sans=["*.example.com"],
        issuer="TestCA",
        profile=None,
    )


def test_renew_deploy_dry_run_generates_plan(monkeypatch: pytest.MonkeyPatch):
    class FakeAliyunProvider:
        def __init__(self, cfg):
            self.name = getattr(cfg, "name", "ali-1")
            self.config = cfg
            self._provider_type = "aliyun"

        async def get_cdn_bindings(self):
            return [
                CloudProductBinding(
                    domain="api.example.com",
                    product_type="cdn",
                    product_id="p-1",
                    cert_id="old-1",
                )
            ]

        async def get_lb_bindings(self):
            return []

        async def get_oss_bindings(self):
            return []

    monkeypatch.setattr(
        deploy_service,
        "load_raw_config",
        lambda: Box(
            {
                "deploy": {
                    "mode": "dry-run",
                    "binding_cache": {"enabled": False},
                },
                "product_scan": {
                    "aliyun": {"cdn_scan": True, "lb_scan": True, "oss_scan": True},
                },
                "providers": [{"name": "ali-1", "type": "aliyun", "enabled": True}],
            },
            default_box=True,
        ),
    )
    monkeypatch.setattr(deploy_service, "AliyunCloudProvider", FakeAliyunProvider)

    planner = deploy_service.DeployService()
    plan = asyncio.run(planner.plan_dry_run(_renewed_cert()))

    assert plan["mode"] == "dry-run"
    assert len(plan["planned_bind_actions"]) == 1
    assert plan["planned_bind_actions"][0]["domain"] == "api.example.com"


def test_renew_deploy_apply_raises_when_any_target_failed(monkeypatch: pytest.MonkeyPatch):
    class FakeAliyunProvider:
        def __init__(self, cfg):
            self.name = getattr(cfg, "name", "ali-1")
            self.config = cfg
            self._provider_type = "aliyun"

        async def get_cdn_bindings(self):
            return [
                CloudProductBinding(
                    domain="api.example.com",
                    product_type="cdn",
                    product_id="p-1",
                    cert_id="old-1",
                )
            ]

        async def get_lb_bindings(self):
            return []

        async def get_oss_bindings(self):
            return []

    class FakeDeployer(CertificateDeployer):
        @property
        def name(self):
            return "aliyun"

        async def upload_certificate(self, cert_pem: str, key_pem: str, main_domain: str | None = None):
            return "new-1"

        async def deploy(self, cert_pem: str, key_pem: str, target: DeployTarget, cert_id=None):
            return DeployResult(success=False, target=target, message="fail")

        async def list_targets(self):
            return []

    apply_config = Box(
        {
            "deploy": {
                "mode": "apply",
                "binding_cache": {"enabled": False, "verify_before_apply": False},
            },
            "product_scan": {
                "aliyun": {"cdn_scan": True, "lb_scan": True, "oss_scan": True},
            },
            "providers": [{"name": "ali-1", "type": "aliyun", "enabled": True, "credentials": {"access_key_id": "x", "access_key_secret": "y", "region": "cn-hangzhou"}}],
        },
        default_box=True,
    )
    monkeypatch.setattr(app_config_module, "get_config", lambda force_reload=False: apply_config)
    monkeypatch.setattr(app_config_module, "load_raw_config", lambda: apply_config)
    monkeypatch.setattr(deploy_service, "load_raw_config", lambda: apply_config)
    monkeypatch.setattr(deploy_service, "AliyunCloudProvider", FakeAliyunProvider)
    monkeypatch.setattr(deploy_service, "build_cloud_deployer", lambda *a, **k: FakeDeployer())

    planner = deploy_service.DeployService()
    fake_provider = FakeAliyunProvider(apply_config.providers[0])
    one_binding = [
        CloudProductBinding(
            domain="api.example.com",
            product_type="cdn",
            product_id="p-1",
            cert_id="old-1",
        )
    ]

    async def _fake_get_bindings(*args, **kwargs):
        return one_binding

    monkeypatch.setattr(deploy_service.DeployService, "_build_cloud_providers", lambda self: [fake_provider])
    monkeypatch.setattr(deploy_service.DeployService, "_get_provider_bindings", _fake_get_bindings)

    renewed_cert = RenewedCert(
        domain="api.example.com",
        cert_path="/tmp/cert.pem",
        key_path="/tmp/key.pem",
        fullchain_path="/tmp/fullchain.pem",
        ca_path=None,
        cert_pem="CERT",
        key_pem="KEY",
        fullchain_pem="FULLCHAIN",
        ca_pem="",
        issued_at=datetime.now(),
        expires_at=datetime.now() + timedelta(days=90),
        key_length="ECC-256",
        sans=[],
        issuer="TestCA",
        profile=None,
    )

    with pytest.raises(RuntimeError, match="失败"):
        asyncio.run(planner.apply_deploy(renewed_cert))


def test_apply_deploy_calls_upload_with_main_domain(monkeypatch: pytest.MonkeyPatch):
    """
    apply_deploy 应该把续签证书的主域名作为 third arg 传给 deployer.upload_certificate，
    便于各云部署器按“域名-时间”规范命名证书别名。
    """

    # 配置：apply 模式，关闭 binding_cache 校验逻辑，避免干扰
    apply_config = Box(
        {
            "deploy": {
                "mode": "apply",
                "binding_cache": {
                    "enabled": False,
                    "ttl_seconds": 3600,
                    "verify_before_apply": False,
                },
            },
            "product_scan": {
                "aliyun": {"cdn_scan": True, "lb_scan": False, "oss_scan": False},
            },
        },
        default_box=True,
    )

    monkeypatch.setattr(deploy_service, "load_raw_config", lambda: apply_config)

    class FakeProvider:
        def __init__(self):
            self.name = "ali-1"
            # config.type 供 _provider_type/_provider_name 使用
            self.config = Box({"type": "aliyun", "name": "ali-1"}, default_box=True)

    fake_provider = FakeProvider()

    # 伪造 _collect_matches，跳过真实的绑定扫描/匹配逻辑，只返回一条 cdn 目标
    async def fake_collect_matches(self, renewed_cert, provider_name=None):
        match = DeployMatchResult(
            provider_type="aliyun",
            provider_name="ali-1",
            product_type="cdn",
            product_id="p-1",
            domain="api.example.com",
            match_type="exact",
            current_cert_id=None,
            listener_port=None,
            metadata={},
        )
        return [fake_provider], {"api.example.com"}, [match], []

    monkeypatch.setattr(
        deploy_service.DeployService,
        "_collect_matches",
        fake_collect_matches,
    )

    upload_calls = []

    class FakeDeployer(CertificateDeployer):
        @property
        def name(self):
            return "aliyun"

        async def upload_certificate(self, cert_pem: str, key_pem: str, main_domain: str | None = None):
            upload_calls.append((cert_pem, key_pem, main_domain))
            return "new-1"

        async def deploy(self, cert_pem: str, key_pem: str, target: DeployTarget, cert_id=None):
            # 模拟部署成功
            return DeployResult(success=True, target=target, message="ok", cert_id=cert_id or "new-1")

        async def list_targets(self):
            return []

    # 所有 provider 均使用同一个 FakeDeployer
    monkeypatch.setattr(
        deploy_service,
        "build_cloud_deployer",
        lambda provider_type, provider_name, credentials: FakeDeployer(),
    )

    planner = deploy_service.DeployService()
    now = datetime.now()
    renewed_cert = RenewedCert(
        domain="api.example.com",
        cert_path="/tmp/cert.pem",
        key_path="/tmp/key.pem",
        fullchain_path="/tmp/fullchain.pem",
        ca_path=None,
        cert_pem="CERT",
        key_pem="KEY",
        fullchain_pem="FULLCHAIN",
        ca_pem="",
        issued_at=now,
        expires_at=now + timedelta(days=90),
        key_length="ECC-256",
        sans=[],
        issuer="TestCA",
        profile=None,
    )

    result = asyncio.run(planner.apply_deploy(renewed_cert))

    # upload_certificate 应该被调用一次，且 third arg 为 renewed_cert.domain
    assert len(upload_calls) == 1
    cert_pem, key_pem, main_domain = upload_calls[0]
    assert cert_pem == "CERT"
    assert key_pem == "KEY"
    assert main_domain == "api.example.com"

    # 返回结果包含一条成功记录
    assert result["mode"] == "apply"
    assert result["success_count"] == 1
    assert result["failed_count"] == 0


# ---------------------------------------------------------------------------
# _get_provider_bindings 三态行为
# ---------------------------------------------------------------------------


class _FakeBindingStore:
    """监控 ProductBindingStore 的 save_provider / get_provider 调用。"""

    def __init__(self, cached: dict | None = None):
        self.cached = cached or {}
        self.save_calls: list = []

    def get_provider(self, provider_key: str):
        return self.cached.get(provider_key)

    def save_provider(self, provider_key, payload, updated_at):
        self.save_calls.append((provider_key, list(payload), updated_at))


class _FakeAliyunProvider:
    def __init__(self, cfg, cdn_bindings=None, lb_bindings=None, oss_bindings=None, raise_exc=None):
        self.name = getattr(cfg, "name", "ali-1") if cfg else "ali-1"
        self.config = cfg
        self._provider_type = "aliyun"
        self._cdn = cdn_bindings or []
        self._lb = lb_bindings or []
        self._oss = oss_bindings or []
        self._raise = raise_exc

    async def get_cdn_bindings(self):
        if self._raise:
            raise self._raise
        return self._cdn

    async def get_lb_bindings(self):
        return self._lb

    async def get_oss_bindings(self):
        return self._oss


def _make_deploy_service(monkeypatch, *, cache_enabled=True, store=None):
    cfg = Box(
        {
            "deploy": {
                "mode": "apply",
                "binding_cache": {
                    "enabled": cache_enabled,
                    "ttl_seconds": 3600,
                    "verify_before_apply": True,
                },
            },
            "product_scan": {
                "aliyun": {"cdn_scan": True, "lb_scan": False, "oss_scan": False},
            },
        },
        default_box=True,
    )
    monkeypatch.setattr(deploy_service, "load_raw_config", lambda: cfg)
    monkeypatch.setattr(deploy_service, "ProductBindingStore", lambda: store or _FakeBindingStore())
    return deploy_service.DeployService()


def test_get_provider_bindings_force_refresh_does_not_persist(monkeypatch):
    """cache_enabled=True && force_refresh=True 走云端实时扫描，且 save_provider 不被调用。"""
    store = _FakeBindingStore()
    svc = _make_deploy_service(monkeypatch, cache_enabled=True, store=store)
    provider = _FakeAliyunProvider(
        cfg=Box({"name": "ali-1", "type": "aliyun"}, default_box=True),
        cdn_bindings=[
            CloudProductBinding(
                domain="api.example.com",
                product_type="cdn",
                product_id="p-1",
                cert_id="realtime-1",
            )
        ],
    )

    bindings = asyncio.run(
        svc._get_provider_bindings(
            provider=provider,
            cache_enabled=True,
            ttl_seconds=3600,
            force_refresh=True,
        )
    )

    assert len(bindings) == 1
    assert bindings[0].cert_id == "realtime-1"
    # verify 不应回写缓存
    assert store.save_calls == []


def test_get_provider_bindings_cache_only_overrides_force_refresh(monkeypatch):
    """cache_only=True 是硬约束：与 force_refresh=True 同时为真时仍只读缓存。"""
    cached_payload = {
        "updated_at": "2099-01-01T00:00:00+00:00",
        "bindings": [
            {"product_type": "cdn", "product_id": "p-1", "domain": "cached.example.com"}
        ],
    }
    store = _FakeBindingStore(cached={"aliyun:ali-1": cached_payload})
    svc = _make_deploy_service(monkeypatch, cache_enabled=True, store=store)

    cdn_calls = {"count": 0}

    class TrackingProvider(_FakeAliyunProvider):
        async def get_cdn_bindings(self):
            cdn_calls["count"] += 1
            return []

    provider = TrackingProvider(cfg=Box({"name": "ali-1", "type": "aliyun"}, default_box=True))

    bindings = asyncio.run(
        svc._get_provider_bindings(
            provider=provider,
            cache_enabled=True,
            ttl_seconds=10**9,
            force_refresh=True,
            cache_only=True,
        )
    )

    # 只读缓存：返回缓存内容，云端 API 未被调用
    assert [b.domain for b in bindings] == ["cached.example.com"]
    assert cdn_calls["count"] == 0
    assert store.save_calls == []


def test_get_provider_bindings_cache_disabled_realtime_no_persist(monkeypatch):
    """cache_enabled=False 直接走实时扫描，不读不写缓存。"""
    store = _FakeBindingStore()
    svc = _make_deploy_service(monkeypatch, cache_enabled=False, store=store)
    provider = _FakeAliyunProvider(
        cfg=Box({"name": "ali-1", "type": "aliyun"}, default_box=True),
        cdn_bindings=[
            CloudProductBinding(
                domain="api.example.com",
                product_type="cdn",
                product_id="p-1",
                cert_id="realtime-2",
            )
        ],
    )

    bindings = asyncio.run(
        svc._get_provider_bindings(
            provider=provider,
            cache_enabled=False,
            ttl_seconds=3600,
            force_refresh=False,  # cache_enabled=False 时 force_refresh 无影响
        )
    )

    assert len(bindings) == 1
    assert bindings[0].cert_id == "realtime-2"
    assert store.save_calls == []


# ---------------------------------------------------------------------------
# _verify_matches_before_apply 行为
# ---------------------------------------------------------------------------


class _FakeNotifier:
    """监控 notify_alert 调用次数与内容。"""

    def __init__(self):
        self.events = []

    async def notify_alert(self, event):
        self.events.append(event)


def _make_match(domain: str, *, cert_id=None) -> DeployMatchResult:
    return DeployMatchResult(
        provider_type="aliyun",
        provider_name="ali-1",
        product_type="cdn",
        product_id="p-1",
        domain=domain,
        match_type="exact",
        current_cert_id=cert_id,
        listener_port=None,
        metadata={},
    )


def test_verify_realtime_hit_overrides_match_fields(monkeypatch):
    """实时存在目标：放行；current_cert_id/listener_port/metadata 取自实时数据。"""
    svc = _make_deploy_service(monkeypatch, cache_enabled=True)
    provider = _FakeAliyunProvider(
        cfg=Box({"name": "ali-1", "type": "aliyun"}, default_box=True),
        cdn_bindings=[
            CloudProductBinding(
                domain="api.example.com",
                product_type="cdn",
                product_id="p-1",
                cert_id="realtime-1",
                listener_port=443,
                metadata={"region": "cn-hangzhou"},
            )
        ],
    )
    match = _make_match("api.example.com", cert_id=None)
    notifier = _FakeNotifier()
    skipped: list = []

    verified = asyncio.run(
        svc._verify_matches_before_apply(
            providers=[provider],
            matches=[match],
            skipped=skipped,
            cache_enabled=True,
            ttl_seconds=3600,
            notifier=notifier,
        )
    )

    assert len(verified) == 1
    assert verified[0].current_cert_id == "realtime-1"
    assert verified[0].listener_port == 443
    assert verified[0].metadata == {"region": "cn-hangzhou"}
    assert skipped == []
    assert notifier.events == []


def test_verify_realtime_missing_skips_and_alerts(monkeypatch):
    """实时缺失目标：skip + 告警一次。"""
    svc = _make_deploy_service(monkeypatch, cache_enabled=True)
    provider = _FakeAliyunProvider(
        cfg=Box({"name": "ali-1", "type": "aliyun"}, default_box=True),
        cdn_bindings=[],  # 实时拉到空
    )
    match = _make_match("api.example.com")
    notifier = _FakeNotifier()
    skipped: list = []

    verified = asyncio.run(
        svc._verify_matches_before_apply(
            providers=[provider],
            matches=[match],
            skipped=skipped,
            cache_enabled=True,
            ttl_seconds=3600,
            notifier=notifier,
        )
    )

    assert verified == []
    assert len(skipped) == 1
    assert "未命中" in skipped[0]["reason"]
    assert len(notifier.events) == 1
    assert notifier.events[0].level == AlertLevel.WARNING
    assert notifier.events[0].source == "deploy"
    assert notifier.events[0].domain == "api.example.com"


def test_verify_realtime_exception_skips_provider_and_alerts(monkeypatch):
    """实时扫描抛异常：该 provider 下全部 match 均 skip + 告警一次。"""
    svc = _make_deploy_service(monkeypatch, cache_enabled=True)

    async def broken_get_provider_bindings(self, **kwargs):
        raise RuntimeError("scan failed")

    monkeypatch.setattr(
        deploy_service.DeployService,
        "_get_provider_bindings",
        broken_get_provider_bindings,
    )
    provider = _FakeAliyunProvider(
        cfg=Box({"name": "ali-1", "type": "aliyun"}, default_box=True),
    )
    matches = [_make_match("api.example.com"), _make_match("www.example.com")]
    notifier = _FakeNotifier()
    skipped: list = []

    verified = asyncio.run(
        svc._verify_matches_before_apply(
            providers=[provider],
            matches=matches,
            skipped=skipped,
            cache_enabled=True,
            ttl_seconds=3600,
            notifier=notifier,
        )
    )

    assert verified == []
    assert len(skipped) == 2
    for s in skipped:
        assert "实时校验失败" in s["reason"]
    # 整 provider 仅发一条告警
    assert len(notifier.events) == 1
    assert notifier.events[0].level == AlertLevel.WARNING


def test_apply_deploy_skips_verify_when_cache_disabled(monkeypatch):
    """cache_enabled=False 时 apply_deploy 不调用 verify。"""
    apply_config = Box(
        {
            "deploy": {
                "mode": "apply",
                "binding_cache": {
                    "enabled": False,
                    "ttl_seconds": 3600,
                    "verify_before_apply": True,  # 显式 true，但 cache_enabled=False 应当忽略
                },
            },
            "product_scan": {
                "aliyun": {"cdn_scan": True, "lb_scan": False, "oss_scan": False},
            },
        },
        default_box=True,
    )
    monkeypatch.setattr(deploy_service, "load_raw_config", lambda: apply_config)

    fake_provider_cfg = Box({"type": "aliyun", "name": "ali-1"}, default_box=True)

    class FakeProvider:
        def __init__(self):
            self.name = "ali-1"
            self.config = fake_provider_cfg

    fake_provider = FakeProvider()

    async def fake_collect_matches(self, renewed_cert, provider_name=None):
        return [fake_provider], {"api.example.com"}, [_make_match("api.example.com")], []

    monkeypatch.setattr(
        deploy_service.DeployService,
        "_collect_matches",
        fake_collect_matches,
    )

    verify_calls = {"count": 0}

    async def spy_verify(self, *args, **kwargs):
        verify_calls["count"] += 1
        return []

    monkeypatch.setattr(deploy_service.DeployService, "_verify_matches_before_apply", spy_verify)

    class FakeDeployer(CertificateDeployer):
        @property
        def name(self):
            return "aliyun"

        async def upload_certificate(self, cert_pem, key_pem, main_domain=None):
            return "new-1"

        async def deploy(self, cert_pem, key_pem, target, cert_id=None):
            return DeployResult(success=True, target=target, message="ok", cert_id=cert_id or "new-1")

        async def list_targets(self):
            return []

    monkeypatch.setattr(
        deploy_service,
        "build_cloud_deployer",
        lambda *args, **kwargs: FakeDeployer(),
    )

    planner = deploy_service.DeployService()
    asyncio.run(planner.apply_deploy(_renewed_cert()))

    # 关键断言：cache_enabled=False 时 verify 完全未被调用
    assert verify_calls["count"] == 0


def test_apply_deploy_tencent_scans_after_upload_and_reuses_certificate(monkeypatch):
    apply_config = Box(
        {
            "deploy": {
                "mode": "apply",
                "binding_cache": {
                    "enabled": False,
                    "ttl_seconds": 3600,
                    "verify_before_apply": False,
                },
            },
            "product_scan": {
                "tencent": {
                    "cdn_scan": True,
                    "live_scan": True,
                    "eo_scan": True,
                    "lb_scan": False,
                    "oss_scan": True,
                },
            },
        },
        default_box=True,
    )
    monkeypatch.setattr(deploy_service, "load_raw_config", lambda: apply_config)

    class FakeTencentProvider:
        name = "tencent-1"
        config = Box(
            {
                "type": "tencent",
                "name": "tencent-1",
                "credentials": {"secret_id": "sid", "secret_key": "key"},
            },
            default_box=True,
        )

        def __init__(self):
            self.scan_calls = []

        async def get_ssl_deploy_bindings(self, certificate_id, **scan_flags):
            self.scan_calls.append((certificate_id, scan_flags))
            return [
                CloudProductBinding(
                    provider_name=self.name,
                    product_type="oss",
                    product_id="ap-guangzhou:bucket-1:img.example.com",
                    domain="img.example.com",
                    cert_id=None,
                ),
                CloudProductBinding(
                    provider_name=self.name,
                    product_type="live",
                    product_id="live.example.com",
                    domain="outside.test",
                    cert_id=None,
                ),
                CloudProductBinding(
                    provider_name=self.name,
                    product_type="teo",
                    product_id="eo.example.com",
                    domain="eo.example.com",
                    cert_id="cert-old",
                ),
            ]

    provider = FakeTencentProvider()

    async def fake_collect_matches(self, renewed_cert, provider_name=None):
        return [provider], {"*.example.com"}, [], []

    monkeypatch.setattr(deploy_service.DeployService, "_collect_matches", fake_collect_matches)

    class FakeDeployer(CertificateDeployer):
        def __init__(self):
            self.upload_calls = []
            self.deploy_calls = []

        @property
        def name(self):
            return "tencent"

        async def upload_certificate(self, cert_pem, key_pem, main_domain=None):
            self.upload_calls.append((cert_pem, key_pem, main_domain))
            return "cert-new"

        async def deploy(self, cert_pem, key_pem, target, cert_id=None):
            self.deploy_calls.append((target, cert_id))
            return DeployResult(success=True, target=target, message="ok", cert_id=cert_id)

        async def list_targets(self):
            return []

    deployer = FakeDeployer()
    monkeypatch.setattr(deploy_service, "build_cloud_deployer", lambda **kwargs: deployer)

    async def no_nginx(self, renewed_cert):
        return []

    monkeypatch.setattr(deploy_service.DeployService, "_apply_nginx_deploy", no_nginx)

    result = asyncio.run(deploy_service.DeployService().apply_deploy(_renewed_cert()))

    assert len(deployer.upload_calls) == 1
    assert provider.scan_calls == [
        (
            "cert-new",
            {
                "cdn_scan": True,
                "live_scan": True,
                "eo_scan": True,
                "lb_scan": False,
                "oss_scan": True,
            },
        )
    ]
    assert {item[0].product_type for item in deployer.deploy_calls} == {"oss", "live", "teo"}
    assert {item[0].domain for item in deployer.deploy_calls} == {
        "img.example.com",
        "outside.test",
        "eo.example.com",
    }
    assert all(item[0].metadata["match_type"] == "ssl-domain-match" for item in deployer.deploy_calls)
    assert all(item[1] == "cert-new" for item in deployer.deploy_calls)
    assert result["success_count"] == 3


def test_apply_deploy_without_any_target_fails(monkeypatch):
    apply_config = Box(
        {
            "deploy": {
                "mode": "apply",
                "binding_cache": {
                    "enabled": False,
                    "ttl_seconds": 3600,
                    "verify_before_apply": False,
                },
            },
            "product_scan": {
                "tencent": {
                    "cdn_scan": False,
                    "live_scan": False,
                    "eo_scan": False,
                    "lb_scan": False,
                    "oss_scan": False,
                },
            },
        },
        default_box=True,
    )
    monkeypatch.setattr(deploy_service, "load_raw_config", lambda: apply_config)

    async def fake_collect_matches(self, renewed_cert, provider_name=None):
        return [], {"*.example.com"}, [], []

    async def no_nginx(self, renewed_cert):
        return []

    monkeypatch.setattr(deploy_service.DeployService, "_collect_matches", fake_collect_matches)
    monkeypatch.setattr(deploy_service.DeployService, "_apply_nginx_deploy", no_nginx)

    with pytest.raises(RuntimeError, match="未发现可部署"):
        asyncio.run(deploy_service.DeployService().apply_deploy(_renewed_cert()))
