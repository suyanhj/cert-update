from __future__ import annotations

import asyncio
import os
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from dingtalkchatbot.chatbot import DingtalkChatbot

from app.config import load_raw_config
from app.schemas.alert import AlertEvent, AlertLevel
from app.utils.logger import get_logger
from app.utils.email_notify import build_email_message, send_email
from app.utils.rocketchat import send_rocketchat
from app.utils.time import TimeUtil

LOGGER = get_logger("app")



class AlertBatcher:
    """
    按 (source, level) 分组告警
    """
    def __init__(self):
        self._events: Dict[Tuple[str, AlertLevel], List[AlertEvent]] = defaultdict(list)

    def add(self, event: AlertEvent):
        key = (event.source, event.level)
        self._events[key].append(event)

    def clear(self):
        self._events.clear()

    def groups(self):
        return self._events.items()


    def build_title(self,source: str, level: AlertLevel, count: int) -> str:
        prefix = {
            AlertLevel.CRITICAL: "🚨",
            AlertLevel.WARNING: "⚠️",
            AlertLevel.INFO: "ℹ️",
        }[level]

        source_name_map = {
            "cert": "证书",
            "domain": "域名",
            "ecs": "ECS",
            "deploy": "部署",
        }
        src_name = source_name_map.get(source, source)
        return f"{prefix} {src_name}{level.value.upper()}告警（{count} 条）"

    def build_text(self,events: List[AlertEvent]) -> str:
        lines = []
        for e in events:
            target = e.domain or "-"
            line = f"- {target}: {e.message}"
            lines.append(line)
        return "\n".join(lines)


    async def flush_alerts(
            self,
            notifier: DingDingNotifier,
    ) -> None:
        for (source, level), events in self.groups():
            if not events:
                continue

            title = self.build_title(source, level, len(events))
            text = self.build_text(events)

            await notifier.notify(title, text)
            await asyncio.sleep(1.2)  # 强制限速，救命用


# =========================
# 统一通知器（保留历史类名）
# =========================

