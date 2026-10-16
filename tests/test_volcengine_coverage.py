import asyncio
import pytest
from types import SimpleNamespace

from app.deploys.base import DeployTarget
from app.deploys import volcengine as deploy_mod
from app.providers import volcengine as provider_mod
from app.schemas.config import ProviderConfig


def test_volcengine_provider_discovers_domains_dns_and_cdn(monkeypatch):
    class Service:
        def __init__(self, kind):
            self.kind = kind

        def list_zones(self, req):
            return {
                "Zones": [
                    {"ZoneName": "example.com.", "ZID": "z1", "Status": "Enabled", "RecordCount": 2}
                ]
            }

        def list_records(self, req):
            assert req["ZID"] == "z1"
            return {
                "Records": [
                    {"RecordID": "r1", "Host": "@", "Type": "A", "Value": "1.2.3.4", "TTL": 600},
                    {"RecordID": "r2", "Host": "www", "Type": "CNAME", "Value": "origin.example.com", "TTL": 300},
                ]
            }

        def list_cdn_domains(self, req):
            return {
                "Data": [
                    {"Domain": "cdn.example.com", "Status": "online", "Cname": "cdn.volcengine.com"},
                    {"Domain": "offline.example.com", "Status": "offline", "Cname": "x"},
                ]
            }

    monkeypatch.setattr(
        provider_mod,
        "build_volcengine_service",
        lambda **kwargs: Service(kwargs["service"]),
    )
    p = provider_mod.VolcengineCloudProvider(
        ProviderConfig(
            name="volc-test",
            type="volcengine",
            credentials={"access_key_id": "ak", "access_key_secret": "sk"},
        )
    )

    domains = asyncio.run(p.get_domain_list())
    assert domains == [
        {"domain": "example.com", "zone_id": "z1", "status": "Enabled", "record_count": 2}
    ]
    records = asyncio.run(p.get_dns_records("example.com"))
    assert records[0]["full_domain"] == "example.com"
    assert records[1]["full_domain"] == "www.example.com"
    assert records[1]["ttl"] == 300

    assert asyncio.run(p.discover_domains()) == domains
    bindings = asyncio.run(p.get_cdn_bindings())
    assert len(bindings) == 1
    assert bindings[0].domain == "cdn.example.com"
    assert bindings[0].metadata["cname"] == "cdn.volcengine.com"
    assert asyncio.run(p.get_lb_bindings()) == []


def test_volcengine_provider_returns_empty_when_zone_missing():
    class DNS:
        def list_zones(self, req):
            return {"Zones": [{"ZoneName": "other.com.", "ZID": "z2"}]}

    class CDN:
        def list_cdn_domains(self, req):
            raise RuntimeError("should not be called")

    original = provider_mod.build_volcengine_service
    try:
        provider_mod.build_volcengine_service = lambda **kwargs: DNS() if kwargs["service"] == "dns" else CDN()
        p = provider_mod.VolcengineCloudProvider(
            ProviderConfig(name="v", type="volcengine", credentials={})
        )
        assert asyncio.run(p.get_dns_records("missing.com")) == []
    finally:
        provider_mod.build_volcengine_service = original


def test_volcengine_provider_error_is_handled_by_provider_wrapper(monkeypatch):
    class Broken:
        def list_zones(self, req):
            raise RuntimeError("network-like failure")

    monkeypatch.setattr(provider_mod, "build_volcengine_service", lambda **kwargs: Broken())
    p = provider_mod.VolcengineCloudProvider(
        ProviderConfig(name="v", type="volcengine", credentials={})
    )
    with pytest.raises(RuntimeError, match="network-like failure"):
        asyncio.run(p.get_domain_list())


def test_volcengine_deployer_lazy_services_and_upload(monkeypatch):
    services = {}

    class Service:
        def __init__(self, kind):
            self.kind = kind

        def import_certificate(self, payload):
            services["import"] = payload
            return {"CertificateId": "cert-1"}

    def build(**kwargs):
        services.setdefault(kwargs["service"], Service(kwargs["service"]))
        return services[kwargs["service"]]

    monkeypatch.setattr(deploy_mod, "build_volcengine_service", build)
    d = deploy_mod.VolcengineDeployer("ak", "sk")
    assert d.name == "volcengine"
    assert d._get_cert_service() is d._get_cert_service()
    assert asyncio.run(d._upload_certificate("CERT", "KEY", "name")) == "cert-1"
    assert services["import"]["CertificateName"] == "name"
    assert services["import"]["PublicKey"] == "CERT"


