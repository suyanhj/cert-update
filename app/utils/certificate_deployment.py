"""部署前补齐实际本地证书文件，并同步本轮所有平台使用的证书内容。"""

from dataclasses import replace
import logging
import os
from pathlib import Path
import shutil
import tempfile
from threading import Lock
from uuid import uuid4

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization

from app.schemas.acme import RenewedCert
from app.utils.certificate_chain import (
    CertificateChainError, SUPPORTED_ISSUER_ORGANIZATIONS, complete_certificate_chain,
)
from app.utils.domain_match import match_domain, normalize_domain
from app.utils.project_root import resolve_project_path


LOGGER = logging.getLogger("app.certificate_deployment")
_PREPARE_LOCK = Lock()


def _fingerprints(pem: bytes) -> list[bytes]:
    return [cert.fingerprint(hashes.SHA256()) for cert in x509.load_pem_x509_certificates(pem)]


def _replace_with_backup(path: Path, original: bytes, updated: bytes) -> None:
    """同目录备份后原子替换，覆盖前确认外部没有更新源文件。"""
    if path.read_bytes() != original:
        raise CertificateChainError(f"证书文件已发生变化，请重新加载后部署: {path}")
    backup = path.with_name(f"{path.name}.chain-{uuid4().hex}.bak")
    shutil.copy2(path, backup)
    LOGGER.info("补链前已备份证书: path=%s backup=%s", path, backup)
    stat = path.stat()
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as temp:
            temp_path = Path(temp.name)
            temp.write(updated)
            temp.flush()
            os.fsync(temp.fileno())
        shutil.copystat(path, temp_path)
        if hasattr(os, "chown"):
            os.chown(temp_path, stat.st_uid, stat.st_gid)
        if path.read_bytes() != original or backup.read_bytes() != original:
            raise CertificateChainError(f"证书文件已发生变化，请重新加载后部署: {path}")
        os.replace(temp_path, path)
        LOGGER.info("已更新本地完整证书链: path=%s", path)
    finally:
        # 此处只清理本次创建的临时文件，原证书备份始终保留。
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def prepare_certificate_for_deployment(cert: RenewedCert) -> RenewedCert:
    """正式 Let's Encrypt / ZeroSSL 证书在平台分发前处理一次，其他 CA 保持原行为。"""
    if cert.issuer not in SUPPORTED_ISSUER_ORGANIZATIONS:
        LOGGER.info("跳过未支持 CA 的证书补链: domain=%s issuer=%s", cert.domain, cert.issuer)
        return cert
    with _PREPARE_LOCK:
        try:
            return _prepare_local_certificate(cert)
        except (OSError, ValueError, TypeError, x509.ExtensionNotFound) as exc:
            LOGGER.error("部署前证书准备失败: domain=%s error=%s", cert.domain, exc)
            raise CertificateChainError(f"部署前证书准备失败: {cert.domain}: {exc}") from exc


def _prepare_local_certificate(cert: RenewedCert) -> RenewedCert:
    if not cert.fullchain_path:
        raise CertificateChainError("缺少已识别的本地 fullchain 路径")
    # 跟随部署实际选择的文件及符号链接，不猜测 acme.sh 默认目录。
    path = resolve_project_path(cert.fullchain_path).resolve(strict=True)
    original = path.read_bytes()
    current = _fingerprints(original)
    if current != _fingerprints(cert.fullchain_pem.encode("utf-8")):
        raise CertificateChainError(f"证书文件与加载快照不同，请重新加载后部署: {path}")
    leaf = x509.load_pem_x509_certificate(original)
    key_path = resolve_project_path(cert.key_path).resolve(strict=True)
    key_bytes = key_path.read_bytes()
    key = serialization.load_pem_private_key(key_bytes, password=None)
    loaded_key = serialization.load_pem_private_key(cert.key_pem.encode("utf-8"), password=None)
    public_format = (serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    public = leaf.public_key().public_bytes(*public_format)
    if key.public_key().public_bytes(*public_format) != public or loaded_key.public_key().public_bytes(*public_format) != public:
        raise CertificateChainError("证书与本地或已加载的私钥不匹配")
    domain = normalize_domain(cert.domain)
    sans = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    if not match_domain(set(sans.get_values_for_type(x509.DNSName)), domain):
        raise CertificateChainError(f"证书 SAN 未覆盖部署域名: {domain}")
    result = complete_certificate_chain(original, hostname=None if domain.startswith("*.") else domain)
    if key_path.read_bytes() != key_bytes or path.read_bytes() != original:
        raise CertificateChainError("验证期间证书或私钥文件发生变化，请重新加载后部署")
    if current != _fingerprints(result.fullchain_pem.encode("ascii")):
        # PEM 内容为 ASCII，保留源文件的换行风格，不改变文件编码。
        newline = "\r\n" if b"\r\n" in original else "\n"
        updated = result.fullchain_pem.replace("\n", newline).encode("ascii")
        _replace_with_backup(path, original, updated)
    else:
        LOGGER.info("本地证书链已完整且验证通过，无需写回: path=%s", path)
    return replace(cert, cert_pem=result.fullchain_pem, fullchain_pem=result.fullchain_pem,
                   ca_pem=result.ca_pem)
