import asyncio
from types import SimpleNamespace

from app.providers import huawei as mod
from app.schemas.config import ProviderConfig


def make_provider():
    p = object.__new__(mod.HuaweiCloudProvider)
    p.config = SimpleNamespace(name="huawei", type="huawei")
    p._region = "cn-north-4"
    p._ak = "ak"
    p._sk = "sk"
    p._project_id = "project"
    p._enterprise_project_id = "ep"
    p._cert_domains_cache = {}
    p._bss_client = None
    p.time = SimpleNamespace(
        parse=lambda value: value,
        to_local_tz=lambda value: f"local:{value}",
        remaining_days=lambda value: 7,
    )
    p.logger = SimpleNamespace(
        debug=lambda *a, **k: None, warning=lambda *a, **k: None,
        error=lambda *a, **k: None, info=lambda *a, **k: None,
    )
    return p


def direct_api(monkeypatch):
    async def direct(self, op_name, func, **kwargs):
        return func()
    monkeypatch.setattr(mod.Provider, "_call_provider_api", direct)


def test_huawei_domain_dns_cdn_and_certificate_helpers(monkeypatch):
    p = make_provider()
    direct_api(monkeypatch)
    p._dns_client = SimpleNamespace(
        list_public_zones=lambda req: SimpleNamespace(zones=[
            SimpleNamespace(name="example.com.", id="z1", status="ACTIVE", record_num=2)
        ]),
        list_record_sets_by_zone=lambda req: SimpleNamespace(recordsets=[
            SimpleNamespace(name="example.com.", id="r0", type="A", records=[], ttl=300),
            SimpleNamespace(name="www.example.com.", id="r1", type="A", records=["1.2.3.4"], ttl=600),
            SimpleNamespace(name="other.test", id="r2", type="CNAME", records=["x"], ttl=60),
        ]),
    )
    assert asyncio.run(p.get_domain_list())[0]["domain"] == "example.com"
    records = asyncio.run(p.get_dns_records("example.com"))
    assert records[0]["subdomain"] == "@"
    assert records[0]["value"] == ""
    assert records[1]["subdomain"] == "www"

    p._cdn_client = SimpleNamespace(list_domains=lambda req: SimpleNamespace(domains=[
        SimpleNamespace(domain_status="online", id="c1", domain_name="cdn.example.com", cname="c.cdn"),
        SimpleNamespace(domain_status="offline", id="c2", domain_name="off.example.com", cname="x"),
    ]))
    monkeypatch.setattr(p, "_get_cdn_client", lambda: p._cdn_client)
    cdn = asyncio.run(p.get_cdn_bindings())
    assert len(cdn) == 1 and cdn[0].domain == "cdn.example.com"

    assert p._normalize_binding_domain(" EXAMPLE.COM. ") == "example.com"
    assert p._normalize_binding_domain("*.example.com") is None
    assert p._normalize_binding_domain("1.2.3.4") is None
    assert p._split_domain_candidates("a; b,,c") == ["a", "b", "c"]
    assert p._parse_cert_id("https://x/cert-1?foo=1#x") == "cert-1"
    assert p._parse_cert_id("") == ""

    cert = SimpleNamespace(
        common_name="Example.COM.",
        subject_alternative_names=["www.example.com", "*.example.com", "1.2.3.4"],
        domain="api.example.com; extra.example.com",
    )
    p._elb_client = SimpleNamespace(show_certificate=lambda req: SimpleNamespace(certificate=cert))
    domains = p._extract_domains_from_certificate("projects/p/cert-1/")
    assert domains == ["api.example.com", "example.com", "extra.example.com", "www.example.com"]
    assert p._extract_domains_from_certificate("cert-1") == domains

    p._elb_client = SimpleNamespace(show_certificate=lambda req: (_ for _ in ()).throw(RuntimeError("down")))
    p._cert_domains_cache.clear()
    assert p._extract_domains_from_certificate("cert-x") == []
    assert p._extract_listener_cert_refs(SimpleNamespace(
        default_tls_container_ref=" /cert/default ",
        sni_container_refs=["/cert/a", "", None],
    )) == [("default", "/cert/default"), ("sni", "/cert/a")]


