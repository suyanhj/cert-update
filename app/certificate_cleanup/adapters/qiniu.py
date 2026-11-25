"""七牛 Fusion SSL 证书库严格清理适配器。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from app.certificate_cleanup.base import (
    AssociationCheck,
    AssociationStatus,
    CertificateCleanupAdapter,
    CertificateSource,
    CertificateStoreId,
    DeleteOutcome,
    DeleteReceipt,
    IncompleteInventoryError,
    RemoteCertificate,
)
from app.deploys.qiniu import QiniuDeployer
from app.utils.qiniu_api import (
    QiniuAPIError,
    list_qiniu_bucket_domains,
    list_qiniu_buckets,
    list_qiniu_domains,
)


_IN_USE_CODES = {"400326", "400611", "400911"}


def _parse_qiniu_time(value: Any) -> datetime:
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000
        return datetime.fromtimestamp(number, tz=timezone.utc)
    text = str(value or "").strip()
    if not text:
        raise IncompleteInventoryError("七牛证书缺少到期时间")
    try:
        number = float(text)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise IncompleteInventoryError(f"七牛证书到期时间格式未知: {text}") from exc
        if parsed.tzinfo is None:
            raise IncompleteInventoryError("七牛证书到期时间缺少时区")
        return parsed.astimezone(timezone.utc)
    if number > 10_000_000_000:
        number /= 1000
    return datetime.fromtimestamp(number, tz=timezone.utc)


def _qiniu_cert_id_from_domain(item: dict[str, Any]) -> str:
    https = item.get("https")
    if not isinstance(https, dict):
        return ""
    return str(
        https.get("certId") or https.get("certID") or https.get("certid") or ""
    ).strip()


class QiniuFusionCleanupAdapter(CertificateCleanupAdapter):
    """七牛 Fusion SSL 证书库及 CDN/Kodo 域名关联盘点。"""

    safe_delete = True

    def __init__(
        self,
        provider_name: str,
        access_key_id: str = "",
        access_key_secret: str = "",
        *,
        deployer: QiniuDeployer | None = None,
    ) -> None:
        self.store_id = CertificateStoreId("qiniu", provider_name, "fusion_ssl")
        self._deployer = deployer or QiniuDeployer(access_key_id, access_key_secret)

    @staticmethod
    def _convert(item: dict[str, Any]) -> RemoteCertificate:
        cert_id = str(item.get("certID") or "").strip()
        if not cert_id:
            raise IncompleteInventoryError("七牛证书项目缺少 certID")
        end_time = item.get("not_after")
        if end_time is None:
            end_time = item.get("notAfter")
        return RemoteCertificate(
            certificate_id=cert_id,
            name=str(item.get("name") or item.get("common_name") or cert_id),
            expires_at=_parse_qiniu_time(end_time),
            source=CertificateSource.UPLOADED,
        )

    async def list_certificates(self):
        return [
            self._convert(item)
            for item in await self._deployer.list_certificates_strict()
        ]

    async def get_certificate(self, certificate_id: str) -> RemoteCertificate | None:
        item = await self._deployer.get_certificate_strict(certificate_id)
        return self._convert(item) if item is not None else None

    async def check_associations(self, certificate_id: str) -> AssociationCheck:
        auth = self._deployer._get_auth()
        domains = await asyncio.to_thread(list_qiniu_domains, auth)
        by_name: dict[str, dict[str, Any]] = {}
        for item in domains:
            name = str(item.get("name") or "").strip().lower()
            if not name:
                raise IncompleteInventoryError("七牛域名列表包含空域名")
            if name in by_name:
                raise IncompleteInventoryError(f"七牛域名列表包含重复域名: {name}")
            by_name[name] = item

        bucket_domain_names: set[str] = set()
        for bucket in await asyncio.to_thread(list_qiniu_buckets, auth):
            bucket_domains = await asyncio.to_thread(
                list_qiniu_bucket_domains,
                auth,
                bucket,
            )
            for domain in bucket_domains:
                normalized = domain.strip().lower()
                if not normalized:
                    raise IncompleteInventoryError(
                        f"七牛 Bucket 域名列表包含空域名: bucket={bucket}"
                    )
                bucket_domain_names.add(normalized)
                if normalized not in by_name:
                    raise IncompleteInventoryError(
                        "七牛 Kodo 域名未出现在可核对证书引用的域名清单: "
                        f"bucket={bucket} domain={domain}"
                    )

        resources: list[str] = []
        for name, item in by_name.items():
            if _qiniu_cert_id_from_domain(item) != certificate_id:
                continue
            product = "kodo" if name in bucket_domain_names else "cdn"
            resources.append(f"{product}:{name}")
        return AssociationCheck(
            status=AssociationStatus.BOUND if resources else AssociationStatus.UNBOUND,
            complete=True,
            resources=tuple(resources),
        )

    async def delete_certificate(self, certificate_id: str) -> DeleteReceipt:
        try:
            await self._deployer.delete_certificate_strict(certificate_id)
        except QiniuAPIError as exc:
            code = str(exc.code or "").strip()
            if exc.status_code == 404 or code in {"404", "612"}:
                return DeleteReceipt(DeleteOutcome.NOT_FOUND, message=code)
            if code in _IN_USE_CODES:
                return DeleteReceipt(DeleteOutcome.IN_USE, message=code)
            raise
        return DeleteReceipt(DeleteOutcome.DELETED)
