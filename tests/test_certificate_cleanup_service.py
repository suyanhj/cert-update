from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.certificate_cleanup.base import (
    AssociationCheck,
    AssociationStatus,
    CertificateCleanupAdapter,
    CertificateSource,
    CertificateStoreId,
    DeleteOutcome,
    DeleteReceipt,
    RemoteCertificate,
)
from app.certificate_cleanup.store import CertificateCleanupStore
from app.schemas.config import CertificateCleanupConfig
from app.services.certificate_cleanup import (
    is_deletable_source,
    is_expired_beyond_grace,
    run_certificate_cleanup,
)


NOW = datetime(2026, 10, 22, tzinfo=timezone.utc)


def _cert(
    certificate_id: str,
    *,
    age_days: int = 60,
    source: CertificateSource = CertificateSource.UPLOADED,
) -> RemoteCertificate:
    return RemoteCertificate(
        certificate_id=certificate_id,
        name=f"证书-{certificate_id}",
        expires_at=NOW - timedelta(days=age_days),
        source=source,
    )


class FakeAdapter(CertificateCleanupAdapter):
    def __init__(
        self,
        name: str,
        certificates: list[RemoteCertificate],
        *,
        safe_delete: bool = True,
        list_error: Exception | None = None,
    ) -> None:
        self.store_id = CertificateStoreId("tencent", name, "ssl")
        self.safe_delete = safe_delete
        self.certificates = {item.certificate_id: item for item in certificates}
        self.list_error = list_error
        self.associations: dict[str, AssociationCheck] = {}
        self.pre_delete_associations: dict[str, AssociationCheck] = {}
        self.association_calls: dict[str, int] = {}
        self.delete_results: dict[str, DeleteReceipt | Exception] = {}
        self.keep_after_delete: set[str] = set()
        self.deleted: list[str] = []

    async def list_certificates(self):
        if self.list_error:
            raise self.list_error
        return list(self.certificates.values())

    async def check_associations(self, certificate_id: str) -> AssociationCheck:
        count = self.association_calls.get(certificate_id, 0)
        self.association_calls[certificate_id] = count + 1
        if count >= 1 and certificate_id in self.pre_delete_associations:
            return self.pre_delete_associations[certificate_id]
        return self.associations.get(
            certificate_id,
            AssociationCheck(AssociationStatus.UNBOUND, complete=True),
        )

    async def get_certificate(self, certificate_id: str):
        return self.certificates.get(certificate_id)

    async def delete_certificate(self, certificate_id: str) -> DeleteReceipt:
        result = self.delete_results.get(
            certificate_id,
            DeleteReceipt(DeleteOutcome.DELETED, request_id="request-1"),
        )
        if isinstance(result, Exception):
            raise result
        self.deleted.append(certificate_id)
        if result.outcome in {DeleteOutcome.DELETED, DeleteOutcome.NOT_FOUND}:
            if certificate_id not in self.keep_after_delete:
                self.certificates.pop(certificate_id, None)
        return result


class Cloud403Error(RuntimeError):
    status_code = 403
    code = "NoPermission"

    def __init__(self) -> None:
        super().__init__(
            "Error: NoPermission code: 403, You are not authorized to perform this action. "
            "request id: request-403 Response: {'EncodedDiagnosticMessage': 'very-long-token'}"
        )


def _options(**overrides) -> CertificateCleanupConfig:
    raw = {
        "enabled": True,
        "mode": "delete",
        "expired_grace_days": 30,
        "confirm_scans": 1,
        "max_deletes_per_run": 20,
        "delete_scopes": [
            {"provider_type": "tencent", "provider_name": "账号A", "store": "ssl"}
        ],
    }
    raw.update(overrides)
    return CertificateCleanupConfig(**raw)


def _run(options, adapters, store):
    return asyncio.run(run_certificate_cleanup(options, adapters, store, now=NOW))


