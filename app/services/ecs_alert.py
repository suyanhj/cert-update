from __future__ import annotations

from collections import defaultdict

from app.schemas.alert import AlertEvent, AlertLevel


class EcsAlertService:
    """负责 ECS 到期告警判定与发送。"""

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
        self._ecs_state_store = state_store
        self.alert_batch = alert_batch

    @staticmethod
    def _platform_name(provider_type: str) -> str:
        mapping = {
            "aliyun": "阿里云",
            "huawei": "华为云",
            "tencent": "腾讯云",
            "volcengine": "火山云",
            "qiniu": "七牛云",
        }
        return mapping.get(provider_type, provider_type)

    async def send_ecs_alerts(self, instances):
        if not self.configs.alert.ecs.enabled:
            return

        warn_days = self.configs.alert.ecs.ecs_warn_days
        exp_days = self.configs.alert.ecs.ecs_expiry_days
        grouped_events = defaultdict(list)
        grouped_keys = defaultdict(list)
        seen_keys: set[str] = set()

        for instance_info in instances:
            provider_type = str(instance_info.get("provider", "") or "").lower()
            # 暂时仅对阿里云 ECS 做到期告警，华为等其它云返回空结果
            if provider_type != "aliyun":
                continue

            # 仅对未开自动续费的实例做告警判断
            if instance_info.get("auto_renewal") is True:
                continue
            renewal_status = str(instance_info.get("renewal_status", "") or "").strip()
            if renewal_status == "AutoRenewal":
                continue

            instance_id = str(instance_info.get("instance_id", "") or "").strip()
            if not instance_id:
                continue

            provider_name = str(instance_info.get("provider_name", "") or "aliyun")
            alert_key = f"{provider_type}:{provider_name}:{instance_id}"
            seen_keys.add(alert_key)
            expires_at = instance_info.get("expires_at", "")
            try:
                days_raw = instance_info.get("days")
                if days_raw in (None, ""):
                    raise ValueError("missing expiry days")
                days = int(days_raw)
            except (TypeError, ValueError):
                self.logger.warning(
                    "跳过到期时间未知的 ECS 实例: provider=%s instance_id=%s expires_at=%s",
                    provider_type,
                    instance_id,
                    expires_at,
                )
                continue

            level = None
            if days <= warn_days:
                level = AlertLevel.CRITICAL
            elif days <= exp_days:
                level = AlertLevel.WARNING

            kind = str(instance_info.get("instance_kind", "") or "").strip().lower()
            host_label = "轻量应用服务器" if kind == "swas" else "ECS主机"
            if not level:
                self._ecs_state_store.recover(alert_key)
                continue

            if self._ecs_state_store.is_firing(alert_key):
                continue

            instance_name = str(instance_info.get("instance_name", "") or instance_id)
            region = str(instance_info.get("region", "") or "")
            platform_name = self._platform_name(provider_type)
            # 展示格式：id: 云 提示 区域
            message = f"{platform_name}/{provider_name} {host_label}将在 {days} 天后到期 {region}"

            grouped_events[(provider_type, provider_name, level)].append(
                AlertEvent(
                    level=level,
                    title=f"{host_label}即将到期",
                    message=message,
                    source="ecs",
                    domain=instance_id,
                    provider=provider_name,
                    expires_at=expires_at,
                    days_remaining=days,
                )
            )
            grouped_keys[(provider_type, provider_name, level)].append(alert_key)

        # 对于本轮未出现的 ECS 告警键，统一标记为 NORMAL，避免资源下线后长期停留在 FIRING
        try:
            current_states = self._ecs_state_store.get_all()
            for key in current_states.keys():
                if key not in seen_keys:
                    self._ecs_state_store.recover(key)
        except Exception as exc:
            self.logger.error("同步 ECS 告警状态失败: %s", exc, exc_info=True)

        for (provider_type, provider_name, level), events in grouped_events.items():
            if not events:
                continue
            try:
                for event in events:
                    self.alert_batch.add(event)
                await self.alert_batch.flush_alerts(self.notify_dd)
                for key in grouped_keys[(provider_type, provider_name, level)]:
                    self._ecs_state_store.fire(key)
            except Exception as exc:
                self.logger.error(
                    "发送ECS批量告警失败: platform=%s account=%s level=%s err=%s",
                    provider_type,
                    provider_name,
                    level.value,
                    exc,
                )
            finally:
                self.alert_batch.clear()
