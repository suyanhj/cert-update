"""部署前本地证书补全与多平台共享行为。"""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.schemas.acme import RenewedCert
from app.schemas.provider import DeployMatchResult
from app.utils import certificate_deployment as prep
from app.utils import certificate_chain as chain
from test_certificate_chain import hierarchy


@pytest.fixture(params=["YE1", "ZeroSSL RSA Domain Secure Site CA", "ZeroSSL ECC Domain Secure Site CA",
                        "ZeroSSL RSA DV SSL CA 2", "ZeroSSL ECC DV SSL CA 2"])
def local_certificate(hierarchy, tmp_path, request):
    _, calls, leaf = hierarchy
    key = ec.generate_private_key(ec.SECP256R1())
    end = leaf(request.param, private_key=key)
    pem = chain._pem(end)
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode("ascii")
    fullchain, key_path = tmp_path / "selected-fullchain.pem", tmp_path / "key.pem"
    fullchain.write_bytes(pem.replace("\n", "\r\n").encode("ascii"))
    key_path.write_text(key_pem, encoding="ascii")
    cert = RenewedCert(domain="example.com", cert_path=str(tmp_path / "leaf.pem"),
                       key_path=str(key_path), fullchain_path=str(fullchain), ca_path=None,
                       cert_pem=pem, fullchain_pem=pem, key_pem=key_pem, ca_pem="",
                       issued_at=end.not_valid_before_utc, expires_at=end.not_valid_after_utc,
                       key_length="ECC-256", sans=["example.com"],
                       issuer=chain.CA_ORGANIZATIONS[request.param], profile=None)
    return cert, fullchain, calls


def test_updates_selected_file_once_and_reuses_complete_chain(local_certificate):
    cert, path, calls = local_certificate
    before = path.read_bytes()
    updated = prep.prepare_certificate_for_deployment(cert)
    expected = 5 if cert.issuer == "Let's Encrypt" else 4 if cert.issuer == "ZeroSSL GmbH" else 3
    assert len(chain._parse_pem(updated.cert_pem)) == expected
    assert updated.cert_pem == updated.fullchain_pem
    assert len(chain._parse_pem(updated.ca_pem)) == expected - 1
    assert b"\r\n" in path.read_bytes()
    assert b"\n" not in path.read_bytes().replace(b"\r\n", b"")
    assert not list(path.parent.glob("*.bak"))
    backups = list((path.parent / "bak").glob("*.bak"))
    assert len(backups) == 1 and backups[0].read_bytes() == before
    modified = path.stat().st_mtime_ns
    calls.clear()
    # 模拟后续部署重新从磁盘加载完整证书，而非依赖上一轮内存对象。
    reloaded = replace(cert, cert_pem=path.read_text(encoding="ascii"),
                       fullchain_pem=path.read_text(encoding="ascii"))
    again = prep.prepare_certificate_for_deployment(reloaded)
    assert again.fullchain_pem == updated.fullchain_pem
    assert path.stat().st_mtime_ns == modified
    assert list((path.parent / "bak").glob("*.bak")) == backups
    assert not calls


@pytest.mark.parametrize("failure", ["key", "snapshot", "download", "domain"])
def test_failure_preserves_source(local_certificate, monkeypatch, failure):
    cert, path, _ = local_certificate
    original = path.read_bytes()
    if failure == "key":
        cert = replace(cert, key_pem="bad key")
    elif failure == "snapshot":
        cert = replace(cert, fullchain_pem="bad snapshot")
    elif failure == "domain":
        cert = replace(cert, domain="wrong.example.com")
    else:
        monkeypatch.setattr(prep, "complete_certificate_chain", Mock(side_effect=chain.CertificateChainError("下载失败")))
    with pytest.raises(chain.CertificateChainError):
        prep.prepare_certificate_for_deployment(cert)
    assert path.read_bytes() == original
    assert not list((path.parent / "bak").glob("*.bak"))


def test_external_change_during_validation_is_not_overwritten(local_certificate, monkeypatch):
    cert, path, _ = local_certificate
    complete = prep.complete_certificate_chain

    def change(*args, **kwargs):
        result = complete(*args, **kwargs)
        path.write_bytes(b"external renewal")
        return result

    monkeypatch.setattr(prep, "complete_certificate_chain", change)
    with pytest.raises(chain.CertificateChainError, match="发生变化"):
        prep.prepare_certificate_for_deployment(cert)
    assert path.read_bytes() == b"external renewal"


