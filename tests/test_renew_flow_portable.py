"""Offline tests for renew_flow orchestration. No ACME/cloud/network calls."""
import asyncio
from types import SimpleNamespace

from box import Box

import pytest

from app.services import renew_flow


def run(coro):
    return asyncio.run(coro)


def config(*, mode="dry-run", blacklist=None, cooldown=7, retries=3, interval=0, providers=None):
    return Box({
        "blacklist": blacklist or [],
        "providers": providers or [],
        "deploy": {
            "mode": mode,
            "renew_cooldown_days": cooldown,
            "max_renew_retries": retries,
            "renew_retry_interval_seconds": interval,
        },
    })


class FakeNotifier:
    issue_calls = []
    renew_calls = []
    deploy_calls = []

    def __init__(self):
        pass

    async def notify_issue_result(self, **kwargs):
        self.issue_calls.append(kwargs)

    async def notify_renew_result(self, **kwargs):
        self.renew_calls.append(kwargs)

    async def notify_deploy_summary(self, **kwargs):
        self.deploy_calls.append(kwargs)


class FakeDeploy:
    result = {"bind_results": [{"status": "success", "provider": "p1", "provider_type": "tencent", "product_type": "lb"}]}

    async def apply_deploy(self, **kwargs):
        return dict(self.result)

    async def plan_dry_run(self, **kwargs):
        return {"planned_bind_actions": [{"id": "x"}], "nginx_target_count": 1}


class FakeState:
    def __init__(self, state=None):
        self.state = state
        self.failures = []
        self.successes = []
        self.deploy_failures = []

    def get(self, domain):
        return self.state

    def record_failure(self, **kwargs):
        self.failures.append(kwargs)

    def record_success(self, **kwargs):
        self.successes.append(kwargs)

    def record_deploy_failure(self, **kwargs):
        self.deploy_failures.append(kwargs)


@pytest.fixture(autouse=True)
def reset_fakes():
    FakeNotifier.issue_calls = []
    FakeNotifier.renew_calls = []
    FakeNotifier.deploy_calls = []


def test_format_helpers_cover_provider_and_dry_run_shapes():
    assert renew_flow.format_provider_type_summary(None) == "无命中"
    assert renew_flow.format_provider_type_summary({"by_provider_type": {"tencent": {"success": 2, "failed": 1}}}) == "tencent:成功2/失败1"
    assert renew_flow._vendor_label("aliyun") == "阿里云"
    assert renew_flow._vendor_label("unknown") == "unknown"
    assert renew_flow._product_label("clb") == "elb"
    assert "部署预演完成" in renew_flow.format_deploy_notify_message(
        {"mode": "dry-run", "planned_bind_actions": [1], "nginx_target_count": 2}, "example.com"
    )


def test_blacklist_and_policy_skip_return_consistent_payload(monkeypatch):
    cfg = config(blacklist=["blocked.example.com"])
    monkeypatch.setattr(renew_flow, "load_raw_config", lambda: cfg)
    result = run(renew_flow.renew_and_plan_deploy("blocked.example.com"))
    assert result["renew_status"] == "skipped"
    assert result["reason"] == "blacklist"
    monkeypatch.setattr(renew_flow, "load_raw_config", lambda: config())
    monkeypatch.setattr(
        renew_flow,
        "certificate_operation_skip",
        lambda *args, **kwargs: ("policy", "policy skipped"),
    )
    result = run(renew_flow.renew_and_plan_deploy("example.com"))
    assert result["reason"] == "policy"


def test_resolve_failure_records_state_and_notifies(monkeypatch):
    state = FakeState()
    cfg = config()
    monkeypatch.setattr(renew_flow, "load_raw_config", lambda: cfg)
    monkeypatch.setattr(renew_flow, "resolve_renew_domain", lambda domain: (_ for _ in ()).throw(RuntimeError("resolve failed")))
    monkeypatch.setattr(renew_flow.store, "RenewStateStore", lambda: state)
    monkeypatch.setattr(renew_flow.notifier, "DingDingNotifier", FakeNotifier)
    result = run(renew_flow.renew_and_plan_deploy("example.com"))
    assert result["renew_status"] == "failed"
    assert result["error"] == "resolve failed"
    assert len(state.failures) == 1
    assert FakeNotifier.renew_calls[-1]["success"] is False


