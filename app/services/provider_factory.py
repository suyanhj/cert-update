from __future__ import annotations

from typing import Any, Callable

from app.providers import (
    AliyunCloudProvider,
    CloudflareDNSProvider,
    HuaweiCloudProvider,
    QiniuCloudProvider,
    StaticCloudProvider,
    TencentCloudProvider,
    VolcengineCloudProvider,
)


ProviderBuilder = Callable[[Any], Any]
ProviderBuilderMap = dict[str, ProviderBuilder]


def normalize_provider_type(raw: Any) -> str:
    return str(raw or "").strip().lower()


def build_provider(cfg: Any, builder_map: ProviderBuilderMap):
    provider_type = normalize_provider_type(cfg.type)
    builder = builder_map.get(provider_type)
    if not builder:
        raise ValueError(f"{provider_type} 没有匹配的提供器")
    provider = builder(cfg)
    setattr(provider, "_provider_type", provider_type)
    return provider


def build_cloud_providers(configs: list[Any], builder_map: ProviderBuilderMap) -> list[Any]:
    providers: list[Any] = []
    for cfg in configs:
        if not cfg.enabled:
            continue
        provider_type = normalize_provider_type(cfg.type)
        if provider_type == "custom":
            continue
        provider = build_provider(cfg, builder_map)
        providers.append(provider)
    return providers


def get_provider_builder_map(
    include_custom: bool = True,
    overrides: dict[str, ProviderBuilder] | None = None,
) -> ProviderBuilderMap:
    """提供器类型 -> 构造函数的映射，供发现与部署流程使用。"""
    builder_map: ProviderBuilderMap = {
        "tencent": TencentCloudProvider,
        "aliyun": AliyunCloudProvider,
        "huawei": HuaweiCloudProvider,
        "volcengine": VolcengineCloudProvider,
        "qiniu": QiniuCloudProvider,
        "cloudflare": CloudflareDNSProvider,
    }
    if include_custom:
        builder_map["custom"] = StaticCloudProvider
    if overrides:
        for provider_type, builder in overrides.items():
            if provider_type in builder_map and callable(builder):
                builder_map[provider_type] = builder
    return builder_map
