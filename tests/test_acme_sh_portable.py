"""Offline tests for acme.sh parsing, provider env mapping and command errors."""
import pytest
from box import Box

from app.utils.acme_sh import AcmeShRenewer
from app.schemas.acme import AcmeShConfigError, AcmeShExecutionError, AcmeShNotFoundError


def renewer_with_providers(providers):
    r = object.__new__(AcmeShRenewer)
    r._config = Box({"providers": providers, "staging": {"enabled": False}})
    return r


@pytest.mark.parametrize(
    "ptype,credentials,expected",
    [
        ("aliyun", {"access_key_id": "ak", "access_key_secret": "sk"}, {"Ali_Key": "ak", "Ali_Secret": "sk"}),
        ("tencent", {"secret_id": "sid", "secret_key": "skey"}, {"Tencent_SecretId": "sid", "Tencent_SecretKey": "skey"}),
        ("huawei", {"access_key_id": "ak", "access_key_secret": "sk"}, {"Huawei_Cloud_AccessKey": "ak", "Huawei_Cloud_SecretKey": "sk"}),
        ("cloudflare", {"api_token": "token"}, {"CF_Token": "token"}),
    ],
)
def test_build_provider_env_maps_credentials(ptype, credentials, expected, monkeypatch):
    provider = Box({"name": "p", "type": ptype, "enabled": True, "credentials": credentials})
    r = renewer_with_providers([provider])
    monkeypatch.setenv("CF_Account_ID", "stale-account")
    monkeypatch.setenv("CF_Zone_ID", "stale-zone")
    env = r._build_provider_env("p")
    for key, value in expected.items():
        assert env[key] == value
    if ptype == "cloudflare":
        assert "CF_Zone_ID" not in env
        assert "CF_Account_ID" not in env


def test_cloudflare_account_id_is_optional_and_trimmed():
    provider = Box({
        "name": "p", "type": "cloudflare", "enabled": True,
        "credentials": {"api_token": "token", "account_id": " acct "},
    })
    env = renewer_with_providers([provider])._build_provider_env("p")
    assert env["CF_Account_ID"] == "acct"


@pytest.mark.parametrize(
    "provider,error",
    [
        (None, "未找到云平台配置"),
        (Box({"name": "p", "type": "tencent", "enabled": False, "credentials": {}}), "已禁用"),
        (Box({"name": "p", "type": "unknown", "enabled": True, "credentials": {}}), "未配置 acme.sh"),
    ],
)
def test_build_provider_env_rejects_invalid_provider(provider, error):
    r = renewer_with_providers([] if provider is None else [provider])
    with pytest.raises(AcmeShConfigError, match=error):
        r._build_provider_env("p")


@pytest.mark.parametrize(
    "ptype,missing",
    [
        ("aliyun", "access_key_secret"),
        ("tencent", "secret_key"),
        ("huawei", "access_key_id"),
        ("cloudflare", "api_token"),
    ],
)
def test_build_provider_env_requires_credentials(ptype, missing):
    credentials = {
        "aliyun": {"access_key_id": "ak", "access_key_secret": "sk"},
        "tencent": {"secret_id": "sid", "secret_key": "skey"},
        "huawei": {"access_key_id": "ak", "access_key_secret": "sk"},
        "cloudflare": {"api_token": "token"},
    }[ptype]
    credentials.pop(missing, None)
    provider = Box({"name": "p", "type": ptype, "enabled": True, "credentials": credentials})
    with pytest.raises(AcmeShConfigError, match=missing):
        renewer_with_providers([provider])._build_provider_env("p")


@pytest.mark.parametrize(
    "output,expected",
    [
        ("Main_Domain KeyLength SAN_Domains Created Renew\nexample.com ec-256 www.example.com,api.example.com LetsEncrypt 2026-01-01T00:00:00 2026-02-01T00:00:00", [("example.com", {"example.com", "www.example.com", "api.example.com"})]),
        ("no certificates", []),
        ("", []),
    ],
)
def test_parse_acme_list(output, expected):
    assert AcmeShRenewer._parse_acme_list(output) == expected


def test_parse_real_acme_sh_list_output_with_profile_and_quoted_key_length():
    output = """Main_Domain       KeyLength  SAN_Domains         Profile  CA               Created               Renew
 gzyys26.com       "ec-256"   *.gzyys26.com                LetsEncrypt.org  2026-10-15T05:55:05Z  2026-11-13T05:55:05Z"""
    assert AcmeShRenewer._parse_acme_list(output) == [
        ("gzyys26.com", {"gzyys26.com", "*.gzyys26.com"})
    ]


def test_pick_acme_main_domain_prefers_exact_then_single_level_wildcard():
    entries = [
        ("wild.example.com", {"wild.example.com", "*.example.com"}),
        ("exact.example.com", {"exact.example.com"}),
    ]
    assert AcmeShRenewer._pick_acme_main_domain(entries, "exact.example.com") == "exact.example.com"
    assert AcmeShRenewer._pick_acme_main_domain(entries, "api.example.com") == "wild.example.com"
    assert AcmeShRenewer._pick_acme_main_domain(entries, "deep.api.example.com") is None


def test_parse_acme_list_row_handles_empty_and_header():
    assert AcmeShRenewer._parse_acme_list_row("") is None
    assert AcmeShRenewer._parse_acme_list_row("Main_Domain KeyLength SAN_Domains") is None


def test_resolve_acme_main_domain_propagates_list_error(monkeypatch):
    r = object.__new__(AcmeShRenewer)
    r._acme_sh = "acme.sh"
    def fail(_cmd):
        from app.schemas.acme import AcmeShExecutionError
        raise AcmeShExecutionError("list failed")
    monkeypatch.setattr(r, "_run_cmd", fail)
    with pytest.raises(AcmeShExecutionError):
        r._resolve_acme_main_domain("example.com")


def test_resolve_acme_sh_path_accepts_command_name(monkeypatch):
    monkeypatch.setattr("app.utils.acme_sh.shutil.which", lambda name: "/usr/bin/acme.sh")
    r = object.__new__(AcmeShRenewer)
    r._config = Box({"acme": {"acme_sh_path": "acme.sh"}, "staging": {"enabled": False}})
    assert r._resolve_acme_sh_path() == "/usr/bin/acme.sh"


def test_resolve_acme_sh_path_rejects_missing_explicit_path(tmp_path):
    r = object.__new__(AcmeShRenewer)
    r._config = Box({"acme": {"acme_sh_path": str(tmp_path / "missing" / "acme.sh")}, "staging": {"enabled": False}})
    with pytest.raises(AcmeShNotFoundError):
        r._resolve_acme_sh_path()


@pytest.mark.parametrize("configured_path", ["acme.sh", "./acme.sh"])
def test_resolve_local_acme_sh_returns_absolute_path(monkeypatch, tmp_path, configured_path):
    """PATH 无命令时，本地脚本必须返回绝对路径，避免再次搜索 PATH。"""
    script = tmp_path / "acme.sh"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("app.utils.acme_sh.shutil.which", lambda name: None)
    r = object.__new__(AcmeShRenewer)
    r._config = Box({"acme": {"acme_sh_path": configured_path}, "staging": {"enabled": False}})
    assert r._resolve_acme_sh_path() == str(script.resolve())
