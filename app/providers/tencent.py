"""腾讯云 Provider：DNSPod 域名发现、CDN / 云直播 / CLB / COS 绑定扫描。"""

from __future__ import annotations

import asyncio
from typing import Any, List, Optional, Set

from qcloud_cos import CosServiceError
from tencentcloud.cdn.v20180606 import models as cdn_models
from tencentcloud.clb.v20180317 import models as clb_models
from tencentcloud.domain.v20180808 import models as domain_models
from tencentcloud.dnspod.v20210323 import models as dnspod_models
from tencentcloud.live.v20180801 import models as live_models
from tencentcloud.ssl.v20191205 import models as ssl_models

from app.config import load_raw_config
from app.schemas.config import ProviderConfig
from app.schemas.provider import CloudProductBinding
from app.utils.domain_match import normalize_domain
from app.utils.logger import get_logger
from app.utils.tls_probe import probe_domains_batch
from app.utils.tencent_sdk import (
    CdnClient,
    ClbClient,
    DomainClient,
    DnspodClient,
    LiveClient,
    SslClient,
    build_tencent_cos_client,
    build_tencent_client,
    suppress_cos_client_logs,
)
from app.utils.time import TimeUtil

from .base import Provider

LOGGER = get_logger("provider")

_DNSPOD_PAGE_SIZE = 100
_DOMAIN_PAGE_SIZE = 100
_DNS_RECORD_PAGE_SIZE = 3000
_CDN_PAGE_SIZE = 100
_CLB_PAGE_SIZE = 100
_LIVE_PAGE_SIZE = 100
_SSL_HOST_PAGE_SIZE = 100


