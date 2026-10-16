"""Portable filesystem tests; no cloud SDKs, network, or user configuration."""
import json
from pathlib import Path

import pytest

from app.utils import store


def test_domain_provider_persistence_and_longest_match(tmp_path):
    path = tmp_path / "providers.json"
    mapping = store.DomainProviderStore(str(path))
    assert mapping.resolve("api.example.com") is None
    mapping.save([
        {"dns_name": "primary", "domain": "example.com", "subs": [{"name": "*.example.com"}]},
        {"dns_name": "specific", "domain": "api.example.com", "subs": []},
        {"dns_name": "", "domain": "ignored.example.com"},
    ])
    assert mapping.resolve("api.example.com") == "specific"
    assert mapping.resolve("x.example.com") == "primary"
    assert mapping.resolve("unrelated.test") is None
    assert store.DomainProviderStore(str(path)).get_all() == mapping.get_all()
    snapshot = mapping.get_all()
    snapshot["api.example.com"] = "modified"
    assert mapping.resolve("api.example.com") == "specific"


def test_alert_state_persists_and_prevents_duplicate_fire(tmp_path):
    path = tmp_path / "alerts.json"
    alerts = store.AlertStateStore(str(path))
    assert alerts.get("cert") is None
    assert not alerts.is_firing("cert")
    assert alerts.fire("cert") is True
    assert alerts.fire("cert") is False
    assert store.AlertStateStore(str(path)).is_firing("cert")
    copy = alerts.get("cert")
    copy["state"] = "changed"
    assert alerts.is_firing("cert")
    alerts.recover("cert")
    assert not store.AlertStateStore(str(path)).is_firing("cert")


def test_corrupt_json_is_backed_up_before_reset(tmp_path):
    path = tmp_path / "state.json"
    original = b"{ invalid json"
    path.write_bytes(original)
    mapping = store.DomainProviderStore(str(path))
    assert mapping.get_all() == {}
    backups = list(tmp_path.glob("state.json.corrupt-*.bak"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
    assert json.loads(path.read_text(encoding="utf-8")) == {}


def test_corrupt_json_backup_failure_preserves_original(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    original = b"{ invalid json"
    path.write_bytes(original)
    def fail_backup(*args, **kwargs):
        raise OSError("backup failed")
    monkeypatch.setattr(store.shutil, "copy2", fail_backup)
    with pytest.raises(OSError, match="backup failed"):
        store.DomainProviderStore(str(path))
    assert path.read_bytes() == original


def test_atomic_replace_failure_keeps_original(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    mapping = store.DomainProviderStore(str(path))
    mapping.save([{"dns_name": "old", "domain": "example.com"}])
    original = path.read_bytes()
    def fail_replace(*args, **kwargs):
        raise OSError("replace failed")
    monkeypatch.setattr(store.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        mapping.save([{"dns_name": "new", "domain": "example.com"}])
    assert path.read_bytes() == original


def test_domain_provider_rejects_legacy_secret_dict(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"example.com": {"secret": "do-not-load"}}', encoding="utf-8")
    with pytest.raises(ValueError, match="dict is not allowed"):
        store.DomainProviderStore(str(path))


def test_renew_state_failure_then_success_preserves_total(tmp_path):
    path = tmp_path / "renew.json"
    state = store.RenewStateStore(str(path))
    assert state.get(" ") is None
    state.record_failure("EXAMPLE.COM", "test", "error 1")
    state.record_failure("example.com", "test", "error 2")
    assert state.get("example.com")["consecutive_failures"] == 2
    state.record_success("example.com", "test", "ok")
    restored = store.RenewStateStore(str(path)).get("EXAMPLE.COM")
    assert restored["last_status"] == "success"
    assert restored["consecutive_failures"] == 0
    assert restored["total_failures"] == 2
    state.record_deploy_failure("example.com", "test", "deploy failed")
    assert state.get("example.com")["last_status"] == "deploy_failed"
    assert state.get("example.com")["total_failures"] == 2


def test_deploy_state_failure_then_success(tmp_path):
    path = tmp_path / "deploy.json"
    state = store.DeployStateStore(str(path))
    args = ("EXAMPLE.COM", "Tencent", "CDN", "resource-1")
    state.record_failure(*args, "failed")
    state.record_failure(*args, "failed again")
    state.record_success(*args, "ok")
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert len(raw) == 1
    entry = next(iter(raw.values()))
    assert entry["last_status"] == "success"
    assert entry["total_failures"] == 2
    assert entry["consecutive_failures"] == 0
