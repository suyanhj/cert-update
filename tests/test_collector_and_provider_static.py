import asyncio
import importlib
import sys
import types

import pytest

from box import Box

import app.providers.static as static_provider_module


def _import_collector_with_fake_nicegui():
    fake_nicegui = types.ModuleType("nicegui")
    fake_nicegui.ui = types.SimpleNamespace(notify=lambda *a, **k: None)
    sys.modules["nicegui"] = fake_nicegui
    module = importlib.import_module("app.services.collector")
    return importlib.reload(module)


def test_collect_all_runs_domains_then_certificates(monkeypatch):
    collector = _import_collector_with_fake_nicegui()
    calls = []

    async def fake_collect_domains():
        calls.append("domains")

    async def fake_collect_certificates():
        calls.append("certificates")

    async def fake_collect_ecs():
        calls.append("ecs")

    async def fake_refresh_bindings():
        calls.append("bindings")

    monkeypatch.setattr(collector, "collect_domains", fake_collect_domains)
    monkeypatch.setattr(collector, "collect_certificates", fake_collect_certificates)
    monkeypatch.setattr(collector, "collect_ecs_instances_in_thread", fake_collect_ecs)
    monkeypatch.setattr(collector, "refresh_product_bindings_cache", fake_refresh_bindings)
    monkeypatch.setattr(collector.timeutils, "now", lambda: "NOW")
    collector.state.LAST_UPDATED = None

    asyncio.run(collector.collect_all())

    assert calls == ["domains", "ecs", "bindings", "certificates"]
    assert collector.state.LAST_UPDATED == "NOW"


def test_collect_all_continues_when_domains_failed(monkeypatch):
    collector = _import_collector_with_fake_nicegui()
    calls = {"cert": 0}

    async def fake_collect_domains():
        raise RuntimeError("domains failed")

    async def fake_collect_certificates():
        calls["cert"] += 1

    async def fake_collect_ecs():
        return None

    async def fake_refresh_bindings():
        return None

    monkeypatch.setattr(collector, "collect_domains", fake_collect_domains)
    monkeypatch.setattr(collector, "collect_certificates", fake_collect_certificates)
    monkeypatch.setattr(collector, "collect_ecs_instances_in_thread", fake_collect_ecs)
    monkeypatch.setattr(collector, "refresh_product_bindings_cache", fake_refresh_bindings)
    monkeypatch.setattr(collector.timeutils, "now", lambda: "NOW")
    collector.state.LAST_UPDATED = None

    with pytest.raises(RuntimeError, match="domains: domains failed"):
        asyncio.run(collector.collect_all())

    assert calls["cert"] == 1
    assert collector.state.LAST_UPDATED is None


def test_collect_domains_updates_state_and_event(monkeypatch):
    collector = _import_collector_with_fake_nicegui()
    expected = [{"domain": "example.com", "subs": []}]

    class FakeDomains:
        async def discovery_all(self):
            return expected

    monkeypatch.setattr(collector, "Domains", FakeDomains)
    monkeypatch.setattr(collector, "load_raw_config", lambda: Box({}, default_box=True))
    collector.state.DOMAIN_GROUPS = []
    collector.state.DOMAIN_READY.set()

    asyncio.run(collector.collect_domains())

    assert collector.state.DOMAIN_GROUPS == expected
    assert collector.state.DOMAIN_READY.is_set() is True


def test_collect_certificates_triggers_auto_renew_flow(monkeypatch):
    collector = _import_collector_with_fake_nicegui()
    expected_certs = [{"domain": "api.example.com", "provider": "ali-1", "days": 5}]
    calls = {"triggered": None}

    class FakeCertificates:
        async def get_certificate_list(self, domains):
            return expected_certs

    async def fake_trigger(certs):
        calls["triggered"] = certs

    monkeypatch.setattr(collector, "Certificates", FakeCertificates)
    monkeypatch.setattr(
        collector,
        "_auto_renew_and_deploy_expiring_certificates",
        fake_trigger,
    )
    collector.state.DOMAIN_GROUPS = [{"domain": "example.com"}]
    collector.state.DOMAIN_READY.set()

    asyncio.run(collector.collect_certificates())

    assert collector.state.CERT_LIST == expected_certs
    assert calls["triggered"] == expected_certs


