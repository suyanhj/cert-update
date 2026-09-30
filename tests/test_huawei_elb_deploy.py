# -*- coding: utf-8 -*-
"""华为云 ELB 证书部署单元测试。

覆盖场景：
  1. 默认证书 + SNI 列表全部更新（新证书覆盖所有域名）
  2. 仅 SNI 更新，默认证书域名不匹配时不被覆盖
  3. 仅默认证书更新，SNI 域名不匹配时保持不变
  4. 幂等：重复调用不产生副作用
  5. 部分 SNI 更新：只替换域名匹配的 SNI 引用
  6. previous_cert_id 兜底：旧证书 API 查不到但 ID 匹配时仍替换
  7. 多个不同旧证书 ID 在 SNI 列表中混合存在的场景
"""

import asyncio
from typing import Any, Dict, List, Optional, Set, Tuple

import pytest

import app.deploys.huawei as huawei_mod
from app.deploys.huawei import HuaweiDeployer
from app.deploys.base import DeployTarget


# ---------------------------------------------------------------------------
# Fake / Helper
# ---------------------------------------------------------------------------


class FakeCertInfo:
    """模拟 ShowCertificate 返回的证书对象。"""

    def __init__(
        self,
        cid: str,
        common_name: str = "",
        sans: Optional[List[str]] = None,
        domain: str = "",
    ):
        self.id = cid
        self.common_name = common_name
        self.subject_alternative_names = sans or []
        self.domain = domain


class FakeListener:
    """模拟 ShowListener 返回的监听器。"""

    def __init__(self, default_ref: str, sni_refs: Optional[List[str]] = None):
        self.default_tls_container_ref = default_ref
        self.sni_container_refs = sni_refs or []


class FakeShowListenerResp:
    def __init__(self, listener: FakeListener):
        self.listener = listener


class FakeShowCertResp:
    def __init__(self, cert: FakeCertInfo):
        self.certificate = cert


class FakeCreateCertResp:
    def __init__(self, cid: str):
        self.certificate = type("C", (), {"id": cid})()


class FakeElbClient:
    """可配置的 ELB mock 客户端，记录 update_listener 调用。"""

    def __init__(
        self,
        listener: FakeListener,
        cert_db: Optional[Dict[str, FakeCertInfo]] = None,
    ):
        self._listener = listener
        self._cert_db: Dict[str, FakeCertInfo] = cert_db or {}
        self.update_calls: List[Any] = []

    def show_listener(self, req: Any) -> FakeShowListenerResp:
        return FakeShowListenerResp(self._listener)

    def show_certificate(self, req: Any) -> FakeShowCertResp:
        cid = str(req.certificate_id or "")
        cert = self._cert_db.get(cid)
        if cert is None:
            raise RuntimeError(f"cert not found: {cid}")
        return FakeShowCertResp(cert)

    def update_listener(self, req: Any) -> None:
        self.update_calls.append(req)
        # 更新内部状态，使后续 ShowListener 反映最新值
        opt = req.body.listener
        if getattr(opt, "default_tls_container_ref", None) is not None:
            self._listener.default_tls_container_ref = opt.default_tls_container_ref
        if getattr(opt, "sni_container_refs", None) is not None:
            self._listener.sni_container_refs = list(opt.sni_container_refs)

    def create_certificate(self, req: Any) -> FakeCreateCertResp:
        return FakeCreateCertResp("new-uploaded")


class FakeParsed:
    def __init__(self, sans: List[str]):
        self.sans = sans


def _make_deployer(
    monkeypatch: pytest.MonkeyPatch,
    listener: FakeListener,
    cert_db: Optional[Dict[str, FakeCertInfo]] = None,
    parsed_sans: Optional[List[str]] = None,
) -> Tuple[HuaweiDeployer, FakeElbClient]:
    """构造一个接入 Fake 客户端的 HuaweiDeployer。"""
    client = FakeElbClient(listener, cert_db)

    monkeypatch.setattr(huawei_mod, "build_elb_client", lambda region, credentials: client)
    if parsed_sans is not None:
        monkeypatch.setattr(
            huawei_mod,
            "parse_cert_info_from_pem",
            lambda pem: FakeParsed(parsed_sans),
        )

    d = HuaweiDeployer(
        access_key_id="ak",
        access_key_secret="sk",
        project_id="proj",
        region="cn-north-4",
    )
    return d, client


