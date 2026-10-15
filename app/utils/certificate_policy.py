"""域名证书策略解析，供编排、DNS 凭据选择和 UI 共用。"""

from app.schemas.config import CertificatePolicyConfig
from app.utils.domain_match import match_domain, normalize_domain


def get_certificate_policy(config, domain: str) -> CertificatePolicyConfig:
    """精确策略优先于单级通配；未配置时使用默认 ACME 策略。"""
    policies = config.get("certificate_policies", {})
    target = normalize_domain(domain)
    raw = policies.get(target)
    if raw is None:
        for rule, candidate in policies.items():
            if match_domain({rule}, target):
                raw = candidate
                break
    return CertificatePolicyConfig.model_validate(raw or {})


def get_dns_provider(config, domain: str) -> str | None:
    """通配 SAN 的 DNS 验证使用其基础域名的账号，显式策略优先。"""
    target = normalize_domain(domain)
    policy = get_certificate_policy(config, target)
    if policy.dns_provider:
        return policy.dns_provider
    if target.startswith("*."):
        return get_certificate_policy(config, target[2:]).dns_provider
    return None


def certificate_operation_skip(config, domain: str, *, automatic: bool = False) -> tuple[str, str] | None:
    """返回签发、续签被策略跳过的原因及提示，部署不调用此限制。"""
    policy = get_certificate_policy(config, domain)
    if policy.source == "manual":
        return "manual_certificate", f"域名 {domain} 使用手动证书，请使用仅部署"
    if automatic and not policy.auto_renew:
        return "auto_renew_disabled", f"域名 {domain} 已关闭自动续签，仍可手动操作"
    return None
