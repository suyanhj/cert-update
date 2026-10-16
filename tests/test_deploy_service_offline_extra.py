import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services.deploy import DeployService, DeploymentError
from app.schemas.provider import CloudProductBinding, DeployMatchResult
from app.schemas.acme import RenewedCert


def service():
    d = object.__new__(DeployService)
    d.conf = SimpleNamespace(
        deploy=SimpleNamespace(binding_cache=SimpleNamespace(enabled=True, ttl_seconds=3600, verify_before_apply=True), mode="dry-run"),
        providers=[],
        product_scan=SimpleNamespace(
            fake=SimpleNamespace(cdn_scan=True, live_scan=True, lb_scan=True, oss_scan=True, waf_scan=True),
            tencent=SimpleNamespace(cdn_scan=False, live_scan=False, eo_scan=False, lb_scan=False, oss_scan=False),
        ),
        ssh_profiles=[],
        nginx=None,
    )
    d._binding_store = SimpleNamespace(get_provider=lambda key: None)
    return d


def test_deploy_service_static_helpers_and_nginx_auth():
    d = service()
    assert d._binding_cache_settings() == (True, 3600, True)
    d.conf.deploy.binding_cache = None
    assert d._binding_cache_settings() == (True, 3600, True)
    d.conf.deploy.binding_cache = SimpleNamespace(enabled="", ttl_seconds=0, verify_before_apply="")
    with pytest.raises(ValueError):
        d._binding_cache_settings()
    d.conf.deploy.binding_cache = SimpleNamespace(enabled=False, ttl_seconds=10, verify_before_apply=False)
    assert d._binding_cache_settings() == (False, 10, False)

    now = datetime.now(timezone.utc)
    assert d._cache_is_valid("", 10) is False
    assert d._cache_is_valid("bad", 10) is False
    assert d._cache_is_valid((now - timedelta(seconds=5)).isoformat(), 10) is True
    assert d._cache_is_valid((now + timedelta(seconds=5)).isoformat(), 10) is True
    assert d._binding_key(" CDN ", "id", " Example.COM. ") == ("cdn", "id", "example.com")

    p = SimpleNamespace(config=SimpleNamespace(type="Aliyun", name="p"), name="p")
    assert d._provider_type(p) == "aliyun"
    assert d._provider_name(p) == "p"
    assert d._provider_cache_key(p) == "aliyun:p"
    legacy = SimpleNamespace(config=None, _provider_type="qiniu", name="q")
    assert d._provider_type(legacy) == "qiniu"
    with pytest.raises(RuntimeError):
        d._provider_type(SimpleNamespace(config=None, name="x"))

    assert d._summarize_matches([])["total_matches"] == 0
    m = DeployMatchResult(provider_type="aliyun", provider_name="p", product_type="cdn", product_id="c1", domain="a.example.com", match_type="exact")
    summary = d._summarize_matches([m, m])
    assert summary["by_provider"]["aliyun:p"] == 2
    bind_summary = d._summarize_bind_results([
        {"provider_type": "aliyun", "provider": "p", "product_type": "cdn", "status": "success"},
        {"provider_type": "aliyun", "provider": "p", "product_type": "cdn", "status": "failed"},
    ])
    assert bind_summary["by_product_type"]["cdn"]["failed"] == 1

    conf = SimpleNamespace(
        ssh_profiles=[
            SimpleNamespace(name="key", auth=SimpleNamespace(type="key", key_path="/k")),
            SimpleNamespace(name="pwd", auth=SimpleNamespace(type="password", password="secret", password_env="")),
        ],
        nginx=SimpleNamespace(nginx_hosts=[
            SimpleNamespace(name="h1", ssh_profile="key"),
            SimpleNamespace(name="h2", ssh_profile="pwd"),
        ]),
    )
    assert d._build_nginx_target_auth_map(conf) == {
        "h1": {"auth_type": "key", "key_path": "/k", "password": ""},
        "h2": {"auth_type": "password", "key_path": "", "password": "secret"},
    }
    with pytest.raises(RuntimeError, match="缺少 ssh_profile"):
        d._build_nginx_target_auth_map(SimpleNamespace(ssh_profiles=[], nginx=SimpleNamespace(nginx_hosts=[SimpleNamespace(name="h")])))


