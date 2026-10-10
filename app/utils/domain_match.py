from __future__ import annotations

import re
from typing import Iterable, Optional, Set


def normalize_domain(domain: str) -> str:
    if not domain:
        raise ValueError("domain 不能为空")
    return str(domain).strip().lower().rstrip(".")


def normalize_blacklist(items: list[str]) -> list[str]:
    """校验黑名单域名并归一化去重，非法配置提前报错。"""
    result: list[str] = []
    for item in items:
        domain = normalize_domain(item)
        base = domain[2:] if domain.startswith("*.") else domain
        labels = base.split(".")
        if len(base) > 253 or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in labels
        ):
            raise ValueError(f"黑名单域名无效: {item!r}")
        if domain not in result:
            result.append(domain)
    return result


def find_blacklisted_domain(domains: Iterable[str], blacklist: Iterable[str]) -> Optional[str]:
    """查找黑名单命中规则：普通域名精确匹配，通配域名匹配直属一级子域。"""
    blocked = list(dict.fromkeys(normalize_domain(item) for item in blacklist))
    for domain in domains:
        normalized = normalize_domain(domain)
        if normalized in blocked:
            return normalized
        for rule in blocked:
            if match_domain({rule}, normalized) is not None:
                return rule
    return None


def build_cert_domains(main_domain: str, sans: Iterable[str]) -> Set[str]:
    domains: Set[str] = {normalize_domain(main_domain)}
    for san in sans or []:
        domains.add(normalize_domain(san))
    return domains


def match_domain(
    cert_domains: Set[str],
    target_domain: str,
) -> Optional[str]:
    # 严格按 RFC 6125 §6.4.3 单级通配语义：*.suffix 仅匹配 suffix 之上紧邻一级 label
    norm_target = normalize_domain(target_domain)
    if norm_target in cert_domains:
        return "exact"

    for item in cert_domains:
        if not item.startswith("*."):
            continue
        suffix = item[2:]
        if not norm_target.endswith(f".{suffix}"):
            continue
        prefix = norm_target[: -len(suffix) - 1]
        if "." not in prefix:
            return "wildcard-single"
    return None

