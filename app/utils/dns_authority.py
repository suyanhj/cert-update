"""公网权威 DNS 查询工具。"""

from __future__ import annotations

from collections.abc import Iterable

import dns.resolver


def normalize_nameservers(values: Iterable[object] | None) -> set[str]:
    """归一化 nameserver 名称，便于跨 API 与公网查询比较。"""
    if isinstance(values, str):
        values = [values]
    return {
        str(value or "").strip().lower().rstrip(".")
        for value in values or []
        if str(value or "").strip()
    }


class DnsAuthorityResolver:
    """通过系统递归解析器查询根域名当前公开的 NS 记录。"""

    def __init__(self, lifetime_seconds: float = 3.0) -> None:
        self.lifetime_seconds = max(0.1, float(lifetime_seconds))

    def resolve_nameservers(self, domain: str) -> set[str]:
        answer = dns.resolver.resolve(
            domain,
            "NS",
            lifetime=self.lifetime_seconds,
            search=False,
        )
        return normalize_nameservers(answer)
