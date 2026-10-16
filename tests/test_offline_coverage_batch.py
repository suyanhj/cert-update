import asyncio
from types import SimpleNamespace

import pytest

from app.deploys.base import DeployTarget
from app.deploys.qiniu import QiniuDeployer
from app.providers.base import Provider
from app.services.ecs_alert import EcsAlertService
from app.schemas.alert import AlertLevel
from app.utils import cert_parser, volcengine_sdk


def test_provider_base_helpers_and_probe(monkeypatch):
    assert Provider._extract_error_field("Error Code: Foo.\nMessage: Bar.", "error code") == "Foo"
    assert Provider._extract_error_field("nope", "Code") is None
    assert Provider._build_exception_summary(RuntimeError("ErrorCode: E123\nError Message: hello")) == "code=E123, reason=hello"
    assert Provider._build_exception_summary(RuntimeError("plain failure")) == "plain failure"
    assert Provider._is_cloud_service_not_enabled(RuntimeError("Error Code: CdnServiceNotFound")) is True
    assert Provider._is_cloud_service_not_enabled(RuntimeError("service has not been opened")) is True
    assert Provider._is_cloud_service_not_enabled(RuntimeError("普通错误")) is False

    class P(Provider):
        async def get_domain_list(self):
            return [{"domain": "example.com"}, {"domain": "bad..domain"}]

    cfg = SimpleNamespace(name="p", sub_extensions=[{"domain": "www.example.com", "redirect_to": "origin.example.com"}], check_extensions=True)
    p = object.__new__(P)
    p.config = cfg
    p.logger = SimpleNamespace(debug=lambda *a, **k: None, warning=lambda *a, **k: None, error=lambda *a, **k: None)
    p.http_client = None
    assert p.get_sub_extensions() == {"www.example.com": "origin.example.com"}
    assert asyncio.run(p.is_domain_provider("www.example.com")) is True
    assert asyncio.run(p.is_domain_provider("bad..domain")) is False

    class Resp:
        def __init__(self, code): self.status_code = code
    class Http:
        def __init__(self): self.calls = []
        async def fetch(self, **kwargs):
            self.calls.append(kwargs["url"])
            if len(self.calls) == 1:
                raise RuntimeError("http down")
            return Resp(200)
    p.http_client = Http()
    assert asyncio.run(p._request("example.com")) == "在工作"
    assert p.http_client.calls == ["http://example.com", "https://example.com"]

    cfg.check_extensions = False
    assert asyncio.run(p._probe_domain("*.example.com"))["status"] == "通配符跳过"
    cfg.check_extensions = True
    p.http_client = Http()
    assert asyncio.run(p._probe_domain("www.example.com"))["status"] == "在工作"


def test_provider_api_retry_service_not_enabled_and_async():
    class P(Provider):
        pass
    p = object.__new__(P)
    p.config = SimpleNamespace(name="p", sub_extensions=[], check_extensions=False)
    logs = []
    p.logger = SimpleNamespace(
        debug=lambda *a, **k: logs.append(("debug", a)),
        warning=lambda *a, **k: logs.append(("warning", a)),
        error=lambda *a, **k: logs.append(("error", a)),
    )

    state = {"n": 0}
    def flaky():
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("temporary")
        return "ok"
    assert asyncio.run(p._call_provider_api("op", flaky, default=[], retries=1, retry_delay_seconds=0)) == "ok"

    assert asyncio.run(p._call_provider_api(
        "op", lambda: (_ for _ in ()).throw(RuntimeError("Error Code: CdnServiceNotFound")),
        default=["empty"], raise_on_error=False
    )) == ["empty"]

    with pytest.raises(RuntimeError, match="op 失败"):
        asyncio.run(p._call_provider_api(
            "op", lambda: (_ for _ in ()).throw(RuntimeError("bad")),
            default=[], raise_on_error=True
        ))

    async def async_fn():
        return 42
    assert asyncio.run(p._call_provider_api("async", async_fn, default=0)) == 42


