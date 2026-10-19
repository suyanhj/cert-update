"""页面在异步操作期间断开或销毁时，后台继续完成且不更新旧页面。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from box import Box
from nicegui import Client, ui as real_ui

from app.ui import pages


class PageClient:
    is_deleted = False
    has_socket_connection = True

    def __enter__(self):
        assert not self.is_deleted and self.has_socket_connection
        return self

    def __exit__(self, *args):
        return False


def setup_ui(monkeypatch):
    client = PageClient()
    ui = Mock()
    ui.context = SimpleNamespace(client=client)
    events, buttons, elements = {}, {}, []

    def element(*args, **kwargs):
        item = MagicMock()
        for method in ['classes', 'props', 'style', 'tooltip']:
            getattr(item, method).return_value = item
        item.__enter__.return_value = item
        item.on.side_effect = lambda name, handler: events.update({name: handler}) or item
        if 'on_click' in kwargs:
            buttons[args[0]] = kwargs['on_click']
        elements.append(item)
        return item

    for name in ['label', 'table', 'column', 'row', 'expansion', 'element',
                 'button', 'separator', 'icon', 'textarea']:
        getattr(ui, name).side_effect = element

    def notify(*args, **kwargs):
        assert not client.is_deleted and client.has_socket_connection

    ui.notify.side_effect = notify
    monkeypatch.setattr(pages, 'ui', ui)
    monkeypatch.setattr(pages, 'get_config', lambda: Box({
        'deploy': {'mode': 'apply'}, 'auto_renew_enabled': True,
        'alert': {'cert': {'cert_warn_days': 7, 'cert_expiry_days': 30}},
    }, default_box=True))
    monkeypatch.setattr(pages.state, 'CERT_LIST', [])
    return client, ui, events, buttons, elements


def disconnect(client, state):
    client.is_deleted = state == 'deleted'
    client.has_socket_connection = state == 'connected'


@pytest.mark.parametrize('state', ['connected', 'disconnected', 'deleted'])
@pytest.mark.parametrize('status, mode', [
    ('skipped', 'apply'), ('deploy_failed', 'apply'), ('deploy_failed_pending', 'apply'),
    ('failed', 'apply'), ('success', 'apply'), ('success', 'dry-run'),
])
def test_force_renew_result_respects_original_client(monkeypatch, state, status, mode):
    client, ui, events, _, _ = setup_ui(monkeypatch)

    async def renew(**kwargs):
        disconnect(client, state)
        return {'renew_status': status, 'mode': mode, 'message': '结果', 'retry_count': 1}

    operation = AsyncMock(side_effect=renew)
    monkeypatch.setattr(pages, 'renew_and_plan_deploy', operation)
    pages.render_certificates()
    asyncio.run(events['manual-renew-force'](SimpleNamespace(args='api.example.com')))
    operation.assert_awaited_once()
    assert ui.notify.call_count == (2 if state == 'connected' else 1)


@pytest.mark.parametrize('state', ['connected', 'disconnected', 'deleted'])
@pytest.mark.parametrize('failure', [False, True])
def test_config_save_skips_old_controls_after_await(monkeypatch, state, failure):
    client, ui, _, buttons, elements = setup_ui(monkeypatch)
    monkeypatch.setattr(pages, '_load_config_text', lambda: 'acme: {}')

    async def apply(text):
        disconnect(client, state)
        if failure:
            raise ValueError('配置校验失败')

    operation = AsyncMock(side_effect=apply)
    monkeypatch.setattr(pages, 'apply_config_text', operation)
    pages.build_config_ui()
    before = sum(e.set_text.call_count for e in elements)
    asyncio.run(buttons['保存配置']())
    operation.assert_awaited_once()
    changes = sum(e.set_text.call_count for e in elements) - before
    expected_changes = 2
    if state == 'connected':
        expected_changes += 2 if failure else 1
    assert changes == expected_changes
    assert ui.notify.call_count == (1 if state == 'connected' else 0)


@pytest.mark.parametrize('state', ['connected', 'disconnected', 'deleted'])
def test_collection_only_refreshes_connected_client(monkeypatch, state):
    client, ui, _, buttons, _ = setup_ui(monkeypatch)
    client.run_javascript = Mock()
    monkeypatch.setattr(pages, 'render_certificates', lambda: None)
    monkeypatch.setattr(pages, 'render_domains', lambda: None)
    monkeypatch.setattr(pages, '_format_last_updated_shanghai', lambda _: '时间')

    async def collect():
        disconnect(client, state)

    operation = AsyncMock(side_effect=collect)
    monkeypatch.setattr(pages, 'collect_all', operation)
    pages.build_ui()
    asyncio.run(buttons['立即采集']())
    operation.assert_awaited_once()
    assert client.run_javascript.call_count == (1 if state == 'connected' else 0)
    assert ui.notify.call_count == (2 if state == 'connected' else 1)


@pytest.mark.parametrize('state', ['connected', 'disconnected', 'deleted'])
def test_notify_with_real_nicegui_client(monkeypatch, caplog, state):
    monkeypatch.setattr(pages, 'ui', real_ui)

    async def run():
        client = Client(real_ui.page('/client-lifecycle-test'))
        try:
            if state == 'connected':
                client.tab_id = 'test-tab'
            elif state == 'deleted':
                client.delete()
            before = len(client.outbox.messages)
            pages._notify_safe(client, '后台操作完成')
            assert len(client.outbox.messages) - before == (1 if state == 'connected' else 0)
        finally:
            if not client.is_deleted:
                client.delete()

    asyncio.run(run())
    assert 'Client has been deleted' not in caplog.text
