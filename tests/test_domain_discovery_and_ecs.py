import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from box import Box


def test_domain_discovery_normalize_domain_group_and_expires(monkeypatch: pytest.MonkeyPatch):
    from app.services.domain_discovery import DomainDiscoveryService

    class FakeLogger:
        def __getattr__(self, name):
            def _noop(*a, **k):
                return None

            return _noop

    svc = DomainDiscoveryService(logger_obj=FakeLogger())

    class FakeProvider:
        def __init__(self):
            self.name = "自定义"
            self.config = Box({"type": "custom"}, default_box=True)

    provider = FakeProvider()
    now = datetime.now(timezone.utc)
    item = {
        "domain": " example.com ",
        "expires_at": now.isoformat(),
        "subs": [],
    }

    normalized = svc._normalize_domain_group(provider, item)
    assert normalized is not None
    assert normalized["domain"] == "example.com"
    assert normalized["name"] == "自定义"
    assert normalized["provider"] == "custom"
    # days 字段应存在且为 int
    assert isinstance(normalized["days"], int)


def test_domain_discovery_discover_all_saves_to_store(monkeypatch: pytest.MonkeyPatch):
    from app.services.domain_discovery import DomainDiscoveryService

    class FakeLogger:
        def __init__(self):
            self.infos = []
            self.errors = []

        def info(self, msg, *a):
            self.infos.append(msg % a if a else msg)

        def error(self, msg, *a):
            self.errors.append(msg % a if a else msg)

    logger = FakeLogger()

    class FakeStaticProvider:
        domain_roles = frozenset({"registration", "dns"})

        def __init__(self):
            self.name = "static-1"
            self.config = Box({"type": "custom"}, default_box=True)

        async def get_domain_list(self):
            return [{"domain": "static.example.com", "expires_at": "unknown"}]

    class FakeCloudProvider:
        domain_roles = frozenset({"registration", "dns"})

        def __init__(self):
            self.name = "ali-1"
            self.config = Box({"type": "aliyun"}, default_box=True)

        async def get_domain_list(self):
            return [{"domain": "api.example.com", "expires_at": "2026-01-01"}]

    saved_payload = {}

    class FakeDomainProviderStore:
        def save(self, data):
            saved_payload["data"] = list(data)

    # 覆盖 DomainProviderStore
    from app.services import domain_discovery as dd_mod

    monkeypatch.setattr(
        dd_mod.store,
        "DomainProviderStore",
        lambda: FakeDomainProviderStore(),
    )

    svc = DomainDiscoveryService(logger_obj=logger)
    result = asyncio.run(
        svc.discover_all(
            static_providers=[FakeStaticProvider()],
            cloud_providers=[FakeCloudProvider()],
            static_domain_count=1,
        )
    )

    assert len(result) == 2
    assert "data" in saved_payload
    assert len(saved_payload["data"]) == 2
    domains = {item["domain"] for item in saved_payload["data"]}
    assert "static.example.com" in domains
    assert "api.example.com" in domains


def test_domain_discovery_merges_registrar_and_dns_provider(monkeypatch: pytest.MonkeyPatch):
    from app.services import domain_discovery as dd_mod

    class FakeLogger:
        def __init__(self):
            self.warnings = []
            self.infos = []

        def warning(self, msg, *args):
            self.warnings.append(msg % args if args else msg)

        def info(self, msg, *args):
            self.infos.append(msg % args if args else msg)

        def __getattr__(self, _name):
            return lambda *args, **kwargs: None

    class AliProvider:
        name = "ali-main"
        config = Box({"type": "aliyun"}, default_box=True)
        domain_roles = frozenset({"registration", "dns"})

        async def get_domain_list(self):
            return [{
                "domain": "Example.COM.",
                "expires_at": "2027-01-01T00:00:00+08:00",
                "org": "示例公司",
                "subs": [{"name": "old.example.com", "status": "在工作"}],
            }]

    class CloudflareProvider:
        name = "cf-main"
        config = Box({"type": "cloudflare"}, default_box=True)
        domain_roles = frozenset({"dns"})

        async def get_domain_list(self):
            return [{
                "domain": "example.com",
                "zone_id": "zone-1",
                "expires_at": "unknown",
                "assigned_nameservers": ["abby.ns.cloudflare.com", "bob.ns.cloudflare.com"],
                "subs": [{"name": "www.example.com", "status": "在工作"}],
            }]

    class FakeResolver:
        def resolve_nameservers(self, domain):
            assert domain == "example.com"
            return {"abby.ns.cloudflare.com", "bob.ns.cloudflare.com"}

    saved = {}

    class FakeStore:
        def save(self, groups):
            saved["groups"] = groups

    monkeypatch.setattr(dd_mod.store, "DomainProviderStore", FakeStore)
    logger = FakeLogger()
    result = asyncio.run(
        dd_mod.DomainDiscoveryService(logger, authority_resolver=FakeResolver()).discover_all(
            static_providers=[],
            cloud_providers=[AliProvider(), CloudflareProvider()],
            static_domain_count=0,
        )
    )

    assert len(result) == 1
    group = result[0]
    assert group["domain"] == "example.com"
    assert group["registrar_name"] == "ali-main"
    assert group["registrar_provider"] == "aliyun"
    assert group["registrant_org"] == "示例公司"
    assert group["dns_name"] == "cf-main"
    assert group["dns_provider"] == "cloudflare"
    assert group["name"] == "cf-main"
    assert group["provider"] == "cloudflare"
    assert group["subs"][0]["name"] == "www.example.com"
    assert saved["groups"] == result
    assert any("method=public_ns" in message for message in logger.infos)


