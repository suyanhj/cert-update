"""Cloudflare Zone/DNS 发现与 acme.sh 凭证映射测试。"""

import asyncio

import pytest
import yaml
from box import Box

import app.providers.cloudflare as cloudflare_provider_mod
from app.providers.cloudflare import CloudflareDNSProvider
from app.schemas.config import GlobalProductScanConfig, ProviderConfig
from app.services.provider_factory import get_provider_builder_map
from app.utils.acme_sh import AcmeShRenewer


class FakeListAPI:
    def __init__(self, items):
        self.items = items
        self.calls = []

    def list(self, **kwargs):
        self.calls.append(kwargs)
        return iter(self.items)


class FakeCloudflareClient:
    def __init__(self, zones, records_by_zone):
        self.zones = FakeListAPI(zones)
        self.dns = Box(default_box=True)
        self.dns.records = FakeListAPI([])

        def list_records(**kwargs):
            self.dns.records.calls.append(kwargs)
            return iter(records_by_zone.get(kwargs["zone_id"], []))

        self.dns.records.list = list_records


def _provider_config(**credentials) -> ProviderConfig:
    return ProviderConfig(
        name="cf-test",
        type="cloudflare",
        credentials=credentials,
    )


def _install_fake_client(monkeypatch, zones, records_by_zone, captured=None):
    client = FakeCloudflareClient(zones, records_by_zone)

    def build_client(**kwargs):
        if captured is not None:
            captured.update(kwargs)
        return client

    monkeypatch.setattr(cloudflare_provider_mod, "Cloudflare", build_client)
    monkeypatch.setattr(
        cloudflare_provider_mod,
        "load_raw_config",
        lambda: Box({"http_scan_parallel": 3}, default_box=True),
    )
    return client


def test_cloudflare_provider_requires_token(monkeypatch):
    monkeypatch.setattr(
        cloudflare_provider_mod,
        "Cloudflare",
        lambda **kwargs: pytest.fail("缺少 Token 时不应创建客户端"),
    )
    with pytest.raises(ValueError, match="api_token"):
        CloudflareDNSProvider(_provider_config(api_token="  "))


def test_cloudflare_provider_initializes_sdk_and_filters_account(monkeypatch):
    captured = {}
    client = _install_fake_client(monkeypatch, [], {}, captured)
    provider = CloudflareDNSProvider(
        _provider_config(api_token="token-value", account_id="account-1")
    )

    assert captured == {"api_token": "token-value"}
    assert provider._list_active_zones() == []
    assert client.zones.calls == [
        {
            "status": "active",
            "per_page": 50,
            "account": {"id": "account-1"},
        }
    ]


def test_cloudflare_domain_discovery_filters_zone_and_probes_host_records(monkeypatch):
    zones = [
        {
            "id": "zone-1",
            "name": "Example.COM.",
            "status": "active",
            "type": "full",
            "name_servers": ["ABBY.NS.CLOUDFLARE.COM.", "bob.ns.cloudflare.com"],
            "account": {"id": "account-1"},
        },
        {"id": "zone-2", "name": "pending.example", "status": "pending"},
    ]
    records = {
        "zone-1": [
            {"id": "a1", "name": "example.com", "type": "A", "content": "192.0.2.1", "ttl": 1, "proxied": True},
            {"id": "a2", "name": "v6.example.com", "type": "AAAA", "content": "2001:db8::1", "ttl": 60},
            {"id": "c1", "name": "www.example.com", "type": "CNAME", "content": "origin.example.net", "comment": "站点"},
            {"id": "t1", "name": "_acme-challenge.example.com", "type": "TXT", "content": "txt-value"},
        ]
    }
    client = _install_fake_client(monkeypatch, zones, records)
    provider = CloudflareDNSProvider(_provider_config(api_token="token-value"))
    probed = []

    async def fake_probe(domain, *, remark="", sem=None):
        probed.append((domain, remark, sem is not None))
        return {"name": domain, "status": "在工作", "remark": remark}

    monkeypatch.setattr(provider, "_probe_domain", fake_probe)
    result = asyncio.run(provider.get_domain_list())

    assert [item["domain"] for item in result] == ["example.com"]
    assert result[0]["account_id"] == "account-1"
    assert result[0]["assigned_nameservers"] == [
        "abby.ns.cloudflare.com",
        "bob.ns.cloudflare.com",
    ]
    assert result[0]["dns_authoritative"] is True
    assert result[0]["expires_at"] == "unknown"
    assert [item[0] for item in probed] == [
        "example.com",
        "v6.example.com",
        "www.example.com",
    ]
    assert len(result[0]["subs"]) == 3
    assert client.dns.records.calls == [{"zone_id": "zone-1", "per_page": 5000}]


