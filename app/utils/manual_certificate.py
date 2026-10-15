"""读取外部同步的 PEM 证书，并在部署前验证其可用性。"""

from cryptography import x509
from cryptography.hazmat.primitives import serialization

from app.schemas.acme import RenewedCert
from app.schemas.config import CertificatePolicyConfig
from app.utils.cert_parser import parse_cert_info_from_pem
from app.utils.domain_match import match_domain, normalize_domain
from app.utils.logger import get_logger
from app.utils.project_root import resolve_project_path
from app.utils.time import TimeUtil


LOGGER = get_logger("app")


def load_manual_certificate(domain: str, policy: CertificatePolicyConfig) -> RenewedCert:
    """每次读取完整链和私钥，失败时不切换来源、不调用 ACME。"""
    domain = normalize_domain(domain)
    if policy.source != "manual" or not policy.fullchain_path or not policy.key_path:
        raise ValueError(f"域名 {domain} 未配置完整的手动证书来源")
    fullchain_path = resolve_project_path(policy.fullchain_path)
    key_path = resolve_project_path(policy.key_path)
    LOGGER.info("读取手动证书: domain=%s fullchain=%s key=%s", domain, fullchain_path, key_path)
    try:
        fullchain_pem = fullchain_path.read_text(encoding="utf-8")
        key_pem = key_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"读取手动证书失败: domain={domain}，请检查证书和私钥路径及权限") from exc
    try:
        chain = x509.load_pem_x509_certificates(fullchain_pem.encode("utf-8"))
        if not chain:
            raise ValueError("证书链为空")
        key = serialization.load_pem_private_key(key_pem.encode("utf-8"), password=None)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"手动证书或私钥 PEM 无效: {domain}") from exc
    leaf = chain[0]
    public_format = (serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    if leaf.public_key().public_bytes(*public_format) != key.public_key().public_bytes(*public_format):
        raise ValueError(f"手动证书与私钥不匹配: {domain}")
    cert_pem = leaf.public_bytes(serialization.Encoding.PEM).decode("utf-8")
    parsed = parse_cert_info_from_pem(cert_pem)
    now = TimeUtil.now_utc()
    if parsed.not_before_utc > now:
        raise ValueError(f"手动证书尚未生效: {domain}")
    if parsed.not_after_utc <= now:
        raise ValueError(f"手动证书已过期: {domain}")
    if not match_domain({normalize_domain(san) for san in parsed.sans}, domain):
        raise ValueError(f"手动证书 SAN 未覆盖目标域名: {domain}")
    ca_pem = "".join(cert.public_bytes(serialization.Encoding.PEM).decode("utf-8") for cert in chain[1:])
    LOGGER.info("手动证书校验通过: domain=%s expires_at=%s", domain, parsed.not_after_utc.isoformat())
    return RenewedCert(
        domain=domain, cert_path=str(fullchain_path), key_path=str(key_path),
        fullchain_path=str(fullchain_path), ca_path=None,
        cert_pem=cert_pem, key_pem=key_pem, fullchain_pem=fullchain_pem, ca_pem=ca_pem,
        issued_at=parsed.not_before_utc, expires_at=parsed.not_after_utc,
        key_length=parsed.key_length, sans=parsed.sans, issuer=parsed.issuer, profile=parsed.profile,
    )
