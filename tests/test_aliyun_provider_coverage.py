import asyncio
from types import SimpleNamespace

import pytest

from app.providers import aliyun as mod
from app.providers.base import Provider
from app.schemas.config import ProviderConfig


class FakeModels:
    def __getattr__(self, name):
        return lambda **kwargs: SimpleNamespace(**kwargs)


class FakeSDK:
    domain_models = FakeModels()
    dns_models = FakeModels()
    cdn_models = FakeModels()
    slb_models = FakeModels()
    alb_models = FakeModels()
    ecs_models = FakeModels()
    swas_models = FakeModels()


def provider():
    obj = object.__new__(mod.AliyunCloudProvider)
    Provider.__init__(
        obj,
        ProviderConfig(
            name="aliyun-test",
            type="aliyun",
            credentials={
                "access_key_id": "ak",
                "access_key_secret": "sk",
                "region": "cn-hangzhou",
            },
        ),
    )
    obj._sdk = FakeSDK()
    obj._access_key_id = "ak"
    obj._access_key_secret = "sk"
    obj._region = "cn-hangzhou"
    obj._slb_clients_by_region = {"cn-hangzhou": object()}
    obj._alb_clients_by_region = {"cn-hangzhou": object()}
    obj._ecs_clients_by_region = {"cn-hangzhou": object()}
    obj._swas_clients_by_region = {}
    obj._slb_scan_regions = None
    obj._alb_scan_regions = None
    obj._ecs_scan_regions = None
    obj.domains = {}
    return obj


def test_region_normalization_and_configuration():
    p = provider()
    p.config.credentials["regions"] = "cn-hangzhou;cn-beijing, cn-hangzhou, us-east-1,"

    assert p._normalize_regions(["cn-hangzhou", "cn-hangzhou", "us-east-1", "", "cn-beijing"]) == [
        "cn-hangzhou",
        "cn-beijing",
    ]
    assert p._configured_regions("ecs") == ["cn-hangzhou", "cn-beijing"]
    assert p._is_mainland_region("cn-hangzhou") is True
    assert p._is_mainland_region("ap-guangzhou") is False


def test_region_discovery_and_scan_region_fallbacks(monkeypatch):
    p = provider()

    p._slb_client = SimpleNamespace(
        describe_regions=lambda req: SimpleNamespace(
            body=SimpleNamespace(
                regions=SimpleNamespace(
                    region=[
                        SimpleNamespace(region_id="cn-beijing"),
                        SimpleNamespace(region_id="cn-beijing"),
                        SimpleNamespace(region_id="us-east-1"),
                    ]
                )
            )
        )
    )
    p._alb_client = SimpleNamespace(
        describe_regions=lambda req: SimpleNamespace(
            body=SimpleNamespace(
                regions=[
                    SimpleNamespace(region_id="cn-shanghai"),
                    SimpleNamespace(region_id="cn-hongkong"),
                ]
            )
        )
    )
    p._ecs_client = SimpleNamespace(
        describe_regions=lambda req: SimpleNamespace(
            body=SimpleNamespace(
                regions=SimpleNamespace(
                    region=[
                        SimpleNamespace(region_id="cn-shenzhen"),
                        SimpleNamespace(region_id="ap-southeast-1"),
                    ]
                )
            )
        )
    )

    assert p._discover_slb_regions() == ["cn-beijing"]
    assert p._discover_alb_regions() == ["cn-shanghai", "cn-hongkong"]
    assert p._discover_ecs_regions() == ["cn-shenzhen"]
    assert p._get_slb_scan_regions() == ["cn-beijing"]
    assert p._get_alb_scan_regions() == ["cn-shanghai", "cn-hongkong"]
    assert p._get_ecs_scan_regions() == ["cn-shenzhen"]

    # 缓存命中后不再发现地域
    monkeypatch.setattr(p, "_discover_slb_regions", lambda: pytest.fail("不应再次发现"))
    assert p._get_slb_scan_regions() == ["cn-beijing"]


