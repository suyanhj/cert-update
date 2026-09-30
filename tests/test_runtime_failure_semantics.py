import asyncio
import subprocess

import pytest
from box import Box

from app.schemas.acme import AcmeShExecutionError
from app.providers.base import Provider
from app.services.domain_alert import DomainAlertService
from app.services.ecs_alert import EcsAlertService
from app.utils.acme_sh import AcmeShRenewer
from app.utils.store import RenewStateStore


class _Logger:
    def debug(self, *args, **kwargs):
        return None

    def warning(self, *args, **kwargs):
        return None

    def error(self, *args, **kwargs):
        return None


class _StateStore:
    def __init__(self):
        self.fired = []
        self.recovered = []

    def is_firing(self, key):
        return key in self.fired

    def fire(self, key):
        self.fired.append(key)
        return True

    def recover(self, key):
        self.recovered.append(key)

    def get_all(self):
        return {}


class _Batcher:
    def __init__(self, fail=False):
        self.fail = fail
        self.events = []

    def add(self, event):
        self.events.append(event)

    async def flush_alerts(self, notifier):
        if self.fail:
            raise RuntimeError("notify failed")

    def clear(self):
        self.events.clear()


def test_domain_alert_commits_state_only_after_send_success():
    config = Box(
        {
            "whitelist": [],
            "alert": {
                "domain": {
                    "enabled": True,
                    "domain_warn_days": 15,
                    "domain_expiry_days": 28,
                }
            },
        },
        default_box=True,
    )
    state = _StateStore()
    batcher = _Batcher(fail=True)
    service = DomainAlertService(config, _Logger(), object(), state, batcher)
    domains = [
        {
            "domain": "example.com",
            "expires_at": "2026-09-12T00:00:00+00:00",
            "days": 2,
            "provider_name": "aliyun-1",
        }
    ]

    asyncio.run(service.send_domain_alerts(domains))
    assert state.fired == []

    batcher.fail = False
    asyncio.run(service.send_domain_alerts(domains))
    assert state.fired == ["example.com"]


def test_domain_alert_uses_registrar_account_instead_of_dns_provider():
    config = Box(
        {
            "whitelist": [],
            "alert": {
                "domain": {
                    "enabled": True,
                    "domain_warn_days": 15,
                    "domain_expiry_days": 28,
                }
            },
        },
        default_box=True,
    )
    state = _StateStore()

    class InspectBatcher(_Batcher):
        def clear(self):
            return None

    batcher = InspectBatcher()
    service = DomainAlertService(config, _Logger(), object(), state, batcher)
    asyncio.run(
        service.send_domain_alerts([
            {
                "domain": "example.com",
                "expires_at": "2026-09-20T00:00:00+00:00",
                "days": 4,
                "registrar_name": "ali-registrar",
                "registrar_provider": "aliyun",
                "dns_name": "cf-dns",
                "dns_provider": "cloudflare",
            }
        ])
    )

    assert len(batcher.events) == 1
    assert batcher.events[0].provider == "ali-registrar"
    assert "ali-registrar" in batcher.events[0].message


def test_ecs_alert_skips_unknown_expiry():
    config = Box(
        {
            "alert": {
                "ecs": {
                    "enabled": True,
                    "ecs_warn_days": 7,
                    "ecs_expiry_days": 15,
                }
            }
        },
        default_box=True,
    )
    state = _StateStore()
    batcher = _Batcher()
    service = EcsAlertService(config, _Logger(), object(), state, batcher)

    asyncio.run(
        service.send_ecs_alerts(
            [
                {
                    "provider": "aliyun",
                    "provider_name": "aliyun-1",
                    "instance_id": "i-unknown",
                    "expires_at": "",
                    "days": None,
                    "auto_renewal": False,
                    "renewal_status": "Normal",
                }
            ]
        )
    )

    assert state.fired == []
    assert batcher.events == []


def test_acme_command_timeout_kills_process(monkeypatch: pytest.MonkeyPatch):
    class FakeStream:
        def readline(self):
            return ""

        def close(self):
            return None

    class FakeProcess:
        returncode = None

        def __init__(self):
            self.killed = False
            self.wait_calls = 0
            self.stdout = FakeStream()
            self.stderr = FakeStream()

        def wait(self, timeout=None):
            self.wait_calls += 1
            if self.wait_calls == 1:
                raise subprocess.TimeoutExpired(["acme.sh"], timeout)
            self.returncode = -9
            return self.returncode

        def kill(self):
            self.killed = True

    process = FakeProcess()
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)

    renewer = AcmeShRenewer.__new__(AcmeShRenewer)
    renewer._config = Box(
        {"acme": {"command_timeout_seconds": 1}},
        default_box=True,
    )

    with pytest.raises(AcmeShExecutionError, match="执行超时"):
        renewer._run_cmd(["acme.sh", "--renew", "-d", "example.com"])

    assert process.killed is True
    assert process.wait_calls == 2


def test_provider_api_treats_service_not_enabled_as_empty():
    provider = Provider(Box({"name": "test-provider"}, default_box=True))

    async def run():
        return await provider._call_provider_api(
            "获取 CDN 域名列表",
            lambda: (_ for _ in ()).throw(
                RuntimeError(
                    "code: 403, Code: CdnServiceNotFound, "
                    "Message: Your account does not open CDN service yet."
                )
            ),
            default=[],
        )

    assert asyncio.run(run()) == []


def test_provider_api_errors_raise_by_default():
    provider = Provider(Box({"name": "test-provider"}, default_box=True))

    async def run():
        return await provider._call_provider_api(
            "query bindings",
            lambda: (_ for _ in ()).throw(RuntimeError("api failed")),
            default=[],
        )

    with pytest.raises(RuntimeError, match="query bindings"):
        asyncio.run(run())


def test_deploy_failure_state_persists_for_manual_handling(tmp_path):
    path = tmp_path / "renew-state.json"
    state = RenewStateStore(str(path))

    state.record_deploy_failure("Example.COM", "tencent-1", "deploy failed")

    saved = RenewStateStore(str(path)).get("example.com")
    assert saved["last_status"] == "deploy_failed"
    assert saved["last_message"] == "deploy failed"
    assert saved["consecutive_failures"] == 0
