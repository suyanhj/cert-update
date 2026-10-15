"""证书来源策略、自动续签开关与手动证书部署回归。"""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from box import Box
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pydantic import ValidationError

from app.schemas.config import AppConfig, CertificatePolicyConfig
from app.services import collector, renew, renew_flow
from app.utils.acme_sh import AcmeShRenewer
from app.utils.certificate_policy import get_certificate_policy
from app.utils.manual_certificate import load_manual_certificate
from app.utils.time import TimeUtil


def _config(policies=None, **kwargs):
    return Box(AppConfig(acme={}, certificate_policies=policies or {}, **kwargs).model_dump())


def _manual_policy(tmp_path, *, sans=None, before=-1, after=30):
    """用真实密钥与证书覆盖加载及验证逻辑，避免使用伪 PEM。"""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "example.com")])
    now = TimeUtil.now_utc()
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now + timedelta(days=before))
        .not_valid_after(now + timedelta(days=after))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(s) for s in (sans or ["example.com", "*.example.com"])]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "example.pem"
    key_path = tmp_path / "example.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return {"source": "manual", "fullchain_path": str(cert_path), "key_path": str(key_path)}


def test_policy_defaults_normalization_and_exact_precedence():
    cfg = _config({" EXAMPLE.COM. ": {"auto_renew": False}, "*.example.com": {"auto_renew": False}, "www.example.com": {"auto_renew": True}})
    assert get_certificate_policy(cfg, "example.com").auto_renew is False
    assert get_certificate_policy(cfg, "WWW.EXAMPLE.COM.").auto_renew is True
    assert get_certificate_policy(cfg, "api.example.com").auto_renew is False
    assert get_certificate_policy(cfg, "deep.api.example.com").auto_renew is True
    assert get_certificate_policy(_config(), "other.com").source == "acme"
    assert get_certificate_policy(_config({"example.com": {"auto_renew": False}}), "www.example.com").auto_renew is True


@pytest.mark.parametrize("policies", [
    {"example.com": {"source": "typo"}},
    {"example.com": {"auto_renewal": False}},
    {"example.com": {"source": "manual"}},
    {"example.com": {"source": "acme", "key_path": "/tmp/key"}},
    {"example.com": {"dns_provider": "missing"}},
    {"example.com": {"dns_provider": " "}},
    {"bad..example.com": {}},
    {"Example.com": {}, "example.com.": {}},
    {"example.com": {"source": "manual", "fullchain_path": "a", "key_path": "b", "auto_renew": True}},
    {"example.com": {"source": "manual", "fullchain_path": "a", "key_path": "b", "dns_provider": "dns"}},
])
def test_invalid_policy_is_rejected_when_loading(policies):
    with pytest.raises(ValidationError):
        _config(policies)


@pytest.mark.parametrize("provider", [
    {"name": "dns", "type": "custom"},
    {"name": "dns", "type": "aliyun", "enabled": False},
    {"name": "dns", "type": "aliyun", "credentials": {"access_key_id": "key"}},
])
def test_invalid_dns_reference_is_rejected(provider):
    with pytest.raises(ValidationError):
        _config({"example.com": {"dns_provider": "dns"}}, providers=[provider])


def test_static_domain_issue_uses_referenced_dns_credentials(monkeypatch):
    cfg = _config({"example.com": {"dns_provider": "dns"}}, providers=[
        {"name": "static", "type": "custom"},
        {"name": "dns", "type": "aliyun", "credentials": {"access_key_id": "test-key", "access_key_secret": "test-secret"}},
    ])
    client = AcmeShRenewer.__new__(AcmeShRenewer)
    client._config = cfg
    client._acme_sh = "acme.sh"
    client._store = Mock()
    client._store.resolve.return_value = "static"
    monkeypatch.setattr(client, "_resolve_exact_acme_main_domain", lambda domain: None)
    run = Mock(return_value="")
    monkeypatch.setattr(client, "_run_cmd", run)
    monkeypatch.setattr(client, "_build_renewed_cert_from_info", lambda *args: "issued")
    assert client.issue("example.com", ["*.example.com"], "static") == "issued"
    assert "dns_ali" in run.call_args_list[0].args[0]
    assert run.call_args_list[0].kwargs["env"]["Ali_Key"] == "test-key"
    assert run.call_args_list[0].kwargs["env"]["Ali_Secret"] == "test-secret"
    with pytest.raises(ValueError):
        _config({"example.com": {"dns_provider": "dns"}}, providers=[])


