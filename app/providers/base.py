import asyncio
import inspect
import re
from abc import ABC
from typing import List

from app.schemas.config import ProviderConfig
from app.schemas.provider import CloudProductBinding
from app.utils import http_client, logger, time
from app.utils.domain_match import normalize_domain


class Provider(ABC):
    """云平台提供器抽象基类。"""

    domain_roles: frozenset[str] = frozenset()

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config
        self.time = time.TimeUtil()
        self.logger = logger.get_logger("provider")
        self.http_client = http_client

    @property
    def name(self) -> str:
        return self.config.name

    @staticmethod
    def _extract_error_field(raw: str, field_name: str) -> str | None:
        match = re.search(rf"{re.escape(field_name)}\s*:\s*([^\r\n]+)", raw, flags=re.IGNORECASE)
        if not match:
            return None
        return match.group(1).strip().rstrip(".")

    @staticmethod
    def _build_exception_summary(exc: Exception) -> str:
        raw = str(exc) or exc.__class__.__name__
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        first_line = lines[0] if lines else exc.__class__.__name__

        code = (
            Provider._extract_error_field(raw, "Error Code")
            or Provider._extract_error_field(raw, "ErrorCode")
            or Provider._extract_error_field(raw, "Code")
        )
        reason = (
            Provider._extract_error_field(raw, "Message")
            or Provider._extract_error_field(raw, "Error Message")
        )

        parts = []
        if code:
            parts.append(f"code={code}")
        if reason:
            parts.append(f"reason={reason[:160]}")

        return ", ".join(parts) if parts else first_line

    @staticmethod
    def _is_cloud_service_not_enabled(exc: Exception) -> bool:
        """云端产品未开通（如 CdnServiceNotFound）视为无绑定，不算 API 逻辑错误。"""
        raw = str(exc) or ""
        code = (
            Provider._extract_error_field(raw, "Error Code")
            or Provider._extract_error_field(raw, "ErrorCode")
            or Provider._extract_error_field(raw, "Code")
            or ""
        )
        if code.endswith("ServiceNotFound") or code.endswith("ServiceNotActivated"):
            return True

        lowered = raw.lower()
        if "does not open" in lowered and "service" in lowered:
            return True
        if "service has not been opened" in lowered:
            return True
        if "未开通" in raw and "服务" in raw:
            return True
        return False

    def get_sub_extensions(self):
        """返回子域扩展映射（域名 -> 重定向目标）。"""
        return {
            item["domain"]: item["redirect_to"]
            for item in self.config.sub_extensions
            if item.get("redirect_to") and item.get("domain")
        }

    async def is_domain_provider(self, domain: str) -> bool:
        try:
            candidate = normalize_domain(str(domain))
        except ValueError:
            return False

        try:
            domains = await self.get_domain_list()
        except Exception:
            return False

        for item in domains or []:
            if not isinstance(item, dict):
                continue
            try:
                root = normalize_domain(str(item.get("domain", "")))
            except ValueError:
                continue
            if candidate == root or candidate.endswith(f".{root}"):
                return True
        return False

    async def _call_provider_api(
        self,
        op_name: str,
        func,
        *,
        default,
        retries: int = 0,
        retry_delay_seconds: float = 0.2,
        log_level: str = "warning",
        raise_on_error: bool = True,
    ):
        tag = f"[{self.name}] {op_name}"
        attempts = max(0, int(retries)) + 1

        for idx in range(attempts):
            try:
                result = func()
                if inspect.isawaitable(result):
                    result = await result
                return result
            except Exception as exc:
                short_err = self._build_exception_summary(exc)
                # 重试阶段只做 debug 级别日志，避免重复的 warning；最终失败时再统一输出一条。
                if idx < attempts - 1:
                    self.logger.debug(
                        "%s 失败，准备重试%d/%d: %s",
                        tag,
                        idx + 1,
                        attempts - 1,
                        short_err,
                        exc_info=True,
                    )
                    await asyncio.sleep(retry_delay_seconds * (idx + 1))
                    continue

                if self._is_cloud_service_not_enabled(exc):
                    self.logger.warning("%s 云端服务未开通，视为空结果: %s", tag, short_err)
                    return default

                log_fn = getattr(self.logger, log_level, self.logger.warning)
                log_fn("%s 失败: %s", tag, short_err)
                self.logger.debug("%s failed detail: %s", tag, exc, exc_info=True)
                if raise_on_error:
                    raise RuntimeError(f"{tag} 失败: {short_err}") from exc
                return default

        return default

    async def _request(self, domain):
        for scheme in ("http", "https"):
            try:
                resp = await self.http_client.fetch(url=f"{scheme}://{domain}")
            except Exception as e:
                self.logger.debug("Probe %s %s failed: %s", scheme, domain, e)
                if scheme == "http":
                    continue
                return "异常"
            if resp.status_code < 500:
                return "在工作"
            if scheme == "http":
                continue
            return "异常"

    def _full_domain(self, main_domain: str, sub_domain: str) -> str:
        """根据主域与子域拼出完整域名。"""
        if sub_domain == "@":
            return main_domain
        return f"{sub_domain}.{main_domain}"

    async def _probe_domain(
        self,
        domain: str,
        *,
        remark: str = "",
        sem: asyncio.Semaphore | None = None,
    ) -> dict:
        async def _run():
            resolved = domain

            if self.config.check_extensions:
                sub_extensions = self.get_sub_extensions()
                if sub_extensions and resolved in sub_extensions:
                    resolved = sub_extensions[resolved]

            is_wildcard = resolved.startswith("*.")
            if is_wildcard and not self.config.check_extensions:
                self.logger.debug("跳过通配符域名: %s", resolved)
                status = "通配符跳过"
            else:
                status = await self._request(resolved)

            return {
                "name": domain,
                "status": status,
                "remark": remark,
            }

        if sem:
            async with sem:
                return await _run()
        return await _run()

    async def get_domain_list(self) -> List[dict]:
        """返回该账号下的根域名列表。"""
        return []

    async def get_dns_records(self, domain: str) -> List[dict]:
        """返回指定域名下的 DNS 记录列表。"""
        return []

    async def get_cdn_bindings(self) -> List[CloudProductBinding]:
        """返回 CDN 产品绑定列表。"""
        return []

    async def get_live_bindings(self) -> List[CloudProductBinding]:
        """返回云直播等产品绑定列表。"""
        return []

    async def get_lb_bindings(self) -> List[CloudProductBinding]:
        """返回负载均衡产品绑定列表。"""
        return []

    async def get_oss_bindings(self) -> List[CloudProductBinding]:
        """返回 OSS/对象存储产品绑定列表。"""
        return []

    async def get_waf_bindings(self) -> List[CloudProductBinding]:
        """返回 WAF 防护域名证书绑定列表。"""
        return []

    async def get_ecs_instances(self) -> List[dict]:
        """返回 ECS 实例列表（含过期等元数据）。"""
        return []

    async def get_all_product_bindings(self) -> List[CloudProductBinding]:
        """返回所有产品绑定（CDN + LB + OSS 等）。"""
        bindings: List[CloudProductBinding] = []
        for method_name in (
            "get_cdn_bindings",
            "get_live_bindings",
            "get_lb_bindings",
            "get_oss_bindings",
            "get_waf_bindings",
        ):
            try:
                result = await getattr(self, method_name)()
                bindings.extend(result)
            except Exception as exc:
                self.logger.warning("[%s] %s 失败，跳过: %s", self.name, method_name, exc)
        return bindings
