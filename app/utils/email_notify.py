"""SMTP 邮件通知发送工具。"""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Any


def _validate_header(name: str, value: str) -> str:
    text = str(value or "").strip()
    if not text or "\r" in text or "\n" in text:
        raise ValueError(f"{name} 不能为空且不能包含换行")
    return text


def _validate_address(name: str, value: str) -> tuple[str, str]:
    text = _validate_header(name, value)
    _, address = parseaddr(text)
    if not address or "@" not in address or address.startswith("@") or address.endswith("@"):
        raise ValueError(f"{name} 不是有效邮件地址")
    return text, address


def _normalize_address_list(name: str, values: Any) -> tuple[list[str], list[str]]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple)):
        raise ValueError(f"{name} 必须是邮件地址列表")

    headers: list[str] = []
    envelopes: list[str] = []
    for value in values:
        header, envelope = _validate_address(name, str(value or ""))
        headers.append(header)
        envelopes.append(envelope)
    return headers, envelopes


def build_email_message(target: dict[str, Any], title: str, body: str) -> tuple[EmailMessage, list[str]]:
    """校验邮件目标并构造 UTF-8 纯文本邮件。"""
    from_header, from_envelope = _validate_address("email.from_address", target.get("from_address", ""))
    to_headers, to_envelopes = _normalize_address_list("email.to", target.get("to", []))
    if not to_headers:
        raise ValueError("email.to 至少需要一个收件人")
    cc_headers, cc_envelopes = _normalize_address_list("email.cc", target.get("cc", []))

    subject_prefix = str(target.get("subject_prefix") or "")
    if "\r" in subject_prefix or "\n" in subject_prefix:
        raise ValueError("email.subject_prefix 不能包含换行")
    subject = _validate_header("邮件主题", f"{subject_prefix}{title}")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = from_header
    message["To"] = ", ".join(to_headers)
    if cc_headers:
        message["Cc"] = ", ".join(cc_headers)
    message.set_content(str(body), subtype="plain", charset="utf-8")
    return message, [*to_envelopes, *cc_envelopes]


def send_email(target: dict[str, Any], title: str, body: str) -> None:
    """根据目标配置连接 SMTP 并发送一封邮件。"""
    host = str(target.get("host") or "").strip()
    if not host:
        raise ValueError("email.host 不能为空")
    port = int(target.get("port") or 0)
    if not 1 <= port <= 65535:
        raise ValueError("email.port 必须在 1 到 65535 之间")
    timeout = float(target.get("timeout_seconds") or 0)
    if timeout <= 0:
        raise ValueError("email.timeout_seconds 必须大于 0")

    security = str(target.get("security") or "starttls").strip().lower()
    if security not in {"starttls", "ssl", "none"}:
        raise ValueError(f"不支持的邮件安全模式: {security}")

    message, recipients = build_email_message(target, title, body)
    _, from_envelope = _validate_address("email.from_address", target.get("from_address", ""))
    username = str(target.get("username") or "").strip()
    password = target.get("password")
    if username and not password:
        raise ValueError("SMTP 认证已启用但密码为空")

    context = ssl.create_default_context()
    client_cls = smtplib.SMTP_SSL if security == "ssl" else smtplib.SMTP
    client_kwargs: dict[str, Any] = {
        "host": host,
        "port": port,
        "timeout": timeout,
    }
    if security == "ssl":
        client_kwargs["context"] = context

    with client_cls(**client_kwargs) as client:
        if security != "ssl":
            client.ehlo()
        if security == "starttls":
            client.starttls(context=context)
            client.ehlo()
        if username:
            client.login(username, str(password))
        client.send_message(
            message,
            from_addr=from_envelope,
            to_addrs=recipients,
        )