def test_deploy_service_get_provider_bindings_cache_and_scan():
    d = service()
    p = SimpleNamespace(
        config=SimpleNamespace(type="fake", name="p"),
        name="p",
        get_cdn_bindings=lambda: asyncio.sleep(0, result=[CloudProductBinding(provider_name="p", product_type="cdn", product_id="c", domain="a")]),
        get_live_bindings=lambda: asyncio.sleep(0, result=[CloudProductBinding(provider_name="p", product_type="live", product_id="l", domain="a")]),
        get_lb_bindings=lambda: asyncio.sleep(0, result=[]),
        get_oss_bindings=lambda: asyncio.sleep(0, result=[]),
        get_waf_bindings=lambda: asyncio.sleep(0, result=[]),
    )
    d.conf.product_scan.fake = SimpleNamespace(cdn_scan=True, live_scan=True, lb_scan=False, oss_scan=False, waf_scan=False)
    d.conf.deploy.binding_cache = SimpleNamespace(enabled=False, ttl_seconds=10, verify_before_apply=True)
    result = asyncio.run(d._get_provider_bindings(p, False, 10))
    assert {x.product_type for x in result} == {"cdn", "live"}

    record = {"updated_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
              "bindings": [{"product_type": "cdn", "product_id": "c", "domain": "a"}]}
    d._binding_store.get_provider = lambda key: record
    d.conf.deploy.binding_cache = SimpleNamespace(enabled=True, ttl_seconds=10, verify_before_apply=True)
    cached = asyncio.run(d._get_provider_bindings(p, True, 10))
    assert cached[0].domain == "a"
    d._binding_store.get_provider = lambda key: {"updated_at": "bad", "bindings": []}
    assert asyncio.run(d._get_provider_bindings(p, True, 10)) == []
    d._binding_store.get_provider = lambda key: {"updated_at": record["updated_at"], "bindings": "bad"}
    assert asyncio.run(d._get_provider_bindings(p, True, 10)) == []

    t = SimpleNamespace(config=SimpleNamespace(type="tencent", name="t"), name="t")
    assert asyncio.run(d._get_provider_bindings(t, True, 10)) == []


def test_deploy_service_verify_and_notify(monkeypatch):
    d = service()
    provider = SimpleNamespace(config=SimpleNamespace(type="fake", name="p"), name="p")
    match = DeployMatchResult(provider_type="fake", provider_name="p", product_type="cdn", product_id="c", domain="a.example.com", match_type="exact")
    refreshed = CloudProductBinding(provider_name="p", product_type="cdn", product_id="c", domain="a.example.com", cert_id="new", listener_port=443, metadata={"x": 1})
    d._get_provider_bindings = lambda **kwargs: asyncio.sleep(0, result=[refreshed])

    class N:
        def __init__(self): self.events=[]
        async def notify_alert(self, e): self.events.append(e)
    n=N()
    out = asyncio.run(d._verify_matches_before_apply([provider], [match], [], True, 10, n))
    assert out[0].current_cert_id == "new" and out[0].listener_port == 443

    missing = []
    d._get_provider_bindings = lambda **kwargs: asyncio.sleep(0, result=[])
    out = asyncio.run(d._verify_matches_before_apply([provider], [match], missing, True, 10, n))
    assert out == [] and missing[0]["reason"] == "apply 前实时校验未命中，已跳过"
    assert n.events

    d._get_provider_bindings = lambda **kwargs: (_ for _ in ()).throw(RuntimeError("down"))
    skipped=[]
    out=asyncio.run(d._verify_matches_before_apply([provider],[match],skipped,True,10,n))
    assert out == [] and skipped[0]["status"] == "failed"

    with pytest.raises(RuntimeError, match="provider 不存在"):
        asyncio.run(d._verify_matches_before_apply([], [match], [], True, 10, n))

    awaitable = d._notify_verify_skip(n, "p", "cdn", "c", "a", "reason")
    asyncio.run(awaitable)


def test_deployment_error_and_legacy_deployer():
    result = {"bind_results": [{"provider": "p", "product_id": "c", "status": "failed", "message": "boom"}]}
    err = DeploymentError(result)
    assert "1 个目标失败" in str(err)
    from app.services.deploy import _LegacyProviderDeployer
    class Legacy:
        name = "legacy"
        async def upload_certificate_to_cas(self, cert, key, alias): return "cid"
        async def deploy_certificate_with_cert_id(self, **kwargs): return True
    dep = _LegacyProviderDeployer(Legacy())
    target = SimpleNamespace(product_type="cdn", product_id="c", domain="a", listener_port=443, metadata={})
    target = type("T", (), target.__dict__)()
    target = __import__("app.deploys.base", fromlist=["DeployTarget"]).DeployTarget("legacy", "cdn", product_id="c", domain="a", listener_port=443)
    assert (asyncio.run(dep.deploy("CERT","KEY",target))).success

    class NoDeploy:
        name = "x"
    dep2 = _LegacyProviderDeployer(NoDeploy())
    assert asyncio.run(dep2.deploy("C","K",target)).success is False
