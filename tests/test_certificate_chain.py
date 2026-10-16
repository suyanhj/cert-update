"""使用临时签发证书和 cryptography 验证补链行为。"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from app.utils import certificate_chain as module

HTTP_CLIENT = httpx.Client

def issue(name, key, issuer_name, issuer_key, *, ca=True, expired=False,
          sans=None, eku=ExtendedKeyUsageOID.SERVER_AUTH):
    now = datetime.now(timezone.utc)
    builder = (x509.CertificateBuilder().subject_name(name).issuer_name(issuer_name)
               .public_key(key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(now - timedelta(days=2))
               .not_valid_after(now + timedelta(days=-1 if expired else 30))
               .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
               .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False)
               .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()), False)
               .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                   key_encipherment=False, data_encipherment=False, key_agreement=False,
                   key_cert_sign=ca, crl_sign=ca, encipher_only=False, decipher_only=False), True))
    if not ca:
        builder = builder.add_extension(x509.SubjectAlternativeName(
            [x509.DNSName("example.com")] if sans is None else sans), False)
        builder = builder.add_extension(x509.ExtendedKeyUsage([eku]), False)
    return builder.sign(issuer_key, hashes.SHA256())


@pytest.fixture
def hierarchy(monkeypatch):
    keys, certs, catalog = {}, {}, {}
    parents = {}
    for path in module.CA_PATHS.values():
        parents[path[-1]] = path[-1]
        for child, parent in reversed(list(zip(path, path[1:]))):
            parents[child] = parent
    for name, parent in parents.items():
        key = keys[name] = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME,
                                               module.CA_ORGANIZATIONS.get(name, "Test Root")),
                             x509.NameAttribute(NameOID.COMMON_NAME, name)])
        parent_subject = subject if parent == name else certs[parent].subject
        cert = certs[name] = issue(subject, key, parent_subject, keys[parent])
        is_zero = name.startswith(("ZeroSSL", "USERTrust", "Sectigo"))
        catalog[name] = {"url": ("https://secure.sectigo.com/products/download/cacert/" if is_zero
                                 else "https://letsencrypt.org/certs/") + name.replace(' ', '-') + '.crt',
                         "sha256": module._fingerprint(cert), "format": "der" if is_zero else "pem"}
    calls = []
    by_url = {entry["url"]: certs[name].public_bytes(
        serialization.Encoding.DER if entry["format"] == "der" else serialization.Encoding.PEM)
        for name, entry in catalog.items()}

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=by_url[str(request.url)])

    client_class = httpx.Client
    monkeypatch.setattr(module, "CA_CATALOG", catalog)
    monkeypatch.setattr(module, "httpx", SimpleNamespace(HTTPError=httpx.HTTPError,
        Client=lambda **kwargs: client_class(transport=httpx.MockTransport(handler), **kwargs)))

    def leaf(issuer, *, expired=False, wrong_key=False, private_key=None, **kwargs):
        key = private_key if private_key is not None else ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "example.com")])
        return issue(subject, key, certs[issuer].subject,
                     key if wrong_key else keys[issuer], ca=False, expired=expired, **kwargs)

    return certs, calls, leaf


@pytest.mark.parametrize("issuer", list(module.CA_PATHS))
def test_complete_every_supported_chain(hierarchy, issuer):
    certs, calls, leaf = hierarchy
    result = module.complete_certificate_chain(module._pem(leaf(issuer)),
                                               hostname="example.com")
    path = module.CA_PATHS[issuer]
    assert result.missing_ca == path
    assert len(calls) == len(path)
    parsed = x509.load_pem_x509_certificates(result.fullchain_pem.encode())
    assert len(parsed) == len(path) + 1
    assert [module._fingerprint(cert) for cert in parsed[1:]] == [
        module._fingerprint(certs[name]) for name in path]
    assert "OK" in result.verify_output


def test_reordered_duplicates_and_root_do_not_download(hierarchy):
    certs, calls, leaf = hierarchy
    end = module._pem(leaf("YE1"))
    bundle = "".join(module._pem(certs[n]) for n in reversed(module.CA_PATHS["YE1"]))
    result = module.complete_certificate_chain(bundle + end + end)
    assert result.duplicates_removed == 1
    assert result.missing_ca == ()
    assert not calls
    assert result.fullchain_pem.startswith(end)
    assert result.fullchain_pem.endswith(module._pem(certs[module.ROOT]))
    assert result.fullchain_pem.count(module._pem(certs[module.ROOT])) == 1


def test_missing_cross_certificates(hierarchy):
    certs, calls, leaf = hierarchy
    result = module.complete_certificate_chain(module._pem(leaf("YE2")) + module._pem(certs["YE2"]))
    assert result.missing_ca == ("Root YE", "ISRG Root X2", module.ROOT)
    assert len(calls) == 3


def test_same_name_root_is_not_trusted(hierarchy):
    certs, _, leaf = hierarchy
    key = ec.generate_private_key(ec.SECP256R1())
    subject = certs[module.ROOT].subject
    fake_root = issue(subject, key, subject, key)
    result = module.complete_certificate_chain(module._pem(leaf("YR2")) + module._pem(fake_root))
    assert module.ROOT in result.missing_ca
    assert result.unused_certificates == 1
    assert result.root_pem == module._pem(certs[module.ROOT])


def test_unknown_issuer_rejected(hierarchy):
    certs, calls, _ = hierarchy
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "unknown.example.com")])
    end = issue(subject, key, certs[module.ROOT].subject, key, ca=False)
    with pytest.raises(module.CertificateChainError, match="不支持的签发 CA"):
        module.complete_certificate_chain(module._pem(end))
    assert not calls


def test_non_certificate_content_rejected(hierarchy):
    _, calls, leaf = hierarchy
    with pytest.raises(module.CertificateChainError, match="只包含 PEM 证书"):
        module.complete_certificate_chain(module._pem(leaf("R10")) + "PRIVATE KEY")
    assert not calls


@pytest.mark.parametrize("options,hostname", [({"expired": True}, None),
                                            ({"wrong_key": True}, None), ({}, "wrong.example.com")])
def test_verify_rejects_invalid_leaf(hierarchy, options, hostname):
    _, _, leaf = hierarchy
    with pytest.raises(module.CertificateChainError, match="证书链验证失败"):
        module.complete_certificate_chain(module._pem(leaf("YR1", **options)),
                                          hostname=hostname)


def test_multiple_leaves_rejected_before_network(hierarchy):
    _, calls, leaf = hierarchy
    with pytest.raises(module.CertificateChainError, match="唯一叶证书"):
        module.complete_certificate_chain(module._pem(leaf("YR1")) + module._pem(leaf("YR2")))
    assert not calls


@pytest.mark.parametrize("raw", ["", "invalid", "-----BEGIN CERTIFICATE-----bad-----END CERTIFICATE-----"])
def test_invalid_input(raw):
    with pytest.raises(module.CertificateChainError):
        module.complete_certificate_chain(raw)


@pytest.mark.parametrize("mode", ["timeout", "http", "redirect", "fingerprint", "oversize", "invalid"])
def test_download_failures(hierarchy, mode):
    certs, _, _ = hierarchy

    def handler(request):
        if mode == "timeout":
            raise httpx.ReadTimeout("模拟下载超时", request=request)
        status = {"http": 503, "redirect": 302}.get(mode, 200)
        body = {"fingerprint": module._pem(certs["YE2"]),
                "oversize": "a" * (module.MAX_CA_BYTES + 1), "invalid": "bad"}.get(mode, "")
        return httpx.Response(status, text=body, headers={"Location": "https://example.com"})

    with HTTP_CLIENT(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(module.CertificateChainError):
            module._download_ca(client, "YE1", 1)


def test_cli_success_and_no_overwrite(hierarchy, tmp_path):
    _, _, leaf = hierarchy
    source, output = tmp_path / "source.pem", tmp_path / "fullchain.pem"
    source.write_text(module._pem(leaf("R10")), encoding="ascii")
    original = source.read_bytes()
    assert module.main([str(source), str(output)]) == 0
    completed = output.read_bytes()
    assert module.main([str(source), str(output)]) == 1
    assert source.read_bytes() == original
    assert output.read_bytes() == completed


def test_cli_verification_failure_writes_nothing(hierarchy, tmp_path):
    _, _, leaf = hierarchy
    source, output = tmp_path / "source.pem", tmp_path / "fullchain.pem"
    source.write_text(module._pem(leaf("R10", expired=True)), encoding="ascii")
    assert module.main([str(source), str(output)]) == 1
    assert not output.exists()


def test_no_external_executable_required(hierarchy, monkeypatch):
    _, _, leaf = hierarchy
    monkeypatch.setenv("PATH", "")
    assert "OK" in module.complete_certificate_chain(module._pem(leaf("YE1"))).verify_output


@pytest.mark.parametrize("hostname", [None, "www.example.com"])
def test_wildcard_san(hierarchy, hostname):
    _, _, leaf = hierarchy
    cert = leaf("YE1", sans=[x509.DNSName("*.example.com")])
    assert "OK" in module.complete_certificate_chain(module._pem(cert), hostname=hostname).verify_output


@pytest.mark.parametrize("hostname", ["", "*.example.com"])
def test_invalid_hostname(hierarchy, hostname):
    _, _, leaf = hierarchy
    with pytest.raises(module.CertificateChainError, match="具体域名"):
        module.complete_certificate_chain(module._pem(leaf("YE1")), hostname=hostname)


def test_ip_san(hierarchy):
    _, _, leaf = hierarchy
    cert = leaf("YR1", sans=[x509.IPAddress(module.ip_address("192.0.2.1"))])
    for hostname in (None, "192.0.2.1"):
        assert "OK" in module.complete_certificate_chain(module._pem(cert), hostname=hostname).verify_output


def test_client_only_certificate_rejected(hierarchy):
    _, _, leaf = hierarchy
    cert = leaf("R10", eku=ExtendedKeyUsageOID.CLIENT_AUTH)
    with pytest.raises(module.CertificateChainError, match="证书链验证失败"):
        module.complete_certificate_chain(module._pem(cert))


def test_no_dns_or_ip_san_rejected(hierarchy):
    _, _, leaf = hierarchy
    cert = leaf("R10", sans=[x509.RFC822Name("test@example.com")])
    with pytest.raises(module.CertificateChainError, match="没有 DNS"):
        module.complete_certificate_chain(module._pem(cert))



def test_is_ca_without_basic_constraints_returns_false():
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "leaf")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    assert module._is_ca(cert) is False


def test_download_rejects_non_official_url(hierarchy):
    certs, _, _ = hierarchy
    catalog = dict(module.CA_CATALOG)
    catalog["YE1"] = {
        **catalog["YE1"],
        "url": "https://example.com/not-an-official-ca.pem",
        "sha256": module._fingerprint(certs["YE1"]),
    }
    client = HTTP_CLIENT(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    original = module.CA_CATALOG
    module.CA_CATALOG = catalog
    try:
        with pytest.raises(module.CertificateChainError, match="非官方地址"):
            module._download_ca(client, "YE1", 1)
    finally:
        module.CA_CATALOG = original
    client.close()


def test_download_rejects_non_ca_certificate(hierarchy):
    _, _, leaf = hierarchy
    non_ca = leaf("YE1")
    catalog = dict(module.CA_CATALOG)
    catalog["YE1"] = {
        **catalog["YE1"],
        "sha256": module._fingerprint(non_ca),
    }
    client = HTTP_CLIENT(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, content=module._pem(non_ca).encode("ascii")
            )
        )
    )
    original = module.CA_CATALOG
    module.CA_CATALOG = catalog
    try:
        with pytest.raises(module.CertificateChainError, match="不是单个 CA"):
            module._download_ca(client, "YE1", 1)
    finally:
        module.CA_CATALOG = original
    client.close()


def test_timeout_must_be_positive(hierarchy):
    _, _, leaf = hierarchy
    with pytest.raises(module.CertificateChainError, match="timeout 必须大于 0"):
        module.complete_certificate_chain(module._pem(leaf("R10")), timeout=0)