def test_replace_failure_keeps_original_and_backup(local_certificate, monkeypatch):
    cert, path, _ = local_certificate
    original = path.read_bytes()
    monkeypatch.setattr(prep.os, "replace", Mock(side_effect=OSError("模拟替换失败")))
    with pytest.raises(chain.CertificateChainError):
        prep.prepare_certificate_for_deployment(cert)
    assert path.read_bytes() == original
    assert len(list((path.parent / "bak").glob("*.bak"))) == 1
    assert not list(path.parent.glob(".selected-fullchain.pem.*"))


def test_other_ca_is_unchanged():
    cert = SimpleNamespace(issuer="Other CA", domain="example.com")
    assert prep.prepare_certificate_for_deployment(cert) is cert


def test_backup_directory_failure_preserves_source(local_certificate):
    cert, path, _ = local_certificate
    original = path.read_bytes()
    (path.parent / 'bak').write_text('已有同名文件', encoding='utf-8')
    with pytest.raises(chain.CertificateChainError):
        prep.prepare_certificate_for_deployment(cert)
    assert path.read_bytes() == original
    assert not list(path.parent.glob('*.bak'))


def test_apply_prepares_once_before_any_platform(local_certificate, monkeypatch):
    from app.services import deploy

    cert, _, _ = local_certificate
    service = deploy.DeployService.__new__(deploy.DeployService)
    service.conf = SimpleNamespace(deploy=SimpleNamespace(mode="apply"))
    prepare = Mock(wraps=prep.prepare_certificate_for_deployment)
    monkeypatch.setattr(deploy, "prepare_certificate_for_deployment", prepare)
    observed = []
    providers = [SimpleNamespace(name=name) for name in ("cloud-a", "cloud-b")]
    matches = [DeployMatchResult("test", p.name, "cdn", p.name, "example.com", "exact", None)
               for p in providers]
    deployers = {f"test:{p.name}": SimpleNamespace(
        prepare_certificate=AsyncMock(return_value="uploaded-id"),
        deploy=AsyncMock(return_value=SimpleNamespace(success=True, message="ok", cert_id="uploaded-id")))
        for p in providers}
    service._provider_cache_key = lambda p: f"test:{p.name}"

    async def collect(renewed_cert, **kwargs):
        observed.append(renewed_cert)
        expected = 5 if cert.issuer == "Let's Encrypt" else 4 if cert.issuer == "ZeroSSL GmbH" else 3
        assert len(chain._parse_pem(renewed_cert.cert_pem)) == expected
        return providers, {"example.com"}, matches, []

    async def augment(**kwargs):
        observed.append(kwargs["renewed_cert"])
        return matches, deployers, {}

    async def nginx(renewed_cert):
        observed.append(renewed_cert)
        return [{"status": "success", "provider": "nginx", "domain": "example.com"}]

    service._collect_matches = collect
    service._binding_cache_settings = lambda: (False, 3600, False)
    service._augment_tencent_ssl_matches = augment
    service._apply_nginx_deploy = nginx
    monkeypatch.setattr(deploy, "DeployStateStore", Mock())
    asyncio.run(service.apply_deploy(cert))
    prepare.assert_called_once_with(cert)
    assert len(observed) == 3
    assert all(item is observed[0] for item in observed)
    for deployer in deployers.values():
        deployer.prepare_certificate.assert_awaited_once()
        assert deployer.prepare_certificate.await_args.args[0] == observed[0].fullchain_pem
        assert deployer.deploy.await_args.kwargs["cert_pem"] == observed[0].fullchain_pem


def test_apply_failure_prevents_cloud_queries(local_certificate, monkeypatch):
    from app.services import deploy

    cert, _, _ = local_certificate
    service = deploy.DeployService.__new__(deploy.DeployService)
    service.conf = SimpleNamespace(deploy=SimpleNamespace(mode="apply"))
    service._collect_matches = AsyncMock()
    monkeypatch.setattr(deploy, "prepare_certificate_for_deployment", Mock(side_effect=chain.CertificateChainError("失败")))
    with pytest.raises(chain.CertificateChainError):
        asyncio.run(service.apply_deploy(cert))
    service._collect_matches.assert_not_awaited()