def test_volcengine_deployer_upload_failure_and_cdn(monkeypatch):
    class CDN:
        def add_certificate(self, payload):
            return {"CertId": "cdn-cert"}

        def update_cdn_config(self, payload):
            self.payload = payload

    class Cert:
        def import_certificate(self, payload):
            raise RuntimeError("import failed")

    cdn = CDN()
    monkeypatch.setattr(
        deploy_mod,
        "build_volcengine_service",
        lambda **kwargs: {"cdn": cdn, "certificate_service": Cert(), "clb": SimpleNamespace()}[kwargs["service"]],
    )
    d = deploy_mod.VolcengineDeployer("ak", "sk")
    assert asyncio.run(d._upload_certificate("CERT", "KEY", "name")) is None
    assert asyncio.run(d._deploy_to_cdn("CERT", "KEY", "cdn.example.com")) is True
    assert cdn.payload["HTTPS"]["CertInfo"]["CertId"] == "cdn-cert"


def test_volcengine_deployer_cdn_failure_and_clb(monkeypatch):
    class CDN:
        def add_certificate(self, payload):
            raise RuntimeError("cdn add failed")

    class CLB:
        def modify_listener_attributes(self, payload):
            self.payload = payload

    cdn, clb = CDN(), CLB()
    monkeypatch.setattr(
        deploy_mod,
        "build_volcengine_service",
        lambda **kwargs: {"cdn": cdn, "clb": clb, "certificate_service": SimpleNamespace()}[kwargs["service"]],
    )
    d = deploy_mod.VolcengineDeployer("ak", "sk")
    assert asyncio.run(d._deploy_to_cdn("CERT", "KEY", "d.example.com")) is False
    assert asyncio.run(d._deploy_to_clb("cert-1", "listener-1")) is True
    assert clb.payload == {"ListenerId": "listener-1", "CertificateId": "cert-1"}


def test_volcengine_deploy_matrix_and_list_targets(monkeypatch):
    class CDN:
        def list_cdn_domains(self, req):
            return {"Data": [{"Domain": "cdn.example.com"}]}

        def add_certificate(self, payload):
            return {"CertId": "c1"}

        def update_cdn_config(self, payload):
            return None

    class CLB:
        def describe_listeners(self, req):
            return {
                "Listeners": [
                    {"Protocol": "HTTPS", "ListenerId": "l1", "ListenerName": "api.example.com", "Port": 443},
                    {"Protocol": "TCP", "ListenerId": "l2", "ListenerName": "tcp", "Port": 80},
                ]
            }

        def modify_listener_attributes(self, payload):
            return None

    class Cert:
        def import_certificate(self, payload):
            return {"CertificateId": "c2"}

    objects = {"cdn": CDN(), "clb": CLB(), "certificate_service": Cert()}
    monkeypatch.setattr(deploy_mod, "build_volcengine_service", lambda **kwargs: objects[kwargs["service"]])
    d = deploy_mod.VolcengineDeployer("ak", "sk")

    result_cdn = asyncio.run(d.deploy("CERT", "KEY", DeployTarget("volcengine", "cdn", domain="cdn.example.com")))
    assert result_cdn.success
    result_clb = asyncio.run(d.deploy("CERT", "KEY", DeployTarget("volcengine", "clb", product_id="l1", domain="api.example.com")))
    assert result_clb.success
    assert result_clb.message == "CLB 证书部署成功"
    bad = asyncio.run(d.deploy("CERT", "KEY", DeployTarget("volcengine", "other")))
    assert bad.success is False
    assert "不支持" in bad.message

    targets = asyncio.run(d.list_targets())
    assert len(targets) == 2
    assert targets[0].product_type == "cdn"
    assert targets[1].product_type == "clb"
    assert targets[1].metadata["port"] == 443


def test_volcengine_list_targets_survives_one_service_failure(monkeypatch):
    class BrokenCDN:
        def list_cdn_domains(self, req):
            raise RuntimeError("cdn down")

    class CLB:
        def describe_listeners(self, req):
            return {"Listeners": []}

    monkeypatch.setattr(
        deploy_mod,
        "build_volcengine_service",
        lambda **kwargs: BrokenCDN() if kwargs["service"] == "cdn" else CLB(),
    )
    d = deploy_mod.VolcengineDeployer("ak", "sk")
    assert asyncio.run(d.list_bindable_resources()) == []
