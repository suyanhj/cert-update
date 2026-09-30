import pytest
from box import Box

import app.services.nginx_planner as nginx_deploy
import app.services.domains as domains_service


def _base_nginx_conf() -> Box:
    return Box(
        {
            "ssh_profiles": [
                {
                    "name": "ops-key",
                    "username": "root",
                    "auth": {"type": "key", "key_path": "~/.ssh/id_ed25519"},
                }
            ],
            "nginx": {
                "nginx_hosts": [
                    {
                        "name": "web-01",
                        "host": "10.0.0.1",
                        "ssh_profile": "ops-key",
                        "base_dir": "/etc/nginx/ssl",
                    }
                ],
                "nginx_target_groups": [
                    {
                        "name": "prod-api",
                        "hosts": ["web-01"],
                        "cert_layout": {
                            "cert_file": "{base_dir}/{resolved_cert_name}/fullchain.pem",
                            "key_file": "{base_dir}/{resolved_cert_name}/privkey.pem",
                        },
                    }
                ],
                "nginx_deploy_rules": [
                    {
                        "cert_domains": ["*.example.com"],
                        "target_group": "prod-api",
                        "cert_name_template": "{main_domain}",
                    }
                ],
            },
        },
        default_box=True,
    )


def test_nginx_planner_validate_config_success(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(nginx_deploy, "load_raw_config", lambda: _base_nginx_conf())
    planner = nginx_deploy.NginxDeployPlanner()
    result = planner.validate_config()
    assert result["valid"] is True
    assert result["errors"] == []


def test_nginx_planner_plan_for_certificate_wildcard_single_level(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(nginx_deploy, "load_raw_config", lambda: _base_nginx_conf())
    planner = nginx_deploy.NginxDeployPlanner()

    plan = planner.plan_for_certificate("example.com", ["*.example.com"])

    assert plan["mode"] == "dry-run"
    assert len(plan["targets"]) == 1
    target = plan["targets"][0]
    assert target["host_name"] == "web-01"
    assert target["cert_name"] == "example.com"


def test_domains_get_provider_returns_merged_provider_map(monkeypatch: pytest.MonkeyPatch):
    class DummyProvider:
        def __init__(self, cfg):
            self.name = cfg.name

    monkeypatch.setattr(
        domains_service,
        "load_raw_config",
        lambda: Box(
            {
                "providers": [
                    {"name": "custom-01", "type": "custom", "enabled": True, "static_domains": []},
                    {"name": "aliyun-01", "type": "aliyun", "enabled": True, "credentials": {}},
                ],
                "dingding": {"webhook": "https://example.com", "secret": ""},
                "alert": {"domain": {"enabled": False}},
                "whitelist": [],
            },
            default_box=True,
        ),
    )
    monkeypatch.setattr(domains_service, "StaticCloudProvider", DummyProvider)
    monkeypatch.setattr(domains_service, "AliyunCloudProvider", DummyProvider)
    monkeypatch.setattr(domains_service, "TencentCloudProvider", DummyProvider)
    monkeypatch.setattr(domains_service, "HuaweiCloudProvider", DummyProvider)
    monkeypatch.setattr(domains_service, "VolcengineCloudProvider", DummyProvider)
    monkeypatch.setattr(domains_service, "QiniuCloudProvider", DummyProvider)
    monkeypatch.setattr(domains_service.notifier, "DingDingNotifier", lambda *a, **k: object())
    monkeypatch.setattr(domains_service.store, "AlertStateStore", lambda *a, **k: object())
    monkeypatch.setattr(domains_service.notifier, "AlertBatcher", lambda *a, **k: object())

    svc = domains_service.Domains()

    assert svc.get_provider("custom-01") is not None
    assert svc.get_provider("aliyun-01") is not None
