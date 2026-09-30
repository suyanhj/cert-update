"""acme.sh 列表解析、主域名匹配与证书路径选择。"""
from datetime import datetime, timezone

from box import Box
import pytest
from pydantic import ValidationError

from app.schemas.acme import AcmeShConfigError, AcmeShInfoParseError
from app.schemas.config import AcmeConfig
from app.utils.acme_sh import AcmeShRenewer
from app.utils.store import DomainProviderStore


# 复现线上日志：通配证书 Main_Domain=testali.sqxs123.com，SAN=*.testali.sqxs123.com
_LIST_OUTPUT = """
Main_Domain   KeyLength       SAN_Domains     Profile CA      Created Renew
sqxs123.com   "ec-256"        *.sqxs123.com           LetsEncrypt.org 2026-08-14T02:12:38Z    2026-10-13T04:03:19Z
testali.sqxs123.com   "ec-256"        *.testali.sqxs123.com           LetsEncrypt.org 2026-08-14T02:26:43Z    2026-10-13T07:43:01Z
sweetplay.test.sqxs123.com    "ec-256"        *.sweetplay.test.sqxs123.com            ZeroSSL.com     2026-05-22T00:54:06Z    2026-08-07T00:54:06Z
"""


_PARENT_WILDCARD_LIST_OUTPUT = """
Main_Domain      KeyLength  SAN_Domains        Profile  CA               Created               Renew
gzyys26.com      "ec-256"   *.gzyys26.com               LetsEncrypt.org  2026-09-03T10:40:44Z  2026-10-02T10:40:44Z
gzyysi.com       "ec-256"   *.gzyysi.com                LetsEncrypt.org  2026-09-01T06:00:52Z  2026-09-30T06:00:52Z
heyan2026.com    "ec-256"   *.heyan2026.com             LetsEncrypt.org  2026-09-10T10:47:43Z  2026-10-09T10:47:43Z
pull.gzyysi.com  "ec-256"   *.pull.gzyysi.com           LetsEncrypt.org  2026-09-10T06:33:27Z  2026-10-09T06:33:27Z
sqdx2026.com     "ec-256"   *.sqdx2026.com              LetsEncrypt.org  2026-09-10T10:58:40Z  2026-10-09T10:58:40Z
"""


# 脱敏前无需访问外部接口；字段结构来自真实 acme.sh ECC 证书信息输出。
_REAL_ECC_INFO_OUTPUT = """
DOMAIN_CONF=/root/.acme.sh/test.gzyys26.com_ecc/test.gzyys26.com.conf
Le_Domain=test.gzyys26.com
Le_Alt=*.test.gzyys26.com
Le_Webroot=dns_tencent
Le_PreHook=
Le_PostHook=
Le_RenewHook=
Le_API=https://acme-staging-v02.api.letsencrypt.org/directory
Le_Keylength=ec-256
Le_CertCreateTime=1787727888
Le_CertCreateTimeStr=2026-08-26T07:04:48Z
Le_NextRenewTimeStr=2026-09-24T07:04:48Z
Le_NextRenewTime=1790233488
Le_RealCertPath=
Le_RealCACertPath=
Le_RealKeyPath=/etc/nginx/certs/test.gzyys26.com.key
Le_ReloadCmd=
Le_RealFullChainPath=/etc/nginx/certs/test.gzyys26.com.pem
"""


def test_parse_acme_list_reads_main_and_wildcard_san():
    entries = AcmeShRenewer._parse_acme_list(_LIST_OUTPUT)
    by_main = {main: domains for main, domains in entries}
    assert "testali.sqxs123.com" in by_main
    assert "*.testali.sqxs123.com" in by_main["testali.sqxs123.com"]
    assert "testali.sqxs123.com" in by_main["testali.sqxs123.com"]


def test_pick_acme_main_domain_maps_single_level_subdomain():
    entries = AcmeShRenewer._parse_acme_list(_LIST_OUTPUT)
    main = AcmeShRenewer._pick_acme_main_domain(entries, "habor.testali.sqxs123.com")
    assert main == "testali.sqxs123.com"


