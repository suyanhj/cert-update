"""Rocket.Chat Incoming Webhook 告警发送。"""
from __future__ import annotations

from typing import Any

import httpx

from app.utils.logger import get_logger

LOGGER = get_logger("app")

COLOR_ALARM = "#E74C3C"
COLOR_OK = "#2ECC71"


class RocketChatSendError(Exception):
    """Rocket.Chat 发送失败。"""


def attachment_color(msg: str) -> str:
    """按告警正文判断 attachment 颜色。"""
    lower = msg.lower()
    if "ok" in lower or "recovered" in lower or "恢复" in msg:
        return COLOR_OK
    return COLOR_ALARM


def send_rocketchat(target: dict[str, Any], msg: str) -> None:
    """
    通过 Rocket.Chat Incoming Webhook 发送告警。

    Args:
        target: 需含 webhook、channel、username、icon_emoji、title
        msg: 告警正文

    Raises:
        RocketChatSendError: HTTP 非 2xx
    """
    text = str(msg)
    payload = {
        "channel": target["channel"],
        "username": target["username"],
        "icon_emoji": target["icon_emoji"],
        "text": text.splitlines()[0],
        "attachments": [
            {
                "color": attachment_color(text),
                "title": target["title"],
                "text": text,
            }
        ],
    }
    LOGGER.info("开始发送 Rocket.Chat 告警 channel=%s", target["channel"])
    response = httpx.post(
        str(target["webhook"]),
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=15.0,
    )
    if response.status_code < 200 or response.status_code >= 300:
        LOGGER.error(
            "Rocket.Chat 发送失败 status=%s body=%s",
            response.status_code,
            response.text,
        )
        raise RocketChatSendError(
            f"Rocket.Chat 发送失败 status={response.status_code} body={response.text}"
        )
    LOGGER.info("Rocket.Chat 发送成功 status=%s", response.status_code)
