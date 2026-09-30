import asyncio
from datetime import datetime, timezone

from box import Box

from app.schemas.alert import AlertEvent, AlertLevel


class _FixedTimeUtil:
    UTC = timezone.utc

    @staticmethod
    def now_utc():
        return datetime(2026, 3, 16, 8, 0, 0, tzinfo=timezone.utc)

    @staticmethod
    def parse(value):
        # 简化：直接用 datetime.fromisoformat 解析
        return datetime.fromisoformat(str(value))

    @staticmethod
    def to_local_tz(dt):
        # 测试中不关心具体时区换算，只要有非空字符串即可
        if isinstance(dt, datetime):
            return dt.replace(tzinfo=None).isoformat(sep=" ")
        return str(dt)


def test_dingding_notifier_format_event(monkeypatch):
    """_format_event 应正确拼接标题和正文字段。"""
    from app.utils import notifier as notifier_mod

    # 固定时间工具（须 patch notifier 内已绑定的 TimeUtil，仅改 time 模块无效）
    monkeypatch.setattr(notifier_mod, "TimeUtil", _FixedTimeUtil)

    n = notifier_mod.DingDingNotifier(webhook="x", secret="y", pc_slide=True, at_all=False)

    event = AlertEvent(
        level=AlertLevel.WARNING,
        title="测试告警",
        message="正文内容",
        source="cert",
        domain="api.example.com",
        provider="aliyun",
        days_remaining=5,
        expires_at=_FixedTimeUtil.now_utc().isoformat(),
        timestamp=_FixedTimeUtil.now_utc().isoformat(),
    )

    title, body = n._format_event(event)
    # 标题前缀应为⚠️
    assert title.startswith("⚠️")
    assert "测试告警" in title

    lines = body.splitlines()
    assert lines[0] == "正文内容"
    assert "域名: api.example.com" in lines[1]
    assert "平台: aliyun" in lines[2]
    assert "剩余天数: 5" in lines[3]
    # 后两行为到期时间和当前时间，断言存在关键前缀即可
    assert lines[4].startswith("到期时间(上海):")
    assert lines[5].startswith("时间(上海):")


def test_dingding_notifier_cert_and_domain_expiry_helpers(monkeypatch):
    """notify_cert_expiry/notify_domain_expiry 应构造出带有正确 title/source/message 的 AlertEvent。"""
    from app.utils import notifier as notifier_mod

    captured = []

    class FakeNotifier(notifier_mod.DingDingNotifier):
        async def notify_alert(self, event: AlertEvent) -> None:
            captured.append(event)

    # 使用 FakeNotifier 覆盖原类
    n = FakeNotifier(webhook="", secret="", pc_slide=True, at_all=False)

    # 证书过期
    asyncio.run(
        n.notify_cert_expiry(
            domain="api.example.com",
            days_remaining=3,
            providers="aliyun",
            expires_at="2026-01-01T00:00:00",
            level=AlertLevel.CRITICAL,
        )
    )
    # 域名过期
    asyncio.run(
        n.notify_domain_expiry(
            domain="www.example.com",
            days_remaining=10,
            provider="huawei",
            expires_at="2026-02-02T00:00:00",
            level=AlertLevel.WARNING,
        )
    )

    assert len(captured) == 2
    cert_event, domain_event = captured

    # 证书
    assert cert_event.source == "cert"
    assert cert_event.title == "证书即将过期"
    assert "在 3 天后过期" in cert_event.message
    assert cert_event.domain == "api.example.com"
    assert cert_event.provider == "aliyun"

    # 域名
    assert domain_event.source == "domain"
    assert domain_event.title == "域名即将过期"
    assert "将在 10 天后过期" in domain_event.message
    assert domain_event.domain == "www.example.com"
    assert domain_event.provider == "huawei"


def test_dingding_notifier_renew_result_formats_success_and_failure(monkeypatch):
    """notify_renew_result 成功时应使用上游传入 message，失败时用失败文案并保留 domain。"""
    from app.utils import notifier as notifier_mod

    events = []

    class FakeNotifier(notifier_mod.DingDingNotifier):
        async def notify_alert(self, event: AlertEvent) -> None:
            events.append(event)

    n = FakeNotifier(webhook="", secret="", pc_slide=True, at_all=False)

    # 成功
    asyncio.run(
        n.notify_renew_result(
            domain="vpn.example.com",
            success=True,
            message="域名: vpn.example.com\n部署目标:\n  奇裕 阿里云 cdn",
            new_expires_at="2026-01-01T00:00:00",
        )
    )
    # 失败
    asyncio.run(
    n.notify_renew_result(
        domain="vpn.example.com",
        success=False,
        message="续期失败",
        new_expires_at=None,
    )
    )

    assert len(events) == 2
    ok, bad = events

    assert ok.level == AlertLevel.INFO
    assert ok.title == "证书续期成功"
    # 成功时域名在 message 中展示，domain 置空
    assert ok.domain == ""
    assert "部署目标" in ok.message

    assert bad.level == AlertLevel.CRITICAL
    assert bad.title == "证书续期失败"
    assert bad.domain == "vpn.example.com"
    assert "续期失败" in bad.message


