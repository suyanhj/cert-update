"""域名注册商与 DNS 托管商组合矩阵测试。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.domain_discovery import DomainDiscoveryService
from app.utils.store import DomainProviderStore


class _Logger:
    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.errors: list[str] = []
        self.infos: list[str] = []

    def warning(self, message, *args) -> None:
        self.warnings.append(message % args if args else message)

    def error(self, message, *args) -> None:
        self.errors.append(message % args if args else message)

    def info(self, message, *args) -> None:
        self.infos.append(message % args if args else message)

    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


def _provider(name: str, provider_type: str, roles: set[str]):
    return SimpleNamespace(
        name=name,
        config=SimpleNamespace(type=provider_type),
        domain_roles=frozenset(roles),
    )


class _Resolver:
    def __init__(self, nameservers=None, error: Exception | None = None):
        self.nameservers = set(nameservers or [])
        self.error = error
        self.calls: list[str] = []

    def resolve_nameservers(self, domain: str) -> set[str]:
        self.calls.append(domain)
        if self.error:
            raise self.error
        return self.nameservers


@pytest.mark.parametrize(
    (
        "case_name",
        "registrar_name",
        "registrar_type",
        "dns_name",
        "dns_type",
        "same_provider",
        "registration_item_roles",
    ),
    [
        (
            "阿里购买+Cloudflare DNS",
            "ali-main",
            "aliyun",
            "cf-main",
            "cloudflare",
            False,
            None,
        ),
        (
            "阿里购买+阿里 DNS",
            "ali-main",
            "aliyun",
            "ali-main",
            "aliyun",
            True,
            None,
        ),
        (
            "腾讯购买+Cloudflare DNS",
            "tencent-main",
            "tencent",
            "cf-main",
            "cloudflare",
            False,
            ["registration"],
        ),
        (
            "腾讯购买+腾讯 DNS",
            "tencent-main",
            "tencent",
            "tencent-main",
            "tencent",
            True,
            None,
        ),
        (
            "未来其他云购买+其他 DNS 厂商",
            "future-registrar",
            "future-cloud",
            "future-dns",
            "future-dns-cloud",
            False,
            ["registration"],
        ),
    ],
    ids=["aliyun-cf", "aliyun-aliyun", "tencent-cf", "tencent-tencent", "future-generic"],
)
def test_registration_dns_provider_matrix(
    tmp_path,
    case_name,
    registrar_name,
    registrar_type,
    dns_name,
    dns_type,
    same_provider,
    registration_item_roles,
):
    logger = _Logger()
    resolver = _Resolver({"ns.selected.test"})
    service = DomainDiscoveryService(logger, authority_resolver=resolver)
    registrar = _provider(
        registrar_name,
        registrar_type,
        {"registration", "dns"} if registrar_type in {"aliyun", "tencent"} else {"registration"},
    )

    registration_payload = {
        "domain": "Example.COM.",
        "expires_at": "2027-09-16T00:00:00+08:00",
        "org": f"{case_name}注册主体",
        "subs": [{"name": "old.example.com", "status": "在工作"}],
    }
    if registration_item_roles:
        registration_payload["domain_roles"] = registration_item_roles

    registration = service._normalize_domain_group(registrar, registration_payload)
    assert registration is not None
    discovered = [(registrar, registration)]

    if not same_provider:
        dns_provider = _provider(dns_name, dns_type, {"dns"})
        dns = service._normalize_domain_group(
            dns_provider,
            {
                "domain": "example.com",
                "expires_at": "unknown",
                "zone_id": f"{dns_name}-zone",
                "assigned_nameservers": ["NS.SELECTED.TEST."],
                "subs": [{"name": "www.example.com", "status": "在工作"}],
            },
        )
        assert dns is not None
        discovered.append((dns_provider, dns))

    groups = service._merge_domain_groups(discovered)

    assert len(groups) == 1
    group = groups[0]
    assert group["domain"] == "example.com"
    assert group["registrar_name"] == registrar_name
    assert group["registrar_provider"] == registrar_type
    assert group["registrant_org"] == f"{case_name}注册主体"
    assert group["dns_name"] == dns_name
    assert group["dns_provider"] == dns_type
    assert group["name"] == dns_name
    assert group["provider"] == dns_type

    expected_subdomain = "old.example.com" if same_provider else "www.example.com"
    assert group["subs"] == [{"name": expected_subdomain, "status": "在工作"}]

    # 证书签发映射必须始终使用 DNS 托管账号，而不是注册商账号。
    provider_store = DomainProviderStore(str(tmp_path / f"{dns_name}.json"))
    provider_store.save(groups)
    assert provider_store.resolve("example.com") == dns_name
    assert provider_store.resolve(expected_subdomain) == dns_name

    has_multiple_dns_candidates = not same_provider and not registration_item_roles
    assert resolver.calls == (["example.com"] if has_multiple_dns_candidates else [])


@pytest.mark.parametrize("reverse_order", [False, True])
def test_dns_conflict_uses_public_ns_regardless_of_provider_order(tmp_path, reverse_order):
    logger = _Logger()
    service = DomainDiscoveryService(
        logger,
        authority_resolver=_Resolver({"abby.ns.cloudflare.com", "bob.ns.cloudflare.com"}),
    )
    first = _provider("dns-first", "aliyun", {"dns"})
    second = _provider("dns-second", "cloudflare", {"dns"})
    first_item = service._normalize_domain_group(
        first,
        {"domain": "example.com", "subs": [{"name": "first.example.com"}]},
    )
    second_item = service._normalize_domain_group(
        second,
        {
            "domain": "example.com",
            "assigned_nameservers": [
                "BOB.NS.CLOUDFLARE.COM.",
                "abby.ns.cloudflare.com",
            ],
            "subs": [{"name": "second.example.com"}],
        },
    )

    discovered = [(first, first_item), (second, second_item)]
    if reverse_order:
        discovered.reverse()
    groups = service._merge_domain_groups(discovered)

    assert groups[0]["dns_name"] == "dns-second"
    assert groups[0]["subs"] == [{"name": "second.example.com"}]
    assert any(
        "method=public_ns" in message and "selected=dns-second" in message
        for message in logger.infos
    )

    provider_store = DomainProviderStore(str(tmp_path / "conflict.json"))
    provider_store.save(groups)
    assert provider_store.resolve("example.com") == "dns-second"


def test_dns_conflict_manual_override_has_highest_priority():
    logger = _Logger()
    resolver = _Resolver(error=AssertionError("人工覆盖时不应查询公网 NS"))
    service = DomainDiscoveryService(
        logger,
        dns_overrides={"Example.COM.": "dns-first"},
        authority_resolver=resolver,
    )
    first = _provider("dns-first", "aliyun", {"dns"})
    second = _provider("dns-second", "cloudflare", {"dns"})
    items = [
        (first, service._normalize_domain_group(first, {"domain": "example.com"})),
        (second, service._normalize_domain_group(second, {"domain": "example.com"})),
    ]

    groups = service._merge_domain_groups(items)

    assert groups[0]["dns_name"] == "dns-first"
    assert resolver.calls == []
    assert any("method=override" in message for message in logger.infos)


def test_dns_conflict_falls_back_to_unique_cloudflare_strong_evidence():
    logger = _Logger()
    service = DomainDiscoveryService(
        logger,
        authority_resolver=_Resolver(error=RuntimeError("DNS timeout")),
    )
    aliyun = _provider("ali-main", "aliyun", {"dns"})
    cloudflare = _provider("cf-main", "cloudflare", {"dns"})
    items = [
        (aliyun, service._normalize_domain_group(aliyun, {"domain": "example.com"})),
        (
            cloudflare,
            service._normalize_domain_group(
                cloudflare,
                {"domain": "example.com", "dns_authoritative": True},
            ),
        ),
    ]

    groups = service._merge_domain_groups(items)

    assert groups[0]["dns_name"] == "cf-main"
    assert any("method=provider_evidence" in message for message in logger.warnings)


def test_dns_conflict_without_evidence_does_not_write_mapping(tmp_path):
    logger = _Logger()
    service = DomainDiscoveryService(
        logger,
        authority_resolver=_Resolver({"ns.other-provider.test"}),
    )
    aliyun = _provider("ali-main", "aliyun", {"registration", "dns"})
    cloudflare = _provider("cf-main", "cloudflare", {"dns"})
    items = [
        (
            aliyun,
            service._normalize_domain_group(
                aliyun,
                {"domain": "example.com", "expires_at": "2027-01-01", "org": "示例公司"},
            ),
        ),
        (
            cloudflare,
            service._normalize_domain_group(
                cloudflare,
                {"domain": "example.com", "dns_authoritative": True},
            ),
        ),
    ]

    groups = service._merge_domain_groups(items)

    assert groups[0]["registrar_name"] == "ali-main"
    assert groups[0]["dns_name"] == ""
    assert groups[0]["subs"] == []
    assert any("无法确认" in message for message in logger.errors)

    provider_store = DomainProviderStore(str(tmp_path / "unresolved.json"))
    provider_store.save(groups)
    assert provider_store.resolve("example.com") is None


def test_dns_conflict_rejects_invalid_manual_override():
    service = DomainDiscoveryService(
        _Logger(),
        dns_overrides={"example.com": "missing-provider"},
        authority_resolver=_Resolver(),
    )
    first = _provider("dns-first", "aliyun", {"dns"})
    second = _provider("dns-second", "cloudflare", {"dns"})
    items = [
        (first, service._normalize_domain_group(first, {"domain": "example.com"})),
        (second, service._normalize_domain_group(second, {"domain": "example.com"})),
    ]

    with pytest.raises(ValueError, match="未匹配唯一 DNS Provider"):
        service._merge_domain_groups(items)


def test_manual_override_does_not_silently_accept_different_single_candidate():
    service = DomainDiscoveryService(
        _Logger(),
        dns_overrides={"example.com": "cf-main"},
        authority_resolver=_Resolver(),
    )
    aliyun = _provider("ali-main", "aliyun", {"dns"})
    item = service._normalize_domain_group(aliyun, {"domain": "example.com"})

    with pytest.raises(ValueError, match="override=cf-main"):
        service._merge_domain_groups([(aliyun, item)])
