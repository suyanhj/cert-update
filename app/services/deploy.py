from __future__ import annotations

from dataclasses import asdict
import os
from typing import Any, Dict, List, Optional, Set

from app.config import load_raw_config
from app.deploys import NginxSSHDeployer
from app.deploys.nginx_ssh import NginxServerConfig as NginxSSHServerConfig
from app.deploys.base import CertificateDeployer, DeployResult, DeployTarget
from app.services.deployer_factory import build_cloud_deployer
from app.services.nginx_planner import NginxDeployPlanner
from app.providers.base import Provider
from app.providers import (
    AliyunCloudProvider,
    HuaweiCloudProvider,
    QiniuCloudProvider,
    TencentCloudProvider,
    VolcengineCloudProvider,
)
from app.schemas.acme import RenewedCert
from app.schemas.alert import AlertEvent, AlertLevel
from app.schemas.provider import (
    CloudProductBinding,
    DeployMatchResult,
    binding_from_cache_dict,
)
from app.services.bindings import BindingCacheService
from app.services.provider_factory import build_cloud_providers, get_provider_builder_map
from app.utils.domain_match import build_cert_domains, match_domain, normalize_domain
from app.utils.logger import get_logger
from app.utils.notifier import DingDingNotifier
from app.utils.store import ProductBindingStore, DeployStateStore
from app.utils.time import TimeUtil

LOGGER = get_logger("app")


class _LegacyProviderDeployer(CertificateDeployer):
    """兼容旧版 provider 接口（upload/deploy_certificate_with_cert_id）。"""
    def __init__(self, provider: Provider) -> None:
        self._provider = provider

    @property
    def name(self) -> str:
        return str(self._provider.name or "legacy-provider")

    async def deploy(
        self,
        cert_pem: str,
        key_pem: str,
        target: DeployTarget,
        cert_id: Optional[str] = None,
    ) -> DeployResult:
        upload = getattr(self._provider, "upload_certificate_to_cas", None)
        deploy = getattr(self._provider, "deploy_certificate_with_cert_id", None)
        if not callable(deploy):
            return DeployResult(
                success=False,
                target=target,
                message="legacy deploy api missing",
                cert_id=None,
            )

        try:
            if cert_id is None and callable(upload):
                alias = f"{target.domain or 'cert'}-{TimeUtil.now().strftime('%Y%m%d%H%M%S')}"
                cert_id_raw = await upload(cert_pem, key_pem, alias)
                cert_id = str(cert_id_raw) if cert_id_raw else None

            ok = await deploy(
                product_type=target.product_type,
                product_id=target.product_id,
                domain=target.domain,
                cert_id=cert_id,
                listener_port=target.listener_port,
                metadata=target.metadata or {},
            )
            if ok:
                return DeployResult(
                    success=True,
                    target=target,
                    message="legacy deploy success",
                    cert_id=cert_id,
                )
            return DeployResult(
                success=False,
                target=target,
                message="legacy deploy failed",
                cert_id=cert_id,
            )
        except Exception as exc:
            return DeployResult(
                success=False,
                target=target,
                message=f"legacy deploy exception: {exc}",
                cert_id=cert_id,
            )

    async def list_targets(self) -> List[DeployTarget]:
        return []


