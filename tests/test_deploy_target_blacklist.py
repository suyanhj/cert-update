"""部署目标黑名单覆盖动态扫描、共享资源及通配边界。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.schemas.provider import CloudProductBinding, DeployMatchResult
from app.services.deploy import DeployService


def target(domain, product_id=None, metadata=None):
    return DeployMatchResult('tencent', 'account', 'clb', product_id or domain,
                             domain, 'exact', None, metadata=metadata or {})


@pytest.mark.parametrize('blacklist, domains, expected', [
    (['api.example.com'], ['api.example.com', 'test.example.com'], ['test.example.com']),
    (['*.example.com'], ['api.example.com', 'example.com', 'deep.api.example.com'],
     ['example.com', 'deep.api.example.com']),
    (['api.example.com'], ['*.example.com', 'test.example.com'], ['test.example.com']),
    ([], ['api.example.com', 'test.example.com'], ['api.example.com', 'test.example.com']),
])
def test_target_blacklist_boundaries(blacklist, domains, expected):
    service = DeployService.__new__(DeployService)
    service.conf = SimpleNamespace(blacklist=blacklist)
    skipped = []
    result = service._filter_blacklisted_matches([target(d) for d in domains], {'*.example.com'}, skipped)
    assert [item.domain for item in result] == expected
    assert len(skipped) == len(domains) - len(expected)
    assert all(item['reason'] == 'blacklist' for item in skipped)


def test_shared_resource_and_listener_scope_are_not_indirectly_updated():
    service = DeployService.__new__(DeployService)
    service.conf = SimpleNamespace(blacklist=['api.example.com'])
    matches = [target('api.example.com', 'shared'), target('test.example.com', 'shared'),
               target('test.example.com', 'listener', {'listener_scope': True}),
               target('test.example.com', 'independent')]
    skipped = []
    result = service._filter_blacklisted_matches(matches, {'*.example.com'}, skipped)
    assert [item.product_id for item in result] == ['independent']
    assert len(skipped) == 3


def test_regular_cloud_collection_filters_blacklisted_destinations():
    service = DeployService.__new__(DeployService)
    service.conf = SimpleNamespace(blacklist=['api.example.com'])
    provider = SimpleNamespace(name='account', config=SimpleNamespace(type='aliyun', name='account'))
    service._build_cloud_providers = lambda: [provider]
    service._binding_cache_settings = lambda: (False, 3600, False)
    service._get_provider_bindings = AsyncMock(return_value=[
        CloudProductBinding(provider_name='account', product_type='cdn', product_id=d, domain=d)
        for d in ['api.example.com', 'test.example.com']
    ])
    _, _, matches, skipped = asyncio.run(service._collect_matches(
        SimpleNamespace(domain='test.example.com', sans=['*.example.com'])))
    assert [m.domain for m in matches] == ['test.example.com']
    assert skipped[0]['domain'] == 'api.example.com'
    assert skipped[0]['matched_domain'] == 'api.example.com'