def test_pick_acme_main_domain_exact_main_domain():
    entries = AcmeShRenewer._parse_acme_list(_LIST_OUTPUT)
    main = AcmeShRenewer._pick_acme_main_domain(entries, "testali.sqxs123.com")
    assert main == "testali.sqxs123.com"


def test_pick_acme_main_domain_prefers_longest_wildcard():
    entries = AcmeShRenewer._parse_acme_list(_LIST_OUTPUT)
    main = AcmeShRenewer._pick_acme_main_domain(entries, "foo.sweetplay.test.sqxs123.com")
    assert main == "sweetplay.test.sqxs123.com"


def test_pick_acme_main_domain_rejects_multi_level_under_parent_wildcard():
    # *.sqxs123.com 不能覆盖 habor.testali.sqxs123.com（两级 label）
    entries = AcmeShRenewer._parse_acme_list(
        """
Main_Domain   KeyLength       SAN_Domains     Profile CA      Created Renew
sqxs123.com   "ec-256"        *.sqxs123.com           LetsEncrypt.org 2026-08-14T02:12:38Z    2026-10-13T04:03:19Z
"""
    )
    main = AcmeShRenewer._pick_acme_main_domain(entries, "habor.testali.sqxs123.com")
    assert main is None


def test_pick_acme_main_domain_missing_cert():
    entries = AcmeShRenewer._parse_acme_list(_LIST_OUTPUT)
    main = AcmeShRenewer._pick_acme_main_domain(entries, "unknown.example.com")
    assert main is None


def test_exact_main_domain_does_not_treat_parent_wildcard_as_existing_child_cert():
    entries = AcmeShRenewer._parse_acme_list(_PARENT_WILDCARD_LIST_OUTPUT)

    assert AcmeShRenewer._pick_acme_main_domain(
        entries,
        "test.gzyys26.com",
    ) == "gzyys26.com"
    assert AcmeShRenewer._pick_exact_acme_main_domain(
        entries,
        "test.gzyys26.com",
    ) is None


def test_exact_main_domain_matches_same_main_regardless_of_case_and_trailing_dot():
    entries = AcmeShRenewer._parse_acme_list(
        _PARENT_WILDCARD_LIST_OUTPUT
        + '\nTEST.GZYYS26.COM. "ec-256" *.test.gzyys26.com LetsEncrypt.org '
        '2026-09-17T01:00:00Z 2026-10-16T01:00:00Z\n'
    )

    assert AcmeShRenewer._pick_exact_acme_main_domain(
        entries,
        "test.gzyys26.com",
    ) == "test.gzyys26.com"


def test_exact_main_domain_does_not_treat_san_only_match_as_same_cert():
    entries = [
        (
            "bundle.gzyys26.com",
            {"bundle.gzyys26.com", "test.gzyys26.com", "*.test.gzyys26.com"},
        )
    ]

    assert AcmeShRenewer._pick_acme_main_domain(
        entries,
        "test.gzyys26.com",
    ) == "bundle.gzyys26.com"
    assert AcmeShRenewer._pick_exact_acme_main_domain(
        entries,
        "test.gzyys26.com",
    ) is None


def test_build_renew_cmd_includes_dnssleep_and_force():
    renewer = AcmeShRenewer.__new__(AcmeShRenewer)
    renewer._acme_sh = "/root/.acme.sh/acme.sh"
    renewer._config = Box({"acme": {"dns_sleep_seconds": 60}}, default_box=True)
    cmd = renewer._build_renew_cmd("example.com", force=True)
    assert cmd == [
        "/root/.acme.sh/acme.sh",
        "--renew",
        "-d",
        "example.com",
        "--dnssleep",
        "60",
        "--force",
    ]


def test_build_renew_cmd_skips_dnssleep_when_zero():
    renewer = AcmeShRenewer.__new__(AcmeShRenewer)
    renewer._acme_sh = "acme.sh"
    renewer._config = Box({"acme": {"dns_sleep_seconds": 0}}, default_box=True)
    cmd = renewer._build_renew_cmd("example.com", force=False)
    assert cmd == ["acme.sh", "--renew", "-d", "example.com"]


