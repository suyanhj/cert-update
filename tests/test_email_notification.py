"""SMTP 邮件通知测试。"""

import asyncio

import pytest
from pydantic import ValidationError
from box import Box

from app.schemas.config import EmailConfig
from app.utils import email_notify


def _target(**overrides):
    target = {
        "host": "smtp.example.com",
        "port": 587,
        "security": "starttls",
        "username": "mailer@example.com",
        "password": "secret-value",
        "from_address": "CRT 监控 <mailer@example.com>",
        "to": ["ops@example.com"],
        "cc": ["owner@example.com"],
        "subject_prefix": "[CRT] ",
        "timeout_seconds": 8,
    }
    target.update(overrides)
    return target


class FakeSMTP:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls = []
        self.message = None
        self.from_addr = None
        self.to_addrs = None
        self.__class__.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.calls.append(("close",))

    def ehlo(self):
        self.calls.append(("ehlo",))

    def starttls(self, context):
        self.calls.append(("starttls", context))

    def login(self, username, password):
        self.calls.append(("login", username, password))

    def send_message(self, message, from_addr, to_addrs):
        self.calls.append(("send_message",))
        self.message = message
        self.from_addr = from_addr
        self.to_addrs = to_addrs


class FakeSMTPSSL(FakeSMTP):
    instances = []


def test_email_config_normalizes_recipient_and_validates_auth():
    assert EmailConfig(enabled=False).enabled is False

    config = EmailConfig(
        host="smtp.example.com",
        from_address="mailer@example.com",
        to="ops@example.com",
        username="mailer@example.com",
        password_env="SMTP_PASSWORD",
    )
    assert config.to == ["ops@example.com"]
    assert config.cc == []
    assert config.security == "starttls"

    with pytest.raises(ValidationError, match="username 启用认证"):
        EmailConfig(
            host="smtp.example.com",
            from_address="mailer@example.com",
            to=["ops@example.com"],
            username="mailer@example.com",
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"to": []},
        {"from_address": "bad-address"},
        {"to": ["ops@example.com\nBcc: attacker@example.com"]},
        {"subject_prefix": "[CRT]\nBcc: attacker@example.com"},
    ],
)
def test_email_config_rejects_invalid_address_or_header(overrides):
    values = {
        "host": "smtp.example.com",
        "from_address": "mailer@example.com",
        "to": ["ops@example.com"],
    }
    values.update(overrides)
    with pytest.raises(ValidationError):
        EmailConfig(**values)


def test_send_email_uses_starttls_auth_and_all_recipients(monkeypatch):
    FakeSMTP.instances.clear()
    monkeypatch.setattr(email_notify.smtplib, "SMTP", FakeSMTP)

    email_notify.send_email(_target(), "证书即将过期", "域名: api.example.com")

    client = FakeSMTP.instances[-1]
    assert client.kwargs == {"host": "smtp.example.com", "port": 587, "timeout": 8.0}
    assert [call[0] for call in client.calls] == [
        "ehlo",
        "starttls",
        "ehlo",
        "login",
        "send_message",
        "close",
    ]
    assert ("login", "mailer@example.com", "secret-value") in client.calls
    assert client.from_addr == "mailer@example.com"
    assert client.to_addrs == ["ops@example.com", "owner@example.com"]
    assert client.message["Subject"] == "[CRT] 证书即将过期"
    assert "域名: api.example.com" in client.message.get_content()


def test_send_email_uses_ssl_without_login_when_username_empty(monkeypatch):
    FakeSMTPSSL.instances.clear()
    monkeypatch.setattr(email_notify.smtplib, "SMTP_SSL", FakeSMTPSSL)

    email_notify.send_email(
        _target(
            port=465,
            security="ssl",
            username="",
            password=None,
            cc=[],
        ),
        "测试通知",
        "正文",
    )

    client = FakeSMTPSSL.instances[-1]
    assert client.kwargs["port"] == 465
    assert "context" in client.kwargs
    assert [call[0] for call in client.calls] == ["send_message", "close"]


