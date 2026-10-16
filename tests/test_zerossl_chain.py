"""ZeroSSL CA 目录、DER 下载与真实端点集成测试。"""

from __future__ import annotations

import os
import socket
import ssl
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from app.utils import certificate_chain as module


EXPECTED = {
    "ZeroSSL RSA Domain Secure Site CA": (
        "21acc1dbd6944f9ac18c782cb5c328d6c2821c6b63731fa3b8987f5625de8a0d",
        "https://secure.sectigo.com/products/download/cacert/ZeroSSLRSADomainSecureSiteCA.crt",
    ),
    "ZeroSSL ECC Domain Secure Site CA": (
        "5dd661d3cb33b5005cbed045a223ddc4445aaa41d1acb5df700884cad9ba4195",
        "https://secure.sectigo.com/products/download/cacert/ZeroSSLECCDomainSecureSiteCA.crt",
    ),
    "ZeroSSL RSA DV SSL CA 2": (
        "ec9d6bcab9818b7dd59ed61da23a658a96a9c4e950edee0d2da93e7cf2174780",
        "https://secure.sectigo.com/products/download/cacert/ZeroSSLRSADVSSLCA2.crt",
    ),
    "ZeroSSL ECC DV SSL CA 2": (
        "bec84fce0daa423f3b3eed38ea48066cc5618f5244ad65cd9f8624a813fa4c3d",
        "https://secure.sectigo.com/products/download/cacert/ZeroSSLECCDVSSLCA2.crt",
    ),
    "USERTrust RSA Certification Authority": (
        "e793c9b02fd8aa13e21c31228accb08119643b749c898964b1746d46c3d4cbd2",
        "https://secure.sectigo.com/products/download/cacert/USERTrustRSACertificationAuthority.crt",
    ),
    "USERTrust ECC Certification Authority": (
        "4ff460d54b9c86dabfbcfc5712e0400d2bed3fbc4d4fbdaa86e06adcd2a9ad7a",
        "https://secure.sectigo.com/products/download/cacert/USERTrustECCCertificationAuthority.crt",
    ),
    "Sectigo Public Server Authentication Root R46": (
        "92f351bf3d54164dfa8dd8f9e1139d3150349786485d2b9eecd00e2971c1e6c5",
        "https://secure.sectigo.com/products/download/cacert/SectigoPublicServerAuthenticationRootR46_USERTrust.crt",
    ),
    "Sectigo Public Server Authentication Root E46": (
        "ea6b89ed6907a209ff9188676fb164e7aced894b8996dfbe5ce5bbcc22de4ddd",
        "https://secure.sectigo.com/products/download/cacert/SectigoPublicServerAuthenticationRootE46_USERTrust.crt",
    ),
}


@pytest.mark.parametrize(
    "issuer,path",
    [
        (
            "ZeroSSL RSA Domain Secure Site CA",
            ("ZeroSSL RSA Domain Secure Site CA", "USERTrust RSA Certification Authority"),
        ),
        (
            "ZeroSSL ECC Domain Secure Site CA",
            ("ZeroSSL ECC Domain Secure Site CA", "USERTrust ECC Certification Authority"),
        ),
        (
            "ZeroSSL RSA DV SSL CA 2",
            (
                "ZeroSSL RSA DV SSL CA 2",
                "Sectigo Public Server Authentication Root R46",
                "USERTrust RSA Certification Authority",
            ),
        ),
        (
            "ZeroSSL ECC DV SSL CA 2",
            (
                "ZeroSSL ECC DV SSL CA 2",
                "Sectigo Public Server Authentication Root E46",
                "USERTrust ECC Certification Authority",
            ),
        ),
    ],
)
def test_catalog_and_paths(issuer, path):
    assert module.CA_PATHS[issuer] == path
    for name in path:
        entry = module.CA_CATALOG[name]
        assert entry["format"] == "der"
        assert (entry["sha256"], entry["url"]) == EXPECTED[name]
        assert entry["url"].startswith("https://secure.sectigo.com/products/download/cacert/")