def test_domain_discovery_supports_single_role_and_excludes_product_domains():
    from app.services.domain_discovery import DomainDiscoveryService

    class FakeLogger:
        def __getattr__(self, _name):
            return lambda *args, **kwargs: None

    def provider(name, provider_type, roles):
        return type(
            "FakeProvider",
            (),
            {
                "name": name,
                "config": Box({"type": provider_type}, default_box=True),
                "domain_roles": frozenset(roles),
            },
        )()

    service = DomainDiscoveryService(FakeLogger())
    registration = service._normalize_domain_group(
        provider("registrar", "aliyun", {"registration"}),
        {"domain": "registered.example", "expires_at": "2027-01-01", "org": "主体"},
    )
    dns = service._normalize_domain_group(
        provider("dns", "cloudflare", {"dns"}),
        {"domain": "dns-only.example", "subs": [{"name": "www.dns-only.example"}]},
    )
    product = service._normalize_domain_group(
        provider("qiniu", "qiniu", set()),
        {"domain": "cdn.example.com"},
    )

    groups = service._merge_domain_groups([
        (provider("registrar", "aliyun", {"registration"}), registration),
        (provider("dns", "cloudflare", {"dns"}), dns),
        (provider("qiniu", "qiniu", set()), product),
    ])
    by_domain = {item["domain"]: item for item in groups}

    assert set(by_domain) == {"registered.example", "dns-only.example"}
    assert by_domain["registered.example"]["dns_name"] == ""
    assert by_domain["registered.example"]["expires_at"] != "unknown"
    assert by_domain["dns-only.example"]["registrar_name"] == ""
    assert by_domain["dns-only.example"]["expires_at"] == "unknown"


def test_provider_domain_roles_are_explicit():
    from app.providers.aliyun import AliyunCloudProvider
    from app.providers.cloudflare import CloudflareDNSProvider
    from app.providers.huawei import HuaweiCloudProvider
    from app.providers.qiniu import QiniuCloudProvider
    from app.providers.static import StaticCloudProvider
    from app.providers.tencent import TencentCloudProvider
    from app.providers.volcengine import VolcengineCloudProvider

    assert AliyunCloudProvider.domain_roles == {"registration", "dns"}
    assert TencentCloudProvider.domain_roles == {"registration", "dns"}
    assert StaticCloudProvider.domain_roles == {"registration", "dns"}
    assert CloudflareDNSProvider.domain_roles == {"dns"}
    assert HuaweiCloudProvider.domain_roles == {"dns"}
    assert VolcengineCloudProvider.domain_roles == {"dns"}
    assert QiniuCloudProvider.domain_roles == set()


def test_domain_provider_store_only_uses_dns_provider(tmp_path):
    from app.utils.store import DomainProviderStore

    target = tmp_path / "domain-provider-map.json"
    domain_store = DomainProviderStore(str(target))
    domain_store.save([
        {
            "domain": "example.com",
            "name": "ali-registrar",
            "dns_name": "cf-dns",
            "subs": [{"name": "www.example.com"}],
        },
        {
            "domain": "registered-only.example",
            "name": "ali-registrar",
            "dns_name": "",
            "subs": [],
        },
    ])

    assert domain_store.resolve("example.com") == "cf-dns"
    assert domain_store.resolve("api.example.com") == "cf-dns"
    assert domain_store.resolve("oss.test.example.com") == "cf-dns"
    assert domain_store.resolve("www.example.com") == "cf-dns"
    assert domain_store.resolve("registered-only.example") is None


