# -*- coding: utf-8 -*-
"""华为云 WAF 证书扫描与部署测试。"""

import asyncio
from types import SimpleNamespace

import pytest

import app.deploys.huawei as huawei_deployer_mod
import app.providers.huawei as huawei_provider_mod
from app.deploys.base import DeployTarget
from app.deploys.huawei import HuaweiDeployer
from app.providers.huawei import HuaweiCloudProvider
from app.schemas.config import ProviderConfig
from app.schemas.provider import CloudProductBinding
from app.services.bindings import BindingCacheService


class FakeWafClient:
    """记录 WAF SDK 请求的测试客户端。"""

    def __init__(self, pages=None, details=None):
        self.pages = pages or {}
        self.details = details or {}
        self.list_requests = []
        self.show_requests = []
        self.create_requests = []
        self.apply_requests = []
        self.create_error = None
        self.apply_error = None

    def list_host(self, request):
        self.list_requests.append(request)
        return self.pages[request.page]

    def show_host(self, request):
        self.show_requests.append(request)
        value = self.details[request.instance_id]
        if isinstance(value, Exception):
            raise value
        return value

    def create_certificate(self, request):
        self.create_requests.append(request)
        if self.create_error:
            raise self.create_error
        return SimpleNamespace(id="waf-cert-new", name=request.body.name)

    def apply_certificate_to_host(self, request):
        self.apply_requests.append(request)
        if self.apply_error:
            raise self.apply_error
        return SimpleNamespace()


class FakeElbClient:
    def __init__(self):
        self.create_requests = []

    def create_certificate(self, request):
        self.create_requests.append(request)
        return SimpleNamespace(certificate=SimpleNamespace(id="elb-cert-new"))


def _host(host_id, hostname, enterprise_project_id="0"):
    return SimpleNamespace(
        id=host_id,
        hostid="",
        hostname=hostname,
        enterprise_project_id=enterprise_project_id,
    )


def _detail(protocol, cert_id="", cert_name="", protect_status=1, access_status=1):
    return SimpleNamespace(
        protocol=protocol,
        certificateid=cert_id,
        certificatename=cert_name,
        protect_status=protect_status,
        access_status=access_status,
    )


def _make_provider(monkeypatch, waf_client):
    monkeypatch.setattr(huawei_provider_mod, "build_dns_client", lambda **kwargs: object())
    monkeypatch.setattr(huawei_provider_mod, "build_ecs_client", lambda **kwargs: object())
    monkeypatch.setattr(huawei_provider_mod, "build_elb_client", lambda **kwargs: object())
    monkeypatch.setattr(huawei_provider_mod, "build_waf_client", lambda **kwargs: waf_client)
    return HuaweiCloudProvider(
        ProviderConfig(
            name="huawei-test",
            type="huawei",
            credentials={
                "access_key_id": "ak",
                "access_key_secret": "sk",
                "project_id": "project-1",
                "region": "cn-south-1",
                "enterprise_project_id": "all_granted_eps",
            },
        )
    )


def test_huawei_waf_scan_filters_https_and_isolates_detail_failure(monkeypatch):
    waf_client = FakeWafClient(
        pages={
            1: SimpleNamespace(
                total=4,
                items=[
                    _host("host-1", "API.EXAMPLE.COM.", "eps-1"),
                    _host("host-2", "http.example.com"),
                    _host("host-3", "bad.example.com"),
                    _host("host-4", "mixed.example.com", "eps-4"),
                ],
            )
        },
        details={
            "host-1": _detail("HTTPS", "cert-old", "old-cert"),
            "host-2": _detail("HTTP"),
            "host-3": RuntimeError("详情接口失败"),
            "host-4": _detail("HTTP&HTTPS", protect_status=0, access_status=0),
        },
    )
    provider = _make_provider(monkeypatch, waf_client)

    bindings = asyncio.run(provider.get_waf_bindings())

    assert [(item.product_id, item.domain) for item in bindings] == [
        ("host-1", "api.example.com"),
        ("host-4", "mixed.example.com"),
    ]
    assert bindings[0].cert_id == "cert-old"
    assert bindings[0].metadata["certificate_name"] == "old-cert"
    assert bindings[0].metadata["enterprise_project_id"] == "eps-1"
    assert bindings[1].cert_id is None
    assert bindings[1].status == "paused"
    assert waf_client.show_requests[0].enterprise_project_id == "eps-1"


def test_huawei_waf_scan_paginates(monkeypatch):
    first_page_hosts = [_host(f"host-{index}", f"http-{index}.example.com") for index in range(100)]
    details = {item.id: _detail("HTTP") for item in first_page_hosts}
    details["host-100"] = _detail("HTTPS", "cert-100", "cert-name-100")
    waf_client = FakeWafClient(
        pages={
            1: SimpleNamespace(total=101, items=first_page_hosts),
            2: SimpleNamespace(total=101, items=[_host("host-100", "secure.example.com")]),
        },
        details=details,
    )
    provider = _make_provider(monkeypatch, waf_client)

    bindings = asyncio.run(provider.get_waf_bindings())

    assert [request.page for request in waf_client.list_requests] == [1, 2]
    assert len(bindings) == 1
    assert bindings[0].domain == "secure.example.com"