# ---------------------------------------------------------------------------
# 场景 1：新证书覆盖所有域名，默认+SNI 全部更新
# ---------------------------------------------------------------------------


def test_default_and_sni_all_updated_when_new_cert_covers_all(monkeypatch):
    """
    监听器：默认 cert-111（qq.com）、SNI [cert-111]（api.qq.com, www.qq.com）
    新证书：qq.com, *.qq.com → 应同时更新默认和 SNI
    """
    listener = FakeListener(
        default_ref="cert-111",
        sni_refs=["cert-111"],
    )
    cert_db = {
        "cert-111": FakeCertInfo(
            "cert-111",
            common_name="qq.com",
            sans=["qq.com", "api.qq.com", "www.qq.com"],
        ),
    }
    deployer, client = _make_deployer(monkeypatch, listener, cert_db)
    new_cert_domains = {"qq.com", "*.qq.com"}

    result = asyncio.run(
        deployer._deploy_to_elb(
            cert_id="cert-222",
            listener_id="lis-1",
            new_cert_domains=new_cert_domains,
            previous_cert_id="cert-111",
        )
    )

    assert result is True
    assert len(client.update_calls) == 1
    req = client.update_calls[0]
    opt = req.body.listener
    assert opt.default_tls_container_ref == "cert-222"
    assert opt.sni_container_refs == ["cert-222"]


def test_default_and_sni_different_cert_ids_all_updated(monkeypatch):
    """
    监听器：默认 cert-aaa（qq.com）、SNI [cert-bbb（api.qq.com）, cert-ccc（www.qq.com）]
    新证书：qq.com, *.qq.com → 三个引用全部更新
    """
    listener = FakeListener(
        default_ref="cert-aaa",
        sni_refs=["cert-bbb", "cert-ccc"],
    )
    cert_db = {
        "cert-aaa": FakeCertInfo("cert-aaa", common_name="qq.com", sans=["qq.com"]),
        "cert-bbb": FakeCertInfo("cert-bbb", common_name="api.qq.com", sans=["api.qq.com"]),
        "cert-ccc": FakeCertInfo("cert-ccc", common_name="www.qq.com", sans=["www.qq.com"]),
    }
    deployer, client = _make_deployer(monkeypatch, listener, cert_db)
    new_cert_domains = {"qq.com", "*.qq.com"}

    result = asyncio.run(
        deployer._deploy_to_elb(
            cert_id="cert-new",
            listener_id="lis-1",
            new_cert_domains=new_cert_domains,
            previous_cert_id="cert-aaa",
        )
    )

    assert result is True
    assert len(client.update_calls) == 1
    opt = client.update_calls[0].body.listener
    assert opt.default_tls_container_ref == "cert-new"
    assert opt.sni_container_refs == ["cert-new", "cert-new"]


# ---------------------------------------------------------------------------
# 场景 2：仅 SNI 更新，默认证书域名不匹配
# ---------------------------------------------------------------------------


def test_only_sni_updated_default_not_touched(monkeypatch):
    """
    监听器：默认 cert-xxx（example.com）、SNI [cert-yyy（api.qq.com）]
    新证书：qq.com, *.qq.com → 仅 SNI 更新，默认不动
    """
    listener = FakeListener(
        default_ref="cert-xxx",
        sni_refs=["cert-yyy"],
    )
    cert_db = {
        "cert-xxx": FakeCertInfo("cert-xxx", common_name="example.com", sans=["example.com"]),
        "cert-yyy": FakeCertInfo("cert-yyy", common_name="api.qq.com", sans=["api.qq.com"]),
    }
    deployer, client = _make_deployer(monkeypatch, listener, cert_db)
    new_cert_domains = {"qq.com", "*.qq.com"}

    result = asyncio.run(
        deployer._deploy_to_elb(
            cert_id="cert-new",
            listener_id="lis-1",
            new_cert_domains=new_cert_domains,
            previous_cert_id="cert-yyy",
        )
    )

    assert result is True
    opt = client.update_calls[0].body.listener
    # 默认证书不变
    assert getattr(opt, "default_tls_container_ref", None) is None
    # SNI 已更新
    assert opt.sni_container_refs == ["cert-new"]


