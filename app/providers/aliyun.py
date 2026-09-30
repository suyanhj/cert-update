"""阿里云提供器：域名发现、DNS 记录与产品绑定。"""

from __future__ import annotations

import asyncio
from typing import Any, List, Optional, Set

from app.schemas.config import ProviderConfig
from app.schemas.provider import CloudProductBinding
from app.utils.aliyun_sdk import (
    AliyunProviderSDK,
    build_aliyun_provider_clients,
    load_aliyun_provider_sdk,
    to_cn_region,
)

from .base import Provider

try:
    _ALIYUN_SDK: AliyunProviderSDK | None = load_aliyun_provider_sdk()
    _ALIYUN_IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover - runtime dependency guard
    _ALIYUN_SDK = None
    _ALIYUN_IMPORT_ERROR = exc


class AliyunCloudProvider(Provider):
    """阿里云资源发现提供器。"""

    domain_roles = frozenset({"registration", "dns"})

    _LB_REGION_FALLBACK = [
        "cn-beijing",
        "cn-hangzhou",
        "cn-shanghai",
        "cn-guangzhou",
        "cn-shenzhen",
        "cn-hongkong",
    ]

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        if _ALIYUN_IMPORT_ERROR is not None or _ALIYUN_SDK is None:
            raise RuntimeError(
                "Aliyun SDK dependencies are not installed completely. Install packages from requirements.txt."
            ) from _ALIYUN_IMPORT_ERROR

        self._sdk: AliyunProviderSDK = _ALIYUN_SDK
        self._access_key_id = config.credentials.get("access_key_id", "")
        self._access_key_secret = config.credentials.get("access_key_secret", "")
        self._region = to_cn_region(config.credentials.get("region", "cn-hangzhou"))

        self._slb_clients_by_region: dict[str, Any] = {}
        self._alb_clients_by_region: dict[str, Any] = {}
        self._ecs_clients_by_region: dict[str, Any] = {}
        self._swas_clients_by_region: dict[str, Any] = {}
        self._slb_scan_regions: List[str] | None = None
        self._alb_scan_regions: List[str] | None = None
        self._ecs_scan_regions: List[str] | None = None

        self._build_client()
        self.domains: dict[str, dict] = self._get_domains()

    def _build_client(self) -> None:
        clients = build_aliyun_provider_clients(
            access_key_id=self._access_key_id,
            access_key_secret=self._access_key_secret,
            region=self._region,
            sdk=self._sdk,
        )
        self._domain_client = clients["domain_client"]
        self._dns_client = clients["dns_client"]
        self._cdn_client = clients["cdn_client"]
        self._slb_client = clients["slb_client"]
        self._cas_client = clients["cas_client"]
        self._alb_client = clients["alb_client"]
        self._ecs_client = clients["ecs_client"]
        self._oss_client = clients["oss_client"]

        self._slb_clients_by_region[self._region] = self._slb_client
        self._alb_clients_by_region[self._region] = self._alb_client
        self._ecs_clients_by_region[self._region] = self._ecs_client

    @staticmethod
    def _is_mainland_region(region: str) -> bool:
        normalized = (region or "").strip().lower()
        return normalized.startswith("cn-")

    def _normalize_regions(self, regions: List[str]) -> List[str]:
        result: List[str] = []
        seen: Set[str] = set()
        for region in regions:
            if not region:
                continue
            try:
                normalized = to_cn_region(region)
            except Exception:
                continue
            if not self._is_mainland_region(normalized):
                continue
            if normalized in seen:
                continue
            seen.add(normalized)
            result.append(normalized)
        return result

    def _configured_regions(self, service: str) -> List[str]:
        # credentials is Dict[str, str], so list values are represented as comma-separated strings.
        raw = (
            self.config.credentials.get(f"{service}_regions", "")
            or self.config.credentials.get("lb_regions", "")
            or self.config.credentials.get("regions", "")
        )
        if not raw:
            return []
        candidates = [item.strip() for item in raw.replace(";", ",").split(",") if item.strip()]
        return self._normalize_regions(candidates)

    def _allowed_lb_regions(self) -> List[str]:
        return self._normalize_regions(list(self._LB_REGION_FALLBACK))

    def _build_slb_client_for_region(self, region: str):
        normalized = to_cn_region(region)
        existing = self._slb_clients_by_region.get(normalized)
        if existing is not None:
            return existing

        client = self._sdk.SlbClient(
            self._sdk.Config(
                access_key_id=self._access_key_id,
                access_key_secret=self._access_key_secret,
                endpoint=f"slb.{normalized}.aliyuncs.com",
            )
        )
        self._slb_clients_by_region[normalized] = client
        return client

    def _build_alb_client_for_region(self, region: str):
        normalized = to_cn_region(region)
        existing = self._alb_clients_by_region.get(normalized)
        if existing is not None:
            return existing

        client = self._sdk.AlbClient(
            self._sdk.Config(
                access_key_id=self._access_key_id,
                access_key_secret=self._access_key_secret,
                endpoint=f"alb.{normalized}.aliyuncs.com",
                region_id=normalized,
            )
        )
        self._alb_clients_by_region[normalized] = client
        return client

    def _build_ecs_client_for_region(self, region: str):
        normalized = to_cn_region(region)
        existing = self._ecs_clients_by_region.get(normalized)
        if existing is not None:
            return existing

        client = self._sdk.EcsClient(
            self._sdk.Config(
                access_key_id=self._access_key_id,
                access_key_secret=self._access_key_secret,
                endpoint=f"ecs.{normalized}.aliyuncs.com",
            )
        )
        self._ecs_clients_by_region[normalized] = client
        return client

    def _build_swas_client_for_region(self, region: str):
        normalized = to_cn_region(region)
        existing = self._swas_clients_by_region.get(normalized)
        if existing is not None:
            return existing

        client = self._sdk.SwasClient(
            self._sdk.Config(
                access_key_id=self._access_key_id,
                access_key_secret=self._access_key_secret,
                endpoint=f"swas.{normalized}.aliyuncs.com",
            )
        )
        self._swas_clients_by_region[normalized] = client
        return client

    @staticmethod
    def _normalize_binding_domain(domain: str) -> Optional[str]:
        value = str(domain or "").strip().lower().rstrip(".")
        if not value:
            return None
        return value

    @staticmethod
    def _extract_slb_listener_cert_id(listener: Any) -> Optional[str]:
        https_cfg = getattr(listener, "httpslistener_config", None)
        cert_id = None
        if https_cfg is not None:
            cert_id = getattr(https_cfg, "server_certificate_id", None)
        if not cert_id:
            cert_id = getattr(listener, "server_certificate_id", None)
        return str(cert_id) if cert_id else None

    def _extract_slb_domains_from_domain_extensions(
        self,
        client: Any,
        *,
        region: str,
        lb_id: str,
        listener_port: int,
    ) -> List[str]:
        req = self._sdk.slb_models.DescribeDomainExtensionsRequest(
            region_id=region,
            load_balancer_id=lb_id,
            listener_port=listener_port,
        )
        resp = client.describe_domain_extensions(req)
        container = getattr(getattr(resp, "body", None), "domain_extensions", None)
        domain_extensions = getattr(container, "domain_extension", None) or []

        domains: Set[str] = set()
        for ext in domain_extensions:
            domain = self._normalize_binding_domain(getattr(ext, "domain", ""))
            if domain:
                domains.add(domain)
        return sorted(domains)

    def _extract_slb_domains_from_https_listener_attribute(
        self,
        client: Any,
        *,
        region: str,
        lb_id: str,
        listener_port: int,
    ) -> List[str]:
        req = self._sdk.slb_models.DescribeLoadBalancerHTTPSListenerAttributeRequest(
            region_id=region,
            load_balancer_id=lb_id,
            listener_port=listener_port,
        )
        resp = client.describe_load_balancer_httpslistener_attribute(req)
        container = getattr(getattr(resp, "body", None), "domain_extensions", None)
        domain_extensions = getattr(container, "domain_extension", None) or []

        domains: Set[str] = set()
        for ext in domain_extensions:
            domain = self._normalize_binding_domain(getattr(ext, "domain", ""))
            if domain:
                domains.add(domain)
        return sorted(domains)

    def _extract_slb_listener_domains(
        self,
        client: Any,
        *,
        region: str,
        lb_id: str,
        listener_port: int,
    ) -> List[str]:
        try:
            domains = self._extract_slb_domains_from_domain_extensions(
                client,
                region=region,
                lb_id=lb_id,
                listener_port=listener_port,
            )
            if domains:
                return domains
        except Exception as exc:
            short_err = self._build_exception_summary(exc)
            self.logger.debug(
                "[%s] SLB describe_domain_extensions failed: region=%s lb=%s port=%s err=%s",
                self.name,
                region,
                lb_id,
                listener_port,
                short_err,
            )
            self.logger.debug(
                "[%s] SLB describe_domain_extensions failed detail: region=%s lb=%s port=%s err=%s",
                self.name,
                region,
                lb_id,
                listener_port,
                exc,
                exc_info=True,
            )

        try:
            return self._extract_slb_domains_from_https_listener_attribute(
                client,
                region=region,
                lb_id=lb_id,
                listener_port=listener_port,
            )
        except Exception as exc:
            short_err = self._build_exception_summary(exc)
            self.logger.debug(
                "[%s] SLB describe_https_listener_attribute failed: region=%s lb=%s port=%s err=%s",
                self.name,
                region,
                lb_id,
                listener_port,
                short_err,
            )
            self.logger.debug(
                "[%s] SLB describe_https_listener_attribute failed detail: region=%s lb=%s port=%s err=%s",
                self.name,
                region,
                lb_id,
                listener_port,
                exc,
                exc_info=True,
            )
            return []

    @staticmethod
    def _is_expected_region_error(exc: Exception) -> bool:
        raw = (str(exc) or "").lower()
        # Common non-actionable failures during multi-region scan:
        # - endpoint DNS not available for that region/product
        # - account/sub-user lacks cross-region permissions
        markers = (
            "failed to resolve",
            "nameresolutionerror",
            "getaddrinfo failed",
            "forbidden.loadbalancer",
            "authentication is failed for loadbalancer",
            "accessdenied",
            "code: 403",
            "http status code: 403",
            "statuscode': 403",
        )
        return any(marker in raw for marker in markers)

    def _log_region_scan_error(self, service: str, region: str, exc: Exception) -> bool:
        short_err = self._build_exception_summary(exc)
        if self._is_expected_region_error(exc):
            self.logger.debug(
                "[%s] 阿里云 %s 扫描跳过: region=%s err=%s",
                self.name,
                service,
                region,
                short_err,
            )
            self.logger.debug(
                "[%s] 阿里云 %s 扫描跳过详情: region=%s err=%s",
                self.name,
                service,
                region,
                exc,
                exc_info=True,
            )
            return True

        self.logger.warning(
            "[%s] 获取阿里云 %s 绑定失败，跳过 region=%s err=%s",
            self.name,
            service,
            region,
            short_err,
        )
        self.logger.debug(
            "[%s] 阿里云 %s 扫描失败详情: region=%s err=%s",
            self.name,
            service,
            region,
            exc,
            exc_info=True,
        )
        return False

    def _discover_slb_regions(self) -> List[str]:
        try:
            req = self._sdk.slb_models.DescribeRegionsRequest()
            resp = self._slb_client.describe_regions(req)
            regions_obj = getattr(getattr(resp, "body", None), "regions", None)
            region_items = getattr(regions_obj, "region", None) or []
            discovered = [str(getattr(item, "region_id", "") or "").strip() for item in region_items]
            return self._normalize_regions(discovered)
        except Exception as exc:
            self.logger.debug("[%s] discover slb regions failed: %s", self.name, exc, exc_info=True)
            return []

    def _discover_alb_regions(self) -> List[str]:
        try:
            req = self._sdk.alb_models.DescribeRegionsRequest()
            resp = self._alb_client.describe_regions(req)
            region_items = getattr(getattr(resp, "body", None), "regions", None) or []
            discovered = [str(getattr(item, "region_id", "") or "").strip() for item in region_items]
            return self._normalize_regions(discovered)
        except Exception as exc:
            self.logger.debug("[%s] discover alb regions failed: %s", self.name, exc, exc_info=True)
            return []

    def _discover_ecs_regions(self) -> List[str]:
        """从 ECS DescribeRegions 拉取账号支持的地域列表。"""
        try:
            req = self._sdk.ecs_models.DescribeRegionsRequest()
            resp = self._ecs_client.describe_regions(req)
            region_items = resp.body.regions.region or []
            discovered = [str(item.region_id or "").strip() for item in region_items]
            return self._normalize_regions(discovered)
        except Exception as exc:
            self.logger.debug("[%s] discover ecs regions failed: %s", self.name, exc, exc_info=True)
            return []

    def _get_slb_scan_regions(self) -> List[str]:
        if self._slb_scan_regions is not None:
            return self._slb_scan_regions

        allowed = set(self._allowed_lb_regions())
        configured = [region for region in self._configured_regions("slb") if region in allowed]
        if configured:
            regions = configured
        else:
            discovered = [region for region in self._discover_slb_regions() if region in allowed]
            regions = discovered or list(allowed)

        self._slb_scan_regions = self._normalize_regions(regions)
        return self._slb_scan_regions

    def _get_alb_scan_regions(self) -> List[str]:
        if self._alb_scan_regions is not None:
            return self._alb_scan_regions

        allowed = set(self._allowed_lb_regions())
        configured = [region for region in self._configured_regions("alb") if region in allowed]
        if configured:
            regions = configured
        else:
            discovered = [region for region in self._discover_alb_regions() if region in allowed]
            regions = discovered or list(allowed)

        self._alb_scan_regions = self._normalize_regions(regions)
        return self._alb_scan_regions

    def _get_ecs_scan_regions(self) -> List[str]:
        """ECS 扫描地域：优先配置 ecs_regions/lb_regions/regions，未配置时从 DescribeRegions 发现（仅大陆），否则用默认地域。"""
        if self._ecs_scan_regions is not None:
            return self._ecs_scan_regions
        configured = self._configured_regions("ecs")
        if configured:
            self._ecs_scan_regions = self._normalize_regions(configured)
            return self._ecs_scan_regions
        allowed = set(self._allowed_lb_regions())
        discovered = [r for r in self._discover_ecs_regions() if r in allowed]
        self._ecs_scan_regions = self._normalize_regions(discovered) if discovered else (self._normalize_regions([self._region]) or [self._region])
        return self._ecs_scan_regions

    def _get_domains(self) -> dict[str, dict]:
        domains: dict[str, dict] = {}
        page_size = 100
        page_num = 1

        try:
            while True:
                request = self._sdk.domain_models.QueryDomainListRequest(
                    page_num=page_num,
                    page_size=page_size,
                )
                resp = self._domain_client.query_domain_list(request)
                items = resp.body.data.domain or []

                for data in items:
                    domains[data.domain_name] = {
                        "name": self.name,
                        "domain": data.domain_name,
                        "expires_at": data.expiration_date,
                        "org": data.ccompany,
                        "provider": self.config.type,
                    }

                if len(items) < page_size:
                    break
                page_num += 1
        except Exception:
            self.logger.exception("aliyun query_domain_list failed: provider=%s", self.name)

        return domains

    async def is_domain_provider(self, domain):
        return domain in self.domains

    async def get_domain_list(self) -> List[dict]:
        async def _run():
            domains = []
            self.logger.info("%s aliyun found %d root domains", self.name, len(self.domains))
            for item in self.domains.values():
                days = self.time.remaining_days(self.time.parse(item["expires_at"]))
                subs = await self.get_dns_records(item["domain"])
                domains.append({**item, "days": days, "subs": subs})
            return domains

        return await self._call_provider_api(
            "获取阿里云域名列表",
            _run,
            default=[],
            retries=1,
        )

    async def get_dns_records(self, domain: str) -> list[dict]:
        async def _run():
            page_size = 500
            page_number = 1
            all_items = []

            while True:
                request = self._sdk.dns_models.DescribeDomainRecordsRequest(
                    domain_name=domain,
                    page_size=page_size,
                    page_number=page_number,
                )
                resp = self._dns_client.describe_domain_records(request)
                total_count = resp.body.total_count or 0
                items = resp.body.domain_records.record or []
                all_items.extend(items)

                if len(all_items) >= total_count or not items:
                    break
                page_number += 1

            from app.config import load_raw_config
            sem = asyncio.Semaphore(int(load_raw_config().http_scan_parallel or 5))
            tasks = [
                self._probe_domain(
                    self._full_domain(domain, item.rr),
                    remark=item.remark or "",
                    sem=sem,
                )
                for item in all_items
                if item.status.upper() == "ENABLE" and item.type.upper() in ("A", "CNAME")
            ]
            records = await asyncio.gather(*tasks)
            self.logger.info(
                "%s aliyun %s dns records: matched=%d total=%d",
                self.name,
                domain,
                len(tasks),
                total_count,
            )
            return records

        return await self._call_provider_api(
            f"{self.name} 获取 {domain} DNS 记录",
            _run,
            default=[],
            retries=1,
        )

    async def get_cdn_bindings(self) -> List[CloudProductBinding]:
        def _run():
            bindings: List[CloudProductBinding] = []
            page_size = 200
            page_number = 1

            while True:
                request = self._sdk.cdn_models.DescribeUserDomainsRequest(
                    page_size=page_size,
                    page_number=page_number,
                )
                resp = self._cdn_client.describe_user_domains(request)
                page_data = resp.body.domains.page_data or []

                for item in page_data:
                    # 只保留在线且已开启 HTTPS 的 CDN 域名（SslProtocol=on）
                    if str(item.domain_status or "").lower() != "online":
                        continue
                    if str(item.ssl_protocol or "").lower() != "on":
                        continue

                    cert_req = self._sdk.cdn_models.DescribeDomainCertificateInfoRequest(
                        domain_name=item.domain_name
                    )
                    cert_resp = self._cdn_client.describe_domain_certificate_info(cert_req)
                    cert_infos = cert_resp.body.cert_infos.cert_info or []
                    cert_id = str(cert_infos[0].cert_id) or None if cert_infos else None
                    cert_expire_time = cert_infos[0].cert_expire_time if cert_infos else None

                    # 只保留确实查到证书 ID 的域名
                    if not cert_id:
                        continue

                    bindings.append(
                        CloudProductBinding(
                            provider_name=self.name,
                            product_type="cdn",
                            product_id=item.domain_name,
                            domain=item.domain_name,
                            resource_type="cdn",
                            resource_name=item.domain_name,
                            status=item.domain_status,
                            cert_id=cert_id,
                            cert_expire_time=cert_expire_time,
                            metadata={
                                "cname": item.cname,
                                "cdn_type": item.cdn_type,
                            },
                        )
                    )

                if len(page_data) < page_size:
                    break
                page_number += 1

            self.logger.info("[%s] 阿里云 CDN 发现 %d 个已开启 HTTPS 的绑定", self.name, len(bindings))
            return bindings

        return await self._call_provider_api(
            "获取阿里云 CDN 域名列表",
            _run,
            default=[],
            retries=1,
        )

    async def get_lb_bindings(self) -> List[CloudProductBinding]:
        def _run():
            bindings: List[CloudProductBinding] = []
            scan_regions = self._get_slb_scan_regions()
            skipped_regions: List[str] = []

            for region in scan_regions:
                try:
                    client = self._build_slb_client_for_region(region)
                    page_size = 100
                    page_number = 1

                    while True:
                        req = self._sdk.slb_models.DescribeLoadBalancersRequest(
                            region_id=region,
                            page_size=page_size,
                            page_number=page_number,
                        )
                        lb_resp = client.describe_load_balancers(req)
                        lbs = lb_resp.body.load_balancers.load_balancer or []

                        for lb in lbs:
                            try:
                                listener_req = self._sdk.slb_models.DescribeLoadBalancerListenersRequest(
                                    load_balancer_id=[lb.load_balancer_id],
                                    listener_protocol="https",
                                )
                                listener_resp = client.describe_load_balancer_listeners(listener_req)
                                listeners = listener_resp.body.listeners or []

                            except Exception as exc:
                                short_err = self._build_exception_summary(exc)
                                self.logger.warning(
                                    "[%s] 读取阿里云 SLB 监听器失败，跳过: region=%s lb=%s err=%s",
                                    self.name,
                                    region,
                                    lb.load_balancer_id,
                                    short_err,
                                )
                                self.logger.debug(
                                    "[%s] 读取阿里云 SLB 监听器失败详情: region=%s lb=%s err=%s",
                                    self.name,
                                    region,
                                    lb.load_balancer_id,
                                    exc,
                                    exc_info=True,
                                )
                                continue

                            for listener in listeners:
                                if str(listener.status or "").lower() == "stopped":
                                    continue
                                listener_port = int(listener.listener_port) if listener.listener_port else None
                                if listener_port is None:
                                    continue
                                server_cert_id = self._extract_slb_listener_cert_id(listener)
                                domains = self._extract_slb_listener_domains(
                                    client,
                                    region=region,
                                    lb_id=lb.load_balancer_id,
                                    listener_port=listener_port,
                                )
                                if not domains:
                                    self.logger.debug(
                                        "[%s] SLB listener has no domain bindings, skip: region=%s lb=%s port=%s",
                                        self.name,
                                        region,
                                        lb.load_balancer_id,
                                        listener_port,
                                    )
                                    continue

                                for domain in domains:
                                    bindings.append(
                                        CloudProductBinding(
                                            provider_name=self.name,
                                            product_type="slb",
                                            product_id=f"{region}:{lb.load_balancer_id}:{listener_port}",
                                            domain=domain,
                                            resource_type="slb",
                                            resource_name=lb.load_balancer_name or lb.load_balancer_id,
                                            status=listener.status,
                                            cert_id=server_cert_id,
                                            listener_port=listener_port,
                                            metadata={
                                                "lb_id": lb.load_balancer_id,
                                                "lb_name": lb.load_balancer_name,
                                                "lb_address": lb.address,
                                                "lb_region": region,
                                                "listener_protocol": "https",
                                            },
                                        )
                                    )

                        if len(lbs) < page_size:
                            break
                        page_number += 1
                except Exception as exc:
                    if self._log_region_scan_error("SLB", region, exc):
                        skipped_regions.append(region)

            if skipped_regions:
                self.logger.info(
                    "[%s] 阿里云 SLB 因鉴权/端点限制跳过 %d 个地域: %s",
                    self.name,
                    len(skipped_regions),
                    ",".join(skipped_regions),
                )
            self.logger.info(
                "[%s] 阿里云 SLB 在 %d 个地域发现 %d 个 HTTPS 绑定",
                self.name,
                len(scan_regions),
                len(bindings),
            )
            return bindings

        return await self._call_provider_api(
            "获取阿里云 SLB 绑定",
            _run,
            default=[],
            retries=1,
        )

    async def get_oss_bindings(self) -> List[CloudProductBinding]:
        async def _run():
            bindings = await asyncio.to_thread(self._list_oss_bindings)
            self.logger.info("[%s] 阿里云 OSS 发现 %d 个绑定", self.name, len(bindings))
            return bindings

        return await self._call_provider_api(
            "获取阿里云 OSS 绑定",
            _run,
            default=[],
            retries=1,
        )

    def _build_oss_client_for_region(self, region: str):
        normalized = to_cn_region(region)
        return self._sdk.OssClient(
            self._sdk.OssConfig(
                credentials_provider=self._sdk.Credentials(
                    access_key_id=self._access_key_id,
                    access_key_secret=self._access_key_secret,
                ),
                region=normalized,
                endpoint=f"oss-{normalized}.aliyuncs.com",
            )
        )

    def _list_oss_bindings(self) -> List[CloudProductBinding]:
        client = self._oss_client
        result = client.list_buckets(self._sdk.oss_models.ListBucketsRequest())
        bindings: List[CloudProductBinding] = []
        region_clients: dict[str, Any] = {}

        for bucket in result.buckets or []:
            bucket_region = to_cn_region(bucket.region or self._region)
            if bucket_region not in region_clients:
                try:
                    region_clients[bucket_region] = self._build_oss_client_for_region(bucket_region)
                except Exception as exc:
                    short_err = self._build_exception_summary(exc)
                    self.logger.warning(
                        "[%s] create OSS region client failed, skip: region=%s err=%s",
                        self.name,
                        bucket_region,
                        short_err,
                    )
                    self.logger.debug(
                        "[%s] create OSS region client failed detail: region=%s err=%s",
                        self.name,
                        bucket_region,
                        exc,
                        exc_info=True,
                    )
                    continue

            try:
                regional_client = region_clients[bucket_region]
                cname_result = regional_client.list_cname(
                    self._sdk.oss_models.ListCnameRequest(bucket=bucket.name)
                )
            except Exception as exc:
                short_err = self._build_exception_summary(exc)
                self.logger.warning(
                    "[%s] read OSS Bucket CNAME failed, skip: bucket=%s region=%s err=%s",
                    self.name,
                    bucket.name,
                    bucket_region,
                    short_err,
                )
                self.logger.debug(
                    "[%s] read OSS Bucket CNAME failed detail: bucket=%s region=%s err=%s",
                    self.name,
                    bucket.name,
                    bucket_region,
                    exc,
                    exc_info=True,
                )
                continue

            for item in cname_result.cnames or []:
                if not item.domain:
                    continue
                cert = item.certificate
                # 只保留已经配置了证书的 OSS 域名绑定
                if not cert or not getattr(cert, "cert_id", None):
                    continue
                cert_id = str(cert.cert_id)
                product_id = f"{bucket_region}:{bucket.name}:{item.domain}"
                bindings.append(
                    CloudProductBinding(
                        provider_name=self.name,
                        product_type="oss",
                        product_id=product_id,
                        domain=item.domain,
                        resource_type="oss",
                        resource_name=bucket.name,
                        status=str(item.status or ""),
                        cert_id=cert_id,
                        metadata={
                            "bucket": bucket.name,
                            "oss_region": bucket_region,
                            "last_modified": str(item.last_modified or ""),
                        },
                    )
                )
        return bindings

    async def get_alb_bindings(self) -> List[CloudProductBinding]:
        async def _run():
            bindings = await asyncio.to_thread(self._list_alb_bindings)
            self.logger.info("[%s] 阿里云 ALB 发现 %d 个绑定", self.name, len(bindings))
            return bindings

        return await self._call_provider_api(
            "获取阿里云 ALB 绑定",
            _run,
            default=[],
            retries=1,
        )

    def _list_alb_bindings(self) -> List[CloudProductBinding]:
        alb_models = self._sdk.alb_models
        bindings: List[CloudProductBinding] = []
        scan_regions = self._get_alb_scan_regions()
        skipped_regions: List[str] = []

        for region in scan_regions:
            try:
                client = self._build_alb_client_for_region(region)

                next_token: Optional[str] = None
                lbs = []
                while True:
                    lb_req = alb_models.ListLoadBalancersRequest(
                        max_results=100,
                        next_token=next_token,
                    )
                    lb_resp = client.list_load_balancers(lb_req)
                    lbs.extend(lb_resp.body.load_balancers or [])
                    next_token = lb_resp.body.next_token
                    if not next_token:
                        break

                for lb in lbs:
                    next_token = None
                    listeners = []
                    while True:
                        listener_req = alb_models.ListListenersRequest(
                            load_balancer_ids=[lb.load_balancer_id],
                            max_results=100,
                            next_token=next_token,
                        )
                        listener_resp = client.list_listeners(listener_req)
                        listeners.extend(listener_resp.body.listeners or [])
                        next_token = listener_resp.body.next_token
                        if not next_token:
                            break

                    for listener in listeners:
                        protocol = str(listener.listener_protocol or "").upper()
                        if protocol != "HTTPS":
                            continue
                        if str(listener.listener_status or "").lower() == "stopped":
                            continue

                        next_token = None
                        rules = []
                        while True:
                            rule_req = alb_models.ListRulesRequest(
                                listener_id=listener.listener_id,
                                max_results=100,
                                next_token=next_token,
                            )
                            rule_resp = client.list_rules(rule_req)
                            rules.extend(rule_resp.body.rules or [])
                            next_token = rule_resp.body.next_token
                            if not next_token:
                                break

                        for domain in self._extract_alb_rule_domains(rules):
                            bindings.append(
                                CloudProductBinding(
                                    provider_name=self.name,
                                    product_type="alb",
                                    product_id=f"{region}:{lb.load_balancer_id}:{listener.listener_id}",
                                    domain=domain,
                                    resource_type="alb",
                                    resource_name=lb.load_balancer_name or lb.load_balancer_id,
                                    status=listener.listener_status,
                                    cert_id=self._extract_alb_listener_cert_id(listener),
                                    listener_port=(
                                        int(listener.listener_port)
                                        if listener.listener_port
                                        else None
                                    ),
                                    metadata={
                                        "lb_id": lb.load_balancer_id,
                                        "lb_name": lb.load_balancer_name,
                                        "lb_region": region,
                                        "listener_id": listener.listener_id,
                                        "listener_protocol": "HTTPS",
                                    },
                                )
                            )
            except Exception as exc:
                if self._log_region_scan_error("ALB", region, exc):
                    skipped_regions.append(region)

        if skipped_regions:
            self.logger.info(
                "[%s] 阿里云 ALB 因鉴权/端点限制跳过 %d 个地域: %s",
                self.name,
                len(skipped_regions),
                ",".join(skipped_regions),
            )

        return bindings

    @staticmethod
    def _extract_alb_listener_cert_id(listener: Any) -> Optional[str]:
        certs = listener.certificates or []
        if certs:
            return str(certs[0].certificate_id) if certs[0].certificate_id else None
        return str(listener.certificate_id) if listener.certificate_id else None

    @staticmethod
    def _extract_alb_rule_domains(rules: List[Any]) -> List[str]:
        domains: Set[str] = set()
        for rule in rules:
            for cond in rule.rule_conditions or []:
                if str(cond.type or "").lower() != "host":
                    continue
                host_cfg = getattr(cond, "host_config", None)
                values = getattr(host_cfg, "values", None) or []
                for value in values:
                    if value:
                        domains.add(value.strip().lower())
        return sorted(domains)

    def _list_ecs_renewal_status_by_region(self, client: Any, region: str) -> dict[str, str]:
        """返回当前地域 instance_id -> renewal_status 映射。Normal=手动续费，AutoRenewal=自动续费。"""
        ecs_models = self._sdk.ecs_models
        result: dict[str, str] = {}
        page_size = 100
        for renewal_status in ("Normal", "AutoRenewal"):
            page_number = 1
            while True:
                request = ecs_models.DescribeInstanceAutoRenewAttributeRequest(
                    region_id=region,
                    renewal_status=renewal_status,
                    page_size=page_size,
                    page_number=page_number,
                )
                response = client.describe_instance_auto_renew_attribute(request)
                attrs = response.body.instance_renew_attributes.instance_renew_attribute or []
                for item in attrs:
                    iid = (item.instance_id or "").strip()
                    if iid:
                        result[iid] = (item.renewal_status or renewal_status).strip() or renewal_status
                if len(attrs) < page_size:
                    break
                page_number += 1
        return result

    def _collect_aliyun_swas_prepaid_for_region(self, region: str) -> List[dict]:
        """轻量应用服务器 ListInstances，仅保留包年包月；续费状态 SWAS API 未提供，按手动续费参与告警。"""
        swas_models = self._sdk.swas_models
        client = self._build_swas_client_for_region(region)
        out: List[dict] = []
        page_number = 1
        page_size = 100
        while True:
            request = swas_models.ListInstancesRequest(
                region_id=region,
                page_size=page_size,
                page_number=page_number,
            )
            response = client.list_instances(request)
            body = response.body
            if body is None:
                raise RuntimeError(f"SWAS ListInstances 响应 body 为空 region={region}")
            page_items = list(body.instances or [])

            for item in page_items:
                charge_type = (item.charge_type or "").strip().lower()
                if charge_type != "prepaid":
                    continue

                instance_id = (item.instance_id or "").strip()
                if not instance_id:
                    continue

                expired_time = (item.expired_time or "").strip()
                if not expired_time:
                    expires_at = ""
                    days = None
                else:
                    try:
                        parsed_expired = self.time.parse(expired_time)
                        expires_at = self.time.to_local_tz(parsed_expired)
                        days = self.time.remaining_days(parsed_expired)
                    except Exception:
                        expires_at = expired_time
                        days = None

                instance_name = item.instance_name or item.instance_id or ""
                item_region = (item.region_id or "").strip() or region

                out.append(
                    {
                        "name": self.name,
                        "provider": self.config.type,
                        "provider_name": self.name,
                        "instance_id": instance_id,
                        "instance_name": str(instance_name),
                        "region": item_region,
                        "status": str(item.status or ""),
                        "charge_type": charge_type,
                        "renewal_status": "Normal",
                        "auto_renewal": False,
                        "expires_at": expires_at,
                        "days": days,
                        "instance_kind": "swas",
                    }
                )

            if len(page_items) < page_size:
                break
            page_number += 1
        return out

    async def get_ecs_instances(self) -> List[dict]:
        """采集全部包年包月 ECS 与轻量应用服务器，附带续费状态与过期时间等元数据，供落盘与告警使用。"""
        def _run():
            instances: List[dict] = []
            ecs_prepaid_count = 0
            swas_prepaid_count = 0
            scan_regions = self._get_ecs_scan_regions()
            ecs_models = self._sdk.ecs_models

            for region in scan_regions:
                try:
                    client = self._build_ecs_client_for_region(region)
                    renewal_map = self._list_ecs_renewal_status_by_region(client, region)

                    page_number = 1
                    page_size = 100
                    while True:
                        request = ecs_models.DescribeInstancesRequest(
                            region_id=region,
                            page_size=page_size,
                            page_number=page_number,
                        )
                        response = client.describe_instances(request)
                        page_items = response.body.instances.instance or []

                        for item in page_items:
                            charge_type = (item.instance_charge_type or "").lower()
                            if charge_type != "prepaid":
                                continue

                            instance_id = (item.instance_id or "").strip()
                            if not instance_id:
                                continue

                            renewal_status = renewal_map.get(instance_id, "Normal")
                            auto_renewal = renewal_status == "AutoRenewal"

                            expired_time = (item.expired_time or "").strip()
                            if not expired_time:
                                expires_at = ""
                                days = None
                            else:
                                try:
                                    parsed_expired = self.time.parse(expired_time)
                                    expires_at = self.time.to_local_tz(parsed_expired)
                                    days = self.time.remaining_days(parsed_expired)
                                except Exception:
                                    expires_at = expired_time
                                    days = None

                            instance_name = (
                                item.instance_name
                                or item.host_name
                                or item.instance_id
                                or ""
                            )

                            instances.append(
                                {
                                    "name": self.name,
                                    "provider": self.config.type,
                                    "provider_name": self.name,
                                    "instance_id": instance_id,
                                    "instance_name": str(instance_name),
                                    "region": region,
                                    "status": str(item.status or ""),
                                    "charge_type": charge_type,
                                    "renewal_status": renewal_status,
                                    "auto_renewal": auto_renewal,
                                    "expires_at": expires_at,
                                    "days": days,
                                }
                            )
                            ecs_prepaid_count += 1

                        if len(page_items) < page_size:
                            break
                        page_number += 1
                except Exception as exc:
                    self._log_region_scan_error("ECS", region, exc)

                try:
                    swas_batch = self._collect_aliyun_swas_prepaid_for_region(region)
                    instances.extend(swas_batch)
                    swas_prepaid_count += len(swas_batch)
                except Exception as exc:
                    self._log_region_scan_error("SWAS", region, exc)

            self.logger.info(
                "[%s] 阿里云 ECS 包年包月 %d 台，轻量应用服务器 %d 台",
                self.name,
                ecs_prepaid_count,
                swas_prepaid_count,
            )
            return instances

        return await self._call_provider_api(
            "获取阿里云 ECS 到期信息",
            _run,
            default=[],
            retries=1,
        )

    async def get_all_product_bindings(self) -> List[CloudProductBinding]:
        bindings = await super().get_all_product_bindings()
        try:
            bindings.extend(await self.get_alb_bindings())
        except Exception as exc:
            self.logger.warning("[%s] get_alb_bindings failed, skip: %s", self.name, exc)
        return bindings