def test_region_discovery_failure_and_expected_error_classification():
    p = provider()
    p._slb_client = SimpleNamespace(describe_regions=lambda req: (_ for _ in ()).throw(RuntimeError("boom")))
    assert p._discover_slb_regions() == []

    assert p._is_expected_region_error(RuntimeError("Error Code: AccessDenied")) is True
    assert p._is_expected_region_error(RuntimeError("HTTP status code: 403")) is True
    assert p._is_expected_region_error(RuntimeError("random failure")) is False


def test_extract_slb_certificate_and_domain_fallback():
    p = provider()

    listener = SimpleNamespace(
        httpslistener_config=SimpleNamespace(server_certificate_id="cert-a"),
        server_certificate_id="cert-b",
    )
    assert p._extract_slb_listener_cert_id(listener) == "cert-a"
    assert p._extract_slb_listener_cert_id(SimpleNamespace(server_certificate_id="cert-b")) == "cert-b"
    assert p._extract_slb_listener_cert_id(SimpleNamespace()) is None

    p._sdk.slb_models = FakeModels()
    client = SimpleNamespace(
        describe_domain_extensions=lambda req: SimpleNamespace(
            body=SimpleNamespace(
                domain_extensions=SimpleNamespace(
                    domain_extension=[
                        SimpleNamespace(domain="WWW.Example.COM"),
                        SimpleNamespace(domain=""),
                        SimpleNamespace(domain="api.example.com."),
                    ]
                )
            )
        ),
        describe_load_balancer_httpslistener_attribute=lambda req: (_ for _ in ()).throw(
            RuntimeError("fallback")
        ),
    )
    assert p._extract_slb_listener_domains(
        client, region="cn-hangzhou", lb_id="lb-1", listener_port=443
    ) == ["api.example.com", "www.example.com"]

    client2 = SimpleNamespace(
        describe_domain_extensions=lambda req: (_ for _ in ()).throw(RuntimeError("first")),
        describe_load_balancer_httpslistener_attribute=lambda req: SimpleNamespace(
            body=SimpleNamespace(
                domain_extensions=SimpleNamespace(
                    domain_extension=[SimpleNamespace(domain="Api.Example.COM")]
                )
            )
        ),
    )
    assert p._extract_slb_listener_domains(
        client2, region="cn-hangzhou", lb_id="lb-1", listener_port=443
    ) == ["api.example.com"]


def test_get_domains_paginates_and_survives_api_error():
    p = provider()
    calls = []

    def query(req):
        calls.append(req.page_num)
        item = SimpleNamespace(
            domain_name=f"example-{req.page_num}.com",
            expiration_date="2030-01-01",
            ccompany="Example",
        )
        if req.page_num == 1:
            return SimpleNamespace(body=SimpleNamespace(data=SimpleNamespace(domain=[item] * 100)))
        return SimpleNamespace(body=SimpleNamespace(data=SimpleNamespace(domain=[item])))

    p._domain_client = SimpleNamespace(query_domain_list=query)
    assert set(p._get_domains()) == {"example-1.com", "example-2.com"}
    assert calls == [1, 2]

    p._domain_client = SimpleNamespace(query_domain_list=lambda req: (_ for _ in ()).throw(RuntimeError("down")))
    assert p._get_domains() == {}


def test_get_domain_list_calls_dns_and_computes_days(monkeypatch):
    p = provider()
    p.domains = {
        "example.com": {
            "name": "aliyun-test",
            "domain": "example.com",
            "expires_at": "2030-01-01",
            "org": "Example",
            "provider": "aliyun",
        }
    }
    p.time.parse = lambda value: value
    p.time.remaining_days = lambda value: 123
    p.get_dns_records = lambda domain: asyncio.sleep(0, result=[{"name": "www.example.com"}])

    result = asyncio.run(p.get_domain_list())
    assert result[0]["days"] == 123
    assert result[0]["subs"][0]["name"] == "www.example.com"


