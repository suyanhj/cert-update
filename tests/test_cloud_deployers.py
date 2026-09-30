from datetime import datetime
import asyncio

import pytest

from app.deploys.base import DeployTarget
from app.deploys.aliyun import AliyunDeployer
from app.deploys.huawei import HuaweiDeployer


class _FixedTime:
    @staticmethod
    def now():
        # 固定一个时间，便于断言别名字符串
        return datetime(2026, 3, 16, 12, 34, 56)


def test_aliyun_upload_certificate_alias_uses_main_domain(monkeypatch):
    """AliyunDeployer.upload_certificate 应优先使用 main_domain 作为 alias 前缀。"""
    # patch TimeUtil.now
    import app.utils.time as time_mod

    monkeypatch.setattr(time_mod.TimeUtil, "now", staticmethod(_FixedTime.now))

    # 构造一个 AliyunDeployer，但 mock 掉底层客户端构造与 _upload_certificate
    def fake_build_clients(access_key_id, access_key_secret, region):
        return {
            "cdn_client": object(),
            "slb_client": object(),
            "cas_client": object(),
            "alb_client": object(),
            "alb_models": object(),
            "oss_client": object(),
            "oss_models": object(),
            "oss_sdk": object(),
        }

    import app.deploys.aliyun as aliyun_mod

    monkeypatch.setattr(aliyun_mod, "build_aliyun_deployer_clients", fake_build_clients)

    captured = {}

    async def fake_upload(self, cert_pem, key_pem, alias):
        captured["args"] = (cert_pem, key_pem, alias)
        return "new-cert-id"

    monkeypatch.setattr(AliyunDeployer, "_upload_certificate", fake_upload)

    d = AliyunDeployer(access_key_id="a", access_key_secret="b", region="cn-hangzhou")

    cert_pem = "CERT"
    key_pem = "KEY"
    # 指定 main_domain
    res = asyncio.run(d.upload_certificate(cert_pem, key_pem, main_domain="api.example.com"))

    assert res == "new-cert-id"
    assert "args" in captured
    _, _, alias = captured["args"]
    assert alias == "api.example.com-20260316123456"

    # 不传 main_domain，fallback 为 renew- 前缀
    captured.clear()
    res2 = asyncio.run(d.upload_certificate(cert_pem, key_pem, main_domain=None))
    assert res2 == "new-cert-id"
    _, _, alias2 = captured["args"]
    assert alias2 == "renew-20260316123456"


def test_aliyun_parse_slb_target_variants(monkeypatch):
    """_parse_slb_target 应正确解析多种 product_id 形式，并对非法输入抛出明确错误。"""
    def fake_build_clients(access_key_id, access_key_secret, region):
        return {
            "cdn_client": object(),
            "slb_client": object(),
            "cas_client": object(),
            "alb_client": object(),
            "alb_models": object(),
            "oss_client": object(),
            "oss_models": object(),
            "oss_sdk": object(),
        }

    import app.deploys.aliyun as aliyun_mod

    monkeypatch.setattr(aliyun_mod, "build_aliyun_deployer_clients", fake_build_clients)
    d = AliyunDeployer(access_key_id="a", access_key_secret="b", region="cn-hangzhou")

    # 完整形式 <region>:<lb_id>:<port>
    t = DeployTarget(provider="aliyun", product_type="slb", product_id="cn-beijing:lb-123:8080")
    region, lb_id, port = d._parse_slb_target(t)
    assert region == "cn-beijing"
    assert lb_id == "lb-123"
    assert port == 8080

    # 只有 lb_id:port，region 取 deployer 默认
    t2 = DeployTarget(provider="aliyun", product_type="slb", product_id="lb-234:443")
    region2, lb_id2, port2 = d._parse_slb_target(t2)
    assert region2 == d._region
    assert lb_id2 == "lb-234"
    assert port2 == 443

    # 只有 lb_id，端口从 listener_port 读取，缺失时默认 443
    t3 = DeployTarget(provider="aliyun", product_type="slb", product_id="lb-345", listener_port=8443)
    region3, lb_id3, port3 = d._parse_slb_target(t3)
    assert region3 == d._region
    assert lb_id3 == "lb-345"
    assert port3 == 8443

    # 非法：空 product_id
    with pytest.raises(ValueError, match="SLB 部署需要 product_id"):
        d._parse_slb_target(DeployTarget(provider="aliyun", product_type="slb", product_id=""))

    # 非法端口
    with pytest.raises(ValueError, match="SLB 端口非法"):
        d._parse_slb_target(DeployTarget(provider="aliyun", product_type="slb", product_id="lb:x"))


