"""阿里云与火山引擎的严格证书库适配器。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from alibabacloud_cas20200407 import models as cas_models
from alibabacloud_slb20140515 import models as slb_models

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
from app.utils.aliyun_sdk import build_aliyun_deployer_clients, to_cn_region
from app.utils.volcengine_sdk import build_volcengine_service


_CHINA_TZ = timezone(timedelta(hours=8))


def _parse_time(value: Any, *, naive_timezone: timezone = timezone.utc) -> datetime:
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000
        return datetime.fromtimestamp(number, tz=timezone.utc)
    text = str(value or "").strip()
    if not text:
        raise IncompleteInventoryError("证书库存项目缺少到期时间")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise IncompleteInventoryError(f"证书到期时间格式未知: {text}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=naive_timezone)
    return parsed.astimezone(timezone.utc)


class _UnknownAssociationReportAdapter(CertificateCleanupAdapter):
    safe_delete = False

    async def check_associations(self, certificate_id: str) -> AssociationCheck:
        return AssociationCheck(AssociationStatus.UNKNOWN, complete=False)


def _exception_code(error: BaseException) -> str:
    """从 SDK 异常链中取出稳定错误码。"""
    current: BaseException | None = error
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        for name in ("code", "error_code"):
            value = getattr(current, name, None)
            if value:
                return str(value).strip()
        getter = getattr(current, "get_code", None)
        if callable(getter):
            try:
                value = getter()
            except Exception:
                value = None
            if value:
                return str(value).strip()
        current = current.__cause__ or current.__context__
    return ""


class AliyunCASCleanupAdapter(CertificateCleanupAdapter):
    """阿里云 CAS 库；按证书 ID 盘点部署资源，但固定仅报告。"""

    safe_delete = False

    def __init__(
        self,
        provider_name: str,
        access_key_id: str = "",
        access_key_secret: str = "",
        region: str = "cn-hangzhou",
        *,
        client: Any | None = None,
    ) -> None:
        self.store_id = CertificateStoreId("aliyun", provider_name, "cas")
        if client is None:
            clients = build_aliyun_deployer_clients(
                access_key_id=access_key_id,
                access_key_secret=access_key_secret,
                region=to_cn_region(region),
            )
            client = clients["cas_client"]
        self._client = client

    @staticmethod
    def _convert(item: Any) -> RemoteCertificate:
        cert_id = str(getattr(item, "certificate_id", "") or "").strip()
        if not cert_id:
            raise IncompleteInventoryError("阿里云 CAS 证书缺少 certificate_id")
        source = (
            CertificateSource.UPLOADED
            if bool(getattr(item, "upload", False))
            else CertificateSource.CLOUD_ISSUED
        )
        return RemoteCertificate(
            certificate_id=cert_id,
            name=str(getattr(item, "name", "") or getattr(item, "common_name", "") or cert_id),
            expires_at=_parse_time(getattr(item, "end_date", None), naive_timezone=_CHINA_TZ),
            source=source,
        )

    async def list_certificates(self):
        page = 1
        size = 100
        result: list[RemoteCertificate] = []
        expected_total: int | None = None
        while True:
            req = cas_models.ListUserCertificateOrderRequest(
                current_page=page,
                show_size=size,
                order_type="CERT",
                status="EXPIRED",
            )
            resp = await asyncio.to_thread(self._client.list_user_certificate_order, req)
            body = getattr(resp, "body", None)
            items = getattr(body, "certificate_order_list", None) if body else None
            total = getattr(body, "total_count", None) if body else None
            if items is None or total is None:
                raise IncompleteInventoryError("阿里云 CAS 证书分页响应不完整")
            if expected_total is not None and int(total) != expected_total:
                raise IncompleteInventoryError("阿里云 CAS 证书分页总数不稳定")
            expected_total = int(total)
            result.extend(self._convert(item) for item in items)
            if len(result) >= expected_total:
                return result
            if not items:
                raise IncompleteInventoryError("阿里云 CAS 证书分页提前返回空页")
            page += 1

    async def get_certificate(self, certificate_id: str) -> RemoteCertificate | None:
        for item in await self.list_certificates():
            if item.certificate_id == certificate_id:
                return item
        return None

    async def check_associations(self, certificate_id: str) -> AssociationCheck:
        try:
            numeric_id = int(str(certificate_id).strip())
        except (TypeError, ValueError) as exc:
            raise IncompleteInventoryError(
                f"阿里云 CAS 证书 ID 不是整数: {certificate_id}"
            ) from exc

        page = 1
        size = 100
        expected_total: int | None = None
        resources: list[str] = []
        request_ids: list[str] = []
        seen_resources: set[str] = set()
        while True:
            req = cas_models.ListCloudResourcesRequest(
                cloud_name="aliyun",
                cert_ids=[numeric_id],
                current_page=page,
                show_size=size,
            )
            resp = await asyncio.to_thread(self._client.list_cloud_resources, req)
            body = getattr(resp, "body", None)
            items = getattr(body, "data", None) if body else None
            total = getattr(body, "total", None) if body else None
            if items is None or total is None:
                raise IncompleteInventoryError(
                    "阿里云 CAS 部署资源分页响应不完整"
                )
            current_total = int(total)
            if current_total < 0:
                raise IncompleteInventoryError(
                    "阿里云 CAS 部署资源分页总数无效"
                )
            if expected_total is not None and current_total != expected_total:
                raise IncompleteInventoryError(
                    "阿里云 CAS 部署资源分页总数不稳定"
                )
            expected_total = current_total
            request_id = str(getattr(body, "request_id", "") or "").strip()
            if request_id:
                request_ids.append(request_id)

            for item in items:
                item_cert_id = getattr(item, "cert_id", None)
                if item_cert_id is None or int(item_cert_id) != numeric_id:
                    raise IncompleteInventoryError(
                        "阿里云 CAS 部署资源返回了非目标证书"
                    )
                product = str(getattr(item, "cloud_product", "") or "").strip()
                region = str(
                    getattr(item, "region_id", "")
                    or getattr(item, "cloud_region", "")
                    or "global"
                ).strip()
                identity_parts = (
                    str(getattr(item, "instance_id", "") or "").strip(),
                    str(getattr(item, "listener_id", "") or "").strip(),
                    str(getattr(item, "listener_port", "") or "").strip(),
                    str(getattr(item, "domain", "") or "").strip(),
                    str(getattr(item, "id", "") or "").strip(),
                )
                if not product or not any(identity_parts):
                    raise IncompleteInventoryError(
                        "阿里云 CAS 部署资源缺少产品或资源标识"
                    )
                resource = ":".join((product, region, *identity_parts))
                if resource in seen_resources:
                    raise IncompleteInventoryError(
                        f"阿里云 CAS 部署资源分页出现重复项: {resource}"
                    )
                seen_resources.add(resource)
                resources.append(resource)

            if len(resources) == expected_total:
                return AssociationCheck(
                    AssociationStatus.BOUND if resources else AssociationStatus.UNBOUND,
                    complete=True,
                    resources=tuple(resources),
                    request_ids=tuple(request_ids),
                )
            if len(resources) > expected_total:
                raise IncompleteInventoryError(
                    "阿里云 CAS 部署资源分页数量超过总数"
                )
            if not items:
                raise IncompleteInventoryError(
                    "阿里云 CAS 部署资源分页提前返回空页"
                )
            page += 1


class AliyunSLBCleanupAdapter(CertificateCleanupAdapter):
    """阿里云地域化 CLB ServerCertificate 库。"""

    safe_delete = True

    def __init__(
        self,
        provider_name: str,
        region: str,
        access_key_id: str = "",
        access_key_secret: str = "",
        *,
        client: Any | None = None,
    ) -> None:
        self._region = to_cn_region(region)
        self.store_id = CertificateStoreId(
            "aliyun", provider_name, "slb", self._region
        )
        if client is None:
            clients = build_aliyun_deployer_clients(
                access_key_id=access_key_id,
                access_key_secret=access_key_secret,
                region=self._region,
            )
            client = clients["slb_client"]
        self._client = client
        self._association_snapshot: (
            tuple[dict[str, tuple[str, ...]], tuple[str, ...]] | None
        ) = None

    @staticmethod
    def _convert(item: Any, request_id: str = "") -> RemoteCertificate:
        cert_id = str(getattr(item, "server_certificate_id", "") or "").strip()
        if not cert_id:
            raise IncompleteInventoryError("阿里云 SLB 证书缺少 server_certificate_id")
        source = (
            CertificateSource.MANAGED
            if int(getattr(item, "is_ali_cloud_certificate", 0) or 0) == 1
            else CertificateSource.UPLOADED
        )
        expiry = getattr(item, "expire_time_stamp", None)
        if expiry is None:
            expiry = getattr(item, "expire_time", None)
        return RemoteCertificate(
            certificate_id=cert_id,
            name=str(getattr(item, "server_certificate_name", "") or cert_id),
            expires_at=_parse_time(expiry, naive_timezone=_CHINA_TZ),
            source=source,
            request_id=request_id,
        )

    async def list_certificates(self):
        # 每轮证书库扫描必须重新建立关联快照。
        self._association_snapshot = None
        req = slb_models.DescribeServerCertificatesRequest(region_id=self._region)
        resp = await asyncio.to_thread(self._client.describe_server_certificates, req)
        body = getattr(resp, "body", None)
        container = getattr(body, "server_certificates", None) if body else None
        items = getattr(container, "server_certificate", None) if container else None
        if items is None:
            raise IncompleteInventoryError("阿里云 SLB 证书列表响应不完整")
        request_id = str(getattr(body, "request_id", "") or "").strip()
        return [self._convert(item, request_id) for item in items]

    async def get_certificate(self, certificate_id: str) -> RemoteCertificate | None:
        # 删除前详情复查会经过此方法，禁止复用首轮关联快照。
        self._association_snapshot = None
        req = slb_models.DescribeServerCertificatesRequest(
            region_id=self._region,
            server_certificate_id=certificate_id,
        )
        resp = await asyncio.to_thread(self._client.describe_server_certificates, req)
        body = getattr(resp, "body", None)
        container = getattr(body, "server_certificates", None) if body else None
        items = getattr(container, "server_certificate", None) if container else None
        if items is None:
            raise IncompleteInventoryError("阿里云 SLB 证书详情响应不完整")
        if not items:
            return None
        if len(items) != 1:
            raise IncompleteInventoryError(
                f"阿里云 SLB 证书详情返回数量异常: {len(items)}"
            )
        result = self._convert(
            items[0], str(getattr(body, "request_id", "") or "").strip()
        )
        if result.certificate_id != certificate_id:
            raise IncompleteInventoryError("阿里云 SLB 证书详情返回了非目标证书")
        return result

    async def _list_load_balancer_ids(self) -> tuple[list[str], list[str]]:
        page = 1
        size = 100
        expected_total: int | None = None
        load_balancer_ids: list[str] = []
        seen_ids: set[str] = set()
        request_ids: list[str] = []
        while True:
            req = slb_models.DescribeLoadBalancersRequest(
                region_id=self._region,
                page_number=page,
                page_size=size,
            )
            resp = await asyncio.to_thread(self._client.describe_load_balancers, req)
            body = getattr(resp, "body", None)
            container = getattr(body, "load_balancers", None) if body else None
            items = getattr(container, "load_balancer", None) if container else None
            total = getattr(body, "total_count", None) if body else None
            if items is None or total is None:
                raise IncompleteInventoryError(
                    "阿里云 CLB 实例分页响应不完整"
                )
            current_total = int(total)
            if expected_total is not None and current_total != expected_total:
                raise IncompleteInventoryError(
                    "阿里云 CLB 实例分页总数不稳定"
                )
            expected_total = current_total
            request_id = str(getattr(body, "request_id", "") or "").strip()
            if request_id:
                request_ids.append(request_id)
            for item in items:
                load_balancer_id = str(
                    getattr(item, "load_balancer_id", "") or ""
                ).strip()
                if not load_balancer_id:
                    raise IncompleteInventoryError("阿里云 CLB 实例缺少 ID")
                if load_balancer_id in seen_ids:
                    raise IncompleteInventoryError(
                        f"阿里云 CLB 实例分页出现重复项: {load_balancer_id}"
                    )
                seen_ids.add(load_balancer_id)
                load_balancer_ids.append(load_balancer_id)
            if len(load_balancer_ids) == expected_total:
                return load_balancer_ids, request_ids
            if len(load_balancer_ids) > expected_total or not items:
                raise IncompleteInventoryError("阿里云 CLB 实例分页数量不完整")
            page += 1

    async def _list_https_listeners(
        self, load_balancer_id: str
    ) -> tuple[list[Any], list[str]]:
        token = ""
        seen_tokens: set[str] = set()
        seen_listeners: set[str] = set()
        listeners: list[Any] = []
        request_ids: list[str] = []
        expected_total: int | None = None
        while True:
            req = slb_models.DescribeLoadBalancerListenersRequest(
                region_id=self._region,
                load_balancer_id=[load_balancer_id],
                listener_protocol="https",
                max_results=100,
                next_token=token or None,
            )
            resp = await asyncio.to_thread(
                self._client.describe_load_balancer_listeners, req
            )
            body = getattr(resp, "body", None)
            items = getattr(body, "listeners", None) if body else None
            total = getattr(body, "total_count", None) if body else None
            if items is None or total is None:
                raise IncompleteInventoryError(
                    f"阿里云 CLB HTTPS 监听器分页响应不完整: lb={load_balancer_id}"
                )
            current_total = int(total)
            if expected_total is not None and current_total != expected_total:
                raise IncompleteInventoryError(
                    f"阿里云 CLB HTTPS 监听器分页总数不稳定: lb={load_balancer_id}"
                )
            expected_total = current_total
            request_id = str(getattr(body, "request_id", "") or "").strip()
            if request_id:
                request_ids.append(request_id)
            for item in items:
                item_lb_id = str(
                    getattr(item, "load_balancer_id", "") or load_balancer_id
                ).strip()
                port = getattr(item, "listener_port", None)
                if item_lb_id != load_balancer_id or port is None:
                    raise IncompleteInventoryError(
                        f"阿里云 CLB HTTPS 监听器缺少完整标识: lb={load_balancer_id}"
                    )
                identity = f"{item_lb_id}:{int(port)}"
                if identity in seen_listeners:
                    raise IncompleteInventoryError(
                        f"阿里云 CLB HTTPS 监听器分页出现重复项: {identity}"
                    )
                seen_listeners.add(identity)
                listeners.append(item)

            next_token = str(getattr(body, "next_token", "") or "").strip()
            if not next_token:
                if len(listeners) != expected_total:
                    raise IncompleteInventoryError(
                        f"阿里云 CLB HTTPS 监听器分页数量不完整: lb={load_balancer_id}"
                    )
                return listeners, request_ids
            if next_token == token or next_token in seen_tokens:
                raise IncompleteInventoryError(
                    f"阿里云 CLB HTTPS 监听器分页 token 未前进: lb={load_balancer_id}"
                )
            seen_tokens.add(next_token)
            token = next_token

    @staticmethod
    def _listener_certificate_id(listener: Any) -> str:
        https_config = getattr(listener, "httpslistener_config", None)
        certificate_id = (
            getattr(https_config, "server_certificate_id", None)
            if https_config is not None
            else None
        )
        if not certificate_id:
            certificate_id = getattr(listener, "server_certificate_id", None)
        return str(certificate_id or "").strip()

    async def _load_association_snapshot(
        self,
    ) -> tuple[dict[str, tuple[str, ...]], tuple[str, ...]]:
        load_balancer_ids, request_ids = await self._list_load_balancer_ids()
        resources_by_certificate: dict[str, list[str]] = {}
        for load_balancer_id in load_balancer_ids:
            listeners, listener_request_ids = await self._list_https_listeners(
                load_balancer_id
            )
            request_ids.extend(listener_request_ids)
            for listener in listeners:
                listener_port = int(getattr(listener, "listener_port"))
                default_certificate_id = self._listener_certificate_id(listener)
                if not default_certificate_id:
                    raise IncompleteInventoryError(
                        "阿里云 CLB HTTPS 监听器缺少默认证书 ID: "
                        f"lb={load_balancer_id} port={listener_port}"
                    )
                resources_by_certificate.setdefault(
                    default_certificate_id, []
                ).append(
                    f"listener:{self._region}:{load_balancer_id}:{listener_port}"
                )

                req = slb_models.DescribeDomainExtensionsRequest(
                    region_id=self._region,
                    load_balancer_id=load_balancer_id,
                    listener_port=listener_port,
                )
                resp = await asyncio.to_thread(
                    self._client.describe_domain_extensions, req
                )
                body = getattr(resp, "body", None)
                container = getattr(body, "domain_extensions", None) if body else None
                extensions = (
                    getattr(container, "domain_extension", None)
                    if container is not None
                    else None
                )
                if extensions is None:
                    raise IncompleteInventoryError(
                        "阿里云 CLB 扩展域名响应不完整: "
                        f"lb={load_balancer_id} port={listener_port}"
                    )
                request_id = str(getattr(body, "request_id", "") or "").strip()
                if request_id:
                    request_ids.append(request_id)
                for extension in extensions:
                    extension_certificate_id = str(
                        getattr(extension, "server_certificate_id", "") or ""
                    ).strip()
                    extension_id = str(
                        getattr(extension, "domain_extension_id", "") or ""
                    ).strip()
                    domain = str(getattr(extension, "domain", "") or "").strip()
                    if not extension_certificate_id or not (extension_id or domain):
                        raise IncompleteInventoryError(
                            "阿里云 CLB 扩展域名缺少证书或资源标识: "
                            f"lb={load_balancer_id} port={listener_port}"
                        )
                    resources_by_certificate.setdefault(
                        extension_certificate_id, []
                    ).append(
                        "domain_extension:"
                        f"{self._region}:{load_balancer_id}:{listener_port}:"
                        f"{domain or extension_id}"
                    )

        snapshot = (
            {
                cert_id: tuple(cert_resources)
                for cert_id, cert_resources in resources_by_certificate.items()
            },
            tuple(request_ids),
        )
        self._association_snapshot = snapshot
        return snapshot

    async def check_associations(self, certificate_id: str) -> AssociationCheck:
        snapshot = self._association_snapshot
        if snapshot is None:
            snapshot = await self._load_association_snapshot()
        resources_by_certificate, request_ids = snapshot
        resources = resources_by_certificate.get(certificate_id, ())
        return AssociationCheck(
            AssociationStatus.BOUND if resources else AssociationStatus.UNBOUND,
            complete=True,
            resources=resources,
            request_ids=request_ids,
        )

    async def delete_certificate(self, certificate_id: str) -> DeleteReceipt:
        req = slb_models.DeleteServerCertificateRequest(
            region_id=self._region,
            server_certificate_id=certificate_id,
        )
        try:
            resp = await asyncio.to_thread(self._client.delete_server_certificate, req)
        except Exception as exc:
            code = _exception_code(exc)
            normalized = "".join(
                character for character in code.lower() if character.isalnum()
            )
            if "notfound" in normalized:
                return DeleteReceipt(DeleteOutcome.NOT_FOUND, message=code)
            if normalized in {
                "certificaterelateddomainextensionexist",
                "certificateandprivatekeyisrefered",
                "certificateandprivatekeyisreferenced",
            }:
                return DeleteReceipt(DeleteOutcome.IN_USE, message=code)
            raise
        body = getattr(resp, "body", None)
        if body is None:
            raise IncompleteInventoryError("阿里云 CLB 删除证书响应不完整")
        return DeleteReceipt(
            DeleteOutcome.DELETED,
            request_id=str(getattr(body, "request_id", "") or "").strip(),
        )


def _dict_value(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data:
            return data[key]
    return None


class VolcengineCertificateReportAdapter(_UnknownAssociationReportAdapter):
    """火山引擎证书中心库存；产品关联范围不完整，固定仅报告。"""

    def __init__(
        self,
        provider_name: str,
        region: str,
        access_key_id: str = "",
        access_key_secret: str = "",
        *,
        service: Any | None = None,
    ) -> None:
        self.store_id = CertificateStoreId(
            "volcengine", provider_name, "certificate_service", region
        )
        self._service = service or build_volcengine_service(
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
            service="certificate_service",
            region=region,
            module_path="volcenginesdkcertificateservice",
            service_class_name="CERTIFICATESERVICEApi",
        )

    @staticmethod
    def _convert(item: dict[str, Any]) -> RemoteCertificate:
        cert_id = str(_dict_value(item, "InstanceId", "instance_id") or "").strip()
        if not cert_id:
            raise IncompleteInventoryError("火山引擎证书缺少 InstanceId")
        instance_type = str(
            _dict_value(item, "InstanceType", "instance_type") or ""
        ).strip().lower()
        source = (
            CertificateSource.UPLOADED
            if instance_type in {"upload", "uploaded", "import", "imported"}
            else CertificateSource.UNKNOWN
        )
        return RemoteCertificate(
            certificate_id=cert_id,
            name=str(_dict_value(item, "CommonName", "common_name") or cert_id),
            expires_at=_parse_time(_dict_value(item, "NotAfter", "not_after")),
            source=source,
        )

    async def list_certificates(self):
        page = 1
        size = 100
        result: list[RemoteCertificate] = []
        expected_total: int | None = None
        while True:
            response = await asyncio.to_thread(
                self._service.certificate_get_instance_list,
                {"PageNumber": page, "PageSize": size},
            )
            if not isinstance(response, dict):
                raise IncompleteInventoryError("火山引擎证书列表响应不是对象")
            items = _dict_value(response, "Instances", "instances")
            total = _dict_value(response, "TotalCount", "total_count")
            if not isinstance(items, list) or total is None:
                raise IncompleteInventoryError("火山引擎证书分页响应不完整")
            if expected_total is not None and int(total) != expected_total:
                raise IncompleteInventoryError("火山引擎证书分页总数不稳定")
            expected_total = int(total)
            result.extend(self._convert(item) for item in items if isinstance(item, dict))
            if len(result) >= expected_total:
                return result
            if not items:
                raise IncompleteInventoryError("火山引擎证书分页提前返回空页")
            page += 1

    async def get_certificate(self, certificate_id: str) -> RemoteCertificate | None:
        for item in await self.list_certificates():
            if item.certificate_id == certificate_id:
                return item
        return None