class DeployService:
    """负责多云匹配与部署，不负责续签。"""
    def __init__(self) -> None:
        self.conf = load_raw_config()
        self._binding_store = ProductBindingStore()

    def _binding_cache_settings(self) -> tuple[bool, int, bool]:
        cache_conf = self.conf.deploy.binding_cache
        if not cache_conf:
            return True, 3600, True

        enabled = bool(cache_conf.enabled) if cache_conf.enabled != "" else True
        ttl_seconds = int(cache_conf.ttl_seconds or 3600)
        verify_before_apply = bool(cache_conf.verify_before_apply) if cache_conf.verify_before_apply != "" else True

        if ttl_seconds <= 0:
            raise ValueError("deploy.binding_cache.ttl_seconds 必须大于 0")
        return enabled, ttl_seconds, verify_before_apply

    @staticmethod
    def _cache_is_valid(updated_at: str, ttl_seconds: int) -> bool:
        if not updated_at:
            return False
        try:
            dt = TimeUtil.parse(updated_at)
        except Exception:
            return False
        age = (TimeUtil.now_utc() - dt).total_seconds()
        if age < 0:
            return True
        return age <= ttl_seconds

    @staticmethod
    def _binding_key(product_type: str, product_id: str, domain: str) -> tuple[str, str, str]:
        # 缓存 key 行为稳定：空域名保持空；非空域名使用统一归一化
        domain_raw = str(domain or "").strip()
        return (
            product_type.strip().lower(),
            product_id.strip(),
            normalize_domain(domain_raw) if domain_raw else "",
        )

    @staticmethod
    def _provider_type(provider: Provider) -> str:
        if provider.config:
            provider_type = str(provider.config.type or "").strip().lower()
            if provider_type:
                return provider_type
        provider_type = str(getattr(provider, "_provider_type", "") or "").strip().lower()
        if provider_type:
            return provider_type
        raise RuntimeError(f"provider 缺少类型信息: {provider}")

    @staticmethod
    def _provider_name(provider: Provider) -> str:
        name = str(provider.name or "").strip()
        if name:
            return name
        if provider.config:
            name = str(provider.config.name or "").strip()
            if name:
                return name
        raise RuntimeError(f"provider 缺少名称信息: {provider}")

    @staticmethod
    def _provider_cache_key(provider: Provider) -> str:
        provider_type = DeployService._provider_type(provider)
        provider_name = DeployService._provider_name(provider)
        return f"{provider_type}:{provider_name}"

    async def _get_provider_bindings(
        self,
        provider: Provider,
        cache_enabled: bool,
        ttl_seconds: int,
        force_refresh: bool = False,
        cache_only: bool = False,
    ) -> List[CloudProductBinding]:
        """
        获取某 provider 的产品绑定。
        行为约定：
        - cache_only=True：硬约束，必须只读缓存，不打云 API（与 force_refresh 同时为真时优先 cache_only）；
        - cache_enabled=True && force_refresh=False：只读本地缓存，无缓存或过期则视为无绑定；
        - cache_enabled=True && force_refresh=True：绕过缓存，按 product_scan.<cloud> 配置走云端实时扫描；
          仅返回结果，不回写缓存（缓存落盘由 ProductBindingService.refresh_all 独占）；
        - cache_enabled=False：直接调用云端实时扫描，不落盘，force_refresh 无影响。
        """
        provider_cache_key = self._provider_cache_key(provider)
        if self._provider_type(provider) == "tencent":
            # 腾讯云部署目标由上传证书后的 SSL Host 扫描提供，不读本地绑定缓存。
            return []

        # cache_only 是硬约束：与 force_refresh 同时为真时仍只读缓存
        if cache_only or (cache_enabled and not force_refresh):
            record = self._binding_store.get_provider(provider_cache_key)
            if not record:
                return []

            updated_at = record.get("updated_at", "")
            if not self._cache_is_valid(updated_at, ttl_seconds):
                return []

            raw_bindings = record.get("bindings", [])
            if not isinstance(raw_bindings, list):
                LOGGER.warning("产品映射缓存数据异常，忽略该 provider: %s", provider_cache_key)
                return []
            return [binding_from_cache_dict(item) for item in raw_bindings]

        # 走云端实时扫描分支（cache_enabled=False，或 force_refresh=True 且未被 cache_only 拦截）
        provider_type = self._provider_type(provider)
        scan_conf = self.conf.product_scan
        cloud_scan_conf = getattr(scan_conf, provider_type)

        bindings: List[CloudProductBinding] = []
        scan_enabled = BindingCacheService._scan_enabled
        scan_tasks = [
            ("CDN", "cdn_scan", True, "get_cdn_bindings"),
            ("云直播", "live_scan", False, "get_live_bindings"),
            ("LB", "lb_scan", True, "get_lb_bindings"),
            ("OSS", "oss_scan", True, "get_oss_bindings"),
            ("WAF", "waf_scan", False, "get_waf_bindings"),
        ]
        for product_label, conf_key, default_enabled, method_name in scan_tasks:
            if not scan_enabled(cloud_scan_conf, conf_key, default_enabled):
                LOGGER.info(
                    "product_scan: 跳过 %s 扫描 provider=%s type=%s",
                    product_label,
                    provider.name,
                    provider_type,
                )
                continue
            method = getattr(provider, method_name, None)
            if method is None:
                continue
            try:
                items = await method()
                bindings.extend(items or [])
            except Exception as exc:
                LOGGER.warning(
                    "[%s] %s 绑定扫描失败，跳过该产品: %s",
                    provider.name,
                    product_label,
                    exc,
                )

        if force_refresh and cache_enabled:
            LOGGER.info(
                "force_refresh: provider=%s 已从云端拉取 binding 数=%d（不回写缓存）",
                provider_cache_key,
                len(bindings),
            )

        return bindings

    async def _verify_matches_before_apply(
        self,
        providers: List[Provider],
        matches: List[DeployMatchResult],
        skipped: List[Dict[str, str]],
        cache_enabled: bool,
        ttl_seconds: int,
        notifier: Optional[DingDingNotifier] = None,
    ) -> List[DeployMatchResult]:
        """
        apply 前实时校验：按 (product_type, product_id, domain) 在实时数据中查找。
        - 实时缺失目标 → skip + 钉钉 WARNING
        - 实时存在 → 用实时 binding 字段（cert_id/listener_port/metadata）覆盖 match 后放行
        - 实时扫描整体抛异常 → 该 provider 下所有 match 全部 skip + 钉钉 WARNING
        verify 拉取的实时数据仅用于本次部署，不回写缓存（由 _get_provider_bindings 保证）。
        """
        provider_map = {self._provider_cache_key(p): p for p in providers}
        verified: List[DeployMatchResult] = []
        grouped_matches: Dict[str, List[DeployMatchResult]] = {}
        for item in matches:
            grouped_matches.setdefault(f"{item.provider_type}:{item.provider_name}", []).append(item)

        notifier = notifier or DingDingNotifier()

        for provider_cache_key, provider_matches in grouped_matches.items():
            provider = provider_map.get(provider_cache_key)
            if not provider:
                # provider 在 collect 阶段存在却在 verify 阶段丢失，属于不可恢复的异常状态，直接抛出而非沿用旧数据
                raise RuntimeError(f"verify: provider 不存在: {provider_cache_key}")
            try:
                realtime_bindings = await self._get_provider_bindings(
                    provider=provider,
                    cache_enabled=cache_enabled,
                    ttl_seconds=ttl_seconds,
                    force_refresh=True,
                )
            except Exception as exc:
                # 实时扫描失败：宁可不动，整 provider 全部 skip + 告警
                LOGGER.warning(
                    "[%s] verify: 实时扫描失败，该 provider 下 %d 个目标全部跳过: %s",
                    provider.name, len(provider_matches), exc,
                )
                for match in provider_matches:
                    skipped.append(
                        {
                            "provider": match.provider_name,
                            "product_type": match.product_type,
                            "product_id": match.product_id,
                            "reason": f"apply 前实时校验失败，已跳过: {exc}",
                        }
                    )
                await self._notify_verify_skip(
                    notifier=notifier,
                    provider_name=provider.name,
                    product_type="*",
                    product_id="*",
                    domain="",
                    reason=f"实时扫描接口异常: {exc}",
                )
                continue

            index: Dict[tuple[str, str, str], CloudProductBinding] = {}
            for binding in realtime_bindings:
                key = self._binding_key(binding.product_type, binding.product_id, binding.domain)
                index[key] = binding

            for match in provider_matches:
                key = self._binding_key(match.product_type, match.product_id, match.domain)
                refreshed = index.get(key)
                if not refreshed:
                    skipped.append(
                        {
                            "provider": match.provider_name,
                            "product_type": match.product_type,
                            "product_id": match.product_id,
                            "reason": "apply 前实时校验未命中，已跳过",
                        }
                    )
                    LOGGER.warning(
                        "verify skip: provider=%s product=%s/%s domain=%s 实时数据已不存在该绑定",
                        match.provider_name, match.product_type, match.product_id, match.domain,
                    )
                    await self._notify_verify_skip(
                        notifier=notifier,
                        provider_name=match.provider_name,
                        product_type=match.product_type,
                        product_id=match.product_id,
                        domain=match.domain,
                        reason="实时绑定中已不存在该目标",
                    )
                    continue
                # 实时存在：用实时字段覆盖 match
                verified.append(
                    DeployMatchResult(
                        provider_type=match.provider_type,
                        provider_name=match.provider_name,
                        product_type=match.product_type,
                        product_id=match.product_id,
                        domain=refreshed.domain,
                        match_type=match.match_type,
                        current_cert_id=refreshed.cert_id,
                        listener_port=refreshed.listener_port,
                        metadata=refreshed.metadata or {},
                    )
                )
        return verified

    @staticmethod
    async def _notify_verify_skip(
        notifier: DingDingNotifier,
        provider_name: str,
        product_type: str,
        product_id: str,
        domain: str,
        reason: str,
    ) -> None:
        # 部署校验差异告警：钉钉 WARNING；钉钉发送失败仅记日志，不影响主流程
        message = (
            f"product: {product_type} / {product_id}\n"
            f"原因: {reason}"
        )
        event = AlertEvent(
            level=AlertLevel.WARNING,
            title="部署校验差异（已跳过）",
            message=message,
            source="deploy",
            domain=domain or None,
            provider=provider_name,
        )
        try:
            await notifier.notify_alert(event)
        except Exception as exc:
            LOGGER.warning("verify skip 告警发送失败: %s", exc)

    def _build_cloud_providers(self) -> List[Provider]:
        provider_builder = get_provider_builder_map(
            include_custom=False,
            overrides={
                "aliyun": AliyunCloudProvider,
                "tencent": TencentCloudProvider,
                "huawei": HuaweiCloudProvider,
                # "volcengine": VolcengineCloudProvider,
                "qiniu": QiniuCloudProvider,
            },
        )
        configs = list(self.conf.providers)
        # 不再向 provider 配置中注入 products，顶层 product_scan 仅由调用方在运行时用于控制哪些能力启用。
        return build_cloud_providers(configs, provider_builder)

    @staticmethod
    def _build_nginx_target_auth_map(conf: Any) -> Dict[str, Dict[str, str]]:
        profiles = conf.ssh_profiles or []
        hosts = conf.nginx.nginx_hosts or [] if conf.nginx else []

        profile_map: Dict[str, Any] = {}
        for profile in profiles:
            profile_name = str(profile.name or "").strip()
            if profile_name:
                profile_map[profile_name] = profile

        auth_map: Dict[str, Dict[str, str]] = {}
        for host in hosts:
            host_name = str(host.name or "").strip()
            profile_name = str(host.ssh_profile or "").strip()
            if not host_name:
                raise RuntimeError("nginx.nginx_hosts 存在空 name")
            if not profile_name:
                raise RuntimeError(f"nginx host={host_name} 缺少 ssh_profile")
            profile = profile_map.get(profile_name)
            if not profile:
                raise RuntimeError(f"nginx host={host_name} 引用的 ssh_profile 不存在: {profile_name}")

            auth = profile.auth
            auth_type = str(auth.type or "key").strip().lower()
            if auth_type == "key":
                key_path = str(auth.key_path or "").strip()
                if not key_path:
                    raise RuntimeError(f"ssh_profile={profile_name} type=key 缺少 key_path")
                auth_map[host_name] = {
                    "auth_type": "key",
                    "key_path": key_path,
                    "password": "",
                }
                continue
            if auth_type == "password":
                password_env = str(auth.password_env or "").strip()
                password = str(auth.password or "").strip()
                resolved_password = os.getenv(password_env) if password_env else password
                if not resolved_password:
                    raise RuntimeError(
                        f"ssh_profile={profile_name} type=password 缺少 password 或 password_env 对应环境变量"
                    )
                auth_map[host_name] = {
                    "auth_type": "password",
                    "key_path": "",
                    "password": resolved_password,
                }
                continue
            raise RuntimeError(f"ssh_profile={profile_name} auth.type 不支持: {auth_type}")
        return auth_map

    async def _plan_nginx_deploy(self, renewed_cert: RenewedCert) -> Dict[str, Any]:
        planner = NginxDeployPlanner()
        try:
            return planner.plan_for_certificate(main_domain=renewed_cert.domain, sans=renewed_cert.sans)
        except Exception as exc:
            LOGGER.warning("nginx plan skipped: %s", exc)
            return {
                "mode": "dry-run",
                "main_domain": renewed_cert.domain,
                "sans": list(renewed_cert.sans),
                "matched_rule_count": 0,
                "target_count": 0,
                "targets": [],
                "errors": [str(exc)],
            }

    async def _apply_nginx_deploy(self, renewed_cert: RenewedCert) -> List[Dict[str, Any]]:
        plan = await self._plan_nginx_deploy(renewed_cert)
        targets = plan.get("targets", [])
        if not isinstance(targets, list) or not targets:
            return []

        try:
            auth_map = self._build_nginx_target_auth_map(self.conf)
        except Exception as exc:
            LOGGER.warning("nginx 认证配置解析失败，跳过 nginx 部署: %s", exc)
            return []

        bind_results: List[Dict[str, Any]] = []
        deploy_state = DeployStateStore()

        for idx, target in enumerate(targets):
            host_name = str(target.get("host_name", "") or "").strip()
            host = str(target.get("host", "") or "").strip()
            if not host_name or not host:
                LOGGER.warning("nginx 计划目标缺少 host_name/host，跳过: %s", target)
                continue
            if host_name not in auth_map:
                LOGGER.warning("nginx 计划目标缺少认证信息，跳过: host=%s", host_name)
                continue

            auth = auth_map[host_name]
            server_name = f"{host_name}#{idx}"
            server = NginxSSHServerConfig(
                name=server_name,
                host=host,
                port=int(target.get("port", 22)),
                username=str(target.get("username", "root") or "root"),
                key_path=auth["key_path"] or None,
                password=auth["password"] or None,
                sudo=bool(target.get("sudo", False)),
                backup_dir=target.get("backup_dir") or None,
                timeout_seconds=int(target.get("timeout_seconds", 20)),
                cert_path=str(target.get("cert_file", "") or ""),
                key_path_tmpl=str(target.get("key_file", "") or ""),
                file_mode=str(target.get("file_mode", "644") or "644"),
                reload_command=str(target.get("reload_command", "nginx -s reload") or "nginx -s reload"),
                reload_use_placeholders=bool(target.get("reload_use_placeholders", False)),
                test_command=str(target.get("test_command", "nginx -t") or "nginx -t"),
            )

            deployer = NginxSSHDeployer([server])
            deploy_target = DeployTarget(
                provider="nginx-ssh",
                product_type="nginx",
                product_id=server_name,
                domain=renewed_cert.domain,
            )

            result = await deployer.deploy(
                cert_pem=renewed_cert.cert_pem,
                key_pem=renewed_cert.key_pem,
                target=deploy_target,
                cert_id=None,
            )

            status = "success" if result.success else "failed"
            payload = {
                "provider_type": "nginx",
                "provider": "nginx-ssh",
                "product_type": "nginx",
                "product_id": host_name,
                "domain": renewed_cert.domain,
                "group": str(target.get("target_group", "") or ""),
                "status": status,
                "message": result.message,
            }
            bind_results.append(payload)

            # 记录 nginx 部署状态
            if status == "success":
                deploy_state.record_success(
                    domain=renewed_cert.domain,
                    provider="nginx-ssh",
                    product_type="nginx",
                    product_id=host_name,
                    message=result.message or "",
                )
            else:
                deploy_state.record_failure(
                    domain=renewed_cert.domain,
                    provider="nginx-ssh",
                    product_type="nginx",
                    product_id=host_name,
                    message=result.message or "",
                )

        return bind_results

    @staticmethod
    def _build_deployer(provider: Provider) -> CertificateDeployer:
        if not provider.config:
            return _LegacyProviderDeployer(provider)

        provider_type = DeployService._provider_type(provider)
        provider_name = DeployService._provider_name(provider)
        creds = provider.config.credentials or {}
        return build_cloud_deployer(
            provider_type=provider_type,
            provider_name=provider_name,
            credentials=creds,
        )

    @staticmethod
    def _summarize_matches(matches: List[DeployMatchResult]) -> Dict[str, Any]:
        by_provider_type: Dict[str, int] = {}
        by_provider: Dict[str, int] = {}
        by_product_type: Dict[str, int] = {}
        for item in matches:
            by_provider_type[item.provider_type] = by_provider_type.get(item.provider_type, 0) + 1
            provider_key = f"{item.provider_type}:{item.provider_name}"
            by_provider[provider_key] = by_provider.get(provider_key, 0) + 1
            by_product_type[item.product_type] = by_product_type.get(item.product_type, 0) + 1
        return {
            "by_provider_type": by_provider_type,
            "by_provider": by_provider,
            "by_product_type": by_product_type,
            "total_matches": len(matches),
        }

    @staticmethod
    def _summarize_bind_results(bind_results: List[Dict[str, Any]]) -> Dict[str, Any]:
        by_provider_type: Dict[str, Dict[str, int]] = {}
        by_provider: Dict[str, Dict[str, int]] = {}
        by_product_type: Dict[str, Dict[str, int]] = {}
        for item in bind_results:
            provider_type = item.get("provider_type", "")
            provider = item.get("provider", "")
            product_type = item.get("product_type", "")
            status = item.get("status", "unknown")
            provider_key = f"{provider_type}:{provider}" if provider_type else provider

            if provider_type:
                by_provider_type.setdefault(provider_type, {"success": 0, "failed": 0})
                by_provider_type[provider_type][status] = by_provider_type[provider_type].get(status, 0) + 1

            if provider_key:
                by_provider.setdefault(provider_key, {"success": 0, "failed": 0})
                by_provider[provider_key][status] = by_provider[provider_key].get(status, 0) + 1

            if product_type:
                by_product_type.setdefault(product_type, {"success": 0, "failed": 0})
                by_product_type[product_type][status] = by_product_type[product_type].get(status, 0) + 1

        return {
            "by_provider_type": by_provider_type,
            "by_provider": by_provider,
            "by_product_type": by_product_type,
        }

    async def _collect_matches(
        self,
        renewed_cert: RenewedCert,
        provider_name: Optional[str] = None,
    ) -> tuple[List[Provider], Set[str], List[DeployMatchResult], List[Dict[str, str]]]:
        providers = self._build_cloud_providers()
        if provider_name:
            providers = [p for p in providers if p.name == provider_name]
            if not providers:
                LOGGER.warning("未找到指定 provider: %s，跳过匹配", provider_name)
                return [], set(), [], []

        cert_domains = build_cert_domains(renewed_cert.domain, renewed_cert.sans)
        cache_enabled, ttl_seconds, _ = self._binding_cache_settings()

        match_priority = {"exact": 3, "wildcard-single": 2, "wildcard-multi": 1}
        deduped_matches: Dict[tuple[str, str, str, str, str], DeployMatchResult] = {}
        skipped: List[Dict[str, str]] = []
        # 匹配阶段：仅用缓存做匹配，不连云 API；启用缓存时无缓存则视为该 provider 无绑定
        for provider in providers:
            provider_type = self._provider_type(provider)
            provider_name = self._provider_name(provider)
            if provider_type == "tencent":
                # 腾讯云匹配在 apply 阶段由 SSL cert_id 扫描完成，此处跳过缓存匹配。
                continue
            try:
                bindings = await self._get_provider_bindings(
                    provider=provider,
                    cache_enabled=cache_enabled,
                    ttl_seconds=ttl_seconds,
                    force_refresh=False,
                    cache_only=cache_enabled,
                )
            except Exception as exc:
                LOGGER.warning("[%s] 获取产品绑定失败，跳过: %s", provider.name, exc)
                skipped.append({
                    "provider": provider.name,
                    "product_type": "*",
                    "product_id": "*",
                    "reason": f"获取产品绑定失败: {exc}",
                })
                continue
            for binding in bindings:
                if not binding.domain:
                    skipped.append(
                        {
                            "provider": provider.name,
                            "product_type": binding.product_type,
                            "product_id": binding.product_id,
                            "reason": "binding.domain 为空，无法匹配",
                        }
                    )
                    continue
                match_type = match_domain(
                    cert_domains=cert_domains,
                    target_domain=binding.domain,
                )
                if not match_type:
                    skipped.append(
                        {
                            "provider": provider.name,
                            "product_type": binding.product_type,
                            "product_id": binding.product_id,
                            "reason": "域名不匹配",
                        }
                    )
                    continue
                candidate = DeployMatchResult(
                    provider_type=provider_type,
                    provider_name=provider_name,
                    product_type=binding.product_type,
                    product_id=binding.product_id,
                    domain=binding.domain,
                    match_type=match_type,
                    current_cert_id=binding.cert_id,
                    listener_port=binding.listener_port,
                    metadata=binding.metadata or {},
                )
                # 华为 ELB 同一监听器上 default 与 SNI 可能挂不同证书但证书 ID 相同（或需按域名拆行），
                # 仅靠 cert_id 去重会丢掉 SNI 命中，只执行 default 更新。
                cert_src = str((binding.metadata or {}).get("cert_source", "") or "")
                dom_key = normalize_domain(binding.domain) if binding.domain else ""
                dedupe_key = (
                    provider_type,
                    provider_name,
                    binding.product_type,
                    binding.product_id,
                    str(binding.cert_id or ""),
                    cert_src,
                    dom_key,
                )
                current = deduped_matches.get(dedupe_key)
                if current is None:
                    deduped_matches[dedupe_key] = candidate
                    continue
                if match_priority.get(candidate.match_type, 0) > match_priority.get(current.match_type, 0):
                    deduped_matches[dedupe_key] = candidate

        return providers, cert_domains, list(deduped_matches.values()), skipped

    async def plan_dry_run(
        self,
        renewed_cert: RenewedCert,
        provider_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        deploy_mode = self.conf.deploy.mode.strip().lower()
        if deploy_mode != "dry-run":
            raise RuntimeError(f"当前 deploy.mode={deploy_mode}，该入口仅支持 dry-run")

        providers, cert_domains, matches, skipped = await self._collect_matches(
            renewed_cert=renewed_cert,
            provider_name=provider_name,
        )
        if matches:
            examples = [
                f"{m.provider_type}:{m.provider_name}:{m.product_type}:{m.product_id}:{m.domain}({m.match_type})"
                for m in matches[:20]
            ]
            LOGGER.info(
                "deploy dry-run match detail: domain=%s matches=%d skipped=%d examples=%s",
                renewed_cert.domain,
                len(matches),
                len(skipped),
                examples,
            )
        else:
            LOGGER.info(
                "deploy dry-run match detail: domain=%s matches=0 skipped=%d",
                renewed_cert.domain,
                len(skipped),
            )
        nginx_plan = await self._plan_nginx_deploy(renewed_cert)
        nginx_targets = nginx_plan.get("targets", [])
        if not isinstance(nginx_targets, list):
            nginx_targets = []

        plan: Dict[str, Any] = {
            "mode": "dry-run",
            "cert": {
                "domain": renewed_cert.domain,
                "sans": sorted(list(cert_domains)),
                "expires_at": renewed_cert.expires_at.isoformat(),
                "issuer": renewed_cert.issuer,
            },
            "target_provider_count": len(providers),
            "planned_bind_actions": [asdict(m) for m in matches],
            "summary": self._summarize_matches(matches),
            "nginx_plan": nginx_plan,
            "nginx_target_count": len(nginx_targets),
            "skipped": skipped,
        }
        LOGGER.info(
            "multi-cloud dry-run plan generated: matched=%d nginx_targets=%d skipped=%d",
            len(matches),
            len(nginx_targets),
            len(skipped),
        )
        return plan

    async def _augment_tencent_ssl_matches(
        self,
        providers: List[Provider],
        renewed_cert: RenewedCert,
        cert_domains: Set[str],
        matches: List[DeployMatchResult],
        skipped: List[Dict[str, str]],
    ) -> tuple[
        List[DeployMatchResult],
        Dict[str, CertificateDeployer],
        Dict[str, Optional[str]],
    ]:
        """上传证书后通过 SSL Host 扫描生成腾讯部署目标（不依赖本地绑定缓存）。"""
        match_map: Dict[tuple[str, str, str, str, str], DeployMatchResult] = {}
        for match in matches:
            key = (
                match.provider_type,
                match.provider_name,
                match.product_type,
                match.product_id,
                normalize_domain(match.domain) if match.domain else "",
            )
            match_map[key] = match

        deployer_map: Dict[str, CertificateDeployer] = {}
        cert_id_map: Dict[str, Optional[str]] = {}
        scan_conf = self.conf.product_scan.tencent
        scan_flags = {
            "cdn_scan": bool(scan_conf.cdn_scan),
            "live_scan": bool(scan_conf.live_scan),
            "eo_scan": bool(scan_conf.eo_scan),
            "lb_scan": bool(scan_conf.lb_scan),
            "oss_scan": bool(scan_conf.oss_scan),
        }
        if not any(scan_flags.values()):
            return list(match_map.values()), deployer_map, cert_id_map

        enabled_products = {
            product_type
            for product_type, flag_name in (
                ("cdn", "cdn_scan"),
                ("live", "live_scan"),
                ("teo", "eo_scan"),
                ("clb", "lb_scan"),
                ("oss", "oss_scan"),
            )
            if scan_flags[flag_name]
        }

        for provider in providers:
            if self._provider_type(provider) != "tencent":
                continue
            discover = getattr(provider, "get_ssl_deploy_bindings", None)
            if not callable(discover):
                continue

            provider_key = self._provider_cache_key(provider)
            provider_name_value = self._provider_name(provider)
            deployer = self._build_deployer(provider)
            cert_id = await deployer.upload_certificate(
                renewed_cert.cert_pem,
                renewed_cert.key_pem,
                renewed_cert.domain,
            )
            if not cert_id:
                raise RuntimeError(f"[{provider.name}] 腾讯云证书上传后未返回 CertificateId")
            deployer_map[provider_key] = deployer
            cert_id_map[provider_key] = cert_id

            ssl_bindings = await discover(cert_id, **scan_flags)
            added = 0
            for binding in ssl_bindings:
                product_type = str(binding.product_type or "").strip().lower()
                product_id = str(binding.product_id or "").strip()
                metadata = binding.metadata or {}
                listener_scope = product_type == "clb" and bool(metadata.get("listener_scope"))
                if product_type not in enabled_products or not product_id:
                    skipped.append(
                        {
                            "provider": provider.name,
                            "product_type": product_type,
                            "product_id": product_id,
                            "reason": "SSL 扫描结果产品类型或 product_id 非法",
                        }
                    )
                    continue
                domain = str(binding.domain or "").strip()
                if not domain and not listener_scope:
                    skipped.append(
                        {
                            "provider": provider.name,
                            "product_type": product_type,
                            "product_id": product_id,
                            "reason": "SSL 扫描结果 domain 为空",
                        }
                    )
                    continue
                candidate = DeployMatchResult(
                    provider_type="tencent",
                    provider_name=provider_name_value,
                    product_type=product_type,
                    product_id=product_id,
                    domain=domain or renewed_cert.domain,
                    match_type="ssl-domain-match",
                    current_cert_id=binding.cert_id,
                    listener_port=binding.listener_port,
                    metadata=metadata,
                )
                key = (
                    candidate.provider_type,
                    candidate.provider_name,
                    candidate.product_type,
                    candidate.product_id,
                    normalize_domain(candidate.domain),
                )
                match_map[key] = candidate
                added += 1
            LOGGER.info(
                "[%s] 腾讯云证书上下文扫描合并完成: certificate_id=%s matched=%d",
                provider.name,
                cert_id,
                added,
            )

        return list(match_map.values()), deployer_map, cert_id_map

    async def apply_deploy(
        self,
        renewed_cert: RenewedCert,
        provider_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        deploy_mode = self.conf.deploy.mode.strip().lower()
        if deploy_mode != "apply":
            raise RuntimeError(f"当前 deploy.mode={deploy_mode}，该入口仅支持 apply")

        providers, cert_domains, matches, skipped = await self._collect_matches(
            renewed_cert=renewed_cert,
            provider_name=provider_name,
        )
        if matches:
            examples = [
                f"{m.provider_type}:{m.provider_name}:{m.product_type}:{m.product_id}:{m.domain}({m.match_type})"
                for m in matches[:50]
            ]
            LOGGER.info(
                "deploy apply match detail(before verify): domain=%s matches=%d skipped=%d examples=%s",
                renewed_cert.domain,
                len(matches),
                len(skipped),
                examples,
            )
        else:
            LOGGER.info(
                "deploy apply match detail(before verify): domain=%s matches=0 skipped=%d",
                renewed_cert.domain,
                len(skipped),
            )
        cache_enabled, ttl_seconds, verify_before_apply = self._binding_cache_settings()
        # 三态行为矩阵：
        # - cache_enabled=False：匹配阶段已是实时数据，verify 无意义，跳过（忽略 verify_before_apply）
        # - cache_enabled=True && verify_before_apply=False：完全以缓存为准，跳过 verify
        # - cache_enabled=True && verify_before_apply=True：拉实时做存在性校验
        if not cache_enabled:
            LOGGER.info(
                "deploy apply: binding_cache.enabled=false，匹配阶段已实时，跳过 verify"
            )
        elif not verify_before_apply:
            LOGGER.info(
                "deploy apply: verify_before_apply=false，完全以缓存为准，跳过 verify"
            )
        else:
            matches = await self._verify_matches_before_apply(
                providers=providers,
                matches=matches,
                skipped=skipped,
                cache_enabled=cache_enabled,
                ttl_seconds=ttl_seconds,
            )
            if matches:
                examples = [
                    f"{m.provider_type}:{m.provider_name}:{m.product_type}:{m.product_id}:{m.domain}({m.match_type})"
                    for m in matches[:50]
                ]
                LOGGER.info(
                    "deploy apply match detail(after verify): domain=%s matches=%d skipped=%d examples=%s",
                    renewed_cert.domain,
                    len(matches),
                    len(skipped),
                    examples,
                )
            else:
                LOGGER.info(
                    "deploy apply match detail(after verify): domain=%s matches=0 skipped=%d",
                    renewed_cert.domain,
                    len(skipped),
                )
        matches, deployer_map, uploaded_cert_ids = await self._augment_tencent_ssl_matches(
            providers=providers,
            renewed_cert=renewed_cert,
            cert_domains=cert_domains,
            matches=matches,
            skipped=skipped,
        )

        provider_map = {self._provider_cache_key(p): p for p in providers}
        deploy_state = DeployStateStore()

        # 只为有命中匹配的 provider 构建 deployer，避免未命中云厂商也被实例化（如仅匹配奇裕时不应建华为 deployer）
        matched_provider_keys = {f"{m.provider_type}:{m.provider_name}" for m in matches}
        for provider in providers:
            provider_cache_key = self._provider_cache_key(provider)
            if provider_cache_key not in matched_provider_keys:
                continue
            if provider_cache_key not in deployer_map:
                deployer_map[provider_cache_key] = self._build_deployer(provider)

        # 按 provider 分组：同一 provider 只上传一次证书到 CAS，再复用 cert_id 部署到该 provider 下所有目标
        matches_by_provider: Dict[str, List[DeployMatchResult]] = {}
        for match in matches:
            key = f"{match.provider_type}:{match.provider_name}"
            matches_by_provider.setdefault(key, []).append(match)

        bind_results: List[Dict[str, Any]] = []
        for provider_cache_key, provider_matches in matches_by_provider.items():
            provider = provider_map.get(provider_cache_key)
            if not provider:
                for match in provider_matches:
                    bind_results.append({
                        "provider_type": match.provider_type,
                        "provider": match.provider_name,
                        "product_type": match.product_type,
                        "product_id": match.product_id,
                        "domain": match.domain,
                        "status": "skipped",
                        "message": f"provider 不存在: {provider_cache_key}",
                    })
                continue
            deployer = deployer_map.get(provider_cache_key)
            if not deployer:
                for match in provider_matches:
                    bind_results.append({
                        "provider_type": match.provider_type,
                        "provider": match.provider_name,
                        "product_type": match.product_type,
                        "product_id": match.product_id,
                        "domain": match.domain,
                        "status": "skipped",
                        "message": f"Deployer 未初始化: {provider_cache_key}",
                    })
                continue

            cert_id = uploaded_cert_ids.get(provider_cache_key)
            if cert_id is None:
                prepare_targets = [
                    DeployTarget(
                        provider=item.provider_type,
                        product_type=item.product_type,
                        product_id=item.product_id,
                        domain=item.domain,
                        listener_port=item.listener_port,
                        metadata=item.metadata or {},
                    )
                    for item in provider_matches
                ]
                # 按实际命中产品准备证书；默认实现仍保持“同 provider 上传一次”的旧行为。
                cert_id = await deployer.prepare_certificate(
                    renewed_cert.cert_pem,
                    renewed_cert.key_pem,
                    renewed_cert.domain,
                    prepare_targets,
                )

            for match in provider_matches:
                target = DeployTarget(
                    provider=match.provider_type,
                    product_type=match.product_type,
                    product_id=match.product_id,
                    domain=match.domain,
                    listener_port=match.listener_port,
                    metadata={
                        "match_type": match.match_type,
                        "previous_cert_id": match.current_cert_id,
                        **(match.metadata or {}),
                    },
                )
                try:
                    result = await deployer.deploy(
                        cert_pem=renewed_cert.cert_pem,
                        key_pem=renewed_cert.key_pem,
                        target=target,
                        cert_id=cert_id,
                    )
                    status = "success" if result.success else "failed"
                    payload: Dict[str, Any] = {
                        "provider_type": match.provider_type,
                        "provider": match.provider_name,
                        "product_type": match.product_type,
                        "product_id": match.product_id,
                        "domain": match.domain,
                        "status": status,
                        "message": result.message,
                        "new_cert_id": result.cert_id,
                        "old_cert_id": match.current_cert_id,
                    }
                    bind_results.append(payload)

                    # 记录部署状态，供后续查询；通知由上层统一处理。
                    if status == "success":
                        deploy_state.record_success(
                            domain=match.domain,
                            provider=match.provider_name,
                            product_type=match.product_type,
                            product_id=match.product_id,
                            message=result.message or "",
                        )
                    else:
                        deploy_state.record_failure(
                            domain=match.domain,
                            provider=match.provider_name,
                            product_type=match.product_type,
                            product_id=match.product_id,
                            message=result.message or "",
                        )
                except Exception as exc:
                    error_msg = str(exc)
                    bind_results.append(
                        {
                            "provider_type": match.provider_type,
                            "provider": match.provider_name,
                            "product_type": match.product_type,
                            "product_id": match.product_id,
                            "domain": match.domain,
                            "status": "failed",
                            "error": error_msg,
                            "old_cert_id": match.current_cert_id,
                        }
                    )
                    deploy_state.record_failure(
                        domain=match.domain,
                        provider=match.provider_name,
                        product_type=match.product_type,
                        product_id=match.product_id,
                        message=error_msg,
                    )

        nginx_bind_results = await self._apply_nginx_deploy(renewed_cert)
        bind_results.extend(nginx_bind_results)

        if not bind_results:
            raise RuntimeError("未发现可部署的证书目标")

        failed = [item for item in bind_results if item.get("status") == "failed"]
        result: Dict[str, Any] = {
            "mode": "apply",
            "cert": {
                "domain": renewed_cert.domain,
                "sans": sorted(list(cert_domains)),
                "expires_at": renewed_cert.expires_at.isoformat(),
                "issuer": renewed_cert.issuer,
            },
            "bind_results": bind_results,
            "summary": self._summarize_bind_results(bind_results),
            "skipped": skipped,
            "failed_count": len(failed),
            "success_count": len([i for i in bind_results if i.get("status") == "success"]),
            "nginx_target_count": len(nginx_bind_results),
        }
        if failed:
            LOGGER.warning("证书部署完成但存在 %d 个目标失败", len(failed))
            raise RuntimeError(f"证书部署存在失败: {len(failed)} 个目标失败")
        return result
