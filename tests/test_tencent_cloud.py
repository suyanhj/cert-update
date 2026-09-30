# -*- coding: utf-8 -*-
"""腾讯云 Provider / Deployer 单测。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import yaml
import pytest
from qcloud_cos import CosServiceError

import app.deploys.tencent as tencent_deploy_mod
import app.providers.tencent as tencent_provider_mod
from app.deploys.base import DeployTarget
from app.deploys.tencent import TencentDeployer
from app.providers.tencent import TencentCloudProvider
from app.schemas.config import ProviderConfig
from app.utils.tencent_sdk import CdnClient, ClbClient, DomainClient, DnspodClient, LiveClient, SslClient


class _FixedTime:
    @staticmethod
    def now():
        return datetime(2026, 3, 16, 12, 34, 56)


def _provider_config() -> ProviderConfig:
    return ProviderConfig(
        name="腾讯云",
        type="tencent",
        credentials={
            "secret_id": "sid",
            "secret_key": "skey",
            "region": "ap-guangzhou",
        },
    )


def _patch_clients(
    monkeypatch,
    *,
    dnspod=None,
    domain=None,
    cdn=None,
    clb=None,
    live=None,
    cos=None,
    ssl=None,
    ssl_by_region=None,
):
    ssl_build_regions: list[str] = []

    def fake_build(*, secret_id, secret_key, client_cls, region=""):
        if client_cls == SslClient:
            ssl_build_regions.append(str(region or ""))
            if ssl_by_region is not None:
                key = str(region or "")
                if key not in ssl_by_region:
                    ssl_by_region[key] = ssl or FakeSsl()
                return ssl_by_region[key]
            return ssl or FakeSsl()
        mapping = {
            DnspodClient: dnspod or SimpleNamespace(),
            DomainClient: domain or FakeDomain(),
            CdnClient: cdn or SimpleNamespace(),
            ClbClient: clb or SimpleNamespace(),
            LiveClient: live or SimpleNamespace(),
        }
        client = mapping.get(client_cls)
        if client is None:
            raise AssertionError(f"unexpected client_cls={client_cls}")
        return client

    monkeypatch.setattr(tencent_provider_mod, "build_tencent_client", fake_build)
    monkeypatch.setattr(tencent_deploy_mod, "build_tencent_client", fake_build)
    monkeypatch.setattr(
        tencent_provider_mod,
        "build_tencent_cos_client",
        lambda **_kwargs: cos or FakeCos(),
    )
    return ssl_build_regions


def _ssl_clb(lb_id: str, lb_name: str, listeners: list) -> SimpleNamespace:
    return SimpleNamespace(LoadBalancerId=lb_id, LoadBalancerName=lb_name, Listeners=listeners)


def _patch_clb_scan_regions(monkeypatch, regions: list[str]):
    monkeypatch.setattr(
        TencentCloudProvider,
        "_get_clb_scan_regions",
        lambda self: regions,
    )


def _patch_cos_scan_regions(monkeypatch, regions: list[str]):
    monkeypatch.setattr(
        TencentCloudProvider,
        "_get_cos_scan_regions",
        lambda self: regions,
    )


class FakeDomain:
    def __init__(self, domains=None, simple_info_by_domain=None):
        self._domains = domains or []
        self._simple_info_by_domain = {
            str(key).strip().lower(): value
            for key, value in (simple_info_by_domain or {}).items()
        }

    def DescribeDomainNameList(self, req):
        return SimpleNamespace(
            DomainSet=self._domains,
            TotalCount=len(self._domains),
        )

    def DescribeDomainSimpleInfo(self, req):
        domain_name = str(getattr(req, "DomainName", "") or "").strip().lower()
        info = self._simple_info_by_domain.get(domain_name)
        if info is None:
            info = SimpleNamespace(OrganizationNameCN="", OrganizationName="")
        return SimpleNamespace(DomainInfo=info)


class FakeDnspod:
    def __init__(self, domains, records_by_domain):
        self._domains = domains
        self._records_by_domain = records_by_domain

    def DescribeDomainList(self, req):
        return SimpleNamespace(
            DomainList=self._domains,
            DomainCountInfo=SimpleNamespace(DomainTotal=len(self._domains)),
        )

    def DescribeRecordList(self, req):
        records = self._records_by_domain.get(req.Domain, [])
        return SimpleNamespace(
            RecordList=records,
            RecordCountInfo=SimpleNamespace(TotalCount=len(records)),
        )


class FakeCdn:
    def __init__(self, domains=None):
        self.domains = list(domains or [])
        self.calls = []

    def DescribeDomainsConfig(self, req):
        self.calls.append(req)
        return SimpleNamespace(Domains=self.domains, TotalNumber=len(self.domains))


class FakeClb:
    def __init__(self, load_balancers=None, listeners_by_lb=None):
        self.load_balancers = list(load_balancers or [])
        self.listeners_by_lb = dict(listeners_by_lb or {})

    def DescribeLoadBalancers(self, req):
        return SimpleNamespace(
            LoadBalancerSet=self.load_balancers,
            TotalCount=len(self.load_balancers),
        )

    def DescribeListeners(self, req):
        return SimpleNamespace(Listeners=self.listeners_by_lb.get(req.LoadBalancerId, []))


class FakeLive:
    def __init__(self, domains=None, certs=None):
        self.domains = list(domains or [])
        self.certs = dict(certs or {})
        self.domain_calls = []
        self.cert_calls = []

    def DescribeLiveDomains(self, req):
        self.domain_calls.append(req)
        return SimpleNamespace(DomainList=self.domains, AllCount=len(self.domains))

    def DescribeLiveDomainCert(self, req):
        self.cert_calls.append(req)
        value = self.certs.get(req.DomainName)
        if isinstance(value, Exception):
            raise value
        return SimpleNamespace(DomainCertInfo=value)


class FakeCos:
    def __init__(self, buckets=None, domains_by_bucket=None):
        self.buckets = list(buckets or [])
        self.domains_by_bucket = dict(domains_by_bucket or {})
        self.list_calls = []
        self.domain_calls = []

    def list_buckets(self, **kwargs):
        self.list_calls.append(kwargs)
        return {
            "Buckets": {"Bucket": self.buckets},
            "IsTruncated": "false",
        }

    def get_bucket_domain(self, *, Bucket):
        self.domain_calls.append(Bucket)
        return {"DomainRule": self.domains_by_bucket.get(Bucket, [])}


def test_tencent_domain_list_probes_enable_a_cname(monkeypatch):
    domains = [
        SimpleNamespace(
            Name="example.com",
            DomainId=1,
            Status="ENABLE",
            VipEndAt="2027-01-01 00:00:00",
            RecordCount=3,
        )
    ]
    records = [
        SimpleNamespace(
            RecordId=11,
            Name="www",
            Type="A",
            Value="1.1.1.1",
            Status="ENABLE",
            TTL=600,
            Line="默认",
            Remark="web",
        ),
        SimpleNamespace(
            RecordId=12,
            Name="mail",
            Type="MX",
            Value="mx.example.com",
            Status="ENABLE",
            TTL=600,
            Line="默认",
            Remark="",
        ),
        SimpleNamespace(
            RecordId=13,
            Name="old",
            Type="CNAME",
            Value="gone.example.com",
            Status="DISABLE",
            TTL=600,
            Line="默认",
            Remark="",
        ),
    ]
    _patch_clients(
        monkeypatch,
        dnspod=FakeDnspod(domains, {"example.com": records}),
        domain=FakeDomain(
            [SimpleNamespace(DomainName="example.com", ExpirationDate="2027-01-01")]
        ),
    )

    probed = []

    async def fake_probe(self, domain, *, remark="", sem=None):
        probed.append(domain)
        return {"name": domain, "status": "在工作", "remark": remark}

    monkeypatch.setattr(TencentCloudProvider, "_probe_domain", fake_probe)
    provider = TencentCloudProvider(_provider_config())
    result = asyncio.run(provider.get_domain_list())

    assert len(result) == 1
    assert result[0]["domain"] == "example.com"
    assert probed == ["www.example.com"]
    assert result[0]["subs"][0]["name"] == "www.example.com"


def test_tencent_is_domain_provider_does_not_probe(monkeypatch):
    domains = [
        SimpleNamespace(
            Name="example.com",
            DomainId=1,
            Status="ENABLE",
            VipEndAt=None,
            RecordCount=0,
        )
    ]
    dnspod = FakeDnspod(domains, {"example.com": []})

    def boom(*_args, **_kwargs):
        raise AssertionError("is_domain_provider 不应拉取解析或探测")

    dnspod.DescribeRecordList = boom
    _patch_clients(monkeypatch, dnspod=dnspod)

    async def fake_probe(self, domain, *, remark="", sem=None):
        raise AssertionError("is_domain_provider 不应探测")

    monkeypatch.setattr(TencentCloudProvider, "_probe_domain", fake_probe)
    provider = TencentCloudProvider(_provider_config())
    assert asyncio.run(provider.is_domain_provider("www.example.com")) is True
    assert asyncio.run(provider.is_domain_provider("other.net")) is False


def test_tencent_cdn_bindings_filter_https(monkeypatch):
    ssl = FakeSsl(
        cdn_instances=[
            SimpleNamespace(
                Domain="cdn.example.com",
                CertId="cert-1",
                Status="online",
                HttpsBillingSwitch="off",
            ),
            SimpleNamespace(
                Domain="plain.example.com",
                CertId="",
                Status="online",
                HttpsBillingSwitch="on",
            ),
            SimpleNamespace(
                Domain="off.example.com",
                CertId="cert-2",
                Status="offline",
                HttpsBillingSwitch="on",
            ),
            SimpleNamespace(
                Domain="empty.example.com",
                CertId="",
                Status="online",
                HttpsBillingSwitch="",
            ),
        ]
    )
    _patch_clients(monkeypatch, ssl=ssl)
    provider = TencentCloudProvider(_provider_config())
    bindings = asyncio.run(provider._get_ssl_cdn_bindings("cert-new"))
    assert len(bindings) == 3
    by_domain = {item.domain: item for item in bindings}
    assert set(by_domain) == {"cdn.example.com", "plain.example.com", "empty.example.com"}
    item = by_domain["cdn.example.com"]
    assert item.product_type == "cdn"
    assert item.product_id == "cdn.example.com"
    assert item.cert_id == "cert-1"
    assert by_domain["plain.example.com"].cert_id is None
    assert item.metadata["https_billing"] == "off"
    assert item.metadata["cdn_deploy_mode"] == "ssl"
    assert ssl.cdn_calls[0].IsCache == 0
    assert ssl.cdn_calls[0].CertificateId == "cert-new"
    assert ssl.cdn_calls[0].Filters[0].FilterKey == "domainMatch"
    assert ssl.cdn_calls[0].Filters[0].FilterValue == "1"


def test_tencent_native_cdn_scan_includes_https_without_certificate(monkeypatch):
    monkeypatch.setattr(TencentCloudProvider, "_ENABLE_BINDING_CACHE_SCAN", True)
    cdn = FakeCdn(
        [
            SimpleNamespace(
                Domain="nocert.example.com",
                Status="online",
                Cname="nocert.example.com.cdn.dnsv1.com",
                ServiceType="web",
                Https=SimpleNamespace(Switch="on", CertInfo=SimpleNamespace(CertId="", ExpireTime=None)),
                HttpsBilling=SimpleNamespace(Switch="off"),
            ),
            SimpleNamespace(
                Domain="off.example.com",
                Status="online",
                Cname="",
                ServiceType="web",
                Https=SimpleNamespace(Switch="off", CertInfo=None),
                HttpsBilling=None,
            ),
        ]
    )
    ssl = FakeSsl()
    _patch_clients(monkeypatch, cdn=cdn, ssl=ssl)
    provider = TencentCloudProvider(_provider_config())

    bindings = asyncio.run(provider.get_cdn_bindings())

    assert len(bindings) == 1
    assert bindings[0].domain == "nocert.example.com"
    assert bindings[0].cert_id is None
    assert bindings[0].metadata["cdn_deploy_mode"] == "modify"
    assert ssl.cdn_calls == []


def test_tencent_native_clb_scan_does_not_call_ssl(monkeypatch):
    monkeypatch.setattr(TencentCloudProvider, "_ENABLE_BINDING_CACHE_SCAN", True)
    listener = SimpleNamespace(
        ListenerId="lbl-1",
        Protocol="HTTPS",
        Port=443,
        ListenerName="https",
        SniSwitch=1,
        Certificate=SimpleNamespace(CertId=""),
        Rules=[SimpleNamespace(Domain="api.example.com", Domains=[], Certificate=None)],
    )
    clb = FakeClb(
        load_balancers=[SimpleNamespace(LoadBalancerId="lb-1", LoadBalancerName="web")],
        listeners_by_lb={"lb-1": [listener]},
    )
    ssl = FakeSsl()
    _patch_clients(monkeypatch, clb=clb, ssl=ssl)
    _patch_clb_scan_regions(monkeypatch, ["ap-guangzhou"])
    provider = TencentCloudProvider(_provider_config())

    bindings = asyncio.run(provider.get_lb_bindings())

    assert len(bindings) == 1
    assert bindings[0].product_id == "ap-guangzhou:lb-1:lbl-1"
    assert bindings[0].cert_id is None
    assert ssl.clb_calls == []


def test_tencent_native_clb_scan_fails_on_region_error(monkeypatch):
    monkeypatch.setattr(TencentCloudProvider, "_ENABLE_BINDING_CACHE_SCAN", True)
    class BrokenClb(FakeClb):
        def DescribeLoadBalancers(self, req):
            raise RuntimeError("permission denied")

    _patch_clients(monkeypatch, clb=BrokenClb(), ssl=FakeSsl())
    _patch_clb_scan_regions(monkeypatch, ["ap-guangzhou"])
    provider = TencentCloudProvider(_provider_config())

    with pytest.raises(RuntimeError, match="ap-guangzhou"):
        asyncio.run(provider.get_lb_bindings())


def test_tencent_native_live_scan_uses_product_api(monkeypatch):
    monkeypatch.setattr(TencentCloudProvider, "_ENABLE_BINDING_CACHE_SCAN", True)
    live = FakeLive(
        domains=[
            SimpleNamespace(Name="play.example.com", Status=1, PlayType=1, CurrentCName="live.cname"),
            SimpleNamespace(Name="push.example.com", Status=1, PlayType=0, CurrentCName="push.cname"),
        ],
        certs={
            "play.example.com": SimpleNamespace(
                Status=1,
                CloudCertId="cert-live",
                CertId=9,
                CertExpireTime="2027-01-02 03:04:05",
            ),
            "push.example.com": SimpleNamespace(
                Status=-1,
                CloudCertId="",
                CertId="",
                CertExpireTime="",
            ),
        },
    )
    ssl = FakeSsl()
    _patch_clients(monkeypatch, live=live, ssl=ssl)
    provider = TencentCloudProvider(_provider_config())

    bindings = asyncio.run(provider.get_live_bindings())

    assert len(bindings) == 2
    by_domain = {item.domain: item for item in bindings}
    assert by_domain["play.example.com"].cert_id == "cert-live"
    assert by_domain["play.example.com"].cert_expire_time == "2027-01-02 03:04:05"
    assert by_domain["push.example.com"].cert_id is None
    assert [call.DomainName for call in live.cert_calls] == ["play.example.com", "push.example.com"]
    assert ssl.live_calls == []


def test_tencent_native_cos_scan_uses_bucket_domains_and_tls_probe(monkeypatch):
    monkeypatch.setattr(TencentCloudProvider, "_ENABLE_BINDING_CACHE_SCAN", True)
    cos = FakeCos(
        buckets=[{"Name": "img-1250000000", "Location": "ap-guangzhou"}],
        domains_by_bucket={
            "img-1250000000": [
                {"Name": "img.example.com", "Status": "ENABLED", "Type": "REST"},
                {"Name": "off.example.com", "Status": "DISABLED", "Type": "REST"},
            ]
        },
    )
    ssl = FakeSsl()
    _patch_clients(monkeypatch, cos=cos, ssl=ssl)

    async def fake_probe(domains):
        assert domains == ["img.example.com"]
        return {"img.example.com": {"exp": datetime(2027, 1, 2, 3, 4, 5), "ca": "test"}}

    monkeypatch.setattr(tencent_provider_mod, "probe_domains_batch", fake_probe)
    provider = TencentCloudProvider(_provider_config())

    bindings = asyncio.run(provider.get_oss_bindings())

    assert len(bindings) == 1
    assert bindings[0].product_id == "ap-guangzhou:img-1250000000:img.example.com"
    assert bindings[0].cert_expire_time == "2027-01-02T03:04:05"
    assert bindings[0].metadata["tls_probe"] is True
    assert cos.domain_calls == ["img-1250000000"]
    assert ssl.cos_calls == []


def test_tencent_native_cos_scan_suppresses_cos_sdk_error_log(monkeypatch, caplog):
    import logging

    monkeypatch.setattr(TencentCloudProvider, "_ENABLE_BINDING_CACHE_SCAN", True)
    class ErrorCos(FakeCos):
        def get_bucket_domain(self, *, Bucket):
            cos_logger = logging.getLogger("qcloud_cos.cos_client")
            cos_logger.error("DomainConfigNotFoundError noise")
            raise CosServiceError(
                "GET",
                {"code": "DomainConfigNotFoundError", "message": "Bucket domain config not found."},
                404,
            )

    _patch_clients(monkeypatch, cos=ErrorCos(), ssl=FakeSsl())
    provider = TencentCloudProvider(_provider_config())

    with caplog.at_level(logging.ERROR, logger="qcloud_cos.cos_client"):
        assert asyncio.run(provider.get_oss_bindings()) == []

    assert not [rec for rec in caplog.records if rec.levelno >= logging.ERROR]


def test_tencent_native_cos_scan_skips_missing_config_but_raises_other_errors(monkeypatch):
    monkeypatch.setattr(TencentCloudProvider, "_ENABLE_BINDING_CACHE_SCAN", True)
    class ErrorCos(FakeCos):
        def __init__(self, code):
            super().__init__([{"Name": "img-1250000000", "Location": "ap-guangzhou"}])
            self.code = code

        def get_bucket_domain(self, *, Bucket):
            raise CosServiceError("GET", {"code": self.code, "message": self.code}, 404)

    missing = ErrorCos("DomainConfigNotFoundError")
    _patch_clients(monkeypatch, cos=missing, ssl=FakeSsl())
    provider = TencentCloudProvider(_provider_config())
    assert asyncio.run(provider.get_oss_bindings()) == []

    denied = ErrorCos("AccessDenied")
    _patch_clients(monkeypatch, cos=denied, ssl=FakeSsl())
    provider = TencentCloudProvider(_provider_config())
    with pytest.raises(RuntimeError, match="AccessDenied"):
        asyncio.run(provider.get_oss_bindings())


def test_tencent_domain_list_uses_domain_api_expiration_date(monkeypatch):
    domains = [
        SimpleNamespace(
            Name="bad-date.com",
            DomainId=1,
            Status="ENABLE",
            RecordCount=0,
        ),
        SimpleNamespace(
            Name="good.com",
            DomainId=2,
            Status="ENABLE",
            RecordCount=0,
        ),
    ]
    registered = [
        SimpleNamespace(DomainName="bad-date.com", ExpirationDate="0000-00-00"),
        SimpleNamespace(DomainName="good.com", ExpirationDate="2027-03-21"),
    ]
    _patch_clients(
        monkeypatch,
        dnspod=FakeDnspod(domains, {"bad-date.com": [], "good.com": []}),
        domain=FakeDomain(registered),
    )

    async def fake_probe(self, domain, *, remark="", sem=None):
        return {"name": domain, "status": "在工作", "remark": remark}

    monkeypatch.setattr(TencentCloudProvider, "_probe_domain", fake_probe)
    provider = TencentCloudProvider(_provider_config())
    result = asyncio.run(provider.get_domain_list())

    assert len(result) == 2
    assert result[0]["domain"] == "bad-date.com"
    assert result[0]["expires_at"] is None
    assert result[1]["domain"] == "good.com"
    assert result[1]["expires_at"] is not None


def test_tencent_domain_list_includes_org_from_simple_info(monkeypatch):
    domains = [
        SimpleNamespace(
            Name="good.com",
            DomainId=1,
            Status="ENABLE",
            RecordCount=0,
        ),
    ]
    registered = [
        SimpleNamespace(DomainName="good.com", ExpirationDate="2027-03-21"),
    ]
    _patch_clients(
        monkeypatch,
        dnspod=FakeDnspod(domains, {"good.com": []}),
        domain=FakeDomain(
            registered,
            simple_info_by_domain={
                "good.com": SimpleNamespace(
                    OrganizationNameCN="广州游逸思网络科技有限公司",
                    OrganizationName="guang zhou you yi si wang luo ke ji you xian gong si",
                ),
            },
        ),
    )

    async def fake_probe(self, domain, *, remark="", sem=None):
        return {"name": domain, "status": "在工作", "remark": remark}

    monkeypatch.setattr(TencentCloudProvider, "_probe_domain", fake_probe)
    provider = TencentCloudProvider(_provider_config())
    result = asyncio.run(provider.get_domain_list())

    assert result[0]["org"] == "广州游逸思网络科技有限公司"


def test_tencent_domain_list_keeps_registered_domain_without_dnspod_zone(monkeypatch):
    registered = [
        SimpleNamespace(DomainName="moved-to-cf.com", ExpirationDate="2027-03-21"),
    ]
    _patch_clients(
        monkeypatch,
        dnspod=FakeDnspod([], {}),
        domain=FakeDomain(
            registered,
            simple_info_by_domain={
                "moved-to-cf.com": SimpleNamespace(
                    OrganizationNameCN="迁移示例公司",
                    OrganizationName="",
                ),
            },
        ),
    )

    provider = TencentCloudProvider(_provider_config())
    result = asyncio.run(provider.get_domain_list())

    assert len(result) == 1
    assert result[0]["domain"] == "moved-to-cf.com"
    assert result[0]["org"] == "迁移示例公司"
    assert result[0]["domain_roles"] == ["registration"]
    assert result[0]["subs"] == []


def test_tencent_domain_list_keeps_dnspod_when_domain_api_fails(monkeypatch):
    domains = [
        SimpleNamespace(
            Name="example.com",
            DomainId=1,
            Status="ENABLE",
            RecordCount=0,
        ),
    ]

    class BrokenDomain:
        def DescribeDomainNameList(self, req):
            raise RuntimeError("domain permission denied")

    _patch_clients(
        monkeypatch,
        dnspod=FakeDnspod(domains, {"example.com": []}),
        domain=BrokenDomain(),
    )

    async def fake_probe(self, domain, *, remark="", sem=None):
        return {"name": domain, "status": "在工作", "remark": remark}

    monkeypatch.setattr(TencentCloudProvider, "_probe_domain", fake_probe)
    provider = TencentCloudProvider(_provider_config())
    result = asyncio.run(provider.get_domain_list())

    assert len(result) == 1
    assert result[0]["domain"] == "example.com"
    assert result[0]["expires_at"] is None


def test_tencent_domain_list_keeps_other_domains_when_record_query_fails(monkeypatch):
    domains = [
        SimpleNamespace(Name="bad-record.com", DomainId=1, Status="ENABLE", VipEndAt=None, RecordCount=0),
        SimpleNamespace(Name="good-record.com", DomainId=2, Status="ENABLE", VipEndAt=None, RecordCount=1),
    ]

    class PartialFailureDnspod(FakeDnspod):
        def DescribeRecordList(self, req):
            if req.Domain == "bad-record.com":
                raise RuntimeError("record permission denied")
            return super().DescribeRecordList(req)

    records = {
        "good-record.com": [
            SimpleNamespace(
                RecordId=11,
                Name="www",
                Type="A",
                Value="1.1.1.1",
                Status="ENABLE",
                TTL=600,
                Line="默认",
                Remark="",
            )
        ]
    }
    _patch_clients(
        monkeypatch,
        dnspod=PartialFailureDnspod(domains, records),
    )

    async def fake_probe(self, domain, *, remark="", sem=None):
        return {"name": domain, "status": "在工作", "remark": remark}

    monkeypatch.setattr(TencentCloudProvider, "_probe_domain", fake_probe)
    provider = TencentCloudProvider(_provider_config())
    result = asyncio.run(provider.get_domain_list())

    assert [item["domain"] for item in result] == ["bad-record.com", "good-record.com"]
    assert result[0]["subs"] == []
    assert result[1]["subs"][0]["name"] == "www.good-record.com"


def test_tencent_clb_bindings_skip_failed_lb(monkeypatch):
    listener_ok = SimpleNamespace(
        ListenerId="lbl-1",
        Port=443,
        ListenerName="https",
        SniSwitch=1,
        Certificate=SimpleNamespace(CertId="lis-cert"),
        Rules=[
            SimpleNamespace(Domain="api.example.com", Url="/", Certificate=SimpleNamespace(CertId="rule-cert")),
        ],
    )
    ssl = FakeSsl(
        clb_instances=[
            _ssl_clb("lb-bad", "bad", []),
            _ssl_clb("lb-ok", "ok", [listener_ok]),
        ]
    )
    _patch_clients(monkeypatch, ssl=ssl)
    _patch_clb_scan_regions(monkeypatch, ["ap-guangzhou"])
    provider = TencentCloudProvider(_provider_config())
    bindings = asyncio.run(provider._get_ssl_lb_bindings("cert-new"))
    assert len(bindings) == 1
    assert bindings[0].product_id == "ap-guangzhou:lb-ok:lbl-1"


def test_tencent_clb_bindings_support_multiple_rule_domains(monkeypatch):
    listener = SimpleNamespace(
        ListenerId="lbl-1",
        Port=443,
        ListenerName="https",
        SniSwitch=1,
        Certificate=SimpleNamespace(CertId="listener-cert"),
        Rules=[
            SimpleNamespace(
                Domain="",
                Domains=["api.example.com", "admin.example.com"],
                Url="/",
                Certificate=SimpleNamespace(CertId="rule-cert"),
            )
        ],
    )

    ssl = FakeSsl(clb_instances=[_ssl_clb("lb-1", "web", [listener])])
    _patch_clients(monkeypatch, ssl=ssl)
    _patch_clb_scan_regions(monkeypatch, ["ap-guangzhou"])
    provider = TencentCloudProvider(_provider_config())
    bindings = asyncio.run(provider._get_ssl_lb_bindings("cert-new"))

    assert {item.domain for item in bindings} == {"api.example.com", "admin.example.com"}
    assert len(bindings) == 2
    assert all(item.product_id == "ap-guangzhou:lb-1:lbl-1" for item in bindings)
    assert all(item.cert_id == "rule-cert" for item in bindings)


def test_tencent_clb_bindings_keep_listener_scope_and_dedupe(monkeypatch):
    listener_sni = SimpleNamespace(
        ListenerId="lbl-1",
        Port=443,
        ListenerName="https",
        SniSwitch=1,
        Certificate=SimpleNamespace(CertId="lis-cert"),
        Rules=[
            SimpleNamespace(Domain="api.example.com", Url="/a", Certificate=SimpleNamespace(CertId="rule-cert")),
            SimpleNamespace(Domain="api.example.com", Url="/b", Certificate=SimpleNamespace(CertId="rule-cert")),
        ],
    )
    listener_no_domain = SimpleNamespace(
        ListenerId="lbl-2",
        Protocol="TCP_SSL",
        Port=8443,
        ListenerName="tls",
        SniSwitch=0,
        Certificate=SimpleNamespace(CertId="vip-cert"),
        Rules=[],
    )

    ssl = FakeSsl(clb_instances=[_ssl_clb("lb-1", "web", [listener_sni, listener_no_domain])])
    _patch_clients(monkeypatch, ssl=ssl)
    _patch_clb_scan_regions(monkeypatch, ["ap-guangzhou"])
    provider = TencentCloudProvider(_provider_config())
    bindings = asyncio.run(provider._get_ssl_lb_bindings("cert-new"))
    assert len(bindings) == 2
    item = next(item for item in bindings if item.domain)
    assert item.product_type == "clb"
    assert item.product_id == "ap-guangzhou:lb-1:lbl-1"
    assert item.domain == "api.example.com"
    assert item.cert_id == "rule-cert"
    assert item.metadata["sni_switch"] is True
    assert item.metadata["listener_id"] == "lbl-1"
    listener_item = next(item for item in bindings if not item.domain)
    assert listener_item.product_id == "ap-guangzhou:lb-1:lbl-2"
    assert listener_item.cert_id == "vip-cert"
    assert listener_item.metadata["listener_scope"] is True


def test_tencent_clb_bindings_scan_multiple_ap_regions(monkeypatch):
    listener_gz = SimpleNamespace(
        ListenerId="lbl-gz",
        Port=443,
        ListenerName="https-gz",
        SniSwitch=1,
        Certificate=SimpleNamespace(CertId="cert-gz"),
        Rules=[
            SimpleNamespace(Domain="gz.example.com", Url="/", Certificate=SimpleNamespace(CertId="cert-gz")),
        ],
    )
    listener_sh = SimpleNamespace(
        ListenerId="lbl-sh",
        Port=443,
        ListenerName="https-sh",
        SniSwitch=1,
        Certificate=SimpleNamespace(CertId="cert-sh"),
        Rules=[
            SimpleNamespace(Domain="sh.example.com", Url="/", Certificate=SimpleNamespace(CertId="cert-sh")),
        ],
    )

    ssl_by_region = {
        "ap-guangzhou": FakeSsl(clb_instances=[_ssl_clb("lb-gz", "gz", [listener_gz])]),
        "ap-shanghai": FakeSsl(clb_instances=[_ssl_clb("lb-sh", "sh", [listener_sh])]),
    }
    ssl_build_regions = _patch_clients(monkeypatch, ssl_by_region=ssl_by_region)
    _patch_clb_scan_regions(monkeypatch, ["ap-guangzhou", "ap-shanghai"])
    provider = TencentCloudProvider(_provider_config())
    bindings = asyncio.run(provider._get_ssl_lb_bindings("cert-new"))

    assert {item.domain for item in bindings} == {"gz.example.com", "sh.example.com"}
    assert {item.product_id for item in bindings} == {
        "ap-guangzhou:lb-gz:lbl-gz",
        "ap-shanghai:lb-sh:lbl-sh",
    }
    assert ssl_build_regions.count("ap-guangzhou") >= 1
    assert ssl_build_regions.count("ap-shanghai") >= 1


def test_tencent_clb_scan_regions_use_configured_list():
    config = ProviderConfig(
        name="腾讯云",
        type="tencent",
        credentials={
            "secret_id": "sid",
            "secret_key": "skey",
            "region": "ap-guangzhou",
            "clb_regions": "ap-shanghai, ap-hongkong",
        },
    )
    provider = TencentCloudProvider(config)
    assert provider._get_clb_scan_regions() == ["ap-shanghai", "ap-hongkong"]


def test_tencent_clb_scan_regions_fail_when_any_region_fails(monkeypatch):
    listener = SimpleNamespace(
        ListenerId="lbl-1",
        Port=443,
        ListenerName="https",
        SniSwitch=1,
        Certificate=SimpleNamespace(CertId="cert-1"),
        Rules=[
            SimpleNamespace(Domain="ok.example.com", Url="/", Certificate=SimpleNamespace(CertId="cert-1")),
        ],
    )

    class FakeSslFail(FakeSsl):
        def DescribeHostClbInstanceList(self, req):
            raise RuntimeError("region permission denied")

    ssl_by_region = {
        "ap-guangzhou": FakeSsl(clb_instances=[_ssl_clb("lb-1", "ok", [listener])]),
        "ap-shanghai": FakeSslFail(),
    }
    _patch_clients(monkeypatch, ssl_by_region=ssl_by_region)
    _patch_clb_scan_regions(monkeypatch, ["ap-guangzhou", "ap-shanghai"])
    provider = TencentCloudProvider(_provider_config())
    with pytest.raises(RuntimeError, match="ap-shanghai"):
        asyncio.run(provider._get_ssl_lb_bindings("cert-new"))


def test_tencent_oss_bindings_keep_enabled_with_cert(monkeypatch):
    ssl_by_region = {
        "ap-guangzhou": FakeSsl(
            cos_instances=[
                SimpleNamespace(
                    Domain="img.example.com",
                    CertId="cert-1",
                    Status="ENABLED",
                    Bucket="img-1250000000",
                    Region="ap-guangzhou",
                ),
                SimpleNamespace(
                    Domain="off.example.com",
                    CertId="cert-2",
                    Status="DISABLED",
                    Bucket="off-1250000000",
                    Region="ap-guangzhou",
                ),
                SimpleNamespace(
                    Domain="nocert.example.com",
                    CertId="",
                    Status="ENABLED",
                    Bucket="nocert-1250000000",
                    Region="ap-guangzhou",
                ),
            ]
        ),
        "ap-hongkong": FakeSsl(
            cos_instances=[
                SimpleNamespace(
                    Domain="hk.example.com",
                    CertId="cert-3",
                    Status="enabled",
                    Bucket="ssl-server-1251810746",
                    Region="ap-hongkong",
                ),
            ]
        ),
    }
    ssl_build_regions = _patch_clients(monkeypatch, ssl_by_region=ssl_by_region)
    _patch_cos_scan_regions(monkeypatch, ["ap-guangzhou", "ap-hongkong"])
    provider = TencentCloudProvider(_provider_config())
    bindings = asyncio.run(provider._get_ssl_oss_bindings("cert-new"))

    assert {(item.domain, item.product_id, item.cert_id) for item in bindings} == {
        ("img.example.com", "ap-guangzhou:img-1250000000:img.example.com", "cert-1"),
        ("hk.example.com", "ap-hongkong:ssl-server-1251810746:hk.example.com", "cert-3"),
        ("nocert.example.com", "ap-guangzhou:nocert-1250000000:nocert.example.com", None),
    }
    assert all(item.product_type == "oss" for item in bindings)
    assert all(item.resource_type == "cos" for item in bindings)
    assert ssl_build_regions.count("ap-guangzhou") >= 1
    assert ssl_build_regions.count("ap-hongkong") >= 1
    gz_ssl = ssl_by_region["ap-guangzhou"]
    assert gz_ssl.cos_calls[0].IsCache == 0
    assert gz_ssl.cos_calls[0].CertificateId == "cert-new"
    assert gz_ssl.cos_calls[0].ResourceType == "cos"


def test_tencent_cos_scan_regions_use_configured_list():
    config = ProviderConfig(
        name="腾讯云",
        type="tencent",
        credentials={
            "secret_id": "sid",
            "secret_key": "skey",
            "region": "ap-guangzhou",
            "cos_regions": "ap-shanghai, ap-hongkong",
        },
    )
    provider = TencentCloudProvider(config)
    assert provider._get_cos_scan_regions() == ["ap-shanghai", "ap-hongkong"]


def test_tencent_deploy_oss_instance_format_uses_bucket_region(monkeypatch):
    ssl_by_region: dict[str, FakeSsl] = {}
    ssl_build_regions = _patch_clients(monkeypatch, ssl_by_region=ssl_by_region)
    deployer = TencentDeployer(secret_id="a", secret_key="b", region="ap-guangzhou")
    target = DeployTarget(
        provider="tencent",
        product_type="oss",
        product_id="ap-hongkong:ssl-server-1251810746:cdn.example.com",
        domain="cdn.example.com",
    )
    result = asyncio.run(deployer.deploy("CERT", "KEY", target, cert_id="cert-1"))
    assert result.success is True
    assert "ap-hongkong" in ssl_build_regions
    req = ssl_by_region["ap-hongkong"].deploy_calls[0]
    assert req.ResourceType == "cos"
    assert req.InstanceIdList == ["ap-hongkong|ssl-server-1251810746|cdn.example.com"]
    assert req.CertificateId == "cert-1"


def test_tencent_deploy_oss_bad_product_id_fails(monkeypatch):
    _patch_clients(monkeypatch, ssl=FakeSsl())
    deployer = TencentDeployer(secret_id="a", secret_key="b")
    target = DeployTarget(
        provider="tencent",
        product_type="oss",
        product_id="bad",
        domain="cdn.example.com",
    )
    result = asyncio.run(deployer.deploy("CERT", "KEY", target, cert_id="cert-1"))
    assert result.success is False
    assert "product_id" in result.message


class FakeSsl:
    def __init__(
        self,
        *,
        cert_id="cert-new",
        repeat_id="",
        deploy_status=1,
        deploy_responses=None,
        deploy_details=None,
        default_deploy_detail=None,
        cdn_instances=None,
        clb_instances=None,
        cos_instances=None,
        live_instances=None,
        teo_instances=None,
    ):
        self.cert_id = cert_id
        self.repeat_id = repeat_id
        self.deploy_status = deploy_status
        self.deploy_responses = list(deploy_responses or [])
        self.deploy_details = {
            str(key): list(value)
            for key, value in (deploy_details or {}).items()
        }
        self.default_deploy_detail = default_deploy_detail
        self.upload_calls = []
        self.deploy_calls = []
        self.deploy_detail_calls = []
        self.cdn_calls = []
        self.clb_calls = []
        self.cos_calls = []
        self.live_calls = []
        self.teo_calls = []
        self.cdn_instances = list(cdn_instances or [])
        self.clb_instances = list(clb_instances or [])
        self.cos_instances = list(cos_instances or [])
        self.live_instances = list(live_instances or [])
        self.teo_instances = list(teo_instances or [])

    def UploadCertificate(self, req):
        self.upload_calls.append(req)
        return SimpleNamespace(CertificateId=self.cert_id, RepeatCertId=self.repeat_id)

    def DeployCertificateInstance(self, req):
        if not req.CertificateId or not req.InstanceIdList or not req.ResourceType:
            raise RuntimeError("FailedOperation.InvalidParam")
        if req.Status is not None or req.IsCache is not None:
            raise RuntimeError("FailedOperation.InvalidParam: unexpected optional deploy fields")
        self.deploy_calls.append(req)
        if self.deploy_responses:
            return self.deploy_responses.pop(0)
        return SimpleNamespace(DeployStatus=self.deploy_status, DeployRecordId=99)

    def DescribeHostDeployRecordDetail(self, req):
        self.deploy_detail_calls.append(req)
        queue = self.deploy_details.get(str(req.DeployRecordId))
        if queue:
            return queue.pop(0)
        return self.default_deploy_detail or _deploy_detail_response(success=1)

    def _page(self, items, req):
        filters = list(getattr(req, "Filters", None) or [])
        if not getattr(req, "CertificateId", None):
            raise RuntimeError("FailedOperation.InvalidParam: CertificateId")
        if getattr(req, "IsCache", None) not in (0, 1):
            raise RuntimeError("FailedOperation.InvalidParam: IsCache")
        if not filters or filters[0].FilterKey != "domainMatch" or filters[0].FilterValue != "1":
            raise RuntimeError("FailedOperation.InvalidParam: Filters")
        offset = int(getattr(req, "Offset", 0) or 0)
        limit = int(getattr(req, "Limit", 10) or 10)
        page = items[offset:offset + limit]
        return SimpleNamespace(InstanceList=page, TotalCount=len(items))

    def DescribeHostCdnInstanceList(self, req):
        self.cdn_calls.append(req)
        return self._page(self.cdn_instances, req)

    def DescribeHostClbInstanceList(self, req):
        self.clb_calls.append(req)
        return self._page(self.clb_instances, req)

    def DescribeHostCosInstanceList(self, req):
        self.cos_calls.append(req)
        return self._page(self.cos_instances, req)

    def DescribeHostLiveInstanceList(self, req):
        self.live_calls.append(req)
        return self._page(self.live_instances, req)

    def DescribeHostTeoInstanceList(self, req):
        self.teo_calls.append(req)
        return self._page(self.teo_instances, req)


def _deploy_detail_response(
    *,
    success: int = 0,
    failed: int = 0,
    running: int = 0,
    pending: int = 0,
    error: str = "",
):
    total = success + failed + running + pending
    details = []
    if failed:
        details.append(SimpleNamespace(Status=2, ErrorMsg=error or "deploy failed"))
    return SimpleNamespace(
        TotalCount=total,
        SuccessTotalCount=success,
        FailedTotalCount=failed,
        RunningTotalCount=running,
        PendingTotalCount=pending,
        DeployRecordDetailList=details,
    )


def test_tencent_live_bindings_keep_https_enabled_with_cert(monkeypatch):
    ssl = FakeSsl(
        live_instances=[
            SimpleNamespace(Domain="live.example.com", CertId="cert-1", Status=1),
            SimpleNamespace(Domain="off.example.com", CertId="cert-2", Status=0),
            SimpleNamespace(Domain="nocert.example.com", CertId="", Status=1),
            SimpleNamespace(Domain="pending.example.com", CertId="", Status=-1),
        ]
    )
    _patch_clients(monkeypatch, ssl=ssl)
    provider = TencentCloudProvider(_provider_config())
    bindings = asyncio.run(provider._get_ssl_live_bindings("cert-new"))

    assert len(bindings) == 3
    by_domain = {item.domain: item for item in bindings}
    assert set(by_domain) == {"live.example.com", "nocert.example.com", "pending.example.com"}
    item = by_domain["live.example.com"]
    assert item.product_type == "live"
    assert item.product_id == "live.example.com"
    assert item.cert_id == "cert-1"
    assert by_domain["nocert.example.com"].cert_id is None
    assert item.metadata["live_deploy_mode"] == "ssl"
    assert ssl.live_calls[0].IsCache == 0
    assert ssl.live_calls[0].CertificateId == "cert-new"


def test_tencent_teo_bindings_paginate_dedupe_and_keep_status(monkeypatch):
    monkeypatch.setattr(tencent_provider_mod, "_SSL_HOST_PAGE_SIZE", 2)
    ssl = FakeSsl(
        teo_instances=[
            SimpleNamespace(
                Host="eo-a.example.com",
                CertId="cert-old",
                ZoneId="zone-a",
                Status="deployed",
            ),
            SimpleNamespace(
                Host="eo-b.example.com",
                CertId="",
                ZoneId="zone-b",
                Status="processing",
            ),
            SimpleNamespace(
                Host="eo-a.example.com",
                CertId="cert-old",
                ZoneId="zone-a",
                Status="deployed",
            ),
            SimpleNamespace(Host="", CertId="", ZoneId="zone-empty", Status="failed"),
        ]
    )
    _patch_clients(monkeypatch, ssl=ssl)
    provider = TencentCloudProvider(_provider_config())

    bindings = asyncio.run(provider._get_ssl_teo_bindings("cert-new"))

    assert len(ssl.teo_calls) == 2
    assert [call.Offset for call in ssl.teo_calls] == [0, 2]
    assert all(call.IsCache == 0 for call in ssl.teo_calls)
    assert all(call.CertificateId == "cert-new" for call in ssl.teo_calls)
    assert [item.domain for item in bindings] == ["eo-a.example.com", "eo-b.example.com"]
    assert all(item.product_type == "teo" for item in bindings)
    assert all(item.resource_type == "teo" for item in bindings)
    assert bindings[0].cert_id == "cert-old"
    assert bindings[0].metadata["zone_id"] == "zone-a"
    assert bindings[1].status == "processing"


def test_tencent_ssl_deploy_bindings_respects_disabled_eo_scan(monkeypatch):
    ssl = FakeSsl(
        teo_instances=[
            SimpleNamespace(
                Host="eo.example.com",
                CertId="cert-old",
                ZoneId="zone-a",
                Status="deployed",
            )
        ]
    )
    _patch_clients(monkeypatch, ssl=ssl)
    provider = TencentCloudProvider(_provider_config())

    bindings = asyncio.run(
        provider.get_ssl_deploy_bindings(
            "cert-new",
            cdn_scan=False,
            live_scan=False,
            eo_scan=False,
            lb_scan=False,
            oss_scan=False,
        )
    )

    assert bindings == []
    assert ssl.teo_calls == []


def test_tencent_deploy_live_uses_ssl_instance_format(monkeypatch):
    ssl = FakeSsl()
    _patch_clients(monkeypatch, ssl=ssl)
    deployer = TencentDeployer(secret_id="a", secret_key="b")
    target = DeployTarget(
        provider="tencent",
        product_type="live",
        product_id="live.example.com",
        domain="live.example.com",
    )
    result = asyncio.run(deployer.deploy("CERT", "KEY", target, cert_id="cert-1"))
    assert result.success is True
    req = ssl.deploy_calls[0]
    assert req.ResourceType == "live"
    assert req.InstanceIdList == ["live.example.com"]
    assert req.CertificateId == "cert-1"


def test_tencent_deploy_teo_uses_ssl_instance_format(monkeypatch):
    ssl = FakeSsl()
    _patch_clients(monkeypatch, ssl=ssl)
    deployer = TencentDeployer(secret_id="a", secret_key="b")
    target = DeployTarget(
        provider="tencent",
        product_type="teo",
        product_id="eo.example.com",
        domain="eo.example.com",
    )

    result = asyncio.run(deployer.deploy("CERT", "KEY", target, cert_id="cert-1"))

    assert result.success is True
    req = ssl.deploy_calls[0]
    assert req.ResourceType == "teo"
    assert req.InstanceIdList == ["eo.example.com"]
    assert req.CertificateId == "cert-1"


def test_tencent_deploy_teo_reports_ssl_failure(monkeypatch):
    ssl = FakeSsl(
        deploy_details={"99": [_deploy_detail_response(failed=1, error="teo deploy failed")]}
    )
    _patch_clients(monkeypatch, ssl=ssl)
    deployer = TencentDeployer(secret_id="a", secret_key="b", deploy_poll_interval_seconds=0)
    target = DeployTarget(
        provider="tencent",
        product_type="teo",
        product_id="eo.example.com",
        domain="eo.example.com",
    )

    result = asyncio.run(deployer.deploy("CERT", "KEY", target, cert_id="cert-1"))

    assert result.success is False
    assert "teo deploy failed" in result.message


def test_tencent_upload_certificate_reuses_id_and_repeat(monkeypatch):
    monkeypatch.setattr(tencent_deploy_mod.TimeUtil, "now", staticmethod(_FixedTime.now))
    ssl = FakeSsl(cert_id="cert-1")
    _patch_clients(monkeypatch, ssl=ssl)
    deployer = TencentDeployer(secret_id="a", secret_key="b")
    cert_id = asyncio.run(deployer.upload_certificate("CERT", "KEY", main_domain="api.example.com"))
    assert cert_id == "cert-1"
    assert ssl.upload_calls[0].Alias == "api.example.com-20260316123456"
    assert ssl.upload_calls[0].Repeatable is False

    ssl2 = FakeSsl(cert_id="", repeat_id="cert-old")
    _patch_clients(monkeypatch, ssl=ssl2)
    deployer2 = TencentDeployer(secret_id="a", secret_key="b")
    assert asyncio.run(deployer2.upload_certificate("CERT", "KEY", main_domain=None)) == "cert-old"
    assert ssl2.upload_calls[0].Alias == "renew-20260316123456"


def test_tencent_deploy_clb_uses_region_ssl_client(monkeypatch):
    ssl_by_region: dict[str, FakeSsl] = {}
    ssl_build_regions = _patch_clients(monkeypatch, ssl_by_region=ssl_by_region)
    deployer = TencentDeployer(secret_id="a", secret_key="b", region="ap-guangzhou")

    target = DeployTarget(
        provider="tencent",
        product_type="clb",
        product_id="ap-shanghai:lb-1:lbl-1",
        domain="api.example.com",
        metadata={"listener_id": "lbl-1", "sni_switch": True},
    )
    result = asyncio.run(deployer.deploy("CERT", "KEY", target, cert_id="cert-1"))
    assert result.success is True
    assert "ap-shanghai" in ssl_build_regions
    assert "ap-shanghai" in ssl_by_region


def test_tencent_deploy_cdn_and_clb_instance_format(monkeypatch):
    ssl = FakeSsl()
    _patch_clients(monkeypatch, ssl=ssl)
    deployer = TencentDeployer(secret_id="a", secret_key="b")

    cdn_target = DeployTarget(
        provider="tencent",
        product_type="cdn",
        product_id="cdn.example.com",
        domain="cdn.example.com",
        metadata={"https_billing": "off", "cdn_deploy_mode": "ssl"},
    )
    cdn_result = asyncio.run(deployer.deploy("CERT", "KEY", cdn_target, cert_id="cert-1"))
    assert cdn_result.success is True
    assert ssl.upload_calls == []
    cdn_req = ssl.deploy_calls[0]
    assert cdn_req.ResourceType == "cdn"
    assert cdn_req.InstanceIdList == ["cdn.example.com|off"]
    assert cdn_req.CertificateId == "cert-1"

    clb_target = DeployTarget(
        provider="tencent",
        product_type="clb",
        product_id="ap-guangzhou:lb-1:lbl-1",
        domain="api.example.com",
        metadata={"listener_id": "lbl-1", "sni_switch": True},
    )
    clb_result = asyncio.run(deployer.deploy("CERT", "KEY", clb_target, cert_id="cert-1"))
    assert clb_result.success is True
    clb_req = ssl.deploy_calls[1]
    assert clb_req.ResourceType == "clb"
    assert clb_req.InstanceIdList == ["lb-1|lbl-1|api.example.com"]

    nosni_target = DeployTarget(
        provider="tencent",
        product_type="clb",
        product_id="ap-guangzhou:lb-2:lbl-2",
        domain="vip.example.com",
        metadata={"listener_id": "lbl-2", "sni_switch": False},
    )
    nosni_result = asyncio.run(deployer.deploy("CERT", "KEY", nosni_target, cert_id="cert-1"))
    assert nosni_result.success is True
    assert ssl.deploy_calls[2].InstanceIdList == ["lb-2|lbl-2"]


def test_tencent_deploy_cdn_old_cache_uses_modify_domain_config(monkeypatch):
    ssl = FakeSsl()
    cdn_calls: list[SimpleNamespace] = []

    class FakeCdnDeploy:
        def ModifyDomainConfig(self, req):
            cdn_calls.append(req)

    _patch_clients(monkeypatch, ssl=ssl, cdn=FakeCdnDeploy())
    deployer = TencentDeployer(secret_id="a", secret_key="b")
    target = DeployTarget(
        provider="tencent",
        product_type="cdn",
        product_id="cdn.example.com",
        domain="",
        metadata={},
    )
    result = asyncio.run(deployer.deploy("CERT", "KEY", target, cert_id="cert-1"))
    assert result.success is True
    assert ssl.deploy_calls == []
    assert len(cdn_calls) == 1
    assert cdn_calls[0].Domain == "cdn.example.com"
    assert cdn_calls[0].Route == "Https.CertInfo.CertId"
    assert json.loads(cdn_calls[0].Value) == {"update": "cert-1"}


def test_tencent_deploy_waits_running_record_until_success(monkeypatch):
    ssl = FakeSsl(
        deploy_details={
            "99": [
                _deploy_detail_response(running=1),
                _deploy_detail_response(success=1),
            ]
        }
    )
    _patch_clients(monkeypatch, ssl=ssl)
    deployer = TencentDeployer(
        secret_id="a",
        secret_key="b",
        deploy_poll_interval_seconds=0,
    )
    target = DeployTarget(
        provider="tencent",
        product_type="clb",
        product_id="ap-guangzhou:lb-1:lbl-1",
        domain="api.example.com",
        metadata={"listener_id": "lbl-1", "sni_switch": True},
    )
    result = asyncio.run(deployer.deploy("CERT", "KEY", target, cert_id="cert-1"))
    assert result.success is True
    assert len(ssl.deploy_detail_calls) == 2


def test_tencent_deploy_waits_conflicting_task_then_retries(monkeypatch):
    ssl = FakeSsl(
        deploy_responses=[
            SimpleNamespace(DeployStatus=0, DeployRecordId=88),
            SimpleNamespace(DeployStatus=1, DeployRecordId=99),
        ],
        deploy_details={
            "88": [_deploy_detail_response(success=1)],
            "99": [_deploy_detail_response(success=1)],
        },
    )
    _patch_clients(monkeypatch, ssl=ssl)
    deployer = TencentDeployer(secret_id="a", secret_key="b", deploy_poll_interval_seconds=0)
    target = DeployTarget(
        provider="tencent",
        product_type="cdn",
        product_id="cdn.example.com",
        domain="cdn.example.com",
        metadata={"cdn_deploy_mode": "ssl", "https_billing": "on"},
    )

    result = asyncio.run(deployer.deploy("CERT", "KEY", target, cert_id="cert-1"))

    assert result.success is True
    assert len(ssl.deploy_calls) == 2
    assert [call.DeployRecordId for call in ssl.deploy_detail_calls] == ["88", "99"]


def test_tencent_deploy_final_failure_returns_failed_result(monkeypatch):
    ssl = FakeSsl(
        deploy_details={"99": [_deploy_detail_response(failed=1, error="domain mismatch")]}
    )
    _patch_clients(monkeypatch, ssl=ssl)
    deployer = TencentDeployer(secret_id="a", secret_key="b", deploy_poll_interval_seconds=0)
    target = DeployTarget(
        provider="tencent",
        product_type="live",
        product_id="live.example.com",
        domain="live.example.com",
    )

    result = asyncio.run(deployer.deploy("CERT", "KEY", target, cert_id="cert-1"))

    assert result.success is False
    assert "domain mismatch" in result.message


def test_tencent_deploy_poll_timeout_returns_failed_result(monkeypatch):
    ssl = FakeSsl(default_deploy_detail=_deploy_detail_response(running=1))
    _patch_clients(monkeypatch, ssl=ssl)
    deployer = TencentDeployer(
        secret_id="a",
        secret_key="b",
        deploy_poll_interval_seconds=0,
        deploy_poll_timeout_seconds=0.01,
    )
    target = DeployTarget(
        provider="tencent",
        product_type="live",
        product_id="live.example.com",
        domain="live.example.com",
    )

    result = asyncio.run(deployer.deploy("CERT", "KEY", target, cert_id="cert-1"))

    assert result.success is False
    assert "等待超时" in result.message


def test_example_product_scan_tencent():
    data = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "config.example.yaml").read_text(encoding="utf-8")
    )
    tencent = data["product_scan"]["tencent"]
    assert tencent["cdn_scan"] is True
    assert tencent["live_scan"] is True
    assert tencent["eo_scan"] is True
    assert tencent["lb_scan"] is False
    assert tencent["domain_scan"] is True
    assert tencent["oss_scan"] is True
    assert tencent["ecs_scan"] is False
