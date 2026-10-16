import asyncio
from types import SimpleNamespace

import pytest

from app.deploys import aliyun as mod
from app.deploys.base import DeployTarget
from app.deploys.aliyun import AliyunDeployer


def make_deployer(monkeypatch):
    calls = []

    def build(access_key_id, access_key_secret, region):
        calls.append(region)
        return {
            "cdn_client": SimpleNamespace(),
            "slb_client": SimpleNamespace(),
            "cas_client": SimpleNamespace(),
            "alb_client": SimpleNamespace(),
            "alb_models": SimpleNamespace(
                UpdateListenerAttributeRequestCertificates=lambda **kw: SimpleNamespace(**kw),
                UpdateListenerAttributeRequest=lambda **kw: SimpleNamespace(**kw),
            ),
            "oss_client": SimpleNamespace(),
            "oss_models": SimpleNamespace(
                CertificateConfiguration=lambda **kw: SimpleNamespace(**kw),
                BucketCnameConfiguration=lambda **kw: SimpleNamespace(**kw),
                Cname=lambda **kw: SimpleNamespace(**kw),
                PutCnameRequest=lambda **kw: SimpleNamespace(**kw),
            ),
            "oss_sdk": object(),
        }

    monkeypatch.setattr(mod, "build_aliyun_deployer_clients", build)
    return AliyunDeployer("ak", "sk", "cn-hangzhou"), calls


def test_region_clients_are_cached(monkeypatch):
    d, calls = make_deployer(monkeypatch)
    first = d._get_region_clients("cn-hangzhou")
    second = d._get_region_clients("cn-hangzhou")
    assert first is second
    assert calls == ["cn-hangzhou"]

    other = d._get_region_clients("cn-beijing")
    assert other is not first
    assert calls == ["cn-hangzhou", "cn-beijing"]


def test_upload_certificate_validates_and_wraps_errors(monkeypatch):
    d, _ = make_deployer(monkeypatch)
    with pytest.raises(ValueError, match="不能为空"):
        asyncio.run(d._upload_certificate("", "KEY", "name"))

    d._cas_client.upload_user_certificate = lambda req: SimpleNamespace(body=SimpleNamespace(cert_id="cert-1"))
    assert asyncio.run(d._upload_certificate("CERT", "KEY", "name")) == "cert-1"

    d._cas_client.upload_user_certificate = lambda req: SimpleNamespace(body=SimpleNamespace(cert_id=""))
    with pytest.raises(RuntimeError, match="CAS"):
        asyncio.run(d._upload_certificate("CERT", "KEY", "name"))

    d._cas_client.upload_user_certificate = lambda req: (_ for _ in ()).throw(RuntimeError("boom"))
    with pytest.raises(RuntimeError, match="上传证书到 CAS 失败"):
        asyncio.run(d._upload_certificate("CERT", "KEY", "name"))


def test_upload_slb_server_certificate_uploads_and_renames(monkeypatch):
    d, _ = make_deployer(monkeypatch)
    captured = []
    d._regional_clients["cn-beijing"]["slb_client"] = SimpleNamespace(
        upload_server_certificate=lambda req: (
            captured.append(("upload", req)) or SimpleNamespace(
                body=SimpleNamespace(server_certificate_id="slb-cert")
            )
        ),
        set_server_certificate_name=lambda req: captured.append(("rename", req)),
    )

    result = asyncio.run(
        d._upload_slb_server_certificate("CERT", "KEY", "api-1", "cn-beijing")
    )
    assert result == "slb-cert"
    assert [x[0] for x in captured] == ["upload", "rename"]
    assert captured[1][1].server_certificate_id == "slb-cert"
    assert captured[1][1].server_certificate_name == "api-1"


def test_upload_slb_server_certificate_rejects_empty_response(monkeypatch):
    d, _ = make_deployer(monkeypatch)
    d._regional_clients["cn-hangzhou"]["slb_client"] = SimpleNamespace(
        upload_server_certificate=lambda req: SimpleNamespace(body=SimpleNamespace(server_certificate_id="")),
        set_server_certificate_name=lambda req: None,
    )
    with pytest.raises(RuntimeError, match="server_certificate_id"):
        asyncio.run(d._upload_slb_server_certificate("CERT", "KEY", "name", "cn-hangzhou"))