def test_aliyun_parse_alb_and_oss_target(monkeypatch):
    """_parse_alb_target/_parse_oss_target 对合法与非法 product_id 的处理。"""
    def fake_build_clients(access_key_id, access_key_secret, region):
        return {
            "cdn_client": object(),
            "slb_client": object(),
            "cas_client": object(),
            "alb_client": object(),
            "alb_models": object(),
            "oss_client": object(),
            "oss_models": object(),
            "oss_sdk": object(),
        }

    import app.deploys.aliyun as aliyun_mod

    monkeypatch.setattr(aliyun_mod, "build_aliyun_deployer_clients", fake_build_clients)
    d = AliyunDeployer(access_key_id="a", access_key_secret="b", region="cn-hangzhou")

    # ALB: <region>:<lb_id>:<listener_id>
    t = DeployTarget(provider="aliyun", product_type="alb", product_id="cn-shanghai:lb-1:lis-1")
    region, lb_id, listener_id = d._parse_alb_target(t)
    assert region == "cn-shanghai"
    assert lb_id == "lb-1"
    assert listener_id == "lis-1"

    # ALB: lb_id:listener_id（region 默认）
    t2 = DeployTarget(provider="aliyun", product_type="alb", product_id="lb-2:lis-2")
    region2, lb_id2, listener_id2 = d._parse_alb_target(t2)
    assert region2 == d._region
    assert lb_id2 == "lb-2"
    assert listener_id2 == "lis-2"

    # ALB: 非法格式
    with pytest.raises(ValueError, match="ALB 部署需要 product_id"):
        d._parse_alb_target(DeployTarget(provider="aliyun", product_type="alb", product_id="only-one-part"))

    # OSS: <region>:<bucket>:<domain>
    t3 = DeployTarget(provider="aliyun", product_type="oss", product_id="oss-cn-beijing:bucket-1:cdn.example.com")
    region3, bucket3, domain3 = AliyunDeployer._parse_oss_target(t3)
    assert region3 == "oss-cn-beijing"
    assert bucket3 == "bucket-1"
    assert domain3 == "cdn.example.com"

    # OSS: 非法格式
    with pytest.raises(ValueError, match="OSS 部署需要 product_id"):
        AliyunDeployer._parse_oss_target(DeployTarget(provider="aliyun", product_type="oss", product_id="bad"))

    with pytest.raises(ValueError, match="OSS product_id 非法"):
        AliyunDeployer._parse_oss_target(DeployTarget(provider="aliyun", product_type="oss", product_id="a:b"))


def test_huawei_upload_certificate_alias_and_domain_field(monkeypatch):
    """HuaweiDeployer.upload_certificate 应使用 main_domain 构造 alias，并在 domain 字段写入 SAN 域名列表。"""
    import app.utils.time as time_mod

    monkeypatch.setattr(time_mod.TimeUtil, "now", staticmethod(_FixedTime.now))

    # fake parse_cert_info_from_pem，让它返回指定 SAN
    import app.deploys.huawei as huawei_mod

    class ParsedCert:
        def __init__(self):
            self.sans = ["a.example.com", "b.example.com"]

    monkeypatch.setattr(huawei_mod, "parse_cert_info_from_pem", lambda pem: ParsedCert())

    # fake elb client
    created = {}

    class FakeCert:
        def __init__(self, cid):
            self.id = cid
            self.common_name = ""
            self.subject_alternative_names = []
            self.domain = ""

    class FakeElbResponse:
        def __init__(self, cid):
            self.certificate = FakeCert(cid)

    class FakeElbClient:
        def create_certificate(self, request):
            # 记录传入的 body 以便断言 name/domain 字段
            created["request"] = request
            # 返回一个带 id 的 certificate
            return FakeElbResponse("cert-123")

    def fake_build_elb_client(region, credentials):
        return FakeElbClient()

    monkeypatch.setattr(huawei_mod, "build_elb_client", fake_build_elb_client)

    d = HuaweiDeployer(access_key_id="ak", access_key_secret="sk", project_id="proj", region="cn-south-1")

    cert_pem = "CERT"
    key_pem = "KEY"
    res = asyncio.run(d.upload_certificate(cert_pem, key_pem, main_domain="vpn.example.com"))
    assert res == "cert-123"

    # 检查 alias 名称
    req = created["request"]
    opt = req.body.certificate
    assert opt.name == "vpn.example.com-20260316123456"
    # domain 字段应为 SAN 列表拼接
    assert opt.domain == "a.example.com,b.example.com"

