from app.utils.rocketchat import COLOR_ALARM, COLOR_OK, RocketChatSendError, attachment_color, send_rocketchat


class _FakeResponse:
    def __init__(self, status_code: int, text: str = "ok"):
        self.status_code = status_code
        self.text = text


def _target():
    return {
        "webhook": "https://chat.example.com/hooks/id/token",
        "channel": ["#test", "@root"],
        "username": "监控系统",
        "icon_emoji": ":warning:",
        "title": "证书监控",
    }


def test_attachment_color_alarm_and_recovery():
    """普通告警为红，含恢复为绿。"""
    assert attachment_color("证书即将过期") == COLOR_ALARM
    assert attachment_color("证书已恢复") == COLOR_OK


def test_send_rocketchat_posts_incoming_webhook_payload(monkeypatch):
    """应 POST Incoming Webhook JSON，text 为正文第一行。"""
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return _FakeResponse(200)

    monkeypatch.setattr("app.utils.rocketchat.httpx.post", fake_post)
    send_rocketchat(_target(), "标题行\n详情行")

    assert captured["url"] == "https://chat.example.com/hooks/id/token"
    assert captured["json"]["channel"] == ["#test", "@root"]
    assert captured["json"]["text"] == "标题行"
    assert captured["json"]["attachments"][0]["title"] == "证书监控"
    assert captured["json"]["attachments"][0]["text"] == "标题行\n详情行"
    assert captured["json"]["attachments"][0]["color"] == COLOR_ALARM


def test_send_rocketchat_http_error_raises(monkeypatch):
    """HTTP 非 2xx 应抛 RocketChatSendError。"""

    def fake_post(url, json=None, headers=None, timeout=None):
        return _FakeResponse(500, "boom")

    monkeypatch.setattr("app.utils.rocketchat.httpx.post", fake_post)
    try:
        send_rocketchat(_target(), "告警")
    except RocketChatSendError as exc:
        assert "500" in str(exc)
        return
    raise AssertionError("应抛出 RocketChatSendError")
