from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.config import load_raw_config
from app.providers import (
    AliyunCloudProvider,
    HuaweiCloudProvider,
    QiniuCloudProvider,
    TencentCloudProvider,
    VolcengineCloudProvider,
)
from app.schemas.provider import CloudProductBinding, binding_from_cache_dict
from app.services.provider_factory import build_cloud_providers, get_provider_builder_map
from app.utils.logger import get_logger
from app.utils.store import ProductBindingStore
from app.utils.time import TimeUtil


LOGGER = get_logger("app")


class BindingCacheService:
    """云产品绑定扫描与缓存服务（只做发现与落盘，不做部署）。"""

    def __init__(self) -> None:
        self.conf = load_raw_config()
        self._binding_store = ProductBindingStore()

    @staticmethod
    def _scan_enabled(cloud_scan_conf, key: str, default: bool) -> bool:
        """读取 product_scan 开关，避免 Box 缺失字段被当成 truthy 子对象。"""
        if hasattr(cloud_scan_conf, "get"):
            raw = cloud_scan_conf.get(key, default)
        else:
            raw = getattr(cloud_scan_conf, key, default)
        if raw is None or raw == "":
            return default
        return bool(raw)

    def _binding_cache_settings(self) -> tuple[bool, int]:
        cache_conf = self.conf.deploy.binding_cache
        if not cache_conf:
            return True, 3600

        enabled = bool(cache_conf.enabled) if cache_conf.enabled != "" else True
        ttl_raw = cache_conf.ttl_seconds
        if ttl_raw is None or ttl_raw == "":
            ttl_seconds = 3600
        else:
            ttl_seconds = int(ttl_raw)

        if ttl_seconds <= 0:
            raise ValueError("deploy.binding_cache.ttl_seconds 必须大于 0")
        return enabled, ttl_seconds

    def _build_cloud_providers(self):
        provider_builder = get_provider_builder_map(
            include_custom=False,
            overrides={
                "aliyun": AliyunCloudProvider,
                "tencent": TencentCloudProvider,
                "huawei": HuaweiCloudProvider,
                "volcengine": VolcengineCloudProvider,
                "qiniu": QiniuCloudProvider,
            },
        )
        configs = list(self.conf.providers)
        return build_cloud_providers(configs, provider_builder)

    async def _invoke_provider_binding_method(
        self,
        provider,
        method_name: str,
    ) -> List[CloudProductBinding]:
        method = getattr(provider, method_name, None)
        if method is None:
            return []
        return await method()

    async def _fetch_product_bindings_safe(
        self,
        provider,
        provider_type: str,
        product_label: str,
        enabled: bool,
        method_name: str,
    ) -> List[CloudProductBinding]:
        """按产品类型扫描绑定；单个产品失败只记 warning，不影响同 provider 其他产品。"""
        if not enabled:
            LOGGER.info(
                "product_scan: 跳过 %s 扫描 provider=%s type=%s",
                product_label,
                provider.name,
                provider_type,
            )
            return []

        try:
            items = await self._invoke_provider_binding_method(provider, method_name)
            return items or []
        except Exception as exc:
            LOGGER.warning(
                "[%s] %s 绑定扫描失败，跳过该产品: %s",
                provider.name,
                product_label,
                exc,
            )
            return []

    async def _scan_provider_bindings(self, provider) -> List[CloudProductBinding]:
        """直接调用云厂商 API 扫描绑定（不读缓存）。"""
        provider_type = str(getattr(provider.config, "type", "") or "").strip().lower()
        scan_conf = getattr(self.conf, "product_scan", None)
        if not scan_conf:
            return []
        cloud_scan_conf = getattr(scan_conf, provider_type, None)
        if cloud_scan_conf is None:
            return []

        bindings: List[CloudProductBinding] = []
        scan_tasks = [
            ("CDN", "cdn_scan", True, "get_cdn_bindings"),
            ("云直播", "live_scan", False, "get_live_bindings"),
            ("LB", "lb_scan", True, "get_lb_bindings"),
            ("OSS", "oss_scan", True, "get_oss_bindings"),
            ("WAF", "waf_scan", False, "get_waf_bindings"),
        ]
        for product_label, conf_key, default_enabled, method_name in scan_tasks:
            items = await self._fetch_product_bindings_safe(
                provider,
                provider_type,
                product_label,
                self._scan_enabled(cloud_scan_conf, conf_key, default_enabled),
                method_name,
            )
            bindings.extend(items)

        return bindings

    async def refresh_binding_cache(self, provider_name: Optional[str] = None) -> Dict[str, Any]:
        """
        强制刷新各云厂商的产品绑定缓存。

        只负责“扫描 + 落盘”，不做任何部署相关逻辑。
        """
        cache_enabled, ttl_seconds = self._binding_cache_settings()
        if not cache_enabled:
            LOGGER.info("deploy.binding_cache.enabled=false，跳过缓存刷新")
            return {
                "cache_enabled": False,
                "target_provider_count": 0,
                "refreshed_provider_count": 0,
                "total_bindings": 0,
            }

        providers = self._build_cloud_providers()
        if provider_name:
            providers = [p for p in providers if p.name == provider_name]
            if not providers:
                LOGGER.warning("未找到指定 provider: %s", provider_name)
                return {
                    "cache_enabled": True,
                    "target_provider_count": 0,
                    "refreshed_provider_count": 0,
                    "total_bindings": 0,
                }

        total_bindings = 0
        refreshed_count = 0
        failed_providers: List[Dict[str, str]] = []
        for provider in providers:
            provider_type = str(getattr(provider.config, "type", "") or "").strip().lower()
            # 腾讯云 apply 部署改走上传证书后的 SSL Host 扫描，不再维护本地产品绑定缓存。
            if provider_type == "tencent":
                LOGGER.info(
                    "[%s] 跳过腾讯产品绑定缓存扫描（部署走 SSL cert_id 扫描）",
                    provider.name,
                )
                continue
            provider_key = f"{provider.config.type}:{provider.name}"
            try:
                bindings = await self._scan_provider_bindings(provider)
                total_bindings += len(bindings)
                refreshed_count += 1

                # 落盘到 ProductBindingStore（仅保存匹配需要的极简字段）
                payload: List[Dict[str, Any]] = []
                for item in bindings:
                    record: Dict[str, Any] = {
                        "product_type": item.product_type,
                        "product_id": item.product_id,
                        "domain": item.domain,
                    }
                    if item.metadata:
                        record["metadata"] = item.metadata
                    payload.append(record)
                updated_at = TimeUtil.now_utc().isoformat(timespec="seconds")
                self._binding_store.save_provider(provider_key, payload, updated_at)
            except Exception as exc:
                LOGGER.warning("[%s] 绑定缓存刷新失败，跳过: %s", provider.name, exc)
                failed_providers.append(
                    {
                        "provider": provider.name,
                        "provider_type": str(provider.config.type or ""),
                        "error": str(exc),
                    }
                )

        result = {
            "cache_enabled": True,
            "target_provider_count": len(providers),
            "refreshed_provider_count": refreshed_count,
            "total_bindings": total_bindings,
            "failed_providers": failed_providers,
        }
        LOGGER.info(
            "product binding cache refreshed: providers=%d bindings=%d",
            result["refreshed_provider_count"],
            result["total_bindings"],
        )
        return result

