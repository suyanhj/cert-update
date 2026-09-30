from datetime import datetime, timezone

from box import Box

from app.services.nginx_planner import NginxDeployPlanner


def _make_base_config() -> Box:
    """构造一个最小可用的 nginx 配置，用于 NginxDeployPlanner 单测。"""
    return Box(
        {
            "ssh_profiles": [
                {
                    "name": "ops-key",
                    "username": "root",
                    "auth": {
                        "type": "key",
                        "key_path": "/root/.ssh/id_ed25519",
                    },
                }
            ],
            "nginx": {
                "nginx_hosts": [
                    {
                        "name": "host-1",
                        "host": "10.0.0.1",
                        "port": 22,
                        "ssh_profile": "ops-key",
                        "sudo": True,
                        "base_dir": "/etc/nginx/ssl",
                        "test_command": "nginx -t",
                        "reload_command": "nginx -s reload",
                        "backup_dir": "/data/nginx/backup",
                    },
                    {
                        "name": "host-2",
                        "host": "10.0.0.2",
                        "port": 2222,
                        "ssh_profile": "ops-key",
                        "sudo": False,
                        "base_dir": "/etc/nginx/ssl2",
                        "test_command": "nginx -t",
                        "reload_command": "nginx -s reload",
                        "backup_dir": "/data/nginx/backup2",
                    }
                ],
                "nginx_target_groups": [
                    {
                        "name": "group-1",
                        "hosts": ["host-1"],
                        "cert_layout": {
                            "cert_file": "{base_dir}/{resolved_cert_name}.pem",
                            "key_file": "{base_dir}/{resolved_cert_name}.key",
                            "owner": "root",
                            "group": "root",
                            "mode": "600",
                        },
                    },
                    {
                        "name": "group-2",
                        "hosts": ["host-2"],
                        "cert_layout": {
                            "cert_file": "{base_dir}/{main_domain}/fullchain.pem",
                            "key_file": "{base_dir}/{main_domain}/privkey.pem",
                            "owner": "root",
                            "group": "root",
                            "mode": "640",
                        },
                    }
                ],
                "nginx_deploy_rules": [
                    {
                        "cert_domains": ["api.example.com"],
                        "target_group": "group-1",
                        "cert_name_template": "{cert_name}",
                    },
                    {
                        "cert_domains": ["www.example.com"],
                        "target_group": "group-2",
                        "cert_name_template": "{main_domain}",
                    }
                ],
            },
        },
        default_box=True,
    )


def test_validate_config_ok(monkeypatch):
    """基础配置应校验通过且统计数量正确。"""
    cfg = _make_base_config()
    monkeypatch.setattr("app.services.nginx_planner.load_raw_config", lambda: cfg)

    planner = NginxDeployPlanner()
    result = planner.validate_config()

    assert result["valid"] is True
    assert result["counts"]["ssh_profiles"] == 1
    assert result["counts"]["nginx_hosts"] == 2
    assert result["counts"]["nginx_target_groups"] == 2
    assert result["counts"]["nginx_deploy_rules"] == 2
    assert result["errors"] == []


def test_validate_config_reports_missing_ssh_profile(monkeypatch):
    """缺少 ssh_profile 或引用不存在的 profile 时应返回错误。"""
    cfg = _make_base_config()
    # 让 host 引用一个不存在的 ssh_profile
    cfg.nginx.nginx_hosts[0].ssh_profile = "missing-profile"
    monkeypatch.setattr("app.services.nginx_planner.load_raw_config", lambda: cfg)

    planner = NginxDeployPlanner()
    result = planner.validate_config()

    assert result["valid"] is False
    errors = result["errors"]
    assert any("引用的 ssh_profile 不存在" in e for e in errors)


def test_validate_config_rejects_unknown_layout_placeholder(monkeypatch):
    """路径模板应拒绝未定义的占位符并列出可用名称。"""
    cfg = _make_base_config()
    cfg.nginx.nginx_target_groups[0].cert_layout.cert_file = (
        "{base_dir}/{unknown_name}.pem"
    )
    monkeypatch.setattr("app.services.nginx_planner.load_raw_config", lambda: cfg)

    result = NginxDeployPlanner().validate_config()

    assert result["valid"] is False
    assert any(
        "cert_layout.cert_file 包含不支持的占位符: unknown_name" in error
        and "{resolved_cert_name}" in error
        and "{cert_name}" in error
        for error in result["errors"]
    )


