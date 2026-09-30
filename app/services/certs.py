from app.utils import tls_probe,logger,store,notifier
from app.config import load_raw_config
from app.schemas.alert import AlertLevel,AlertEvent


class Certificates:
    """证书管理"""
    def __init__(self):
        self.config = load_raw_config()
        self._tls_probe = tls_probe
        self.logger = logger.get_logger("app")
        self._sub_extensions = self.get_sub_extensions()
        self._domain_store = store.DomainProviderStore()
        self._crt_state_store = store.AlertStateStore('data/alert_crt_state.json')
        self.notify_dd = notifier.DingDingNotifier()
        self.alert_batch = notifier.AlertBatcher()
        self._pending_alert_keys: set[str] = set()

    def get_sub_extensions(self):
        return {sub['domain']: sub['redirect_to']
            for item in self.config.providers
                if item.check_extensions
                    for sub in item.sub_extensions
        }



    def get_domains(self,domains):
        """获取所有域名"""
        data = set()
        for item in domains:
            subs = item.get("subs") or []
            if not subs:
                domain = item.get("domain")
                if domain:
                    subs = [{"name": domain, "status": "在工作", "remark": ""}]
            for sub in subs:
                _ = sub.get("name") or sub.get("full_domain")
                status = sub.get("status", "在工作")
                if not _:
                    continue
                if status == "在工作":
                    if self._sub_extensions and _ in self._sub_extensions:
                        _ = self._sub_extensions[_]
                    data.add( _)

        self.logger.info("待解析证书: %d", len(data))
        return data

    async def cert_alert(self, cert: dict):
        domain = cert['domain']
        days = int(cert['days'])

        enabled = self.config.alert.cert.enabled
        if not enabled:
            return

        if domain in self.config.whitelist:
            self.logger.debug("%s 在白名单中，跳过证书提醒", domain)
            return

        warn_days = int(self.config.alert.cert.cert_warn_days)
        exp_days = int(self.config.alert.cert.cert_expiry_days)

        level = None
        if days <= warn_days:
            level = AlertLevel.CRITICAL
        elif days <= exp_days:
            level = AlertLevel.WARNING

        if not level:
            self._crt_state_store.recover(domain)
            return

        if self._crt_state_store.is_firing(domain):
            return

        try:
            self.alert_batch.add(AlertEvent(
                level=level,
                title="证书即将过期",
                message=f"证书将在 {cert['days']} 天后过期",
                source="cert",
                domain=cert["domain"],
                provider=cert["provider"],
                expires_at=cert["exp"],
                days_remaining=cert["days"],
            ))
            self._pending_alert_keys.add(domain)
        except Exception as e:
            self.logger.error("%s 发生证书过期告警失败: %s", domain, e, exc_info=True)



    async def get_certificate_list(self,domains = None):
        """证书列表"""
        data = []
        try:
            _ = await self._tls_probe.probe_domains_batch(self.get_domains( domains))
            self.logger.info("已获取证书: %d", len(_))
            seen_domains: set[str] = set()
            for k,v in _.items():
                _dict = v
                _dict['domain'] =  k
                seen_domains.add(k)
                _dict['provider'] =  self._domain_store.resolve(k)
                data.append(_dict)
                await self.cert_alert(_dict)
            try:
                await self.alert_batch.flush_alerts(self.notify_dd)
                for key in self._pending_alert_keys:
                    self._crt_state_store.fire(key)
                self.alert_batch.clear()
                self._pending_alert_keys.clear()
            except Exception as e:
                self.logger.error("flush_alerts failed: %s", e, exc_info=True)
                self.alert_batch.clear()
                self._pending_alert_keys.clear()

            # 对于本轮未出现的证书域名，统一标记为 NORMAL，避免长期停留在 FIRING
            try:
                current_states = self._crt_state_store.get_all()
                for key in current_states.keys():
                    if key not in seen_domains:
                        self._crt_state_store.recover(key)
            except Exception as e:
                self.logger.error("同步证书告警状态失败: %s", e, exc_info=True)
        except Exception as e:
                self.logger.error("get_certificate_list failed: %s", e, exc_info=True)
                raise
        return data