class TencentCloudProvider(Provider):
    """腾讯云 Provider，支持 DNSPod、CDN、云直播、CLB、COS。"""

    domain_roles = frozenset({"registration", "dns"})

    # 本地产品绑定缓存扫描开关；部署改走上传证书后的 SSL Host 扫描，默认关闭。
    _ENABLE_BINDING_CACHE_SCAN = False

    _CLB_REGION_FALLBACK = [
        "ap-beijing",
        "ap-nanjing",
        "ap-shanghai",
        "ap-guangzhou",
        "ap-chengdu",
        "ap-chongqing",
        "ap-hongkong",
        "ap-taipei",
        "ap-singapore",
        "ap-jakarta",
        "ap-bangkok",
        "ap-seoul",
        "ap-tokyo",
    ]

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        self._secret_id = config.credentials.get("secret_id", "")
        self._secret_key = config.credentials.get("secret_key", "")
        self._region = self._normalize_ap_region(config.credentials.get("region", "ap-guangzhou"))
        self._ssl_clients_by_region: dict[str, Any] = {}
        self._clb_clients_by_region: dict[str, Any] = {}
        self._cos_clients_by_region: dict[str, Any] = {}
        self._clb_scan_regions: List[str] | None = None
        self._cos_scan_regions: List[str] | None = None
        self._dnspod_client = build_tencent_client(
            secret_id=self._secret_id,
            secret_key=self._secret_key,
            client_cls=DnspodClient,
        )
        self._domain_client = build_tencent_client(
            secret_id=self._secret_id,
            secret_key=self._secret_key,
            client_cls=DomainClient,
        )
        self._cdn_client = build_tencent_client(
            secret_id=self._secret_id,
            secret_key=self._secret_key,
            client_cls=CdnClient,
        )
        self._live_client = build_tencent_client(
            secret_id=self._secret_id,
            secret_key=self._secret_key,
            client_cls=LiveClient,
        )
        self._clb_client = self._build_clb_client_for_region(self._region)
        self._cos_client = self._build_cos_client_for_region(self._region)
        self._ssl_client = self._get_ssl_client(self._region)

    @staticmethod
    def _normalize_ap_region(region: str) -> str:
        normalized = (region or "").strip().lower()
        if not normalized:
            raise ValueError("tencent region must not be empty")
        if not normalized.startswith("ap-"):
            raise ValueError(f"tencent region must start with ap-, got {region!r}")
        return normalized

    @staticmethod
    def _is_ap_region(region: str) -> bool:
        normalized = (region or "").strip().lower()
        return bool(normalized) and normalized.startswith("ap-")

    def _normalize_regions(self, regions: List[str]) -> List[str]:
        result: List[str] = []
        seen: Set[str] = set()
        for region in regions:
            if not region:
                continue
            try:
                normalized = self._normalize_ap_region(region)
            except ValueError:
                continue
            if normalized in seen:
                continue
            seen.add(normalized)
            result.append(normalized)
        return result

    def _configured_regions(self, service: str) -> List[str]:
        raw = (
            self.config.credentials.get(f"{service}_regions", "")
            or self.config.credentials.get("lb_regions", "")
            or self.config.credentials.get("regions", "")
        )
        if not raw:
            return []
        candidates = [item.strip() for item in raw.replace(";", ",").split(",") if item.strip()]
        return self._normalize_regions(candidates)

    def _allowed_clb_regions(self) -> List[str]:
        return self._normalize_regions(list(self._CLB_REGION_FALLBACK))

    def _build_clb_client_for_region(self, region: str) -> Any:
        normalized = self._normalize_ap_region(region)
        existing = self._clb_clients_by_region.get(normalized)
        if existing is not None:
            return existing
        client = build_tencent_client(
            secret_id=self._secret_id,
            secret_key=self._secret_key,
            client_cls=ClbClient,
            region=normalized,
        )
        self._clb_clients_by_region[normalized] = client
        return client

    def _build_cos_client_for_region(self, region: str) -> Any:
        normalized = str(region or "").strip().lower()
        if not normalized:
            raise ValueError("tencent COS region must not be empty")
        existing = self._cos_clients_by_region.get(normalized)
        if existing is not None:
            return existing
        client = build_tencent_cos_client(
            secret_id=self._secret_id,
            secret_key=self._secret_key,
            region=normalized,
        )
        self._cos_clients_by_region[normalized] = client
        return client

    def _build_ssl_client_for_region(self, region: str) -> Any:
        normalized = str(region or "").strip()
        existing = self._ssl_clients_by_region.get(normalized)
        if existing is not None:
            return existing
        client = build_tencent_client(
            secret_id=self._secret_id,
            secret_key=self._secret_key,
            client_cls=SslClient,
            region=normalized,
        )
        self._ssl_clients_by_region[normalized] = client
        return client

    def _get_ssl_client(self, region: str = "") -> Any:
        """CDN/云直播走 provider 默认 Region；CLB/COS 扫描按地域带 Region。"""
        normalized = str(region or "").strip()
        if not normalized:
            normalized = self._region
        return self._build_ssl_client_for_region(normalized)

    def _get_ssl_scan_regions(self, service: str) -> List[str]:
        """CLB/COS 的 DescribeHost*InstanceList 需按地域循环查询。"""
        allowed = set(self._allowed_clb_regions())
        configured = [region for region in self._configured_regions(service) if region in allowed]
        if configured:
            regions = configured
        else:
            regions = list(allowed)
            if self._region not in regions:
                regions = [self._region, *regions]
        return self._normalize_regions(regions)

    def _get_clb_scan_regions(self) -> List[str]:
        if self._clb_scan_regions is not None:
            return self._clb_scan_regions
        self._clb_scan_regions = self._get_ssl_scan_regions("clb")
        return self._clb_scan_regions

    def _get_cos_scan_regions(self) -> List[str]:
        if self._cos_scan_regions is not None:
            return self._cos_scan_regions
        self._cos_scan_regions = self._get_ssl_scan_regions("cos")
        return self._cos_scan_regions

    async def is_domain_provider(self, domain: str) -> bool:
        try:
            candidate = normalize_domain(str(domain))
        except ValueError:
            return False

        roots = await self._call_provider_api(
            "判断腾讯云域名归属",
            self._list_root_domains,
            default=[],
            retries=1,
        )
        for item in roots or []:
            root = str(item.get("domain") or "").strip()
            if not root:
                continue
            try:
                normalized_root = normalize_domain(root)
            except ValueError:
                continue
            if candidate == normalized_root or candidate.endswith(f".{normalized_root}"):
                return True
        return False

    def _parse_domain_expiration(self, expiration: str, domain_name: str) -> Any | None:
        normalized = str(expiration or "").strip()
        if not normalized or normalized.startswith("0000-"):
            return None
        try:
            return TimeUtil.parse(normalized)
        except (ValueError, TypeError):
            LOGGER.warning(
                "[%s] 域名 %s ExpirationDate 解析失败，忽略: %s",
                self.name,
                domain_name,
                normalized,
            )
            return None

    def _fetch_domain_org(self, domain_name: str) -> str:
        req = domain_models.DescribeDomainSimpleInfoRequest()
        req.DomainName = domain_name
        resp = self._domain_client.DescribeDomainSimpleInfo(req)
        info = resp.DomainInfo
        if info is None:
            return ""
        org_cn = str(getattr(info, "OrganizationNameCN", "") or "").strip()
        if org_cn:
            return org_cn
        return str(getattr(info, "OrganizationName", "") or "").strip()

    def _fetch_domain_registration_map(self) -> dict[str, dict[str, Any]]:
        """从域名注册 API 拉取到期时间与注册主体，按根域名索引。"""
        registration_by_domain: dict[str, dict[str, Any]] = {}
        offset = 0
        while True:
            req = domain_models.DescribeDomainNameListRequest()
            req.Offset = offset
            req.Limit = _DOMAIN_PAGE_SIZE
            resp = self._domain_client.DescribeDomainNameList(req)
            page = list(resp.DomainSet or [])
            for item in page:
                domain_name = str(getattr(item, "DomainName", "") or "").strip().lower()
                if not domain_name:
                    continue
                registration_by_domain[domain_name] = {
                    "expires_at": self._parse_domain_expiration(
                        str(getattr(item, "ExpirationDate", "") or ""),
                        domain_name,
                    ),
                    "org": "",
                }
            total = int(resp.TotalCount or 0)
            offset += len(page)
            if not page or (total and offset >= total) or len(page) < _DOMAIN_PAGE_SIZE:
                break

        for domain_name in registration_by_domain:
            try:
                registration_by_domain[domain_name]["org"] = self._fetch_domain_org(domain_name)
            except Exception as exc:
                LOGGER.warning(
                    "[%s] 获取域名 %s 注册主体失败，忽略: %s",
                    self.name,
                    domain_name,
                    exc,
                )

        LOGGER.info(
            "[%s] 腾讯云域名注册 API 发现 %d 条注册信息",
            self.name,
            len(registration_by_domain),
        )
        return registration_by_domain

    def _list_root_domains(self) -> List[dict]:
        try:
            registration_by_domain = self._fetch_domain_registration_map()
        except Exception as exc:
            LOGGER.warning(
                "[%s] 获取腾讯云域名注册信息失败，DNSPod 列表仍继续: %s",
                self.name,
                exc,
            )
            registration_by_domain = {}

        domains: List[dict] = []
        dns_domains: set[str] = set()
        offset = 0
        while True:
            req = dnspod_models.DescribeDomainListRequest()
            req.Type = "ALL"
            req.Offset = offset
            req.Limit = _DNSPOD_PAGE_SIZE
            resp = self._dnspod_client.DescribeDomainList(req)
            page = list(resp.DomainList or [])
            for item in page:
                domain_name = str(item.Name or "").strip()
                registration = registration_by_domain.get(domain_name.lower(), {})
                normalized_name = domain_name.lower()
                dns_domains.add(normalized_name)
                roles = ["dns"]
                if normalized_name in registration_by_domain:
                    roles.insert(0, "registration")
                domains.append(
                    {
                        "domain": domain_name,
                        "domain_id": item.DomainId,
                        "status": item.Status,
                        "expires_at": registration.get("expires_at"),
                        "org": registration.get("org", ""),
                        "record_count": getattr(item, "RecordCount", 0),
                        "domain_roles": roles,
                    }
                )
            total = 0
            if resp.DomainCountInfo is not None:
                total = int(resp.DomainCountInfo.DomainTotal or 0)
            offset += len(page)
            if not page or (total and offset >= total) or len(page) < _DNSPOD_PAGE_SIZE:
                break

        for domain_name, registration in registration_by_domain.items():
            if domain_name in dns_domains:
                continue
            domains.append(
                {
                    "domain": domain_name,
                    "expires_at": registration.get("expires_at"),
                    "org": registration.get("org", ""),
                    "record_count": 0,
                    "domain_roles": ["registration"],
                }
            )
        LOGGER.info("[%s] 腾讯云发现 %d 个域名", self.name, len(domains))
        return domains

    def _list_dns_records(self, domain: str) -> List[dict]:
        records: List[dict] = []
        offset = 0
        while True:
            req = dnspod_models.DescribeRecordListRequest()
            req.Domain = domain
            req.Offset = offset
            req.Limit = _DNS_RECORD_PAGE_SIZE
            req.ErrorOnEmpty = "no"
            resp = self._dnspod_client.DescribeRecordList(req)
            page = list(resp.RecordList or [])
            for item in page:
                full_domain = f"{item.Name}.{domain}" if item.Name != "@" else domain
                records.append(
                    {
                        "record_id": item.RecordId,
                        "subdomain": item.Name,
                        "full_domain": full_domain,
                        "type": item.Type,
                        "value": item.Value,
                        "status": item.Status,
                        "ttl": item.TTL,
                        "line": getattr(item, "Line", "默认"),
                        "remark": getattr(item, "Remark", "") or "",
                    }
                )
            total = 0
            if resp.RecordCountInfo is not None:
                total = int(resp.RecordCountInfo.TotalCount or 0)
            offset += len(page)
            if not page or (total and offset >= total) or len(page) < _DNS_RECORD_PAGE_SIZE:
                break
        LOGGER.debug("域名 %s 发现 %d 条解析记录", domain, len(records))
        return records

    async def get_domain_list(self) -> List[dict]:
        async def _run():
            roots = self._list_root_domains()
            conf = load_raw_config()
            sem = asyncio.Semaphore(int(conf.http_scan_parallel or 5))
            result: List[dict] = []
            for item in roots:
                domain = item["domain"]
                try:
                    raw_records = self._list_dns_records(domain)
                except Exception as exc:
                    LOGGER.warning(
                        "[%s] 获取域名 %s DNS 记录失败，保留根域名并跳过解析探测: %s",
                        self.name,
                        domain,
                        exc,
                    )
                    result.append({**item, "subs": []})
                    continue
                tasks = [
                    self._probe_domain(
                        str(record["full_domain"]),
                        remark=str(record.get("remark") or ""),
                        sem=sem,
                    )
                    for record in raw_records
                    if str(record.get("status") or "").upper() == "ENABLE"
                    and str(record.get("type") or "").upper() in ("A", "CNAME")
                ]
                subs = list(await asyncio.gather(*tasks)) if tasks else []
                result.append({**item, "subs": subs})
            return result

        return await self._call_provider_api(
            "获取腾讯云域名列表",
            _run,
            default=[],
        )

    async def get_dns_records(self, domain: str) -> List[dict]:
        return await self._call_provider_api(
            f"获取 {domain} DNS 记录",
            lambda: self._list_dns_records(domain),
            default=[],
            retries=1,
        )

    async def discover_domains(self) -> List[dict]:
        """兼容旧接口，复用 get_domain_list。"""
        return await self.get_domain_list()

    @staticmethod
    def _ssl_domain_match_filter() -> Any:
        item = ssl_models.Filter()
        item.FilterKey = "domainMatch"
        item.FilterValue = "1"
        return item

    def _iter_ssl_host_instances(
        self,
        client: Any,
        make_request,
        method_name: str,
        certificate_id: str,
        *,
        use_cache: bool = False,
    ):
        """按待部署证书翻页拉取 SSL DescribeHost*InstanceList。"""
        normalized_cert_id = str(certificate_id or "").strip()
        if not normalized_cert_id:
            raise ValueError("腾讯云 SSL Host 扫描必须提供 CertificateId")
        offset = 0
        while True:
            req = make_request()
            req.CertificateId = normalized_cert_id
            # 部署扫描必须实时拉取；IsCache=1 会命中半小时缓存，刚上传证书后 COS 等可能返回空列表。
            req.IsCache = 1 if use_cache else 0
            req.Filters = [self._ssl_domain_match_filter()]
            if hasattr(req, "Offset"):
                req.Offset = offset
            if hasattr(req, "Limit"):
                req.Limit = _SSL_HOST_PAGE_SIZE
            resp = getattr(client, method_name)(req)
            page = list(getattr(resp, "InstanceList", None) or [])
            yield from page
            total = int(getattr(resp, "TotalCount", 0) or 0)
            offset += len(page)
            if not page or (total and offset >= total) or len(page) < _SSL_HOST_PAGE_SIZE:
                break

    @staticmethod
    def _ssl_cert_id(source: Any) -> Optional[str]:
        cert = getattr(source, "Certificate", None)
        if cert is None:
            return None
        cert_id = str(getattr(cert, "CertId", "") or "").strip()
        return cert_id or None

    @staticmethod
    def _rule_domains(rule: Any) -> List[str]:
        domains: List[str] = []
        primary_domain = str(getattr(rule, "Domain", "") or "").strip()
        if primary_domain:
            domains.append(primary_domain)
        extra_domains = getattr(rule, "Domains", None) or []
        if isinstance(extra_domains, str):
            extra_domains = [extra_domains]
        for raw_domain in extra_domains:
            domain = str(raw_domain or "").strip()
            if domain and domain not in domains:
                domains.append(domain)
        return domains

    async def _get_ssl_cdn_bindings(self, certificate_id: str) -> List[CloudProductBinding]:
        def _run():
            def make_request():
                return ssl_models.DescribeHostCdnInstanceListRequest()

            bindings: List[CloudProductBinding] = []
            seen: set[str] = set()
            for item in self._iter_ssl_host_instances(
                self._ssl_client,
                make_request,
                "DescribeHostCdnInstanceList",
                certificate_id,
            ):
                domain = str(getattr(item, "Domain", "") or "").strip()
                status = str(getattr(item, "Status", "") or "").strip().lower()
                if not domain or status not in ("online", "normal"):
                    continue
                cert_id = str(getattr(item, "CertId", "") or "").strip() or None
                if domain in seen:
                    continue
                seen.add(domain)
                billing = str(getattr(item, "HttpsBillingSwitch", "") or "").strip().lower()
                if billing not in ("on", "off"):
                    billing = "on"
                bindings.append(
                    CloudProductBinding(
                        provider_name=self.name,
                        product_type="cdn",
                        product_id=domain,
                        domain=domain,
                        resource_type="cdn",
                        resource_name=domain,
                        status=status,
                        cert_id=cert_id,
                        metadata={
                            "https_billing": billing,
                            "cdn_deploy_mode": "ssl",
                        },
                    )
                )
            LOGGER.info("[%s] 腾讯云 CDN 发现 %d 个 HTTPS 绑定", self.name, len(bindings))
            return bindings

        return await self._call_provider_api(
            "获取腾讯云 CDN 域名列表",
            _run,
            default=[],
            retries=1,
            log_level="warning",
            raise_on_error=True,
        )

    @staticmethod
    def _live_https_ready(status: Any) -> bool:
        """云直播 Status=1 HTTPS 已开启；Status=-1 已开 HTTPS 但未关联证书。"""
        try:
            return int(status) in (1, -1)
        except (TypeError, ValueError):
            return str(status or "").strip() in ("1", "-1")

    async def _get_ssl_live_bindings(self, certificate_id: str) -> List[CloudProductBinding]:
        def _run():
            req = ssl_models.DescribeHostLiveInstanceListRequest()
            req.CertificateId = str(certificate_id or "").strip()
            req.IsCache = 0
            req.Filters = [self._ssl_domain_match_filter()]
            resp = self._ssl_client.DescribeHostLiveInstanceList(req)
            bindings: List[CloudProductBinding] = []
            seen: set[str] = set()
            for item in list(getattr(resp, "InstanceList", None) or []):
                domain = str(getattr(item, "Domain", "") or "").strip()
                if not domain or not self._live_https_ready(getattr(item, "Status", None)):
                    continue
                cert_id = str(getattr(item, "CertId", "") or "").strip() or None
                if domain in seen:
                    continue
                seen.add(domain)
                bindings.append(
                    CloudProductBinding(
                        provider_name=self.name,
                        product_type="live",
                        product_id=domain,
                        domain=domain,
                        resource_type="live",
                        resource_name=domain,
                        status="online",
                        cert_id=cert_id,
                        metadata={"live_deploy_mode": "ssl"},
                    )
                )
            LOGGER.info("[%s] 腾讯云云直播发现 %d 个 HTTPS 绑定", self.name, len(bindings))
            return bindings

        return await self._call_provider_api(
            "获取腾讯云云直播域名列表",
            _run,
            default=[],
            retries=1,
            log_level="warning",
            raise_on_error=True,
        )

    async def _get_ssl_teo_bindings(self, certificate_id: str) -> List[CloudProductBinding]:
        def _run():
            def make_request():
                return ssl_models.DescribeHostTeoInstanceListRequest()

            bindings: List[CloudProductBinding] = []
            seen: set[str] = set()
            for item in self._iter_ssl_host_instances(
                self._ssl_client,
                make_request,
                "DescribeHostTeoInstanceList",
                certificate_id,
            ):
                domain = str(getattr(item, "Host", "") or "").strip()
                if not domain or domain in seen:
                    continue
                seen.add(domain)
                cert_id = str(getattr(item, "CertId", "") or "").strip() or None
                status = str(getattr(item, "Status", "") or "").strip().lower()
                bindings.append(
                    CloudProductBinding(
                        provider_name=self.name,
                        product_type="teo",
                        product_id=domain,
                        domain=domain,
                        resource_type="teo",
                        resource_name=domain,
                        status=status,
                        cert_id=cert_id,
                        metadata={"zone_id": str(getattr(item, "ZoneId", "") or "").strip()},
                    )
                )
            LOGGER.info("[%s] 腾讯云 EdgeOne 发现 %d 个证书部署目标", self.name, len(bindings))
            return bindings

        return await self._call_provider_api(
            "获取腾讯云 EdgeOne 域名列表",
            _run,
            default=[],
            retries=1,
            log_level="warning",
            raise_on_error=True,
        )

    def _scan_ssl_clb_bindings_for_region(
        self,
        client: Any,
        region: str,
        seen: set[tuple[str, str, str]],
        certificate_id: str,
    ) -> List[CloudProductBinding]:
        bindings: List[CloudProductBinding] = []

        def make_request():
            return ssl_models.DescribeHostClbInstanceListRequest()

        for lb in self._iter_ssl_host_instances(
            client,
            make_request,
            "DescribeHostClbInstanceList",
            certificate_id,
        ):
            lb_id = str(getattr(lb, "LoadBalancerId", "") or "").strip()
            if not lb_id:
                continue
            lb_name = str(getattr(lb, "LoadBalancerName", "") or "").strip() or lb_id
            for listener in list(getattr(lb, "Listeners", None) or []):
                protocol = str(getattr(listener, "Protocol", "") or "").strip().upper()
                if protocol and protocol not in ("HTTPS", "TCP_SSL"):
                    continue
                listener_id = str(getattr(listener, "ListenerId", "") or "").strip()
                if not listener_id:
                    continue
                sni_switch = int(getattr(listener, "SniSwitch", 0) or 0) == 1
                listener_cert_id = self._ssl_cert_id(listener)
                common_metadata = {
                    "lb_name": lb_name,
                    "listener_id": listener_id,
                    "listener_name": str(getattr(listener, "ListenerName", "") or ""),
                    "sni_switch": sni_switch,
                    "protocol": protocol,
                    "region": region,
                }
                if not sni_switch:
                    key = (region, listener_id, "")
                    if key in seen:
                        continue
                    seen.add(key)
                    bindings.append(
                        CloudProductBinding(
                            provider_name=self.name,
                            product_type="clb",
                            product_id=f"{region}:{lb_id}:{listener_id}",
                            domain="",
                            resource_type="clb",
                            resource_name=lb_name,
                            status="online",
                            cert_id=listener_cert_id,
                            metadata={**common_metadata, "listener_scope": True},
                        )
                    )
                    continue
                rules = list(getattr(listener, "Rules", None) or [])
                for rule in rules:
                    if getattr(rule, "IsMatch", None) is False:
                        continue
                    cert_id = self._ssl_cert_id(rule) or listener_cert_id or None
                    for domain in self._rule_domains(rule):
                        key = (region, listener_id, domain)
                        if key in seen:
                            continue
                        seen.add(key)
                        bindings.append(
                            CloudProductBinding(
                                provider_name=self.name,
                                product_type="clb",
                                product_id=f"{region}:{lb_id}:{listener_id}",
                                domain=domain,
                                resource_type="clb",
                                resource_name=lb_name,
                                status="online",
                                cert_id=cert_id,
                                metadata=common_metadata,
                            )
                        )
        return bindings

    async def _get_ssl_lb_bindings(self, certificate_id: str) -> List[CloudProductBinding]:
        def _run():
            bindings: List[CloudProductBinding] = []
            seen: set[tuple[str, str, str]] = set()
            scan_regions = self._get_clb_scan_regions()
            skipped_regions: List[str] = []

            for region in scan_regions:
                try:
                    client = self._get_ssl_client(region)
                    bindings.extend(
                        self._scan_ssl_clb_bindings_for_region(
                            client,
                            region,
                            seen,
                            certificate_id,
                        )
                    )
                except Exception as exc:
                    skipped_regions.append(region)
                    LOGGER.warning(
                        "[%s] 腾讯云 CLB 扫描失败，跳过地域: region=%s err=%s",
                        self.name,
                        region,
                        exc,
                    )

            if skipped_regions:
                raise RuntimeError(
                    f"腾讯云 CLB SSL 扫描存在失败地域: {','.join(skipped_regions)}"
                )
            LOGGER.info(
                "[%s] 腾讯云 CLB 在 %d 个地域发现 %d 个 HTTPS 绑定",
                self.name,
                len(scan_regions),
                len(bindings),
            )
            return bindings

        return await self._call_provider_api(
            "获取腾讯云 CLB 列表",
            _run,
            default=[],
            retries=1,
            log_level="warning",
            raise_on_error=True,
        )

    def _scan_ssl_cos_bindings_for_region(
        self,
        client: Any,
        seen: set[tuple[str, str, str]],
        certificate_id: str,
    ) -> tuple[List[CloudProductBinding], int]:
        def make_request():
            req = ssl_models.DescribeHostCosInstanceListRequest()
            req.ResourceType = "cos"
            return req

        bindings: List[CloudProductBinding] = []
        raw_total = 0
        for item in self._iter_ssl_host_instances(
            client,
            make_request,
            "DescribeHostCosInstanceList",
            certificate_id,
        ):
            raw_total += 1
            status = str(getattr(item, "Status", "") or "").strip().upper()
            if status != "ENABLED":
                continue
            domain = str(getattr(item, "Domain", "") or "").strip()
            bucket = str(getattr(item, "Bucket", "") or "").strip()
            region = str(getattr(item, "Region", "") or "").strip()
            if not domain or not bucket or not region:
                continue
            cert_id = str(getattr(item, "CertId", "") or "").strip() or None
            key = (region, bucket, domain)
            if key in seen:
                continue
            seen.add(key)
            bindings.append(
                CloudProductBinding(
                    provider_name=self.name,
                    product_type="oss",
                    product_id=f"{region}:{bucket}:{domain}",
                    domain=domain,
                    resource_type="cos",
                    resource_name=bucket,
                    status=status,
                    cert_id=cert_id,
                    metadata={
                        "bucket": bucket,
                        "oss_region": region,
                    },
                )
            )
        return bindings, raw_total

    async def _get_ssl_oss_bindings(self, certificate_id: str) -> List[CloudProductBinding]:
        def _run():
            bindings: List[CloudProductBinding] = []
            seen: set[tuple[str, str, str]] = set()
            scan_regions = self._get_cos_scan_regions()
            skipped_regions: List[str] = []
            raw_total = 0

            for region in scan_regions:
                try:
                    client = self._get_ssl_client(region)
                    region_bindings, region_raw = self._scan_ssl_cos_bindings_for_region(
                        client,
                        seen,
                        certificate_id,
                    )
                    raw_total += region_raw
                    bindings.extend(region_bindings)
                    LOGGER.info(
                        "[%s] 腾讯云 COS SSL 扫描: region=%s raw=%d matched=%d",
                        self.name,
                        region,
                        region_raw,
                        len(region_bindings),
                    )
                except Exception as exc:
                    skipped_regions.append(region)
                    LOGGER.warning(
                        "[%s] 腾讯云 COS SSL 扫描失败，跳过地域: region=%s err=%s",
                        self.name,
                        region,
                        exc,
                    )

            if skipped_regions:
                raise RuntimeError(
                    f"腾讯云 COS SSL 扫描存在失败地域: {','.join(skipped_regions)}"
                )
            LOGGER.info(
                "[%s] 腾讯云 COS 在 %d 个地域发现 %d 个 HTTPS 绑定",
                self.name,
                len(scan_regions),
                len(bindings),
            )
            LOGGER.info(
                "[%s] 腾讯云 COS SSL 扫描: raw=%d matched=%d",
                self.name,
                raw_total,
                len(bindings),
            )
            return bindings

        return await self._call_provider_api(
            "获取腾讯云 COS SSL 可部署列表",
            _run,
            default=[],
            retries=1,
            log_level="warning",
            raise_on_error=True,
        )

    async def get_ssl_deploy_bindings(
        self,
        certificate_id: str,
        *,
        cdn_scan: bool,
        live_scan: bool,
        eo_scan: bool,
        lb_scan: bool,
        oss_scan: bool,
    ) -> List[CloudProductBinding]:
        """按已上传证书扫描可部署的腾讯云资源。"""
        normalized_cert_id = str(certificate_id or "").strip()
        if not normalized_cert_id:
            raise ValueError("腾讯云 SSL 部署扫描缺少 CertificateId")

        bindings: List[CloudProductBinding] = []
        if cdn_scan:
            bindings.extend(await self._get_ssl_cdn_bindings(normalized_cert_id))
        if live_scan:
            bindings.extend(await self._get_ssl_live_bindings(normalized_cert_id))
        if eo_scan:
            bindings.extend(await self._get_ssl_teo_bindings(normalized_cert_id))
        if lb_scan:
            bindings.extend(await self._get_ssl_lb_bindings(normalized_cert_id))
        if oss_scan:
            bindings.extend(await self._get_ssl_oss_bindings(normalized_cert_id))
        LOGGER.info(
            "[%s] 腾讯云 SSL 按证书扫描完成: certificate_id=%s bindings=%d",
            self.name,
            normalized_cert_id,
            len(bindings),
        )
        return bindings

    @staticmethod
    def _https_switch(https: Any) -> str:
        if https is None:
            return ""
        return str(getattr(https, "Switch", "") or "").strip().lower()

    @staticmethod
    def _https_cert_id(https: Any) -> Optional[str]:
        if https is None:
            return None
        cert_info = getattr(https, "CertInfo", None)
        if cert_info is None:
            return None
        cert_id = str(getattr(cert_info, "CertId", "") or "").strip()
        return cert_id or None

    @staticmethod
    def _https_billing_switch(item: Any) -> str:
        billing = getattr(item, "HttpsBilling", None)
        switch = str(getattr(billing, "Switch", "") or "").strip().lower() if billing is not None else ""
        return switch if switch in ("on", "off") else "on"

    async def get_cdn_bindings(self) -> List[CloudProductBinding]:
        """全量缓存刷新使用 CDN 产品接口，不依赖待部署证书。"""
        if not self._ENABLE_BINDING_CACHE_SCAN:
            return []
        def _run():
            bindings: List[CloudProductBinding] = []
            offset = 0
            while True:
                req = cdn_models.DescribeDomainsConfigRequest()
                req.Offset = offset
                req.Limit = _CDN_PAGE_SIZE
                resp = self._cdn_client.DescribeDomainsConfig(req)
                page = list(resp.Domains or [])
                for item in page:
                    status = str(item.Status or "").strip().lower()
                    https = getattr(item, "Https", None)
                    if status != "online" or self._https_switch(https) != "on":
                        continue
                    domain = str(item.Domain or "").strip()
                    if not domain:
                        continue
                    cert_info = getattr(https, "CertInfo", None)
                    bindings.append(
                        CloudProductBinding(
                            provider_name=self.name,
                            product_type="cdn",
                            product_id=domain,
                            domain=domain,
                            resource_type="cdn",
                            resource_name=domain,
                            status=status,
                            cert_id=self._https_cert_id(https),
                            cert_expire_time=getattr(cert_info, "ExpireTime", None) if cert_info else None,
                            metadata={
                                "cname": getattr(item, "Cname", "") or "",
                                "service_type": getattr(item, "ServiceType", "") or "",
                                "https_billing": self._https_billing_switch(item),
                                "cdn_deploy_mode": "modify",
                            },
                        )
                    )
                total = int(resp.TotalNumber or 0)
                offset += len(page)
                if not page or (total and offset >= total) or len(page) < _CDN_PAGE_SIZE:
                    break
            LOGGER.info("[%s] 腾讯云 CDN 原生接口发现 %d 个 HTTPS 域名", self.name, len(bindings))
            return bindings

        return await self._call_provider_api(
            "获取腾讯云 CDN 域名列表",
            _run,
            default=[],
            retries=1,
            log_level="warning",
            raise_on_error=True,
        )

    def _scan_native_clb_bindings_for_region(
        self,
        client: Any,
        region: str,
        seen: set[tuple[str, str, str]],
    ) -> List[CloudProductBinding]:
        bindings: List[CloudProductBinding] = []
        offset = 0
        while True:
            lb_req = clb_models.DescribeLoadBalancersRequest()
            lb_req.Offset = offset
            lb_req.Limit = _CLB_PAGE_SIZE
            lb_resp = client.DescribeLoadBalancers(lb_req)
            page = list(lb_resp.LoadBalancerSet or [])
            for lb in page:
                lb_id = str(lb.LoadBalancerId or "").strip()
                if not lb_id:
                    continue
                listener_req = clb_models.DescribeListenersRequest()
                listener_req.LoadBalancerId = lb_id
                try:
                    listener_resp = client.DescribeListeners(listener_req)
                except Exception as exc:
                    LOGGER.warning(
                        "[%s] 获取 CLB %s 监听器失败，跳过: region=%s err=%s",
                        self.name,
                        lb_id,
                        region,
                        exc,
                    )
                    continue
                for listener in listener_resp.Listeners or []:
                    protocol = str(getattr(listener, "Protocol", "") or "").strip().upper()
                    if protocol not in ("HTTPS", "TCP_SSL"):
                        continue
                    listener_id = str(listener.ListenerId or "").strip()
                    if not listener_id:
                        continue
                    sni_switch = int(getattr(listener, "SniSwitch", 0) or 0) == 1
                    if not sni_switch:
                        LOGGER.debug(
                            "[%s] CLB %s 监听器 %s 为 %s 非 SNI，产品接口无域名关系，交由部署阶段 SSL 扫描",
                            self.name,
                            lb_id,
                            listener_id,
                            protocol,
                        )
                        continue
                    listener_cert_id = self._ssl_cert_id(listener)
                    for rule in list(getattr(listener, "Rules", None) or []):
                        cert_id = self._ssl_cert_id(rule) or listener_cert_id
                        for domain in self._rule_domains(rule):
                            key = (region, listener_id, domain)
                            if key in seen:
                                continue
                            seen.add(key)
                            bindings.append(
                                CloudProductBinding(
                                    provider_name=self.name,
                                    product_type="clb",
                                    product_id=f"{region}:{lb_id}:{listener_id}",
                                    domain=domain,
                                    resource_type="clb",
                                    resource_name=getattr(lb, "LoadBalancerName", "") or lb_id,
                                    status="online",
                                    cert_id=cert_id,
                                    listener_port=getattr(listener, "Port", None),
                                    metadata={
                                        "lb_name": getattr(lb, "LoadBalancerName", "") or "",
                                        "listener_id": listener_id,
                                        "listener_name": getattr(listener, "ListenerName", "") or "",
                                        "sni_switch": sni_switch,
                                        "protocol": protocol,
                                        "region": region,
                                    },
                                )
                            )
            total = int(lb_resp.TotalCount or 0)
            offset += len(page)
            if not page or (total and offset >= total) or len(page) < _CLB_PAGE_SIZE:
                break
        return bindings

    async def get_lb_bindings(self) -> List[CloudProductBinding]:
        """全量缓存刷新使用 CLB 产品接口，不依赖待部署证书。"""
        if not self._ENABLE_BINDING_CACHE_SCAN:
            return []
        def _run():
            bindings: List[CloudProductBinding] = []
            seen: set[tuple[str, str, str]] = set()
            scan_regions = self._get_clb_scan_regions()
            failed_regions: List[str] = []
            for region in scan_regions:
                try:
                    bindings.extend(
                        self._scan_native_clb_bindings_for_region(
                            self._build_clb_client_for_region(region),
                            region,
                            seen,
                        )
                    )
                except Exception as exc:
                    failed_regions.append(region)
                    LOGGER.warning(
                        "[%s] 腾讯云 CLB 原生扫描失败: region=%s err=%s",
                        self.name,
                        region,
                        exc,
                    )
            if failed_regions:
                raise RuntimeError(
                    f"腾讯云 CLB 产品扫描存在失败地域: {','.join(failed_regions)}"
                )
            LOGGER.info("[%s] 腾讯云 CLB 原生接口发现 %d 个 HTTPS 绑定", self.name, len(bindings))
            return bindings

        return await self._call_provider_api(
            "获取腾讯云 CLB 列表",
            _run,
            default=[],
            retries=1,
            log_level="warning",
            raise_on_error=True,
        )

    async def get_live_bindings(self) -> List[CloudProductBinding]:
        if not self._ENABLE_BINDING_CACHE_SCAN:
            return []
        def _run():
            bindings: List[CloudProductBinding] = []
            page_num = 1
            while True:
                req = live_models.DescribeLiveDomainsRequest()
                req.DomainStatus = 1
                req.DomainType = 1
                req.PageSize = _LIVE_PAGE_SIZE
                req.PageNum = page_num
                resp = self._live_client.DescribeLiveDomains(req)
                page = list(getattr(resp, "DomainList", None) or [])
                for item in page:
                    domain = str(getattr(item, "Name", "") or "").strip()
                    if not domain or int(getattr(item, "Status", 0) or 0) != 1:
                        continue
                    cert_req = live_models.DescribeLiveDomainCertRequest()
                    cert_req.DomainName = domain
                    try:
                        cert_resp = self._live_client.DescribeLiveDomainCert(cert_req)
                    except Exception as exc:
                        LOGGER.debug(
                            "[%s] 云直播域名未获取到 HTTPS 证书，跳过: domain=%s err=%s",
                            self.name,
                            domain,
                            exc,
                        )
                        continue
                    cert_info = getattr(cert_resp, "DomainCertInfo", None)
                    if cert_info is None or not self._live_https_ready(
                        getattr(cert_info, "Status", None)
                    ):
                        continue
                    cert_id = str(
                        getattr(cert_info, "CloudCertId", "")
                        or getattr(cert_info, "CertId", "")
                        or ""
                    ).strip() or None
                    bindings.append(
                        CloudProductBinding(
                            provider_name=self.name,
                            product_type="live",
                            product_id=domain,
                            domain=domain,
                            resource_type="live",
                            resource_name=domain,
                            status="online",
                            cert_id=cert_id,
                            cert_expire_time=str(getattr(cert_info, "CertExpireTime", "") or "") or None,
                            metadata={
                                "live_deploy_mode": "ssl",
                                "play_type": getattr(item, "PlayType", None),
                                "cname": str(getattr(item, "CurrentCName", "") or ""),
                            },
                        )
                    )
                total = int(getattr(resp, "AllCount", 0) or 0)
                if not page or page_num * _LIVE_PAGE_SIZE >= total:
                    break
                page_num += 1
            LOGGER.info("[%s] 腾讯云云直播产品接口发现 %d 个 HTTPS 播放域名", self.name, len(bindings))
            return bindings

        return await self._call_provider_api(
            "获取腾讯云云直播产品绑定",
            _run,
            default=[],
            retries=1,
            log_level="warning",
            raise_on_error=True,
        )

    def _list_cos_domain_rules(self) -> List[dict[str, str]]:
        result: List[dict[str, str]] = []
        marker = ""
        while True:
            response = self._cos_client.list_buckets(Marker=marker, MaxKeys=2000)
            buckets = ((response.get("Buckets") or {}).get("Bucket") or [])
            if isinstance(buckets, dict):
                buckets = [buckets]
            for bucket_info in buckets:
                bucket = str(bucket_info.get("Name") or "").strip()
                region = str(bucket_info.get("Location") or "").strip().lower()
                if not bucket or not region:
                    continue
                try:
                    with suppress_cos_client_logs():
                        domain_response = self._build_cos_client_for_region(region).get_bucket_domain(
                            Bucket=bucket
                        )
                except CosServiceError as exc:
                    if exc.get_error_code() == "DomainConfigNotFoundError":
                        LOGGER.debug(
                            "[%s] COS 存储桶未配置自定义域名: bucket=%s region=%s",
                            self.name,
                            bucket,
                            region,
                        )
                        continue
                    LOGGER.warning(
                        "[%s] 查询 COS 自定义域名失败，跳过存储桶: bucket=%s region=%s err=%s",
                        self.name,
                        bucket,
                        region,
                        exc,
                    )
                    raise
                except Exception as exc:
                    LOGGER.warning(
                        "[%s] 查询 COS 自定义域名失败，跳过存储桶: bucket=%s region=%s err=%s",
                        self.name,
                        bucket,
                        region,
                        exc,
                    )
                    raise
                rules = domain_response.get("DomainRule") or []
                if isinstance(rules, dict):
                    rules = [rules]
                for rule in rules:
                    domain = str(rule.get("Name") or "").strip()
                    status = str(rule.get("Status") or "").strip().upper()
                    if domain and status == "ENABLED":
                        result.append(
                            {
                                "bucket": bucket,
                                "region": region,
                                "domain": domain,
                                "domain_type": str(rule.get("Type") or ""),
                            }
                        )
            is_truncated = str(response.get("IsTruncated") or "false").strip().lower() == "true"
            next_marker = str(response.get("NextMarker") or response.get("Marker") or "").strip()
            if not is_truncated or not next_marker or next_marker == marker:
                break
            marker = next_marker
        return result

    async def get_oss_bindings(self) -> List[CloudProductBinding]:
        if not self._ENABLE_BINDING_CACHE_SCAN:
            return []
        async def _run():
            rules = await asyncio.to_thread(self._list_cos_domain_rules)
            tls_results = await probe_domains_batch([item["domain"] for item in rules]) if rules else {}
            bindings: List[CloudProductBinding] = []
            for item in rules:
                domain = item["domain"]
                cert_info = tls_results.get(domain)
                if cert_info is None:
                    continue
                expires_at = cert_info.get("exp") if isinstance(cert_info, dict) else None
                if expires_at is not None and hasattr(expires_at, "isoformat"):
                    expires_at = expires_at.isoformat()
                region = item["region"]
                bucket = item["bucket"]
                bindings.append(
                    CloudProductBinding(
                        provider_name=self.name,
                        product_type="oss",
                        product_id=f"{region}:{bucket}:{domain}",
                        domain=domain,
                        resource_type="cos",
                        resource_name=bucket,
                        status="ENABLED",
                        cert_expire_time=str(expires_at) if expires_at else None,
                        metadata={
                            "bucket": bucket,
                            "oss_region": region,
                            "domain_type": item["domain_type"],
                            "tls_probe": True,
                        },
                    )
                )
            LOGGER.info("[%s] 腾讯云 COS 产品接口发现 %d 个 TLS 自定义域名", self.name, len(bindings))
            return bindings

        return await self._call_provider_api(
            "获取腾讯云 COS 产品绑定",
            _run,
            default=[],
            retries=1,
            log_level="warning",
            raise_on_error=True,
        )