def test_cloudflare_get_dns_records_keeps_all_types_and_normalizes_names(monkeypatch):
    zones = [{"id": "zone-1", "name": "example.com", "status": "active"}]
    records = {
        "zone-1": [
            {"id": "root", "name": "example.com", "type": "MX", "content": "mail.example.com", "ttl": 300},
            {"id": "txt", "name": "_acme-challenge.example.com", "type": "TXT", "content": "value", "comment": "ACME"},
        ]
    }
    _install_fake_client(monkeypatch, zones, records)
    provider = CloudflareDNSProvider(_provider_config(api_token="token-value"))

    result = asyncio.run(provider.get_dns_records("EXAMPLE.COM."))

    assert [item["type"] for item in result] == ["MX", "TXT"]
    assert result[0]["subdomain"] == "@"
    assert result[1]["subdomain"] == "_acme-challenge"
    assert result[1]["remark"] == "ACME"


def test_cloudflare_schema_factory_and_dns_only_defaults():
    config = _provider_config(api_token="token-value")
    assert config.type == "cloudflare"
    assert get_provider_builder_map()["cloudflare"] is CloudflareDNSProvider

    scan = GlobalProductScanConfig().cloudflare
    assert scan.domain_scan is True
    assert scan.cdn_scan is False
    assert scan.live_scan is False
    assert scan.oss_scan is False
    assert scan.lb_scan is False
    assert scan.ecs_scan is False


def test_cloudflare_domain_scan_switch_skips_provider():
    from app.services.domains import Domains

    class FakeProvider:
        name = "cf-test"
        config = Box({"type": "cloudflare"}, default_box=True)

        async def get_domain_list(self):
            raise AssertionError("domain_scan=false 时不应调用 Cloudflare Zone API")

    captured = {}

    class FakeDiscoveryService:
        async def discover_all(self, **kwargs):
            captured.update(kwargs)
            return []

    service = Domains.__new__(Domains)
    service.configs = Box(
        {"product_scan": {"cloudflare": {"domain_scan": False}}},
        default_box=True,
    )
    service.logger = Box(
        {
            "info": lambda *args, **kwargs: None,
        },
        default_box=True,
    )
    service._registry = Box(
        {
            "cloud_providers": [FakeProvider()],
            "static_providers": [],
        },
        default_box=True,
    )
    service._discovery_service = FakeDiscoveryService()
    service.static_domains = []

    async def ignore_alert(domains):
        return None

    service.domain_alert = ignore_alert
    result = asyncio.run(service.discovery_all())

    assert result == []
    assert captured["cloud_providers"] == []


def test_cloudflare_acme_env_maps_token_and_optional_account(monkeypatch):
    monkeypatch.setenv("CF_Zone_ID", "stale-zone")
    renewer = AcmeShRenewer.__new__(AcmeShRenewer)
    renewer._config = Box(
        {
            "providers": [
                {
                    "name": "cf-test",
                    "type": "cloudflare",
                    "enabled": True,
                    "credentials": {
                        "api_token": "token-value",
                        "account_id": "account-1",
                    },
                }
            ]
        },
        default_box=True,
    )

    env = renewer._build_provider_env("cf-test")

    assert env["CF_Token"] == "token-value"
    assert env["CF_Account_ID"] == "account-1"
    assert "CF_Zone_ID" not in env


def test_cloudflare_acme_env_drops_stale_optional_account(monkeypatch):
    monkeypatch.setenv("CF_Account_ID", "stale-account")
    renewer = AcmeShRenewer.__new__(AcmeShRenewer)
    renewer._config = Box(
        {
            "providers": [
                {
                    "name": "cf-test",
                    "type": "cloudflare",
                    "enabled": True,
                    "credentials": {"api_token": "token-value"},
                }
            ]
        },
        default_box=True,
    )

    env = renewer._build_provider_env("cf-test")

    assert env["CF_Token"] == "token-value"
    assert "CF_Account_ID" not in env


def test_cloudflare_example_config_is_dns_only():
    with open("config.example.yaml", encoding="utf-8") as file:
        config = yaml.safe_load(file)

    scan = config["product_scan"]["cloudflare"]
    assert scan["domain_scan"] is True
    assert all(
        scan[key] is False
        for key in ("cdn_scan", "live_scan", "oss_scan", "lb_scan", "ecs_scan")
    )
