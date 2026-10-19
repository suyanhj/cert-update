"""补全 Let's Encrypt / ZeroSSL 官方证书链，并使用 cryptography 验证后返回。"""

import argparse
from dataclasses import dataclass
import json
from ipaddress import ip_address
import logging
from pathlib import Path
import re

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509.oid import NameOID
from cryptography.x509.verification import PolicyBuilder, Store, VerificationError


LOGGER = logging.getLogger("app.certificate_chain")
CA_CATALOG = json.loads(Path(__file__).with_name("letsencrypt_ca.json").read_text(encoding="utf-8"))
CA_CATALOG.update(json.loads(Path(__file__).with_name("zerossl_ca.json").read_text(encoding="utf-8")))
ROOT = "ISRG Root X1"

# ZeroSSL 目前同时存在两代公开 DV 层级：
#   旧：Domain Secure Site CA -> USERTrust 同类自签根
#   新：DV SSL CA 2 -> Sectigo Public Server Authentication Root R46/E46
#       -> USERTrust 同类自签根。
# 显式建模路径，避免后续只按名称猜测 RSA/ECC 的信任根。
ZERO_SSL_PATHS = {
    "ZeroSSL RSA Domain Secure Site CA": (
        "ZeroSSL RSA Domain Secure Site CA",
        "USERTrust RSA Certification Authority",
    ),
    "ZeroSSL ECC Domain Secure Site CA": (
        "ZeroSSL ECC Domain Secure Site CA",
        "USERTrust ECC Certification Authority",
    ),
    "ZeroSSL RSA DV SSL CA 2": (
        "ZeroSSL RSA DV SSL CA 2",
        "Sectigo Public Server Authentication Root R46",
        "USERTrust RSA Certification Authority",
    ),
    "ZeroSSL ECC DV SSL CA 2": (
        "ZeroSSL ECC DV SSL CA 2",
        "Sectigo Public Server Authentication Root E46",
        "USERTrust ECC Certification Authority",
    ),
}
ZERO_SSL_ISSUER_ORGANIZATIONS = {
    "ZeroSSL RSA Domain Secure Site CA": "ZeroSSL",
    "ZeroSSL ECC Domain Secure Site CA": "ZeroSSL",
    "ZeroSSL RSA DV SSL CA 2": "ZeroSSL GmbH",
    "ZeroSSL ECC DV SSL CA 2": "ZeroSSL GmbH",
}

CA_PATHS = {
    **{f"YE{i}": (f"YE{i}", "Root YE", "ISRG Root X2", ROOT) for i in range(1, 4)},
    **{f"YR{i}": (f"YR{i}", "Root YR", ROOT) for i in range(1, 4)},
    **{f"R{i}": (f"R{i}", ROOT) for i in range(10, 15)},
    **{f"E{i}": (f"E{i}", "ISRG Root X2", ROOT) for i in range(5, 10)},
    **ZERO_SSL_PATHS,
}
CA_ORGANIZATIONS = {
    name: "Let's Encrypt"
    for name in CA_PATHS
    if name not in ZERO_SSL_ISSUER_ORGANIZATIONS
}
CA_ORGANIZATIONS.update(ZERO_SSL_ISSUER_ORGANIZATIONS)
SUPPORTED_ISSUER_ORGANIZATIONS = frozenset(CA_ORGANIZATIONS.values())
PEM_BLOCK = re.compile(rb"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", re.DOTALL)
MAX_CA_BYTES = 64 * 1024


class CertificateChainError(ValueError):
    """证书链补全或验证失败。"""


@dataclass(frozen=True)
class ChainCompletionResult:
    """验证成功的链及诊断信息；missing_ca 包含缺失的验证根证书。"""

    fullchain_pem: str
    ca_pem: str
    root_pem: str
    chain_names: tuple[str, ...]
    missing_ca: tuple[str, ...]
    duplicates_removed: int
    unused_certificates: int
    verify_output: str