def test_build_issue_cmd_includes_dns_plugin_domains_and_sleep():
    renewer = AcmeShRenewer.__new__(AcmeShRenewer)
    renewer._acme_sh = "/root/.acme.sh/acme.sh"
    renewer._config = Box({"acme": {"dns_sleep_seconds": 60}}, default_box=True)

    cmd = renewer._build_issue_cmd(
        ["example.com", "*.example.com", "www.example.com"],
        "dns_cf",
    )

    assert cmd == [
        "/root/.acme.sh/acme.sh",
        "--issue",
        "--dns",
        "dns_cf",
        "-d",
        "example.com",
        "-d",
        "*.example.com",
        "-d",
        "www.example.com",
        "--dnssleep",
        "60",
    ]


def test_normalize_issue_domains_keeps_main_first_and_deduplicates():
    assert AcmeShRenewer._normalize_issue_domains(
        "Example.COM.",
        ["example.com", "*.Example.com.", "www.example.com", "*.example.com"],
    ) == ["example.com", "*.example.com", "www.example.com"]

    with pytest.raises(AcmeShConfigError, match="主域名不能是通配"):
        AcmeShRenewer._normalize_issue_domains("*.example.com", [])


@pytest.mark.parametrize(
    ("provider_type", "expected"),
    [
        ("aliyun", "dns_ali"),
        ("tencent", "dns_tencent"),
        ("cloudflare", "dns_cf"),
    ],
)
def test_resolve_issue_dns_plugin(provider_type, expected):
    renewer = AcmeShRenewer.__new__(AcmeShRenewer)
    renewer._config = Box(
        {
            "providers": [
                {
                    "name": "provider-1",
                    "type": provider_type,
                }
            ]
        },
        default_box=True,
    )
    assert renewer._resolve_dns_plugin("provider-1") == expected


def test_resolve_issue_dns_plugin_rejects_unsupported_huawei_ak_config():
    renewer = AcmeShRenewer.__new__(AcmeShRenewer)
    renewer._config = Box(
        {
            "providers": [
                {"name": "huawei-1", "type": "huawei"}
            ]
        },
        default_box=True,
    )

    with pytest.raises(AcmeShConfigError, match="不支持 acme.sh DNS 签发"):
        renewer._resolve_dns_plugin("huawei-1")


def test_validate_issue_domains_rejects_cross_provider():
    renewer = AcmeShRenewer.__new__(AcmeShRenewer)
    renewer._store = Box(
        {
            "resolve": lambda domain: "cf-main" if domain == "example.com" else "ali-other",
        },
        default_box=True,
    )

    with pytest.raises(AcmeShConfigError, match="必须属于同一云平台"):
        renewer._validate_issue_domain_providers(
            ["example.com", "www.example.net"],
            "cf-main",
        )


def test_validate_issue_domains_inherits_dns_provider_from_managed_root(tmp_path):
    provider_store = DomainProviderStore(str(tmp_path / "domain-provider-map.json"))
    provider_store.save([
        {
            "domain": "gzyys26.com",
            "dns_name": "cf-main",
            "subs": [],
        }
    ])
    renewer = AcmeShRenewer.__new__(AcmeShRenewer)
    renewer._store = provider_store

    renewer._validate_issue_domain_providers(
        ["test.gzyys26.com", "*.test.gzyys26.com"],
        "cf-main",
    )


def test_validate_issue_domains_rejects_more_specific_other_provider(tmp_path):
    provider_store = DomainProviderStore(str(tmp_path / "domain-provider-map.json"))
    provider_store.save([
        {"domain": "gzyys26.com", "dns_name": "cf-main", "subs": []},
        {"domain": "test.gzyys26.com", "dns_name": "ali-other", "subs": []},
    ])
    renewer = AcmeShRenewer.__new__(AcmeShRenewer)
    renewer._store = provider_store

    with pytest.raises(AcmeShConfigError, match="必须属于同一云平台"):
        renewer._validate_issue_domain_providers(
            ["test.gzyys26.com", "*.test.gzyys26.com"],
            "cf-main",
        )