def test_qiniu_deployer_offline_matrix(monkeypatch):
    d = QiniuDeployer("ak", "sk")
    auth = object()
    monkeypatch.setattr("app.deploys.qiniu.build_qiniu_auth", lambda ak, sk: auth)
    assert d._get_auth() is auth
    assert d._get_auth() is auth

    with pytest.raises(ValueError):
        QiniuDeployer._validate_target(DeployTarget("qiniu", "cdn"))
    assert QiniuDeployer._validate_target(DeployTarget("qiniu", "cdn", domain="A.example.com")) == "A.example.com"
    with pytest.raises(ValueError):
        QiniuDeployer._validate_target(DeployTarget("qiniu", "oss", product_id="bucket"))
    with pytest.raises(ValueError):
        QiniuDeployer._validate_target(DeployTarget("qiniu", "oss", product_id="bucket:a.example.com", domain="b.example.com"))
    with pytest.raises(ValueError):
        QiniuDeployer._validate_target(DeployTarget("qiniu", "bad"))

    calls = []
    monkeypatch.setattr(d, "_make_request", lambda *args, **kwargs: calls.append((args, kwargs)) or {"certID": "c1"})
    assert asyncio.run(d._upload_certificate("CERT", "KEY", "name")) == "c1"
    with pytest.raises(ValueError):
        asyncio.run(d._upload_certificate("", "KEY", "name"))
    monkeypatch.setattr(d, "_make_request", lambda *args, **kwargs: {"x": 1})
    with pytest.raises(RuntimeError):
        asyncio.run(d._upload_certificate("CERT", "KEY", "name"))

    calls.clear()
    monkeypatch.setattr(d, "_make_request", lambda *args, **kwargs: calls.append((args, kwargs)) or {})
    asyncio.run(d._deploy_to_cdn("c1", DeployTarget("qiniu", "cdn", domain="x"), "x.example.com"))
    assert calls[0][0][0].endswith("/x.example.com/httpsconf")
    calls.clear()
    asyncio.run(d._deploy_to_cdn("c1", DeployTarget("qiniu", "cdn", metadata={"protocol": "http"}), "a.example.com"))
    assert calls[0][0][0].endswith("/a.example.com/sslize")
    assert calls[0][0][2]["forceHttps"] is False

    d._make_request = lambda *args, **kwargs: {"certs": [{"certID": "1"}], "marker": "m1"}
    assert asyncio.run(d.list_certificates()) == [{"certID": "1"}]
    d._make_request = lambda *args, **kwargs: {"certs": [], "marker": "m1"}
    assert asyncio.run(d.list_certificates()) == []

    monkeypatch.setattr("app.deploys.qiniu.list_qiniu_domains", lambda auth: [
        {"name": "a.example.com", "product": "cdn", "protocol": "HTTPS", "operatingState": "online", "https": {"cert": "c"}},
        {"name": "bucket", "product": "objectStorage"},
        {"name": "", "product": "cdn"},
    ])
    targets = asyncio.run(d.list_bindable_resources())
    assert len(targets) == 1 and targets[0].domain == "a.example.com"

    monkeypatch.setattr(d, "_make_request", lambda *args, **kwargs: {})
    assert asyncio.run(d.delete_certificate("c/1")) is True
    with pytest.raises(ValueError):
        asyncio.run(d.delete_certificate(""))

    monkeypatch.setattr(d, "_make_request", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    assert asyncio.run(d.delete_certificate("c1")) is False


def test_volcengine_sdk_offline_adapters(monkeypatch):
    class Model:
        attribute_map = {"foo": "Foo", "nested": "Nested"}
        def to_dict(self):
            return {"foo": "bar", "nested": {"x": 1}}
    assert volcengine_sdk._to_compat_dict([Model(), {"a": Model()}])[0]["Foo"] == "bar"
    assert volcengine_sdk._to_compat_dict(Model())["Nested"] == {"x": 1}
    assert volcengine_sdk._to_compat_dict("x") == "x"

    fake_module = SimpleNamespace(MyApi=lambda api_client: SimpleNamespace(ping=lambda x: {"ok": x}))
    monkeypatch.setattr(volcengine_sdk.importlib, "import_module", lambda path: fake_module)
    assert volcengine_sdk._resolve_service_class("custom.module", "MyApi") is fake_module.MyApi
    with pytest.raises(ValueError):
        volcengine_sdk._resolve_service_class("unknown.module")
    fake_missing = SimpleNamespace()
    monkeypatch.setattr(volcengine_sdk.importlib, "import_module", lambda path: fake_missing)
    with pytest.raises(AttributeError):
        volcengine_sdk._resolve_service_class("custom.module", "MyApi")

    class FakeConfig:
        def __init__(self): self.ak = self.sk = self.region = None
    class FakeCore:
        Configuration = FakeConfig
        class ApiClient:
            def __init__(self, config): self.config = config
    monkeypatch.setattr(volcengine_sdk, "volcenginesdkcore", FakeCore)
    assert volcengine_sdk.build_credentials("a", "s", "dns", "cn") .region == "cn"

    class Client:
        def __init__(self, **kwargs): self.kwargs = kwargs
        def ping(self, x): return Model()
    adapter = volcengine_sdk._VolcengineServiceAdapter(Client())
    assert adapter.ping("x")["Foo"] == "bar"
    assert adapter.kwargs == adapter._client.kwargs


def test_ecs_alert_service_matrix():
    class Logger:
        def __init__(self): self.logs=[]
        def warning(self,*a,**k): self.logs.append(("warning",a))
        def error(self,*a,**k): self.logs.append(("error",a))
    class State:
        def __init__(self): self.states={}; self.recovered=[]; self.fired=[]
        def recover(self,k): self.recovered.append(k); self.states.pop(k,None)
        def is_firing(self,k): return k in self.states
        def fire(self,k): self.states[k]=1; self.fired.append(k)
        def get_all(self): return dict(self.states)
    class Batch:
        def __init__(self): self.events=[]; self.flushes=0; self.clears=0
        def add(self,e): self.events.append(e)
        async def flush_alerts(self, notify): self.flushes += 1
        def clear(self): self.clears += 1

    cfg = SimpleNamespace(alert=SimpleNamespace(ecs=SimpleNamespace(enabled=True, ecs_warn_days=3, ecs_expiry_days=30)))
    log, state, batch = Logger(), State(), Batch()
    s = EcsAlertService(cfg, log, lambda: None, state, batch)
    instances = [
        {"provider": "huawei", "instance_id": "h1", "days": 1},
        {"provider": "aliyun", "instance_id": "a1", "days": 2, "instance_name": "A", "region": "cn", "provider_name": "prod"},
        {"provider": "aliyun", "instance_id": "a2", "days": 10, "auto_renewal": True},
        {"provider": "aliyun", "instance_id": "a3", "days": "bad"},
        {"provider": "aliyun", "instance_id": "a4", "days": 50},
        {"provider": "aliyun", "instance_id": "a5", "days": 20, "instance_kind": "swas"},
        {"provider": "aliyun", "instance_id": "a6", "days": 2},
    ]
    asyncio.run(s.send_ecs_alerts(instances))
    assert batch.flushes == 2
    assert "aliyun:prod:a1" in state.fired
    assert any("未知" not in str(x) for x in log.logs)

    cfg.alert.ecs.enabled = False
    asyncio.run(s.send_ecs_alerts(instances))