def test_duplicate_certificate_is_skipped_after_resolution(monkeypatch):
    cfg = config()
    monkeypatch.setattr(renew_flow, "load_raw_config", lambda: cfg)
    monkeypatch.setattr(renew_flow, "resolve_renew_domain", lambda domain: "example.com")
    processed = {"example.com"}
    result = run(renew_flow.renew_and_plan_deploy("www.example.com", processed_certificates=processed))
    assert result["reason"] == "duplicate_certificate"
    assert result["renew_status"] == "skipped"


def test_success_apply_records_state_deploys_and_notifies(monkeypatch):
    cfg = config(mode="apply")
    state = FakeState()
    cert = SimpleNamespace(expires_at="2030-01-01")
    calls = []
    async def fake_renew(**kwargs):
        calls.append(kwargs)
        return cert
    monkeypatch.setattr(renew_flow, "load_raw_config", lambda: cfg)
    monkeypatch.setattr(renew_flow, "resolve_renew_domain", lambda domain: "example.com")
    monkeypatch.setattr(renew_flow, "renew_certificate", fake_renew)
    monkeypatch.setattr(renew_flow.store, "RenewStateStore", lambda: state)
    monkeypatch.setattr(renew_flow.notifier, "DingDingNotifier", FakeNotifier)
    monkeypatch.setattr(renew_flow, "DeployService", FakeDeploy)
    result = run(renew_flow.renew_and_plan_deploy("www.example.com", provider_name="p1"))
    assert result["renew_status"] == "success"
    assert result["deploy_status"] == "success"
    assert state.successes and state.successes[0]["domain"] == "example.com"
    assert calls[0]["main_domain"] == "example.com"
    assert FakeNotifier.renew_calls[-1]["success"] is True


def test_dry_run_does_not_record_success(monkeypatch):
    cfg = config(mode="dry-run")
    state = FakeState()
    cert = SimpleNamespace(expires_at="2030-01-01")
    monkeypatch.setattr(renew_flow, "load_raw_config", lambda: cfg)
    monkeypatch.setattr(renew_flow, "resolve_renew_domain", lambda domain: "example.com")
    monkeypatch.setattr(renew_flow, "renew_certificate", lambda **kwargs: asyncio.sleep(0, result=cert))
    monkeypatch.setattr(renew_flow.store, "RenewStateStore", lambda: state)
    monkeypatch.setattr(renew_flow.notifier, "DingDingNotifier", FakeNotifier)
    monkeypatch.setattr(renew_flow, "DeployService", FakeDeploy)
    result = run(renew_flow.renew_and_plan_deploy("example.com"))
    assert result["renew_status"] == "success"
    assert result["deploy_status"] == "planned"
    assert state.successes == []


def test_renew_retries_then_succeeds(monkeypatch):
    cfg = config(mode="apply", retries=3)
    state = FakeState()
    cert = SimpleNamespace(expires_at="2030-01-01")
    attempts = {"n": 0}
    async def fake_renew(**kwargs):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError(f"failure-{attempts['n']}")
        return cert
    monkeypatch.setattr(renew_flow, "load_raw_config", lambda: cfg)
    monkeypatch.setattr(renew_flow, "resolve_renew_domain", lambda domain: "example.com")
    monkeypatch.setattr(renew_flow, "renew_certificate", fake_renew)
    monkeypatch.setattr(renew_flow.store, "RenewStateStore", lambda: state)
    monkeypatch.setattr(renew_flow.notifier, "DingDingNotifier", FakeNotifier)
    monkeypatch.setattr(renew_flow, "DeployService", FakeDeploy)
    result = run(renew_flow.renew_and_plan_deploy("example.com"))
    assert result["retry_count"] == 3
    assert result["renew_status"] == "success"
    assert len(state.failures) == 2


def test_renew_retry_exhaustion_does_not_deploy(monkeypatch):
    cfg = config(retries=2)
    state = FakeState()
    async def fail(**kwargs):
        raise RuntimeError("renew failed")
    class NoDeploy:
        async def apply_deploy(self, **kwargs):
            raise AssertionError("must not deploy")
        async def plan_dry_run(self, **kwargs):
            raise AssertionError("must not plan")
    monkeypatch.setattr(renew_flow, "load_raw_config", lambda: cfg)
    monkeypatch.setattr(renew_flow, "resolve_renew_domain", lambda domain: "example.com")
    monkeypatch.setattr(renew_flow, "renew_certificate", fail)
    monkeypatch.setattr(renew_flow.store, "RenewStateStore", lambda: state)
    monkeypatch.setattr(renew_flow.notifier, "DingDingNotifier", FakeNotifier)
    monkeypatch.setattr(renew_flow, "DeployService", NoDeploy)
    result = run(renew_flow.renew_and_plan_deploy("example.com"))
    assert result["renew_status"] == "failed"
    assert result["retry_count"] == 2
    assert FakeNotifier.renew_calls[-1]["success"] is False


