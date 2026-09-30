import asyncio
import socket
from pathlib import Path

import pytest
import yaml
from box import Box

import app.config as config_mod
import app.utils.http_client as http_client
import app.utils.tls_probe as tls_probe
from app.services import config_runtime


@pytest.fixture(autouse=True)
def restore_config_path():
    original_path = config_mod.CONFIG_PATH
    yield
    config_mod.set_config_path(original_path)
    config_mod.reload_config()


def _copy_runtime_config(tmp_path: Path) -> Path:
    source = Path(config_mod.CONFIG_PATH)
    target = tmp_path / "config.yaml"
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return target


def test_update_config_atomic_applies_changes(tmp_path: Path):
    target = _copy_runtime_config(tmp_path)
    config_mod.set_config_path(target)

    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    data["check_interval_minutes"] = 13
    data["alert"]["cert"]["cert_warn_days"] = 11

    updated = config_mod.update_config_atomic(data)

    assert int(updated.check_interval_minutes) == 13
    assert int(updated.alert.cert.cert_warn_days) == 11

    on_disk = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert int(on_disk["check_interval_minutes"]) == 13
    assert int(on_disk["alert"]["cert"]["cert_warn_days"]) == 11


def test_update_config_atomic_invalid_config_does_not_overwrite_file(tmp_path: Path):
    target = _copy_runtime_config(tmp_path)
    config_mod.set_config_path(target)
    before = target.read_text(encoding="utf-8")

    with pytest.raises(ValueError):
        config_mod.update_config_atomic({"invalid": "x"})

    after = target.read_text(encoding="utf-8")
    assert before == after


def test_config_extends_merges_parent_and_child(tmp_path: Path):
    parent_path = _copy_runtime_config(tmp_path)
    child_path = tmp_path / "config.child.yaml"
    child_path.write_text(
        yaml.safe_dump(
            {
                "extends": "./config.yaml",
                "check_interval_minutes": 17,
                "alert": {"cert": {"cert_warn_days": 9}},
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    config_mod.set_config_path(child_path)
    loaded = config_mod.get_config(force_reload=True)

    assert int(loaded.check_interval_minutes) == 17
    assert int(loaded.alert.cert.cert_warn_days) == 9
    assert hasattr(loaded, "acme")
    assert hasattr(loaded, "providers")


def test_config_extends_cycle_raises_error(tmp_path: Path):
    path_a = tmp_path / "a.yaml"
    path_b = tmp_path / "b.yaml"
    path_a.write_text("extends: ./b.yaml\n", encoding="utf-8")
    path_b.write_text("extends: ./a.yaml\n", encoding="utf-8")

    config_mod.set_config_path(path_a)
    with pytest.raises(ValueError, match="cycle"):
        config_mod.get_config(force_reload=True)


def test_probe_tls_sync_reads_timeout_from_runtime_config(monkeypatch: pytest.MonkeyPatch):
    captured = {"timeout": None}

    class DummySock:
        def settimeout(self, timeout):
            captured["timeout"] = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

    class DummySslSock(DummySock):
        def getpeercert(self, binary_form=False):
            return b"dummy-cert"

    class DummySSLContext:
        def wrap_socket(self, sock, server_hostname=None):
            return DummySslSock()

    def fake_create_connection(addr, timeout=None):
        captured["timeout"] = timeout
        return DummySock()

    monkeypatch.setattr(
        tls_probe,
        "load_raw_config",
        lambda: Box({"tls_timeout_seconds": 9}, default_box=True),
    )
    monkeypatch.setattr(tls_probe, "_SSL_CONTEXT", DummySSLContext())
    monkeypatch.setattr(tls_probe, "_extract_cert_info", lambda cert, domain: {"domain": domain})
    monkeypatch.setattr(socket, "create_connection", fake_create_connection)

    result = tls_probe.probe_tls_sync("example.com", 443)

    assert result == {"domain": "example.com"}
    assert captured["timeout"] == 9


def test_http_fetch_reads_timeout_from_runtime_config(monkeypatch: pytest.MonkeyPatch):
    captured = {"timeout": None}

    class DummyResponse:
        status_code = 200

    class DummyAsyncClient:
        def __init__(self, timeout=None, follow_redirects=True, headers=None):
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url):
            return DummyResponse()

    monkeypatch.setattr(
        http_client,
        "load_raw_config",
        lambda: Box({"http_timeout_seconds": 7}, default_box=True),
    )
    monkeypatch.setattr(http_client.httpx, "AsyncClient", DummyAsyncClient)

    response = asyncio.run(http_client.fetch(url="https://example.com"))

    assert response.status_code == 200
    assert captured["timeout"] == 7


# ---------- config_runtime.parse_config_text / validate_config_dict ----------
def test_parse_config_text_invalid_yaml_raises():
    with pytest.raises(ValueError, match="YAML parse failed"):
        config_runtime.parse_config_text("key: [unclosed\n  - 1")


def test_parse_config_text_non_dict_root_raises():
    with pytest.raises(ValueError, match="Config root must be a mapping"):
        config_runtime.parse_config_text("- a\n- b")


def test_parse_config_text_valid_returns_dict():
    out = config_runtime.parse_config_text("acme:\n  cert_storage_path: ./certs")
    assert isinstance(out, dict)
    assert out.get("acme", {}).get("cert_storage_path") == "./certs"


def test_validate_config_dict_invalid_raises():
    with pytest.raises(ValueError, match="Config validation failed"):
        config_runtime.validate_config_dict({"invalid": "x"})
    with pytest.raises(ValueError, match="Config validation failed"):
        config_runtime.validate_config_dict({})


def test_validate_config_dict_minimal_valid_passes():
    config_runtime.validate_config_dict({"acme": {}})
