"""自定义域名留空字段的配置加载及采集回归。"""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
import yaml
from box import Box

from app.config import _validate_config
from app.providers.static import StaticCloudProvider
from app.schemas.config import StaticDomainConfig


@pytest.mark.parametrize("fields", [
    {},
    {"expires_at": "", "registrant_org": ""},
    {"expires_at": None, "registrant_org": None},
    {"expires_at": " \t\n", "registrant_org": " \t\n"},
])
def test_empty_fields_load_as_unknown_and_skip_date_parsing(monkeypatch, fields):
    raw = yaml.safe_load(yaml.safe_dump({
        "acme": {},
        "providers": [{
            "name": "恒拓", "type": "custom", "enabled": True,
            "static_domains": [{"domain": "sqkm2023.com", "sub_domains": ["api"], **fields}],
        }],
    }, allow_unicode=True))
    cfg = Box(_validate_config(raw))["providers"][0]
    assert cfg.static_domains[0].expires_at == "unknown"
    assert cfg.static_domains[0].registrant_org == "unknown"
    provider = StaticCloudProvider(cfg)

    async def discover(domains):
        assert domains == ["api.sqkm2023.com"]
        return []

    monkeypatch.setattr(provider, "discover_sub_domains", AsyncMock(side_effect=discover))
    monkeypatch.setattr(provider.time, "parse", Mock(side_effect=AssertionError("未知日期不应解析")))
    result = asyncio.run(provider.get_domain_list())
    assert result[0]["expires_at"] == "unknown"
    assert result[0]["org"] == "unknown"
    assert result[0]["days"] == 0
    provider.discover_sub_domains.assert_awaited_once()


def test_nonempty_static_domain_fields_remain_unchanged():
    cfg = StaticDomainConfig(domain="sqkm2023.com", expires_at="2027-01-01 00:00:00", registrant_org="恒拓")
    assert cfg.expires_at == "2027-01-01 00:00:00"
    assert cfg.registrant_org == "恒拓"


@pytest.mark.parametrize("fields", [
    {"expires_at": "", "registrant_org": ""},
    {"expires_at": None, "registrant_org": None},
    {"expires_at": " \t", "registrant_org": " \t"},
    {},
])
def test_discovery_accepts_raw_empty_fields_for_three_static_accounts(monkeypatch, fields):
    from app.services.domain_discovery import DomainDiscoveryService
    from app.utils import store

    providers = []
    for index, name in enumerate(["星闪", "恒拓", "科曼"]):
        provider = StaticCloudProvider(Box({
            "name": name, "type": "custom", "enabled": True,
            "static_domains": [{"domain": f"example{index}.com", "sub_domains": ["api"], **fields}],
        }, default_box=True))
        monkeypatch.setattr(provider, "discover_sub_domains", AsyncMock(return_value=[]))
        providers.append(provider)
    mapping = Mock()
    monkeypatch.setattr(store, "DomainProviderStore", lambda: mapping)
    discovery = DomainDiscoveryService(Mock())
    result = asyncio.run(discovery.discover_all(providers, [], 3))
    assert len(result) == 3
    assert {item["name"] for item in result} == {"星闪", "恒拓", "科曼"}
    assert all(item["expires_at"] == "unknown" for item in result)
    assert all(item["registrant_org"] == "unknown" for item in result)
    mapping.save.assert_called_once_with(result)