def test_deploy_failure_marks_pending_state_in_apply(monkeypatch):
    cfg = config(mode="apply")
    state = FakeState()
    cert = SimpleNamespace(expires_at="2030-01-01")
    class BadDeploy:
        async def apply_deploy(self, **kwargs):
            raise RuntimeError("deploy failed")
    monkeypatch.setattr(renew_flow, "load_raw_config", lambda: cfg)
    monkeypatch.setattr(renew_flow, "resolve_renew_domain", lambda domain: "example.com")
    monkeypatch.setattr(renew_flow, "renew_certificate", lambda **kwargs: asyncio.sleep(0, result=cert))
    monkeypatch.setattr(renew_flow.store, "RenewStateStore", lambda: state)
    monkeypatch.setattr(renew_flow.notifier, "DingDingNotifier", FakeNotifier)
    monkeypatch.setattr(renew_flow, "DeployService", BadDeploy)
    result = run(renew_flow.renew_and_plan_deploy("example.com"))
    assert result["renew_status"] == "deploy_failed"
    assert state.deploy_failures
    assert state.successes == []


def test_deploy_failed_pending_skips_new_renew(monkeypatch):
    cfg = config(mode="apply")
    state = FakeState({"last_status": "deploy_failed", "last_message": "manual action required"})
    called = []
    async def must_not_renew(**kwargs):
        called.append(1)
        return SimpleNamespace(expires_at="2030")
    monkeypatch.setattr(renew_flow, "load_raw_config", lambda: cfg)
    monkeypatch.setattr(renew_flow, "resolve_renew_domain", lambda domain: "example.com")
    monkeypatch.setattr(renew_flow.store, "RenewStateStore", lambda: state)
    monkeypatch.setattr(renew_flow, "renew_certificate", must_not_renew)
    result = run(renew_flow.renew_and_plan_deploy("example.com", automatic=True))
    assert result["renew_status"] == "deploy_failed_pending"
    assert called == []


def test_cooldown_skips_automatic_and_normal_flow(monkeypatch):
    cfg = config(mode="apply", cooldown=7)
    state = FakeState({"last_status": "success", "last_success_at": "2030-01-01T00:00:00+00:00"})
    monkeypatch.setattr(renew_flow, "load_raw_config", lambda: cfg)
    monkeypatch.setattr(renew_flow, "resolve_renew_domain", lambda domain: "example.com")
    monkeypatch.setattr(renew_flow.store, "RenewStateStore", lambda: state)
    monkeypatch.setattr(renew_flow.TimeUtil, "now_utc", lambda: renew_flow.TimeUtil.parse("2030-01-03T00:00:00+00:00"))
    result = run(renew_flow.renew_and_plan_deploy("example.com", automatic=True))
    assert result["renew_status"] == "skipped"
    assert result["cooldown_days"] == 7


def test_notification_failure_does_not_change_success(monkeypatch):
    cfg = config(mode="apply")
    state = FakeState()
    cert = SimpleNamespace(expires_at="2030")
    class BadNotifier(FakeNotifier):
        async def notify_renew_result(self, **kwargs):
            raise RuntimeError("notify failed")
    monkeypatch.setattr(renew_flow, "load_raw_config", lambda: cfg)
    monkeypatch.setattr(renew_flow, "resolve_renew_domain", lambda domain: "example.com")
    monkeypatch.setattr(renew_flow, "renew_certificate", lambda **kwargs: asyncio.sleep(0, result=cert))
    monkeypatch.setattr(renew_flow.store, "RenewStateStore", lambda: state)
    monkeypatch.setattr(renew_flow.notifier, "DingDingNotifier", BadNotifier)
    monkeypatch.setattr(renew_flow, "DeployService", FakeDeploy)
    result = run(renew_flow.renew_and_plan_deploy("example.com"))
    assert result["renew_status"] == "success"
    assert state.successes
