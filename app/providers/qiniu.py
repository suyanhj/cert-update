"""七牛云 CDN 提供器。"""

from __future__ import annotations

from typing import Any, List

from app.schemas.config import ProviderConfig
from app.schemas.provider import CloudProductBinding
from app.utils.logger import get_logger
from app.utils.qiniu_api import (
    build_qiniu_auth,
    list_qiniu_bucket_domains,
    list_qiniu_buckets,
    list_qiniu_domains,
)

from .base import Provider

LOGGER = get_logger("provider")


class QiniuCloudProvider(Provider):
    """通过七牛 CDN Domain API 发现域名与证书绑定目标。"""

    domain_roles = frozenset()

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        self._ak = config.credentials.get("access_key_id", "")
        self._sk = config.credentials.get("access_key_secret", "")
        self._auth = None

    def _get_auth(self):
        if self._auth is None:
            self._auth = build_qiniu_auth(self._ak, self._sk)
        return self._auth

    @staticmethod
    def _is_cdn_domain(item: dict[str, Any]) -> bool:
        name = str(item.get("name") or "").strip()
        product = str(item.get("product") or "cdn").strip().lower()
        return bool(name) and product == "cdn"

    def _list_cdn_domains(self) -> list[dict[str, Any]]:
        return [
            item
            for item in list_qiniu_domains(self._get_auth())
            if self._is_cdn_domain(item)
        ]

    @staticmethod
    def _qiniu_bucket_name(item: dict[str, Any]) -> str:
        source = item.get("source")
        if not isinstance(source, dict):
            return ""
        if str(source.get("sourceType") or "").strip() != "qiniuBucket":
            return ""
        return str(source.get("sourceQiniuBucket") or "").strip()

    def _build_binding(
        self,
        item: dict[str, Any],
        *,
        product_type: str,
        product_id: str,
        bucket: str = "",
    ) -> CloudProductBinding:
        domain_name = str(item["name"]).strip()
        https_conf = item.get("https")
        if not isinstance(https_conf, dict):
            https_conf = {}
        cert_id = str(
            https_conf.get("certId") or https_conf.get("certid") or ""
        ).strip()
        metadata = {
            "type": item.get("type"),
            "status": item.get("operatingState"),
            "cname": item.get("cname"),
            "protocol": str(item.get("protocol") or "").lower(),
            "https": https_conf,
        }
        if bucket:
            metadata["bucket"] = bucket
        return CloudProductBinding(
            provider_name=self.name,
            product_type=product_type,
            product_id=product_id,
            domain=domain_name,
            status=str(item.get("operatingState") or ""),
            cert_id=cert_id or None,
            metadata=metadata,
        )

    async def get_domain_list(self) -> List[dict]:
        def _run() -> List[dict]:
            domains = [
                {
                    "domain": str(item["name"]).strip(),
                    "type": item.get("type"),
                    "status": item.get("operatingState"),
                }
                for item in self._list_cdn_domains()
            ]
            LOGGER.info("[%s] 七牛云发现 %d 个 CDN 域名", self.name, len(domains))
            return domains

        return await self._call_provider_api(
            "获取七牛云 CDN 域名列表",
            _run,
            default=[],
            retries=1,
        )

    async def get_dns_records(self, domain: str) -> List[dict]:
        """七牛 CDN API 不提供 DNS 解析记录。"""
        return []

    async def discover_domains(self) -> List[dict]:
        return await self.get_domain_list()

    async def get_cdn_bindings(self) -> List[CloudProductBinding]:
        def _run() -> List[CloudProductBinding]:
            bindings: List[CloudProductBinding] = []
            for item in self._list_cdn_domains():
                if self._qiniu_bucket_name(item):
                    continue
                domain_name = str(item["name"]).strip()
                bindings.append(
                    self._build_binding(
                        item,
                        product_type="cdn",
                        product_id=domain_name,
                    )
                )

            LOGGER.info("[%s] 七牛云发现 %d 个 CDN 绑定", self.name, len(bindings))
            return bindings

        return await self._call_provider_api(
            "获取七牛云 CDN 绑定",
            _run,
            default=[],
            retries=1,
        )

    async def get_lb_bindings(self) -> List[CloudProductBinding]:
        return []

    async def get_oss_bindings(self) -> List[CloudProductBinding]:
        """扫描 Kodo Bucket，并返回可通过 CDN Domain API 部署的加速域名。"""
        def _run() -> List[CloudProductBinding]:
            cdn_items = {
                str(item["name"]).strip().lower(): item
                for item in self._list_cdn_domains()
                if self._qiniu_bucket_name(item)
            }
            bindings: List[CloudProductBinding] = []
            skipped_source_domains = 0
            for bucket in list_qiniu_buckets(self._get_auth()):
                for domain in list_qiniu_bucket_domains(self._get_auth(), bucket):
                    item = cdn_items.get(domain.lower())
                    if item is None or self._qiniu_bucket_name(item) != bucket:
                        skipped_source_domains += 1
                        continue
                    bindings.append(
                        self._build_binding(
                            item,
                            product_type="oss",
                            product_id=f"{bucket}:{domain}",
                            bucket=bucket,
                        )
                    )

            LOGGER.info(
                "[%s] 七牛云发现 %d 个 Kodo 加速域名，跳过 %d 个原生源站/未确认域名",
                self.name,
                len(bindings),
                skipped_source_domains,
            )
            return bindings

        return await self._call_provider_api(
            "获取七牛云 Kodo 对象存储绑定",
            _run,
            default=[],
            retries=1,
        )
