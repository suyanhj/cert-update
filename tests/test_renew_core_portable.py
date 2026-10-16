"""Offline tests for renew service orchestration and domain normalization."""
import asyncio
from types import SimpleNamespace

import pytest

from app.services import renew


def test_prepare_issue_domains_normalizes_deduplicates_and_keeps_main():
    assert renew.prepare_issue_domains(" EXAMPLE.COM. ", [
        "example.com", " api.EXAMPLE.com. ", "", "*.example.com"
    ]) == ["example.com", "api.example.com", "*.example.com"]


@pytest.mark.parametrize("domain", ["", "   ", "*.example.com"])
def test_prepare_issue_domains_rejects_invalid_main(domain):
    with pytest.raises(ValueError):
        renew.prepare_issue_domains(domain, [])


@pytest.mark.parametrize("fn", [
    renew.issue_certificate,
    renew.renew_certificate,
    renew.load_existing_certificate,
])
def test_empty_domain_rejected_without_side_effect(fn):
    with pytest.raises(ValueError, match="domain"):
        if fn is renew.issue_certificate:
            asyncio.run(fn("", []))
        else:
            asyncio.run(fn(""))


@pytest.mark.asyncio
async def test_issue_certificate_delegates_to_thread(monkeypatch):
    called = {}
    class FakeRenewer:
        def issue(self, domain, sans, provider):
            called.update(domain=domain, sans=sans, provider=provider)
            return "cert"
    monkeypatch.setattr(renew, "AcmeShRenewer", FakeRenewer)
    assert await renew.issue_certificate("example.com", ["www.example.com"], "dns") == "cert"
    assert called == {"domain": "example.com", "sans": ["www.example.com"], "provider": "dns"}


@pytest.mark.asyncio
async def test_renew_certificate_delegates_main_domain_and_force(monkeypatch):
    called = {}
    class FakeRenewer:
        def renew(self, *args, **kwargs):
            called["args"] = args
            called["kwargs"] = kwargs
            return "renewed"
    monkeypatch.setattr(renew, "AcmeShRenewer", FakeRenewer)
    assert await renew.renew_certificate("www.example.com", "dns", True, main_domain="example.com") == "renewed"
    assert called["args"] == ("www.example.com", "dns", True)
    assert called["kwargs"] == {"main_domain": "example.com"}


@pytest.mark.asyncio
async def test_load_existing_manual_policy_uses_manual_loader(monkeypatch):
    class FakePolicy:
        source = "manual"
    monkeypatch.setattr(renew, "load_raw_config", lambda: SimpleNamespace())
    monkeypatch.setattr(renew, "get_certificate_policy", lambda config, domain: FakePolicy())
    monkeypatch.setattr(renew, "load_manual_certificate", lambda domain, policy: f"manual:{domain}")
    assert await renew.load_existing_certificate("example.com") == "manual:example.com"


@pytest.mark.asyncio
async def test_resolve_renew_domain_uses_renewer(monkeypatch):
    class FakeRenewer:
        def resolve_main_domain(self, domain):
            return "example.com"
    monkeypatch.setattr(renew, "AcmeShRenewer", FakeRenewer)
    assert await renew.resolve_renew_domain("www.example.com") == "example.com"
