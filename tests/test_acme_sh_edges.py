from pathlib import Path
from types import SimpleNamespace

import pytest

from app.utils.acme_sh import (
    AcmeShConfigError,
    AcmeShExecutionError,
    AcmeShInfoParseError,
    AcmeShNotFoundError,
    AcmeShRenewer,
)


def renewer(config=None):
    r = object.__new__(AcmeShRenewer)
    r._config = config or SimpleNamespace(
        acme=SimpleNamespace(
            acme_sh_path="acme.sh",
            cert_storage_path="./certs",
            command_timeout_seconds=10,
            dns_sleep_seconds=60,
        ),
        staging=SimpleNamespace(enabled=False, use_self_signed=True),
        providers=[],
    )
    r._acme_sh = "acme.sh"
    return r


def test_parse_info_output_handles_quotes_equals_duplicates_and_noise():
    result = AcmeShRenewer._parse_info_output(
        '''
        Le_Domain="example.com"
        Le_Alt="www.example.com,a.example.com"
        EMPTY=
        invalid line
        Weird="a=b=c"
        Le_Domain="last.example.com"
        '''
    )
    assert result["Le_Domain"] == "last.example.com"
    assert result["Le_Alt"] == "www.example.com,a.example.com"
    assert result["Weird"] == "a=b=c"
    assert result["EMPTY"] == ""


def test_must_path_and_read_helpers(tmp_path):
    path = tmp_path / "cert.pem"
    path.write_text("CERT", encoding="utf-8")
    assert AcmeShRenewer._must_path({"Cert": str(path)}, "Cert") == str(path)

    with pytest.raises(AcmeShInfoParseError, match="缺少关键字段"):
        AcmeShRenewer._must_path({}, "Cert")

    assert AcmeShRenewer._read_text(str(path), "Cert") == "CERT"
    assert AcmeShRenewer._read_optional_text(str(path)) == "CERT"
    assert AcmeShRenewer._read_optional_text(str(tmp_path / "missing")) == ""

    with pytest.raises(AcmeShInfoParseError, match="不存在"):
        AcmeShRenewer._read_text(str(tmp_path / "missing"), "Cert")


def test_fallback_paths_from_domain_conf_and_missing_inputs(tmp_path):
    conf = tmp_path / "example.com.conf"
    conf.write_text("", encoding="utf-8")
    result = AcmeShRenewer._fallback_paths_from_domain_conf(
        {"DOMAIN_CONF": str(conf), "Le_Domain": "example.com"}
    )
    assert result == {
        "key": str(tmp_path / "example.com.key"),
        "fullchain": str(tmp_path / "fullchain.cer"),
        "ca": str(tmp_path / "ca.cer"),
    }
    assert AcmeShRenewer._fallback_paths_from_domain_conf({}) is None


def test_certificate_path_mode_and_pair_selection(tmp_path):
    custom_full = tmp_path / "custom-fullchain.pem"
    custom_key = tmp_path / "custom-key.pem"
    recorded_full = tmp_path / "recorded-fullchain.pem"
    recorded_key = tmp_path / "recorded-key.pem"
    for p in (custom_full, custom_key, recorded_full, recorded_key):
        p.write_text(p.name, encoding="utf-8")

    r = renewer()
    r._config.acme = {"certificate_path_mode": "auto"}
    assert r._certificate_path_mode() == "auto"

    info = {
        "Le_RealFullChainPath": str(custom_full),
        "Le_RealKeyPath": str(custom_key),
        "Le_FullchainPath": str(recorded_full),
        "Le_KeyPath": str(recorded_key),
        "Le_CAPath": "ca.pem",
    }
    assert r._select_certificate_paths("example.com", info, "auto") == (
        "custom", str(custom_full), str(custom_key), None
    )
    assert r._select_certificate_paths("example.com", info, "recorded") == (
        "recorded", str(recorded_full), str(recorded_key), "ca.pem"
    )

    r._config.acme = {"certificate_path_mode": "bad"}
    with pytest.raises(AcmeShConfigError, match="不支持"):
        r._certificate_path_mode()

    with pytest.raises(AcmeShInfoParseError, match="证书路径不可用"):
        r._select_certificate_paths("example.com", {}, "custom")


def test_build_commands_cover_dns_sleep_and_force():
    r = renewer()
    r._config.acme.dns_sleep_seconds = 60
    assert r._build_renew_cmd("example.com", force=True) == [
        "acme.sh", "--renew", "-d", "example.com", "--dnssleep", "60", "--force"
    ]
    assert r._build_issue_cmd(["example.com", "*.example.com"], "dns_ali") == [
        "acme.sh", "--issue", "--dns", "dns_ali",
        "-d", "example.com", "-d", "*.example.com", "--dnssleep", "60"
    ]

    r._config.acme.dns_sleep_seconds = 0
    assert "--dnssleep" not in r._build_renew_cmd("example.com", force=False)
    with pytest.raises(AcmeShConfigError, match="域名集合不能为空"):
        r._build_issue_cmd([], "dns_ali")
    with pytest.raises(AcmeShConfigError, match="DNS 插件不能为空"):
        r._build_issue_cmd(["example.com"], "")


