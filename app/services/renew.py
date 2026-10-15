from __future__ import annotations

import asyncio
from typing import Iterable, Optional

from app.schemas.acme import RenewedCert
from app.config import load_raw_config
from app.utils.acme_sh import AcmeShRenewer
from app.utils.certificate_policy import get_certificate_policy
from app.utils.manual_certificate import load_manual_certificate
from app.utils.domain_match import normalize_domain

async def resolve_renew_domain(raw_domain: str) -> str:
    """从原始目标解析实际续签证书，不使用采集快照改写域名。"""
    renewer = AcmeShRenewer()
    return await asyncio.to_thread(renewer.resolve_main_domain, raw_domain)


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
    *,
    main_domain: Optional[str] = None,
) -> RenewedCert:
    """续签原始目标；main_domain 仅传入本轮从 acme.sh 解析的证书标识。"""
    if not domain:
        raise ValueError("domain 不能为空")

    renewer = AcmeShRenewer()
    return await asyncio.to_thread(
        renewer.renew, domain, provider_name, force, main_domain=main_domain
    )


async def load_existing_certificate(
    domain: str,
    provider_name: Optional[str] = None,
    *,
    main_domain: Optional[str] = None,
) -> RenewedCert:
    """加载已有证书；main_domain 仅传入本轮解析结果，不再次匹配父域。"""
    if not domain:
        raise ValueError("domain 不能为空")

    policy = get_certificate_policy(load_raw_config(), domain)
    if policy.source == "manual":
        return await asyncio.to_thread(load_manual_certificate, domain, policy)
    renewer = AcmeShRenewer()
    return await asyncio.to_thread(
        renewer.load_existing, domain, provider_name, main_domain=main_domain
    )