def test_deploy_cdn_alb_oss_and_unsupported(monkeypatch):
    d, _ = make_deployer(monkeypatch)
    cdn_calls, alb_calls, oss_calls = [], [], []

    d._cdn_client.set_cdn_domain_sslcertificate = lambda req: cdn_calls.append(req)
    d._alb_client.update_listener_attribute = lambda req: alb_calls.append(req)
    d._regional_clients["cn-beijing"]["alb_client"] = d._alb_client
    d._regional_clients["cn-beijing"]["oss_client"] = SimpleNamespace(
        put_cname=lambda req: oss_calls.append(req)
    )

    async def upload(*args): return "cas-cert"
    d._upload_certificate = upload

    assert asyncio.run(d.deploy("CERT", "KEY", DeployTarget("aliyun", "cdn", domain="cdn.example.com"))).success
    assert cdn_calls[0].domain_name == "cdn.example.com"
    assert cdn_calls[0].cert_id == "cas-cert"

    target_alb = DeployTarget("aliyun", "alb", product_id="cn-beijing:alb-1:listener-1")
    assert asyncio.run(d.deploy("CERT", "KEY", target_alb, cert_id="reuse")).success
    assert alb_calls[0].listener_id == "listener-1"
    assert alb_calls[0].certificates[0].certificate_id == "reuse"

    target_oss = DeployTarget(
        "aliyun", "oss", product_id="cn-beijing:bucket:cdn.example.com",
        domain="cdn.example.com", metadata={"previous_cert_id": "old"},
    )
    assert asyncio.run(d.deploy("CERT", "KEY", target_oss, cert_id="reuse")).success
    assert oss_calls[0].bucket == "bucket"
    assert oss_calls[0].bucket_cname_configuration.cname.domain == "cdn.example.com"
    assert oss_calls[0].bucket_cname_configuration.cname.certificate_configuration.previous_cert_id == "old"

    bad = asyncio.run(d.deploy("CERT", "KEY", DeployTarget("aliyun", "unknown")))
    assert bad.success is False
    assert "不支持" in bad.message


def test_deploy_slb_uploads_server_certificate_and_binds_listener(monkeypatch):
    d, _ = make_deployer(monkeypatch)
    calls = []
    d._regional_clients["cn-beijing"]["slb_client"] = SimpleNamespace(
        upload_server_certificate=lambda req: (
            calls.append(("upload", req)) or SimpleNamespace(body=SimpleNamespace(server_certificate_id="slb-cert"))
        ),
        set_server_certificate_name=lambda req: calls.append(("rename", req)),
        set_load_balancer_httpslistener_attribute=lambda req: calls.append(("bind", req)),
    )

    result = asyncio.run(
        d.deploy(
            "CERT", "KEY",
            DeployTarget("aliyun", "slb", product_id="cn-beijing:lb-1:443", domain="api.example.com"),
        )
    )
    assert result.success is True
    assert result.cert_id == "slb-cert"
    assert [x[0] for x in calls] == ["upload", "rename", "bind"]
    assert calls[-1][1].load_balancer_id == "lb-1"
    assert calls[-1][1].listener_port == 443


def test_deploy_reuses_cas_cert_and_handles_failures(monkeypatch):
    d, _ = make_deployer(monkeypatch)
    d._upload_certificate = lambda *args: (_ for _ in ()).throw(RuntimeError("upload failed"))
    result = asyncio.run(
        d.deploy("CERT", "KEY", DeployTarget("aliyun", "cdn", domain="a.example.com"))
    )
    assert result.success is False
    assert "upload failed" in result.message

    d._cdn_client.set_cdn_domain_sslcertificate = lambda req: (_ for _ in ()).throw(RuntimeError("cdn failed"))
    result = asyncio.run(
        d.deploy("CERT", "KEY", DeployTarget("aliyun", "cdn", domain="a.example.com"), cert_id="reuse")
    )
    assert result.success is False
    assert "cdn failed" in result.message


def test_target_parsers_cover_edge_cases(monkeypatch):
    d, _ = make_deployer(monkeypatch)

    assert d._parse_slb_target(DeployTarget("aliyun", "slb", product_id="lb-1")) == ("cn-hangzhou", "lb-1", 443)
    assert d._parse_slb_target(DeployTarget("aliyun", "slb", product_id="lb-1", listener_port=8443)) == ("cn-hangzhou", "lb-1", 8443)

    with pytest.raises(ValueError):
        d._parse_slb_target(DeployTarget("aliyun", "slb", product_id=":443"))
    with pytest.raises(ValueError):
        d._parse_alb_target(DeployTarget("aliyun", "alb", product_id="cn-beijing::listener"))
    with pytest.raises(ValueError):
        d._parse_oss_target(DeployTarget("aliyun", "oss", product_id="cn-beijing:bucket:"))

    assert d._parse_alb_target(DeployTarget("aliyun", "alb", product_id="cn-beijing:alb:listener:extra")) == (
        "cn-beijing", "alb", "listener:extra"
    )
    assert d.name == "aliyun"
    assert asyncio.run(d.list_targets()) == []
