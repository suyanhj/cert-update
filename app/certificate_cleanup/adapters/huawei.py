"""华为云 ELB、WAF 与 CCM 分证书库清理适配器。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from huaweicloudsdkelb.v3 import model as elb_models
from huaweicloudsdkwaf.v1 import model as waf_models
from huaweicloudsdkscm.v3 import model as scm_models

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
from app.utils.huawei_sdk import (
    build_basic_credentials,
    build_elb_client,
    build_scm_client,
    build_waf_client,
)


def _parse_time(value: Any) -> datetime:
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000
        return datetime.fromtimestamp(number, tz=timezone.utc)
    text = str(value or "").strip()
    if not text:
        raise IncompleteInventoryError("华为云证书缺少到期时间")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise IncompleteInventoryError(f"华为云证书到期时间格式未知: {text}") from exc
    if parsed.tzinfo is None:
        raise IncompleteInventoryError("华为云证书到期时间缺少时区")
    return parsed.astimezone(timezone.utc)


def _cert_ref_id(value: Any) -> str:
    text = str(value or "").strip().rstrip("/")
    return text.rsplit("/", 1)[-1] if text else ""


class HuaweiELBCleanupAdapter(CertificateCleanupAdapter):
    """按项目与地域隔离的 ELB 证书库。"""

    safe_delete = True

    def __init__(
        self,
        provider_name: str,
        region: str,
        project_id: str,
        access_key_id: str = "",
        access_key_secret: str = "",
        *,
        client: Any | None = None,
    ) -> None:
        scope = f"{region}/{project_id or 'default'}"
        self.store_id = CertificateStoreId("huawei", provider_name, "elb", scope)
        credentials = build_basic_credentials(access_key_id, access_key_secret, project_id)
        self._client = client or build_elb_client(region=region, credentials=credentials)

    @staticmethod
    def _convert(item: Any, request_id: str = "") -> RemoteCertificate:
        cert_id = str(getattr(item, "id", "") or "").strip()
        if not cert_id:
            raise IncompleteInventoryError("华为 ELB 证书缺少 ID")
        source = (
            CertificateSource.MANAGED
            if str(getattr(item, "source", "") or "").strip().lower() == "scm"
            else CertificateSource.UPLOADED
        )
        return RemoteCertificate(
            certificate_id=cert_id,
            name=str(getattr(item, "name", "") or cert_id),
            expires_at=_parse_time(getattr(item, "expire_time", None)),
            source=source,
            request_id=request_id,
        )

    async def list_certificates(self):
        marker = None
        seen_markers: set[str] = set()
        result: list[RemoteCertificate] = []
        while True:
            req = elb_models.ListCertificatesRequest(limit=100, marker=marker)
            resp = await asyncio.to_thread(self._client.list_certificates, req)
            page = getattr(resp, "certificates", None)
            page_info = getattr(resp, "page_info", None)
            if page is None or page_info is None:
                raise IncompleteInventoryError("华为 ELB 证书分页响应不完整")
            request_id = str(getattr(resp, "request_id", "") or "")
            result.extend(self._convert(item, request_id) for item in page)
            next_marker = str(getattr(page_info, "next_marker", "") or "").strip()
            if not next_marker:
                return result
            if next_marker == marker or next_marker in seen_markers:
                raise IncompleteInventoryError(
                    f"华为 ELB 证书分页 marker 未前进: marker={next_marker}"
                )
            seen_markers.add(next_marker)
            marker = next_marker

    async def get_certificate(self, certificate_id: str) -> RemoteCertificate | None:
        req = elb_models.ShowCertificateRequest(certificate_id=certificate_id)
        try:
            resp = await asyncio.to_thread(self._client.show_certificate, req)
        except Exception as exc:
            if int(getattr(exc, "status_code", 0) or 0) == 404:
                return None
            raise
        item = getattr(resp, "certificate", None)
        if item is None:
            raise IncompleteInventoryError("华为 ELB 证书详情响应不完整")
        return self._convert(item, str(getattr(resp, "request_id", "") or ""))

    async def check_associations(self, certificate_id: str) -> AssociationCheck:
        marker = None
        seen_markers: set[str] = set()
        resources: list[str] = []
        request_ids: list[str] = []
        while True:
            req = elb_models.ListListenersRequest(
                protocol=["HTTPS", "TERMINATED_HTTPS"],
                limit=100,
                marker=marker,
            )
            resp = await asyncio.to_thread(self._client.list_listeners, req)
            listeners = getattr(resp, "listeners", None)
            page_info = getattr(resp, "page_info", None)
            if listeners is None or page_info is None:
                raise IncompleteInventoryError("华为 ELB 监听器分页响应不完整")
            request_ids.append(str(getattr(resp, "request_id", "") or ""))
            for listener in listeners:
                refs = [getattr(listener, "default_tls_container_ref", "")]
                refs.extend(getattr(listener, "sni_container_refs", None) or [])
                if certificate_id in {_cert_ref_id(item) for item in refs}:
                    resources.append(f"listener:{getattr(listener, 'id', '')}")
            next_marker = str(getattr(page_info, "next_marker", "") or "").strip()
            if not next_marker:
                break
            if next_marker == marker or next_marker in seen_markers:
                raise IncompleteInventoryError(
                    f"华为 ELB 监听器分页 marker 未前进: marker={next_marker}"
                )
            seen_markers.add(next_marker)
            marker = next_marker
        return AssociationCheck(
            AssociationStatus.BOUND if resources else AssociationStatus.UNBOUND,
            complete=True,
            resources=tuple(resources),
            request_ids=tuple(item for item in request_ids if item),
        )

    async def delete_certificate(self, certificate_id: str) -> DeleteReceipt:
        req = elb_models.DeleteCertificateRequest(certificate_id=certificate_id)
        try:
            resp = await asyncio.to_thread(self._client.delete_certificate, req)
        except Exception as exc:
            status_code = int(getattr(exc, "status_code", 0) or 0)
            if status_code == 404:
                return DeleteReceipt(DeleteOutcome.NOT_FOUND)
            if status_code == 409:
                return DeleteReceipt(DeleteOutcome.IN_USE, message="409")
            raise
        return DeleteReceipt(
            DeleteOutcome.DELETED,
            request_id=str(getattr(resp, "request_id", "") or ""),
        )


class HuaweiWAFCleanupAdapter(CertificateCleanupAdapter):
    """WAF 证书库仅报告；删除接口的在用保护尚未通过测试门禁。"""

    safe_delete = False

    def __init__(
        self,
        provider_name: str,
        region: str,
        project_id: str,
        enterprise_project_id: str,
        access_key_id: str = "",
        access_key_secret: str = "",
        *,
        client: Any | None = None,
    ) -> None:
        enterprise = enterprise_project_id or "0"
        self.store_id = CertificateStoreId(
            "huawei", provider_name, "waf", f"{region}/{project_id or 'default'}/{enterprise}"
        )
        self._enterprise_project_id = enterprise
        credentials = build_basic_credentials(access_key_id, access_key_secret, project_id)
        self._client = client or build_waf_client(region=region, credentials=credentials)

    @staticmethod
    def _convert(item: Any) -> RemoteCertificate:
        cert_id = str(getattr(item, "id", "") or "").strip()
        if not cert_id:
            raise IncompleteInventoryError("华为 WAF 证书缺少 ID")
        return RemoteCertificate(
            certificate_id=cert_id,
            name=str(getattr(item, "name", "") or cert_id),
            expires_at=_parse_time(getattr(item, "expire_time", None)),
            source=CertificateSource.UPLOADED,
        )

    async def _list_raw(self) -> list[Any]:
        page = 1
        size = 100
        result: list[Any] = []
        expected_total: int | None = None
        while True:
            req = waf_models.ListCertificatesRequest(
                enterprise_project_id=self._enterprise_project_id,
                page=page,
                pagesize=size,
            )
            resp = await asyncio.to_thread(self._client.list_certificates, req)
            items = getattr(resp, "items", None)
            total = getattr(resp, "total", None)
            if items is None or total is None:
                raise IncompleteInventoryError("华为 WAF 证书分页响应不完整")
            if expected_total is not None and int(total) != expected_total:
                raise IncompleteInventoryError("华为 WAF 证书分页总数不稳定")
            expected_total = int(total)
            result.extend(items)
            if len(result) >= expected_total:
                return result
            if not items:
                raise IncompleteInventoryError("华为 WAF 证书分页提前返回空页")
            page += 1

    async def list_certificates(self):
        return [self._convert(item) for item in await self._list_raw()]

    async def get_certificate(self, certificate_id: str) -> RemoteCertificate | None:
        req = waf_models.ShowCertificateRequest(
            enterprise_project_id=self._enterprise_project_id,
            certificate_id=certificate_id,
        )
        try:
            resp = await asyncio.to_thread(self._client.show_certificate, req)
        except Exception as exc:
            if int(getattr(exc, "status_code", 0) or 0) == 404:
                return None
            raise
        return self._convert(resp)

    async def check_associations(self, certificate_id: str) -> AssociationCheck:
        matches = [
            item for item in await self._list_raw()
            if str(getattr(item, "id", "") or "").strip() == certificate_id
        ]
        if len(matches) != 1:
            raise IncompleteInventoryError(
                f"华为 WAF 证书关联查询结果不唯一: certificate_id={certificate_id}"
            )
        hosts = tuple(
            f"host:{getattr(host, 'id', '') or getattr(host, 'hostname', '')}"
            for host in (getattr(matches[0], "bind_host", None) or [])
        )
        return AssociationCheck(
            AssociationStatus.BOUND if hosts else AssociationStatus.UNBOUND,
            complete=True,
            resources=hosts,
        )


class HuaweiCCMCleanupAdapter(CertificateCleanupAdapter):
    """CCM 源证书库仅报告，并使用官方部署资源查询确认关联。"""

    safe_delete = False

    def __init__(
        self,
        provider_name: str,
        region: str,
        project_id: str,
        enterprise_project_id: str,
        access_key_id: str = "",
        access_key_secret: str = "",
        *,
        client: Any | None = None,
    ) -> None:
        enterprise = enterprise_project_id or "0"
        self.store_id = CertificateStoreId(
            "huawei", provider_name, "ccm", f"{region}/{project_id or 'default'}/{enterprise}"
        )
        self._enterprise_project_id = enterprise
        credentials = build_basic_credentials(access_key_id, access_key_secret, project_id)
        self._client = client or build_scm_client(region=region, credentials=credentials)

    @staticmethod
    def _convert(item: Any) -> RemoteCertificate:
        cert_id = str(getattr(item, "id", "") or "").strip()
        if not cert_id:
            raise IncompleteInventoryError("华为 CCM 证书缺少 ID")
        status = str(getattr(item, "status", "") or "").strip().upper()
        source = CertificateSource.UPLOADED if status == "UPLOAD" else CertificateSource.CLOUD_ISSUED
        return RemoteCertificate(
            certificate_id=cert_id,
            name=str(getattr(item, "name", "") or cert_id),
            expires_at=_parse_time(getattr(item, "expire_time", None)),
            source=source,
        )

    async def list_certificates(self):
        limit = 100
        offset = 0
        result: list[RemoteCertificate] = []
        expected_total: int | None = None
        while True:
            req = scm_models.ListCertificatesRequest(
                limit=limit,
                offset=offset,
                enterprise_project_id=self._enterprise_project_id,
                owned_by_self=True,
            )
            resp = await asyncio.to_thread(self._client.list_certificates, req)
            page = getattr(resp, "certificates", None)
            total = getattr(resp, "total_count", None)
            if page is None or total is None:
                raise IncompleteInventoryError("华为 CCM 证书分页响应不完整")
            if expected_total is not None and int(total) != expected_total:
                raise IncompleteInventoryError("华为 CCM 证书分页总数不稳定")
            expected_total = int(total)
            result.extend(self._convert(item) for item in page)
            offset += len(page)
            if offset >= expected_total:
                return result
            if not page:
                raise IncompleteInventoryError("华为 CCM 证书分页提前返回空页")

    async def get_certificate(self, certificate_id: str) -> RemoteCertificate | None:
        req = scm_models.ShowCertificateRequest(certificate_id=certificate_id)
        try:
            resp = await asyncio.to_thread(self._client.show_certificate, req)
        except Exception as exc:
            if int(getattr(exc, "status_code", 0) or 0) == 404:
                return None
            raise
        item = getattr(resp, "certificate", None) or resp
        return self._convert(item)

    async def check_associations(self, certificate_id: str) -> AssociationCheck:
        req = scm_models.ListDeployedResourcesRequest(
            body=scm_models.ListDeployedResourcesRequestBody(
                certificate_ids=[certificate_id],
                service_names=["WAF", "CDN", "ELB"],
            )
        )
        resp = await asyncio.to_thread(self._client.list_deployed_resources, req)
        results = getattr(resp, "results", None)
        matches = [
            item for item in (results or [])
            if str(getattr(item, "certificate_id", "") or "").strip() == certificate_id
        ]
        if results is None or len(matches) != 1:
            raise IncompleteInventoryError(
                f"华为 CCM 部署资源响应不完整: certificate_id={certificate_id}"
            )
        item = matches[0]
        total = getattr(item, "total_num", None)
        details = getattr(item, "deployed_resources", None)
        if total is None or details is None:
            raise IncompleteInventoryError("华为 CCM 部署资源明细不完整")
        resources: list[str] = []
        for detail in details:
            service = str(getattr(detail, "service", "") or "unknown")
            count = getattr(detail, "resource_num", None)
            if count is None:
                raise IncompleteInventoryError("华为 CCM 部署资源数量缺失")
            if int(count) > 0:
                resources.append(f"{service}:{int(count)}")
        if sum(int(item.rsplit(":", 1)[1]) for item in resources) != int(total):
            raise IncompleteInventoryError("华为 CCM 部署资源汇总数量不一致")
        return AssociationCheck(
            AssociationStatus.BOUND if resources else AssociationStatus.UNBOUND,
            complete=True,
            resources=tuple(resources),
        )
