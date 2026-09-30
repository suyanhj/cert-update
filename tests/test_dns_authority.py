"""公网权威 DNS 查询与人工覆盖配置测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.config import AppConfig
from app.utils import dns_authority


def test_dns_authority_resolver_normalizes_nameservers(monkeypatch):
    captured = {}

    def fake_resolve(domain, record_type, **kwargs):
        captured.update({"domain": domain, "record_type": record_type, **kwargs})
        return ["ABBY.NS.CLOUDFLARE.COM.", "bob.ns.cloudflare.com."]

    monkeypatch.setattr(dns_authority.dns.resolver, "resolve", fake_resolve)

    resolver = dns_authority.DnsAuthorityResolver(lifetime_seconds=1.5)

    assert resolver.resolve_nameservers("example.com") == {
        "abby.ns.cloudflare.com",
        "bob.ns.cloudflare.com",
    }
    assert captured == {
        "domain": "example.com",
        "record_type": "NS",
        "lifetime": 1.5,
        "search": False,
    }


def test_normalize_nameservers_accepts_single_string():
    assert dns_authority.normalize_nameservers("NS1.EXAMPLE.COM.") == {
        "ns1.example.com"
    }


def test_domain_dns_overrides_are_normalized():
    config = AppConfig(
        acme={},
        domain_dns_overrides={" Example.COM. ": " cf-main "},
    )

    assert config.domain_dns_overrides == {"example.com": "cf-main"}


@pytest.mark.parametrize(
    "value",
    ["cf-main", {"": "cf-main"}, {"example.com": "  "}],
)
def test_domain_dns_overrides_reject_invalid_values(value):
    with pytest.raises(ValidationError, match="domain_dns_overrides"):
        AppConfig(acme={}, domain_dns_overrides=value)