def test_validate_issue_domains_rejects_missing_provider_mapping(tmp_path):
    renewer = AcmeShRenewer.__new__(AcmeShRenewer)
    renewer._store = DomainProviderStore(str(tmp_path / "domain-provider-map.json"))

    with pytest.raises(AcmeShConfigError, match="未找到签发域名"):
        renewer._validate_issue_domain_providers(
            ["test.gzyys26.com", "*.test.gzyys26.com"],
            "cf-main",
        )


def test_issue_uses_cloudflare_dns_and_loads_certificate_info(monkeypatch):
    renewer = AcmeShRenewer.__new__(AcmeShRenewer)
    renewer._acme_sh = "acme.sh"
    renewer._config = Box(
        {
            "acme": {"dns_sleep_seconds": 30},
            "staging": {"enabled": False},
            "providers": [
                {
                    "name": "cf-main",
                    "type": "cloudflare",
                    "enabled": True,
                    "credentials": {"api_token": "token-value"},
                }
            ],
        },
        default_box=True,
    )
    renewer._store = Box(
        {"resolve": lambda domain: "cf-main"},
        default_box=True,
    )
    monkeypatch.setattr(renewer, "_resolve_exact_acme_main_domain", lambda domain: None)
    commands = []

    def fake_run_cmd(cmd, env=None):
        commands.append((cmd, env))
        return "Le_Domain=example.com\n" if "--info" in cmd else ""

    expected = object()
    monkeypatch.setattr(renewer, "_run_cmd", fake_run_cmd)
    monkeypatch.setattr(
        renewer,
        "_build_renewed_cert_from_info",
        lambda domain, info: expected,
    )

    result = renewer.issue("example.com", ["*.example.com"], "cf-main")

    assert result is expected
    assert commands[0][0] == [
        "acme.sh",
        "--issue",
        "--dns",
        "dns_cf",
        "-d",
        "example.com",
        "-d",
        "*.example.com",
        "--dnssleep",
        "30",
    ]
    assert commands[0][1]["CF_Token"] == "token-value"
    assert commands[1][0] == ["acme.sh", "--info", "-d", "example.com"]


def test_issue_rejects_existing_acme_certificate_before_issue(monkeypatch):
    renewer = AcmeShRenewer.__new__(AcmeShRenewer)
    renewer._config = Box(
        {
            "staging": {"enabled": False},
            "providers": [
                {"name": "cf-main", "type": "cloudflare", "enabled": True}
            ],
        },
        default_box=True,
    )
    renewer._store = Box({"resolve": lambda domain: "cf-main"}, default_box=True)
    monkeypatch.setattr(
        renewer,
        "_resolve_exact_acme_main_domain",
        lambda domain: "example.com",
    )
    monkeypatch.setattr(
        renewer,
        "_run_cmd",
        lambda *args, **kwargs: pytest.fail("已有证书时不应执行签发命令"),
    )

    with pytest.raises(AcmeShConfigError, match="请使用强制续签"):
        renewer.issue("example.com", ["*.example.com"], "cf-main")


def _make_path_renewer(mode: str = "auto") -> AcmeShRenewer:
    """构造只用于路径选择测试的 renewer。"""
    renewer = AcmeShRenewer.__new__(AcmeShRenewer)
    renewer._config = Box(
        {"acme": {"certificate_path_mode": mode}},
        default_box=True,
    )
    return renewer


def test_acme_certificate_path_mode_defaults_and_validates():
    assert AcmeConfig().certificate_path_mode == "auto"
    assert AcmeConfig(certificate_path_mode="default").certificate_path_mode == "default"
    assert AcmeConfig(certificate_path_mode="custom").certificate_path_mode == "custom"

    with pytest.raises(ValidationError):
        AcmeConfig(certificate_path_mode="unknown")