# ---------------------------------------------------------------------------
# 场景 3：仅默认证书更新，SNI 域名不匹配
# ---------------------------------------------------------------------------


def test_only_default_updated_sni_not_touched(monkeypatch):
    """
    监听器：默认 cert-aaa（qq.com）、SNI [cert-bbb（api.example.com）]
    新证书：qq.com, *.qq.com → 仅更新默认
    """
    listener = FakeListener(
        default_ref="cert-aaa",
        sni_refs=["cert-bbb"],
    )
    cert_db = {
        "cert-aaa": FakeCertInfo("cert-aaa", common_name="qq.com", sans=["qq.com"]),
        "cert-bbb": FakeCertInfo("cert-bbb", common_name="api.example.com", sans=["api.example.com"]),
    }
    deployer, client = _make_deployer(monkeypatch, listener, cert_db)
    new_cert_domains = {"qq.com", "*.qq.com"}

    result = asyncio.run(
        deployer._deploy_to_elb(
            cert_id="cert-new",
            listener_id="lis-1",
            new_cert_domains=new_cert_domains,
            previous_cert_id="cert-aaa",
        )
    )

    assert result is True
    opt = client.update_calls[0].body.listener
    assert opt.default_tls_container_ref == "cert-new"
    # SNI 列表保持不变（只是回传原始内容）
    assert opt.sni_container_refs == ["cert-bbb"]


# ---------------------------------------------------------------------------
# 场景 4：幂等 — 全部已是新证书时不再调用 UpdateListener
# ---------------------------------------------------------------------------


def test_idempotent_no_update_when_already_new(monkeypatch):
    """全部引用已是新证书，不应调 UpdateListener。"""
    listener = FakeListener(
        default_ref="cert-new",
        sni_refs=["cert-new", "cert-new"],
    )
    deployer, client = _make_deployer(monkeypatch, listener, cert_db={})
    new_cert_domains = {"qq.com", "*.qq.com"}

    result = asyncio.run(
        deployer._deploy_to_elb(
            cert_id="cert-new",
            listener_id="lis-1",
            new_cert_domains=new_cert_domains,
        )
    )

    assert result is True
    assert len(client.update_calls) == 0


def test_idempotent_second_call_noop(monkeypatch):
    """第一次调用更新完成后，第二次调用应为幂等 no-op。"""
    listener = FakeListener(
        default_ref="cert-111",
        sni_refs=["cert-111"],
    )
    cert_db = {
        "cert-111": FakeCertInfo("cert-111", common_name="qq.com", sans=["qq.com"]),
        "cert-222": FakeCertInfo("cert-222", common_name="qq.com", sans=["qq.com", "*.qq.com"]),
    }
    deployer, client = _make_deployer(monkeypatch, listener, cert_db)
    new_cert_domains = {"qq.com", "*.qq.com"}

    # 第一次调用
    r1 = asyncio.run(
        deployer._deploy_to_elb(
            cert_id="cert-222",
            listener_id="lis-1",
            new_cert_domains=new_cert_domains,
            previous_cert_id="cert-111",
        )
    )
    assert r1 is True
    assert len(client.update_calls) == 1

    # 第二次调用：监听器已被更新
    r2 = asyncio.run(
        deployer._deploy_to_elb(
            cert_id="cert-222",
            listener_id="lis-1",
            new_cert_domains=new_cert_domains,
            previous_cert_id="cert-111",
        )
    )
    assert r2 is True
    # 不应有新的 update_listener 调用
    assert len(client.update_calls) == 1


# ---------------------------------------------------------------------------
# 场景 5：部分 SNI 更新
# ---------------------------------------------------------------------------


