"""复现通配黑名单未能在证书解析前拦截子域名的流程问题。"""

import asyncio
from unittest.mock import Mock

import pytest
from box import Box

from app.services import collector, renew_flow
from app.utils.domain_match import find_blacklisted_domain


BLACKLIST = ["*.test2.megao.cn", "*.test2.meiquick.com", "*.test.megaovip.com"]


@pytest.mark.parametrize(
    ("domain", "rule"),
    [
        ("pc.test2.megao.cn", BLACKLIST[0]),
        ("pc.test2.meiquick.com", BLACKLIST[1]),
        ("pc.test.megaovip.com", BLACKLIST[2]),
    ],
)
@pytest.mark.parametrize("operation", ["manual_renew", "auto_renew", "deploy", "issue"])
def test_wildcard_blacklist_blocks_real_flow_before_acme(monkeypatch, domain, rule, operation):
    config = Box({
        "blacklist": BLACKLIST,
        "deploy": {"mode": "apply"},
        "auto_renew_enabled": True,
        "auto_renew_days": 20,
    })
    monkeypatch.setattr(renew_flow, "load_raw_config", lambda: config)
    monkeypatch.setattr(collector, "load_raw_config", lambda: config)

    def unexpected(*args, **kwargs):
        pytest.fail("黑名单命中后不应解析证书、签发、续签、部署、写状态或发告警")

    # 保留真实公共编排和自动循环，只将不应到达的外部操作设为失败哨兵。
    for name in ["resolve_renew_domain", "renew_certificate", "issue_certificate", "load_existing_certificate", "DeployService"]:
        monkeypatch.setattr(renew_flow, name, unexpected)
    monkeypatch.setattr(renew_flow.store, "RenewStateStore", unexpected)
    monkeypatch.setattr(renew_flow.notifier, "DingDingNotifier", unexpected)
    if operation == "auto_renew":
        logger = Mock()
        monkeypatch.setattr(collector, "logger", logger)
        asyncio.run(collector._auto_renew_and_deploy_expiring_certificates([
            {"domain": domain, "provider": "test", "days": 5},
        ]))
        skipped = [call for call in logger.info.call_args_list if "编排跳过" in call.args[0]]
        assert len(skipped) == 1
        assert skipped[0].args[1:] == (domain, "blacklist", rule)
        logger.exception.assert_not_called()
        logger.warning.assert_not_called()
        return
    if operation == "manual_renew":
        call = renew_flow.renew_and_plan_deploy(domain, force=True)
        status = "renew_status"
    elif operation == "deploy":
        call = renew_flow.deploy_existing_only(domain)
        status = "renew_status"
    else:
        call = renew_flow.issue_and_plan_deploy(domain, [domain])
        status = "issue_status"
    result = asyncio.run(call)
    assert result[status] == "skipped"
    assert result["reason"] == "blacklist"
    assert result["matched_domain"] == rule
    assert result["retry_count"] == 0


@pytest.mark.parametrize("domain", ["example.com", "pc.example.com", "deep.pc.example.com"])
def test_plain_blacklist_has_no_wildcard_expansion(domain):
    assert find_blacklisted_domain([domain], ["pc.example.com"]) == (
        "pc.example.com" if domain == "pc.example.com" else None
    )


@pytest.mark.parametrize("domain", ["test2.megao.cn", "deep.pc.test2.megao.cn", "pc.test2.megao.cn.evil", "pc.nottest2.megao.cn"])
def test_wildcard_blacklist_does_not_match_other_boundaries(domain):
    assert find_blacklisted_domain([domain], BLACKLIST) is None