def test_zero_ssl_issuer_organizations():
    assert module.CA_ORGANIZATIONS["ZeroSSL RSA Domain Secure Site CA"] == "ZeroSSL"
    assert module.CA_ORGANIZATIONS["ZeroSSL ECC Domain Secure Site CA"] == "ZeroSSL"
    assert module.CA_ORGANIZATIONS["ZeroSSL RSA DV SSL CA 2"] == "ZeroSSL GmbH"
    assert module.CA_ORGANIZATIONS["ZeroSSL ECC DV SSL CA 2"] == "ZeroSSL GmbH"


def _make_ca() -> x509.Certificate:
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "AT"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "ZeroSSL GmbH"),
            x509.NameAttribute(NameOID.COMMON_NAME, "ZeroSSL RSA DV SSL CA 2"),
        ]
    )
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )


def test_download_zerossl_der(monkeypatch):
    name = "ZeroSSL RSA DV SSL CA 2"
    cert = _make_ca()
    entry = dict(module.CA_CATALOG[name])
    entry["sha256"] = cert.fingerprint(hashes.SHA256()).hex()
    monkeypatch.setitem(module.CA_CATALOG, name, entry)

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == entry["url"]
        return httpx.Response(200, content=cert.public_bytes(serialization.Encoding.DER))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = module._download_ca(client, name, timeout=1)
    assert result.fingerprint(hashes.SHA256()) == cert.fingerprint(hashes.SHA256())


def test_download_zerossl_rejects_pem_for_der(monkeypatch):
    name = "ZeroSSL RSA DV SSL CA 2"
    cert = _make_ca()
    entry = dict(module.CA_CATALOG[name])
    entry["sha256"] = cert.fingerprint(hashes.SHA256()).hex()
    monkeypatch.setitem(module.CA_CATALOG, name, entry)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=cert.public_bytes(serialization.Encoding.PEM))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(module.CertificateChainError, match="无法解析官方 CA 证书"):
            module._download_ca(client, name, timeout=1)


def test_unknown_zerossl_ca_is_rejected():
    key = ec.generate_private_key(ec.SECP256R1())
    issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "AT"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "ZeroSSL GmbH"),
            x509.NameAttribute(NameOID.COMMON_NAME, "ZeroSSL RSA Unknown CA"),
        ]
    )
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "example.com")]))
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("example.com")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    with pytest.raises(module.CertificateChainError, match="不支持的签发 CA"):
        module.complete_certificate_chain(leaf.public_bytes(serialization.Encoding.PEM))


def _fetch_leaf(domain: str, port: int, timeout: float) -> str:
    context = ssl._create_unverified_context()
    with socket.create_connection((domain, port), timeout=timeout) as raw:
        with context.wrap_socket(raw, server_hostname=domain) as tls:
            return ssl.DER_cert_to_PEM_cert(tls.getpeercert(binary_form=True))


def test_real_zerossl_endpoint():
    domain = os.getenv("ZERO_SSL_TEST_DOMAIN")
    if not domain:
        pytest.skip("设置 ZERO_SSL_TEST_DOMAIN 后执行真实 ZeroSSL 在线链测试")
    port = int(os.getenv("ZERO_SSL_TEST_PORT", "443"))
    timeout = float(os.getenv("ZERO_SSL_TEST_TIMEOUT", "10"))

    result = module.complete_certificate_chain(
        _fetch_leaf(domain, port, timeout),
        hostname=domain,
        timeout=timeout,
    )
    assert "OK" in result.verify_output
    assert result.chain_names in (
        module.ZERO_SSL_PATHS["ZeroSSL RSA DV SSL CA 2"],
        module.ZERO_SSL_PATHS["ZeroSSL ECC DV SSL CA 2"],
        module.ZERO_SSL_PATHS["ZeroSSL RSA Domain Secure Site CA"],
        module.ZERO_SSL_PATHS["ZeroSSL ECC Domain Secure Site CA"],
    )