def test_eligibility_uses_real_expiry_grace_and_source() -> None:
    assert is_expired_beyond_grace(_cert("old", age_days=30), NOW, 30)
    assert not is_expired_beyond_grace(_cert("recent", age_days=29), NOW, 30)
    assert is_deletable_source(CertificateSource.UPLOADED)
    assert is_deletable_source(CertificateSource.MANAGED)
    assert not is_deletable_source(CertificateSource.CLOUD_ISSUED)
    assert not is_deletable_source(CertificateSource.PURCHASED)
    assert not is_deletable_source(CertificateSource.UNKNOWN)


def test_report_mode_accumulates_confirmation_without_deleting(tmp_path) -> None:
    store = CertificateCleanupStore(str(tmp_path / "state.json"))
    adapter = FakeAdapter("账号A", [_cert("cert-1")])
    options = _options(mode="report", confirm_scans=2)

    first = _run(options, [adapter], store)[0]
    second = _run(options, [adapter], store)[0]

    assert first.candidates == second.candidates == 1
    assert adapter.deleted == []
    record = next(iter(store.get_candidates().values()))
    assert record["confirm_count"] == 2


def test_candidate_resets_when_rebound_disappears_or_source_changes(tmp_path) -> None:
    store = CertificateCleanupStore(str(tmp_path / "state.json"))
    adapter = FakeAdapter("账号A", [_cert("cert-1")])
    options = _options(mode="report")
    _run(options, [adapter], store)
    assert store.get_candidates()

    adapter.associations["cert-1"] = AssociationCheck(
        AssociationStatus.BOUND, complete=True, resources=("cdn:one",)
    )
    _run(options, [adapter], store)
    assert store.get_candidates() == {}

    adapter.associations.clear()
    adapter.certificates["cert-1"] = _cert("cert-1", source=CertificateSource.UNKNOWN)
    summary = _run(options, [adapter], store)[0]
    assert summary.report_only == 1
    assert store.get_candidates() == {}

    adapter.certificates.clear()
    _run(options, [adapter], store)
    assert store.get_candidates() == {}


def test_partial_store_failure_closes_only_that_store(tmp_path) -> None:
    store = CertificateCleanupStore(str(tmp_path / "state.json"))
    failed = FakeAdapter("账号A", [], list_error=PermissionError("无权限"))
    healthy = FakeAdapter("账号B", [_cert("cert-ok")])
    options = _options(
        mode="report",
        delete_scopes=[],
    )

    summaries = _run(options, [failed, healthy], store)

    assert summaries[0].failed is True
    assert summaries[0].deleted == 0
    assert summaries[1].failed is False
    assert summaries[1].candidates == 1


def test_permission_unavailable_is_audited_without_becoming_failure(tmp_path) -> None:
    store = CertificateCleanupStore(str(tmp_path / "state.json"))
    adapter = FakeAdapter("无权限账号", [_cert("cert-old")])
    options = _options(mode="report", delete_scopes=[])
    _run(options, [adapter], store)
    assert store.get_candidates()

    adapter.list_error = Cloud403Error()
    summary = _run(options, [adapter], store)[0]

    assert summary.unavailable is True
    assert summary.unavailable_reason == "permission_denied"
    assert summary.failed is False
    assert summary.deleted == 0
    assert store.get_candidates() == {}
    assert "NoPermission" in summary.error
    assert "Response:" not in summary.error
    assert "EncodedDiagnosticMessage" not in summary.error
    saved = (tmp_path / "state.json").read_text(encoding="utf-8")
    assert '"unavailable": true' in saved
    assert "very-long-token" not in saved