def test_send_email_none_security_skips_tls_and_login(monkeypatch):
    FakeSMTP.instances.clear()
    monkeypatch.setattr(email_notify.smtplib, "SMTP", FakeSMTP)

    email_notify.send_email(
        _target(
            port=25,
            security="none",
            username="",
            password=None,
            cc=[],
        ),
        "内网通知",
        "正文",
    )

    client = FakeSMTP.instances[-1]
    assert [call[0] for call in client.calls] == ["ehlo", "send_message", "close"]


def test_notifier_uses_password_env_and_supports_email_only(monkeypatch):
    from app.utils import notifier as notifier_mod

    sent = []
    monkeypatch.setenv("CRT_SMTP_PASSWORD", "env-secret")
    monkeypatch.setattr(
        notifier_mod,
        "load_raw_config",
        lambda: {
            "email": {
                **_target(password="yaml-secret"),
                "enabled": True,
                "password_env": "CRT_SMTP_PASSWORD",
            }
        },
    )
    monkeypatch.setattr(
        notifier_mod,
        "send_email",
        lambda target, title, body: sent.append((target, title, body)),
    )

    notifier = notifier_mod.DingDingNotifier(at_all=False)
    asyncio.run(notifier.notify("部署成功", "api.example.com"))

    assert notifier._email["password"] == "env-secret"
    assert len(sent) == 1
    assert sent[0][1] == "部署成功"
    assert sent[0][2] == "部署成功\n\napi.example.com"


def test_notifier_missing_password_env_fails_fast(monkeypatch):
    from app.utils import notifier as notifier_mod

    monkeypatch.delenv("MISSING_SMTP_PASSWORD", raising=False)
    monkeypatch.setattr(
        notifier_mod,
        "load_raw_config",
        lambda: {
            "email": {
                **_target(password=None),
                "enabled": True,
                "password_env": "MISSING_SMTP_PASSWORD",
            }
        },
    )

    with pytest.raises(RuntimeError, match="MISSING_SMTP_PASSWORD"):
        notifier_mod.DingDingNotifier()


def test_notifier_attempts_email_after_dingtalk_failure(monkeypatch):
    from app.utils import notifier as notifier_mod

    sent = []
    monkeypatch.setattr(
        notifier_mod,
        "load_raw_config",
        lambda: {"email": {**_target(), "enabled": True}},
    )
    monkeypatch.setattr(
        notifier_mod,
        "send_email",
        lambda target, title, body: sent.append((title, body)),
    )

    class FailingBot:
        def send_text(self, msg, is_at_all):
            raise RuntimeError("钉钉失败")

    notifier = notifier_mod.DingDingNotifier()
    notifier._bot = FailingBot()

    with pytest.raises(RuntimeError, match="钉钉失败"):
        asyncio.run(notifier.notify("证书告警", "正文"))
    assert sent == [("证书告警", "证书告警\n\n正文")]


def test_domain_and_ecs_services_do_not_require_dingtalk_config(monkeypatch):
    from app.services import domains as domains_mod
    from app.services import ecs as ecs_mod

    config = Box(
        {
            "providers": [],
            "domain_dns_overrides": {},
            "product_scan": {},
            "alert": {
                "domain": {"enabled": False},
                "ecs": {"enabled": False},
            },
        },
        default_box=True,
    )
    notifier_calls = []

    def build_notifier(*args, **kwargs):
        notifier_calls.append((args, kwargs))
        return object()

    for module in (domains_mod, ecs_mod):
        monkeypatch.setattr(module, "load_raw_config", lambda: config)
        monkeypatch.setattr(module.notifier, "DingDingNotifier", build_notifier)
        monkeypatch.setattr(module.store, "AlertStateStore", lambda *args, **kwargs: object())
        monkeypatch.setattr(module.notifier, "AlertBatcher", lambda: object())

    domains_mod.Domains()
    ecs_mod.Ecs()

    assert notifier_calls == [((), {}), ((), {})]