def test_dingding_notifier_issue_result_uses_issue_titles():
    from app.utils import notifier as notifier_mod

    events = []

    class FakeNotifier(notifier_mod.DingDingNotifier):
        async def notify_alert(self, event: AlertEvent) -> None:
            events.append(event)

    notifier = FakeNotifier(webhook="", secret="", pc_slide=True, at_all=False)
    asyncio.run(
        notifier.notify_issue_result(
            domain="example.com",
            success=True,
            message="域名: example.com",
            new_expires_at="2026-12-01T00:00:00",
        )
    )
    asyncio.run(
        notifier.notify_issue_result(
            domain="example.com",
            success=False,
            message="DNS 验证失败",
        )
    )
    asyncio.run(
        notifier.notify_issue_result(
            domain="example.com",
            success=False,
            message="CDN 部署失败",
            issued=True,
        )
    )

    assert events[0].title == "证书签发成功"
    assert events[0].domain == ""
    assert events[1].title == "证书签发失败"
    assert events[1].domain == "example.com"
    assert events[2].title == "证书签发成功但部署失败"


def test_dingding_notifier_deploy_summary_formats_success_and_failure(monkeypatch):
    """notify_deploy_summary 成功/失败应使用部署标题，且成功时 domain 置空。"""
    from app.utils import notifier as notifier_mod

    events = []

    class FakeNotifier(notifier_mod.DingDingNotifier):
        async def notify_alert(self, event: AlertEvent) -> None:
            events.append(event)

    n = FakeNotifier(webhook="", secret="", pc_slide=True, at_all=False)

    asyncio.run(
        n.notify_deploy_summary(
            domain="vpn.example.com",
            success=True,
            message="域名: vpn.example.com\n部署目标:\n  奇裕 阿里云 cdn",
        )
    )
    asyncio.run(
        n.notify_deploy_summary(
            domain="vpn.example.com",
            success=False,
            message="证书部署失败: boom",
        )
    )

    assert len(events) == 2
    ok, bad = events

    assert ok.level == AlertLevel.INFO
    assert ok.title == "证书部署成功"
    assert ok.domain == ""
    assert ok.source == "deploy"
    assert "部署目标" in ok.message

    assert bad.level == AlertLevel.CRITICAL
    assert bad.title == "证书部署失败"
    assert bad.domain == "vpn.example.com"
    assert bad.source == "deploy"
    assert "证书部署失败" in bad.message


def test_alert_batcher_groups_and_builds_text():
    """AlertBatcher 应按 (source, level) 分组并生成多条合并文本。"""
    from app.utils.notifier import AlertBatcher

    batch = AlertBatcher()
    batch.add(
        AlertEvent(
            level=AlertLevel.CRITICAL,
            title="证书即将过期",
            message="在 3 天后过期",
            source="cert",
            domain="api.example.com",
        )
    )
    batch.add(
        AlertEvent(
            level=AlertLevel.CRITICAL,
            title="证书即将过期",
            message="在 1 天后过期",
            source="cert",
            domain="www.example.com",
        )
    )
    groups = list(batch.groups())
    assert len(groups) == 1
    (source, level), events = groups[0]
    assert source == "cert"
    assert level == AlertLevel.CRITICAL
    assert len(events) == 2

    title = batch.build_title(source, level, len(events))
    text = batch.build_text(events)
    assert title.startswith("🚨 证书")
    lines = text.splitlines()
    assert "- api.example.com: 在 3 天后过期" in lines
    assert "- www.example.com: 在 1 天后过期" in lines


def test_alert_batcher_skips_when_no_notification_channel(monkeypatch, caplog):
    """未启用通知通道时应跳过发送，不能影响证书扫描流程。"""
    from app.utils import notifier as notifier_mod

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(notifier_mod, "load_raw_config", lambda: {})
    monkeypatch.setattr(notifier_mod.asyncio, "sleep", no_sleep)

    notifier = notifier_mod.DingDingNotifier()
    batch = notifier_mod.AlertBatcher()
    batch.add(
        AlertEvent(
            level=AlertLevel.WARNING,
            title="证书即将过期",
            message="将在 30 天后过期",
            source="cert",
            domain="api.example.com",
        )
    )

    asyncio.run(batch.flush_alerts(notifier))

    assert "未启用任何通知通道，后续通知将被跳过" in caplog.text