def test_auto_path_mode_prefers_complete_custom_pair(tmp_path):
    custom_fullchain = tmp_path / "installed.pem"
    custom_key = tmp_path / "installed.key"
    recorded_fullchain = tmp_path / "fullchain.cer"
    recorded_key = tmp_path / "example.com.key"
    for path in (custom_fullchain, custom_key, recorded_fullchain, recorded_key):
        path.write_text(path.name, encoding="utf-8")

    selected = _make_path_renewer()._select_certificate_paths(
        "example.com",
        {
            "Le_RealFullChainPath": str(custom_fullchain),
            "Le_RealKeyPath": str(custom_key),
            "Le_FullchainPath": str(recorded_fullchain),
            "Le_KeyPath": str(recorded_key),
        },
        "auto",
    )

    assert selected[:3] == ("custom", str(custom_fullchain), str(custom_key))


def test_auto_path_mode_skips_incomplete_custom_pair_without_mixing(tmp_path):
    custom_fullchain = tmp_path / "installed.pem"
    recorded_fullchain = tmp_path / "fullchain.cer"
    recorded_key = tmp_path / "example.com.key"
    for path in (custom_fullchain, recorded_fullchain, recorded_key):
        path.write_text(path.name, encoding="utf-8")

    selected = _make_path_renewer()._select_certificate_paths(
        "example.com",
        {
            "Le_RealFullChainPath": str(custom_fullchain),
            "Le_RealKeyPath": str(tmp_path / "missing.key"),
            "Le_FullchainPath": str(recorded_fullchain),
            "Le_KeyPath": str(recorded_key),
        },
        "auto",
    )

    assert selected[:3] == ("recorded", str(recorded_fullchain), str(recorded_key))


def test_default_path_mode_uses_domain_conf_directory(tmp_path):
    cert_dir = tmp_path / "example.com_ecc"
    cert_dir.mkdir()
    domain_conf = cert_dir / "example.com.conf"
    fullchain = cert_dir / "fullchain.cer"
    key = cert_dir / "example.com.key"
    domain_conf.write_text("", encoding="utf-8")
    fullchain.write_text("fullchain", encoding="utf-8")
    key.write_text("key", encoding="utf-8")

    selected = _make_path_renewer("default")._select_certificate_paths(
        "example.com",
        {
            "DOMAIN_CONF": str(domain_conf),
            "Le_Domain": "example.com",
            "Le_RealFullChainPath": str(tmp_path / "ignored.pem"),
            "Le_RealKeyPath": str(tmp_path / "ignored.key"),
        },
        "default",
    )

    assert selected[:3] == ("default", str(fullchain), str(key))


def test_auto_path_mode_falls_back_to_default_pair(tmp_path):
    cert_dir = tmp_path / "example.com_ecc"
    cert_dir.mkdir()
    domain_conf = cert_dir / "example.com.conf"
    fullchain = cert_dir / "fullchain.cer"
    key = cert_dir / "example.com.key"
    domain_conf.write_text("", encoding="utf-8")
    fullchain.write_text("fullchain", encoding="utf-8")
    key.write_text("key", encoding="utf-8")

    selected = _make_path_renewer()._select_certificate_paths(
        "example.com",
        {"DOMAIN_CONF": str(domain_conf), "Le_Domain": "example.com"},
        "auto",
    )

    assert selected[:3] == ("default", str(fullchain), str(key))


def test_custom_path_mode_uses_real_path_pair(tmp_path):
    custom_fullchain = tmp_path / "example.com.pem"
    custom_key = tmp_path / "example.com.key"
    custom_fullchain.write_text("fullchain", encoding="utf-8")
    custom_key.write_text("key", encoding="utf-8")

    selected = _make_path_renewer("custom")._select_certificate_paths(
        "example.com",
        {
            "Le_RealFullChainPath": str(custom_fullchain),
            "Le_RealKeyPath": str(custom_key),
        },
        "custom",
    )

    assert selected[:3] == ("custom", str(custom_fullchain), str(custom_key))


