"""ZeroSSL current ECC DV SSL CA 2 regression tests using the supplied certificate."""
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography import x509

from app.utils import certificate_chain as module


LEAF_FIXTURE = Path(__file__).parent / "fixtures" / "zerossl_nczx2025.pem"
HOSTNAME = "nczx2025.com"
VERIFICATION_TIME = datetime(2026, 10, 16, tzinfo=timezone.utc)


@pytest.fixture
def fixed_verification_time(monkeypatch):
    """仅固定样本回归使用历史验证时间，生产验证仍使用当前时间。"""
    builder = module.PolicyBuilder
    monkeypatch.setattr(module, "PolicyBuilder", lambda: builder().time(VERIFICATION_TIME))


def _names(certificates):
    return [
        cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)[0].value
        for cert in certificates
    ]


def test_supplied_zerossl_leaf_fixture_is_valid_at_reference_time():
    certificates = x509.load_pem_x509_certificates(LEAF_FIXTURE.read_bytes())
    assert len(certificates) == 1

    leaf = certificates[0]
    assert leaf.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)[0].value == HOSTNAME
    assert leaf.issuer.get_attributes_for_oid(x509.NameOID.COMMON_NAME)[0].value == (
        "ZeroSSL ECC DV SSL CA 2"
    )
    assert leaf.issuer.get_attributes_for_oid(x509.NameOID.ORGANIZATION_NAME)[0].value == (
        "ZeroSSL GmbH"
    )
    assert leaf.not_valid_before_utc <= VERIFICATION_TIME < leaf.not_valid_after_utc
    assert HOSTNAME in leaf.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value.get_values_for_type(x509.DNSName)


def test_zerossl_catalog_is_complete_and_explicit():
    assert set(module.ZERO_SSL_PATHS) == {
        "ZeroSSL RSA Domain Secure Site CA",
        "ZeroSSL ECC Domain Secure Site CA",
        "ZeroSSL RSA DV SSL CA 2",
        "ZeroSSL ECC DV SSL CA 2",
    }
    expected_catalog_names = {
        "ZeroSSL RSA DV SSL CA 2",
        "ZeroSSL ECC DV SSL CA 2",
        "ZeroSSL RSA Domain Secure Site CA",
        "ZeroSSL ECC Domain Secure Site CA",
        "USERTrust RSA Certification Authority",
        "USERTrust ECC Certification Authority",
        "Sectigo Public Server Authentication Root R46",
        "Sectigo Public Server Authentication Root E46",
    }
    assert expected_catalog_names <= module.CA_CATALOG.keys()
    for name in expected_catalog_names:
        entry = module.CA_CATALOG[name]
        assert entry["format"] == "der"
        assert entry["url"].startswith(
            "https://secure.sectigo.com/products/download/cacert/"
        )
        assert len(entry["sha256"]) == 64


@pytest.mark.parametrize(
    "issuer,expected",
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
def test_zerossl_path_matrix(issuer, expected):
    assert module.ZERO_SSL_PATHS[issuer] == expected


@pytest.mark.integration
def test_zerossl_chain_completion_from_supplied_leaf(fixed_verification_time):
    result = module.complete_certificate_chain(
        LEAF_FIXTURE.read_bytes(),
        hostname=HOSTNAME,
        timeout=30,
    )

    assert result.chain_names == module.ZERO_SSL_PATHS["ZeroSSL ECC DV SSL CA 2"]
    assert result.missing_ca == (
        "ZeroSSL ECC DV SSL CA 2",
        "Sectigo Public Server Authentication Root E46",
        "USERTrust ECC Certification Authority",
    )
    assert result.duplicates_removed == 0
    assert result.unused_certificates == 0
    assert "cryptography: OK" in result.verify_output

    certificates = x509.load_pem_x509_certificates(result.fullchain_pem.encode("ascii"))
    assert len(certificates) == 4
    assert _names(certificates) == [
        HOSTNAME,
        "ZeroSSL ECC DV SSL CA 2",
        "Sectigo Public Server Authentication Root E46",
        "USERTrust ECC Certification Authority",
    ]


@pytest.mark.integration
def test_zerossl_completed_chain_is_idempotent(fixed_verification_time):
    first = module.complete_certificate_chain(
        LEAF_FIXTURE.read_bytes(),
        hostname=HOSTNAME,
        timeout=30,
    )
    second = module.complete_certificate_chain(
        first.fullchain_pem,
        hostname=HOSTNAME,
        timeout=30,
    )

    assert second.missing_ca == ()
    assert second.duplicates_removed == 0
    assert second.unused_certificates == 0
    assert second.fullchain_pem == first.fullchain_pem
    assert "cryptography: OK" in second.verify_output
