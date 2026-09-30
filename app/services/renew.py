from __future__ import annotations

import asyncio
from typing import Iterable, Optional, List

from app.schemas.acme import RenewedCert
from app.utils.acme_sh import AcmeShRenewer
from app.utils.store import CollectorSnapshotStore
from app.utils.logger import get_logger
from app.utils.domain_match import normalize_domain

_log = get_logger("app")


def _resolve_effective_renew_domain(raw_domain: str) -> str:
    """根据采集快照推导实际续签使用的 acme.sh 主域名。

    规则：
    1. 在 cert_list 中找到 domain==raw_domain 的证书记录；
    2. 如果该证书的 sans 包含泛域名 *.X，则直接返回 X（acme.sh Main_Domain 约定）；
    3. 否则，在整个 cert_list 中查找 sans 含 *.X 且 raw_domain 是 X 的子域，返回 X；
    4. 以上均未命中时，从当前证书的 [domain]+sans 中选标签数最少的非通配域名。
    """
    domain = str(raw_domain or "").strip()
    if not domain:
        return domain

    try:
        snapshot = CollectorSnapshotStore().get_all()
    except Exception:
        return domain

    cert_list = snapshot.get("cert_list", [])
    if not isinstance(cert_list, list):
        return domain

    # 在 cert_list 中找到 domain==raw_domain 的证书记录
    target = None
    for item in cert_list:
        if not isinstance(item, dict):
            continue
        d = str(item.get("domain", "") or "").strip()
        if d.lower() == domain.lower():
            target = item
            break

    # 当前证书的 sans 里找泛域名 *.X → 返回 X
    if target:
        sans = target.get("sans", [])
        if isinstance(sans, list):
            for s in sans:
                s_str = str(s or "").strip()
                if s_str.startswith("*."):
                    base = s_str[len("*."):].strip()
                    if base:
                        return base

    # 当前证书无泛域名 → 在整个 cert_list 中找覆盖该子域的泛域名证书
    # *.X 只覆盖单级子域 Y.X（Y 不含点），优先匹配最具体的（base 最长的）
    domain_lower = domain.lower()
    best_wildcard_base: str | None = None
    for item in cert_list:
        if not isinstance(item, dict):
            continue
        item_sans = item.get("sans", [])
        if not isinstance(item_sans, list):
            continue
        for s in item_sans:
            s_str = str(s or "").strip()
            if not s_str.startswith("*."):
                continue
            base = s_str[len("*."):].strip().lower()
            if not base or not domain_lower.endswith("." + base):
                continue
            prefix = domain_lower[:-(len(base) + 1)]
            if "." in prefix:
                continue
            if best_wildcard_base is None or len(base) > len(best_wildcard_base):
                best_wildcard_base = base
    if best_wildcard_base:
        return best_wildcard_base

    # 兜底：从当前证书的 [domain]+sans 中选标签数最少的非通配域名
    if target:
        names: List[str] = []
        d = str(target.get("domain", "") or "").strip()
        if d:
            names.append(d)
        sans = target.get("sans", [])
        if isinstance(sans, list):
            for s in sans:
                s_str = str(s or "").strip()
                if s_str:
                    names.append(s_str)

        if names:
            def _score(name: str) -> tuple[int, int, str]:
                raw = name.strip()
                base = raw.lstrip("*.").strip()
                if not base:
                    return (1, 0, raw)
                is_wildcard = 1 if raw.startswith("*.") else 0
                label_count = len(base.split("."))
                return (is_wildcard, label_count, base)

            best = min(names, key=_score)
            if best:
                return best

    return domain


def prepare_issue_domains(main_domain: str, domains: Iterable[str]) -> list[str]:
    """归一化用户确认的签发域名，并确保主域名始终位于首项。"""
    main = normalize_domain(main_domain)
    if not main:
        raise ValueError("签发主域名不能为空")
    if main.startswith("*."):
        raise ValueError("签发主域名不能是通配域名")

    result = [main]
    seen = {main}
    for raw_domain in domains:
        normalized = normalize_domain(str(raw_domain or ""))
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


async def issue_certificate(
    domain: str,
    sans: list[str],
    provider_name: Optional[str] = None,
) -> RenewedCert:
    """首次签发证书并返回签发后的证书信息。"""
    if not domain:
        raise ValueError("domain 不能为空")

    renewer = AcmeShRenewer()
    return await asyncio.to_thread(renewer.issue, domain, sans, provider_name)


async def renew_certificate(
    domain: str,
    provider_name: Optional[str] = None,
    force: bool = False,
) -> RenewedCert:
    """仅续签证书并返回续签后的证书信息。"""
    if not domain:
        raise ValueError("domain 不能为空")

    effective_domain = _resolve_effective_renew_domain(domain)
    if effective_domain != domain:
        _log.info("renew domain resolved: raw=%s effective=%s", domain, effective_domain)

    renewer = AcmeShRenewer()
    return await asyncio.to_thread(renewer.renew, effective_domain, provider_name, force)


async def load_existing_certificate(
    domain: str,
    provider_name: Optional[str] = None,
) -> RenewedCert:
    """仅加载已存在的证书（不触发 acme.sh --renew）。"""
    if not domain:
        raise ValueError("domain 不能为空")

    effective_domain = _resolve_effective_renew_domain(domain)
    if effective_domain != domain:
        _log.info("load_existing domain resolved: raw=%s effective=%s", domain, effective_domain)

    renewer = AcmeShRenewer()
    return await asyncio.to_thread(renewer.load_existing, effective_domain, provider_name)
