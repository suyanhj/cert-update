from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.certificate_cleanup.base import (
    AssociationCheck,
    AssociationStatus,
    CertificateSource,
    CertificateStoreId,
    RemoteCertificate,
)
from app.certificate_cleanup.store import CertificateCleanupStore, sanitize_cleanup_text
from app.schemas.config import AppConfig
from app.services.certificate_operation_lock import (
    certificate_operation_lock,
    get_certificate_operation_lock,
)
from app.services.deploy import DeployService


def _provider(name: str = "腾讯测试") -> dict:
    return {
        "type": "tencent",
        "name": name,
        "enabled": True,
        "credentials": {"secret_id": "id", "secret_key": "secret"},
    }


def test_certificate_cleanup_defaults_are_safe() -> None:
    conf = AppConfig(acme={})

    assert conf.certificate_cleanup.enabled is False
    assert conf.certificate_cleanup.mode == "report"
    assert conf.certificate_cleanup.interval_hours == 24
    assert conf.certificate_cleanup.expired_grace_days == 30
    assert conf.certificate_cleanup.confirm_scans == 1
    assert conf.certificate_cleanup.max_deletes_per_run == 20
    assert conf.certificate_cleanup.delete_scopes == []


def test_certificate_cleanup_scope_must_be_exact_unique_and_existing() -> None:
    raw = {
        "enabled": True,
        "mode": "delete",
        "delete_scopes": [
            {"provider_type": "tencent", "provider_name": "腾讯测试", "store": "ssl"}
        ],
    }
    conf = AppConfig(acme={}, providers=[_provider()], certificate_cleanup=raw)
    assert conf.certificate_cleanup.delete_scopes[0].store == "ssl"

    duplicate = dict(raw)
    duplicate["delete_scopes"] = raw["delete_scopes"] * 2
    with pytest.raises(ValidationError, match="存在重复范围"):
        AppConfig(acme={}, providers=[_provider()], certificate_cleanup=duplicate)

    with pytest.raises(ValidationError, match="不存在的 Provider"):
        AppConfig(acme={}, providers=[], certificate_cleanup=raw)


def test_cleanup_models_reject_incomplete_unbound_result() -> None:
    with pytest.raises(ValueError, match="不完整的关联检查"):
        AssociationCheck(status=AssociationStatus.UNBOUND, complete=False)

    cert = RemoteCertificate(
        certificate_id="cert-1",
        name="测试证书",
        expires_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        source=CertificateSource.UNKNOWN,
    )
    assert cert.source.allows_delete is False


def test_cleanup_store_sanitizes_and_bounds_history(tmp_path) -> None:
    path = tmp_path / "cleanup.json"
    store = CertificateCleanupStore(str(path), max_run_history=2)
    store.set_candidates(
        {
            "one": {
                "certificate_id": "cert-1",
                "private_key": "不应落盘",
                "nested": {"access_key_secret": "不应落盘", "safe": "ok"},
            }
        }
    )
    for index in range(3):
        store.append_run({"index": index, "cert_pem": "不应落盘"})
    store.save()

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert "private_key" not in raw["candidates"]["one"]
    assert "access_key_secret" not in raw["candidates"]["one"]["nested"]
    assert raw["runs"] == [{"index": 1}, {"index": 2}]

    loaded = CertificateCleanupStore(str(path), max_run_history=2)
    assert loaded.get_candidates()["one"]["nested"]["safe"] == "ok"


def test_cleanup_error_sanitizer_removes_sdk_response_and_diagnostic_payload() -> None:
    raw = (
        "Error: NoPermission code: 403, request id: request-1 "
        "Response: {'EncodedDiagnosticMessage': 'large-secret-token', "
        "'AccessDeniedDetail': {'AuthPrincipalOwnerId': 'owner-id'}}"
    )

    sanitized = sanitize_cleanup_text(raw)

    assert sanitized == "Error: NoPermission code: 403, request id: request-1"
    assert "large-secret-token" not in sanitized
    assert "owner-id" not in sanitized


def test_cleanup_store_backs_up_corrupt_state_and_requires_reconfirmation(tmp_path) -> None:
    path = tmp_path / "cleanup.json"
    path.write_bytes(b"{not-json")

    store = CertificateCleanupStore(str(path))

    assert store.get_candidates() == {}
    assert list(tmp_path.glob("cleanup.json.corrupt-*.bak"))
    assert json.loads(path.read_text(encoding="utf-8"))["candidates"] == {}


def test_account_lock_is_shared_by_provider_identity() -> None:
    assert get_certificate_operation_lock("Tencent", " 腾讯测试 ") is get_certificate_operation_lock(
        "tencent", "腾讯测试"
    )


def test_deploy_holds_account_lock_for_whole_operation() -> None:
    async def run() -> None:
        service = DeployService.__new__(DeployService)
        service.conf = SimpleNamespace(
            providers=[SimpleNamespace(type="tencent", name="腾讯测试", enabled=True)]
        )
        deploy_entered = asyncio.Event()
        allow_deploy_finish = asyncio.Event()
        cleanup_acquired = asyncio.Event()

        async def fake_apply_locked(_renewed_cert, _provider_name=None):
            deploy_entered.set()
            await allow_deploy_finish.wait()
            return {"ok": True}

        service._apply_deploy_locked = fake_apply_locked
        deploy_task = asyncio.create_task(service.apply_deploy(object()))
        await deploy_entered.wait()

        async def cleanup() -> None:
            async with certificate_operation_lock("tencent", "腾讯测试"):
                cleanup_acquired.set()

        cleanup_task = asyncio.create_task(cleanup())
        await asyncio.sleep(0)
        assert cleanup_acquired.is_set() is False

        allow_deploy_finish.set()
        assert await deploy_task == {"ok": True}
        await cleanup_task
        assert cleanup_acquired.is_set() is True

    asyncio.run(run())


def test_remote_certificate_requires_aware_expiry() -> None:
    store_id = CertificateStoreId("tencent", "腾讯测试", "ssl")
    assert store_id.key == "tencent:腾讯测试:ssl:global"
    with pytest.raises(ValueError, match="时区"):
        RemoteCertificate(
            certificate_id="cert-1",
            name="无时区",
            expires_at=datetime(2020, 1, 1),
            source=CertificateSource.UPLOADED,
        )