@pytest.mark.parametrize(
    ("mode", "safe_delete", "scope", "expected"),
    [
        ("report", True, True, []),
        ("delete", False, True, []),
        ("delete", True, False, []),
        ("delete", True, True, ["cert-1"]),
    ],
)
def test_delete_requires_mode_scope_and_safe_adapter(
    tmp_path, mode, safe_delete, scope, expected
) -> None:
    store = CertificateCleanupStore(str(tmp_path / f"{mode}-{safe_delete}-{scope}.json"))
    adapter = FakeAdapter("账号A", [_cert("cert-1")], safe_delete=safe_delete)
    scopes = _options().delete_scopes if scope else []
    options = _options(mode=mode, delete_scopes=scopes)

    _run(options, [adapter], store)

    assert adapter.deleted == expected


def test_delete_order_and_global_limit(tmp_path) -> None:
    store = CertificateCleanupStore(str(tmp_path / "state.json"))
    adapter = FakeAdapter(
        "账号A",
        [_cert("newer", age_days=50), _cert("oldest", age_days=100), _cert("middle", age_days=70)],
    )

    summary = _run(_options(max_deletes_per_run=2), [adapter], store)[0]

    assert adapter.deleted == ["oldest", "middle"]
    assert summary.deleted == 2
    assert summary.deferred == 1


def test_delete_precheck_rejects_new_binding_and_platform_in_use(tmp_path) -> None:
    store = CertificateCleanupStore(str(tmp_path / "state.json"))
    adapter = FakeAdapter("账号A", [_cert("new-bound"), _cert("platform-in-use")])
    adapter.pre_delete_associations["new-bound"] = AssociationCheck(
        AssociationStatus.BOUND, complete=True, resources=("eo:zone-1",)
    )
    adapter.delete_results["platform-in-use"] = DeleteReceipt(DeleteOutcome.IN_USE)

    summary = _run(_options(), [adapter], store)[0]

    assert summary.deleted == 0
    assert summary.bound == 2
    assert adapter.deleted == ["platform-in-use"]
    assert store.get_candidates() == {}


def test_unknown_delete_error_stops_remaining_items_in_same_store(tmp_path) -> None:
    store = CertificateCleanupStore(str(tmp_path / "state.json"))
    adapter = FakeAdapter("账号A", [_cert("first", age_days=100), _cert("second", age_days=90)])
    adapter.delete_results["first"] = TimeoutError("异步删除超时")

    summary = _run(_options(), [adapter], store)[0]

    assert summary.failed is True
    assert summary.deleted == 0
    assert summary.deferred == 1
    assert adapter.deleted == []


def test_delete_permission_error_remains_a_notifiable_failure(tmp_path) -> None:
    store = CertificateCleanupStore(str(tmp_path / "state.json"))
    adapter = FakeAdapter("账号A", [_cert("cert-1")])
    adapter.delete_results["cert-1"] = Cloud403Error()

    summary = _run(_options(), [adapter], store)[0]

    assert summary.failed is True
    assert summary.unavailable is False
    assert summary.deleted == 0
    assert "NoPermission" in summary.error
    assert "Response:" not in summary.error


def test_delete_postcheck_existing_certificate_fails_closed(tmp_path) -> None:
    store = CertificateCleanupStore(str(tmp_path / "state.json"))
    adapter = FakeAdapter("账号A", [_cert("cert-1")])
    adapter.keep_after_delete.add("cert-1")

    summary = _run(_options(), [adapter], store)[0]

    assert summary.failed is True
    assert summary.deleted == 0
    assert "删除后证书仍存在" in summary.error
    assert store.get_candidates()


def test_incomplete_association_resets_store_candidates(tmp_path) -> None:
    store = CertificateCleanupStore(str(tmp_path / "state.json"))
    adapter = FakeAdapter("账号A", [_cert("cert-1")])
    _run(_options(mode="report"), [adapter], store)
    adapter.associations["cert-1"] = AssociationCheck(
        AssociationStatus.UNKNOWN, complete=False
    )

    summary = _run(_options(mode="report"), [adapter], store)[0]

    assert summary.failed is True
    assert summary.deleted == 0
    assert store.get_candidates() == {}
