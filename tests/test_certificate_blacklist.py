"""验证黑名单入口拦截、配置校验和自动续签反馈。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
import yaml
from box import Box
from pydantic import ValidationError

from app.config import _dedupe_config_lists
from app.schemas.config import AppConfig
from app.services import collector, renew_flow
from app.utils.domain_match import find_blacklisted_domain


@pytest.mark.parametrize(
    ("domains", "expected"),
    [
        ([" EXAMPLE.COM. "], "example.com"),
        (["*.Example.com."], "*.example.com"),
        (["api.example.com"], "*.example.com"),
        ([" PC.EXAMPLE.COM. "], "*.example.com"),
        (["deep.api.example.com"], None),
        (["notexample.com", "example.com.evil"], None),
    ],
)
def test_blacklist_exact_boundaries(domains, expected):
    assert find_blacklisted_domain(domains, ["example.com", "*.example.com"]) == expected


def test_blacklist_config_defaults_normalization_and_dedupe():
    assert AppConfig(acme={}).blacklist == []
    data = {"blacklist": [" EXAMPLE.COM. ", "example.com", "*.Example.com."]}
    assert _dedupe_config_lists(data)["blacklist"] == ["example.com", "*.example.com"]
    assert AppConfig(acme={}, **data).blacklist == ["example.com", "*.example.com"]


def test_inherited_blacklist_can_be_cleared_by_hot_config_update(monkeypatch, tmp_path):
    from app import config

    parent = tmp_path / "base.yaml"
    child = tmp_path / "config.yaml"
    parent.write_text(yaml.safe_dump({"acme": {}, "log_level": "info", "blacklist": [" EXAMPLE.COM. "]}), encoding="utf-8")
    child.write_text("extends: base.yaml\n", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", child)
    monkeypatch.setattr(config, "_CONFIG_CACHE", None)
    assert list(config.get_config().blacklist) == ["example.com"]
    assert list(config.update_config_atomic({"extends": "base.yaml", "blacklist": []}).blacklist) == []
    assert yaml.safe_load(child.read_text(encoding="utf-8")) == {"extends": "base.yaml", "blacklist": []}


@pytest.mark.parametrize("value", [None, "example.com", [None], [123], [""], [" "], ["."], ["example..com"], ["https://example.com"], ["foo.*.example.com"], ["-bad.com"], ["a" * 64 + ".com"]])
def test_blacklist_rejects_invalid_config(value):
    with pytest.raises(ValidationError):
        AppConfig(acme={}, blacklist=value)


@pytest.mark.parametrize("operation", ["issue", "renew", "deploy"])
@pytest.mark.parametrize("mode", ["dry-run", "apply"])
def test_blacklist_blocks_before_any_side_effect(monkeypatch, operation, mode):
    config = Box({"blacklist": ["example.com"], "deploy": {"mode": mode}})
    monkeypatch.setattr(renew_flow, "load_raw_config", lambda: config)

    def unexpected(*args, **kwargs):
        pytest.fail("黑名单命中后不得创建通知器、状态存储或执行 ACME、部署、域名解析")

    for name in ["issue_certificate", "renew_certificate", "load_existing_certificate", "DeployService", "resolve_renew_domain"]:
        monkeypatch.setattr(renew_flow, name, unexpected)
    monkeypatch.setattr(renew_flow.store, "RenewStateStore", unexpected)
    monkeypatch.setattr(renew_flow.notifier, "DingDingNotifier", unexpected)

    if operation == "issue":
        call = renew_flow.issue_and_plan_deploy("example.com", ["example.com"])
        key = "issue_status"
    elif operation == "renew":
        call = renew_flow.renew_and_plan_deploy(" EXAMPLE.COM. ", force=True)
        key = "renew_status"
    else:
        call = renew_flow.deploy_existing_only("example.com")
        key = "renew_status"
    result = asyncio.run(call)
    assert result[key] == "skipped"
    assert result["mode"] == mode
    assert result["reason"] == "blacklist"
    assert result["matched_domain"] == "example.com"
    assert result["retry_count"] == 0
    assert "黑名单" in result["message"]


def test_issue_san_hit_skips_entire_request(monkeypatch):
    monkeypatch.setattr(renew_flow, "load_raw_config", lambda: Box({"blacklist": ["*.example.com"], "deploy": {"mode": "apply"}}))
    monkeypatch.setattr(renew_flow.notifier, "DingDingNotifier", lambda: pytest.fail("不得初始化通知器"))
    result = asyncio.run(renew_flow.issue_and_plan_deploy("example.com", ["example.com", "*.example.com"]))
    assert result["issue_status"] == "skipped"
    assert result["matched_domain"] == "*.example.com"
    assert result["domains"] == ["example.com", "*.example.com"]


@pytest.mark.parametrize("operation", ["renew", "deploy"])
def test_resolved_main_domain_is_also_blocked(monkeypatch, operation):
    monkeypatch.setattr(renew_flow, "load_raw_config", lambda: Box({"blacklist": ["example.com"], "deploy": {"mode": "apply"}}))
    monkeypatch.setattr(renew_flow, "resolve_renew_domain", AsyncMock(return_value="example.com"))
    monkeypatch.setattr(renew_flow.notifier, "DingDingNotifier", lambda: pytest.fail("不得初始化通知器"))
    call = renew_flow.renew_and_plan_deploy("api.example.com", force=True) if operation == "renew" else renew_flow.deploy_existing_only("api.example.com")
    result = asyncio.run(call)
    assert result["renew_status"] == "skipped"
    assert result["matched_domain"] == "example.com"


def test_auto_renew_logs_skip_and_continues_other_certificates(monkeypatch):
    config = Box({"auto_renew_enabled": True, "auto_renew_days": 20})
    monkeypatch.setattr(collector, "load_raw_config", lambda: config)
    logger = Mock()
    monkeypatch.setattr(collector, "logger", logger)
    called = []

    async def renew(domain, **kwargs):
        called.append(domain)
        if domain == "example.com":
            return {"renew_status": "skipped", "reason": "blacklist", "matched_domain": domain}
        return {"renew_status": "success", "mode": "apply"}

    monkeypatch.setattr(collector, "renew_and_plan_deploy", renew)
    certs = [{"domain": name, "provider": "test", "days": 5} for name in ["example.com", "other.com"]]
    asyncio.run(collector._auto_renew_and_deploy_expiring_certificates(certs))
    assert called == ["example.com", "other.com"]
    skips = [c for c in logger.info.call_args_list if "编排跳过" in c.args[0]]
    completes = [c for c in logger.info.call_args_list if "编排完成" in c.args[0]]
    assert len(skips) == len(completes) == 1
    assert skips[0].args[1:] == ("example.com", "blacklist", "example.com")
    assert completes[0].args[1] == "other.com"


@pytest.mark.parametrize("operation", ["renew", "deploy", "issue"])
def test_ui_blacklist_skip_shows_reason_without_success(monkeypatch, operation):
    from app.ui import pages

    events = {}
    buttons = {}
    client = MagicMock()
    ui = Mock()
    ui.context = SimpleNamespace(client=client)

    def element(*args, **kwargs):
        item = MagicMock()
        for name in ["classes", "props", "style", "tooltip"]:
            getattr(item, name).return_value = item
        item.__enter__.return_value = item
        item.on.side_effect = lambda name, callback: events.update({name: callback})
        item.add_slot.return_value = item
        if args and "on_click" in kwargs:
            buttons[args[0]] = kwargs["on_click"]
        item.value = kwargs.get("value", "")
        return item

    for name in ["label", "table", "column", "row", "expansion", "element", "button", "dialog", "card", "textarea", "separator", "icon"]:
        getattr(ui, name).side_effect = element
    monkeypatch.setattr(pages, "ui", ui)
    monkeypatch.setattr(pages, "_notify_safe", lambda client, message, **kwargs: ui.notify(message, **kwargs))
    monkeypatch.setattr(pages, "get_config", lambda: Box({"deploy": {"mode": "apply"}, "staging": {"enabled": False}, "alert": {"cert": {"cert_warn_days": 15, "cert_expiry_days": 28}, "domain": {"domain_warn_days": 15, "domain_expiry_days": 28}}}))
    monkeypatch.setattr(pages.state, "CERT_LIST", [])
    message = "命中黑名单 example.com，本次操作已跳过"

    async def skipped(*args, **kwargs):
        return {"renew_status": "skipped", "issue_status": "skipped", "reason": "blacklist", "message": message}

    for name in ["renew_and_plan_deploy", "deploy_existing_only", "issue_and_plan_deploy"]:
        monkeypatch.setattr(pages, name, skipped)
    if operation == "issue":
        monkeypatch.setattr(pages.state, "DOMAIN_GROUPS", [{"domain": "example.com", "name": "test", "expires_at": "unknown", "subs": []}])
        pages.render_domains()
        buttons["签发"]()
        asyncio.run(buttons["确认签发"]())
    else:
        pages.render_certificates()
        event = "manual-renew-force" if operation == "renew" else "manual-deploy"
        asyncio.run(events[event](SimpleNamespace(args="example.com")))
    assert ui.notify.call_args.args[0] == message
    assert ui.notify.call_args.kwargs["type"] == "info"
    assert not any(call.kwargs.get("type") == "positive" for call in ui.notify.call_args_list)