def test_partial_sni_update(monkeypatch):
    """
    SNI 列表混合了匹配和不匹配的证书，仅匹配的被替换。
    SNI: cert-bbb(api.qq.com) + cert-ccc(api.example.com)
    新证书覆盖 *.qq.com → 仅 cert-bbb 被替换
    """
    listener = FakeListener(
        default_ref="cert-xxx",
        sni_refs=["cert-bbb", "cert-ccc"],
    )
    cert_db = {
        "cert-xxx": FakeCertInfo("cert-xxx", common_name="other.net", sans=["other.net"]),
        "cert-bbb": FakeCertInfo("cert-bbb", common_name="api.qq.com", sans=["api.qq.com"]),
        "cert-ccc": FakeCertInfo("cert-ccc", common_name="api.example.com", sans=["api.example.com"]),
    }
    deployer, client = _make_deployer(monkeypatch, listener, cert_db)
    new_cert_domains = {"qq.com", "*.qq.com"}

    result = asyncio.run(
        deployer._deploy_to_elb(
            cert_id="cert-new",
            listener_id="lis-1",
            new_cert_domains=new_cert_domains,
            previous_cert_id="cert-bbb",
        )
    )

    assert result is True
    opt = client.update_calls[0].body.listener
    assert getattr(opt, "default_tls_container_ref", None) is None
    assert opt.sni_container_refs == ["cert-new", "cert-ccc"]


# ---------------------------------------------------------------------------
# 场景 6：previous_cert_id 兜底（ShowCertificate 失败）
# ---------------------------------------------------------------------------


def test_previous_cert_id_fallback_when_show_cert_fails(monkeypatch):
    """
    cert_db 中没有旧证书（模拟 ShowCertificate 失败），
    但 previous_cert_id 匹配时仍应替换该引用。
    """
    listener = FakeListener(
        default_ref="cert-old",
        sni_refs=["cert-old"],
    )
    # cert_db 为空 → show_certificate 会抛异常
    deployer, client = _make_deployer(monkeypatch, listener, cert_db={})
    new_cert_domains = {"qq.com", "*.qq.com"}

    result = asyncio.run(
        deployer._deploy_to_elb(
            cert_id="cert-new",
            listener_id="lis-1",
            new_cert_domains=new_cert_domains,
            previous_cert_id="cert-old",
        )
    )

    assert result is True
    opt = client.update_calls[0].body.listener
    assert opt.default_tls_container_ref == "cert-new"
    assert opt.sni_container_refs == ["cert-new"]


# ---------------------------------------------------------------------------
# 场景 7：旧证书覆盖范围大于新证书时不替换
# ---------------------------------------------------------------------------


def test_no_replace_when_old_cert_has_extra_domains(monkeypatch):
    """
    旧证书覆盖 api.qq.com + api.example.com
    新证书仅覆盖 *.qq.com → 不应替换（会丢失 api.example.com 的覆盖）
    """
    listener = FakeListener(
        default_ref="cert-wide",
        sni_refs=[],
    )
    cert_db = {
        "cert-wide": FakeCertInfo(
            "cert-wide",
            common_name="api.qq.com",
            sans=["api.qq.com", "api.example.com"],
        ),
    }
    deployer, client = _make_deployer(monkeypatch, listener, cert_db)
    new_cert_domains = {"qq.com", "*.qq.com"}

    result = asyncio.run(
        deployer._deploy_to_elb(
            cert_id="cert-new",
            listener_id="lis-1",
            new_cert_domains=new_cert_domains,
        )
    )

    # 不应更新任何引用
    assert result is True
    assert len(client.update_calls) == 0


# ---------------------------------------------------------------------------
# 场景 8：通配符证书替换通配符证书
# ---------------------------------------------------------------------------


