import pytest

from app.ui import pages


def test_parse_issue_domain_text_normalizes_common_separators():
    assert pages._parse_issue_domain_text(
        "EXAMPLE.COM, www.example.com\n*.example.com"
    ) == ["example.com", "www.example.com", "*.example.com"]

    with pytest.raises(ValueError, match="至少填写"):
        pages._parse_issue_domain_text(" , ， ")


@pytest.mark.parametrize(
    "managed,selected,expected",
    [
        ("example.com", "example.com", "example.com"),
        ("example.com", "www.example.com", "example.com"),
        ("example.com", "api.test.example.com", "test.example.com"),
        ("example.com", "*.api.example.com", "api.example.com"),
    ],
)
def test_default_issue_main_domain_matrix(managed, selected, expected):
    assert pages._default_issue_main_domain(managed, selected) == expected


@pytest.mark.parametrize(
    "managed,selected",
    [
        ("*.example.com", "example.com"),
        ("example..com", "example.com"),
        ("example.com", "other.com"),
        ("example.com", "*.other.com"),
        ("example.com", ""),
    ],
)
def test_default_issue_main_domain_rejects_invalid_scope(managed, selected):
    with pytest.raises(ValueError):
        pages._default_issue_main_domain(managed, selected)


def test_format_time_shanghai_variants():
    assert pages._format_time_shanghai(None) == "N/A"
    assert pages._format_time_shanghai("") == "N/A"
    assert pages._format_time_shanghai("unknown") == "unknown"
    assert pages._format_time_shanghai("unknow") == "unknown"
    assert pages._format_last_updated_shanghai(None) == "N/A"


def test_load_config_text_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(pages.app_config, "CONFIG_PATH", tmp_path / "missing.yaml")
    with pytest.raises(FileNotFoundError, match="Config file not found"):
        pages._load_config_text()


def test_certificate_rows_only_labels_probed_domains(monkeypatch):
    class Policy:
        def __init__(self, source="acme", auto_renew=True):
            self.source = source
            self.auto_renew = auto_renew

    policies = {
        "manual.example.com": Policy("manual", False),
        "acme.example.com": Policy("acme", True),
        "off.example.com": Policy("acme", False),
    }
    monkeypatch.setattr(pages, "get_certificate_policy", lambda cfg, domain: policies[domain])

    cfg = {
        "certificate_policies": {"manual.example.com": {}, "acme.example.com": {}, "off.example.com": {}},
        "auto_renew_enabled": True,
    }
    rows = pages._certificate_rows(
        cfg,
        [
            {"domain": "acme.example.com", "days": 30},
            {"domain": "off.example.com", "days": 10},
            {"domain": "manual.example.com", "days": 20},
        ],
    )
    assert len(rows) == 3
    assert pages._certificate_rows(cfg, []) == []
    labels = {row["domain"]: row["certificate_policy_label"] for row in rows}
    assert labels["manual.example.com"] == "手动证书 · 仅部署"
    assert labels["off.example.com"] == "ACME · 自动续签已关闭"


def test_notify_safe_skips_deleted_client(monkeypatch):
    class FakeClient:
        is_deleted = True
        has_socket_connection = False
    calls = []
    monkeypatch.setattr(pages.ui, "notify", lambda *args, **kwargs: calls.append(args))
    pages._notify_safe(FakeClient(), "message")
    assert calls == []
