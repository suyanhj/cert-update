import asyncio
from types import SimpleNamespace

import pytest

from app.deploys.base import DeployTarget
from app.deploys import huawei as mod


def make_deployer():
    d = object.__new__(mod.HuaweiDeployer)
    d._ak = "ak"; d._sk = "sk"; d._project_id = "p"; d._region = "cn-north-4"
    d._enterprise_project_id = "ep"
    d._waf_certificate_id = None
    d._waf_certificate_name = ""
    d._waf_certificate_prepared = False
    d._elb_client = SimpleNamespace()
    d._waf_client = SimpleNamespace()
    d._cdn_client = None
    return d


def test_huawei_deployer_certificate_upload_and_domain_helpers(monkeypatch):
    d = make_deployer()

    d._waf_client = SimpleNamespace(create_certificate=lambda req: SimpleNamespace(id="w1", name="WAF-CERT"))
    assert asyncio.run(d._upload_waf_certificate("CERT", "KEY", "name")) == "w1"
    assert d._waf_certificate_name == "WAF-CERT"
    d._waf_client = SimpleNamespace(create_certificate=lambda req: SimpleNamespace(id="", name=""))
    assert asyncio.run(d._upload_waf_certificate("CERT", "KEY", "name")) is None
    d._waf_client = SimpleNamespace(create_certificate=lambda req: (_ for _ in ()).throw(RuntimeError("down")))
    assert asyncio.run(d._upload_waf_certificate("CERT", "KEY", "name")) is None

    d._elb_client = SimpleNamespace(create_certificate=lambda req: SimpleNamespace(
        certificate=SimpleNamespace(id="e1")
    ))
    monkeypatch.setattr(mod, "parse_cert_info_from_pem", lambda pem: SimpleNamespace(sans=["*.example.com", "API.EXAMPLE.COM", "api.example.com"]))
    assert asyncio.run(d._upload_certificate("CERT", "KEY", "name")) == "e1"
    d._elb_client = SimpleNamespace(create_certificate=lambda req: SimpleNamespace(certificate=None))
    assert asyncio.run(d._upload_certificate("CERT", "KEY", "name")) is None
    d._elb_client = SimpleNamespace(create_certificate=lambda req: (_ for _ in ()).throw(RuntimeError("down")))
    assert asyncio.run(d._upload_certificate("CERT", "KEY", "name")) is None

    assert d._split_domain_candidates("a;b,, c") == ["a", "b", "c"]
    d._elb_client = SimpleNamespace(show_certificate=lambda req: SimpleNamespace(certificate=SimpleNamespace(
        common_name="Example.COM.",
        subject_alternative_names=["www.example.com", "*.api.example.com", "1.2.3.4"],
        domain="foo.example.com; bar.example.com"
    )))
    concrete, wild = d._get_cert_domain_info("c1")
    assert concrete == {"example.com", "www.example.com", "foo.example.com", "bar.example.com"}
    assert wild == {"api.example.com"}
    d._elb_client = SimpleNamespace(show_certificate=lambda req: (_ for _ in ()).throw(RuntimeError("down")))
    assert d._get_cert_domain_info("c2") == (set(), set())

    d._get_cert_domain_info = lambda cert_id: ({"api.example.com"}, {"example.com"})
    assert d._cert_covers_domain("c1", "api.example.com") is True
    assert d._cert_covers_domain("c1", "x.example.com") is True
    assert d._cert_covers_domain("", "x.example.com") is False
    assert d._cert_covers_domain("c1", "a.b.example.com") is True
    assert d._cert_covers_domain("c1", "other.com") is False
    assert d._certs_san_overlap("same", "same") is False
    assert d._certs_san_overlap("", "b") is False
    d._get_cert_domain_info = lambda cid: (
        ({"foo.example.com"}, set()) if cid == "a" else
        ({"bar.example.com"}, set())
    )
    assert d._certs_san_overlap("a", "b") is False
    d._get_cert_domain_info = lambda cid: (
        ({"foo.example.com"}, set()) if cid == "a" else
        ({"foo.example.com"}, set())
    )
    assert d._certs_san_overlap("a", "b") is True
    assert d._wildcard_suffix_touches_cert("example.com", {"a.example.com"}, set()) is True
    assert d._wildcard_suffix_touches_cert("example.com", {"a.b.example.com"}, set()) is False
    assert d._wildcard_suffix_touches_cert("example.com", set(), {"example.com"}) is True
    assert d._wildcard_suffix_touches_cert("", set(), set()) is False

    d._get_cert_domain_info = lambda cid: ({"a.example.com"}, {"example.com"})
    assert d._new_cert_covers_old_cert({"a.example.com", "*.example.com"}, "old") is True
    assert d._new_cert_covers_old_cert({"a.example.com"}, "old") is False
    d._get_cert_domain_info = lambda cid: (set(), set())
    assert d._new_cert_covers_old_cert({"a.example.com"}, "old", previous_cert_id="old") is True
    assert d._new_cert_covers_old_cert({"a.example.com"}, "old", previous_cert_id="other") is False
    assert d._new_cert_covers_old_cert(set(), "") is False