def test_get_dns_records_paginates_and_filters_probe_targets(monkeypatch):
    p = provider()
    calls = []

    def describe(req):
        calls.append(req.page_number)
        records = [
            SimpleNamespace(rr="www", remark="web", status="ENABLE", type="A"),
            SimpleNamespace(rr="@", remark="", status="DISABLE", type="A"),
            SimpleNamespace(rr="mail", remark="", status="ENABLE", type="MX"),
        ]
        if req.page_number == 1:
            return SimpleNamespace(body=SimpleNamespace(total_count=4, domain_records=SimpleNamespace(record=records)))
        return SimpleNamespace(body=SimpleNamespace(total_count=501, domain_records=SimpleNamespace(record=[records[0]])))

    p._dns_client = SimpleNamespace(describe_domain_records=describe)
    monkeypatch.setattr(
        "app.config.load_raw_config",
        lambda: SimpleNamespace(http_scan_parallel=2),
    )

    async def probe(domain, *, remark="", sem=None):
        return {"name": domain, "remark": remark, "status": "在工作"}

    p._probe_domain = probe
    result = asyncio.run(p.get_dns_records("example.com"))

    assert calls == [1, 2]
    assert result == [
        {"name": "www.example.com", "remark": "web", "status": "在工作"},
        {"name": "www.example.com", "remark": "web", "status": "在工作"},
    ]


def test_get_cdn_bindings_filters_status_https_and_missing_cert():
    p = provider()
    page = [
        SimpleNamespace(
            domain_name="on.example.com",
            domain_status="online",
            ssl_protocol="on",
            cname="cname.example",
            cdn_type="dynamic",
        ),
        SimpleNamespace(
            domain_name="off.example.com",
            domain_status="offline",
            ssl_protocol="on",
            cname="",
            cdn_type="",
        ),
        SimpleNamespace(
            domain_name="http.example.com",
            domain_status="online",
            ssl_protocol="off",
            cname="",
            cdn_type="",
        ),
    ]

    p._cdn_client = SimpleNamespace(
        describe_user_domains=lambda req: SimpleNamespace(
            body=SimpleNamespace(domains=SimpleNamespace(page_data=page))
        ),
        describe_domain_certificate_info=lambda req: SimpleNamespace(
            body=SimpleNamespace(
                cert_infos=SimpleNamespace(
                    cert_info=[SimpleNamespace(cert_id="cert-1", cert_expire_time="2030-01-01")]
                )
            )
        ),
    )

    result = asyncio.run(p.get_cdn_bindings())
    assert len(result) == 1
    assert result[0].domain == "on.example.com"
    assert result[0].cert_id == "cert-1"
    assert result[0].metadata["cname"] == "cname.example"


def test_get_lb_bindings_builds_https_domain_bindings_and_skips_stopped():
    p = provider()
    p._slb_scan_regions = ["cn-hangzhou"]

    listener = SimpleNamespace(
        status="running",
        listener_port=443,
        httpslistener_config=SimpleNamespace(server_certificate_id="cert-1"),
    )
    lb = SimpleNamespace(
        load_balancer_id="lb-1",
        load_balancer_name="main",
        address="1.2.3.4",
    )
    client = SimpleNamespace(
        describe_load_balancers=lambda req: SimpleNamespace(
            body=SimpleNamespace(load_balancers=SimpleNamespace(load_balancer=[lb]))
        ),
        describe_load_balancer_listeners=lambda req: SimpleNamespace(
            body=SimpleNamespace(listeners=[listener])
        ),
        describe_domain_extensions=lambda req: SimpleNamespace(
            body=SimpleNamespace(
                domain_extensions=SimpleNamespace(
                    domain_extension=[SimpleNamespace(domain="API.Example.COM")]
                )
            )
        ),
    )
    p._slb_clients_by_region["cn-hangzhou"] = client

    result = asyncio.run(p.get_lb_bindings())
    assert len(result) == 1
    assert result[0].product_id == "cn-hangzhou:lb-1:443"
    assert result[0].domain == "api.example.com"
    assert result[0].cert_id == "cert-1"


