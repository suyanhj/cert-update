"""renew 单测：renew_certificate 空 domain 抛错、mock 续签返回 RenewedCert。"""
import asyncio
from datetime import datetime, timezone

import pytest

from app.schemas.acme import RenewedCert
from app.services.renew import renew_certificate


def test_renew_certificate_empty_domain_raises():
    with pytest.raises(ValueError, match="domain 不能为空"):
        asyncio.run(renew_certificate(""))


def _make_renewed_cert(domain: str = "api.example.com") -> RenewedCert:
    now = datetime.now(timezone.utc)
    return RenewedCert(
        domain=domain,
        cert_path=f"/path/{domain}.pem",
        key_path=f"/path/{domain}.key",
        fullchain_path="",
        ca_path=None,
        cert_pem="",
        key_pem="",
        fullchain_pem="",
        ca_pem="",
        issued_at=now,
        expires_at=now,
        key_length="2048",
        sans=[],
        issuer="",
        profile=None,
    )


def test_renew_certificate_mock_returns_renewed_cert(monkeypatch: pytest.MonkeyPatch):
    from app.services import renew as renew_mod

    expected = _make_renewed_cert()

    class FakeRenewer:
        def renew(self, domain, provider_name, force):
            return expected

    monkeypatch.setattr(renew_mod, "AcmeShRenewer", FakeRenewer)
    result = asyncio.run(renew_certificate("api.example.com"))
    assert result == expected
    assert result.domain == "api.example.com"


def test_prepare_issue_domains_uses_explicit_domains_and_deduplicates():
    from app.services import renew as renew_mod

    assert renew_mod.prepare_issue_domains(
        "EXAMPLE.COM.",
        ["example.com", "*.example.com", "www.example.com", "*.Example.com."],
    ) == [
        "example.com",
        "*.example.com",
        "www.example.com",
    ]


def test_prepare_issue_domains_does_not_restore_removed_wildcard():
    from app.services import renew as renew_mod

    assert renew_mod.prepare_issue_domains("example.com", ["example.com"]) == ["example.com"]


def test_prepare_issue_domains_rejects_wildcard_main():
    from app.services import renew as renew_mod

    with pytest.raises(ValueError, match="主域名不能是通配域名"):
        renew_mod.prepare_issue_domains("*.example.com", ["*.example.com"])


def test_issue_certificate_calls_acme_issue(monkeypatch):
    from app.services import renew as renew_mod

    expected = _make_renewed_cert("example.com")
    calls = []

    class FakeRenewer:
        def issue(self, domain, sans, provider_name):
            calls.append((domain, sans, provider_name))
            return expected

    monkeypatch.setattr(renew_mod, "AcmeShRenewer", FakeRenewer)
    result = asyncio.run(
        renew_mod.issue_certificate(
            "example.com",
            ["*.example.com"],
            "cf-main",
        )
    )

    assert result == expected
    assert calls == [("example.com", ["*.example.com"], "cf-main")]
