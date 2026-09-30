from app.utils.logger import get_logger
import ipaddress
from urllib.parse import parse_qs, urlparse
from typing import List, Optional, Set

from app.schemas.config import ProviderConfig
from app.schemas.provider import CloudProductBinding
from huaweicloudsdkdns.v2 import model as dns_models
from huaweicloudsdkcdn.v2 import model as cdn_models
from huaweicloudsdkecs.v2 import model as ecs_models
from huaweicloudsdkelb.v3 import model as elb_models
from huaweicloudsdkbss.v2 import model as bss_models
from huaweicloudsdkwaf.v1 import model as waf_models

from app.utils.huawei_sdk import (
    HuaweiBasicCredentialMixin,
    HuaweiCdnClientMixin,
    build_dns_client,
    build_ecs_client,
    build_elb_client,
    build_bss_client,
    build_global_credentials,
    build_waf_client,
)

from .base import Provider

LOGGER = get_logger("provider")


class HuaweiCloudProvider(HuaweiBasicCredentialMixin, HuaweiCdnClientMixin, Provider):
    """华为云 Provider，支持 DNS、CDN 域名发现"""

    domain_roles = frozenset({"dns"})

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        self._region = config.credentials.get("region", "cn-north-4")
        self._ak = config.credentials.get("access_key_id", "")
        self._sk = config.credentials.get("access_key_secret", "")
        self._project_id = config.credentials.get("project_id", "")
        self._enterprise_project_id = config.credentials.get("enterprise_project_id", "0") or "0"
        self._credentials = None
        credentials = self._get_credentials()
        self._cdn_client = None  # lazy via mixin
        self._dns_client = build_dns_client(region=self._region, credentials=credentials)
        self._ecs_client = build_ecs_client(region=self._region, credentials=credentials)
        self._elb_client = build_elb_client(region=self._region, credentials=credentials)
        self._waf_client = build_waf_client(region=self._region, credentials=credentials)
        self._cert_domains_cache: dict[str, List[str]] = {}
        # BSS 客户运营能力客户端懒加载，仅在 ECS 到期采集时需要；部分 region 可能不受支持。
        self._bss_client = None

    @staticmethod
    def _normalize_binding_domain(raw: str) -> Optional[str]:
        value = str(raw or "").strip().lower().rstrip(".")
        if not value:
            return None
        if value.startswith("*."):
            return None
        try:
            ipaddress.ip_address(value)
            return None
        except Exception:
            return value

    @staticmethod
    def _split_domain_candidates(raw: str) -> List[str]:
        text = str(raw or "").strip()
        if not text:
            return []
        text = text.replace(";", ",")
        return [item.strip() for item in text.split(",") if item.strip()]

    @staticmethod
    def _parse_cert_id(cert_ref: str) -> str:
        value = str(cert_ref or "").strip()
        if not value:
            return ""
        value = value.split("?", 1)[0].split("#", 1)[0].rstrip("/")
        if "/" not in value:
            return value
        return value.rsplit("/", 1)[-1]

    def _extract_domains_from_certificate(self, cert_ref: str) -> List[str]:
        cert_id = self._parse_cert_id(cert_ref)
        if not cert_id:
            return []
        if cert_id in self._cert_domains_cache:
            return self._cert_domains_cache[cert_id]

        try:
            req = elb_models.ShowCertificateRequest(certificate_id=cert_id)
            resp = self._elb_client.show_certificate(req)
            cert = resp.certificate
        except Exception as e:
            error_code = getattr(e, "error_code", "") or ""
            error_msg = getattr(e, "error_msg", "") or str(e)
            LOGGER.debug(
                "华为云 ELB 证书查询失败 cert_id=%s code=%s msg=%s",
                cert_id,
                error_code,
                error_msg,
            )
            self._cert_domains_cache[cert_id] = []
            return []

        domains: Set[str] = set()
        main_domain = self._normalize_binding_domain(cert.common_name or "")
        if main_domain:
            domains.add(main_domain)
        for item in cert.subject_alternative_names or []:
            domain = self._normalize_binding_domain(item)
            if domain:
                domains.add(domain)
        for item in self._split_domain_candidates(cert.domain or ""):
            domain = self._normalize_binding_domain(item)
            if domain:
                domains.add(domain)

        resolved = sorted(domains)
        self._cert_domains_cache[cert_id] = resolved
        return resolved

    def _extract_listener_cert_refs(self, listener: elb_models.Listener) -> List[tuple[str, str]]:
        refs: List[tuple[str, str]] = []
        default_ref = str(listener.default_tls_container_ref or "").strip()
        if default_ref:
            refs.append(("default", default_ref))
        for ref in listener.sni_container_refs or []:
            ref_text = str(ref or "").strip()
            if ref_text:
                refs.append(("sni", ref_text))
        return refs


    async def get_domain_list(self) -> List[dict]:
        """获取华为云 DNS 托管的域名列表。"""
        def _run():
            
            client = self._dns_client
            request = dns_models.ListPublicZonesRequest()
            response = client.list_public_zones(request)
            
            domains = []
            for zone in response.zones or []:
                domain_name = zone.name.rstrip(".")
                domains.append({
                    "domain": domain_name,
                    "zone_id": zone.id,
                    "status": zone.status,
                    "record_count": zone.record_num,
                })
            
            LOGGER.info("华为云发现 %d 个域名", len(domains))
            return domains

        return await self._call_provider_api(
            "获取华为云域名列表",
            _run,
            default=[],
            retries=1,
        )

    async def get_dns_records(self, domain: str) -> List[dict]:
        """获取指定域名的 DNS 解析记录。"""
        def _run():
            
            client = self._dns_client
            
            # 先获取 zone_id
            zones_request = dns_models.ListPublicZonesRequest()
            zones_response = client.list_public_zones(zones_request)
            
            zone_id = None
            for zone in zones_response.zones or []:
                if zone.name.rstrip(".") == domain:
                    zone_id = zone.id
                    break
            
            if not zone_id:
                LOGGER.warning("未找到域名 %s 的 Zone", domain)
                return []
            
            # 获取记录
            request = dns_models.ListRecordSetsByZoneRequest(zone_id=zone_id)
            response = client.list_record_sets_by_zone(request)
            
            records = []
            for record in response.recordsets or []:
                name = record.name.rstrip(".")
                rr = name[:-len(domain)-1] if name.endswith(f".{domain}") else name
                if rr == domain:
                    rr = "@"
                
                records.append({
                    "record_id": record.id,
                    "subdomain": rr,
                    "full_domain": name,
                    "type": record.type,
                    "value": record.records[0] if record.records else "",
                    "ttl": record.ttl,
                })
            
            LOGGER.debug("域名 %s 发现 %d 条解析记录", domain, len(records))
            return records

        return await self._call_provider_api(
            f"获取 {domain} DNS 记录",
            _run,
            default=[],
            retries=1,
        )

    async def discover_domains(self) -> List[dict]:
        """兼容旧接口，复用 get_domain_list。"""
        return await self.get_domain_list()

    async def get_cdn_bindings(self) -> List[CloudProductBinding]:
        """获取华为云 CDN 绑定。"""
        def _run():
            bindings = []
            
            client = self._get_cdn_client()
            request = cdn_models.ListDomainsRequest()
            response = client.list_domains(request)
            
            for domain in response.domains or []:
                if str(domain.domain_status or "").lower() != "online":
                    continue
                bindings.append(
                    CloudProductBinding(
                        provider_name=self.name,
                        product_type="cdn",
                        product_id=domain.id,
                        domain=domain.domain_name,
                        metadata={
                            "status": domain.domain_status,
                            "cname": domain.cname,
                        },
                    )
                )
            
            LOGGER.info("[%s] 华为云 CDN 发现 %d 个域名绑定", self.name, len(bindings))
            return bindings

        return await self._call_provider_api(
            "获取华为云 CDN 绑定",
            _run,
            default=[],
            retries=1,
        )

    async def get_lb_bindings(self) -> List[CloudProductBinding]:
        """获取华为云 ELB 的 HTTPS 绑定。"""
        def _run():

            client = self._elb_client
            bindings: List[CloudProductBinding] = []

            marker = None
            while True:
                req = elb_models.ListListenersRequest(
                    protocol=["HTTPS", "TERMINATED_HTTPS"],
                    limit=100,
                    marker=marker,
                )
                resp = client.list_listeners(req)
                for listener in resp.listeners or []:
                    if not listener.admin_state_up:
                        continue
                    listener_id = str(listener.id or "").strip()
                    if not listener_id:
                        continue
                    lb_id = ""
                    if listener.loadbalancers:
                        lb_id = listener.loadbalancers[0].id

                    cert_refs = self._extract_listener_cert_refs(listener)
                    if not cert_refs:
                        continue

                    for cert_source, cert_ref in cert_refs:
                        cert_id = self._parse_cert_id(cert_ref)
                        domains = self._extract_domains_from_certificate(cert_ref)
                        for domain in domains:
                            bindings.append(
                                CloudProductBinding(
                                    provider_name=self.name,
                                    product_type="elb",
                                    product_id=listener_id,
                                    domain=domain,
                                    resource_type="elb",
                                    resource_name=listener.name or listener_id,
                                    status="active" if listener.admin_state_up else "inactive",
                                    cert_id=cert_id or None,
                                    listener_port=listener.protocol_port,
                                    metadata={
                                        "lb_id": lb_id,
                                        "project_id": listener.project_id or "",
                                        "listener_id": listener_id,
                                        "listener_name": listener.name or "",
                                        "listener_protocol": listener.protocol,
                                        "cert_source": cert_source,
                                        "cert_ref": cert_ref,
                                    },
                                )
                            )
                if not resp.listeners or len(resp.listeners) < 100:
                    break
                marker = resp.listeners[-1].id

            LOGGER.info("[%s] 华为云 ELB 发现 %d 个 HTTPS 绑定", self.name, len(bindings))
            return bindings

        return await self._call_provider_api(
            "获取华为云 ELB 绑定",
            _run,
            default=[],
            retries=1,
        )

    async def get_waf_bindings(self) -> List[CloudProductBinding]:
        """获取华为云 WAF 云模式 HTTPS 防护域名证书绑定。"""

        def _run():
            bindings: List[CloudProductBinding] = []
            page = 1
            page_size = 100

            while True:
                request = waf_models.ListHostRequest(
                    enterprise_project_id=self._enterprise_project_id,
                    page=page,
                    pagesize=page_size,
                )
                response = self._waf_client.list_host(request)
                hosts = response.items or []

                for host in hosts:
                    host_id = str(host.id or host.hostid or "").strip()
                    hostname = self._normalize_binding_domain(host.hostname or "")
                    if not host_id or not hostname:
                        continue

                    enterprise_project_id = str(
                        host.enterprise_project_id or self._enterprise_project_id
                    ).strip() or "0"
                    try:
                        detail_request = waf_models.ShowHostRequest(
                            enterprise_project_id=enterprise_project_id,
                            instance_id=host_id,
                        )
                        detail = self._waf_client.show_host(detail_request)
                    except Exception as exc:
                        LOGGER.warning(
                            "[%s] 华为云 WAF 域名详情查询失败，跳过: host_id=%s domain=%s enterprise_project_id=%s err=%s",
                            self.name,
                            host_id,
                            hostname,
                            enterprise_project_id,
                            exc,
                        )
                        continue

                    protocol = str(detail.protocol or "").strip().upper()
                    if "HTTPS" not in protocol:
                        continue

                    protect_status = getattr(detail, "protect_status", None)
                    status = {
                        -1: "bypass",
                        0: "paused",
                        1: "active",
                    }.get(protect_status, "unknown")
                    cert_id = str(detail.certificateid or "").strip()
                    bindings.append(
                        CloudProductBinding(
                            provider_name=self.name,
                            product_type="waf",
                            product_id=host_id,
                            domain=hostname,
                            resource_type="waf",
                            resource_name=hostname,
                            status=status,
                            cert_id=cert_id or None,
                            metadata={
                                "certificate_name": str(detail.certificatename or ""),
                                "enterprise_project_id": enterprise_project_id,
                                "protocol": protocol,
                                "protect_status": protect_status,
                                "access_status": getattr(detail, "access_status", None),
                            },
                        )
                    )

                total = int(response.total or 0)
                if not hosts or len(hosts) < page_size or page * page_size >= total:
                    break
                page += 1

            LOGGER.info("[%s] 华为云 WAF 发现 %d 个 HTTPS 证书绑定", self.name, len(bindings))
            return bindings

        return await self._call_provider_api(
            "获取华为云 WAF 绑定",
            _run,
            default=[],
            retries=1,
        )

    async def get_ecs_instances(self) -> List[dict]:
        """采集当前项目下所有 ECS 主机信息，并补充包年包月到期时间。"""
        def _run():
            client = self._ecs_client
            instances: List[dict] = []
            limit = 100
            marker = None

            while True:
                request = ecs_models.ListServersDetailsRequest(
                    limit=limit,
                    marker=marker,
                )
                response = client.list_servers_details(request)
                servers = response.servers or []

                for server in servers:
                    instance_id = str(getattr(server, "id", "") or "").strip()
                    if not instance_id:
                        continue

                    server_name = getattr(server, "name", None) or instance_id

                    instances.append(
                        {
                            "name": self.name,
                            "provider": self.config.type,
                            "provider_name": self.name,
                            "instance_id": instance_id,
                            "instance_name": str(server_name),
                            "region": self._region,
                            "status": str(getattr(server, "status", "") or ""),
                            # 计费与到期信息通过 BSS 查询补充
                            "charge_type": "",
                            "renewal_status": "",
                            "auto_renewal": False,
                            "expires_at": "",
                            "days": 0,
                        }
                    )

                if not servers or len(servers) < limit:
                    break
                marker = servers[-1].id

            # 通过 BSS 查询包年包月资源到期时间，按 resource_id 和 ECS 实例 ID 关联
            # 部分地域暂不支持 BSS，该情况下仅返回基础实例信息。
            try:
                if self._bss_client is None:
                    self._bss_client = build_bss_client(
                        region="cn-north-1",
                        credentials=build_global_credentials(self._ak, self._sk),
                    )
                bss_client = self._bss_client
            except Exception as exc:
                LOGGER.warning(
                    "[%s] 初始化华为云 BSS 客户端失败，跳过 ECS 到期信息补充: region=%s err=%s",
                    self.name,
                    self._region,
                    exc,
                )
                LOGGER.debug(
                    "[%s] 初始化华为云 BSS 客户端失败详情: region=%s err=%s",
                    self.name,
                    self._region,
                    exc,
                    exc_info=True,
                )
                LOGGER.info(
                    "[%s] 华为云 ECS 发现 %d 台主机（BSS 不可用，未补充到期信息）",
                    self.name,
                    len(instances),
                )
                return instances

            bss_limit = 100
            bss_offset = 0
            bss_resources: dict[str, Any] = {}

            while True:
                req = bss_models.ListPayPerUseCustomerResourcesRequest(
                    service_type_code="hws.service.type.ec2",
                    region_code=self._region,
                    project_id=self._project_id or None,
                    limit=bss_limit,
                    offset=bss_offset,
                )
                resp = bss_client.list_pay_per_use_customer_resources(req)
                items = getattr(resp, "customer_resources", None) or []
                if not items:
                    break

                for item in items:
                    rid = str(getattr(item, "resource_id", "") or "").strip()
                    if not rid:
                        continue
                    bss_resources[rid] = item

                if len(items) < bss_limit:
                    break
                bss_offset += bss_limit

            # 将 BSS 的到期时间映射回 ECS 实例
            for inst in instances:
                rid = inst.get("instance_id", "")
                bss_item = bss_resources.get(rid)
                if not bss_item:
                    continue

                expire_time = getattr(bss_item, "expire_time", "") or ""
                charge_type = "prepaid"

                if expire_time:
                    try:
                        parsed = self.time.parse(expire_time)
                        inst["expires_at"] = self.time.to_local_tz(parsed)
                        inst["days"] = self.time.remaining_days(parsed)
                    except Exception:
                        inst["expires_at"] = expire_time
                        inst["days"] = 0

                inst["charge_type"] = charge_type

            LOGGER.info("[%s] 华为云 ECS 发现 %d 台主机（已尝试补充包年包月到期时间）", self.name, len(instances))
            return instances

        return await self._call_provider_api(
            "获取华为云 ECS 列表",
            _run,
            default=[],
            retries=1,
        )