def test_manual_certificate_loads_and_checks_wildcard_boundary(tmp_path):
    policy = CertificatePolicyConfig(**_manual_policy(tmp_path))
    cert = load_manual_certificate("www.example.com", policy)
    assert policy.auto_renew is False
    assert cert.domain == "www.example.com"
    assert "BEGIN CERTIFICATE" in cert.fullchain_pem
    assert "BEGIN PRIVATE KEY" in cert.key_pem
    with pytest.raises(ValueError, match="未覆盖"):
        load_manual_certificate("a.b.example.com", policy)


@pytest.mark.parametrize(("before", "after", "message"), [(-10, -1, "已过期"), (1, 30, "尚未生效")])
def test_manual_certificate_rejects_invalid_time(tmp_path, before, after, message):
    policy = CertificatePolicyConfig(**_manual_policy(tmp_path, before=before, after=after))
    with pytest.raises(ValueError, match=message):
        load_manual_certificate("example.com", policy)


@pytest.mark.parametrize("failure", ["missing", "invalid", "mismatch", "domain"])
def test_manual_certificate_rejects_bad_files(tmp_path, failure):
    raw = _manual_policy(tmp_path, sans=["other.com"] if failure == "domain" else None)
    if failure == "missing":
        raw["key_path"] = str(tmp_path / "missing.key")
    elif failure == "invalid":
        (tmp_path / "example.pem").write_text("INVALID PEM", encoding="utf-8")
    elif failure == "mismatch":
        key = ec.generate_private_key(ec.SECP256R1())
        (tmp_path / "example.key").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    with pytest.raises(ValueError):
        load_manual_certificate("example.com", CertificatePolicyConfig(**raw))


@pytest.mark.parametrize("operation", ["issue", "renew", "automatic"])
def test_manual_source_skips_all_issuance_before_acme(monkeypatch, tmp_path, operation):
    cfg = _config({"example.com": _manual_policy(tmp_path)})
    monkeypatch.setattr(renew_flow, "load_raw_config", lambda: cfg)
    monkeypatch.setattr(renew_flow, "resolve_renew_domain", Mock(side_effect=AssertionError("不得查询 ACME")))
    monkeypatch.setattr(renew_flow.notifier, "DingDingNotifier", Mock(side_effect=AssertionError("不得通知失败")))
    if operation == "issue":
        call = renew_flow.issue_and_plan_deploy("example.com", ["example.com"])
    else:
        call = renew_flow.renew_and_plan_deploy("example.com", force=True, automatic=operation == "automatic")
    result = asyncio.run(call)
    assert result["reason"] == "manual_certificate"


@pytest.mark.parametrize("resolved", [False, True])
def test_disabled_auto_renew_skips_before_renewal_side_effects(monkeypatch, resolved):
    cfg = _config({"example.com": {"auto_renew": False}})
    monkeypatch.setattr(renew_flow, "load_raw_config", lambda: cfg)
    resolve = AsyncMock(return_value="example.com")
    monkeypatch.setattr(renew_flow, "resolve_renew_domain", resolve)
    monkeypatch.setattr(renew_flow.store, "RenewStateStore", Mock(side_effect=AssertionError("不得记录续签失败")))
    result = asyncio.run(renew_flow.renew_and_plan_deploy("www.example.com" if resolved else "example.com", force=True, automatic=True))
    assert result["reason"] == "auto_renew_disabled"
    assert resolve.await_count == int(resolved)


def _mock_deploy_flow(monkeypatch, cfg):
    monkeypatch.setattr(renew_flow, "load_raw_config", lambda: cfg)
    monkeypatch.setattr(renew, "load_raw_config", lambda: cfg)
    notification = Mock(notify_deploy_summary=AsyncMock(), notify_renew_result=AsyncMock(), notify_issue_result=AsyncMock())
    monkeypatch.setattr(renew_flow.notifier, "DingDingNotifier", lambda: notification)
    state = Mock()
    state.get.return_value = None
    monkeypatch.setattr(renew_flow.store, "RenewStateStore", lambda: state)
    deploy = Mock(apply_deploy=AsyncMock(return_value={}), plan_dry_run=AsyncMock(return_value={}))
    monkeypatch.setattr(renew_flow, "DeployService", lambda: deploy)
    return deploy