def test_wildcard_old_cert_replaced_by_same_wildcard(monkeypatch):
    """
    旧证书: *.qq.com、qq.com
    新证书: *.qq.com、qq.com → 完全覆盖 → 替换
    """
    listener = FakeListener(
        default_ref="cert-old-wild",
        sni_refs=[],
    )
    cert_db = {
        "cert-old-wild": FakeCertInfo(
            "cert-old-wild",
            common_name="qq.com",
            sans=["qq.com", "*.qq.com"],
        ),
    }
    deployer, client = _make_deployer(monkeypatch, listener, cert_db)
    new_cert_domains = {"qq.com", "*.qq.com"}

    result = asyncio.run(
        deployer._deploy_to_elb(
            cert_id="cert-new-wild",
            listener_id="lis-1",
            new_cert_domains=new_cert_domains,
        )
    )

    assert result is True
    assert len(client.update_calls) == 1
    opt = client.update_calls[0].body.listener
    assert opt.default_tls_container_ref == "cert-new-wild"


# ---------------------------------------------------------------------------
# 场景 9：URL 格式的证书引用
# ---------------------------------------------------------------------------


def test_url_format_cert_refs(monkeypatch):
    """证书引用为完整 URL 时，替换后保持 URL 前缀格式。"""
    base_url = "https://elb.cn-north-4.myhuaweicloud.com/v3/proj/certificates"
    listener = FakeListener(
        default_ref=f"{base_url}/cert-111",
        sni_refs=[f"{base_url}/cert-111"],
    )
    cert_db = {
        "cert-111": FakeCertInfo("cert-111", common_name="qq.com", sans=["qq.com"]),
    }
    deployer, client = _make_deployer(monkeypatch, listener, cert_db)
    new_cert_domains = {"qq.com", "*.qq.com"}

    result = asyncio.run(
        deployer._deploy_to_elb(
            cert_id="cert-222",
            listener_id="lis-1",
            new_cert_domains=new_cert_domains,
            previous_cert_id="cert-111",
        )
    )

    assert result is True
    opt = client.update_calls[0].body.listener
    assert opt.default_tls_container_ref == f"{base_url}/cert-222"
    assert opt.sni_container_refs == [f"{base_url}/cert-222"]


# ---------------------------------------------------------------------------
# 场景 10：deploy() 集成测试 — 从 PEM 到 _deploy_to_elb 的完整链路
# ---------------------------------------------------------------------------


def test_deploy_elb_integration(monkeypatch):
    """deploy() 应从 PEM 解析域名，传给 _deploy_to_elb 做域名覆盖判断。"""
    listener = FakeListener(
        default_ref="cert-old",
        sni_refs=["cert-sni-old"],
    )
    cert_db = {
        "cert-old": FakeCertInfo("cert-old", common_name="qq.com", sans=["qq.com"]),
        "cert-sni-old": FakeCertInfo("cert-sni-old", common_name="api.qq.com", sans=["api.qq.com"]),
    }
    deployer, client = _make_deployer(
        monkeypatch,
        listener,
        cert_db,
        parsed_sans=["qq.com", "*.qq.com"],
    )

    target = DeployTarget(
        provider="huawei",
        product_type="elb",
        product_id="lis-1",
        domain="qq.com",
        metadata={
            "previous_cert_id": "cert-old",
            "cert_source": "default",
            "cert_ref": "cert-old",
        },
    )

    result = asyncio.run(
        deployer.deploy(
            cert_pem="FAKE-PEM",
            key_pem="FAKE-KEY",
            target=target,
            cert_id="cert-new",
        )
    )

    assert result.success is True
    assert len(client.update_calls) == 1
    opt = client.update_calls[0].body.listener
    assert opt.default_tls_container_ref == "cert-new"
    assert opt.sni_container_refs == ["cert-new"]


# ---------------------------------------------------------------------------
# 场景 11：同一 cert_id 挂在 default+SNI 但服务不同域名组
#   → previous_cert_id 不能跳过域名覆盖检查，否则误替换不相关槽位
# ---------------------------------------------------------------------------


