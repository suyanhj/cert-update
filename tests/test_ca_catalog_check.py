"""官方目录核对、持久化周期和通知重试测试，不请求外部网络。"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
import asyncio

import pytest

from app.services import ca_catalog_check as module
from app.schemas.config import CACatalogCheckConfig
from test_certificate_chain import hierarchy


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_official_catalog_and_expiry(hierarchy):
    certs, calls, _ = hierarchy
    now = datetime.now(timezone.utc)
    report = module.audit_catalog(CACatalogCheckConfig(warn_days=0), now)
    assert report["issues"] == []
    assert len(calls) == len(certs)
    assert len(report["certificates"]) == len(certs)
    report = module.audit_catalog(CACatalogCheckConfig(warn_days=31), now)
    assert len(report["issues"]) == len(certs)
    assert all("即将过期" in issue for issue in report["issues"])


def test_changed_fingerprint_is_not_accepted(hierarchy):
    module.chain.CA_CATALOG["YE1"]["sha256"] = "0" * 64
    report = module.audit_catalog(CACatalogCheckConfig(warn_days=0), datetime.now(timezone.utc))
    assert len(report["issues"]) == 1
    assert "指纹不匹配" in report["issues"][0]
    assert "YE1" not in report["certificates"]


def test_network_failure_is_not_expiry(hierarchy, monkeypatch):
    def fail(*args):
        raise module.chain.CertificateChainError("下载官方 CA 失败: timeout")
    monkeypatch.setattr(module.chain, "_download_ca", fail)
    report = module.audit_catalog(CACatalogCheckConfig(), datetime.now(timezone.utc))
    assert all("核对失败" in issue and "过期" not in issue for issue in report["issues"])


@pytest.mark.anyio
async def test_persisted_cadence_dedup_retry_and_recovery(tmp_path, monkeypatch):
    path = str(tmp_path / "state.json")
    store_class = module.CACatalogCheckStore
    monkeypatch.setattr(module, "CACatalogCheckStore", lambda: store_class(path))
    monkeypatch.setattr(module, "load_raw_config", lambda: {})
    notify = AsyncMock(side_effect=RuntimeError("发送失败"))
    monkeypatch.setattr(module, "DingDingNotifier", lambda: type("Notifier", (), {"notify": notify})())
    calls = []
    issues = ["YE1: 核对失败"]
    def audit(options, now):
        calls.append(now)
        return {"checked_at": now.isoformat(), "issues": list(issues)}
    monkeypatch.setattr(module, "audit_catalog", audit)
    with pytest.raises(RuntimeError, match="发送失败"):
        await module.check_ca_catalog_if_due()
    notify.side_effect = None
    await module.check_ca_catalog_if_due()
    await module.check_ca_catalog_if_due()
    assert len(calls) == 1
    assert notify.await_count == 2
    def expire():
        store = store_class(path)
        store.state["report"]["checked_at"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        store.save()
    expire()
    await module.check_ca_catalog_if_due()
    assert len(calls) == 2
    assert notify.await_count == 2
    issues.clear()
    expire()
    await module.check_ca_catalog_if_due()
    await module.check_ca_catalog_if_due()
    assert notify.await_count == 3
    assert "恢复正常" in notify.call_args.args[0]


@pytest.mark.anyio
async def test_disabled_does_not_access_state(monkeypatch):
    monkeypatch.setattr(module, "load_raw_config", lambda: {"ca_catalog_check": {"enabled": False}})
    monkeypatch.setattr(module, "CACatalogCheckStore", lambda: pytest.fail("不应读取状态"))
    await module.check_ca_catalog_if_due()


def test_wrong_parent_signature(hierarchy, monkeypatch):
    monkeypatch.setattr(module.chain, "CA_PATHS", {"YE1": ("YE1", "USERTrust RSA Certification Authority")})
    report = module.audit_catalog(CACatalogCheckConfig(warn_days=0), datetime.now(timezone.utc))
    assert any("YE1 -> USERTrust RSA Certification Authority: 签名关系无效" in x for x in report["issues"])


@pytest.mark.anyio
async def test_scheduler_isolates_audit_failure(monkeypatch):
    from app.services import scheduler

    events = []
    async def collect():
        events.append("collect")
    async def check():
        events.append("check")
        scheduler._stop_event.set()
        raise RuntimeError("官方网络失败")
    monkeypatch.setattr(scheduler, "collect_all", collect)
    monkeypatch.setattr(scheduler, "check_ca_catalog_if_due", check)
    scheduler.start_scheduler(run_immediately=True)
    await asyncio.wait_for(scheduler._scheduler_task, 2)
    await scheduler.stop_scheduler()
    assert events == ["collect", "check"]
