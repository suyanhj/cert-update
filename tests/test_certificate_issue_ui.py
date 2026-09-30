"""证书池与域名列表签发入口的静态行为测试。"""

import inspect

import pytest

from app.ui.pages import (
    _HOME_USAGE_TOOLTIP,
    _default_issue_main_domain,
    _parse_issue_domain_text,
    render_certificates,
    render_domains,
)


def test_certificate_pool_only_keeps_force_renew_and_deploy():
    source = inspect.getsource(render_certificates)

    assert 'label="签发"' not in source
    assert "manual-issue" not in source
    assert "table.on('manual-renew-force'" in source
    assert "table.on('manual-deploy'" in source
    assert "table.on('manual-renew'," not in source


def test_domain_list_opens_editable_issue_dialog():
    source = inspect.getsource(render_domains)

    assert "_open_issue_dialog" in source
    assert "*.{default_main_domain}" in source
    assert "确认签发" in source
    assert "domains=issue_domains" in source
    assert "registrar_provider" in source
    assert "dns_provider" in source
    assert "group.get('dns_name') or group.get('name', '')" in source


@pytest.mark.parametrize(
    ("managed_domain", "selected_domain", "expected"),
    [
        ("gzyys26.com", "gzyys26.com", "gzyys26.com"),
        ("gzyys26.com", "test.gzyys26.com", "gzyys26.com"),
        ("gzyys26.com", "oss.test.gzyys26.com", "test.gzyys26.com"),
        ("gzyys26.com", "api.oss.test.gzyys26.com", "oss.test.gzyys26.com"),
        ("gzyys26.com", "*.gzyys26.com", "gzyys26.com"),
        ("gzyys26.com", "*.test.gzyys26.com", "test.gzyys26.com"),
        ("gzyys26.com", "*.oss.test.gzyys26.com", "oss.test.gzyys26.com"),
        ("test.hj.com", "www.test.hj.com", "test.hj.com"),
        ("GZYYS26.COM.", "OSS.TEST.GZYYS26.COM.", "test.gzyys26.com"),
        (" gzyys26.com. ", " OSS.TEST.GZYYS26.COM. ", "test.gzyys26.com"),
    ],
)
def test_default_issue_main_domain_uses_selected_host_parent(
    managed_domain,
    selected_domain,
    expected,
):
    assert _default_issue_main_domain(managed_domain, selected_domain) == expected


@pytest.mark.parametrize(
    ("managed_domain", "selected_domain", "message"),
    [
        ("example.com", "api.other.com", "不属于托管域名"),
        ("example.com", "api.notexample.com", "不属于托管域名"),
        ("example.com", "example.com.evil", "不属于托管域名"),
        ("", "api.example.com", "domain 不能为空"),
        (" ", "api.example.com", "托管域名无效"),
        (".", "api.example.com", "托管域名无效"),
        ("*.example.com", "api.example.com", "托管域名无效"),
        ("example..com", "api.example.com", "托管域名无效"),
        ("example.com", "", "domain 不能为空"),
        ("example.com", " ", "所选域名无效"),
        ("example.com", "*.", "所选域名无效"),
        ("example.com", "api..example.com", "所选域名无效"),
        ("example.com", "foo.*.example.com", "所选域名无效"),
    ],
)
def test_default_issue_main_domain_rejects_invalid_boundaries(
    managed_domain,
    selected_domain,
    message,
):
    with pytest.raises(ValueError, match=message):
        _default_issue_main_domain(managed_domain, selected_domain)


def test_issue_domain_text_defaults_can_be_edited_and_deduplicated():
    assert _parse_issue_domain_text("test.hj.com\n*.test.hj.com\nwww.test.hj.com") == [
        "test.hj.com",
        "*.test.hj.com",
        "www.test.hj.com",
    ]
    assert _parse_issue_domain_text("test.hj.com") == ["test.hj.com"]
    assert _parse_issue_domain_text(
        " TEST.HJ.COM., *.TEST.HJ.COM.，test.hj.com  www.test.hj.com "
    ) == ["test.hj.com", "*.test.hj.com", "www.test.hj.com"]


@pytest.mark.parametrize("value", ["", "  \n ， , "])
def test_issue_domain_text_rejects_empty_input(value):
    with pytest.raises(ValueError, match="至少填写一个"):
        _parse_issue_domain_text(value)


def test_issue_domain_text_rejects_wildcard_as_main_domain():
    with pytest.raises(ValueError, match="主域名不能是通配域名"):
        _parse_issue_domain_text("*.test.gzyys26.com\ntest.gzyys26.com")


def test_usage_tooltip_explains_domain_issue_and_force_renew():
    assert "托管域名列表" in _HOME_USAGE_TOOLTIP
    assert "强制续签" in _HOME_USAGE_TOOLTIP
