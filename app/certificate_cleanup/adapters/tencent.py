"""腾讯云 SSL 证书库严格清理适配器。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import Any

from tencentcloud.ssl.v20191205 import models as ssl_models

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
from app.utils.logger import get_logger
from app.utils.tencent_sdk import SslClient, build_tencent_client


LOGGER = get_logger("app.certificate_cleanup.tencent")
_CHINA_TZ = timezone(timedelta(hours=8))


def _parse_tencent_time(value: Any) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise IncompleteInventoryError("腾讯云证书缺少到期时间")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_CHINA_TZ)
        except ValueError as exc:
            raise IncompleteInventoryError(f"腾讯云证书到期时间格式未知: {text}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_CHINA_TZ)
    return parsed.astimezone(timezone.utc)


def _tencent_source(item: Any) -> CertificateSource:
    source = str(getattr(item, "From", "") or "").strip().lower()
    if source == "upload":
        return CertificateSource.UPLOADED
    if bool(getattr(item, "IsPackage", False)):
        return CertificateSource.PURCHASED
    if source in {"buy", "trustasia", "wosign", "sheca"}:
        return CertificateSource.CLOUD_ISSUED
    return CertificateSource.UNKNOWN


def _exception_code(exc: Exception) -> str:
    getter = getattr(exc, "get_code", None)
    if callable(getter):
        return str(getter() or "").strip()
    return str(getattr(exc, "code", "") or "").strip()


class TencentSSLCleanupAdapter(CertificateCleanupAdapter):
    """腾讯云全局 SSL 证书库；关联检查和删除均使用官方异步任务。"""

    safe_delete = True

    def __init__(
        self,
        provider_name: str,
        secret_id: str = "",
        secret_key: str = "",
        region: str = "ap-guangzhou",
        *,
        client: Any | None = None,
        page_size: int = 200,
        poll_interval_seconds: float = 2.0,
        poll_timeout_seconds: float = 300.0,
    ) -> None:
        self.store_id = CertificateStoreId("tencent", provider_name, "ssl")
        self._client = client or build_tencent_client(
            secret_id=secret_id,
            secret_key=secret_key,
            client_cls=SslClient,
            region=region,
        )
        self._page_size = max(1, min(1000, int(page_size)))
        self._poll_interval_seconds = max(0.0, float(poll_interval_seconds))
        self._poll_timeout_seconds = max(0.1, float(poll_timeout_seconds))

    @staticmethod
    def _convert(item: Any, request_id: str = "") -> RemoteCertificate:
        certificate_id = str(getattr(item, "CertificateId", "") or "").strip()
        if not certificate_id:
            raise IncompleteInventoryError("腾讯云证书列表项缺少 CertificateId")
        source = _tencent_source(item)
        return RemoteCertificate(
            certificate_id=certificate_id,
            name=str(getattr(item, "Alias", "") or getattr(item, "Domain", "") or certificate_id),
            expires_at=_parse_tencent_time(getattr(item, "CertEndTime", None)),
            source=source,
            request_id=request_id,
        )

    async def list_certificates(self):
        offset = 0
        expected_total: int | None = None
        certificates: list[RemoteCertificate] = []
        while True:
            req = ssl_models.DescribeCertificatesRequest()
            req.Offset = offset
            req.Limit = self._page_size
            resp = await asyncio.to_thread(self._client.DescribeCertificates, req)
            request_id = str(getattr(resp, "RequestId", "") or "").strip()
            total_raw = getattr(resp, "TotalCount", None)
            page = getattr(resp, "Certificates", None)
            if total_raw is None or page is None:
                raise IncompleteInventoryError(
                    f"腾讯云证书分页响应不完整: offset={offset} request_id={request_id or '-'}"
                )
            total = int(total_raw)
            if total < 0 or (expected_total is not None and total != expected_total):
                raise IncompleteInventoryError("腾讯云证书分页 TotalCount 不稳定")
            expected_total = total
            if not isinstance(page, list):
                raise IncompleteInventoryError("腾讯云证书分页 Certificates 不是数组")
            if not page and offset < total:
                raise IncompleteInventoryError(
                    f"腾讯云证书分页提前返回空页: offset={offset} total={total}"
                )
            converted = [self._convert(item, request_id) for item in page]
            known_ids = {item.certificate_id for item in certificates}
            repeated_ids = [
                item.certificate_id for item in converted if item.certificate_id in known_ids
            ]
            if repeated_ids:
                raise IncompleteInventoryError(
                    f"腾讯云证书分页返回重复证书: certificate_id={repeated_ids[0]}"
                )
            certificates.extend(converted)
            offset += len(page)
            if offset >= total:
                break
            if len(page) == 0:
                raise IncompleteInventoryError("腾讯云证书分页未前进")
        if len(certificates) != (expected_total or 0):
            raise IncompleteInventoryError(
                f"腾讯云证书分页数量不一致: expected={expected_total} actual={len(certificates)}"
            )
        LOGGER.info(
            "腾讯云 SSL 证书严格分页完成: provider=%s count=%d",
            self.store_id.provider_name,
            len(certificates),
        )
        return certificates

    async def get_certificate(self, certificate_id: str) -> RemoteCertificate | None:
        req = ssl_models.DescribeCertificatesRequest()
        req.Offset = 0
        req.Limit = 10
        req.CertIds = [certificate_id]
        try:
            resp = await asyncio.to_thread(self._client.DescribeCertificates, req)
        except Exception as exc:
            if _exception_code(exc) in {"ResourceNotFound", "InvalidParameter.CertificateNotFound"}:
                return None
            raise
        page = getattr(resp, "Certificates", None)
        total = getattr(resp, "TotalCount", None)
        if page is None or total is None:
            raise IncompleteInventoryError("腾讯云证书实时详情响应不完整")
        matches = [
            item
            for item in page
            if str(getattr(item, "CertificateId", "") or "").strip() == certificate_id
        ]
        if int(total) == 0 and not matches:
            return None
        if int(total) != 1 or len(matches) != 1:
            raise IncompleteInventoryError(
                f"腾讯云证书实时查询结果不唯一: certificate_id={certificate_id} total={total}"
            )
        return self._convert(matches[0], str(getattr(resp, "RequestId", "") or ""))

    async def check_associations(self, certificate_id: str) -> AssociationCheck:
        create_req = ssl_models.CreateCertificateBindResourceSyncTaskRequest()
        create_req.CertificateIds = [certificate_id]
        create_req.IsCache = 0
        create_resp = await asyncio.to_thread(
            self._client.CreateCertificateBindResourceSyncTask,
            create_req,
        )
        request_ids = [str(getattr(create_resp, "RequestId", "") or "")]
        tasks = getattr(create_resp, "CertTaskIds", None)
        matching = [
            item
            for item in (tasks or [])
            if str(getattr(item, "CertId", "") or "").strip() == certificate_id
            and str(getattr(item, "TaskId", "") or "").strip()
        ]
        if tasks is None or len(matching) != 1:
            raise IncompleteInventoryError(
                f"腾讯云关联同步任务响应不完整: certificate_id={certificate_id}"
            )
        task_id = str(matching[0].TaskId).strip()
        deadline = monotonic() + self._poll_timeout_seconds
        while True:
            result_req = ssl_models.DescribeCertificateBindResourceTaskResultRequest()
            result_req.TaskIds = [task_id]
            result_resp = await asyncio.to_thread(
                self._client.DescribeCertificateBindResourceTaskResult,
                result_req,
            )
            request_ids.append(str(getattr(result_resp, "RequestId", "") or ""))
            results = getattr(result_resp, "SyncTaskBindResourceResult", None)
            matching_results = [
                item
                for item in (results or [])
                if str(getattr(item, "TaskId", "") or "").strip() == task_id
            ]
            if results is None or len(matching_results) != 1:
                raise IncompleteInventoryError(
                    f"腾讯云关联任务结果不完整: task_id={task_id}"
                )
            task = matching_results[0]
            status = getattr(task, "Status", None)
            if status == 0:
                if monotonic() >= deadline:
                    raise TimeoutError(f"腾讯云关联资源任务超时: task_id={task_id}")
                await asyncio.sleep(self._poll_interval_seconds)
                continue
            if status != 1:
                error = getattr(task, "Error", None)
                raise IncompleteInventoryError(
                    f"腾讯云关联资源任务失败: task_id={task_id} status={status} error={error}"
                )
            bindings = getattr(task, "BindResourceResult", None)
            if bindings is None:
                raise IncompleteInventoryError(
                    f"腾讯云关联资源任务缺少结果: task_id={task_id}"
                )
            resources: list[str] = []
            for binding in bindings:
                resource_type = str(getattr(binding, "ResourceType", "") or "").strip()
                regions = getattr(binding, "BindResourceRegionResult", None)
                if not resource_type or regions is None:
                    raise IncompleteInventoryError(
                        f"腾讯云关联资源类型结果不完整: task_id={task_id}"
                    )
                for region_result in regions:
                    region = str(getattr(region_result, "Region", "") or "global").strip()
                    error = str(getattr(region_result, "Error", "") or "").strip()
                    total = getattr(region_result, "TotalCount", None)
                    if error or total is None or int(total) < 0:
                        raise IncompleteInventoryError(
                            "腾讯云关联资源地域查询失败: "
                            f"task_id={task_id} type={resource_type} region={region} error={error or '-'}"
                        )
                    if int(total) > 0:
                        resources.append(f"{resource_type}:{region}:{int(total)}")
            return AssociationCheck(
                status=AssociationStatus.BOUND if resources else AssociationStatus.UNBOUND,
                complete=True,
                resources=tuple(resources),
                request_ids=tuple(item for item in request_ids if item),
            )

    async def delete_certificate(self, certificate_id: str) -> DeleteReceipt:
        req = ssl_models.DeleteCertificateRequest()
        req.CertificateId = certificate_id
        req.IsCheckResource = True
        try:
            resp = await asyncio.to_thread(self._client.DeleteCertificate, req)
        except Exception as exc:
            code = _exception_code(exc)
            if "NotFound" in code:
                return DeleteReceipt(DeleteOutcome.NOT_FOUND, message=code)
            if "InUse" in code or "Resource" in code and "Used" in code:
                return DeleteReceipt(DeleteOutcome.IN_USE, message=code)
            raise
        task_id = str(getattr(resp, "TaskId", "") or "").strip()
        request_id = str(getattr(resp, "RequestId", "") or "").strip()
        if not task_id:
            raise IncompleteInventoryError(
                f"腾讯云安全删除未返回异步任务 ID: certificate_id={certificate_id}"
            )
        deadline = monotonic() + self._poll_timeout_seconds
        while True:
            result_req = ssl_models.DescribeDeleteCertificatesTaskResultRequest()
            result_req.TaskIds = [task_id]
            result_resp = await asyncio.to_thread(
                self._client.DescribeDeleteCertificatesTaskResult,
                result_req,
            )
            poll_request_id = str(getattr(result_resp, "RequestId", "") or "").strip()
            results = getattr(result_resp, "DeleteTaskResult", None)
            matching = [
                item
                for item in (results or [])
                if str(getattr(item, "TaskId", "") or "").strip() == task_id
                and str(getattr(item, "CertId", "") or "").strip() == certificate_id
            ]
            if results is None or len(matching) != 1:
                raise IncompleteInventoryError(
                    f"腾讯云删除任务结果不完整: task_id={task_id}"
                )
            result = matching[0]
            status = getattr(result, "Status", None)
            if status == 0:
                if monotonic() >= deadline:
                    raise TimeoutError(f"腾讯云删除证书任务超时: task_id={task_id}")
                await asyncio.sleep(self._poll_interval_seconds)
                continue
            if status == 1:
                return DeleteReceipt(
                    DeleteOutcome.DELETED,
                    request_id=poll_request_id or request_id,
                    task_id=task_id,
                )
            error = str(getattr(result, "Error", "") or "").strip()
            domains = tuple(getattr(result, "Domains", None) or ())
            if domains or any(word in error.lower() for word in ("in use", "resource", "关联", "使用")):
                return DeleteReceipt(
                    DeleteOutcome.IN_USE,
                    request_id=poll_request_id or request_id,
                    task_id=task_id,
                    message=error,
                )
            raise RuntimeError(
                f"腾讯云删除证书任务失败: task_id={task_id} status={status} error={error or '-'}"
            )