def test_custom_path_mode_does_not_fallback(tmp_path):
    recorded_fullchain = tmp_path / "fullchain.cer"
    recorded_key = tmp_path / "example.com.key"
    recorded_fullchain.write_text("fullchain", encoding="utf-8")
    recorded_key.write_text("key", encoding="utf-8")

    with pytest.raises(AcmeShInfoParseError, match="mode=custom"):
        _make_path_renewer("custom")._select_certificate_paths(
            "example.com",
            {
                "Le_FullchainPath": str(recorded_fullchain),
                "Le_KeyPath": str(recorded_key),
            },
            "custom",
        )


def test_build_cert_uses_fullchain_without_modifying_source(tmp_path, monkeypatch):
    cert_dir = tmp_path / "example.com_ecc"
    cert_dir.mkdir()
    domain_conf = cert_dir / "example.com.conf"
    fullchain = cert_dir / "fullchain.cer"
    key = cert_dir / "example.com.key"
    domain_conf.write_text("", encoding="utf-8")
    fullchain.write_text("FULLCHAIN-CONTENT", encoding="utf-8")
    key.write_text("KEY-CONTENT", encoding="utf-8")
    renewer = _make_path_renewer("default")
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(
        renewer,
        "_parse_certificate",
        lambda _: (now, now, "EC-256", ["example.com"], "issuer", None),
    )

    cert = renewer._build_renewed_cert_from_info(
        "example.com",
        {"DOMAIN_CONF": str(domain_conf), "Le_Domain": "example.com"},
    )

    assert cert.cert_path == str(fullchain)
    assert cert.fullchain_path == str(fullchain)
    assert cert.cert_pem == "FULLCHAIN-CONTENT"
    assert cert.key_pem == "KEY-CONTENT"
    assert fullchain.name == "fullchain.cer"
    assert fullchain.read_text(encoding="utf-8") == "FULLCHAIN-CONTENT"


def test_real_ecc_info_auto_uses_install_cert_paths(tmp_path):
    """真实 ECC 输出在 auto 下应采用成对存在的 Le_Real 路径。"""
    info_map = AcmeShRenewer._parse_info_output(_REAL_ECC_INFO_OUTPUT)
    installed_fullchain = tmp_path / "test.gzyys26.com.pem"
    installed_key = tmp_path / "test.gzyys26.com.key"
    installed_fullchain.write_text("fullchain", encoding="utf-8")
    installed_key.write_text("key", encoding="utf-8")
    info_map["Le_RealFullChainPath"] = str(installed_fullchain)
    info_map["Le_RealKeyPath"] = str(installed_key)

    selected = _make_path_renewer()._select_certificate_paths(
        "test.gzyys26.com",
        info_map,
        "auto",
    )

    assert info_map["Le_Alt"] == "*.test.gzyys26.com"
    assert selected[:3] == (
        "custom",
        str(installed_fullchain),
        str(installed_key),
    )


def test_real_ecc_info_default_uses_domain_conf_directory(tmp_path):
    """真实 ECC 输出在 default 下应忽略可用的 Le_Real 路径。"""
    info_map = AcmeShRenewer._parse_info_output(_REAL_ECC_INFO_OUTPUT)
    cert_dir = tmp_path / "test.gzyys26.com_ecc"
    cert_dir.mkdir()
    domain_conf = cert_dir / "test.gzyys26.com.conf"
    default_fullchain = cert_dir / "fullchain.cer"
    default_key = cert_dir / "test.gzyys26.com.key"
    installed_fullchain = tmp_path / "installed.pem"
    installed_key = tmp_path / "installed.key"
    for path in (
        domain_conf,
        default_fullchain,
        default_key,
        installed_fullchain,
        installed_key,
    ):
        path.write_text(path.name, encoding="utf-8")
    info_map["DOMAIN_CONF"] = str(domain_conf)
    info_map["Le_RealFullChainPath"] = str(installed_fullchain)
    info_map["Le_RealKeyPath"] = str(installed_key)

    selected = _make_path_renewer("default")._select_certificate_paths(
        "test.gzyys26.com",
        info_map,
        "default",
    )

    assert selected[:3] == (
        "default",
        str(default_fullchain),
        str(default_key),
    )