def test_auto_renew_flow_skips_when_auto_renew_disabled(monkeypatch):
    collector = _import_collector_with_fake_nicegui()
    calls: list[tuple[str, str | None, bool]] = []

    async def fake_renew_and_plan_deploy(domain, provider_name=None, force=False):
        calls.append((domain, provider_name, force))
        return {"mode": "dry-run"}

    monkeypatch.setattr(
        collector,
        "load_raw_config",
        lambda: Box(
            {
                "auto_renew_enabled": False,
                "auto_renew_days": 20,
            },
            default_box=True,
        ),
    )
    monkeypatch.setattr(collector, "renew_and_plan_deploy", fake_renew_and_plan_deploy)

    asyncio.run(
        collector._auto_renew_and_deploy_expiring_certificates(
            [{"domain": "api.example.com", "provider": "ali-1", "days": 5}]
        )
    )

    assert calls == []


def test_auto_renew_flow_triggers_for_expiring_certificates(monkeypatch):
    collector = _import_collector_with_fake_nicegui()
    calls: list[tuple[str, str | None, bool]] = []

    async def fake_renew_and_plan_deploy(domain, provider_name=None, force=False):
        calls.append((domain, provider_name, force))
        return {"mode": "apply"}

    monkeypatch.setattr(
        collector,
        "load_raw_config",
        lambda: Box(
            {
                "auto_renew_enabled": True,
                "auto_renew_days": 20,
            },
            default_box=True,
        ),
    )
    monkeypatch.setattr(collector, "renew_and_plan_deploy", fake_renew_and_plan_deploy)

    asyncio.run(
        collector._auto_renew_and_deploy_expiring_certificates(
            [
                {"domain": "api.example.com", "provider": "ali-1", "days": 5},
                {"domain": "api.example.com", "provider": "ali-1", "days": 5},
                {"domain": "www.example.com", "provider": "ali-1", "days": 30},
                {"domain": "bad-days.example.com", "provider": "ali-1", "days": "x"},
                {"domain": "no-provider.example.com", "provider": "", "days": 5},
            ]
        )
    )

    # 自动续签应与 Web 强制续签共用全量部署语义：
    # provider_name=None 避免限定部署云厂商，force=True 在命中阈值后立即续签。
    assert calls == [("api.example.com", None, True)]


def test_static_provider_full_domain_expands_at_and_subdomain():
    cfg = Box(
        {
            "name": "custom-1",
            "type": "custom",
            "concurrency": 5,
            "check_extensions": False,
            "sub_extensions": [],
            "static_domains": [
                {
                    "domain": "example.com",
                    "sub_domains": ["@", "api"],
                    "expires_at": "unknow",
                    "registrant_org": "org",
                }
            ],
        },
        default_box=True,
    )
    provider = static_provider_module.StaticCloudProvider(cfg)

    assert provider._full_domain() == ["example.com", "api.example.com"]


def test_static_provider_get_domain_list_unknown_expiry(monkeypatch):
    cfg = Box(
        {
            "name": "custom-1",
            "type": "custom",
            "concurrency": 5,
            "check_extensions": False,
            "sub_extensions": [],
            "static_domains": [
                {
                    "domain": "example.com",
                    "sub_domains": ["@"],
                    "expires_at": "unknow",
                    "registrant_org": "org",
                }
            ],
        },
        default_box=True,
    )
    provider = static_provider_module.StaticCloudProvider(cfg)

    async def fake_discover_sub_domains(domains):
        return [{"name": domains[0], "status": "在工作", "remark": ""}]

    monkeypatch.setattr(provider, "discover_sub_domains", fake_discover_sub_domains)
    result = asyncio.run(provider.get_domain_list())

    assert len(result) == 1
    assert result[0]["domain"] == "example.com"
    assert result[0]["days"] == 0
    assert result[0]["provider"] == "custom"
    assert result[0]["subs"][0]["status"] == "在工作"
