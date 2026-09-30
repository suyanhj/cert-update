from __future__ import annotations

from typing import Any

from app.utils import store, time
from app.utils.dns_authority import DnsAuthorityResolver, normalize_nameservers
from app.utils.domain_match import normalize_domain


class DomainDiscoveryService:
    """负责域名发现、结果归一化、provider 映射落盘。"""

    def __init__(
        self,
        logger_obj,
        dns_overrides: dict[str, str] | None = None,
        authority_resolver: DnsAuthorityResolver | None = None,
    ) -> None:
        self.logger = logger_obj
        self.time = time.TimeUtil()
        self.dns_overrides = {
            normalize_domain(domain): str(provider_name or "").strip()
            for domain, provider_name in (dns_overrides or {}).items()
        }
        self.authority_resolver = authority_resolver or DnsAuthorityResolver()

    def _normalize_expires_at(self, expires_raw: Any) -> str:
        if expires_raw in (None, "", False):
            return "unknown"
        try:
            return self.time.to_local_tz(self.time.parse(expires_raw))
        except Exception:
            if isinstance(expires_raw, str):
                return expires_raw
            if hasattr(expires_raw, "isoformat"):
                return expires_raw.isoformat()
            return str(expires_raw)

    def _normalize_domain_group(self, provider: Any, item: dict) -> dict | None:
        if not isinstance(item, dict):
            return None
        try:
            domain = normalize_domain(str(item.get("domain", "") or ""))
        except ValueError:
            return None

        payload = dict(item)
        payload["domain"] = domain
        payload.setdefault("name", provider.name)
        payload.setdefault("provider", provider.config.type if provider.config else "")
        payload.setdefault("subs", [])

        expires_raw = payload.get("expires_at", "unknown")
        expires_at = self._normalize_expires_at(expires_raw)
        payload["expires_at"] = expires_at

        if "days" not in payload:
            if expires_at == "unknown":
                payload["days"] = 0
            else:
                try:
                    payload["days"] = self.time.remaining_days(self.time.parse(expires_at))
                except Exception:
                    payload["days"] = 0
        return payload

    @staticmethod
    def _has_registration_data(item: dict) -> bool:
        expires_at = str(item.get("expires_at", "") or "").strip().lower()
        registrant_org = str(
            item.get("registrant_org") or item.get("org") or ""
        ).strip()
        return bool(registrant_org or expires_at not in ("", "unknown", "unknow"))

    def _select_dns_candidate(
        self,
        domain: str,
        candidates: list[dict],
    ) -> dict | None:
        """按人工覆盖、公开 NS 和平台强证据选择实际 DNS 托管来源。"""
        if not candidates:
            return None

        candidate_names = [str(item.get("name") or "") for item in candidates]
        override_name = self.dns_overrides.get(domain)
        if override_name:
            matched = [
                item for item in candidates if str(item.get("name") or "") == override_name
            ]
            if len(matched) != 1:
                raise ValueError(
                    "domain_dns_overrides 未匹配唯一 DNS Provider: "
                    f"domain={domain} override={override_name} candidates={candidate_names}"
                )
            self.logger.info(
                "DNS 托管来源已由人工配置确认: domain=%s candidates=%s selected=%s method=override",
                domain,
                candidate_names,
                override_name,
            )
            return matched[0]

        if len(candidates) == 1:
            return candidates[0]

        public_nameservers: set[str] = set()
        lookup_failed = False
        try:
            public_nameservers = normalize_nameservers(
                self.authority_resolver.resolve_nameservers(domain)
            )
            if not public_nameservers:
                lookup_failed = True
                self.logger.warning("公网 NS 查询返回空结果: domain=%s", domain)
        except Exception as exc:
            lookup_failed = True
            self.logger.warning("公网 NS 查询失败: domain=%s error=%s", domain, exc)

        if public_nameservers:
            matched = [
                item
                for item in candidates
                if normalize_nameservers(item.get("assigned_nameservers"))
                == public_nameservers
                and public_nameservers
            ]
            if len(matched) == 1:
                selected_name = str(matched[0].get("name") or "")
                self.logger.info(
                    "DNS 托管来源已由公网 NS 确认: domain=%s candidates=%s selected=%s method=public_ns nameservers=%s",
                    domain,
                    candidate_names,
                    selected_name,
                    sorted(public_nameservers),
                )
                return matched[0]

        if lookup_failed:
            strong_candidates = [
                item for item in candidates if bool(item.get("dns_authoritative"))
            ]
            if len(strong_candidates) == 1:
                selected_name = str(strong_candidates[0].get("name") or "")
                self.logger.warning(
                    "DNS 托管来源使用平台强证据确认: domain=%s candidates=%s selected=%s method=provider_evidence",
                    domain,
                    candidate_names,
                    selected_name,
                )
                return strong_candidates[0]

        self.logger.error(
            "DNS 托管来源冲突且无法确认，不写入签发映射: domain=%s candidates=%s public_nameservers=%s",
            domain,
            candidate_names,
            sorted(public_nameservers),
        )
        return None

    def _merge_domain_groups(self, discovered: list[tuple[Any, dict]]) -> list[dict]:
        """按根域名融合注册商元数据与 DNS 托管数据。"""
        merged: dict[str, dict[str, Any]] = {}

        for provider, item in discovered:
            domain = item["domain"]
            roles = frozenset(
                item.get("domain_roles")
                or getattr(provider, "domain_roles", frozenset())
            )
            if not roles:
                self.logger.info(
                    "域名发现结果不参与注册/DNS 融合: provider=%s domain=%s",
                    provider.name,
                    domain,
                )
                continue

            state = merged.setdefault(
                domain,
                {"registration": None, "dns_candidates": []},
            )

            if "registration" in roles and self._has_registration_data(item):
                previous = state["registration"]
                if previous and previous.get("name") != item.get("name"):
                    self.logger.warning(
                        "域名注册来源冲突，按配置顺序使用后者: domain=%s candidates=%s,%s selected=%s",
                        domain,
                        previous.get("name"),
                        item.get("name"),
                        item.get("name"),
                    )
                state["registration"] = item

            if "dns" in roles:
                state["dns_candidates"].append(item)

        result: list[dict] = []
        for domain, state in merged.items():
            registration = state["registration"]
            dns = self._select_dns_candidate(domain, state["dns_candidates"])
            payload = dict(dns or registration or {})
            payload.pop("domain_roles", None)

            registrar_name = str((registration or {}).get("name") or "")
            registrar_provider = str((registration or {}).get("provider") or "")
            registrant_org = str(
                (registration or {}).get("registrant_org")
                or (registration or {}).get("org")
                or ""
            )
            dns_name = str((dns or {}).get("name") or "")
            dns_provider = str((dns or {}).get("provider") or "")
            try:
                remaining_days = int((registration or {}).get("days", 0) or 0)
            except (TypeError, ValueError):
                remaining_days = 0

            payload.update(
                {
                    "domain": domain,
                    "registrar_name": registrar_name,
                    "registrar_provider": registrar_provider,
                    "registrant_org": registrant_org,
                    "dns_name": dns_name,
                    "dns_provider": dns_provider,
                    # 兼容字段明确代表 DNS Provider。
                    "name": dns_name,
                    "provider": dns_provider,
                    "expires_at": (registration or {}).get("expires_at", "unknown"),
                    "days": remaining_days,
                    "org": registrant_org,
                    "subs": list((dns or {}).get("subs") or []),
                }
            )
            result.append(payload)

        return result

    async def discover_all(
        self,
        static_providers: list[Any],
        cloud_providers: list[Any],
        static_domain_count: int,
    ) -> list[dict]:
        self.logger.info("discovery_all START")
        self.logger.info("静态域名数量: %s", static_domain_count)

        discovered: list[tuple[Any, dict]] = []
        failures: list[str] = []
        for provider in [*static_providers, *cloud_providers]:
            try:
                domains = await provider.get_domain_list()
            except Exception as exc:
                provider_name = str(getattr(provider, "name", "unknown") or "unknown")
                failures.append(f"{provider_name}: {exc}")
                self.logger.error("域名发现失败: provider=%s error=%s", provider_name, exc)
                continue
            for item in domains or []:
                normalized = self._normalize_domain_group(provider, item)
                if normalized:
                    discovered.append((provider, normalized))

        domain_data = self._merge_domain_groups(discovered)

        self.logger.info("云服务发现 %s 个域名", len(domain_data))
        if failures:
            raise RuntimeError(f"域名发现部分失败: {'; '.join(failures)}")

        store.DomainProviderStore().save(domain_data)
        return domain_data
