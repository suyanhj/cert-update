import asyncio
from types import SimpleNamespace

from app.services.deployer_factory import build_cloud_deployer
from app.services.provider_registry import ProviderRegistry


def test_provider_registry_filters_disabled_groups_and_static_domains():
    configs = [
        SimpleNamespace(name="static", type="custom", enabled=True, static_domains=[{"domain": "a.example.com"}]),
        SimpleNamespace(name="cloud", type="Tencent", enabled=True, static_domains=[]),
        SimpleNamespace(name="disabled", type="custom", enabled=False, static_domains=[{"domain": "ignored.example.com"}]),
    ]
    built = []
    registry = ProviderRegistry(configs, lambda cfg: built.append(cfg.name) or SimpleNamespace(name=cfg.name))
    assert built == ["static", "cloud"]
    assert registry.configs == configs[:2]
    assert registry.static_nums == 1
    assert registry.cloud_nums == 1
    assert registry.static_providers[0].name == "static"
    assert registry.cloud_providers[0].name == "cloud"
    assert registry.get_provider("cloud").name == "cloud"
    assert registry.get_static_provider("cloud") is None
    assert registry.get_cloud_provider("static") is None
    assert registry.static_domains == ["a.example.com"]


def test_provider_registry_duplicate_names_use_last_provider():
    configs = [
        SimpleNamespace(name="same", type="custom", enabled=True, static_domains=[]),
        SimpleNamespace(name="same", type="qiniu", enabled=True, static_domains=[]),
    ]
    providers = [SimpleNamespace(name="same"), SimpleNamespace(name="same")]
    registry = ProviderRegistry(configs, lambda cfg: providers.pop(0))
    assert registry.get_provider("same") is registry.providers[-1]


def test_deployer_factory_validates_credentials_and_selects_types(monkeypatch):
    import app.services.deployer_factory as mod

    captured = []

    class FakeDeployer:
        def __init__(self, **kwargs):
            captured.append(kwargs)

    for name in ("AliyunDeployer", "HuaweiDeployer", "TencentDeployer", "VolcengineDeployer", "QiniuDeployer"):
        monkeypatch.setattr(mod, name, FakeDeployer)

    cases = [
        ("aliyun", {"access_key_id": "a", "access_key_secret": "b", "region": "cn-beijing"}, {"region": "cn-beijing"}),
        ("huawei", {"access_key_id": "a", "access_key_secret": "b"}, {"project_id": "", "region": "cn-north-4", "enterprise_project_id": "0"}),
        ("tencent", {"secret_id": "a", "secret_key": "b"}, {"region": "ap-guangzhou"}),
        ("volcengine", {"access_key_id": "a", "access_key_secret": "b"}, {"region": "cn-north-1"}),
        ("qiniu", {"access_key_id": "a", "access_key_secret": "b"}, {}),
    ]
    for provider_type, creds, expected in cases:
        captured.clear()
        build_cloud_deployer(provider_type, "p", creds)
        assert captured[0].items() >= expected.items()

    for provider_type, creds in [
        ("aliyun", {"access_key_id": "a"}),
        ("huawei", {"access_key_id": "a"}),
        ("tencent", {"secret_id": "a"}),
        ("volcengine", {"access_key_id": "a"}),
        ("qiniu", {"access_key_id": "a"}),
    ]:
        try:
            build_cloud_deployer(provider_type, "p", creds)
        except RuntimeError as exc:
            assert "missing access credentials" in str(exc)
        else:
            raise AssertionError("missing credentials should fail")

    try:
        build_cloud_deployer("unknown", "p", {})
    except RuntimeError as exc:
        assert "unsupported" in str(exc)
    else:
        raise AssertionError("unsupported provider should fail")