def test_get_lb_bindings_skips_region_errors_and_listener_errors():
    p = provider()
    p._slb_scan_regions = ["cn-hangzhou", "cn-beijing"]
    good_lb = SimpleNamespace(load_balancer_id="lb-good", load_balancer_name="good", address="1.1.1.1")
    good_client = SimpleNamespace(
        describe_load_balancers=lambda req: SimpleNamespace(
            body=SimpleNamespace(load_balancers=SimpleNamespace(load_balancer=[good_lb]))
        ),
        describe_load_balancer_listeners=lambda req: (_ for _ in ()).throw(RuntimeError("listener down")),
    )
    bad_client = SimpleNamespace(
        describe_load_balancers=lambda req: (_ for _ in ()).throw(RuntimeError("Error Code: AccessDenied")),
    )
    p._slb_clients_by_region.update({"cn-hangzhou": good_client, "cn-beijing": bad_client})

    result = asyncio.run(p.get_lb_bindings())
    assert result == []


def test_get_alb_bindings_paginates_lbs_listeners_rules_and_filters():
    p = provider()
    p._alb_scan_regions = ["cn-hangzhou"]

    lb = SimpleNamespace(load_balancer_id="alb-1", load_balancer_name="alb")
    listener = SimpleNamespace(
        listener_protocol="HTTPS",
        listener_status="running",
        listener_port=443,
        listener_id="listener-1",
        certificates=[SimpleNamespace(certificate_id="cert-alb")],
    )
    rule = SimpleNamespace(
        rule_conditions=[
            SimpleNamespace(type="host", host_config=SimpleNamespace(values=["A.Example.COM", ""]))
        ]
    )

    class Client:
        def __init__(self):
            self.lb_calls = 0
            self.listener_calls = 0
            self.rule_calls = 0

        def list_load_balancers(self, req):
            self.lb_calls += 1
            return SimpleNamespace(
                body=SimpleNamespace(
                    load_balancers=[lb] if self.lb_calls == 1 else [],
                    next_token="next" if self.lb_calls == 1 else None,
                )
            )

        def list_listeners(self, req):
            self.listener_calls += 1
            return SimpleNamespace(
                body=SimpleNamespace(
                    listeners=[listener] if self.listener_calls == 1 else [],
                    next_token="next" if self.listener_calls == 1 else None,
                )
            )

        def list_rules(self, req):
            self.rule_calls += 1
            return SimpleNamespace(
                body=SimpleNamespace(
                    rules=[rule] if self.rule_calls == 1 else [],
                    next_token="next" if self.rule_calls == 1 else None,
                )
            )

    p._alb_clients_by_region["cn-hangzhou"] = Client()
    result = asyncio.run(p.get_alb_bindings())

    assert len(result) == 1
    assert result[0].domain == "a.example.com"
    assert result[0].product_id == "cn-hangzhou:alb-1:listener-1"
    assert result[0].cert_id == "cert-alb"


def test_alb_rule_and_certificate_helpers():
    assert mod.AliyunCloudProvider._extract_alb_rule_domains(
        [
            SimpleNamespace(
                rule_conditions=[
                    SimpleNamespace(type="host", host_config=SimpleNamespace(values=["B.EXAMPLE.COM", "a.example.com"]))
                ]
            ),
            SimpleNamespace(
                rule_conditions=[SimpleNamespace(type="path", host_config=SimpleNamespace(values=["ignored.example.com"]))]
            ),
        ]
    ) == ["a.example.com", "b.example.com"]

    assert mod.AliyunCloudProvider._extract_alb_listener_cert_id(
        SimpleNamespace(certificates=[SimpleNamespace(certificate_id="c1")], certificate_id="c2")
    ) == "c1"
    assert mod.AliyunCloudProvider._extract_alb_listener_cert_id(
        SimpleNamespace(certificates=[], certificate_id="c2")
    ) == "c2"