def _fingerprint(cert: x509.Certificate) -> str:
    return cert.fingerprint(hashes.SHA256()).hex()


def _pem(cert: x509.Certificate) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode("ascii")


def _is_ca(cert: x509.Certificate) -> bool:
    try:
        return cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    except x509.ExtensionNotFound:
        return False


def _parse_pem(data: str | bytes) -> list[x509.Certificate]:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    if not raw.strip() or PEM_BLOCK.sub(b"", raw).strip():
        raise CertificateChainError("输入必须只包含 PEM 证书，不能包含私钥或其他内容")
    try:
        return x509.load_pem_x509_certificates(raw)
    except ValueError as exc:
        raise CertificateChainError("无法解析 PEM 证书链") from exc


def _download_ca(client: httpx.Client, name: str, timeout: float) -> x509.Certificate:
    entry = CA_CATALOG[name]
    url = entry["url"]
    if not url.startswith(("https://letsencrypt.org/certs/",
                           "https://secure.sectigo.com/products/download/cacert/")):
        raise CertificateChainError(f"CA 目录包含非官方地址: {name}")
    LOGGER.info("下载官方 CA: ca=%s url=%s", name, url)
    try:
        with client.stream("GET", url, timeout=timeout, follow_redirects=False) as response:
            response.raise_for_status()
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > MAX_CA_BYTES:
                    raise CertificateChainError(f"官方 CA 响应过大: {name}")
    except httpx.HTTPError as exc:
        raise CertificateChainError(f"下载官方 CA 失败: {name}: {exc}") from exc
    try:
        certificates = ([x509.load_der_x509_certificate(bytes(body))]
                        if entry.get("format", "pem") == "der" else _parse_pem(bytes(body)))
    except ValueError as exc:
        raise CertificateChainError(f"无法解析官方 CA 证书: {name}") from exc
    if len(certificates) != 1 or not _is_ca(certificates[0]):
        raise CertificateChainError(f"下载内容不是单个 CA 证书: {name}")
    cert = certificates[0]
    if _fingerprint(cert) != entry["sha256"]:
        raise CertificateChainError(f"官方 CA 指纹不匹配: {name}，请核对官方 CA 是否已轮换")
    return cert


def _verify(
    leaf: x509.Certificate, chain: list[x509.Certificate],
    root: x509.Certificate, hostname: str | None,
) -> str:
    try:
        if hostname is not None:
            subject = _verification_subject(hostname)
        else:
            # 未指定目标时只验证证书本身，不能据此宣称匹配某个外部域名。
            sans = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            candidates = [name for name in sans if isinstance(name, (x509.DNSName, x509.IPAddress))]
            if not candidates:
                raise ValueError("证书 SAN 中没有 DNS 名称或 IP 地址")
            subject = candidates[0]
            if isinstance(subject, x509.DNSName) and subject.value.startswith("*."):
                subject = x509.DNSName("chain-check." + subject.value[2:])
        verifier = PolicyBuilder().store(Store([root])).build_server_verifier(subject)
        verified = verifier.verify(leaf, chain)
    except (VerificationError, ValueError, x509.ExtensionNotFound) as exc:
        LOGGER.error("证书链验证失败: %s", exc)
        raise CertificateChainError(f"cryptography 证书链验证失败: {exc}") from exc
    target = hostname if hostname is not None else "未指定外部域名（使用证书 SAN）"
    return f"cryptography: OK；链长度={len(verified)}；目标={target}"


def _verification_subject(hostname: str) -> x509.DNSName | x509.IPAddress:
    """将明确的目标域名或 IP 转成库支持的验证对象。"""
    if not hostname or "*" in hostname:
        raise ValueError("hostname 必须是具体域名或 IP，不能是空值或通配符")
    try:
        return x509.IPAddress(ip_address(hostname))
    except ValueError:
        return x509.DNSName(hostname.encode("idna").decode("ascii"))


