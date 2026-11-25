from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.certificate_cleanup.store import CertificateCleanupStore
from app.services import certificate_cleanup as module
from tests.test_certificate_cleanup_service import Cloud403Error, FakeAdapter, _cert


NOW = datetime(2026, 10, 22, tzinfo=timezone.utc)


class FakeNotifier:
    def __init__(self) -> None:
        self.messages = []

    async def notify(self, title, body):
        self.messages.append((title, body))


class FailingOnceNotifier(FakeNotifier):
    async def notify(self, title, body):
        self.messages.append((title, body))
        if len(self.messages) == 1:
            raise RuntimeError("通知临时失败")


def _conf(*, enabled=True, mode="report"):
    return SimpleNamespace(
        certificate_cleanup={
            "enabled": enabled,
            "mode": mode,
            "interval_hours": 24,
            "expired_grace_days": 30,
            "confirm_scans": 1,
            "max_deletes_per_run": 20,
            "delete_scopes": [],
        },
        providers=[],
    )


def test_cleanup_disabled_performs_no_cloud_or_notification(monkeypatch, tmp_path) -> None:
    calls = []
    monkeypatch.setattr(module, "load_raw_config", lambda: _conf(enabled=False))
    monkeypatch.setattr(
        module,
        "build_certificate_cleanup_adapters",
        lambda _conf: calls.append("factory"),
    )
    notifier = FakeNotifier()

    result = asyncio.run(
        module.cleanup_if_due(
            store=CertificateCleanupStore(str(tmp_path / "state.json")),
            notifier=notifier,
            now=NOW,
        )
    )

    assert result is None
    assert calls == []
    assert notifier.messages == []


def test_cleanup_due_state_survives_restart_and_unchanged_result_is_not_renotified(
    monkeypatch, tmp_path
) -> None:
    path = tmp_path / "state.json"
    adapter = FakeAdapter("账号A", [_cert("cert-1")])
    calls = []
    monkeypatch.setattr(module, "load_raw_config", lambda: _conf())
    monkeypatch.setattr(
        module,
        "build_certificate_cleanup_adapters",
        lambda _conf: calls.append("scan") or [adapter],
    )
    notifier = FakeNotifier()

    first = asyncio.run(
        module.cleanup_if_due(
            store=CertificateCleanupStore(str(path)), notifier=notifier, now=NOW
        )
    )
    skipped = asyncio.run(
        module.cleanup_if_due(
            store=CertificateCleanupStore(str(path)),
            notifier=notifier,
            now=NOW + timedelta(hours=1),
        )
    )
    second = asyncio.run(
        module.cleanup_if_due(
            store=CertificateCleanupStore(str(path)),
            notifier=notifier,
            now=NOW + timedelta(hours=24),
        )
    )

    assert first and second
    assert skipped is None
    assert calls == ["scan", "scan"]
    assert len(notifier.messages) == 1
    assert "账号A" in notifier.messages[0][1]


def test_cleanup_failure_notification_is_fail_closed_and_redacted(monkeypatch, tmp_path) -> None:
    error = RuntimeError(
        "权限失败 access_key_secret=very-secret\n"
        "-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----"
    )
    adapter = FakeAdapter("账号A", [], list_error=error)
    monkeypatch.setattr(module, "load_raw_config", lambda: _conf())
    monkeypatch.setattr(
        module, "build_certificate_cleanup_adapters", lambda _conf: [adapter]
    )
    notifier = FakeNotifier()
    store = CertificateCleanupStore(str(tmp_path / "state.json"))

    result = asyncio.run(
        module.cleanup_if_due(store=store, notifier=notifier, now=NOW)
    )

    assert result[0].failed is True
    assert result[0].deleted == 0
    body = notifier.messages[0][1]
    disk = (tmp_path / "state.json").read_text(encoding="utf-8")
    for payload in (body, disk):
        assert "very-secret" not in payload
        assert "BEGIN PRIVATE KEY" not in payload
    assert "失败关闭" in body
    assert "删除 0" in body


def test_permission_unavailable_only_logs_and_does_not_notify(monkeypatch, tmp_path) -> None:
    adapter = FakeAdapter("无权限账号", [], list_error=Cloud403Error())
    monkeypatch.setattr(module, "load_raw_config", lambda: _conf())
    monkeypatch.setattr(
        module, "build_certificate_cleanup_adapters", lambda _conf: [adapter]
    )
    notifier = FakeNotifier()
    store = CertificateCleanupStore(str(tmp_path / "state.json"))
    store.state["notified_signature"] = "旧异常签名"
    store.save()

    result = asyncio.run(
        module.cleanup_if_due(store=store, notifier=notifier, now=NOW)
    )

    assert result[0].unavailable is True
    assert result[0].failed is False
    assert notifier.messages == []


def test_permission_unavailable_is_omitted_from_other_cleanup_report(
    monkeypatch, tmp_path
) -> None:
    unavailable = FakeAdapter("无权限账号", [], list_error=Cloud403Error())
    healthy = FakeAdapter("正常账号", [_cert("cert-1")])
    monkeypatch.setattr(module, "load_raw_config", lambda: _conf())
    monkeypatch.setattr(
        module,
        "build_certificate_cleanup_adapters",
        lambda _conf: [unavailable, healthy],
    )
    notifier = FakeNotifier()

    asyncio.run(
        module.cleanup_if_due(
            store=CertificateCleanupStore(str(tmp_path / "state.json")),
            notifier=notifier,
            now=NOW,
        )
    )

    assert len(notifier.messages) == 1
    assert "正常账号" in notifier.messages[0][1]
    assert "无权限账号" not in notifier.messages[0][1]
    assert "NoPermission" not in notifier.messages[0][1]


def test_notification_failure_retries_without_rescanning_cloud(monkeypatch, tmp_path) -> None:
    path = tmp_path / "state.json"
    calls = []
    adapter = FakeAdapter("账号A", [_cert("cert-1")])
    monkeypatch.setattr(module, "load_raw_config", lambda: _conf())
    monkeypatch.setattr(
        module,
        "build_certificate_cleanup_adapters",
        lambda _conf: calls.append("scan") or [adapter],
    )
    notifier = FailingOnceNotifier()

    try:
        asyncio.run(
            module.cleanup_if_due(
                store=CertificateCleanupStore(str(path)),
                notifier=notifier,
                now=NOW,
            )
        )
    except RuntimeError as exc:
        assert "通知临时失败" in str(exc)
    else:
        raise AssertionError("首次通知应失败")

    result = asyncio.run(
        module.cleanup_if_due(
            store=CertificateCleanupStore(str(path)),
            notifier=notifier,
            now=NOW + timedelta(hours=1),
        )
    )

    assert result is None
    assert calls == ["scan"]
    assert len(notifier.messages) == 2
    assert CertificateCleanupStore(str(path)).state["pending_notification"] is None
