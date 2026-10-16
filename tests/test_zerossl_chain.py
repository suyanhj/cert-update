"""ZeroSSL 当前 ECC DV SSL CA 2 真实证书链回归测试。"""

from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography import x509

from app.utils import certificate_chain as module


FIXTURE = Path(__file__).parent / "fixtures" / "zerossl_nczx2025.pem"
HOSTNAME = "nczx2025.com"


def test_current_zerossl_leaf_fixture():
    certificates = x509.load_pem_x509_certificates(FIXTURE.read_bytes())
    assert len(certificates) == 1

    leaf = certificates[0]
    assert leaf.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)[0].value == HOSTNAME
    assert leaf.issuer.get_attributes_for_oid(x509.NameOID.COMMON_NAME)[0].value == (
        "ZeroSSL ECC DV SSL CA 2"
    )
    assert leaf.issuer.get_attributes_for_oid(x509.NameOID.ORGANIZATION_NAME)[0].value == (
        "ZeroSSL GmbH"
    )
    assert leaf.not_valid_after_utc > datetime.now(timezone.utc)
    assert HOSTNAME in leaf.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value.get_values_for_type(x509.DNSName)


@pytest.mark.integration
def test_current_zerossl_chain_completion():
    result = module.complete_certificate_chain(
        FIXTURE.read_bytes(),
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
    assert [cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)[0].value for cert in certificates] == [
        "nczx2025.com",
        "ZeroSSL ECC DV SSL CA 2",
        "Sectigo Public Server Authentication Root E46",
        "USERTrust ECC Certification Authority",
    ]


@pytest.mark.integration
def test_current_zerossl_completed_chain_is_idempotent():
    first = module.complete_certificate_chain(
        FIXTURE.read_bytes(),
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