def test_binding_cache_service_uses_waf_scan_switch():
    calls = []

    class FakeProvider:
        name = "huawei-test"
        config = SimpleNamespace(type="huawei")

        async def get_waf_bindings(self):
            calls.append("waf")
            return [CloudProductBinding(product_type="waf", product_id="host-1", domain="api.example.com")]

    service = BindingCacheService.__new__(BindingCacheService)
    service.conf = SimpleNamespace(
        product_scan=SimpleNamespace(
            huawei=SimpleNamespace(
                cdn_scan=False,
                live_scan=False,
                lb_scan=False,
                oss_scan=False,
                waf_scan=True,
            )
        )
    )

    bindings = asyncio.run(service._scan_provider_bindings(FakeProvider()))
    assert calls == ["waf"]
    assert [item.product_type for item in bindings] == ["waf"]

    service.conf.product_scan.huawei.waf_scan = False
    calls.clear()
    assert asyncio.run(service._scan_provider_bindings(FakeProvider())) == []
    assert calls == []


def _make_deployer(monkeypatch, waf_client, elb_client):
    monkeypatch.setattr(huawei_deployer_mod, "build_waf_client", lambda **kwargs: waf_client)
    monkeypatch.setattr(huawei_deployer_mod, "build_elb_client", lambda **kwargs: elb_client)
    monkeypatch.setattr(
        huawei_deployer_mod,
        "parse_cert_info_from_pem",
        lambda pem: SimpleNamespace(sans=["api.example.com"]),
    )
    return HuaweiDeployer(
        access_key_id="ak",
        access_key_secret="sk",
        project_id="project-1",
        region="cn-south-1",
        enterprise_project_id="all_granted_eps",
    )


def test_huawei_prepare_and_deploy_reuses_separate_elb_and_waf_certificates(monkeypatch):
    waf_client = FakeWafClient()
    elb_client = FakeElbClient()
    deployer = _make_deployer(monkeypatch, waf_client, elb_client)
    waf_targets = [
        DeployTarget(provider="huawei", product_type="waf", product_id="host-1", domain="api.example.com"),
        DeployTarget(provider="huawei", product_type="waf", product_id="host-2", domain="www.example.com"),
    ]
    targets = [
        *waf_targets,
        DeployTarget(provider="huawei", product_type="elb", product_id="listener-1", domain="api.example.com"),
    ]

    elb_cert_id = asyncio.run(
        deployer.prepare_certificate("CERT", "KEY", "example.com", targets)
    )
    assert elb_cert_id == "elb-cert-new"
    assert len(elb_client.create_requests) == 1
    assert len(waf_client.create_requests) == 1

    results = [
        asyncio.run(
            deployer.deploy(
                "CERT",
                "KEY",
                DeployTarget(
                    provider=target.provider,
                    product_type=target.product_type,
                    product_id=target.product_id,
                    domain=target.domain,
                    metadata={"enterprise_project_id": f"eps-{index}"},
                ),
                cert_id=elb_cert_id,
            )
        )
        for index, target in enumerate(waf_targets, start=1)
    ]

    assert all(item.success for item in results)
    assert [item.cert_id for item in results] == ["waf-cert-new", "waf-cert-new"]
    assert len(waf_client.create_requests) == 1
    assert [request.body.cloud_host_ids for request in waf_client.apply_requests] == [
        ["host-1"],
        ["host-2"],
    ]
    assert [request.enterprise_project_id for request in waf_client.apply_requests] == [
        "eps-1",
        "eps-2",
    ]


def test_huawei_waf_apply_failure_returns_failed_result(monkeypatch):
    waf_client = FakeWafClient()
    waf_client.apply_error = RuntimeError("绑定失败")
    deployer = _make_deployer(monkeypatch, waf_client, FakeElbClient())
    deployer._waf_certificate_id = "waf-cert-new"

    result = asyncio.run(
        deployer.deploy(
            "CERT",
            "KEY",
            DeployTarget(
                provider="huawei",
                product_type="waf",
                product_id="host-1",
                domain="api.example.com",
            ),
        )
    )

    assert result.success is False
    assert "绑定失败" in result.message


def test_huawei_waf_upload_failure_is_not_retried_for_each_target(monkeypatch):
    waf_client = FakeWafClient()
    waf_client.create_error = RuntimeError("上传失败")
    deployer = _make_deployer(monkeypatch, waf_client, FakeElbClient())
    targets = [
        DeployTarget(provider="huawei", product_type="waf", product_id="host-1", domain="a.example.com"),
        DeployTarget(provider="huawei", product_type="waf", product_id="host-2", domain="b.example.com"),
    ]

    asyncio.run(deployer.prepare_certificate("CERT", "KEY", "example.com", targets))
    results = [asyncio.run(deployer.deploy("CERT", "KEY", target)) for target in targets]

    assert len(waf_client.create_requests) == 1
    assert all(result.success is False for result in results)
    assert all(result.message == "WAF 证书上传失败" for result in results)