def test_same_cert_id_on_default_and_sni_only_matching_slot_replaced(monkeypatch):
    """
    监听器：默认 cert-A（example.com + api.qq.com）、SNI [cert-A]
    新证书：qq.com, *.qq.com
    previous_cert_id = cert-A

    cert-A 覆盖 {example.com, api.qq.com}，新证书不覆盖 example.com。
    即使 previous_cert_id 匹配，因为新证书无法完全覆盖 cert-A 的全部域名
    （替换会导致 example.com 丢失 SSL 覆盖），所以两个槽位都不应替换。
    """
    listener = FakeListener(
        default_ref="cert-A",
        sni_refs=["cert-A"],
    )
    cert_db = {
        "cert-A": FakeCertInfo(
            "cert-A",
            common_name="example.com",
            sans=["example.com", "api.qq.com"],
        ),
    }
    deployer, client = _make_deployer(monkeypatch, listener, cert_db)
    new_cert_domains = {"qq.com", "*.qq.com"}

    result = asyncio.run(
        deployer._deploy_to_elb(
            cert_id="cert-new",
            listener_id="lis-1",
            new_cert_domains=new_cert_domains,
            previous_cert_id="cert-A",
        )
    )

    assert result is True
    # cert-A 域名范围 > 新证书覆盖范围 → 不应替换任何引用 → 不调 UpdateListener
    assert len(client.update_calls) == 0


def test_same_cert_default_sni_partial_domain_groups(monkeypatch):
    """
    默认 cert-X（example.com）、SNI [cert-Y（api.qq.com）]
    新证书：qq.com, *.qq.com
    previous_cert_id = cert-Y

    预期：仅 SNI cert-Y（纯 api.qq.com）被替换，默认 cert-X（example.com）不动。
    """
    listener = FakeListener(
        default_ref="cert-X",
        sni_refs=["cert-Y"],
    )
    cert_db = {
        "cert-X": FakeCertInfo("cert-X", common_name="example.com", sans=["example.com"]),
        "cert-Y": FakeCertInfo("cert-Y", common_name="api.qq.com", sans=["api.qq.com"]),
    }
    deployer, client = _make_deployer(monkeypatch, listener, cert_db)
    new_cert_domains = {"qq.com", "*.qq.com"}

    result = asyncio.run(
        deployer._deploy_to_elb(
            cert_id="cert-new",
            listener_id="lis-1",
            new_cert_domains=new_cert_domains,
            previous_cert_id="cert-Y",
        )
    )

    assert result is True
    opt = client.update_calls[0].body.listener
    assert getattr(opt, "default_tls_container_ref", None) is None
    assert opt.sni_container_refs == ["cert-new"]


# ---------------------------------------------------------------------------
# 场景 12：SNI 列表完整性保证 — 不匹配的条目必须原样保留
# ---------------------------------------------------------------------------


def test_sni_list_integrity_unmatched_refs_preserved(monkeypatch):
    """
    SNI 5 条：3 条匹配（被替换），2 条不匹配（原样保留）。
    最终提交的列表必须仍有 5 条。
    """
    listener = FakeListener(
        default_ref="cert-def",
        sni_refs=["cert-a", "cert-b", "cert-c", "cert-d", "cert-e"],
    )
    cert_db = {
        "cert-def": FakeCertInfo("cert-def", common_name="root.net", sans=["root.net"]),
        "cert-a": FakeCertInfo("cert-a", common_name="api.qq.com", sans=["api.qq.com"]),
        "cert-b": FakeCertInfo("cert-b", common_name="app.other.com", sans=["app.other.com"]),
        "cert-c": FakeCertInfo("cert-c", common_name="www.qq.com", sans=["www.qq.com"]),
        "cert-d": FakeCertInfo("cert-d", common_name="mail.third.org", sans=["mail.third.org"]),
        "cert-e": FakeCertInfo("cert-e", common_name="xx.qq.com", sans=["xx.qq.com"]),
    }
    deployer, client = _make_deployer(monkeypatch, listener, cert_db)
    new_cert_domains = {"qq.com", "*.qq.com"}

    result = asyncio.run(
        deployer._deploy_to_elb(
            cert_id="cert-new",
            listener_id="lis-1",
            new_cert_domains=new_cert_domains,
        )
    )

    assert result is True
    opt = client.update_calls[0].body.listener
    # 默认 root.net 不匹配 → 不变
    assert getattr(opt, "default_tls_container_ref", None) is None
    # SNI 5 条完整保留
    sni = opt.sni_container_refs
    assert len(sni) == 5
    assert sni[0] == "cert-new"      # cert-a (api.qq.com) → 替换
    assert sni[1] == "cert-b"        # app.other.com → 原样
    assert sni[2] == "cert-new"      # cert-c (www.qq.com) → 替换
    assert sni[3] == "cert-d"        # mail.third.org → 原样
    assert sni[4] == "cert-new"      # cert-e (xx.qq.com) → 替换


