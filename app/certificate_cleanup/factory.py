"""按已启用 Provider 创建证书清理适配器。"""

from __future__ import annotations

from typing import Any, Callable

from app.certificate_cleanup.adapters.qiniu import QiniuFusionCleanupAdapter
from app.certificate_cleanup.adapters.huawei import (
    HuaweiCCMCleanupAdapter,
    HuaweiELBCleanupAdapter,
    HuaweiWAFCleanupAdapter,
)
from app.certificate_cleanup.adapters.report_only import (
    AliyunCASCleanupAdapter,
    AliyunSLBCleanupAdapter,
    VolcengineCertificateReportAdapter,
)
from app.certificate_cleanup.adapters.tencent import TencentSSLCleanupAdapter
from app.certificate_cleanup.base import (
    AssociationCheck,
    CertificateCleanupAdapter,
    CertificateStoreId,
    InventoryUnavailableError,
    RemoteCertificate,
    classify_inventory_unavailable,
)
from app.certificate_cleanup.store import sanitize_cleanup_text
from app.utils.logger import get_logger


LOGGER = get_logger("app.certificate_cleanup.factory")


class _UnavailableCleanupAdapter(CertificateCleanupAdapter):
    """把单证书库初始化失败转换为可隔离的扫描结果。"""

    safe_delete = False

    def __init__(self, store_id: CertificateStoreId, error: Exception) -> None:
        self.store_id = store_id
        safe_error = sanitize_cleanup_text(str(error))
        reason = classify_inventory_unavailable(error)
        self._error = (
            InventoryUnavailableError(safe_error, reason=reason)
            if reason
            else RuntimeError(safe_error)
        )

    async def list_certificates(self):
        raise self._error

    async def check_associations(self, certificate_id: str) -> AssociationCheck:
        raise self._error

    async def get_certificate(self, certificate_id: str) -> RemoteCertificate | None:
        raise self._error


def _build_store_adapter(
    store_id: CertificateStoreId,
    builder: Callable[[], CertificateCleanupAdapter],
) -> CertificateCleanupAdapter:
    """逐证书库初始化，避免一个产品能力缺失拖垮同账号其他产品。"""
    try:
        return builder()
    except Exception as exc:
        safe_error = sanitize_cleanup_text(str(exc))
        reason = classify_inventory_unavailable(exc)
        if reason:
            LOGGER.warning(
                "云证书库能力不可用，已隔离当前证书库: store=%s reason=%s error=%s",
                store_id.key,
                reason,
                safe_error,
            )
        else:
            LOGGER.error(
                "证书清理适配器初始化失败，已隔离当前证书库: store=%s error_type=%s error=%s",
                store_id.key,
                type(exc).__name__,
                safe_error,
            )
        return _UnavailableCleanupAdapter(store_id, exc)


def _build_provider_adapters(provider: Any) -> list[CertificateCleanupAdapter]:
    provider_type = str(provider.type or "").strip().lower()
    provider_name = str(provider.name or "").strip()
    credentials = dict(provider.credentials)
    if provider_type == "tencent":
        return [
            _build_store_adapter(
                CertificateStoreId("tencent", provider_name, "ssl"),
                lambda: TencentSSLCleanupAdapter(
                    provider_name,
                    secret_id=str(credentials.get("secret_id") or ""),
                    secret_key=str(credentials.get("secret_key") or ""),
                    region=str(credentials.get("region") or "ap-guangzhou"),
                ),
            )
        ]
    if provider_type == "qiniu":
        return [
            _build_store_adapter(
                CertificateStoreId("qiniu", provider_name, "fusion_ssl"),
                lambda: QiniuFusionCleanupAdapter(
                    provider_name,
                    access_key_id=str(credentials.get("access_key_id") or ""),
                    access_key_secret=str(credentials.get("access_key_secret") or ""),
                ),
            )
        ]
    if provider_type == "huawei":
        common = {
            "provider_name": provider_name,
            "region": str(credentials.get("region") or "cn-north-4"),
            "project_id": str(credentials.get("project_id") or ""),
            "access_key_id": str(credentials.get("access_key_id") or ""),
            "access_key_secret": str(credentials.get("access_key_secret") or ""),
        }
        enterprise_project_id = str(credentials.get("enterprise_project_id") or "0")
        region = common["region"]
        project = common["project_id"] or "default"
        return [
            _build_store_adapter(
                CertificateStoreId("huawei", provider_name, "elb", f"{region}/{project}"),
                lambda: HuaweiELBCleanupAdapter(**common),
            ),
            _build_store_adapter(
                CertificateStoreId(
                    "huawei",
                    provider_name,
                    "waf",
                    f"{region}/{project}/{enterprise_project_id}",
                ),
                lambda: HuaweiWAFCleanupAdapter(
                    **common,
                    enterprise_project_id=enterprise_project_id,
                ),
            ),
            _build_store_adapter(
                CertificateStoreId(
                    "huawei",
                    provider_name,
                    "ccm",
                    f"{region}/{project}/{enterprise_project_id}",
                ),
                lambda: HuaweiCCMCleanupAdapter(
                    **common,
                    enterprise_project_id=enterprise_project_id,
                ),
            ),
        ]
    if provider_type == "aliyun":
        common = {
            "provider_name": provider_name,
            "access_key_id": str(credentials.get("access_key_id") or ""),
            "access_key_secret": str(credentials.get("access_key_secret") or ""),
            "region": str(credentials.get("region") or "cn-hangzhou"),
        }
        return [
            _build_store_adapter(
                CertificateStoreId("aliyun", provider_name, "cas"),
                lambda: AliyunCASCleanupAdapter(**common),
            ),
            _build_store_adapter(
                CertificateStoreId("aliyun", provider_name, "slb", common["region"]),
                lambda: AliyunSLBCleanupAdapter(**common),
            ),
        ]
    if provider_type == "volcengine":
        region = str(credentials.get("region") or "cn-north-1")
        return [
            _build_store_adapter(
                CertificateStoreId(
                    "volcengine", provider_name, "certificate_service", region
                ),
                lambda: VolcengineCertificateReportAdapter(
                    provider_name,
                    region=region,
                    access_key_id=str(credentials.get("access_key_id") or ""),
                    access_key_secret=str(credentials.get("access_key_secret") or ""),
                ),
            )
        ]
    if provider_type in {"cloudflare", "custom"}:
        LOGGER.info(
            "跳过无云证书库存清能力的 Provider: type=%s name=%s",
            provider_type,
            provider_name,
        )
        return []
    raise ValueError(f"未知 Provider 类型，无法创建证书清理适配器: {provider_type}")


def build_certificate_cleanup_adapters(conf: Any) -> list[CertificateCleanupAdapter]:
    """Cloudflare/custom 无云证书库；账号初始化失败隔离为单库失败。"""
    adapters: list[CertificateCleanupAdapter] = []
    for provider in conf.providers:
        if not bool(provider.enabled):
            continue
        provider_type = str(provider.type or "").strip().lower()
        provider_name = str(provider.name or "").strip()
        try:
            adapters.extend(_build_provider_adapters(provider))
        except Exception as exc:
            LOGGER.error(
                "证书清理适配器初始化失败，已隔离当前账号: type=%s name=%s error_type=%s",
                provider_type,
                provider_name,
                type(exc).__name__,
            )
            adapters.append(
                _UnavailableCleanupAdapter(
                    CertificateStoreId(
                        provider_type or "unknown",
                        provider_name or "unknown",
                        "adapter_init",
                    ),
                    exc,
                )
            )
    return adapters