def test_cert_layout_distinguishes_three_certificate_name_placeholders(monkeypatch):
    """路径模板中的渲染名称、主域名和命中域名应保持独立语义。"""
    cfg = _make_base_config()
    layout = cfg.nginx.nginx_target_groups[0].cert_layout
    layout.cert_file = (
        "{base_dir}/{main_domain}/{cert_name}/{resolved_cert_name}.pem"
    )
    layout.key_file = (
        "{base_dir}/{main_domain}/{cert_name}/{resolved_cert_name}.key"
    )
    rule = cfg.nginx.nginx_deploy_rules[0]
    rule.cert_domains = ["api.example.com"]
    rule.cert_name_template = "release-{main_domain}"
    monkeypatch.setattr("app.services.nginx_planner.load_raw_config", lambda: cfg)

    plan = NginxDeployPlanner().plan_for_certificate(
        main_domain="example.com",
        sans=["*.example.com"],
    )

    target = plan["targets"][0]
    assert target["cert_name"] == "release-example.com"
    assert target["cert_file"] == (
        "/etc/nginx/ssl/example.com/api.example.com/release-example.com.pem"
    )
    assert target["key_file"] == (
        "/etc/nginx/ssl/example.com/api.example.com/release-example.com.key"
    )


def test_plan_for_certificate_matches_exact_domain(monkeypatch):
    """main_domain 精确命中 cert_domains 时，应生成一条匹配规则和一个目标 plan。"""
    cfg = _make_base_config()
    monkeypatch.setattr("app.services.nginx_planner.load_raw_config", lambda: cfg)

    planner = NginxDeployPlanner()
    main_domain = "api.example.com"
    sans: list[str] = []

    plan = planner.plan_for_certificate(main_domain=main_domain, sans=sans)

    assert plan["mode"] == "dry-run"
    assert plan["cert"]["main_domain"] == main_domain
    # matched_rules 中应只命中 group-1 的规则
    matched = plan["matched_rules"]
    assert len(matched) == 1
    rule = matched[0]
    assert rule["target_group"] == "group-1"
    # cert_name_template={cert_name}，命中 api.example.com，去掉 *. 后 cert_name 也应为 api.example.com
    assert rule["cert_name"] == "api.example.com"

    targets = plan["targets"]
    assert len(targets) == 1
    t = targets[0]
    assert t["host_name"] == "host-1"
    assert t["target_group"] == "group-1"
    # cert_name_template 的渲染结果应通过 {resolved_cert_name} 写入目标路径
    assert t["cert_file"] == "/etc/nginx/ssl/api.example.com.pem"
    assert t["key_file"] == "/etc/nginx/ssl/api.example.com.key"
    # 模式应来自 layout.mode
    assert t["file_mode"] == "600"


def test_plan_writes_fullchain_with_main_domain_target_name(monkeypatch):
    """源 fullchain.cer 的内容应按目标模板写成主域名 PEM/KEY 文件名。"""
    cfg = _make_base_config()
    cfg.nginx.nginx_target_groups[0].cert_layout = {
        "cert_file": "{base_dir}/{main_domain}.pem",
        "key_file": "{base_dir}/{main_domain}.key",
        "mode": "600",
    }
    cfg.nginx.nginx_deploy_rules[0].cert_name_template = "{cert_name}"
    monkeypatch.setattr("app.services.nginx_planner.load_raw_config", lambda: cfg)

    plan = NginxDeployPlanner().plan_for_certificate(
        main_domain="example.com",
        sans=["api.example.com"],
    )

    assert plan["targets"][0]["cert_file"] == "/etc/nginx/ssl/example.com.pem"
    assert plan["targets"][0]["key_file"] == "/etc/nginx/ssl/example.com.key"


def test_plan_for_certificate_multi_group_targets(monkeypatch):
    """不同域名应命中不同 target_group，并生成各自的 host/cert 布局。"""
    cfg = _make_base_config()
    monkeypatch.setattr("app.services.nginx_planner.load_raw_config", lambda: cfg)

    planner = NginxDeployPlanner()

    # 1) 命中 group-1（host-1）
    plan1 = planner.plan_for_certificate(main_domain="api.example.com", sans=[])
    targets1 = sorted(plan1["targets"], key=lambda x: x["host_name"])
    assert [t["host_name"] for t in targets1] == ["host-1"]
    t1 = targets1[0]
    assert t1["target_group"] == "group-1"
    assert t1["cert_file"] == "/etc/nginx/ssl/api.example.com.pem"
    assert t1["key_file"] == "/etc/nginx/ssl/api.example.com.key"
    assert t1["file_mode"] == "600"

    # 2) 命中 group-2（host-2），模板中使用 {main_domain}
    plan2 = planner.plan_for_certificate(main_domain="www.example.com", sans=[])
    targets2 = sorted(plan2["targets"], key=lambda x: x["host_name"])
    assert [t["host_name"] for t in targets2] == ["host-2"]
    t2 = targets2[0]
    assert t2["target_group"] == "group-2"
    # cert_file/key_file 中应使用 host-2 的 base_dir 和 main_domain 占位符
    assert t2["cert_file"] == "/etc/nginx/ssl2/www.example.com/fullchain.pem"
    assert t2["key_file"] == "/etc/nginx/ssl2/www.example.com/privkey.pem"
    assert t2["file_mode"] == "640"


