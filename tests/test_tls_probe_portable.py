"""No-network TLS probe tests, portable across Windows and GitHub Actions."""
import asyncio
import socket
import ssl
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from app.utils import tls_probe


def _certificate(*, cn="example.com", sans=None):
    from datetime import datetime, timedelta, timezone
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = datetime.now(timezone.utc)
    builder = (x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30)))
    if sans is not None:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName(s) for s in sans]), False)
    from cryptography.hazmat.primitives.serialization import Encoding
    return builder.sign(key, hashes.SHA256()).public_bytes(Encoding.DER)


def test_cert_match_san_and_cn():
    cert = x509.load_der_x509_certificate(_certificate(cn="fallback.example.com", sans=["api.example.com"]))
    assert tls_probe._cert_match_domain(cert, "api.example.com")
    assert tls_probe._cert_match_domain(cert, "fallback.example.com")
    assert not tls_probe._cert_match_domain(cert, "other.example.com")


def test_extract_rejects_unmatched_or_malformed_certificate():
    der = _certificate(cn="example.com")
    assert tls_probe._extract_cert_info(der, "wrong.example.com") is None
    assert tls_probe._extract_cert_info(b"not a certificate", "example.com") is None


def test_probe_sync_without_certificate(monkeypatch):
    class Socket:
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def settimeout(self, value): pass
    class SSLSocket(Socket):
        def getpeercert(self, binary_form=False): return None
    monkeypatch.setattr(tls_probe, "load_raw_config", lambda: SimpleNamespace(tls_timeout_seconds=2))
    monkeypatch.setattr(tls_probe.socket, "create_connection", lambda *a, **kw: Socket())
    monkeypatch.setattr(tls_probe, "_SSL_CONTEXT", SimpleNamespace(wrap_socket=lambda *a, **kw: SSLSocket()))
    assert tls_probe.probe_tls_sync("example.com") is None


@pytest.mark.parametrize("error", [
    socket.timeout("timeout"),
    socket.gaierror("dns"),
    ConnectionRefusedError("refused"),
    ssl.SSLError("ssl"),
    RuntimeError("unexpected"),
])
def test_probe_sync_network_errors_are_handled(monkeypatch, error):
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(tls_probe, "load_raw_config", lambda: SimpleNamespace(tls_timeout_seconds=2))
    monkeypatch.setattr(tls_probe.socket, "create_connection", fail)
    assert tls_probe.probe_tls_sync("example.com") is None


def test_batch_rejects_invalid_concurrency(monkeypatch):
    monkeypatch.setattr(tls_probe, "load_raw_config", lambda: SimpleNamespace(tls_scan_parallel=0))
    with pytest.raises(ValueError, match="并发数"):
        asyncio.run(tls_probe.probe_domains_batch(["example.com"]))


def test_batch_collects_success_and_skips_failure(monkeypatch):
    async def fake_probe(domain, port):
        if domain == "bad.example.com":
            raise RuntimeError("probe failed")
        if domain == "empty.example.com":
            return None
        return {"ca": "test", "exp": "tomorrow"}
    monkeypatch.setattr(tls_probe, "probe_tls", fake_probe)
    monkeypatch.setattr(tls_probe, "load_raw_config", lambda: SimpleNamespace(tls_scan_parallel=2))
    results = asyncio.run(tls_probe.probe_domains_batch(
        ["good.example.com", "bad.example.com", "empty.example.com"]))
    assert results == {"good.example.com": {"ca": "test", "exp": "tomorrow"}}
