from __future__ import annotations

from app.schemas.alert import AlertEvent, AlertLevel


class DomainAlertService:
    """负责域名过期告警判定与发送。"""

    def __init__(
        self,
        configs,
        logger_obj,
        notify_dd,
        state_store,
        alert_batch,
    ) -> None:
        self.configs = configs
        self.logger = logger_obj
        self.notify_dd = notify_dd
        self._domain_state_store = state_store
        self.alert_batch = alert_batch

    async def send_domain_alerts(self, domains):
        enabled = self.configs.alert.domain.enabled if isinstance(self.configs.alert.domain.enabled, bool) else True
        if not enabled:
            return

        seen_keys: set[str] = set()
        pending_alert_keys: set[str] = set()

        for domain_info in domains:
            domain = domain_info["domain"]
            expires_at = domain_info["expires_at"]
            days = domain_info["days"]
            seen_keys.add(domain)
            # 域名到期属于注册商信息，不能使用 DNS 托管账号代替。
            account_name = (
                str(domain_info.get("registrar_name") or "").strip()
                or str(domain_info.get("registrar_provider") or "").strip()
                or str(domain_info.get("provider_name") or "").strip()
                or str(domain_info.get("name") or "").strip()
                or str(domain_info.get("provider") or "Unknown").strip()
            )

            if domain in self.configs.whitelist:
                self.logger.debug("%s 在白名单中，跳过证书提醒", domain)
                continue

            if expires_at == "unknown":
                self.logger.debug("%s 静态域名，未设置过期时间，跳过证书提醒", domain)
                continue

            warn_days = self.configs.alert.domain.domain_warn_days if isinstance(self.configs.alert.domain.domain_warn_days, int) else 15
            exp_days = self.configs.alert.domain.domain_expiry_days if isinstance(self.configs.alert.domain.domain_expiry_days, int) else 28

            level = None
            if days <= warn_days:
                level = AlertLevel.CRITICAL
            elif days <= exp_days:
                level = AlertLevel.WARNING

            if not level:
                self._domain_state_store.recover(domain)
                continue

            if self._domain_state_store.is_firing(domain):
                continue

            self.alert_batch.add(
                AlertEvent(
                    level=level,
                    title="域名即将过期",
                    message=f"[{account_name}] 域名将在 {days} 天后过期",
                    source="domain",
                    domain=domain,
                    provider=account_name,
                    expires_at=expires_at,
                    days_remaining=days,
                )
            )
            pending_alert_keys.add(domain)

        # 对于本轮未出现的域名，统一标记为 NORMAL，避免长期停留在 FIRING
        try:
            current_states = self._domain_state_store.get_all()
            for key in current_states.keys():
                if key not in seen_keys:
                    self._domain_state_store.recover(key)
        except Exception as exc:
            self.logger.error("同步域名告警状态失败: %s", exc, exc_info=True)

        try:
            await self.alert_batch.flush_alerts(self.notify_dd)
            for key in pending_alert_keys:
                self._domain_state_store.fire(key)
        except Exception as e:
            self.logger.error("发送告警失败: %s", e)
        finally:
            self.alert_batch.clear()
