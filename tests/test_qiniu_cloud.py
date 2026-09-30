"""七牛云 API、Provider 与 Deployer 单元测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import requests
import yaml

import app.providers.qiniu as qiniu_provider_mod
import app.utils.qiniu_api as qiniu_api
from app.deploys.base import DeployTarget
from app.deploys.qiniu import QiniuDeployer
from app.providers.qiniu import QiniuCloudProvider
from app.schemas.config import ProviderConfig


class FakeResponse:
    def __init__(self, payload, *, status_code: int = 200, reason: str = "OK"):
        self._payload = payload
        self.status_code = status_code
        self.reason = reason
        self.ok = 200 <= status_code < 400
        self.content = json.dumps(payload).encode("utf-8")

    def json(self):
        return self._payload


def test_qiniu_request_uses_qiniu_auth_and_exact_compact_body(monkeypatch):
    captured = {}

    def fake_request(method, url, *, headers, data, auth, timeout):
        request = requests.Request(method, url, headers=headers, data=data).prepare()
        auth(request)
        captured.update(
            method=method,
            url=url,
            headers=dict(request.headers),
            data=request.body,
            timeout=timeout,
        )
        return FakeResponse({"code": 200})

    monkeypatch.setattr(qiniu_api.requests, "request", fake_request)
    auth = qiniu_api.build_qiniu_auth("test-ak", "test-sk")
    result = qiniu_api.qiniu_request(
        auth,
        "https://api.qiniu.com/domain/测试.example.com/httpsconf",
        method="PUT",
        body={"certId": "cert-1", "forceHttps": False},
    )

    assert result == {"code": 200}
    assert captured["headers"]["Authorization"].startswith("Qiniu test-ak:")
    assert captured["headers"]["X-Qiniu-Date"].endswith("Z")
    assert captured["data"] == (
        '{"certId":"cert-1","forceHttps":false}'.encode("utf-8")
    )


def test_qiniu_request_uses_qbox_auth_for_fusion(monkeypatch):
    captured = {}

    def fake_request(method, url, *, headers, data, auth, timeout):
        captured.update(headers=headers, auth=auth, data=data)
        return FakeResponse({"certID": "cert-1"})

    monkeypatch.setattr(qiniu_api.requests, "request", fake_request)
    auth = qiniu_api.build_qiniu_auth("test-ak", "test-sk")
    result = qiniu_api.qiniu_request(
        auth,
        "https://fusion.qiniuapi.com/sslcert",
        method="POST",
        body={"name": "证书", "pri": "KEY", "ca": "CERT"},
    )

    assert result["certID"] == "cert-1"
    assert captured["headers"]["Authorization"].startswith("QBox test-ak:")
    assert captured["auth"] is None
    assert captured["data"] == (
        '{"name":"证书","pri":"KEY","ca":"CERT"}'.encode("utf-8")
    )


def test_qiniu_kodo_helpers_use_uc_qiniu_auth(monkeypatch):
    calls = []

    def fake_request(method, url, *, headers, data, auth, timeout):
        request = requests.Request(method, url, headers=headers, data=data).prepare()
        auth(request)
        calls.append((url, dict(request.headers)))
        if url == qiniu_api.QINIU_BUCKETS_API:
            return FakeResponse(["assets", "logs"])
        return FakeResponse(["img.example.com"])

    monkeypatch.setattr(qiniu_api.requests, "request", fake_request)
    auth = qiniu_api.build_qiniu_auth("test-ak", "test-sk")

    assert qiniu_api.list_qiniu_buckets(auth) == ["assets", "logs"]
    assert qiniu_api.list_qiniu_bucket_domains(auth, "assets bucket") == [
        "img.example.com"
    ]
    assert calls[1][0] == "https://uc.qiniuapi.com/v2/domains?tbl=assets+bucket"
    assert all(
        headers["Authorization"].startswith("Qiniu test-ak:")
        for _, headers in calls
    )


def test_qiniu_request_rejects_unknown_host_before_network(monkeypatch):
    monkeypatch.setattr(
        qiniu_api.requests,
        "request",
        lambda *args, **kwargs: pytest.fail("不应发送网络请求"),
    )
    with pytest.raises(ValueError, match="不支持的七牛 API 主机"):
        qiniu_api.qiniu_request(
            qiniu_api.build_qiniu_auth("test-ak", "test-sk"),
            "https://example.com/domain",
        )


def test_qiniu_request_raises_for_business_error(monkeypatch):
    monkeypatch.setattr(
        qiniu_api.requests,
        "request",
        lambda *args, **kwargs: FakeResponse(
            {"code": 400324, "error": "证书私钥不匹配"}
        ),
    )
    with pytest.raises(qiniu_api.QiniuAPIError, match="400324.*证书私钥不匹配"):
        qiniu_api.qiniu_request(
            qiniu_api.build_qiniu_auth("test-ak", "test-sk"),
            qiniu_api.QINIU_CERT_API,
        )


def test_list_qiniu_domains_follows_marker(monkeypatch):
    urls = []
    pages = iter(
        [
            {"domains": [{"name": "a.example.com"}], "marker": "next token"},
            {"domains": [{"name": "b.example.com"}], "marker": ""},
        ]
    )

    def fake_request(auth, url, **kwargs):
        urls.append(url)
        return next(pages)

    monkeypatch.setattr(qiniu_api, "qiniu_request", fake_request)
    result = qiniu_api.list_qiniu_domains(object())

    assert [item["name"] for item in result] == ["a.example.com", "b.example.com"]
    assert urls == [
        "https://api.qiniu.com/domain?limit=1000",
        "https://api.qiniu.com/domain?limit=1000&marker=next+token",
    ]


def test_list_qiniu_domains_rejects_stuck_marker(monkeypatch):
    monkeypatch.setattr(
        qiniu_api,
        "qiniu_request",
        lambda *args, **kwargs: {"domains": [], "marker": "same"},
    )
    with pytest.raises(qiniu_api.QiniuAPIError, match="marker 未前进"):
        qiniu_api.list_qiniu_domains(object())


def _provider() -> QiniuCloudProvider:
    return QiniuCloudProvider(
        ProviderConfig(
            name="qiniu-test",
            type="qiniu",
            credentials={
                "access_key_id": "test-ak",
                "access_key_secret": "test-sk",
            },
        )
    )


def test_qiniu_provider_filters_dcdn_and_builds_binding(monkeypatch):
    monkeypatch.setattr(
        qiniu_provider_mod,
        "list_qiniu_domains",
        lambda auth: [
            {"name": ""},
            {"name": "dynamic.example.com", "product": "dcdn"},
            {
                "name": "cdn.example.com",
                "product": "cdn",
                "type": "normal",
                "protocol": "https",
                "operatingState": "success",
                "cname": "cdn.qiniudns.com",
                "https": {"certId": "old-cert"},
            },
        ],
    )

    provider = _provider()
    domains = asyncio.run(provider.get_domain_list())
    bindings = asyncio.run(provider.get_cdn_bindings())

    assert domains == [
        {"domain": "cdn.example.com", "type": "normal", "status": "success"}
    ]
    assert len(bindings) == 1
    assert bindings[0].provider_name == "qiniu-test"
    assert bindings[0].product_id == "cdn.example.com"
    assert bindings[0].cert_id == "old-cert"
    assert bindings[0].metadata["protocol"] == "https"


def test_qiniu_provider_classifies_bucket_domain_as_oss(monkeypatch):
    domain_items = [
        {
            "name": "cdn.example.com",
            "product": "cdn",
            "protocol": "https",
            "source": {"sourceType": "domain", "sourceDomain": "origin.example.com"},
        },
        {
            "name": "img.example.com",
            "product": "cdn",
            "protocol": "https",
            "operatingState": "success",
            "source": {
                "sourceType": "qiniuBucket",
                "sourceQiniuBucket": "assets",
            },
            "https": {"certId": "old-cert"},
        },
    ]
    monkeypatch.setattr(qiniu_provider_mod, "list_qiniu_domains", lambda auth: domain_items)
    monkeypatch.setattr(qiniu_provider_mod, "list_qiniu_buckets", lambda auth: ["assets"])
    monkeypatch.setattr(
        qiniu_provider_mod,
        "list_qiniu_bucket_domains",
        lambda auth, bucket: ["img.example.com", "source.example.com"],
    )

    provider = _provider()
    cdn_bindings = asyncio.run(provider.get_cdn_bindings())
    oss_bindings = asyncio.run(provider.get_oss_bindings())

    assert [item.domain for item in cdn_bindings] == ["cdn.example.com"]
    assert len(oss_bindings) == 1
    assert oss_bindings[0].product_type == "oss"
    assert oss_bindings[0].product_id == "assets:img.example.com"
    assert oss_bindings[0].cert_id == "old-cert"
    assert oss_bindings[0].metadata["bucket"] == "assets"


def test_qiniu_deployer_uploads_to_fusion_and_reuses_cert_id(monkeypatch):
    deployer = QiniuDeployer("test-ak", "test-sk")
    calls = []

    def fake_request(url, method="GET", body=None):
        calls.append((url, method, body))
        if url == qiniu_api.QINIU_CERT_API:
            return {"certID": "cert-1"}
        return {"code": 200}

    monkeypatch.setattr(deployer, "_make_request", fake_request)
    cert_id = asyncio.run(deployer.upload_certificate("CERT", "KEY", "example.com"))
    result = asyncio.run(
        deployer.deploy(
            "CERT",
            "KEY",
            DeployTarget(
                provider="qiniu",
                product_type="cdn",
                product_id="cdn.example.com",
                domain="cdn.example.com",
                metadata={"protocol": "https"},
            ),
            cert_id=cert_id,
        )
    )

    assert result.success is True
    assert result.cert_id == "cert-1"
    upload_calls = [item for item in calls if item[0] == qiniu_api.QINIU_CERT_API]
    assert len(upload_calls) == 1
    assert upload_calls[0][2]["name"].startswith("example.com-")
    assert upload_calls[0][2]["pri"] == "KEY"
    assert upload_calls[0][2]["ca"] == "CERT"
    assert calls[-1] == (
        "https://api.qiniu.com/domain/cdn.example.com/httpsconf",
        "PUT",
        {"certId": "cert-1"},
    )


def test_qiniu_deployer_uses_sslize_for_http_domain(monkeypatch):
    deployer = QiniuDeployer("test-ak", "test-sk")
    calls = []
    monkeypatch.setattr(
        deployer,
        "_make_request",
        lambda url, method="GET", body=None: calls.append((url, method, body)) or {"code": 200},
    )

    result = asyncio.run(
        deployer.deploy(
            "CERT",
            "KEY",
            DeployTarget(
                provider="qiniu",
                product_type="cdn",
                domain="http.example.com",
                metadata={"protocol": "http"},
            ),
            cert_id="cert-1",
        )
    )

    assert result.success is True
    assert calls == [
        (
            "https://api.qiniu.com/domain/http.example.com/sslize",
            "PUT",
            {"certId": "cert-1", "forceHttps": False, "http2Enable": True},
        )
    ]


def test_qiniu_deployer_deploys_oss_acceleration_domain(monkeypatch):
    deployer = QiniuDeployer("test-ak", "test-sk")
    calls = []
    monkeypatch.setattr(
        deployer,
        "_make_request",
        lambda url, method="GET", body=None: calls.append((url, method, body)) or {"code": 200},
    )

    result = asyncio.run(
        deployer.deploy(
            "CERT",
            "KEY",
            DeployTarget(
                provider="qiniu",
                product_type="oss",
                product_id="assets:img.example.com",
                domain="img.example.com",
                metadata={"protocol": "https", "bucket": "assets"},
            ),
            cert_id="cert-1",
        )
    )

    assert result.success is True
    assert result.cert_id == "cert-1"
    assert calls == [
        (
            "https://api.qiniu.com/domain/img.example.com/httpsconf",
            "PUT",
            {"certId": "cert-1"},
        )
    ]


def test_qiniu_deployer_rejects_invalid_oss_product_id(monkeypatch):
    deployer = QiniuDeployer("test-ak", "test-sk")
    monkeypatch.setattr(
        deployer,
        "_make_request",
        lambda *args, **kwargs: pytest.fail("不应调用七牛 API"),
    )
    result = asyncio.run(
        deployer.deploy(
            "CERT",
            "KEY",
            DeployTarget(
                provider="qiniu",
                product_type="oss",
                product_id="assets",
                domain="img.example.com",
            ),
            cert_id="cert-1",
        )
    )
    assert result.success is False
    assert "<bucket>:<domain>" in result.message


def test_qiniu_deployer_rejects_other_product_without_api_call(monkeypatch):
    deployer = QiniuDeployer("test-ak", "test-sk")
    monkeypatch.setattr(
        deployer,
        "_make_request",
        lambda *args, **kwargs: pytest.fail("不应调用七牛 API"),
    )
    result = asyncio.run(
        deployer.deploy(
            "CERT",
            "KEY",
            DeployTarget(provider="qiniu", product_type="clb", domain="a.example.com"),
        )
    )
    assert result.success is False
    assert "仅支持 CDN/对象存储" in result.message


def test_qiniu_example_config_enables_cdn_and_oss_scan():
    path = Path(__file__).resolve().parents[1] / "config.example.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    scan = config["product_scan"]["qiniu"]

    assert scan["cdn_scan"] is True
    assert scan["live_scan"] is False
    assert scan["oss_scan"] is True
    assert scan["lb_scan"] is False
    assert scan["ecs_scan"] is False
    assert scan["domain_scan"] is False
