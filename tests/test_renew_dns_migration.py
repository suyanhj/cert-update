"""验证 DNS 插件迁移使用强制签发并保留现有证书配置。"""

from unittest.mock import Mock

import pytest
from box import Box

from app.schemas.acme import AcmeShConfigError, AcmeShInfoParseError
from app.utils.acme_sh import AcmeShRenewer


@pytest.fixture
def client():
    renewer = AcmeShRenewer.__new__(AcmeShRenewer)
    renewer._acme_sh = "acme.sh"
    renewer._config = Box({
        "staging": {"enabled": False},
        "acme": {"dns_sleep_seconds": 60},
        "providers": [{
            "name": "current-dns", "type": "cloudflare", "enabled": True,
            "credentials": {"api_token": "current-cf-token"},
        }],
    })
    renewer._store = Mock()
    renewer._store.resolve.return_value = "current-dns"
    return renewer


@pytest.fixture
def info():
    return {
        "Le_Domain": "sqht789.com", "Le_Alt": "*.sqht789.com,api.sqht789.com",
        "Le_Webroot": "dns_ali", "Le_Keylength": "ec-256",
        "Le_API": "https://acme-v02.api.letsencrypt.org/directory",
        "Le_RealFullChainPath": "/etc/nginx/certs/sqht789.com.pem",
        "Le_RealKeyPath": "/etc/nginx/certs/sqht789.com.key",
        "Le_PreHook": 'echo "prepare"', "Le_PostHook": "echo complete",
        "Le_RenewHook": "echo renewed", "Le_ReloadCmd": "nginx -t && nginx -s reload",
    }


def _run_renew(monkeypatch, client, info, *, force=False):
    output = "\n".join(f"{k}={v}" for k, v in info.items())
    run = Mock(return_value=output)
    monkeypatch.setattr(client, "_run_cmd", run)
    monkeypatch.setattr(client, "_build_renewed_cert_from_info", lambda domain, data: data)
    result = client.renew("api.sqht789.com", force=force, main_domain="sqht789.com")
    return run, result


@pytest.mark.parametrize("force", [False, True])
def test_aliyun_to_cloudflare_reissues_with_all_domains_and_current_credentials(monkeypatch, client, info, force):
    run, result = _run_renew(monkeypatch, client, info, force=force)
    assert result == info
    assert run.call_args_list[0].args[0] == ["acme.sh", "--info", "-d", "sqht789.com"]
    command = run.call_args_list[1].args[0]
    assert command[:4] == ["acme.sh", "--issue", "--dns", "dns_cf"]
    assert "--force" in command
    assert [command[i + 1] for i, value in enumerate(command) if value == "-d"] == [
        "sqht789.com", "*.sqht789.com", "api.sqht789.com",
    ]
    assert command == ["acme.sh", "--issue", "--dns", "dns_cf", "-d", "sqht789.com",
                       "-d", "*.sqht789.com", "-d", "api.sqht789.com", "--dnssleep", "60", "--force"]
    assert run.call_args_list[2].args[0] == ["acme.sh", "--install-cert", "-d", "sqht789.com",
        "--key-file", info["Le_RealKeyPath"], "--fullchain-file", info["Le_RealFullChainPath"]]
    assert run.call_args_list[1].kwargs["env"]["CF_Token"] == "current-cf-token"
    assert run.call_args_list[3].args[0] == ["acme.sh", "--info", "-d", "sqht789.com"]


@pytest.mark.parametrize("saved", ["dns_cf", "dns_cf,dns_cf,dns_cf"])
def test_same_dns_plugin_keeps_renew(monkeypatch, client, info, saved):
    info["Le_Webroot"] = saved
    run, _ = _run_renew(monkeypatch, client, info)
    assert run.call_args_list[1].args[0] == ["acme.sh", "--renew", "-d", "sqht789.com", "--dnssleep", "60"]


@pytest.mark.parametrize("field", ["Le_Domain", "Le_Alt", "Le_Webroot"])
def test_missing_original_fields_stops_before_issuance(monkeypatch, client, info, field):
    info.pop(field)
    run = Mock(return_value="\n".join(f"{k}={v}" for k, v in info.items()))
    monkeypatch.setattr(client, "_run_cmd", run)
    with pytest.raises(AcmeShInfoParseError):
        client.renew("sqht789.com", main_domain="sqht789.com", force=True)
    assert run.call_count == 1


def test_cross_provider_san_blocks_migration(monkeypatch, client, info):
    client._store.resolve.side_effect = lambda domain: "other-account" if domain == "api.sqht789.com" else "current-dns"
    run = Mock(return_value="\n".join(f"{k}={v}" for k, v in info.items()))
    monkeypatch.setattr(client, "_run_cmd", run)
    with pytest.raises(AcmeShConfigError, match="同一云平台"):
        client.renew("sqht789.com", main_domain="sqht789.com")
    assert run.call_count == 1


def test_single_domain_without_adding_wildcard(client, info):
    info["Le_Alt"] = "no"
    info["Le_Keylength"] = ""
    command = client._build_dns_migration_cmd("sqht789.com", info, "dns_cf")
    assert "--keylength" not in command
    assert command.count("-d") == 1
    assert "*.sqht789.com" not in command


def test_optional_saved_settings_are_not_mapped(client, info):
    options = {
        "Le_RealCertPath": ("--cert-file", "/cert.pem"),
        "Le_RealCACertPath": ("--ca-file", "/ca.pem"),
        "Le_LocalAddress": ("--local-address", "127.0.0.1"),
        "Le_ChallengeAlias": ("--challenge-alias", "validation.example.com"),
        "Le_Preferred_Chain": ("--preferred-chain", "ISRG Root X1"),
        "Le_Valid_From": ("--valid-from", "+0"),
        "Le_Valid_To": ("--valid-to", "+90d"),
        "Le_Certificate_Profile": ("--certificate-profile", "classic"),
        "Le_ExtKeyUse": ("--extended-key-usage", "serverAuth"),
    }
    info.update({key: value for key, (flag, value) in options.items()})
    command = client._build_dns_migration_cmd("sqht789.com", info, "dns_cf")
    for flag, value in options.values():
        assert flag not in command


def test_no_install_paths_skips_install(monkeypatch, client, info):
    info.pop("Le_RealKeyPath")
    info.pop("Le_RealFullChainPath")
    run, _ = _run_renew(monkeypatch, client, info)
    assert run.call_count == 3
    assert all("--install-cert" not in call.args[0] for call in run.call_args_list)


@pytest.mark.parametrize("failure_step", [1, 2])
def test_issue_or_install_failure_stops_certificate_read(monkeypatch, client, info, failure_step):
    output = "\n".join(f"{k}={v}" for k, v in info.items())
    run = Mock(side_effect=[output] * failure_step + [AcmeShConfigError("执行失败")])
    build = Mock()
    monkeypatch.setattr(client, "_run_cmd", run)
    monkeypatch.setattr(client, "_build_renewed_cert_from_info", build)
    with pytest.raises(AcmeShConfigError, match="执行失败"):
        client.renew("sqht789.com", main_domain="sqht789.com")
    assert run.call_count == failure_step + 1
    build.assert_not_called()