def test_certificates_domain_selection_and_alert_levels(monkeypatch):
    import app.services.certs as mod

    cfg = SimpleNamespace(
        providers=[
            SimpleNamespace(
                check_extensions=True,
                sub_extensions=[{"domain": "www.example.com", "redirect_to": "origin.example.com"}],
            ),
        ],
        alert=SimpleNamespace(
            cert=SimpleNamespace(enabled=True, cert_warn_days=7, cert_expiry_days=30)
        ),
        whitelist=[],
    )
    monkeypatch.setattr(mod, "load_raw_config", lambda: cfg)

    class State:
        def __init__(self):
            self.firing = set()
            self.recovered = []
            self.fired = []
        def recover(self, key): self.recovered.append(key); self.firing.discard(key)
        def is_firing(self, key): return key in self.firing
        def fire(self, key): self.fired.append(key); self.firing.add(key)
        def get_all(self): return {key: "FIRING" for key in self.firing}

    state = State()
    monkeypatch.setattr(mod.store, "AlertStateStore", lambda path: state)
    monkeypatch.setattr(mod.notifier, "DingDingNotifier", lambda: object())
    monkeypatch.setattr(mod.notifier, "AlertBatcher", lambda: SimpleNamespace(add=lambda event: None, flush_alerts=lambda notifier: asyncio.sleep(0), clear=lambda: None))

    c = mod.Certificates()
    assert c.get_domains([{"domain": "example.com", "subs": [{"name": "www.example.com", "status": "在工作"}]}]) == {"origin.example.com"}
    assert c.get_domains([{"domain": "example.com", "subs": []}]) == {"example.com"}
    assert c.get_domains([{"domain": "example.com", "subs": [{"name": "dead.example.com", "status": "异常"}]}]) == set()

    events = []
    c.alert_batch.add = lambda event: events.append(event)
    asyncio.run(c.cert_alert({"domain": "a.example.com", "days": 5, "provider": "p", "exp": "2030"}))
    assert len(events) == 1
    assert events[0].level.name == "CRITICAL"

    events.clear()
    asyncio.run(c.cert_alert({"domain": "b.example.com", "days": 20, "provider": "p", "exp": "2030"}))
    assert len(events) == 1
    assert events[0].level.name == "WARNING"

    asyncio.run(c.cert_alert({"domain": "c.example.com", "days": 100, "provider": "p", "exp": "2030"}))
    assert "c.example.com" in state.recovered

    cfg.whitelist = ["white.example.com"]
    asyncio.run(c.cert_alert({"domain": "white.example.com", "days": 1, "provider": "p", "exp": "2030"}))
    assert events and all(e.domain != "white.example.com" for e in events)


def test_certificates_get_certificate_list_flushes_and_recovers_missing(monkeypatch):
    import app.services.certs as mod

    cfg = SimpleNamespace(
        providers=[],
        alert=SimpleNamespace(cert=SimpleNamespace(enabled=True, cert_warn_days=7, cert_expiry_days=30)),
        whitelist=[],
    )
    monkeypatch.setattr(mod, "load_raw_config", lambda: cfg)

    state = SimpleNamespace(
        recovered=[],
        fired=[],
        firing=set(),
        get_all=lambda: {"missing.example.com": "FIRING"},
        recover=lambda key: state.recovered.append(key),
        fire=lambda key: state.fired.append(key),
        is_firing=lambda key: False,
    )
    monkeypatch.setattr(mod.store, "AlertStateStore", lambda path: state)
    monkeypatch.setattr(mod.notifier, "DingDingNotifier", lambda: object())

    class Batch:
        def __init__(self): self.clear_count = 0
        def add(self, event): pass
        async def flush_alerts(self, notifier): pass
        def clear(self): self.clear_count += 1
    batch = Batch()
    monkeypatch.setattr(mod.notifier, "AlertBatcher", batch.__class__)

    c = mod.Certificates()
    c.alert_batch = batch
    c._tls_probe = SimpleNamespace(
        probe_domains_batch=lambda domains: asyncio.sleep(
            0,
            result={"a.example.com": {"days": 5, "provider": "p", "exp": "2030"}}
        )
    )
    c.get_domains = lambda domains: {"a.example.com"}
    c._domain_store.resolve = lambda domain: "p"
    c.cert_alert = lambda cert: asyncio.sleep(0)

    result = asyncio.run(c.get_certificate_list([{"domain": "a.example.com"}]))
    assert result[0]["domain"] == "a.example.com"
    assert result[0]["provider"] == "p"
    assert "missing.example.com" in state.recovered
    assert batch.clear_count == 1


def test_deployer_factory_huawei_project_and_defaults(monkeypatch):
    import app.services.deployer_factory as mod

    captured = {}
    class Fake:
        def __init__(self, **kwargs):
            captured.update(kwargs)
    monkeypatch.setattr(mod, "HuaweiDeployer", Fake)
    build_cloud_deployer(
        "huawei", "p",
        {"access_key_id": "ak", "access_key_secret": "sk"},
    )
    assert captured["region"] == "cn-north-4"
    assert captured["enterprise_project_id"] == "0"