def test_huawei_elb_waf_bindings_and_ecs_offline(monkeypatch):
    p = make_provider()
    direct_api(monkeypatch)

    listener = SimpleNamespace(
        admin_state_up=True, id="l1", loadbalancers=[SimpleNamespace(id="lb1")],
        default_tls_container_ref="/cert/default", sni_container_refs=["/cert/sni", ""],
        name="api", protocol_port=443, project_id="project", protocol="HTTPS",
    )
    p._elb_client = SimpleNamespace(
        list_listeners=lambda req: SimpleNamespace(listeners=[listener])
    )
    monkeypatch.setattr(p, "_extract_domains_from_certificate", lambda ref: ["api.example.com"] if "default" in ref else [])
    bindings = asyncio.run(p.get_lb_bindings())
    assert len(bindings) == 1
    assert bindings[0].cert_id == "default"
    assert bindings[0].metadata["lb_id"] == "lb1"

    host_https = SimpleNamespace(id="h1", hostid="", hostname="waf.example.com.", enterprise_project_id="")
    host_http = SimpleNamespace(id="h2", hostid="", hostname="http.example.com", enterprise_project_id="ep")
    details = {
        "h1": SimpleNamespace(protocol="HTTPS", protect_status=1, certificateid="wc1", certificatename="cert", access_status=1),
        "h2": SimpleNamespace(protocol="HTTP", protect_status=0, certificateid="wc2", certificatename="cert2", access_status=1),
    }
    p._waf_client = SimpleNamespace(
        list_host=lambda req: SimpleNamespace(items=[host_https, host_http], total=2),
        show_host=lambda req: details[req.instance_id],
    )
    waf = asyncio.run(p.get_waf_bindings())
    assert len(waf) == 1
    assert waf[0].status == "active"
    assert waf[0].metadata["enterprise_project_id"] == "ep"

    p._ecs_client = SimpleNamespace(list_servers_details=lambda req: SimpleNamespace(servers=[
        SimpleNamespace(id="i1", name="", status="ACTIVE"),
        SimpleNamespace(id="", name="skip", status="ACTIVE"),
    ]))
    monkeypatch.setattr(mod, "build_bss_client", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("bss unavailable")))
    ecs = asyncio.run(p.get_ecs_instances())
    assert len(ecs) == 1
    assert ecs[0]["instance_name"] == "i1"
    assert ecs[0]["charge_type"] == ""


def test_huawei_ecs_bss_mapping_and_error_time(monkeypatch):
    p = make_provider()
    direct_api(monkeypatch)
    p._ecs_client = SimpleNamespace(list_servers_details=lambda req: SimpleNamespace(servers=[
        SimpleNamespace(id="i1", name="server", status="ACTIVE"),
        SimpleNamespace(id="i2", name="server2", status="SHUTOFF"),
    ]))
    class Bss:
        def list_pay_per_use_customer_resources(self, req):
            return SimpleNamespace(customer_resources=[
                SimpleNamespace(resource_id="i1", expire_time="2026-12-01 00:00:00"),
                SimpleNamespace(resource_id="i2", expire_time="bad-time"),
                SimpleNamespace(resource_id="", expire_time="x"),
            ])
    p._bss_client = Bss()
    p.time = SimpleNamespace(
        parse=lambda value: value if value != "bad-time" else (_ for _ in ()).throw(ValueError("bad")),
        to_local_tz=lambda value: "LOCAL",
        remaining_days=lambda value: 9,
    )
    ecs = asyncio.run(p.get_ecs_instances())
    by_id = {x["instance_id"]: x for x in ecs}
    assert by_id["i1"]["days"] == 9 and by_id["i1"]["expires_at"] == "LOCAL"
    assert by_id["i2"]["days"] == 0 and by_id["i2"]["expires_at"] == "bad-time"
    assert by_id["i1"]["charge_type"] == "prepaid"
