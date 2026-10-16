from types import SimpleNamespace

from app.utils import huawei_sdk
from app.providers import static as static_mod


def test_huawei_sdk_builders_are_offline(monkeypatch):
    calls = []
    class Builder:
        def with_credentials(self, c): calls.append(("cred", c)); return self
        def with_region(self, r): calls.append(("region", r)); return self
        def build(self): return "client"
    class Client:
        @classmethod
        def new_builder(cls): return Builder()
    class Region:
        @staticmethod
        def value_of(r): return f"REGION:{r}"

    for fn, client, region in [
        (huawei_sdk.build_cdn_client, Client, Region),
        (huawei_sdk.build_dns_client, Client, Region),
        (huawei_sdk.build_elb_client, Client, Region),
        (huawei_sdk.build_ecs_client, Client, Region),
        (huawei_sdk.build_scm_client, Client, Region),
        (huawei_sdk.build_bss_client, Client, Region),
        (huawei_sdk.build_waf_client, Client, Region),
    ]:
        monkeypatch.setattr(huawei_sdk, fn.__globals__[fn.__name__.replace("build_", "").split("_")[0].capitalize()+"Client"] if False else "huawei_sdk.BasicCredentials", huawei_sdk.BasicCredentials, raising=False)
        monkeypatch.setattr(huawei_sdk, {"build_cdn_client":"CdnClient","build_dns_client":"DnsClient","build_elb_client":"ElbClient","build_ecs_client":"EcsClient","build_scm_client":"ScmClient","build_bss_client":"BssClient","build_waf_client":"WafClient"}[fn.__name__], Client)
        monkeypatch.setattr(huawei_sdk, {"build_cdn_client":"CdnRegion","build_dns_client":"DnsRegion","build_elb_client":"ElbRegion","build_ecs_client":"EcsRegion","build_scm_client":"ScmRegion","build_bss_client":"BssRegion","build_waf_client":"WafRegion"}[fn.__name__], Region)
        assert fn(region="cn-test", credentials="cred") == "client"

    basic = huawei_sdk.build_basic_credentials("ak", "sk", "p")
    assert basic is not None
    assert huawei_sdk.build_global_credentials("ak", "sk") is not None


def test_huawei_mixins_cache(monkeypatch):
    class M(huawei_sdk.HuaweiBasicCredentialMixin, huawei_sdk.HuaweiCdnClientMixin):
        pass
    m = M(); m._ak="ak"; m._sk="sk"; m._project_id="p"; m._credentials=None; m._cdn_client=None
    first = m._get_credentials()
    assert m._get_credentials() is first
    monkeypatch.setattr(huawei_sdk, "build_cdn_client", lambda **kw: "cdn")
    assert m._get_cdn_client() == "cdn"
    assert m._get_cdn_client() == "cdn"


def test_static_provider_edge_cases():
    cfg = SimpleNamespace(
        name="s", type="custom", concurrency=2, check_extensions=False,
        sub_extensions=[], static_domains=[]
    )
    p = static_mod.StaticCloudProvider(cfg)
    assert p._full_domain() == []
