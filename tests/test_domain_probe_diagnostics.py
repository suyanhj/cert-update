import asyncio
from types import SimpleNamespace

import httpx
import pytest

from app.providers.base import Provider


@pytest.mark.parametrize(
    'responses, expected_status, expected_fragments',
    [
        ([502, httpx.ReadTimeout('')], '异常', ['HTTP：HTTP 502', 'HTTPS：ReadTimeout']),
        ([httpx.ConnectError('DNS lookup failed'), 503], '异常', ['HTTP：ConnectError', 'DNS lookup failed', 'HTTPS：HTTP 503']),
        ([httpx.ConnectTimeout('connect timed out'), httpx.ConnectError('connection refused')], '异常', ['ConnectTimeout', 'connection refused']),
        ([502, 200], '在工作', []),
        ([httpx.ConnectError('failed'), 403], '在工作', []),
        ([404], '在工作', []),
    ],
)
def test_probe_preserves_failure_reason(responses, expected_status, expected_fragments):
    calls = []
    pending = iter(responses)

    async def fetch(*, url):
        calls.append(url)
        result = next(pending)
        if isinstance(result, Exception):
            raise result
        return SimpleNamespace(status_code=result)

    provider = Provider(SimpleNamespace(name='test', check_extensions=True, sub_extensions=[
        {'domain': 'www.example.com', 'redirect_to': 'origin.example.com'},
    ]))
    provider.http_client = SimpleNamespace(fetch=fetch)
    result = asyncio.run(provider._probe_domain('www.example.com', remark='备注'))
    assert result['status'] == expected_status
    assert result['name'] == 'www.example.com'
    assert result['remark'] == '备注'
    assert calls == [f'{scheme}://origin.example.com' for scheme in ('http', 'https')[:len(responses)]]
    for fragment in expected_fragments:
        assert fragment in result['status_reason']
    if expected_status == '在工作':
        assert result['status_reason'] == ''


def test_probe_bounds_error_text_and_clears_wildcard_reason():
    async def fetch(*, url):
        raise httpx.ConnectError('long\n' * 1000)

    provider = Provider(SimpleNamespace(name='test', check_extensions=False, sub_extensions=[]))
    provider.http_client = SimpleNamespace(fetch=fetch)
    result = asyncio.run(provider._probe_domain('example.com'))
    assert len(result['status_reason']) < 500
    assert '\n' not in result['status_reason']
    result = asyncio.run(provider._probe_domain('*.example.com'))
    assert result['status'] == '通配符跳过'
    assert result['status_reason'] == ''


@pytest.mark.parametrize('http_version', [b'HTTP/1.1', b'HTTP/2'])
@pytest.mark.parametrize('first_failure', [None, 502, httpx.ConnectError('connection refused')])
def test_waf_567_is_reachable_with_diagnostic(http_version, first_failure):
    calls = []

    async def fetch(*, url):
        calls.append(url)
        if len(calls) == 1 and first_failure is not None:
            if isinstance(first_failure, Exception):
                raise first_failure
            return httpx.Response(first_failure)
        return httpx.Response(567, extensions={'http_version': http_version})

    provider = Provider(SimpleNamespace(name='test', check_extensions=False, sub_extensions=[]))
    provider.http_client = SimpleNamespace(fetch=fetch)
    result = asyncio.run(provider._probe_domain('waf.example.com'))
    scheme = 'HTTP' if first_failure is None else 'HTTPS'
    assert result['status'] == '在工作'
    assert result['status_reason'] == f'{scheme}：WAF 拦截（HTTP 567），服务器可响应'
    assert len(calls) == (1 if first_failure is None else 2)
