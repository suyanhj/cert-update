"""Volcengine (火山引擎) Provider - 域名发现、DNS 记录管理"""

from __future__ import annotations

from app.utils.logger import get_logger
from typing import List, Optional

from app.schemas.config import ProviderConfig
from app.schemas.provider import CloudProductBinding
from app.utils.volcengine_sdk import build_volcengine_service, DNSApi, CDNApi

from .base import Provider

LOGGER = get_logger("provider")


class VolcengineCloudProvider(Provider):
    """火山引擎提供器，支持 DNS、CDN 域名发现与证书部署。"""

    domain_roles = frozenset({"dns"})

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        self._ak = config.credentials.get("access_key_id", "")
        self._sk = config.credentials.get("access_key_secret", "")
        self._region = config.credentials.get("region", "cn-north-1")
        self._dns_service = build_volcengine_service(
            access_key_id=self._ak, access_key_secret=self._sk,
            service="dns", region=self._region, service_cls=DNSApi,
        )
        self._cdn_service = build_volcengine_service(
            access_key_id=self._ak, access_key_secret=self._sk,
            service="cdn", region=self._region, service_cls=CDNApi,
        )

    async def get_domain_list(self) -> List[dict]:
        """获取 Volcengine DNS 托管的域名列表。"""
        def _run():
            service = self._dns_service
            
            # 调用 ListZones API
            response = service.list_zones({
                "PageNumber": 1,
                "PageSize": 100,
            })
            
            domains = []
            for zone in response.get("Zones", []):
                domains.append({
                    "domain": zone.get("ZoneName", "").rstrip("."),
                    "zone_id": zone.get("ZID"),
                    "status": zone.get("Status"),
                    "record_count": zone.get("RecordCount", 0),
                })
            
            LOGGER.info("Volcengine 发现 %d 个域名", len(domains))
            return domains

        return await self._call_provider_api(
            "获取 Volcengine 域名列表",
            _run,
            default=[],
            retries=1,
        )

    async def get_dns_records(self, domain: str) -> List[dict]:
        """获取指定域名的 DNS 解析记录。"""
        def _run():
            service = self._dns_service
            
            # 先获取 zone_id
            zones_response = service.list_zones({
                "PageNumber": 1,
                "PageSize": 100,
            })
            
            zone_id = None
            for zone in zones_response.get("Zones", []):
                if zone.get("ZoneName", "").rstrip(".") == domain:
                    zone_id = zone.get("ZID")
                    break
            
            if not zone_id:
                LOGGER.warning("未找到域名 %s 的 Zone", domain)
                return []
            
            # 获取记录
            response = service.list_records({
                "ZID": zone_id,
                "PageNumber": 1,
                "PageSize": 500,
            })
            
            records = []
            for record in response.get("Records", []):
                host = record.get("Host", "@")
                full_domain = f"{host}.{domain}" if host != "@" else domain
                
                records.append({
                    "record_id": record.get("RecordID"),
                    "subdomain": host,
                    "full_domain": full_domain,
                    "type": record.get("Type"),
                    "value": record.get("Value", ""),
                    "ttl": record.get("TTL"),
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
        """获取 Volcengine CDN 上绑定的所有域名。"""
        def _run():
            bindings = []
            service = self._cdn_service
            
            response = service.list_cdn_domains({
                "PageNum": 1,
                "PageSize": 100,
            })
            
            for domain in response.get("Data", []):
                if str(domain.get("Status", "")).lower() != "online":
                    continue
                bindings.append(
                    CloudProductBinding(
                        provider_name=self.name,
                        product_type="cdn",
                        product_id=domain.get("Domain"),
                        domain=domain.get("Domain"),
                        metadata={
                            "status": domain.get("Status"),
                            "cname": domain.get("Cname"),
                        },
                    )
                )
            
            LOGGER.info("Volcengine CDN 发现 %d 个域名绑定", len(bindings))
            return bindings

        return await self._call_provider_api(
            "获取 Volcengine CDN 绑定",
            _run,
            default=[],
            retries=1,
        )

    async def get_lb_bindings(self) -> List[CloudProductBinding]:
        """Volcengine ELB 绑定（暂未实现，返回空列表）。"""
        return []