@pytest.mark.parametrize("mode", ["apply", "dry-run"])
def test_manual_deploy_without_acme_or_dns(monkeypatch, tmp_path, mode):
    cfg = _config({"example.com": _manual_policy(tmp_path)}, deploy={"mode": mode}, providers=[{"name": "static", "type": "custom"}])
    deploy = _mock_deploy_flow(monkeypatch, cfg)
    monkeypatch.setattr(renew_flow, "resolve_renew_domain", Mock(side_effect=AssertionError("不得解析 ACME")))
    monkeypatch.setattr(renew, "AcmeShRenewer", Mock(side_effect=AssertionError("不得创建 ACME")))
    result = asyncio.run(renew_flow.deploy_existing_only("example.com", "static"))
    assert result["renew_status"] == "deploy_only"
    call = deploy.apply_deploy if mode == "apply" else deploy.plan_dry_run
    assert call.await_count == 1
    assert call.call_args.kwargs["provider_name"] is None
    assert call.call_args.kwargs["renewed_cert"].domain == "example.com"


def test_manual_bad_file_fails_without_deploy_or_acme(monkeypatch, tmp_path):
    raw = _manual_policy(tmp_path)
    raw["key_path"] = str(tmp_path / "missing.key")
    cfg = _config({"example.com": raw})
    deploy = _mock_deploy_flow(monkeypatch, cfg)
    monkeypatch.setattr(renew, "AcmeShRenewer", Mock(side_effect=AssertionError("不得回退 ACME")))
    with pytest.raises(ValueError, match="读取手动证书失败"):
        asyncio.run(renew_flow.deploy_existing_only("example.com"))
    deploy.apply_deploy.assert_not_awaited()
    deploy.plan_dry_run.assert_not_awaited()


@pytest.mark.parametrize("operation", ["issue", "renew", "deploy"])
def test_auto_disabled_still_allows_manual_operations(monkeypatch, tmp_path, operation):
    cfg = _config({"example.com": {"auto_renew": False}}, deploy={"mode": "dry-run"})
    deploy = _mock_deploy_flow(monkeypatch, cfg)
    cert = load_manual_certificate("example.com", CertificatePolicyConfig(**_manual_policy(tmp_path)))
    monkeypatch.setattr(renew_flow, "resolve_renew_domain", AsyncMock(return_value="example.com"))
    for name in ["issue_certificate", "renew_certificate", "load_existing_certificate"]:
        monkeypatch.setattr(renew_flow, name, AsyncMock(return_value=cert))
    calls = {
        "issue": lambda: renew_flow.issue_and_plan_deploy("example.com", ["example.com"]),
        "renew": lambda: renew_flow.renew_and_plan_deploy("example.com", force=True),
        "deploy": lambda: renew_flow.deploy_existing_only("example.com"),
    }
    asyncio.run(calls[operation]())
    deploy.plan_dry_run.assert_awaited_once()


@pytest.mark.parametrize("operation", ["issue", "renew", "deploy"])
def test_blacklist_remains_higher_priority(monkeypatch, tmp_path, operation):
    cfg = _config({"example.com": _manual_policy(tmp_path)}, blacklist=["example.com"])
    monkeypatch.setattr(renew_flow, "load_raw_config", lambda: cfg)
    monkeypatch.setattr(renew_flow.notifier, "DingDingNotifier", Mock(side_effect=AssertionError("不得触发操作")))
    calls = {
        "issue": lambda: renew_flow.issue_and_plan_deploy("example.com", ["example.com"]),
        "renew": lambda: renew_flow.renew_and_plan_deploy("example.com", automatic=True),
        "deploy": lambda: renew_flow.deploy_existing_only("example.com"),
    }
    assert asyncio.run(calls[operation]())["reason"] == "blacklist"


def test_collector_skips_policies_but_processes_other_domains(monkeypatch, tmp_path):
    cfg = _config({"example.com": {"auto_renew": False}, "manual.com": _manual_policy(tmp_path)})
    monkeypatch.setattr(collector, "load_raw_config", lambda: cfg)
    execute = AsyncMock(return_value={"renew_status": "success"})
    monkeypatch.setattr(collector, "renew_and_plan_deploy", execute)
    asyncio.run(collector._auto_renew_and_deploy_expiring_certificates([
        {"domain": domain, "provider": "test", "days": 1}
        for domain in ["example.com", "manual.com", "other.com"]
    ]))
    execute.assert_awaited_once()
    assert execute.call_args.kwargs["domain"] == "other.com"