class DingDingNotifier:
    """
    钉钉、Rocket.Chat 与 SMTP 邮件统一通知封装。

    - 同步 SDK + asyncio.to_thread
    - 逐通道尝试并聚合错误
    - 保留历史类名以兼容现有调用方
    """

    LEVEL_ICON = {
        AlertLevel.INFO: "ℹ️",
        AlertLevel.WARNING: "⚠️",
        AlertLevel.CRITICAL: "🚨",
    }

    def __init__(
        self,
        webhook: str = "",
        secret: Optional[str] = None,
        pc_slide: bool = True,
        at_all: bool = True,
    ):
        self.conf = load_raw_config()
        self._at_all = at_all
        self._bot = self._init_dingtalk_bot(webhook, secret, pc_slide)
        self._rc = self._init_rocketchat_target()
        self._email = self._init_email_target()
        if self._bot is None and self._rc is None and self._email is None:
            LOGGER.warning("未启用任何通知通道，后续通知将被跳过")

    def _init_dingtalk_bot(self, webhook: str, secret: Optional[str], pc_slide: bool):
        """按配置启用钉钉机器人；enabled=false 时不创建。"""
        dd = self.conf.get("dingding")
        dd_enabled = True if not dd else dd.get("enabled") is not False
        webhook_url = str(webhook) if webhook else ""
        if not webhook_url and dd:
            webhook_url = str(dd.get("webhook") or "")
        secret_val = secret
        if secret_val is None and dd:
            secret_val = dd.get("secret")
        if not dd_enabled or not webhook_url:
            LOGGER.info("钉钉通知未启用")
            return None
        return DingtalkChatbot(
            webhook=webhook_url,
            secret=secret_val,
            pc_slide=pc_slide,
        )

    def _init_rocketchat_target(self) -> Optional[dict]:
        """从配置读取 Rocket.Chat 目标；未配置或关闭则返回 None。"""
        rc = self.conf.get("rocketchat")
        if not rc or rc.get("enabled") is False:
            return None
        webhook = rc.get("webhook")
        if not isinstance(webhook, str) or not webhook.strip():
            return None
        LOGGER.info("Rocket.Chat 通知已启用 channel=%s", rc.get("channel"))
        return {
            "webhook": webhook,
            "channel": rc.get("channel"),
            "username": rc.get("username"),
            "icon_emoji": rc.get("icon_emoji"),
            "title": rc.get("title"),
        }

    def _init_email_target(self) -> Optional[dict]:
        """从配置读取邮件目标并解析 SMTP 密码。"""
        email_conf = self.conf.get("email")
        if not email_conf or email_conf.get("enabled") is False:
            return None

        password = email_conf.get("password")
        password_env = str(email_conf.get("password_env") or "").strip()
        if password_env:
            password = os.environ.get(password_env)
            if not password:
                raise RuntimeError(f"SMTP 密码环境变量未设置: {password_env}")

        username = str(email_conf.get("username") or "").strip()
        if username and not password:
            raise RuntimeError("SMTP 认证已启用但未配置密码")

        target = {
            "host": str(email_conf.get("host") or "").strip(),
            "port": int(email_conf.get("port") or 587),
            "security": str(email_conf.get("security") or "starttls").strip().lower(),
            "username": username,
            "password": password,
            "from_address": str(email_conf.get("from_address") or "").strip(),
            "to": list(email_conf.get("to") or []),
            "cc": list(email_conf.get("cc") or []),
            "subject_prefix": str(email_conf.get("subject_prefix") or ""),
            "timeout_seconds": float(email_conf.get("timeout_seconds") or 10),
        }
        # 初始化时完成地址和邮件头校验，避免在首条告警发送时才暴露配置错误。
        build_email_message(target, "配置校验", "配置校验")
        LOGGER.info(
            "邮件通知已启用 host=%s port=%s to=%d cc=%d security=%s",
            target["host"],
            target["port"],
            len(target["to"]),
            len(target["cc"]),
            target["security"],
        )
        return target

    # ---------- 底层发送 ----------

    async def _send_text(self, text: str, email_subject: str = "CRT 通知") -> None:
        if self._bot is None and self._rc is None and self._email is None:
            return

        errors: List[Exception] = []
        if self._bot is not None:
            def _send():
                return self._bot.send_text(
                    msg=text,
                    is_at_all=self._at_all,
                )

            try:
                resp = await asyncio.to_thread(_send)
                errcode = resp.get("errcode")
                if errcode not in (0, "0", None):
                    LOGGER.error("钉钉返回失败: %s", resp)
                    raise RuntimeError(f"DingTalk API error: errcode={errcode}, resp={resp}")
            except Exception as exc:
                LOGGER.error("钉钉发送失败: %s", exc)
                errors.append(exc)

        if self._rc is not None:
            try:
                await asyncio.to_thread(send_rocketchat, self._rc, text)
            except Exception as exc:
                LOGGER.error("Rocket.Chat 发送失败: %s", exc)
                errors.append(exc)

        if self._email is not None:
            try:
                await asyncio.to_thread(send_email, self._email, email_subject, text)
            except Exception as exc:
                LOGGER.error("邮件发送失败: %s", exc)
                errors.append(exc)

        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise RuntimeError(f"通知发送失败: {errors}") from errors[0]




    # ---------- 通用入口 ----------

    async def notify(self, title: str, body: str) -> None:
        text = f"{title}\n\n{body}"
        await self._send_text(text, email_subject=title)

    # ---------- Event 格式化 ----------

    @staticmethod
    def _format_shanghai_time(value: datetime | str | None) -> str:
        if value in (None, ""):
            return ""
        return TimeUtil.to_local_tz(TimeUtil.parse(value))

    def _format_event(self, event: AlertEvent) -> tuple[str, str]:
        icon = self.LEVEL_ICON.get(event.level, "")
        title = f"{icon} {event.title}"

        lines = [event.message]

        if event.domain:
            lines.append(f"域名: {event.domain}")
        if event.provider:
            lines.append(f"平台: {event.provider}")
        if event.days_remaining is not None:
            lines.append(f"剩余天数: {event.days_remaining}")
        expires_at_text = self._format_shanghai_time(event.expires_at)
        if expires_at_text:
            lines.append(f"到期时间(上海): {expires_at_text}")
        timestamp_text = self._format_shanghai_time(event.timestamp)
        lines.append(f"时间(上海): {timestamp_text or event.timestamp}")


        return title, "\n".join(lines)

    # ---------- 单条告警 ----------

    async def notify_alert(self, event: AlertEvent) -> None:
        title, body = self._format_event(event)
        await self.notify(title, body)

    # ---------- 批量告警 ----------

    async def notify_batch(self, events: List[AlertEvent]) -> None:
        if not events:
            return

        groups = {
            AlertLevel.CRITICAL: [],
            AlertLevel.WARNING: [],
            AlertLevel.INFO: [],
        }

        for e in events:
            groups[e.level].append(e)

        lines: List[str] = []

        for level in (AlertLevel.CRITICAL, AlertLevel.WARNING, AlertLevel.INFO):
            items = groups[level]
            if not items:
                continue

            icon = self.LEVEL_ICON[level]
            lines.append(f"{icon} {level.name}")
            for e in items:
                lines.append(f"  • {e.title}: {e.message}")
            lines.append("")

        title = f"证书 / 域名告警汇总（{len(events)} 条）"
        await self.notify(title, "\n".join(lines).strip())

    # =========================
    # 业务快捷方法
    # =========================

    async def notify_cert_expiry(
        self,
        domain: str,
        days_remaining: int,
        providers: str,
        expires_at: datetime|str,
        level = AlertLevel.CRITICAL
    ) -> None:
        await self.notify_alert(
            AlertEvent(
                level=level,
                title="证书即将过期",
                message=f"在 {days_remaining} 天后过期",
                source="cert",
                domain=domain,
                # provider=", ".join(providers),
                provider=providers,
                days_remaining=days_remaining,
                expires_at=expires_at,
            )
        )

    async def notify_domain_expiry(
        self,
        domain: str,
        days_remaining: int,
        provider: str,
        expires_at: datetime|str,
        level = AlertLevel.CRITICAL
    ) -> None:
        await self.notify_alert(
            AlertEvent(
                level=level,
                title="域名即将过期",
                message=f"将在 {days_remaining} 天后过期",
                source="domain",
                domain=domain,
                provider=provider,
                days_remaining=days_remaining,
                expires_at=expires_at,
            )
        )


    async def notify_deploy_result(
        self,
        domain: str,
        provider: str,
        product_type: str,
        success: bool,
        message: str = "",
    ) -> None:
        if success:
            event = AlertEvent(
                level=AlertLevel.INFO,
                title="证书部署成功",
                message=f"已部署到 {provider} ({product_type})",
                source="deploy",
                domain=domain,
                provider=provider,
            )
        else:
            event = AlertEvent(
                level=AlertLevel.CRITICAL,
                title="证书部署失败",
                message=f"{provider} ({product_type}) 失败: {message}",
                source="deploy",
                domain=domain,
                provider=provider,
            )

        await self.notify_alert(event)

    async def notify_deploy_summary(
        self,
        domain: str,
        success: bool,
        message: str = "",
    ) -> None:
        """仅部署（不续签）结果汇总通知。"""
        if success:
            event = AlertEvent(
                level=AlertLevel.INFO,
                title="证书部署成功",
                message=message or "证书已成功部署",
                source="deploy",
                domain="",
            )
        else:
            event = AlertEvent(
                level=AlertLevel.CRITICAL,
                title="证书部署失败",
                message=message,
                source="deploy",
                domain=domain,
            )

        await self.notify_alert(event)

    async def notify_renew_result(
        self,
        domain: str,
        success: bool,
        message: str = "",
        new_expires_at: Optional[datetime] = None,
    ) -> None:
        if success:
            event = AlertEvent(
                level=AlertLevel.INFO,
                title="证书续期成功",
                # 正文完全由上游拼接（包含域名、部署目标等），这里不再额外添加文案
                message=message or "证书已成功续期",
                source="cert",
                # 域名在 message 中展示，避免重复一行「域名:」
                domain="",
                expires_at=new_expires_at,
            )
        else:
            event = AlertEvent(
                level=AlertLevel.CRITICAL,
                title="证书续期失败",
                message=message,
                source="cert",
                domain=domain,
            )

        await self.notify_alert(event)

    async def notify_issue_result(
        self,
        domain: str,
        success: bool,
        message: str = "",
        new_expires_at: Optional[datetime] = None,
        issued: bool = False,
    ) -> None:
        """发送首次签发及其后续部署的汇总结果。"""
        if success:
            event = AlertEvent(
                level=AlertLevel.INFO,
                title="证书签发成功",
                message=message or "证书已成功签发",
                source="cert",
                domain="",
                expires_at=new_expires_at,
            )
        else:
            event = AlertEvent(
                level=AlertLevel.CRITICAL,
                title="证书签发成功但部署失败" if issued else "证书签发失败",
                message=message,
                source="cert",
                domain=domain,
            )

        await self.notify_alert(event)