# ---------------------------------------------------------------------------
# _new_cert_covers_old_cert 单元测试
# ---------------------------------------------------------------------------


class TestNewCertCoversOldCert:
    """测试 _new_cert_covers_old_cert 的各种边界情况。"""

    def _make(self, monkeypatch, cert_db=None):
        listener = FakeListener(default_ref="x")
        d, _ = _make_deployer(monkeypatch, listener, cert_db or {})
        return d

    def test_previous_cert_id_only_fallback_when_api_fails(self, monkeypatch):
        """previous_cert_id 仅在 ShowCertificate 不可用时作为兜底。"""
        # cert_db 为空 → ShowCertificate 会失败 → 才使用 previous_cert_id 兜底
        d = self._make(monkeypatch, cert_db={})
        assert d._new_cert_covers_old_cert(
            new_cert_domains={"a.com"},
            old_cert_id="cert-x",
            previous_cert_id="cert-x",
        ) is True

    def test_previous_cert_id_does_not_skip_domain_check(self, monkeypatch):
        """即使 previous_cert_id 匹配，有域名信息时仍做覆盖检查。"""
        cert_db = {
            "cert-x": FakeCertInfo(
                "cert-x",
                common_name="example.com",
                sans=["example.com", "api.qq.com"],
            ),
        }
        d = self._make(monkeypatch, cert_db)
        # 新证书只覆盖 *.qq.com，不覆盖 example.com → 不能替换
        assert d._new_cert_covers_old_cert(
            new_cert_domains={"qq.com", "*.qq.com"},
            old_cert_id="cert-x",
            previous_cert_id="cert-x",
        ) is False

    def test_empty_old_cert_id(self, monkeypatch):
        d = self._make(monkeypatch)
        assert d._new_cert_covers_old_cert({"a.com"}, "", None) is False

    def test_show_cert_fails_no_previous_returns_false(self, monkeypatch):
        """ShowCertificate 失败且无 previous_cert_id → 返回 False。"""
        d = self._make(monkeypatch, cert_db={})
        assert d._new_cert_covers_old_cert(
            new_cert_domains={"*.qq.com"},
            old_cert_id="cert-unknown",
        ) is False

    def test_show_cert_fails_with_different_previous_returns_false(self, monkeypatch):
        """ShowCertificate 失败且 previous_cert_id 不匹配 → 返回 False。"""
        d = self._make(monkeypatch, cert_db={})
        assert d._new_cert_covers_old_cert(
            new_cert_domains={"*.qq.com"},
            old_cert_id="cert-unknown",
            previous_cert_id="cert-other",
        ) is False

    def test_concrete_domain_covered(self, monkeypatch):
        cert_db = {
            "cert-1": FakeCertInfo("cert-1", common_name="api.qq.com", sans=["api.qq.com"]),
        }
        d = self._make(monkeypatch, cert_db)
        assert d._new_cert_covers_old_cert(
            new_cert_domains={"qq.com", "*.qq.com"},
            old_cert_id="cert-1",
        ) is True

    def test_concrete_domain_not_covered(self, monkeypatch):
        cert_db = {
            "cert-1": FakeCertInfo("cert-1", common_name="api.other.com", sans=["api.other.com"]),
        }
        d = self._make(monkeypatch, cert_db)
        assert d._new_cert_covers_old_cert(
            new_cert_domains={"qq.com", "*.qq.com"},
            old_cert_id="cert-1",
        ) is False

    def test_wildcard_covered_by_same_wildcard(self, monkeypatch):
        cert_db = {
            "cert-w": FakeCertInfo("cert-w", common_name="*.qq.com", sans=["*.qq.com", "qq.com"]),
        }
        d = self._make(monkeypatch, cert_db)
        assert d._new_cert_covers_old_cert(
            new_cert_domains={"qq.com", "*.qq.com"},
            old_cert_id="cert-w",
        ) is True

    def test_wildcard_not_covered(self, monkeypatch):
        """旧证书有 *.qq.com，新证书没有 → 不能替换。"""
        cert_db = {
            "cert-w": FakeCertInfo("cert-w", common_name="*.qq.com", sans=["*.qq.com"]),
        }
        d = self._make(monkeypatch, cert_db)
        assert d._new_cert_covers_old_cert(
            new_cert_domains={"qq.com", "api.qq.com"},
            old_cert_id="cert-w",
        ) is False

    def test_partial_coverage_not_enough(self, monkeypatch):
        """旧证书有多个域名，新证书只覆盖部分 → 不能替换。"""
        cert_db = {
            "cert-multi": FakeCertInfo(
                "cert-multi",
                common_name="api.qq.com",
                sans=["api.qq.com", "api.other.com"],
            ),
        }
        d = self._make(monkeypatch, cert_db)
        assert d._new_cert_covers_old_cert(
            new_cert_domains={"qq.com", "*.qq.com"},
            old_cert_id="cert-multi",
        ) is False

    def test_parent_wildcard_does_not_cover_child_concrete(self, monkeypatch):
        """回归：父级泛域名不应覆盖多级具体子域名。

        线上事故场景：新证书 SAN={nczx2025.com, *.nczx2025.com}，
        旧 SNI 引用证书 SAN={m.nczx2025.com, *.m.nczx2025.com}。
        按 SSL/TLS 单级通配符语义，``*.nczx2025.com`` 不能覆盖 ``foo.m.nczx2025.com``，
        因此必须返回 False，禁止替换该 SNI 引用，避免丢失 ``*.m.nczx2025.com`` 的服务能力。
        """
        cert_db = {
            "cert-mwild": FakeCertInfo(
                "cert-mwild",
                common_name="*.m.nczx2025.com",
                sans=["m.nczx2025.com", "*.m.nczx2025.com"],
            ),
        }
        d = self._make(monkeypatch, cert_db)
        assert d._new_cert_covers_old_cert(
            new_cert_domains={"nczx2025.com", "*.nczx2025.com"},
            old_cert_id="cert-mwild",
        ) is False

    def test_parent_wildcard_does_not_cover_child_wildcard(self, monkeypatch):
        """回归：父级泛域名不应覆盖更深一级的泛域名。

        旧证书仅含 ``*.m.nczx2025.com``，新证书仅含 ``*.nczx2025.com``。
        虽然父级泛域名能匹配 ``m.nczx2025.com``，但无法匹配 ``foo.m.nczx2025.com``，
        故按 SSL 单级通配符语义不能视为覆盖。
        """
        cert_db = {
            "cert-mwild2": FakeCertInfo(
                "cert-mwild2",
                common_name="*.m.nczx2025.com",
                sans=["*.m.nczx2025.com"],
            ),
        }
        d = self._make(monkeypatch, cert_db)
        assert d._new_cert_covers_old_cert(
            new_cert_domains={"nczx2025.com", "*.nczx2025.com"},
            old_cert_id="cert-mwild2",
        ) is False


# ---------------------------------------------------------------------------
# _build_new_cert_domains 单元测试
# ---------------------------------------------------------------------------


def test_build_new_cert_domains(monkeypatch):
    monkeypatch.setattr(
        huawei_mod,
        "parse_cert_info_from_pem",
        lambda pem: FakeParsed(["qq.com", "*.qq.com", "QQ.COM"]),
    )
    result = HuaweiDeployer._build_new_cert_domains("FAKE-PEM")
    assert result == {"qq.com", "*.qq.com"}
