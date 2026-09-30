import json
import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, Optional
from app.utils.time import TimeUtil

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from app.config import load_raw_config
from app.schemas.acme import (
    AcmeShConfigError,
    AcmeShError,
    AcmeShExecutionError,
    AcmeShInfoParseError,
    AcmeShNotFoundError,
    BaseAcmeRenewer,
    RenewedCert,
)
from app.utils.store import DomainProviderStore
from app.utils.logger import get_logger
from app.utils.project_root import resolve_project_path
from app.utils.cert_parser import parse_cert_info_from_pem
from app.utils.domain_match import build_cert_domains, match_domain, normalize_domain


LOGGER = get_logger("acme")
time_util = TimeUtil()
# acme.sh --list 的 Created/Renew 列为 ISO8601
_ACME_LIST_ISO_DT = re.compile(r"^\d{4}-\d{2}-\d{2}T")
_DNS_PLUGIN_BY_PROVIDER_TYPE = {
    "aliyun": "dns_ali",
    "tencent": "dns_tencent",
    "cloudflare": "dns_cf",
}


class AcmeShRenewer(BaseAcmeRenewer):
    """
    acme.sh 证书签发与续签客户端。

    Args:
        config: 可选配置对象。默认读取 `load_raw_config()`。

    Returns:
        None

    Raises:
        AcmeShConfigError: 配置不完整或 provider 不存在时。
        AcmeShNotFoundError: acme.sh 路径无效时。

    Example:
        ```python
        from app.utils.acme_sh import AcmeShRenewer
        renewer = AcmeShRenewer()
        cert = renewer.renew("example.com")
        print(cert.expires_at.isoformat())
        ```
    """

    def __init__(self, config: Any | None = None) -> None:
        """初始化续签器并解析 acme.sh 可执行路径。"""
        self._config = config or load_raw_config()
        self._acme_sh = self._resolve_acme_sh_path()
        self._store = DomainProviderStore()

    def issue(
        self,
        domain: str,
        sans: list[str],
        provider_name: Optional[str] = None,
    ) -> RenewedCert:
        """通过 Provider 对应的 DNS 插件首次签发证书。"""
        issue_domains = self._normalize_issue_domains(domain, sans)

        if self._should_use_self_signed():
            LOGGER.info(
                "staging.enabled=true, use_self_signed=true, local self-signed issue: domains=%s",
                issue_domains,
            )
            return self._renew_with_cryptography(domain, issue_domains=issue_domains)

        existing_main = self._resolve_exact_acme_main_domain(domain)
        if existing_main:
            raise AcmeShConfigError(
                f"acme.sh 已存在 Main_Domain={existing_main} 的证书，请使用强制续签"
            )

        resolved_provider_name = self._resolve_provider_name(domain, provider_name)
        self._validate_issue_domain_providers(issue_domains, resolved_provider_name)
        dns_plugin = self._resolve_dns_plugin(resolved_provider_name)

        env = self._build_provider_env(resolved_provider_name)
        issue_cmd = self._build_issue_cmd(issue_domains, dns_plugin)
        LOGGER.info(
            "执行 acme.sh DNS 签发命令: provider=%s plugin=%s domains=%s",
            resolved_provider_name,
            dns_plugin,
            issue_domains,
        )
        self._run_cmd(issue_cmd, env=env)

        main_domain = issue_domains[0]
        info_cmd = [self._acme_sh, "--info", "-d", main_domain]
        LOGGER.info("执行 acme.sh 信息查询命令(issue): %s", " ".join(info_cmd))
        info_output = self._run_cmd(info_cmd, env=env)
        info_map = self._parse_info_output(info_output)
        return self._build_renewed_cert_from_info(main_domain, info_map)

    def renew(
        self,
        domain: str,
        provider_name: Optional[str] = None,
        force: bool = False,
    ) -> RenewedCert:
        """
        续签指定域名证书。

        Args:
            domain: 需要续签的主域名，例如 `example.com`。
            provider_name: 可选 provider 名称。默认先通过域名映射自动解析。
            force: 是否附加 `--force` 参数强制续签。

        Returns:
            `RenewedCert`: 包含证书路径、PEM 内容、有效期与证书元信息。

        Raises:
            AcmeShConfigError: provider 不存在、禁用或凭证不完整。
            AcmeShExecutionError: `acme.sh --renew/--info` 执行失败。
            AcmeShInfoParseError: `--info` 缺少必要路径字段或证书文件不可读。
        """
        if not domain:
            LOGGER.error("renew failed: domain is empty")
            raise AcmeShConfigError("domain 不能为空")

        # staging 且 use_self_signed 时，使用 cryptography 本地自签模拟续签（不调 acme.sh）
        if self._should_use_self_signed():
            LOGGER.info(
                "staging.enabled=true, use_self_signed=true, local self-signed renew: domain=%s",
                domain,
            )
            return self._renew_with_cryptography(domain)

        # 非 staging 模式：先解析 provider，再走 acme.sh 续签
        resolved_provider_name = self._resolve_provider_name(domain, provider_name)
        LOGGER.info(
            "renew provider resolved: domain=%s provider=%s",
            domain,
            resolved_provider_name,
        )
        # 线上环境要求证书已通过 acme.sh 手动签发一次，否则仅记录失败原因，由上层做页面通知。
        main_domain = self._require_acme_main_domain(domain, action="renew")

        env = self._build_provider_env(resolved_provider_name)
        renew_cmd = self._build_renew_cmd(main_domain, force=force)

        LOGGER.info("执行 acme.sh 续签命令: %s", " ".join(renew_cmd))
        self._run_cmd(renew_cmd, env=env)

        # 通过 --info 获取 acme.sh 管理的证书实际路径
        info_cmd = [self._acme_sh, "--info", "-d", main_domain]
        LOGGER.info("执行 acme.sh 信息查询命令: %s", " ".join(info_cmd))
        info_output = self._run_cmd(info_cmd, env=env)
        info_map = self._parse_info_output(info_output)

        return self._build_renewed_cert_from_info(main_domain, info_map)

    def load_existing(self, domain: str, provider_name: Optional[str] = None) -> RenewedCert:
        """
        仅加载现有证书，不执行 acme.sh --renew。
        - 线上：acme.sh 模式下，通过 --info 获取路径并读取文件。
        - 本地自签：从 cert_storage_path/domain 下直接读取已有文件，若不存在则生成自签。
        """
        if not domain:
            LOGGER.error("load_existing failed: domain is empty")
            raise AcmeShConfigError("domain 不能为空")

        if self._should_use_self_signed():
            LOGGER.info("staging 自签模式下 load_existing，直接复用/生成本地证书: domain=%s", domain)
            return self._renew_with_cryptography(domain)

        resolved_provider_name = self._resolve_provider_name(domain, provider_name)
        LOGGER.info(
            "load_existing provider resolved: domain=%s provider=%s",
            domain,
            resolved_provider_name,
        )
        main_domain = self._require_acme_main_domain(domain, action="load_existing")

        env = self._build_provider_env(resolved_provider_name)
        info_cmd = [self._acme_sh, "--info", "-d", main_domain]
        LOGGER.info("执行 acme.sh 信息查询命令(load_existing): %s", " ".join(info_cmd))
        info_output = self._run_cmd(info_cmd, env=env)
        info_map = self._parse_info_output(info_output)

        return self._build_renewed_cert_from_info(main_domain, info_map)

    def _build_renewed_cert_from_info(self, domain: str, info_map: Dict[str, str]) -> RenewedCert:
        """
        根据 acme.sh --info 输出构造 RenewedCert。

        `auto` 按自定义路径、内部记录路径、默认目录路径成对选择；
        `default` 和 `custom` 仅允许各自对应的路径来源。
        """
        mode = self._certificate_path_mode()
        source, fullchain_path, key_path, ca_path = self._select_certificate_paths(
            domain,
            info_map,
            mode,
        )

        # 云平台和 Nginx 都需要完整证书链，源文件扩展名不影响部署内容。
        cert_path = fullchain_path
        key_pem = self._read_text(key_path, "Le_KeyPath")
        fullchain_pem = self._read_text(fullchain_path, "Le_FullchainPath")
        cert_pem = fullchain_pem
        ca_pem = self._read_optional_text(ca_path)

        LOGGER.info(
            "acme.sh 证书路径已选择: domain=%s mode=%s source=%s fullchain=%s key=%s",
            domain,
            mode,
            source,
            fullchain_path,
            key_path,
        )

        # 4) 解析证书元信息
        issued_at, expires_at, key_length, sans, issuer, profile = self._parse_certificate(
            fullchain_pem or cert_pem
        )

        return RenewedCert(
            domain=domain,
            cert_path=cert_path,
            key_path=key_path,
            fullchain_path=fullchain_path,
            ca_path=ca_path,
            cert_pem=cert_pem,
            key_pem=key_pem,
            fullchain_pem=fullchain_pem,
            ca_pem=ca_pem,
            issued_at=issued_at,
            expires_at=expires_at,
            key_length=key_length,
            sans=sans,
            issuer=issuer,
            profile=profile,
        )

    def _certificate_path_mode(self) -> str:
        """读取证书路径模式；旧配置未声明时保持 auto。"""
        acme_config = getattr(self._config, "acme", {}) or {}
        raw_mode = acme_config.get("certificate_path_mode", "auto")
        mode = str(raw_mode or "auto").strip().lower()
        if mode not in {"auto", "default", "custom"}:
            raise AcmeShConfigError(f"不支持的 certificate_path_mode: {mode}")
        return mode

    def _select_certificate_paths(
        self,
        domain: str,
        info_map: Dict[str, str],
        mode: str,
    ) -> tuple[str, str, str, Optional[str]]:
        """按模式选择同一来源的完整链和私钥，禁止跨来源拼接。"""
        default_paths = self._fallback_paths_from_domain_conf(info_map)
        candidates: Dict[str, tuple[str, str, Optional[str]]] = {
            "custom": (
                (info_map.get("Le_RealFullChainPath") or "").strip(),
                (info_map.get("Le_RealKeyPath") or "").strip(),
                None,
            ),
            "recorded": (
                (info_map.get("Le_FullchainPath") or "").strip(),
                (info_map.get("Le_KeyPath") or "").strip(),
                (info_map.get("Le_CAPath") or "").strip() or None,
            ),
            "default": (
                default_paths["fullchain"] if default_paths else "",
                default_paths["key"] if default_paths else "",
                default_paths["ca"] if default_paths else None,
            ),
        }
        sources = ("custom", "recorded", "default") if mode == "auto" else (mode,)
        failures = []

        for source in sources:
            fullchain_path, key_path, ca_path = candidates[source]
            missing = self._missing_pair_paths(fullchain_path, key_path)
            if not missing:
                return source, fullchain_path, key_path, ca_path
            failures.append(f"{source}={','.join(missing)}")
            if mode == "auto":
                LOGGER.warning(
                    "acme.sh 证书路径候选不可用，继续自动识别: domain=%s source=%s missing=%s",
                    domain,
                    source,
                    missing,
                )

        detail = "; ".join(failures)
        LOGGER.error(
            "acme.sh 证书路径选择失败: domain=%s mode=%s detail=%s",
            domain,
            mode,
            detail,
        )
        raise AcmeShInfoParseError(
            f"acme.sh 证书路径不可用，domain={domain} mode={mode} detail={detail}"
        )

    @staticmethod
    def _missing_pair_paths(fullchain_path: str, key_path: str) -> list[str]:
        """返回证书路径对中缺失的字段或文件。"""
        missing = []
        for label, raw_path in (("fullchain", fullchain_path), ("key", key_path)):
            if not raw_path:
                missing.append(f"{label}:empty")
                continue
            path = Path(raw_path).expanduser()
            if not path.is_file():
                missing.append(f"{label}:{path}")
        return missing

    def _build_renew_cmd(self, main_domain: str, *, force: bool) -> list[str]:
        """构造 acme.sh --renew 命令行。"""
        renew_cmd = [self._acme_sh, "--renew", "-d", main_domain]
        dns_sleep_seconds = int(self._config.acme.dns_sleep_seconds)
        if dns_sleep_seconds > 0:
            renew_cmd.extend(["--dnssleep", str(dns_sleep_seconds)])
        if force:
            renew_cmd.append("--force")
        return renew_cmd

    def _build_issue_cmd(self, domains: list[str], dns_plugin: str) -> list[str]:
        """构造 acme.sh DNS-01 首次签发命令。"""
        if not domains:
            raise AcmeShConfigError("签发域名集合不能为空")
        plugin = str(dns_plugin or "").strip()
        if not plugin:
            raise AcmeShConfigError("acme.sh DNS 插件不能为空")

        issue_cmd = [self._acme_sh, "--issue", "--dns", plugin]
        for item in domains:
            issue_cmd.extend(["-d", item])
        dns_sleep_seconds = int(self._config.acme.dns_sleep_seconds)
        if dns_sleep_seconds > 0:
            issue_cmd.extend(["--dnssleep", str(dns_sleep_seconds)])
        return issue_cmd

    @staticmethod
    def _normalize_issue_domains(domain: str, sans: list[str]) -> list[str]:
        """保持主域名在首位，对 SAN 做归一化和稳定去重。"""
        main_domain = normalize_domain(domain)
        if main_domain.startswith("*."):
            raise AcmeShConfigError("签发主域名不能是通配域名")

        result = [main_domain]
        seen = {main_domain}
        for item in sans or []:
            normalized = normalize_domain(item)
            if normalized in seen:
                continue
            seen.add(normalized)
            result.append(normalized)
        return result

    def _validate_issue_domain_providers(
        self,
        domains: list[str],
        expected_provider_name: str,
    ) -> None:
        """确保所有签发域名均由同一个 Provider 托管。"""
        for item in domains:
            lookup_domain = item[2:] if item.startswith("*.") else item
            resolved = self._store.resolve(lookup_domain)
            if not resolved:
                raise AcmeShConfigError(
                    f"domain_provider_map 未找到签发域名 {item} 对应的云平台，请先完成域名扫描"
                )
            if resolved != expected_provider_name:
                raise AcmeShConfigError(
                    "签发域名必须属于同一云平台: "
                    f"domain={item} provider={resolved} expected={expected_provider_name}"
                )

    def _resolve_dns_plugin(self, provider_name: str) -> str:
        """按 Provider 类型解析 acme.sh DNS 插件。"""
        provider = self._find_provider(provider_name)
        if not provider:
            raise AcmeShConfigError(f"未找到云平台配置: {provider_name}")

        metadata = provider.get("metadata", {}) or {}
        custom_plugin = str(metadata.get("acme_sh_dns", "") or "").strip()
        if custom_plugin:
            return custom_plugin

        provider_type = str(provider.type or "").strip().lower()
        dns_plugin = _DNS_PLUGIN_BY_PROVIDER_TYPE.get(provider_type)
        if not dns_plugin:
            raise AcmeShConfigError(
                f"云平台 {provider_name} 不支持 acme.sh DNS 签发: type={provider_type}"
            )
        return dns_plugin

    def _require_acme_main_domain(self, domain: str, action: str) -> str:
        """解析 acme.sh 中覆盖该域名的 Main_Domain；未命中则立即失败。"""
        main_domain = self._resolve_acme_main_domain(domain)
        if not main_domain:
            LOGGER.error(
                "acme.sh cert not found: action=%s domain=%s，需先通过 acme.sh --issue 首次签发",
                action,
                domain,
            )
            raise AcmeShConfigError(
                f"acme.sh 未找到域名 {domain} 的证书，请先手动执行 acme.sh --issue 完成首次签发"
            )
        if main_domain != normalize_domain(domain):
            LOGGER.info(
                "acme.sh main domain resolved: action=%s requested=%s main=%s",
                action,
                domain,
                main_domain,
            )
        return main_domain

    def _resolve_acme_main_domain(self, domain: str) -> Optional[str]:
        """
        从 acme.sh --list 中找出覆盖目标域名的 Main_Domain。

        匹配语义与 domain_match 一致：精确命中或 RFC 6125 单级通配。
        """
        entries = self._load_acme_list_entries()
        main_domain = self._pick_acme_main_domain(entries, domain)
        LOGGER.info(
            "acme.sh list match: domain=%s entries=%s main=%s",
            domain,
            len(entries),
            main_domain,
        )
        return main_domain

    def _resolve_exact_acme_main_domain(self, domain: str) -> Optional[str]:
        """仅查找与目标完全同名的 Main_Domain，供首次签发冲突检查使用。"""
        entries = self._load_acme_list_entries()
        main_domain = self._pick_exact_acme_main_domain(entries, domain)
        LOGGER.info(
            "acme.sh list exact main match: domain=%s entries=%s main=%s",
            domain,
            len(entries),
            main_domain,
        )
        return main_domain

    def _load_acme_list_entries(self) -> list[tuple[str, set[str]]]:
        """执行 acme.sh --list 并解析证书条目。"""
        try:
            output = self._run_cmd([self._acme_sh, "--list"])
        except AcmeShError as exc:
            LOGGER.error("acme.sh --list 执行失败，无法解析已有证书: %s", exc)
            raise
        return self._parse_acme_list(output)

    @staticmethod
    def _parse_acme_list(output: str) -> list[tuple[str, set[str]]]:
        """解析 acme.sh --list，返回 [(main_domain, cert_domains), ...]。"""
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        header_index = -1
        for idx, line in enumerate(lines):
            if "Main_Domain" in line:
                header_index = idx
                break
        if header_index == -1:
            return []

        entries: list[tuple[str, set[str]]] = []
        for line in lines[header_index + 1 :]:
            parsed = AcmeShRenewer._parse_acme_list_row(line)
            if parsed:
                entries.append(parsed)
        return entries

    @staticmethod
    def _parse_acme_list_row(line: str) -> Optional[tuple[str, set[str]]]:
        """解析 acme.sh --list 的一行：Main_Domain + SAN_Domains。"""
        parts = [p for p in line.split() if p]
        if not parts:
            return None
        main = parts[0].strip().strip('"').lower().rstrip(".")
        if not main or main == "main_domain":
            return None

        created_idx = None
        for idx, token in enumerate(parts):
            if _ACME_LIST_ISO_DT.match(token):
                created_idx = idx
                break

        sans: list[str] = []
        # parts[0]=Main_Domain, parts[1]=KeyLength, 最后一个非时间 token 是 CA
        if created_idx is not None and created_idx >= 3:
            middle = parts[2 : created_idx - 1]
        elif len(parts) >= 3:
            middle = [parts[2]]
        else:
            middle = []
        for token in middle:
            for item in token.split(","):
                name = item.strip().strip('"')
                if name and "." in name:
                    sans.append(name)

        return (main, build_cert_domains(main, sans))

    @staticmethod
    def _pick_acme_main_domain(
        entries: list[tuple[str, set[str]]],
        domain: str,
    ) -> Optional[str]:
        """从已解析的 list 条目中选出覆盖目标域名的 Main_Domain。"""
        target = normalize_domain(domain)
        exact_main: Optional[str] = None
        wildcard_main: Optional[str] = None
        wildcard_base_len = -1

        for main, cert_domains in entries:
            hit = match_domain(cert_domains, target)
            if hit == "exact":
                if main == target:
                    return main
                if exact_main is None:
                    exact_main = main
                continue
            if hit != "wildcard-single":
                continue
            for item in cert_domains:
                if not item.startswith("*."):
                    continue
                base = item[2:]
                if not target.endswith(f".{base}"):
                    continue
                prefix = target[: -len(base) - 1]
                if "." in prefix:
                    continue
                if len(base) > wildcard_base_len:
                    wildcard_base_len = len(base)
                    wildcard_main = main

        return exact_main or wildcard_main

    @staticmethod
    def _pick_exact_acme_main_domain(
        entries: list[tuple[str, set[str]]],
        domain: str,
    ) -> Optional[str]:
        """只按 Main_Domain 精确匹配，不把父通配或 SAN 覆盖视为同一证书。"""
        target = normalize_domain(domain)
        for main, _cert_domains in entries:
            if normalize_domain(main) == target:
                return main
        return None

    def _is_staging_enabled(self) -> bool:
        """判断是否启用 staging 模式。"""
        return bool(self._config.staging.enabled)

    def _should_use_self_signed(self) -> bool:
        """staging 且 use_self_signed 时，本地续签用自签模拟，不调 acme.sh。"""
        if not self._is_staging_enabled():
            return False
        return bool(getattr(self._config.staging, "use_self_signed", True))

    def _renew_with_cryptography(
        self,
        domain: str,
        issue_domains: Optional[list[str]] = None,
    ) -> RenewedCert:
        """
        使用 cryptography 生成自签证书模拟“续签”。

        说明：
        - 仅在 staging.enabled 且 staging.use_self_signed 为 true 时由 renew() 调用；
        - 不依赖 acme.sh、openssl 或云平台凭证，证书写入 acme.cert_storage_path 下 {domain}/ 目录。
        """
        cert_storage = self._resolve_cert_storage_path()
        cert_dir = cert_storage / domain
        cert_dir.mkdir(parents=True, exist_ok=True)

        cert_path = cert_dir / "cert.pem"
        key_path = cert_dir / "key.pem"
        fullchain_path = cert_dir / "fullchain.pem"
        ca_path = cert_dir / "ca.pem"

        # 续签/仅加载继续复用已有证书；显式签发则按本次域名集合重新生成。
        if (
            issue_domains is None
            and cert_path.exists()
            and key_path.exists()
            and fullchain_path.exists()
        ):
            LOGGER.info("发现本地已存在自签证书，直接复用: domain=%s cert_dir=%s", domain, cert_dir.resolve())
            cert_pem = cert_path.read_text(encoding="utf-8")
            key_pem = key_path.read_text(encoding="utf-8")
            fullchain_pem = fullchain_path.read_text(encoding="utf-8")
            ca_pem = ca_path.read_text(encoding="utf-8") if ca_path.exists() else fullchain_pem

            issued_at, expires_at, key_length, sans, issuer, profile = self._parse_certificate(
                fullchain_pem or cert_pem
            )
            return RenewedCert(
                domain=domain,
                cert_path=str(cert_path),
                key_path=str(key_path),
                fullchain_path=str(fullchain_path),
                ca_path=str(ca_path),
                cert_pem=cert_pem,
                key_pem=key_pem,
                fullchain_pem=fullchain_pem,
                ca_pem=ca_pem,
                issued_at=issued_at,
                expires_at=expires_at,
                key_length=key_length,
                sans=sans,
                issuer=issuer,
                profile=profile,
            )

        # 生成新的 RSA 私钥
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = time_util.now_utc()

        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, domain)])
        cert_domains = issue_domains or [domain, f"*.{domain}"]
        sans = x509.SubjectAlternativeName(
            [x509.DNSName(item) for item in cert_domains]
        )

        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(private_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(time_util.add_minutes(now, -1))
            .not_valid_after(time_util.add_days(now, 90))
            .add_extension(sans, critical=False)
            .sign(private_key=private_key, algorithm=hashes.SHA256())
        )

        cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")
        key_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("utf-8")
        fullchain_pem = cert_pem
        ca_pem = cert_pem

        cert_path.write_text(cert_pem, encoding="utf-8")
        key_path.write_text(key_pem, encoding="utf-8")
        fullchain_path.write_text(fullchain_pem, encoding="utf-8")
        ca_path.write_text(ca_pem, encoding="utf-8")

        LOGGER.info("自签证书已写入: domain=%s cert_dir=%s", domain, cert_dir.resolve())

        issued_at, expires_at, key_length, sans, issuer, profile = self._parse_certificate(
            cert_pem
        )
        return RenewedCert(
            domain=domain,
            cert_path=str(cert_path),
            key_path=str(key_path),
            fullchain_path=str(fullchain_path),
            ca_path=str(ca_path),
            cert_pem=cert_pem,
            key_pem=key_pem,
            fullchain_pem=fullchain_pem,
            ca_pem=ca_pem,
            issued_at=issued_at,
            expires_at=expires_at,
            key_length=key_length,
            sans=sans,
            issuer=issuer,
            profile=profile,
        )

    def _resolve_cert_storage_path(self) -> Path:
        """
        解析证书存储路径。

        规则：
        - 绝对路径按配置直接使用；
        - 相对路径统一以 `app` 目录为基准（与 `utils` 同级），避免落到 `utils` 内部。
        """
        raw_path = str(self._config.acme.cert_storage_path or "./certs")
        return resolve_project_path(raw_path)

    def _resolve_provider_name(
        self,
        domain: str,
        provider_name: Optional[str] = None,
    ) -> str:
        """
        解析续签所需 provider 名称。

        优先级：
        1) 调用方显式传入 `provider_name`
        2) 从 domain_provider_map 通过域名匹配解析
        """
        if provider_name:
            return provider_name


        resolved = self._store.resolve(domain)
        if resolved:
            return resolved

        LOGGER.error("provider map resolve failed: domain=%s", domain)
        raise AcmeShConfigError(
            f"domain_provider_map 未找到域名 {domain} 对应的云平台，请先完成域名扫描"
        )




    def _resolve_acme_sh_path(self) -> str:
        """
        从配置解析 acme.sh 路径。

        支持：
        - PATH 命令名（如 `acme.sh`）
        - 文件路径（支持 `~` 展开）
        """
        if self._should_use_self_signed():
            # 自签模拟时不调用 acme.sh，返回占位可执行名避免空值
            return "acme.sh"
        raw_path = str(self._config.acme.acme_sh_path or "acme.sh")
        path = Path(raw_path).expanduser()

        has_path_hint = any(token in raw_path for token in ("/", "\\", "~", "."))
        if has_path_hint:
            if not path.exists():
                LOGGER.error("acme.sh path not found: %s", path)
                raise AcmeShNotFoundError(f"acme.sh 路径不存在: {path}")
            return str(path)

        return raw_path

    def _build_provider_env(self, provider_name: str) -> Dict[str, str]:
        """
        构建 acme.sh 执行环境变量。

        规则：
        - 优先使用 provider.metadata.acme_sh_env 自定义映射；
        - 否则按内置云厂商类型映射标准环境变量。
        """
        provider = self._find_provider(provider_name)
        if not provider:
            LOGGER.error("provider not found: %s", provider_name)
            raise AcmeShConfigError(f"未找到云平台配置: {provider_name}")

        if not bool(provider.enabled):
            LOGGER.error("provider disabled: %s", provider_name)
            raise AcmeShConfigError(f"云平台已禁用: {provider_name}")

        credentials = provider.credentials
        env = os.environ.copy()
        metadata = provider.get("metadata", {})

        # 自定义映射：{"Ali_Key":"access_key_id", ...}
        custom_env_map = metadata.get("acme_sh_env")
        if custom_env_map:
            env_map = self._parse_env_map(provider_name, custom_env_map)
            for env_key, cred_key in env_map.items():
                value = credentials.get(cred_key)
                if not value:
                    LOGGER.error(
                        "provider credential missing: provider=%s key=%s",
                        provider_name,
                        cred_key,
                    )
                    raise AcmeShConfigError(
                        f"云平台 {provider_name} 缺少凭证字段: {cred_key}"
                    )
                env[str(env_key)] = str(value)
            return env

        provider_type = str(provider.type).strip().lower()
        # 内置映射：兼容常见 DNS API 凭证字段
        if provider_type == "aliyun":
            self._require_credential(credentials, provider_name, "access_key_id")
            self._require_credential(credentials, provider_name, "access_key_secret")
            env["Ali_Key"] = str(credentials["access_key_id"])
            env["Ali_Secret"] = str(credentials["access_key_secret"])
            return env

        if provider_type == "tencent":
            self._require_credential(credentials, provider_name, "secret_id")
            self._require_credential(credentials, provider_name, "secret_key")
            env["Tencent_SecretId"] = str(credentials["secret_id"])
            env["Tencent_SecretKey"] = str(credentials["secret_key"])
            return env

        if provider_type == "huawei":
            self._require_credential(credentials, provider_name, "access_key_id")
            self._require_credential(credentials, provider_name, "access_key_secret")
            env["Huawei_Cloud_AccessKey"] = str(credentials["access_key_id"])
            env["Huawei_Cloud_SecretKey"] = str(credentials["access_key_secret"])
            return env

        if provider_type == "cloudflare":
            self._require_credential(credentials, provider_name, "api_token")
            # 防止进程环境中的旧账号或单 Zone 限定污染当前 Provider。
            env.pop("CF_Account_ID", None)
            env.pop("CF_Zone_ID", None)
            env["CF_Token"] = str(credentials["api_token"])
            account_id = str(credentials.get("account_id", "") or "").strip()
            if account_id:
                env["CF_Account_ID"] = account_id
            return env

        LOGGER.error("provider type unsupported for acme renew: %s", provider_type)
        raise AcmeShConfigError(
            f"云平台 {provider_name} 未配置 acme.sh 续签映射: type={provider_type}"
        )

    def _find_provider(self, provider_name: str) -> Any | None:
        """按 provider 名称从配置中查找 provider。"""
        for provider in self._config.providers:
            if str(provider.name) == provider_name:
                return provider
        return None

    @staticmethod
    def _parse_env_map(provider_name: str, env_map_raw: Any) -> Dict[str, str]:
        """解析 acme_sh_env（支持 dict 或 JSON 字符串）。"""
        if isinstance(env_map_raw, dict):
            return {str(k): str(v) for k, v in env_map_raw.items()}
        try:
            parsed = json.loads(str(env_map_raw))
        except json.JSONDecodeError as exc:
            raise AcmeShConfigError(
                f"云平台 {provider_name} 的 acme_sh_env 不是有效 JSON"
            ) from exc
        if not isinstance(parsed, dict):
            raise AcmeShConfigError(
                f"云平台 {provider_name} 的 acme_sh_env 必须是对象映射"
            )
        return {str(k): str(v) for k, v in parsed.items()}

    @staticmethod
    def _require_credential(credentials: Dict[str, Any], provider_name: str, key: str) -> None:
        """校验必需凭证字段是否存在。"""
        value = credentials.get(key)
        if not value:
            LOGGER.error("provider credential missing: provider=%s key=%s", provider_name, key)
            raise AcmeShConfigError(f"云平台 {provider_name} 缺少凭证字段: {key}")

    @staticmethod
    def _stream_subprocess_output(
        stream: Any,
        lines: list[str],
        log_line,
    ) -> None:
        """后台线程逐行读取子进程输出，避免 stdout/stderr 管道阻塞。"""
        try:
            for raw_line in iter(stream.readline, ""):
                line = raw_line.rstrip("\r\n")
                lines.append(line)
                log_line(line)
        finally:
            stream.close()

    def _join_output_threads(
        self,
        stdout_thread: threading.Thread,
        stderr_thread: threading.Thread,
    ) -> None:
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)

    def _run_cmd(self, cmd: list[str], env: Optional[Dict[str, str]] = None) -> str:
        """执行外部命令并返回 stdout，执行过程中实时输出日志。"""
        cmd_str = " ".join(cmd)
        try:
            proc = subprocess.Popen(
                cmd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as exc:
            LOGGER.error("command not found: %s", cmd_str)
            raise AcmeShNotFoundError(f"命令不存在: {cmd_str}") from exc

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []

        def log_stdout(line: str) -> None:
            LOGGER.info("[acme.sh stdout] %s", line)

        def log_stderr(line: str) -> None:
            if line:
                LOGGER.warning("[acme.sh stderr] %s", line)

        stdout_thread = threading.Thread(
            target=self._stream_subprocess_output,
            args=(proc.stdout, stdout_lines, log_stdout),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=self._stream_subprocess_output,
            args=(proc.stderr, stderr_lines, log_stderr),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()

        timeout_seconds = int(self._config.acme.command_timeout_seconds)
        try:
            proc.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            proc.wait()
            self._join_output_threads(stdout_thread, stderr_thread)
            stdout_text = "\n".join(stdout_lines)
            stderr_text = "\n".join(stderr_lines)
            LOGGER.error(
                "acme.sh command timeout: cmd=%s timeout=%ss stdout=%s stderr=%s",
                cmd_str,
                timeout_seconds,
                stdout_text,
                stderr_text,
            )
            raise AcmeShExecutionError(
                f"acme.sh 命令执行超时: timeout={timeout_seconds}s cmd={cmd_str}"
            ) from exc

        self._join_output_threads(stdout_thread, stderr_thread)
        stdout_text = "\n".join(stdout_lines)
        stderr_text = "\n".join(stderr_lines)
        returncode = proc.returncode

        if returncode != 0:
            LOGGER.error(
                "command failed: cmd=%s code=%s stdout=%s stderr=%s",
                cmd_str, returncode, stdout_text, stderr_text,
            )
            LOGGER.error("---------- acme.sh command end ----------")
            raise AcmeShExecutionError(
                f"执行命令失败: {cmd_str}; exit_code={returncode}; stderr={stderr_text}"
            )

        return stdout_text

    @staticmethod
    def _parse_info_output(raw: str) -> Dict[str, str]:
        """解析 `acme.sh --info` 输出为键值映射。"""
        data: Dict[str, str] = {}
        for line in raw.splitlines():
            line = line.strip()
            if not line or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip("'").strip('"')
            if key:
                data[key] = value
        return data

    @staticmethod
    def _must_path(info_map: Dict[str, str], key: str) -> str:
        """从 info 映射中读取必填路径字段。"""
        value = info_map.get(key, "").strip()
        if not value:
            raise AcmeShInfoParseError(f"`acme.sh --info` 缺少关键字段: {key}")
        path = str(Path(value).expanduser())
        return path

    @staticmethod
    def _fallback_paths_from_domain_conf(
        info_map: Dict[str, str],
    ) -> Optional[Dict[str, str]]:
        """
        根据 DOMAIN_CONF 与 Le_Domain 推断 acme.sh 默认证书路径。

        acme.sh 仅 --issue（未 --install-cert）的证书，conf 中不会写入
        Le_*Path 字段，但证书文件按固定约定存放在 DOMAIN_CONF 同目录：
            <Le_Domain>.key / fullchain.cer / ca.cer

        说明：不返回 leaf 单证书（<Le_Domain>.cer）。下游 nginx 部署使用
        cert_pem 内容写入对端，需要带中间链，故统一以 fullchain.cer 充当
        cert_pem 的来源（由调用方通过 cert_path = cert_path or fullchain_path
        兜底实现），避免对端只拿到 leaf 丢失中间链。
        """
        domain_conf = (info_map.get("DOMAIN_CONF") or "").strip()
        le_domain = (info_map.get("Le_Domain") or "").strip()
        if not domain_conf or not le_domain:
            return None
        cert_dir = Path(domain_conf).expanduser().parent
        return {
            "key": str(cert_dir / f"{le_domain}.key"),
            "fullchain": str(cert_dir / "fullchain.cer"),
            "ca": str(cert_dir / "ca.cer"),
        }

    @staticmethod
    def _read_text(path: str | None, key: str) -> str:
        """读取文本文件并校验存在性。"""
        if not path:
            raise AcmeShInfoParseError(f"`acme.sh --info` 缺少路径字段: {key}")
        p = Path(path).expanduser()
        if not p.exists():
            raise AcmeShInfoParseError(f"证书文件不存在: {key}={p}")
        return p.read_text(encoding="utf-8")

    @staticmethod
    def _read_optional_text(path: str | None) -> str:
        """读取可选文件；路径未配置或文件不存在时返回空内容。"""
        if not path:
            return ""
        resolved = Path(path).expanduser()
        if not resolved.is_file():
            LOGGER.warning("可选 CA 文件不存在，跳过读取: %s", resolved)
            return ""
        return resolved.read_text(encoding="utf-8")

    @staticmethod
    def _parse_certificate(
        cert_pem: str,
    ) -> tuple[Any, Any, str, list[str], str, Optional[str]]:
        """从 PEM 中提取证书元数据。"""
        parsed = parse_cert_info_from_pem(cert_pem)
        return (
            parsed.not_before_utc,
            parsed.not_after_utc,
            parsed.key_length,
            parsed.sans,
            parsed.issuer,
            parsed.profile,
        )


# 兼容历史导入路径
AcmeShIssuer = AcmeShRenewer
