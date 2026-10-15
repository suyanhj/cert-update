"""验证原始目标只匹配一次，并固定续签、状态和部署所用证书。"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from box import Box

from app.schemas.acme import AcmeShConfigError, AcmeShExecutionError, RenewedCert
from app.services import collector, renew, renew_flow
from app.utils import store
from app.utils.acme_sh import AcmeShRenewer


PARENT = ("hj.com", {"hj.com", "*.hj.com"})
CHILD = ("xx.hj.com", {"xx.hj.com", "*.xx.hj.com"})


@pytest.mark.parametrize(
    ("entries", "target", "expected"),
    [
        ([PARENT, CHILD], "api.xx.hj.com", "xx.hj.com"),
        ([PARENT, CHILD], "XX.HJ.COM.", "xx.hj.com"),
        ([PARENT, CHILD, ("api.xx.hj.com", {"api.xx.hj.com"})], "api.xx.hj.com", "api.xx.hj.com"),
        ([CHILD, ("other.com", {"other.com", "api.xx.hj.com"})], "api.xx.hj.com", "other.com"),
        ([PARENT], "api.xx.hj.com", None),
        ([PARENT], "*.xx.hj.com", None),
    ],
)
def test_certificate_selection_priority(entries, target, expected):
    assert AcmeShRenewer._pick_acme_main_domain(entries, target) == expected


@pytest.fixture
def flow_env(monkeypatch):
    """使用真实匹配和命令构造，隔离外部 ACME、云部署与持久化。"""
    config = Box(
        {
            "deploy": {"mode": "apply", "max_renew_retries": 3, "renew_cooldown_days": 7},
            "staging": {"enabled": False},
            "acme": {"dns_sleep_seconds": 0},
            "blacklist": [],
            "auto_renew_enabled": True,
            "auto_renew_days": 20,
        },
        default_box=True,
    )
    env = SimpleNamespace(
        config=config,
        entries=[PARENT, CHILD],
        commands=[],
        provider_domains=[],
        records=[],
        queried_states=[],
        states={},
        renew_failures=0,
        deploy_error=None,
        certificates=[],
    )
    client = AcmeShRenewer.__new__(AcmeShRenewer)
    client._config = config
    client._acme_sh = "acme.sh"

    def resolve_provider(domain, provider_name=None):
        env.provider_domains.append(domain)
        return provider_name or "dns-test"

    def run_cmd(command, env=None):
        flow_env_state.commands.append(command)
        if "--list" in command:
            rows = ["Main_Domain KeyLength SAN_Domains CA Created Renew"]
            for main, names in flow_env_state.entries:
                sans = ",".join(sorted(names - {main})) or "no"
                rows.append(f'{main} "ec-256" {sans} LetsEncrypt.org 2026-01-01T00:00:00Z 2026-03-01T00:00:00Z')
            return "\n".join(rows)
        if "--renew" in command and flow_env_state.renew_failures:
            flow_env_state.renew_failures -= 1
            # 列表变化后重试仍必须使用本轮已经选中的子域证书。
            flow_env_state.entries = [PARENT]
            raise AcmeShExecutionError("临时 DNS 失败")
        return "Le_Domain='xx.hj.com'\nLe_Webroot='dns_cf'"

    flow_env_state = env

    def build_certificate(domain, info):
        now = datetime.now(timezone.utc)
        cert = RenewedCert(
            domain=domain, cert_path="cert.pem", key_path="key.pem", fullchain_path="fullchain.pem",
            ca_path=None, cert_pem="CERT", key_pem="KEY", fullchain_pem="CERT", ca_pem="",
            issued_at=now, expires_at=now + timedelta(days=90), key_length="ec-256",
            sans=[f"*.{domain}"], issuer="测试签发者", profile=None,
        )
        env.certificates.append(cert)
        return cert

    monkeypatch.setattr(client, "_resolve_provider_name", resolve_provider)
    monkeypatch.setattr(client, "_resolve_dns_plugin", lambda provider: "dns_cf")
    monkeypatch.setattr(client, "_build_provider_env", lambda provider: {})
    monkeypatch.setattr(client, "_run_cmd", run_cmd)
    monkeypatch.setattr(client, "_build_renewed_cert_from_info", build_certificate)
    monkeypatch.setattr(renew, "AcmeShRenewer", lambda: client)
    monkeypatch.setattr(renew_flow, "load_raw_config", lambda: config)
    monkeypatch.setattr(collector, "load_raw_config", lambda: config)
    monkeypatch.setattr(
        store.CollectorSnapshotStore, "get_all",
        lambda self: pytest.fail("续签不得通过采集快照改写目标"),
    )

    class StateStore:
        def get(self, domain):
            env.queried_states.append(domain)
            return env.states.get(domain)

        def record_success(self, **kwargs):
            env.records.append(("success", kwargs))

        def record_failure(self, **kwargs):
            env.records.append(("failed", kwargs))

        def record_deploy_failure(self, **kwargs):
            env.records.append(("deploy_failed", kwargs))

    async def deploy(renewed_cert, provider_name=None):
        if env.deploy_error:
            raise RuntimeError(env.deploy_error)
        return {"mode": "apply", "summary": {}, "domain": renewed_cert.domain}

    env.deploy = SimpleNamespace(apply_deploy=AsyncMock(side_effect=deploy), plan_dry_run=AsyncMock(side_effect=deploy))
    env.notifier = SimpleNamespace(notify_renew_result=AsyncMock(), notify_deploy_summary=AsyncMock())
    monkeypatch.setattr(renew_flow.store, "RenewStateStore", StateStore)
    monkeypatch.setattr(renew_flow, "DeployService", lambda: env.deploy)
    monkeypatch.setattr(renew_flow.notifier, "DingDingNotifier", lambda: env.notifier)
    env.client = client
    return env


@pytest.mark.parametrize("force", [False, True])
def test_renew_flow_uses_child_main_domain_once(flow_env, force):
    result = asyncio.run(renew_flow.renew_and_plan_deploy("api.xx.hj.com", force=force))
    assert result["renew_status"] == "success"
    assert flow_env.commands == [
        ["acme.sh", "--list"],
        ["acme.sh", "--info", "-d", "xx.hj.com"],
        ["acme.sh", "--renew", "-d", "xx.hj.com"] + (["--force"] if force else []),
        ["acme.sh", "--info", "-d", "xx.hj.com"],
    ]
    assert flow_env.provider_domains == ["xx.hj.com"]
    assert flow_env.queried_states == ["xx.hj.com"]
    assert flow_env.records[0][1]["domain"] == "xx.hj.com"
    assert flow_env.notifier.notify_renew_result.call_args.kwargs["domain"] == "xx.hj.com"
    assert flow_env.deploy.apply_deploy.call_args.kwargs["renewed_cert"].domain == "xx.hj.com"


def test_direct_renew_keeps_original_target(flow_env):
    cert = asyncio.run(renew.renew_certificate("api.xx.hj.com"))
    assert cert.domain == "xx.hj.com"
    assert flow_env.commands[2] == ["acme.sh", "--renew", "-d", "xx.hj.com"]
    assert len([cmd for cmd in flow_env.commands if "--list" in cmd]) == 1


def test_missing_child_cert_stops_before_renew_and_deploy(flow_env):
    flow_env.entries = [PARENT]
    result = asyncio.run(renew_flow.renew_and_plan_deploy("api.xx.hj.com", force=True))
    assert result["renew_status"] == "failed"
    assert result["retry_count"] == 0
    assert "api.xx.hj.com" in result["error"]
    assert flow_env.commands == [["acme.sh", "--list"]]
    assert flow_env.queried_states == []
    assert flow_env.records[0][1]["domain"] == "api.xx.hj.com"
    flow_env.deploy.apply_deploy.assert_not_called()


def test_direct_missing_cert_raises(flow_env):
    flow_env.entries = [PARENT]
    with pytest.raises(AcmeShConfigError, match="api.xx.hj.com"):
        asyncio.run(renew.renew_certificate("api.xx.hj.com"))
    assert flow_env.commands == [["acme.sh", "--list"]]


def test_retries_keep_selected_cert_when_list_changes(flow_env):
    flow_env.renew_failures = 1
    result = asyncio.run(renew_flow.renew_and_plan_deploy("api.xx.hj.com", force=True))
    assert result["renew_status"] == "success"
    assert result["retry_count"] == 2
    assert len([cmd for cmd in flow_env.commands if "--list" in cmd]) == 1
    assert [cmd for cmd in flow_env.commands if "--renew" in cmd] == [
        ["acme.sh", "--renew", "-d", "xx.hj.com", "--force"],
        ["acme.sh", "--renew", "-d", "xx.hj.com", "--force"],
    ]
    assert [status for status, record in flow_env.records] == ["failed", "success"]
    assert all(record["domain"] == "xx.hj.com" for status, record in flow_env.records)


def test_cooldown_uses_selected_certificate_state(flow_env):
    flow_env.states["xx.hj.com"] = {
        "last_status": "success", "last_success_at": datetime.now(timezone.utc).isoformat(),
    }
    result = asyncio.run(renew_flow.renew_and_plan_deploy("web.xx.hj.com"))
    assert result["renew_status"] == "skipped"
    assert flow_env.queried_states == ["xx.hj.com"]
    assert flow_env.commands == [["acme.sh", "--list"]]


def test_deploy_failure_and_manual_recovery_use_same_cert(flow_env):
    flow_env.deploy_error = "部署失败"
    result = asyncio.run(renew_flow.renew_and_plan_deploy("api.xx.hj.com", force=True))
    assert result["renew_status"] == "deploy_failed"
    assert flow_env.records[-1][0] == "deploy_failed"
    assert flow_env.records[-1][1]["domain"] == "xx.hj.com"
    flow_env.deploy_error = None
    flow_env.commands.clear()
    result = asyncio.run(renew_flow.deploy_existing_only("api.xx.hj.com"))
    assert result["renew_status"] == "deploy_only"
    assert flow_env.commands == [["acme.sh", "--list"], ["acme.sh", "--info", "-d", "xx.hj.com"]]
    assert flow_env.records[-1][0] == "success"
    assert flow_env.records[-1][1]["domain"] == "xx.hj.com"
    assert flow_env.notifier.notify_deploy_summary.call_args.kwargs["domain"] == "xx.hj.com"


def test_manual_deploy_missing_cert_notifies_and_raises(flow_env):
    flow_env.entries = [PARENT]
    with pytest.raises(AcmeShConfigError, match="api.xx.hj.com"):
        asyncio.run(renew_flow.deploy_existing_only("api.xx.hj.com"))
    assert flow_env.commands == [["acme.sh", "--list"]]
    flow_env.deploy.apply_deploy.assert_not_called()
    assert flow_env.notifier.notify_deploy_summary.call_args.kwargs["success"] is False


def test_auto_renew_continues_after_missing_child_cert(flow_env):
    flow_env.entries = [PARENT]
    certs = [{"domain": domain, "provider": "dns-test", "days": 5} for domain in ["api.xx.hj.com", "hj.com"]]
    asyncio.run(collector._auto_renew_and_deploy_expiring_certificates(certs))
    assert [status for status, record in flow_env.records] == ["failed", "success"]
    assert [cmd for cmd in flow_env.commands if "--renew" in cmd] == [["acme.sh", "--renew", "-d", "hj.com", "--force"]]
    assert flow_env.deploy.apply_deploy.call_count == 1


def test_resolved_blacklisted_cert_stops_before_commands(flow_env):
    flow_env.config.blacklist = ["xx.hj.com"]
    result = asyncio.run(renew_flow.renew_and_plan_deploy("api.xx.hj.com", force=True))
    assert result["renew_status"] == "skipped"
    assert result["reason"] == "blacklist"
    assert flow_env.commands == [["acme.sh", "--list"]]
    assert flow_env.records == []
    flow_env.notifier.notify_renew_result.assert_not_called()


def test_self_signed_resolution_keeps_normalized_original_target(flow_env):
    flow_env.config.staging = {"enabled": True, "use_self_signed": True}
    assert asyncio.run(renew.resolve_renew_domain(" API.XX.HJ.COM. ")) == "api.xx.hj.com"
    assert flow_env.commands == []