def test_get_ecs_instances_collects_prepaid_ecs_and_swas(monkeypatch):
    p = provider()
    p._ecs_scan_regions = ["cn-hangzhou"]

    ecs_item = SimpleNamespace(
        instance_charge_type="PrePaid",
        instance_id="i-1",
        instance_name="ecs-one",
        host_name="host-one",
        status="Running",
        expired_time="2030-01-01 00:00:00",
    )
    swas_item = SimpleNamespace(
        charge_type="Prepaid",
        instance_id="s-1",
        instance_name="swas-one",
        region_id="",
        status="Running",
        expired_time="2030-01-02 00:00:00",
    )

    p._ecs_clients_by_region["cn-hangzhou"] = SimpleNamespace(
        describe_instance_auto_renew_attribute=lambda req: SimpleNamespace(
            body=SimpleNamespace(
                instance_renew_attributes=SimpleNamespace(
                    instance_renew_attribute=[
                        SimpleNamespace(instance_id="i-1", renewal_status="AutoRenewal")
                    ] if req.renewal_status == "AutoRenewal" else []
                )
            )
        ),
        describe_instances=lambda req: SimpleNamespace(
            body=SimpleNamespace(instances=SimpleNamespace(instance=[ecs_item]))
        ),
    )
    p._swas_clients_by_region["cn-hangzhou"] = SimpleNamespace(
        list_instances=lambda req: SimpleNamespace(
            body=SimpleNamespace(instances=[swas_item])
        )
    )
    p.time.parse = lambda value: value
    p.time.to_local_tz = lambda value: f"LOCAL:{value}"
    p.time.remaining_days = lambda value: 10
    p._build_swas_client_for_region = lambda region: p._swas_clients_by_region[region]

    result = asyncio.run(p.get_ecs_instances())
    assert [item["instance_id"] for item in result] == ["i-1", "s-1"]
    assert result[0]["auto_renewal"] is True
    assert result[0]["days"] == 10
    assert result[1]["instance_kind"] == "swas"


def test_swas_filters_non_prepaid_invalid_and_missing_expiry():
    p = provider()
    p._sdk.swas_models = FakeModels()
    p._build_swas_client_for_region = lambda region: SimpleNamespace(
        list_instances=lambda req: SimpleNamespace(
            body=SimpleNamespace(
                instances=[
                    SimpleNamespace(charge_type="Postpaid", instance_id="post", expired_time="", instance_name=""),
                    SimpleNamespace(charge_type="Prepaid", instance_id="", expired_time="", instance_name=""),
                    SimpleNamespace(charge_type="Prepaid", instance_id="pre", expired_time="", instance_name=""),
                    SimpleNamespace(
                        charge_type="Prepaid",
                        instance_id="bad-date",
                        expired_time="not-a-date",
                        instance_name="bad-date",
                        region_id="cn-hangzhou",
                        status="Running",
                    ),
                ]
            )
        )
    )
    p.time.parse = lambda value: (_ for _ in ()).throw(ValueError("bad")) if value == "not-a-date" else value

    result = p._collect_aliyun_swas_prepaid_for_region("cn-hangzhou")
    assert [item["instance_id"] for item in result] == ["pre", "bad-date"]
    assert result[0]["days"] is None
    assert result[1]["expires_at"] == "not-a-date"


def test_get_all_product_bindings_includes_alb_and_skips_failure(monkeypatch):
    p = provider()
    marker = object()
    async def cdn(): return [marker]
    async def alb(): return ["alb"]
    p.get_cdn_bindings = cdn
    p.get_alb_bindings = alb
    p.get_lb_bindings = lambda: asyncio.sleep(0, result=[])
    p.get_oss_bindings = lambda: asyncio.sleep(0, result=[])
    p.get_waf_bindings = lambda: (_ for _ in ()).throw(RuntimeError("waf"))

    result = asyncio.run(p.get_all_product_bindings())
    assert result == [marker, "alb"]
