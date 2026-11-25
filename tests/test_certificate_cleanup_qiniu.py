from __future__ import annotations

import asyncio

import pytest

from app.certificate_cleanup.adapters import qiniu as module
from app.certificate_cleanup.adapters.qiniu import QiniuFusionCleanupAdapter
from app.certificate_cleanup.base import AssociationStatus, DeleteOutcome
from app.deploys.qiniu import QiniuDeployer
from app.utils.qiniu_api import QiniuAPIError


def _raw_cert(cert_id="cert-1", not_after=1_700_000_000):
    return {"certID": cert_id, "name": f"证书-{cert_id}", "not_after": not_after}


class FakeDeployer:
    def __init__(self, certs=None):
        self.certs = {item["certID"]: item for item in (certs or [])}
        self.delete_error = None
        self.auth = object()

    async def list_certificates_strict(self):
        return list(self.certs.values())

    async def get_certificate_strict(self, cert_id):
        return self.certs.get(cert_id)

    async def delete_certificate_strict(self, cert_id):
        if self.delete_error:
            raise self.delete_error
        self.certs.pop(cert_id, None)

    def _get_auth(self):
        return self.auth


def test_qiniu_deployer_strict_marker_pagination() -> None:
    deployer = QiniuDeployer("ak", "sk")
    pages = iter(
        [
            {"certs": [_raw_cert("one")], "marker": "m1"},
            {"certs": [_raw_cert("two")], "marker": ""},
        ]
    )
    deployer._make_request = lambda *args, **kwargs: next(pages)

    result = asyncio.run(deployer.list_certificates_strict())

    assert [item["certID"] for item in result] == ["one", "two"]


@pytest.mark.parametrize(
    "pages",
    [
        [{"certs": [_raw_cert("one")], "marker": "m1"}, {"certs": [], "marker": "m1"}],
        [{"certs": [_raw_cert("same")], "marker": "m1"}, {"certs": [_raw_cert("same")]}],
        [{"certs": "invalid"}],
        [{"certs": [{}]}],
    ],
)
def test_qiniu_deployer_strict_rejects_invalid_pages(pages) -> None:
    deployer = QiniuDeployer("ak", "sk")
    iterator = iter(pages)
    deployer._make_request = lambda *args, **kwargs: next(iterator)

    with pytest.raises(QiniuAPIError):
        asyncio.run(deployer.list_certificates_strict())


def test_qiniu_legacy_list_still_converts_failure_to_empty() -> None:
    deployer = QiniuDeployer("ak", "sk")
    deployer._make_request = lambda *args, **kwargs: (_ for _ in ()).throw(
        QiniuAPIError("请求失败")
    )

    assert asyncio.run(deployer.list_certificates()) == []


def test_qiniu_association_covers_cdn_and_kodo(monkeypatch) -> None:
    deployer = FakeDeployer([_raw_cert()])
    adapter = QiniuFusionCleanupAdapter("七牛测试", deployer=deployer)
    monkeypatch.setattr(
        module,
        "list_qiniu_domains",
        lambda _auth: [
            {"name": "cdn.example.com", "https": {"certId": "cert-1"}},
            {"name": "kodo.example.com", "https": {"certID": "cert-1"}},
        ],
    )
    monkeypatch.setattr(module, "list_qiniu_buckets", lambda _auth: ["bucket-1"])
    monkeypatch.setattr(
        module,
        "list_qiniu_bucket_domains",
        lambda _auth, _bucket: ["kodo.example.com"],
    )

    result = asyncio.run(adapter.check_associations("cert-1"))

    assert result.status is AssociationStatus.BOUND
    assert set(result.resources) == {
        "cdn:cdn.example.com",
        "kodo:kodo.example.com",
    }


def test_qiniu_bucket_scope_failure_never_reports_unbound(monkeypatch) -> None:
    adapter = QiniuFusionCleanupAdapter("七牛测试", deployer=FakeDeployer([_raw_cert()]))
    monkeypatch.setattr(
        module,
        "list_qiniu_domains",
        lambda _auth: [{"name": "cdn.example.com", "https": {}}],
    )
    monkeypatch.setattr(module, "list_qiniu_buckets", lambda _auth: ["bucket-1"])
    monkeypatch.setattr(
        module,
        "list_qiniu_bucket_domains",
        lambda _auth, _bucket: (_ for _ in ()).throw(QiniuAPIError("无权限")),
    )

    with pytest.raises(QiniuAPIError):
        asyncio.run(adapter.check_associations("cert-1"))


def test_qiniu_missing_kodo_domain_fails_closed(monkeypatch) -> None:
    adapter = QiniuFusionCleanupAdapter("七牛测试", deployer=FakeDeployer([_raw_cert()]))
    monkeypatch.setattr(module, "list_qiniu_domains", lambda _auth: [])
    monkeypatch.setattr(module, "list_qiniu_buckets", lambda _auth: ["bucket-1"])
    monkeypatch.setattr(
        module,
        "list_qiniu_bucket_domains",
        lambda _auth, _bucket: ["kodo.example.com"],
    )

    with pytest.raises(Exception, match="未出现在"):
        asyncio.run(adapter.check_associations("cert-1"))


@pytest.mark.parametrize("code", [400326, 400611, 400911])
def test_qiniu_delete_classifies_in_use_codes(code) -> None:
    deployer = FakeDeployer([_raw_cert()])
    deployer.delete_error = QiniuAPIError("证书正在使用", code=code, status_code=400)
    adapter = QiniuFusionCleanupAdapter("七牛测试", deployer=deployer)

    result = asyncio.run(adapter.delete_certificate("cert-1"))

    assert result.outcome is DeleteOutcome.IN_USE


def test_qiniu_delete_permission_error_is_not_hidden() -> None:
    deployer = FakeDeployer([_raw_cert()])
    deployer.delete_error = QiniuAPIError("权限不足", code=401, status_code=401)
    adapter = QiniuFusionCleanupAdapter("七牛测试", deployer=deployer)

    with pytest.raises(QiniuAPIError):
        asyncio.run(adapter.delete_certificate("cert-1"))


def test_qiniu_delete_and_post_query_absent() -> None:
    deployer = FakeDeployer([_raw_cert()])
    adapter = QiniuFusionCleanupAdapter("七牛测试", deployer=deployer)

    receipt = asyncio.run(adapter.delete_certificate("cert-1"))

    assert receipt.outcome is DeleteOutcome.DELETED
    assert asyncio.run(adapter.get_certificate("cert-1")) is None