def complete_certificate_chain(
    pem: str | bytes, *, hostname: str | None = None,
    timeout: float = 20,
) -> ChainCompletionResult:
    """补到对应自签根并验证；返回的 fullchain 包含根证书，不修改输入文件。

    使用 cryptography 的服务端验证器，只支持 CA_PATHS 列出的正式环境签发链。
    timeout 限制 HTTP 网络操作时间。
    """
    if timeout <= 0:
        raise CertificateChainError("timeout 必须大于 0")
    certificates = _parse_pem(pem)
    unique = {_fingerprint(cert): cert for cert in certificates}
    duplicates = len(certificates) - len(unique)
    leaves = [cert for cert in unique.values() if not _is_ca(cert)]
    if len(leaves) != 1:
        raise CertificateChainError(f"输入必须包含唯一叶证书，当前数量: {len(leaves)}")
    leaf = leaves[0]
    names = leaf.issuer.get_attributes_for_oid(NameOID.COMMON_NAME)
    organizations = leaf.issuer.get_attributes_for_oid(NameOID.ORGANIZATION_NAME)
    issuer = names[0].value if len(names) == 1 else ""
    if issuer not in CA_PATHS or [attr.value for attr in organizations] != [CA_ORGANIZATIONS[issuer]]:
        raise CertificateChainError(f"不支持的签发 CA: {leaf.issuer.rfc4514_string()}")
    path = CA_PATHS[issuer]
    missing = tuple(name for name in path if CA_CATALOG[name]["sha256"] not in unique)
    LOGGER.info("识别证书链: issuer=%s path=%s missing_ca=%s duplicates=%d",
                issuer, " -> ".join(path), missing, duplicates)
    chain = []
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        for name in path:
            cert = unique.get(CA_CATALOG[name]["sha256"])
            chain.append(cert if cert is not None else _download_ca(client, name, timeout))
    # 按固定官方指纹选链，避免信任输入携带的任意自签根或同名 CA。
    output = _verify(leaf, chain[:-1], chain[-1], hostname)
    selected = {_fingerprint(leaf), *(_fingerprint(cert) for cert in chain)}
    unused = len(set(unique) - selected)
    ca_pem = "".join(_pem(cert) for cert in chain)
    LOGGER.info("证书链验证通过: issuer=%s added=%d duplicates=%d unused=%d",
                issuer, len(missing), duplicates, unused)
    return ChainCompletionResult(
        fullchain_pem=_pem(leaf) + ca_pem, ca_pem=ca_pem, root_pem=_pem(chain[-1]),
        chain_names=path, missing_ca=missing, duplicates_removed=duplicates,
        unused_certificates=unused, verify_output=output,
    )


def main(argv: list[str] | None = None) -> int:
    """读取输入并创建新的 fullchain 文件，任何失败返回非零退出码。"""
    parser = argparse.ArgumentParser(description="补全 Let's Encrypt / ZeroSSL 证书链并使用 cryptography 验证")
    parser.add_argument("input", type=Path, help="叶证书或不完整的 PEM 证书链")
    parser.add_argument("output", type=Path, help="新 fullchain 路径，不允许覆盖")
    parser.add_argument("--hostname", help="额外校验证书覆盖的域名")
    parser.add_argument("--timeout", type=float, default=20, help="网络操作超时秒数")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        if args.output.exists():
            raise CertificateChainError(f"输出文件已存在，拒绝覆盖: {args.output}")
        result = complete_certificate_chain(args.input.read_bytes(), hostname=args.hostname,
                                            timeout=args.timeout)
        with args.output.open("x", encoding="ascii", newline="\n") as output:
            output.write(result.fullchain_pem)
    except (CertificateChainError, OSError) as exc:
        LOGGER.error("补链失败: %s", exc)
        return 1
    LOGGER.info("已保存完整链: %s；验证结果: %s", args.output, result.verify_output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
