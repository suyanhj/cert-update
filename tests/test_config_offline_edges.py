from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import app.config as cfg


def test_config_normalizers_and_dedupe():
    assert cfg._normalize_extends(None) == []
    assert cfg._normalize_extends(" base.yaml ") == [" base.yaml "]
    assert cfg._normalize_extends([" a ", "b"]) == ["a", "b"]
    with pytest.raises(ValueError):
        cfg._normalize_extends([1])
    with pytest.raises(ValueError):
        cfg._normalize_extends(123)

    assert cfg._dedupe_scalar_list([" A ", "a", "", None, "B"]) == [" A ", "B"]
    items = [{"name": "A", "v": 1}, {"name": "a", "v": 2}, "raw", {}]
    assert cfg._dedupe_named_list(items, "name") == [{"name": "a", "v": 2}, "raw", {}]
    assert cfg._rule_key({"target_group": "G", "cert_name_template": "T", "cert_domains": ["B", "a", "a"]}) == "g|T|a,b"
    assert cfg._rule_key(None) == ""
    rules = [{"target_group": "g", "cert_name_template": "t", "cert_domains": ["a"]},
             {"target_group": "G", "cert_name_template": "t", "cert_domains": ["A"]}]
    assert cfg._dedupe_rule_list(rules)[0]["target_group"] == "G"

    raw = {
        "providers": [
            {"name": "P", "static_domains": [{"domain": "a"}, {"domain": "A", "sub_domains": ["x", "X"]}],
             "sub_extensions": [{"domain": "www", "redirect_to": "x"}, {"domain": "WWW", "redirect_to": "y"}]},
            {"name": "p", "static_domains": []},
        ],
        "ssh_profiles": [{"name": "s"}, {"name": "S", "x": 1}],
        "whitelist": ["a", "A", ""],
        "blacklist": ["*.Example.com", "EXAMPLE.com"],
        "nginx": {
            "nginx_hosts": [{"name": "h"}, {"name": "H", "x": 1}],
            "nginx_target_groups": [{"name": "g"}, {"name": "G", "x": 1}],
            "nginx_deploy_rules": rules,
        },
    }
    out = cfg._dedupe_config_lists(raw)
    assert len(out["providers"]) == 1
    assert out["providers"][0]["name"] == "p"
    assert out["whitelist"] == ["a"]
    assert len(out["nginx"]["nginx_hosts"]) == 1
    assert len(out["nginx"]["nginx_deploy_rules"]) == 1


def test_config_paths_merge_extends_and_validation_expansion(tmp_path, monkeypatch):
    parent = tmp_path / "parent.yaml"
    child = tmp_path / "child.yaml"
    parent.write_text(yaml.safe_dump({"a": 1, "nested": {"x": 1}, "list": [1], "providers": [{"name": "p"}]}), encoding="utf-8")
    child.write_text(yaml.safe_dump({"extends": "parent.yaml", "nested": {"y": 2}, "a": 2}), encoding="utf-8")

    assert cfg._read_yaml_dict(parent)["a"] == 1
    assert cfg._resolve_extends_path(child, "parent.yaml") == parent.resolve()
    assert cfg._load_with_extends(child)["nested"] == {"x": 1, "y": 2}
    assert cfg._load_with_extends(child)["a"] == 2

    cyc = tmp_path / "cyc.yaml"
    cyc.write_text(yaml.safe_dump({"extends": "cyc.yaml"}), encoding="utf-8")
    with pytest.raises(ValueError, match="cycle"):
        cfg._load_with_extends(cyc)

    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump(["x"]), encoding="utf-8")
    with pytest.raises(ValueError, match="mapping"):
        cfg._read_yaml_dict(bad)

    missing = tmp_path / "missing.yaml"
    with pytest.raises(FileNotFoundError):
        cfg._read_yaml_dict(missing)

    directory = tmp_path / "dir"
    directory.mkdir()
    with pytest.raises(ValueError, match="not a file"):
        cfg._read_yaml_dict(directory)

    monkeypatch.setattr(cfg, "CONFIG_PATH", child)
    expanded = cfg.get_expanded_config_for_validation({"extends": "parent.yaml", "nested": {"z": 3}})
    assert expanded["nested"] == {"x": 1, "z": 3}


def test_config_set_path_and_runtime_cache(monkeypatch, tmp_path):
    target = tmp_path / "config.yaml"
    target.write_text("x: 1", encoding="utf-8")
    old_path = cfg.CONFIG_PATH
    old_cache = cfg._CONFIG_CACHE
    try:
        cfg.set_config_path(target)
        assert cfg.CONFIG_PATH == target
        with pytest.raises(ValueError):
            cfg.set_config_path(tmp_path)
    finally:
        cfg.CONFIG_PATH = old_path
        cfg._CONFIG_CACHE = old_cache

    conf = SimpleNamespace(log_level="INFO")
    monkeypatch.setattr(cfg, "set_global_log_level", lambda value: (_ for _ in ()).throw(RuntimeError("bad")))
    with pytest.raises(ValueError, match="log_level"):
        cfg._apply_log_level(conf)

    monkeypatch.setattr(cfg, "_read_raw_dict", lambda: {"log_level": "INFO"})
    monkeypatch.setattr(cfg, "_validate_config", lambda data: {"log_level": data["log_level"]})
    monkeypatch.setattr(cfg, "_to_box", lambda data: SimpleNamespace(**data))
    monkeypatch.setattr(cfg, "_apply_log_level", lambda conf: None)
    cfg._CONFIG_CACHE = None
    first = cfg.get_config()
    second = cfg.get_config()
    assert first is second
    cfg.reload_config()
    assert cfg.load_raw_config() is cfg.get_config()


def test_update_config_atomic_error_paths(monkeypatch, tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("log_level: INFO", encoding="utf-8")
    monkeypatch.setattr(cfg, "CONFIG_PATH", path)
    monkeypatch.setattr(cfg, "_expand_config_for_validation", lambda raw, base: raw)
    monkeypatch.setattr(cfg, "_validate_config", lambda raw: None)
    monkeypatch.setattr(cfg, "reload_config", lambda: "reloaded")

    assert cfg.update_config_atomic({"log_level": "DEBUG"}) == "reloaded"
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["log_level"] == "DEBUG"

    monkeypatch.setattr(cfg.os, "access", lambda *args: False)
    with pytest.raises(RuntimeError, match="不可写"):
        cfg.update_config_atomic({"log_level": "INFO"})

    monkeypatch.setattr(cfg.os, "access", lambda *args: True)
    monkeypatch.setattr(cfg.tempfile, "mkstemp", lambda **kwargs: (_ for _ in ()).throw(OSError(30, "read only")))
    with pytest.raises(RuntimeError, match="只读"):
        cfg.update_config_atomic({"log_level": "INFO"})
