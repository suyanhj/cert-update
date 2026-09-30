"""provider_factory 单测：normalize、build_provider、build_cloud_providers、get_provider_builder_map。"""
from box import Box

import pytest

from app.services.provider_factory import (
    build_cloud_providers,
    build_provider,
    get_provider_builder_map,
    normalize_provider_type,
)


def test_normalize_provider_type():
    assert normalize_provider_type("Aliyun") == "aliyun"
    assert normalize_provider_type("  tencent  ") == "tencent"
    assert normalize_provider_type(None) == ""
    assert normalize_provider_type("") == ""


def test_build_provider_unknown_type_raises():
    builder_map = get_provider_builder_map(include_custom=False)
    cfg = Box({"name": "x", "type": "unknown_cloud", "enabled": True}, default_box=True)
    with pytest.raises(ValueError, match="没有匹配的提供器"):
        build_provider(cfg, builder_map)


def test_build_cloud_providers_skips_disabled():
    builder_map = get_provider_builder_map(include_custom=False)

    class FakeBuilder:
        def __init__(self, cfg):
            self.name = getattr(cfg, "name", "x")

    builder_map["aliyun"] = FakeBuilder
    configs = [
        Box({"name": "a", "type": "aliyun", "enabled": False}, default_box=True),
        Box({"name": "b", "type": "aliyun", "enabled": True}, default_box=True),
    ]
    providers = build_cloud_providers(configs, builder_map)
    assert len(providers) == 1
    assert providers[0].name == "b"


def test_build_cloud_providers_skips_custom():
    builder_map = get_provider_builder_map(include_custom=True)
    configs = [
        Box({"name": "static-1", "type": "custom", "enabled": True}, default_box=True),
    ]
    providers = build_cloud_providers(configs, builder_map)
    assert len(providers) == 0


def test_get_provider_builder_map_include_custom():
    m = get_provider_builder_map(include_custom=True)
    assert "custom" in m
    assert "aliyun" in m
    assert "tencent" in m


def test_get_provider_builder_map_overrides():
    m = get_provider_builder_map(include_custom=False, overrides=None)
    orig_aliyun = m["aliyun"]

    def dummy(_):
        return "dummy"

    m2 = get_provider_builder_map(include_custom=False, overrides={"aliyun": dummy})
    assert m2["aliyun"] is dummy
    assert m["aliyun"] is orig_aliyun
