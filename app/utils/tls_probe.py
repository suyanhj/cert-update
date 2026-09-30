import asyncio

import socket
import ssl

from app.config import load_raw_config

from cryptography import x509


from .logger import get_logger
from .time import TimeUtil
from .cert_parser import parse_cert_info_from_der

logger = get_logger("app")


tutil = TimeUtil()

_SSL_CONTEXT = ssl.create_default_context()
_SSL_CONTEXT.check_hostname = False
_SSL_CONTEXT.verify_mode = ssl.CERT_NONE



def _cert_match_domain(cert: x509.Certificate, domain: str) -> bool:
    domain = domain.lower().lstrip("*.")
    names: list[str] = []

    # SAN 优先
    try:
        ext = cert.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        )
        names.extend(ext.value.get_values_for_type(x509.DNSName))
    except x509.ExtensionNotFound:
        pass

    # CN 兜底
    for attr in cert.subject:
        if attr.oid == x509.NameOID.COMMON_NAME:
            names.append(attr.value)

    for name in names:
        name = name.lower()
        if name.startswith("*.") and domain.endswith(name[1:]):
            return True
        if name == domain:
            return True

    return False


def _extract_cert_info(cert_der: bytes, domain: str):
    """从 DER 格式证书中提取信息。"""
    try:
        cert = x509.load_der_x509_certificate(cert_der)

        if not _cert_match_domain(cert, domain):
            logger.debug(
                "证书 主体/sans 不匹配,可能未使用tls 跳过: %s", domain
            )
            return None

        parsed = parse_cert_info_from_der(cert_der)
        issuer_org = parsed.issuer
        subject_cn = parsed.subject
        key_length = parsed.key_length
        sans = parsed.sans

        t = tutil.parse(parsed.not_after_utc)
        expires_at = tutil.to_local_tz( t)

        logger.debug(f"证书解析成功: {subject_cn}")

        return {
            "domain": subject_cn,
            "key":key_length,
            "sans": sans,
            "ca": issuer_org,
            "exp": expires_at,
            "days": tutil.remaining_days(t)
        }
    except Exception as e:
        logger.debug("解析证书失败: %s", e)



def probe_tls_sync(domain: str, port: int = 443) :
    """
    同步探测域名的 TLS 证书。
    
    Args:
        domain: 要探测的域名
        port: 端口号，默认 443
        
    Returns:
        RemoteCertInfo 包含证书信息或错误
    """
    try:
        probe_timeout = int(load_raw_config().tls_timeout_seconds)
        # 创建 SSL 上下文，不验证证书（我们只是获取信息）
        context = _SSL_CONTEXT
        logger.debug(f"开始获取证书 {domain}")
        with socket.create_connection((domain, port), timeout=probe_timeout) as sock:
            sock.settimeout(probe_timeout)
            with context.wrap_socket(sock, server_hostname=domain) as ssock:
                ssock.settimeout(probe_timeout)
                cert_der = ssock.getpeercert(binary_form=True)
                if not cert_der:
                    logger.debug("未获取到证书 %s", domain)
                    return None

                return _extract_cert_info(cert_der, domain)
    except socket.timeout:
        logger.debug(f"连接超时: {domain}:{port}")
    except socket.gaierror as e:
        logger.error(f"DNS 解析失败 {domain}: {e}")
    except ConnectionRefusedError:
        logger.debug(f"连接被拒绝 {domain}:{port}")
    except ssl.SSLError as e:
        logger.debug(f"SSL 错误 {domain}: {e}")
    except Exception as e:
        logger.debug(f"探测失败 { domain}:{port}: {e}")



async def probe_tls(domain: str, port: int = 443):
    """
    异步探测域名的 TLS 证书。
    
    Args:
        domain: 要探测的域名
        port: 端口号，默认 443
        
    Returns:
        RemoteCertInfo 包含证书信息或错误
    """
    return await asyncio.to_thread(probe_tls_sync, domain, port)



async def probe_domains_batch(
    domains: set[str]|list[ str],
    port: int = 443,
    concurrency: int | None = None,
) :
    """
    批量探测多个域名的 TLS 证书。
    
    Args:
        domains: 域名列表
        port: 端口号
        concurrency: 并发数
        
    Returns:
        域名到证书信息的映射
    """
    current_concurrency = int(concurrency or load_raw_config().tls_scan_parallel)
    semaphore = asyncio.Semaphore(current_concurrency)
    results = {}
    
    async def probe_with_semaphore(domain: str):
        async with semaphore:
            info = await probe_tls(domain, port)
            return domain, info

    tasks = [ asyncio.create_task(probe_with_semaphore(d)) for d in domains]
    for coro in asyncio.as_completed(tasks):
        try:
            domain, info = await coro
            if info:
                results[domain] = info
                logger.debug(
                    "探测 %s 成功: issuer=%s, expires=%s",
                    domain, info['ca'], info['exp']
                )
            else:
                logger.debug("探测 %s 失败: %s", domain, info)


        except Exception:
            logger.exception("single domain probe failed")

    return results
