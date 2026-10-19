from box import Box

import app.ui.pages as pages


class Dummy:
    def __init__(self, *args, **kwargs):
        self.value = kwargs.get("value", "")
        self.args = args
        self.kwargs = kwargs
    def __enter__(self): return self
    def __exit__(self, *exc): return False
    def __call__(self, *args, **kwargs): return Dummy(*args, **kwargs)
    def __getattr__(self, name):
        if name in {"classes", "props", "style", "tooltip", "on", "add_slot", "set_visibility", "disable", "set_text"}:
            return lambda *args, **kwargs: self
        if name in {"open", "close"}:
            return lambda *args, **kwargs: None
        return Dummy()


class FakeUI:
    def __getattr__(self, name):
        if name == "context":
            return Dummy(client=Dummy(id="client"))
        if name == "notify":
            return lambda *a, **k: None
        if name == "navigate":
            return Dummy()
        return lambda *a, **k: Dummy(*a, **k)


def test_render_dashboard_and_config_with_offline_fake_ui(monkeypatch, tmp_path):
    tooltips = []
    monkeypatch.setattr(Dummy, "tooltip", lambda self, text: tooltips.append(text) or self, raising=False)
    monkeypatch.setattr(pages, "ui", FakeUI())
    monkeypatch.setattr(pages, "get_config", lambda: Box({
        "auto_renew_enabled": True,
        "alert": {
            "cert": {"cert_warn_days": 7, "cert_expiry_days": 30},
            "domain": {"domain_warn_days": 7, "domain_expiry_days": 30},
        },
        "certificate_policies": {},
        "deploy": {"mode": "dry-run"},
        "staging": {"enabled": False},
    }, default_box=True))
    monkeypatch.setattr(pages.app_config, "CONFIG_PATH", tmp_path / "config.yaml")
    (tmp_path / "config.yaml").write_text("log_level: INFO\n", encoding="utf-8")
    monkeypatch.setattr(pages, "get_certificate_policy", lambda cfg, domain: SimplePolicy())
    monkeypatch.setattr(pages, "collect_all", lambda: None)
    monkeypatch.setattr(pages, "apply_config_text", lambda text: None)
    monkeypatch.setattr(pages, "format_provider_type_summary", lambda x: "summary")
    monkeypatch.setattr(pages, "state", type("S", (), {
        "CERT_LIST": [{"domain": "a.example.com", "days": 20, "sans": [], "key": "RSA", "ca": "LE", "exp": "2030", "provider": "p"}],
        "DOMAIN_GROUPS": [{
            "domain": "example.com", "days": 40, "expires_at": "2030",
            "registrant_org": "Org", "registrar_provider": "r", "registrar_name": "R",
            "dns_provider": "d", "dns_name": "D",
            "subs": [
                {"name": "www.example.com", "status": "在工作", "remark": ""},
                {"name": "waf.example.com", "status": "在工作", "status_reason": "HTTPS：WAF 拦截（HTTP 567），服务器可响应", "remark": ""},
                {"name": "bad.example.com", "status": "异常", "status_reason": "HTTP：HTTP 502；HTTPS：ReadTimeout", "remark": "bad"},
                {"name": "legacy.example.com", "status": "异常", "remark": ""},
                {"name": "x.example.com", "status": "unknown", "remark": ""},
            ],
        }],
        "LAST_UPDATED": "2030",
    })())
    pages.render_certificates()
    pages.render_domains()
    assert tooltips.count("HTTPS：WAF 拦截（HTTP 567），服务器可响应") == 2
    assert tooltips.count("HTTP：HTTP 502；HTTPS：ReadTimeout") == 2
    assert tooltips.count("暂无详细原因，请重新扫描获取探测结果") == 2
    pages.build_ui()
    pages.build_config_ui()


class SimplePolicy:
    source = "acme"
    auto_renew = True
    dns_provider = "dns"
