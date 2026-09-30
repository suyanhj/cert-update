import asyncio
from datetime import datetime, timezone

import pytest
from box import Box

from app.services.bindings import BindingCacheService
from app.schemas.provider import CloudProductBinding


class _FixedTimeUtil:
    UTC = timezone.utc

    @staticmethod
    def now_utc():
        return datetime(2026, 3, 16, 8, 0, 0, tzinfo=timezone.utc)


def test_binding_cache_disabled_returns_empty(monkeypatch: pytest.MonkeyPatch):
    """binding_cache.enabled=false 时，refresh_binding_cache 直接返回空结果，不扫描 provider。"""
    cfg = Box(
        {
            "deploy": {"binding_cache": {"enabled": False, "ttl_seconds": 3600}},
            "providers": [],
        },
        default_box=True,
    )

    monkeypatch.setattr("app.services.bindings.load_raw_config", lambda: cfg)

    svc = BindingCacheService()
    result = asyncio.run(svc.refresh_binding_cache())

    assert result["cache_enabled"] is False
    assert result["target_provider_count"] == 0
    assert result["refreshed_provider_count"] == 0
    assert result["total_bindings"] == 0


def test_binding_cache_ttl_must_be_positive(monkeypatch: pytest.MonkeyPatch):
    """binding_cache.ttl_seconds<=0 时应抛 ValueError，避免静默使用非法配置。"""
    cfg = Box(
        {
            "deploy": {"binding_cache": {"enabled": True, "ttl_seconds": 0}},
            "providers": [],
        },
        default_box=True,
    )

    monkeypatch.setattr("app.services.bindings.load_raw_config", lambda: cfg)

    svc = BindingCacheService()
    with pytest.raises(ValueError, match="ttl_seconds 必须大于 0"):
        # 触发 _binding_cache_settings
        asyncio.run(svc.refresh_binding_cache())


def test_refresh_binding_cache_filters_by_provider_name_and_persists(monkeypatch: pytest.MonkeyPatch):
    """refresh_binding_cache(provider_name=...) 只扫描指定 provider 并正确落盘到 ProductBindingStore。"""
    cfg = Box(
        {
            "deploy": {"binding_cache": {"enabled": True, "ttl_seconds": 3600}},
            "product_scan": {
                "aliyun": {"cdn_scan": True, "lb_scan": False, "oss_scan": False},
            },
            "providers": [
                {"name": "ali-1", "type": "aliyun", "enabled": True},
                {"name": "ali-2", "type": "aliyun", "enabled": True},
            ],
        },
        default_box=True,
    )

    monkeypatch.setattr("app.services.bindings.load_raw_config", lambda: cfg)
    monkeypatch.setattr("app.services.bindings.TimeUtil", _FixedTimeUtil)

    class FakeAliyunProvider:
        def __init__(self, cfg):
            self.name = cfg.name
            self.config = cfg

        async def get_cdn_bindings(self):
            return [
                CloudProductBinding(
                    domain="api.example.com",
                    product_type="cdn",
                    product_id="p-1",
                    cert_id="old-1",
                )
            ]

        async def get_lb_bindings(self):
            return []

        async def get_oss_bindings(self):
            return []

    def fake_builder_map(include_custom=False, overrides=None):
        return {"aliyun": FakeAliyunProvider}

    def fake_build_cloud_providers(configs, builder):
        # 根据配置构造两个 provider 实例（须为同步函数，与 bindings._build_cloud_providers 一致）
        return [FakeAliyunProvider(cfg.providers[0]), FakeAliyunProvider(cfg.providers[1])]

    # 覆盖 provider_factory 相关方法
    import app.services.bindings as bindings_mod

    monkeypatch.setattr(bindings_mod, "get_provider_builder_map", fake_builder_map)
    monkeypatch.setattr(bindings_mod, "build_cloud_providers", fake_build_cloud_providers)

    # 捕获 ProductBindingStore.save_provider 调用
    saved = {}

    class FakeProductBindingStore:
        def save_provider(self, provider_key: str, payload, updated_at: str):
            saved["key"] = provider_key
            saved["payload"] = list(payload)
            saved["updated_at"] = updated_at

    monkeypatch.setattr(bindings_mod, "ProductBindingStore", lambda: FakeProductBindingStore())

    svc = BindingCacheService()
    result = asyncio.run(svc.refresh_binding_cache(provider_name="ali-1"))

    # 只应扫描到一个 provider
    assert result["cache_enabled"] is True
    assert result["target_provider_count"] == 1
    assert result["refreshed_provider_count"] == 1
    assert result["total_bindings"] == 1
    assert result["failed_providers"] == []

    # ProductBindingStore 里保存的 key/payload 正确
    assert saved["key"] == "aliyun:ali-1"
    assert saved["payload"][0]["domain"] == "api.example.com"
    assert saved["payload"][0]["product_type"] == "cdn"
    assert saved["payload"][0]["product_id"] == "p-1"
    # updated_at 使用固定时间
    assert saved["updated_at"].startswith("2026-03-16T08:00:00")


