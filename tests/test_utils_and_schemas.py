"""工具与 schema 单测：domain_match、TimeUtil、provider.binding_from_cache_dict。"""
from datetime import datetime, timedelta

import pytest

from app.schemas.provider import CloudProductBinding, binding_from_cache_dict
from app.utils import domain_match
from app.utils.time import TimeUtil


# ---------- domain_match ----------
def test_normalize_domain_raises_on_empty():
    with pytest.raises(ValueError, match="不能为空"):
        domain_match.normalize_domain("")


def test_normalize_domain_strips_and_lower():
    assert domain_match.normalize_domain("  Example.COM.  ") == "example.com"


def test_build_cert_domains_includes_main_and_sans():
    s = domain_match.build_cert_domains("example.com", ["*.example.com", "api.example.com"])
    assert "example.com" in s
    assert "*.example.com" in s
    assert "api.example.com" in s


def test_match_domain_exact():
    cert_domains = domain_match.build_cert_domains("api.example.com", [])
    assert domain_match.match_domain(cert_domains, "api.example.com") == "exact"


def test_match_domain_wildcard_single():
    cert_domains = domain_match.build_cert_domains("example.com", ["*.example.com"])
    assert domain_match.match_domain(cert_domains, "api.example.com") == "wildcard-single"


def test_match_domain_multi_level_subdomain_not_matched():
    # RFC 6125 §6.4.3：*.example.com 不允许匹配 a.b.example.com
    cert_domains = domain_match.build_cert_domains("example.com", ["*.example.com"])
    assert domain_match.match_domain(cert_domains, "a.b.example.com") is None


def test_match_domain_parent_wildcard_does_not_cover_child_wildcard():
    # 父级泛域名不视为覆盖更深一级的泛域名（线上 *.m.nczx2025.com 事故场景的最小复现）
    cert_domains = domain_match.build_cert_domains("nczx2025.com", ["*.nczx2025.com"])
    assert domain_match.match_domain(cert_domains, "*.m.nczx2025.com") is None


def test_match_domain_signature_rejects_legacy_kwarg():
    # 历史 match_multi_level_subdomain 形参已删除，残留调用方应在解释器层立刻报错
    cert_domains = domain_match.build_cert_domains("example.com", ["*.example.com"])
    with pytest.raises(TypeError):
        domain_match.match_domain(
            cert_domains,
            "a.b.example.com",
            match_multi_level_subdomain=True,  # type: ignore[call-arg]
        )


def test_match_domain_no_match():
    cert_domains = domain_match.build_cert_domains("other.com", [])
    assert domain_match.match_domain(cert_domains, "api.example.com") is None


# ---------- TimeUtil ----------
def test_time_util_parse_iso_utc():
    dt = TimeUtil.parse("2025-01-15T08:00:00Z")
    assert dt.tzinfo is not None
    assert dt.year == 2025 and dt.month == 1 and dt.day == 15


def test_time_util_parse_datetime_passthrough():
    now = datetime.now()
    out = TimeUtil.parse(now)
    assert out.tzinfo is not None
    assert out.year == now.year


def test_time_util_remaining_days_expired():
    past = TimeUtil.now_utc() - timedelta(days=1)
    assert TimeUtil.remaining_days(past) == 0


def test_time_util_remaining_days_future():
    future = TimeUtil.now_utc() + timedelta(days=3)
    assert TimeUtil.remaining_days(future) == 3


def test_time_util_add_days():
    base = TimeUtil.now_utc()
    later = TimeUtil.add_days(base, 7)
    assert (later - base).days == 7


# ---------- provider.binding_from_cache_dict ----------
def test_binding_from_cache_dict_minimal():
    raw = {"product_type": "cdn", "product_id": "p-1", "domain": "api.example.com"}
    b = binding_from_cache_dict(raw)
    assert isinstance(b, CloudProductBinding)
    assert b.product_type == "cdn"
    assert b.product_id == "p-1"
    assert b.domain == "api.example.com"
    assert b.metadata == {}


def test_binding_from_cache_dict_with_metadata():
    raw = {
        "product_type": "cdn",
        "product_id": "p-1",
        "domain": "api.example.com",
        "metadata": {"region": "cn-hangzhou"},
    }
    b = binding_from_cache_dict(raw)
    assert b.metadata == {"region": "cn-hangzhou"}


def test_binding_from_cache_dict_missing_required_raises():
    with pytest.raises(ValueError, match="缺少关键字段"):
        binding_from_cache_dict({"product_type": "cdn"})
    with pytest.raises(ValueError, match="缺少关键字段"):
        binding_from_cache_dict({"domain": "x.com"})


def test_binding_from_cache_dict_invalid_metadata_raises():
    with pytest.raises(ValueError, match="metadata 必须是 dict"):
        binding_from_cache_dict(
            {"product_type": "cdn", "product_id": "p-1", "domain": "x.com", "metadata": "not-dict"}
        )


def test_binding_from_cache_dict_invalid_listener_port_raises():
    with pytest.raises(ValueError, match="listener_port 非法"):
        binding_from_cache_dict(
            {"product_type": "cdn", "product_id": "p-1", "domain": "x.com", "listener_port": "abc"}
        )