def test_huawei_deployer_elb_update_matrix(monkeypatch):
    d = make_deployer()
    calls = []
    d._elb_client = SimpleNamespace(
        show_listener=lambda req: SimpleNamespace(listener=SimpleNamespace(
            default_tls_container_ref="/cert/old",
            sni_container_refs=["/cert/sni-old", "", "/cert/new"],
        )),
        update_listener=lambda req: calls.append(req),
    )
    d._new_cert_covers_old_cert = lambda new, old, previous=None: old in {"old", "sni-old"}
    assert asyncio.run(d._deploy_to_elb(
        "new", "listener", {"a.example.com"}, domain="a.example.com"
    )) is True
    assert len(calls) == 1
    opt = calls[0].body.listener
    assert opt.default_tls_container_ref == "/cert/new"
    assert opt.sni_container_refs == ["/cert/new", "", "/cert/new"]

    d._elb_client = SimpleNamespace(
        show_listener=lambda req: SimpleNamespace(listener=SimpleNamespace(
            default_tls_container_ref="new", sni_container_refs=["/x/new"]
        )),
        update_listener=lambda req: (_ for _ in ()).throw(AssertionError("must not update")),
    )
    assert asyncio.run(d._deploy_to_elb("new", "listener", {"a"})) is True

    d._elb_client = SimpleNamespace(
        show_listener=lambda req: (_ for _ in ()).throw(RuntimeError("show failed"))
    )
    with pytest.raises(RuntimeError, match="show failed"):
        asyncio.run(d._deploy_to_elb("new", "listener", {"a"}))


def test_huawei_deployer_prepare_and_deploy_matrix(monkeypatch):
    d = make_deployer()
    calls = []
    async def upload_waf(cert, key, name): calls.append(("waf", name)); return "w1"
    async def upload_elb(cert, key, name): calls.append(("elb", name)); return "e1"
    d._upload_waf_certificate = upload_waf
    d._upload_certificate = upload_elb
    targets = [DeployTarget("huawei", "waf", product_id="h1"), DeployTarget("huawei", "elb", product_id="l1")]
    assert asyncio.run(d.prepare_certificate("CERT", "KEY", "example.com", targets)) == "e1"
    assert d._waf_certificate_id == "w1" and d._waf_certificate_prepared is True

    d._deploy_to_cdn = lambda cert, key, domain: asyncio.sleep(0, result=True)
    assert asyncio.run(d.deploy("CERT", "KEY", DeployTarget("huawei", "cdn", domain="a.example.com"))).success

    d._upload_certificate = upload_elb
    d._build_new_cert_domains = lambda cert: {"a.example.com"}
    d._deploy_to_elb = lambda **kwargs: asyncio.sleep(0, result=True)
    result = asyncio.run(d.deploy("CERT", "KEY", DeployTarget("huawei", "elb", product_id="l1", domain="a.example.com")))
    assert result.success and result.cert_id == "e1"

    d._upload_waf_certificate = upload_waf
    d._waf_certificate_prepared = False
    d._waf_certificate_id = None
    d._deploy_to_waf = lambda **kwargs: asyncio.sleep(0, result=True)
    result = asyncio.run(d.deploy("CERT", "KEY", DeployTarget("huawei", "waf", product_id="h1", domain="a.example.com")))
    assert result.success and result.cert_id == "w1"
    assert asyncio.run(d.deploy("CERT", "KEY", DeployTarget("huawei", "waf", product_id=""))).success is False
    d._waf_certificate_id = None; d._waf_certificate_prepared = True
    assert asyncio.run(d.deploy("CERT", "KEY", DeployTarget("huawei", "waf", product_id="h1"))).success is False
    bad = asyncio.run(d.deploy("CERT", "KEY", DeployTarget("huawei", "bad")))
    assert bad.success is False and "不支持" in bad.message


def test_huawei_deployer_list_targets(monkeypatch):
    d = make_deployer()
    d._cdn_client = SimpleNamespace(list_domains=lambda req: SimpleNamespace(domains=[
        SimpleNamespace(id="c1", domain_name="cdn.example.com")
    ]))
    monkeypatch.setattr(d, "_get_cdn_client", lambda: d._cdn_client)
    d._elb_client = SimpleNamespace(list_listeners=lambda req: SimpleNamespace(listeners=[
        SimpleNamespace(protocol="HTTPS", id="l1", name="api.example.com", protocol_port=443),
        SimpleNamespace(protocol="TCP", id="l2", name="tcp", protocol_port=80),
    ]))
    targets = d.list_bindable_resources()
    assert len(targets) == 2
    assert targets[0].product_type == "cdn"
    assert targets[1].metadata["port"] == 443
