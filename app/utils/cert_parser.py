from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import dsa, ec, ed25519, ed448, rsa

from app.schemas.cert_parser import CertParsedInfo
from app.utils.time import TimeUtil


time_util = TimeUtil()


def parse_cert_info_from_pem(cert_pem: str) -> CertParsedInfo:
    """从 PEM 证书文本解析通用元信息。"""
    cert = x509.load_pem_x509_certificate(cert_pem.encode("utf-8"))
    return _parse_cert_info(cert)


def parse_cert_info_from_der(cert_der: bytes) -> CertParsedInfo:
    """从 DER 证书字节解析通用元信息。"""
    cert = x509.load_der_x509_certificate(cert_der)
    return _parse_cert_info(cert)


def _parse_cert_info(cert: x509.Certificate) -> CertParsedInfo:
    issuer = _get_name(cert.issuer)
    subject = _get_name(cert.subject)
    key_length = _get_key_info(cert.public_key())
    sans = _get_sans(cert)
    not_before_utc, not_after_utc = _get_not_before_after_utc(cert)
    profile = cert.signature_hash_algorithm.name if cert.signature_hash_algorithm else None

    return CertParsedInfo(
        issuer=issuer,
        subject=subject,
        key_length=key_length,
        sans=sans,
        not_before_utc=not_before_utc,
        not_after_utc=not_after_utc,
        profile=profile,
    )


def _get_name(name: x509.Name) -> str:
    for oid in (
        x509.NameOID.ORGANIZATION_NAME,
        x509.NameOID.COMMON_NAME,
    ):
        attrs = name.get_attributes_for_oid(oid)
        if attrs:
            return attrs[0].value
    return name.rfc4514_string()


def _get_key_info(public_key) -> str:
    if isinstance(public_key, rsa.RSAPublicKey):
        return f"RSA-{public_key.key_size}"
    if isinstance(public_key, ec.EllipticCurvePublicKey):
        return f"ECC-{public_key.curve.key_size}"
    if isinstance(public_key, dsa.DSAPublicKey):
        return "DSA"
    if isinstance(public_key, ed25519.Ed25519PublicKey):
        return "Ed25519"
    if isinstance(public_key, ed448.Ed448PublicKey):
        return "Ed448"
    return str(getattr(public_key, "key_size", "Unknown"))


def _get_sans(cert: x509.Certificate) -> list[str]:
    try:
        ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        return list(ext.value.get_values_for_type(x509.DNSName))
    except x509.ExtensionNotFound:
        return []


def _get_not_before_after_utc(cert: x509.Certificate):
    raw_not_before = (
        cert.not_valid_before_utc
        if hasattr(cert, "not_valid_before_utc")
        else cert.not_valid_before
    )
    raw_not_after = (
        cert.not_valid_after_utc
        if hasattr(cert, "not_valid_after_utc")
        else cert.not_valid_after
    )
    return time_util.ensure_utc(raw_not_before), time_util.ensure_utc(raw_not_after)