def test_ui_keeps_manual_deploy_entry_without_probe_and_uses_current_policy(tmp_path):
    from app.ui.pages import _certificate_rows

    cfg = _config({"example.com": _manual_policy(tmp_path), "other.com": {"auto_renew": False}})
    snapshot = [{"domain": "other.com", "days": 10}]
    rows = {row["domain"]: row for row in _certificate_rows(cfg, snapshot)}
    assert rows["example.com"]["certificate_source"] == "manual"
    assert rows["example.com"]["exp"] == "未探测"
    assert "已关闭" in rows["other.com"]["certificate_policy_label"]
    assert "certificate_source" not in snapshot[0]
    assert len(_certificate_rows(cfg, list(rows.values()))) == 2


def test_config_save_and_reload_preserves_policy(monkeypatch, tmp_path):
    from app import config

    path = tmp_path / "config.yaml"
    path.write_text("acme: {}\n", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    monkeypatch.setattr(config, "_CONFIG_CACHE", None)
    raw = _manual_policy(tmp_path)
    config.update_config_atomic({"acme": {}, "certificate_policies": {"Example.com.": raw, "other.com": {"auto_renew": False}}})
    reloaded = config.get_config(force_reload=True)
    assert get_certificate_policy(reloaded, "example.com").fullchain_path == raw["fullchain_path"]
    assert get_certificate_policy(reloaded, "example.com").auto_renew is False
    assert get_certificate_policy(reloaded, "other.com").auto_renew is False


def test_manual_certificate_reads_new_files_on_each_deploy(tmp_path):
    policy = CertificatePolicyConfig(**_manual_policy(tmp_path))
    first = load_manual_certificate("example.com", policy)
    _manual_policy(tmp_path)
    second = load_manual_certificate("example.com", policy)
    assert first.cert_pem != second.cert_pem
    assert first.key_pem != second.key_pem


def test_policy_dns_credentials_are_used_for_renewal(monkeypatch):
    cfg = _config({"example.com": {"dns_provider": "dns"}}, providers=[
        {"name": "dns", "type": "aliyun", "credentials": {"access_key_id": "test-key", "access_key_secret": "test-secret"}},
    ])
    client = AcmeShRenewer.__new__(AcmeShRenewer)
    client._config = cfg
    client._acme_sh = "acme.sh"
    client._store = Mock()
    client._store.resolve.return_value = "static"
    run = Mock(return_value="Le_Domain=example.com\nLe_Webroot=dns_ali\n")
    monkeypatch.setattr(client, "_run_cmd", run)
    monkeypatch.setattr(client, "_build_renewed_cert_from_info", lambda *args: "renewed")
    assert client.renew("www.example.com", "static", main_domain="example.com") == "renewed"
    assert run.call_args_list[0].kwargs["env"]["Ali_Key"] == "test-key"
    assert "--renew" in run.call_args_list[1].args[0]


def test_ui_renders_manual_source_with_only_deploy_action(monkeypatch, tmp_path):
    from app.ui import pages

    cfg = _config({"example.com": _manual_policy(tmp_path)})
    ui = Mock()
    elements = []
    slots = {}

    def element(*args, **kwargs):
        item = MagicMock()
        for method in ["classes", "props", "style", "tooltip"]:
            getattr(item, method).return_value = item
        item.__enter__.return_value = item
        item.add_slot.side_effect = lambda name, value="": slots.update({name: value}) or item
        elements.append((args, item))
        return item

    for name in ["label", "table", "column", "row", "expansion", "element", "button", "separator", "icon"]:
        getattr(ui, name).side_effect = element
    monkeypatch.setattr(pages, "ui", ui)
    monkeypatch.setattr(pages, "get_config", lambda: cfg)
    monkeypatch.setattr(pages.state, "CERT_LIST", [])
    monkeypatch.setattr(pages.state, "DOMAIN_GROUPS", [{
        "domain": "example.com", "name": "static", "expires_at": "unknown",
        "subs": [{"name": "www.example.com", "status": "在工作"}],
    }])
    pages.render_certificates()
    assert ui.table.call_args.kwargs["rows"][0]["certificate_source"] == "manual"
    assert "v-if=\"props.row.certificate_source !== 'manual'\"" in slots["body-cell-action"]
    assert 'label="仅部署"' in slots["body-cell-action"]
    pages.render_domains()
    issue_buttons = [item for args, item in elements if args and args[0] == "签发"]
    assert len(issue_buttons) == 2
    for button in issue_buttons:
        button.set_visibility.assert_called_once_with(False)