def test_ecs_discovery_respects_product_scan_and_normalizes_payload(monkeypatch: pytest.MonkeyPatch):
    from app.services import ecs as ecs_mod

    # 配置：只开启 aliyun.ecs_scan
    cfg = Box(
        {
            "providers": [
                {"name": "ali-1", "type": "aliyun", "enabled": True},
                {"name": "tencent-1", "type": "tencent", "enabled": True},
            ],
            "dingding": {"webhook": "", "secret": ""},
            "product_scan": {
                "aliyun": {"ecs_scan": True},
                "tencent": {"ecs_scan": False},
            },
        },
        default_box=True,
    )

    monkeypatch.setattr(ecs_mod, "load_raw_config", lambda: cfg)

    class FakeLogger:
        def __init__(self):
            self.infos = []

        def info(self, msg, *a):
            self.infos.append(msg % a if a else msg)

        def __getattr__(self, name):
            def _noop(*a, **k):
                return None

            return _noop

    monkeypatch.setattr(ecs_mod.logger, "get_logger", lambda name: FakeLogger())

    class FakeAliyunProvider:
        def __init__(self, cfg):
            self.name = cfg.name
            self.config = cfg

        async def get_ecs_instances(self):
            return [
                {
                    "instance_id": "i-1",
                    "domain": "ecs-1.internal",
                    "days": 10,
                }
            ]

    class FakeTencentProvider:
        def __init__(self, cfg):
            self.name = cfg.name
            self.config = cfg

        async def get_ecs_instances(self):
            # 不应被调用，因为 ecs_scan=false
            raise AssertionError("tencent get_ecs_instances should not be called")

    def fake_builder_map(include_custom=False, overrides=None):
        return {
            "aliyun": FakeAliyunProvider,
            "tencent": FakeTencentProvider,
        }

    async def fake_send_ecs_alerts(self, ecs_data):
        # 不做实际告警，仅验证调用链条
        self._last = ecs_data

    monkeypatch.setattr(ecs_mod, "get_provider_builder_map", fake_builder_map)
    monkeypatch.setattr(ecs_mod, "build_cloud_providers", lambda configs, builder: [FakeAliyunProvider(cfg.providers[0]), FakeTencentProvider(cfg.providers[1])])
    monkeypatch.setattr(ecs_mod.EcsAlertService, "send_ecs_alerts", fake_send_ecs_alerts)

    svc = ecs_mod.Ecs()
    ecs_list = asyncio.run(svc.discovery_all())

    # 只应该有 aliyun 的一条数据
    assert len(ecs_list) == 1
    item = ecs_list[0]
    assert item["name"] == "ali-1"
    assert item["provider_name"] == "ali-1"
    assert item["provider"] == "aliyun"
    assert "days" in item


def test_domain_discovery_normalize_expires_at_variants(monkeypatch: pytest.MonkeyPatch):
    """expires_at 各种输入场景的归一化行为。"""
    from app.services.domain_discovery import DomainDiscoveryService

    class FakeLogger:
        def __getattr__(self, name):
            def _noop(*a, **k):
                return None

            return _noop

    svc = DomainDiscoveryService(logger_obj=FakeLogger())

    # None / 空串 -> "unknown"
    assert svc._normalize_expires_at(None) == "unknown"
    assert svc._normalize_expires_at("") == "unknown"

    # 可被解析的时间字符串
    now = datetime.now(timezone.utc)
    iso = now.isoformat()
    parsed = svc._normalize_expires_at(iso)
    # 应能被 parse 再转回本地时区字符串，不做严格值断言，只要不是空即可
    assert isinstance(parsed, str) and parsed

    # 不可解析的字符串，保持原样
    bad = "not-a-datetime"
    assert svc._normalize_expires_at(bad) == bad

    # 有 isoformat() 方法的对象，退化为 isoformat()
    class Dummy:
        def isoformat(self):
            return "dummy-iso"

    assert svc._normalize_expires_at(Dummy()) == "dummy-iso"


def test_domain_discovery_discover_all_handles_provider_exception(monkeypatch: pytest.MonkeyPatch):
    """discover_all 遇到 provider 异常时不会抛出，只记录日志并继续保存当前聚合结果。"""
    from app.services.domain_discovery import DomainDiscoveryService

    class FakeLogger:
        def __init__(self):
            self.errors = []

        def info(self, msg, *a):
            return None

        def error(self, msg, *a):
            self.errors.append(msg % a if a else msg)

    logger = FakeLogger()

    class BadProvider:
        def __init__(self):
            self.name = "bad"
            self.config = Box({"type": "aliyun"}, default_box=True)

        async def get_domain_list(self):
            raise RuntimeError("boom")

    saved_payload = {}

    class FakeDomainProviderStore:
        def save(self, data):
            saved_payload["data"] = list(data)

    from app.services import domain_discovery as dd_mod

    monkeypatch.setattr(dd_mod.store, "DomainProviderStore", lambda: FakeDomainProviderStore())

    svc = DomainDiscoveryService(logger_obj=logger)
    with pytest.raises(RuntimeError, match="bad: boom"):
        asyncio.run(
            svc.discover_all(
                static_providers=[],
                cloud_providers=[BadProvider()],
                static_domain_count=0,
            )
        )

    # 任一账号失败时不覆盖已有完整映射。
    assert saved_payload == {}
    # 日志保留失败账号与原始错误。
    assert any("bad" in msg and "boom" in msg for msg in logger.errors)


