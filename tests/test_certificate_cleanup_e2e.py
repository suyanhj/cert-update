from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from app.certificate_cleanup.base import AssociationCheck, AssociationStatus
from app.certificate_cleanup.store import CertificateCleanupStore
from app.schemas.config import CertificateCleanupConfig
from app.services.certificate_cleanup import run_certificate_cleanup
from tests.test_certificate_cleanup_service import FakeAdapter, _cert


NOW = datetime(2026, 10, 22, tzinfo=timezone.utc)


class ReportOnlyAdapter(FakeAdapter):
    def __init__(self, name, certificates):
        super().__init__(name, certificates, safe_delete=False)

    async def check_associations(self, certificate_id):
        return AssociationCheck(AssociationStatus.UNKNOWN, complete=False)


def test_offline_end_to_end_restart_partial_failure_and_global_limit(tmp_path) -> None:
    path = tmp_path / "state.json"
    oldest = FakeAdapter("账号A", [_cert("oldest", age_days=120)])
    newer = FakeAdapter("账号B", [_cert("newer", age_days=90)])
    failed = FakeAdapter(
        "账号C",
        [],
        list_error=RuntimeError("secret_key=must-not-leak 权限不足"),
    )
    report_only = ReportOnlyAdapter("账号D", [_cert("report", age_days=100)])
    report_options = CertificateCleanupConfig(
        enabled=True,
        mode="report",
        expired_grace_days=30,
        confirm_scans=2,
        max_deletes_per_run=1,
    )

    first = asyncio.run(
        run_certificate_cleanup(
            report_options,
            [oldest, newer, failed, report_only],
            CertificateCleanupStore(str(path)),
            now=NOW,
        )
    )
    assert [item.deleted for item in first] == [0, 0, 0, 0]
    assert first[2].failed is True
    assert first[3].report_only == 1

    delete_options = CertificateCleanupConfig(
        enabled=True,
        mode="delete",
        expired_grace_days=30,
        confirm_scans=2,
        max_deletes_per_run=1,
        delete_scopes=[
            {"provider_type": "tencent", "provider_name": "账号A", "store": "ssl"},
            {"provider_type": "tencent", "provider_name": "账号B", "store": "ssl"},
        ],
    )
    second = asyncio.run(
        run_certificate_cleanup(
            delete_options,
            [oldest, newer, failed, report_only],
            CertificateCleanupStore(str(path)),
            now=NOW,
        )
    )

    assert oldest.deleted == ["oldest"]
    assert newer.deleted == []
    assert second[0].deleted == 1
    assert second[1].deferred == 1
    assert second[2].failed is True
    assert report_only.deleted == []
    persisted = path.read_text(encoding="utf-8")
    assert "must-not-leak" not in persisted
    assert "newer" in persisted