def test_refresh_binding_cache_continues_other_products_when_cdn_failed(monkeypatch: pytest.MonkeyPatch):
    """CDN 扫描失败时，同 provider 的 OSS 仍应继续扫描并落盘。"""
    cfg = Box(
        {
            "deploy": {"binding_cache": {"enabled": True, "ttl_seconds": 3600}},
            "product_scan": {
                "aliyun": {"cdn_scan": True, "lb_scan": False, "oss_scan": True},
            },
            "providers": [
                {"name": "mixed", "type": "aliyun", "enabled": True},
            ],
        },
        default_box=True,
    )

    monkeypatch.setattr("app.services.bindings.load_raw_config", lambda: cfg)
    monkeypatch.setattr("app.services.bindings.TimeUtil", _FixedTimeUtil)

    class MixedProvider:
        def __init__(self, cfg):
            self.name = cfg.name
            self.config = cfg

        async def get_cdn_bindings(self):
            raise RuntimeError("cdn scan failed")

        async def get_lb_bindings(self):
            return []

        async def get_oss_bindings(self):
            return [
                CloudProductBinding(
                    domain="oss.example.com",
                    product_type="oss",
                    product_id="bucket-1",
                    cert_id="oss-1",
                )
            ]

    import app.services.bindings as bindings_mod

    monkeypatch.setattr(
        bindings_mod,
        "get_provider_builder_map",
        lambda include_custom=False, overrides=None: {"aliyun": MixedProvider},
    )
    monkeypatch.setattr(
        bindings_mod,
        "build_cloud_providers",
        lambda configs, builder: [MixedProvider(cfg.providers[0])],
    )

    saved = {}

    class FakeProductBindingStore:
        def save_provider(self, provider_key: str, payload, updated_at: str):
            saved["key"] = provider_key
            saved["payload"] = list(payload)

    monkeypatch.setattr(bindings_mod, "ProductBindingStore", lambda: FakeProductBindingStore())

    svc = BindingCacheService()
    result = asyncio.run(svc.refresh_binding_cache())

    assert result["refreshed_provider_count"] == 1
    assert result["failed_providers"] == []
    assert saved["payload"][0]["domain"] == "oss.example.com"


def test_refresh_binding_cache_handles_scan_exception(monkeypatch: pytest.MonkeyPatch):
    """某个 provider 的 CDN 扫描失败时，仅记录 warning，不影响其他 provider 刷新。"""
    cfg = Box(
        {
            "deploy": {"binding_cache": {"enabled": True, "ttl_seconds": 3600}},
            "product_scan": {
                "aliyun": {"cdn_scan": True, "lb_scan": False, "oss_scan": False},
            },
            "providers": [
                {"name": "ok", "type": "aliyun", "enabled": True},
                {"name": "bad", "type": "aliyun", "enabled": True},
            ],
        },
        default_box=True,
    )

    monkeypatch.setattr("app.services.bindings.load_raw_config", lambda: cfg)
    monkeypatch.setattr("app.services.bindings.TimeUtil", _FixedTimeUtil)

    class OkProvider:
        def __init__(self, cfg):
            self.name = cfg.name
            self.config = cfg

        async def get_cdn_bindings(self):
            return [
                CloudProductBinding(
                    domain="ok.example.com",
                    product_type="cdn",
                    product_id="p-ok",
                    cert_id="old-ok",
                )
            ]

        async def get_lb_bindings(self):
            return []

        async def get_oss_bindings(self):
            return []

    class BadProvider:
        def __init__(self, cfg):
            self.name = cfg.name
            self.config = cfg

        async def get_cdn_bindings(self):
            raise RuntimeError("scan failed")

        async def get_lb_bindings(self):
            return []

        async def get_oss_bindings(self):
            return []

    def fake_builder_map(include_custom=False, overrides=None):
        return {"aliyun": OkProvider}

    def fake_build_cloud_providers(configs, builder):
        # 返回一个 ok 和一个 bad provider（须为同步函数）
        return [OkProvider(cfg.providers[0]), BadProvider(cfg.providers[1])]

    import app.services.bindings as bindings_mod

    monkeypatch.setattr(bindings_mod, "get_provider_builder_map", fake_builder_map)
    monkeypatch.setattr(bindings_mod, "build_cloud_providers", fake_build_cloud_providers)

    # 记录 save_provider 调用次数
    class FakeProductBindingStore:
        def __init__(self):
            self.calls = []

        def save_provider(self, provider_key: str, payload, updated_at: str):
            self.calls.append((provider_key, list(payload), updated_at))

    store = FakeProductBindingStore()
    monkeypatch.setattr(bindings_mod, "ProductBindingStore", lambda: store)

    svc = BindingCacheService()
    result = asyncio.run(svc.refresh_binding_cache())

    assert result["target_provider_count"] == 2
    assert result["refreshed_provider_count"] == 2
    assert result["total_bindings"] == 1
    assert result["failed_providers"] == []
    assert len(store.calls) == 2
    ok_call = next(call for call in store.calls if call[0] == "aliyun:ok")
    assert ok_call[1][0]["domain"] == "ok.example.com"
    bad_call = next(call for call in store.calls if call[0] == "aliyun:bad")
    assert bad_call[1] == []