def test_dns_migration_validation_and_normalization():
    r = renewer()
    cmd = r._build_dns_migration_cmd(
        "example.com",
        {"Le_Domain": "example.com", "Le_Alt": "www.example.com,no,*.example.com"},
        "dns_tencent",
    )
    assert cmd[:4] == ["acme.sh", "--issue", "--dns", "dns_tencent"]
    assert "--force" in cmd

    with pytest.raises(AcmeShInfoParseError, match="不一致"):
        r._build_dns_migration_cmd(
            "example.com", {"Le_Domain": "other.com", "Le_Alt": "no"}, "dns_ali"
        )
    with pytest.raises(AcmeShInfoParseError, match="缺少 Le_Alt"):
        r._build_dns_migration_cmd(
            "example.com", {"Le_Domain": "example.com"}, "dns_ali"
        )

    assert AcmeShRenewer._normalize_issue_domains(
        "EXAMPLE.COM.", ["www.example.com", "WWW.EXAMPLE.COM", "*.example.com"]
    ) == ["example.com", "www.example.com", "*.example.com"]
    with pytest.raises(AcmeShConfigError, match="不能是通配域名"):
        AcmeShRenewer._normalize_issue_domains("*.example.com", [])


def test_pick_main_domain_prefers_exact_then_best_single_level_wildcard():
    entries = [
        ("parent", {"parent", "*.example.com"}),
        ("wild", {"wild.example.com", "*.deep.example.com"}),
        ("exact", {"exact.example.com"}),
    ]
    assert AcmeShRenewer._pick_acme_main_domain(entries, "exact.example.com") == "exact"
    assert AcmeShRenewer._pick_acme_main_domain(entries, "api.deep.example.com") == "wild"
    assert AcmeShRenewer._pick_acme_main_domain(entries, "a.example.com") == "parent"
    assert AcmeShRenewer._pick_exact_acme_main_domain(entries, "a.example.com") is None


def test_parse_acme_list_variants_and_empty_header():
    output = """Main_Domain KeyLength SAN_Domains Profile CA Created Renew
example.com "ec-256" www.example.com,api.example.com "" LetsEncrypt.org 2026-01-01T00:00:00Z 2026-02-01T00:00:00Z
"""
    entries = AcmeShRenewer._parse_acme_list(output)
    assert entries[0][0] == "example.com"
    assert "www.example.com" in entries[0][1]
    assert "api.example.com" in entries[0][1]
    assert AcmeShRenewer._parse_acme_list("nothing here") == []
    assert AcmeShRenewer._parse_acme_list_row("") is None


def test_provider_env_and_dns_plugin_mapping():
    r = renewer()
    r._config.providers = [
        SimpleNamespace(
            name="ali", type="aliyun", enabled=True,
            credentials={"access_key_id": "ak", "access_key_secret": "sk"},
        ),
        SimpleNamespace(
            name="cf", type="cloudflare", enabled=True,
            credentials={"api_token": "token", "account_id": "acct"},
        ),
    ]
    env = r._build_provider_env("ali")
    assert env["Ali_Key"] == "ak"
    assert env["Ali_Secret"] == "sk"

    env = r._build_provider_env("cf")
    assert env["CF_Token"] == "token"
    assert env["CF_Account_ID"] == "acct"
    assert "CF_Zone_ID" not in env

    with pytest.raises(AcmeShConfigError, match="未找到云平台"):
        r._build_provider_env("missing")

    disabled = SimpleNamespace(
        name="off", type="aliyun", enabled=False,
        credentials={"access_key_id": "ak", "access_key_secret": "sk"},
    )
    r._config.providers.append(disabled)
    with pytest.raises(AcmeShConfigError, match="已禁用"):
        r._build_provider_env("off")

    assert r._require_credential({"x": "1"}, "p", "x") is None
    with pytest.raises(AcmeShConfigError, match="缺少凭证字段"):
        r._require_credential({}, "p", "x")



def test_resolve_acme_path_and_storage_path(monkeypatch, tmp_path):
    r = renewer()
    r._config.staging.enabled = True
    r._config.staging.use_self_signed = True
    assert r._resolve_acme_sh_path() == "acme.sh"

    r._config.staging.enabled = False
    r._config.acme.acme_sh_path = "definitely-missing-command"
    monkeypatch.setattr("app.utils.acme_sh.shutil.which", lambda value: None)
    with pytest.raises(AcmeShNotFoundError, match="不存在"):
        r._resolve_acme_sh_path()

    explicit = tmp_path / "acme.sh"
    explicit.write_text("#!/bin/sh", encoding="utf-8")
    r._config.acme.acme_sh_path = str(explicit)
    assert r._resolve_acme_sh_path() == str(explicit)
    r._config.acme.cert_storage_path = "./certs"
    assert r._resolve_cert_storage_path().name == "certs"


def test_run_cmd_nonzero_and_command_not_found(monkeypatch):
    r = renewer()

    class Proc:
        returncode = 2
        stdout = None
        stderr = None
        def wait(self, timeout): return None

    monkeypatch.setattr(
        "app.utils.acme_sh.subprocess.Popen",
        lambda *args, **kwargs: Proc(),
    )
    monkeypatch.setattr(r, "_stream_subprocess_output", lambda *args, **kwargs: None)
    monkeypatch.setattr(r, "_join_output_threads", lambda *args: None)
    with pytest.raises(AcmeShExecutionError, match="执行命令失败"):
        r._run_cmd(["acme.sh", "--bad"])

    def missing(*args, **kwargs):
        raise FileNotFoundError("missing")

    monkeypatch.setattr("app.utils.acme_sh.subprocess.Popen", missing)
    with pytest.raises(AcmeShNotFoundError, match="命令不存在"):
        r._run_cmd(["missing"])
