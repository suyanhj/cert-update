from __future__ import annotations

from typing import Iterable, Optional, Set


def normalize_domain(domain: str) -> str:
    if not domain:
        raise ValueError("domain 不能为空")
    return str(domain).strip().lower().rstrip(".")


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