def test_plan_for_certificate_matches_wildcard_by_san(monkeypatch):
    """当证书 SAN 包含与规则匹配的通配域名时，应命中该规则。"""
    cfg = _make_base_config()
    # 仅保留通配规则，避免同一张泛域名证书同时命中多条 cert_domains
    cfg.nginx.nginx_deploy_rules = [
        {
            "cert_domains": ["*.example.com"],
            "target_group": "group-1",
            "cert_name_template": "{main_domain}",
        }
    ]
    monkeypatch.setattr("app.services.nginx_planner.load_raw_config", lambda: cfg)

    planner = NginxDeployPlanner()
    main_domain = "main.example.com"
    # 证书 SAN 须含与规则一致的通配符，*.example.com 与规则 cert_domains 中 *.example.com 对应
    sans = ["*.example.com"]

    plan = planner.plan_for_certificate(main_domain=main_domain, sans=sans)

    matched = plan["matched_rules"]
    assert len(matched) == 1
    rule = matched[0]
    # selected_cert_name 应为 "*.example.com"，但 cert_name_clean 去掉 "*." 后为 "example.com"
    # cert_name_template 默认 "{main_domain}"，所以 cert_name 应为 main_domain
    assert rule["cert_name"] == "main.example.com"

    targets = plan["targets"]
    assert len(targets) == 1
    t = targets[0]
    # cert_file 中的 resolved_cert_name 应为 main_domain
    assert t["cert_file"] == "/etc/nginx/ssl/main.example.com.pem"
    assert t["key_file"] == "/etc/nginx/ssl/main.example.com.key"


def test_plan_for_certificate_matches_www_when_cert_has_wildcard_san(monkeypatch):
    """规则写 www 子域、证书为主域 + *.apex SAN 时应命中（与 acme 主域规范为 apex 的场景一致）。"""
    cfg = _make_base_config()
    cfg.nginx.nginx_deploy_rules = [
        {
            "cert_domains": ["www.example.com"],
            "target_group": "group-1",
            "cert_name_template": "{cert_name}",
        }
    ]
    monkeypatch.setattr("app.services.nginx_planner.load_raw_config", lambda: cfg)

    planner = NginxDeployPlanner()
    plan = planner.plan_for_certificate(main_domain="example.com", sans=["*.example.com"])

    matched = plan["matched_rules"]
    assert len(matched) == 1
    assert matched[0]["target_group"] == "group-1"
    assert matched[0]["cert_name"] == "www.example.com"


def test_plan_for_certificate_skips_non_matching_rules(monkeypatch):
    """当没有任何 cert_domains 命中时，matched_rules 为空且返回 skipped_rules 说明原因。"""
    cfg = _make_base_config()
    # 让规则只匹配另一个域名
    cfg.nginx.nginx_deploy_rules[0].cert_domains = ["other.example.com"]
    monkeypatch.setattr("app.services.nginx_planner.load_raw_config", lambda: cfg)

    planner = NginxDeployPlanner()
    plan = planner.plan_for_certificate(main_domain="api.example.com", sans=[])

    assert plan["matched_rules"] == []
    assert plan["targets"] == []
    skipped = plan["skipped_rules"]
    assert len(skipped) == 2
    assert all(s["reason"] == "证书域名不匹配" for s in skipped)


def test_validate_config_rejects_invalid_cert_domains(monkeypatch):
    """cert_domains 中存在空串或非法域名时，应在 validate_config 阶段直接报错。"""
    cfg = _make_base_config()
    cfg.nginx.nginx_deploy_rules[0].cert_domains = ["", "??bad-domain"]
    monkeypatch.setattr("app.services.nginx_planner.load_raw_config", lambda: cfg)

    planner = NginxDeployPlanner()
    result = planner.validate_config()

    assert result["valid"] is False
    errors = result["errors"]
    assert any("cert_domains 存在空或非法域名" in e for e in errors)
