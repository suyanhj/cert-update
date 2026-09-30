"""Cloudflare Provider：只读发现 Zone 与 DNS 记录。"""

from __future__ import annotations

import asyncio
from typing import Any, List

from cloudflare import Cloudflare

from app.config import load_raw_config
from app.schemas.config import ProviderConfig
from app.utils.dns_authority import normalize_nameservers

from .base import Provider


_ZONE_PAGE_SIZE = 50
_DNS_RECORD_PAGE_SIZE = 5000
_PROBE_RECORD_TYPES = {"A", "AAAA", "CNAME"}


class CloudflareDNSProvider(Provider):
    """使用 API Token 读取 Cloudflare Zone 与 DNS 记录。"""

    domain_roles = frozenset({"dns"})

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        self._api_token = str(config.credentials.get("api_token", "") or "").strip()
        if not self._api_token:
            raise ValueError("cloudflare credentials.api_token 不能为空")
        self._account_id = str(config.credentials.get("account_id", "") or "").strip()
        self._client = Cloudflare(api_token=self._api_token)

    @staticmethod
    def _value(item: Any, field: str, default: Any = None) -> Any:
        """同时读取 SDK 模型和测试中的字典响应。"""
        if isinstance(item, dict):
            return item.get(field, default)
        return getattr(item, field, default)

    @classmethod
    def _nested_value(cls, item: Any, *fields: str) -> Any:
        current = item
        for field in fields:
            current = cls._value(current, field)
            if current is None:
                return None
        return current

    @staticmethod
    def _normalize_zone_name(name: Any) -> str:
        return str(name or "").strip().rstrip(".").lower()

    @classmethod
    def _relative_record_name(cls, record_name: Any, zone_name: str) -> str:
        full_name = cls._normalize_zone_name(record_name)
        normalized_zone = cls._normalize_zone_name(zone_name)
        if full_name == normalized_zone:
            return "@"
        suffix = f".{normalized_zone}"
        if full_name.endswith(suffix):
            return full_name[: -len(suffix)]
        return full_name

    def _list_active_zones(self) -> list[Any]:
        params: dict[str, Any] = {
            "status": "active",
            "per_page": _ZONE_PAGE_SIZE,
        }
        if self._account_id:
            params["account"] = {"id": self._account_id}
        # SDK 分页对象的迭代器会自动获取后续页面。
        return list(self._client.zones.list(**params))

    def _list_dns_records(self, zone_id: str, zone_name: str) -> list[dict]:
        records = self._client.dns.records.list(
            zone_id=zone_id,
            per_page=_DNS_RECORD_PAGE_SIZE,
        )
        result: list[dict] = []
        for item in records:
            full_domain = self._normalize_zone_name(self._value(item, "name"))
            if not full_domain:
                continue
            result.append(
                {
                    "record_id": str(self._value(item, "id", "") or ""),
                    "subdomain": self._relative_record_name(full_domain, zone_name),
                    "full_domain": full_domain,
                    "type": str(self._value(item, "type", "") or "").upper(),
                    "value": self._value(item, "content", ""),
                    "ttl": self._value(item, "ttl", 0),
                    "proxied": bool(self._value(item, "proxied", False)),
                    "status": "ENABLE",
                    "remark": str(self._value(item, "comment", "") or ""),
                }
            )
        return result

    async def get_dns_records(self, domain: str) -> List[dict]:
        target = self._normalize_zone_name(domain)
        zones = await self._call_provider_api(
            "查询 Cloudflare Zone",
            self._list_active_zones,
            default=[],
            retries=1,
        )
        for zone in zones:
            zone_name = self._normalize_zone_name(self._value(zone, "name"))
            zone_id = str(self._value(zone, "id", "") or "").strip()
            status = str(self._value(zone, "status", "") or "").lower()
            if zone_name == target and zone_id and status == "active":
                return await self._call_provider_api(
                    f"查询 Cloudflare DNS 记录 domain={target}",
                    lambda: self._list_dns_records(zone_id, zone_name),
                    default=[],
                    retries=1,
                )
        return []

    async def get_domain_list(self) -> List[dict]:
        zones = await self._call_provider_api(
            "查询 Cloudflare Zone",
            self._list_active_zones,
            default=[],
            retries=1,
        )
        try:
            parallel = int(load_raw_config().http_scan_parallel or 5)
        except Exception:
            parallel = 5
        sem = asyncio.Semaphore(max(1, parallel))

        result: list[dict] = []
        for zone in zones:
            zone_name = self._normalize_zone_name(self._value(zone, "name"))
            zone_id = str(self._value(zone, "id", "") or "").strip()
            status = str(self._value(zone, "status", "") or "").lower()
            if not zone_name or not zone_id or status != "active":
                continue
            zone_type = str(self._value(zone, "type", "") or "").lower()
            assigned_nameservers = normalize_nameservers(
                self._value(zone, "name_servers", []) or []
            )

            records = await self._call_provider_api(
                f"查询 Cloudflare DNS 记录 domain={zone_name}",
                lambda zone_id=zone_id, zone_name=zone_name: self._list_dns_records(
                    zone_id,
                    zone_name,
                ),
                default=[],
                retries=1,
            )
            tasks = [
                self._probe_domain(
                    record["full_domain"],
                    remark=record["remark"],
                    sem=sem,
                )
                for record in records
                if record["type"] in _PROBE_RECORD_TYPES
            ]
            subs = await asyncio.gather(*tasks) if tasks else []
            result.append(
                {
                    "domain": zone_name,
                    "zone_id": zone_id,
                    "status": status,
                    "type": zone_type,
                    "account_id": str(self._nested_value(zone, "account", "id") or ""),
                    "assigned_nameservers": sorted(assigned_nameservers),
                    "dns_authoritative": status == "active" and zone_type == "full",
                    "expires_at": "unknown",
                    "subs": subs,
                }
            )
        return result
