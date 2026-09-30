"""Nginx 部署规划器（只做配置校验和 dry-run 规划）。"""

from __future__ import annotations

from dataclasses import dataclass
from string import Formatter
from typing import Any, Dict, List, Optional

from app.config import load_raw_config
from app.utils.domain_match import build_cert_domains, match_domain, normalize_domain
from app.utils.logger import get_logger


LOGGER = get_logger("app")


@dataclass
class NginxDeployPlanItem:
    host_name: str
    host: str
    port: int
    username: str
    target_group: str
    cert_name: str
    cert_file: str
    key_file: str
    file_mode: str
    test_command: str
    reload_command: str
    reload_use_placeholders: bool
    sudo: bool
    backup_dir: Optional[str]


class NginxDeployPlanner:
    """Nginx 多主机部署规划器。"""

    _LAYOUT_PLACEHOLDERS = {
        "base_dir",
        "resolved_cert_name",
        "main_domain",
        "cert_name",
    }

    def __init__(self) -> None:
        self.conf = load_raw_config()

    @staticmethod
    def _as_list(raw: Any, field_name: str) -> List[Any]:
        if raw is None:
            return []
        if isinstance(raw, list):
            return raw
        # Box 会把 YAML 列表包装成 BoxList，isinstance(raw, list) 为 False，需按可迭代转成 list
        if isinstance(raw, (dict, str)):
            raise ValueError(f"{field_name} 必须是列表")
        try:
            return list(raw)
        except TypeError:
            raise ValueError(f"{field_name} 必须是列表")

    def _read_nginx_lists(self) -> tuple[List[Any], List[Any], List[Any], List[Any]]:
        section = self.conf.nginx
        profiles_raw = self.conf.ssh_profiles or section.ssh_profiles
        hosts_raw = section.nginx_hosts or self.conf.nginx_hosts
        groups_raw = section.nginx_target_groups or self.conf.nginx_target_groups
        rules_raw = section.nginx_deploy_rules or self.conf.nginx_deploy_rules

        profiles = self._as_list(profiles_raw or [], "ssh_profiles")
        hosts = self._as_list(hosts_raw or [], "nginx.nginx_hosts")
        groups = self._as_list(groups_raw or [], "nginx.nginx_target_groups")
        rules = self._as_list(rules_raw or [], "nginx.nginx_deploy_rules")
        return profiles, hosts, groups, rules

    @classmethod
    def _validate_layout_template(cls, template: str, field_name: str) -> List[str]:
        """校验目标路径模板，只允许语义明确的占位符。"""
        try:
            placeholders = {
                placeholder
                for _, placeholder, _, _ in Formatter().parse(template)
                if placeholder
            }
        except ValueError as exc:
            return [f"{field_name} 格式非法: {exc}"]

        unsupported = sorted(placeholders - cls._LAYOUT_PLACEHOLDERS)
        if not unsupported:
            return []
        return [
            f"{field_name} 包含不支持的占位符: {name}；"
            "可用占位符为 {base_dir}、{resolved_cert_name}、{main_domain}、{cert_name}"
            for name in unsupported
        ]

    def validate_config(self) -> Dict[str, Any]:
        profiles, hosts, groups, rules = self._read_nginx_lists()

        profile_map = {str(p.get("name", "")).strip(): p for p in profiles if p.get("name")}
        host_map = {str(h.get("name", "")).strip(): h for h in hosts if h.get("name")}
        group_map = {str(g.get("name", "")).strip(): g for g in groups if g.get("name")}

        errors: List[str] = []

        for host_name, host in host_map.items():
            profile_name = str(host.get("ssh_profile", "")).strip()
            if not profile_name:
                errors.append(f"nginx_hosts[{host_name}] 缺少 ssh_profile")
                continue
            profile = profile_map.get(profile_name)
            if not profile:
                errors.append(f"nginx_hosts[{host_name}] 引用的 ssh_profile 不存在: {profile_name}")
                continue
            auth = profile.get("auth", {}) or {}
            auth_type = str(auth.get("type", "key")).strip().lower()
            if auth_type == "key":
                if not auth.get("key_path"):
                    errors.append(f"ssh_profile[{profile_name}] type=key 时必须配置 key_path")
            elif auth_type == "password":
                if not auth.get("password") and not auth.get("password_env"):
                    errors.append(
                        f"ssh_profile[{profile_name}] type=password 时必须配置 password 或 password_env"
                    )
            else:
                errors.append(f"ssh_profile[{profile_name}] auth.type 不支持: {auth_type}")

        for group_name, group in group_map.items():
            group_hosts = self._as_list(group.get("hosts", []), f"nginx_target_groups[{group_name}].hosts")
            if not group_hosts:
                errors.append(f"nginx_target_groups[{group_name}] hosts 不能为空")
            for host_name in group_hosts:
                if str(host_name).strip() not in host_map:
                    errors.append(f"nginx_target_groups[{group_name}] 引用的 host 不存在: {host_name}")
            layout = group.get("cert_layout", {}) or {}
            layout_templates = {
                "cert_file": str(
                    layout.get(
                        "cert_file",
                        "{base_dir}/{resolved_cert_name}/fullchain.pem",
                    )
                ),
                "key_file": str(
                    layout.get(
                        "key_file",
                        "{base_dir}/{resolved_cert_name}/privkey.pem",
                    )
                ),
            }
            for layout_field, template in layout_templates.items():
                errors.extend(
                    self._validate_layout_template(
                        template,
                        f"nginx_target_groups[{group_name}].cert_layout.{layout_field}",
                    )
                )

        for idx, rule in enumerate(rules):
            target_group = str(rule.get("target_group", "")).strip()
            if not target_group:
                errors.append(f"nginx_deploy_rules[{idx}] 缺少 target_group")
                continue
            if target_group not in group_map:
                errors.append(f"nginx_deploy_rules[{idx}] 引用的 target_group 不存在: {target_group}")
            cert_domains = self._as_list(
                rule.get("cert_domains", []),
                f"nginx_deploy_rules[{idx}].cert_domains",
            )
            if not cert_domains:
                errors.append(f"nginx_deploy_rules[{idx}] cert_domains 不能为空")
            # 直接复用统一域名工具，提前暴露非法值，避免规划阶段才抛异常。
            for domain in cert_domains:
                try:
                    normalize_domain(str(domain))
                except ValueError:
                    errors.append(f"nginx_deploy_rules[{idx}] cert_domains 存在空或非法域名: {domain}")

        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "counts": {
                "ssh_profiles": len(profile_map),
                "nginx_hosts": len(host_map),
                "nginx_target_groups": len(group_map),
                "nginx_deploy_rules": len(rules),
            },
        }

    def plan_for_certificate(self, main_domain: str, sans: List[str]) -> Dict[str, Any]:
        check = self.validate_config()
        if not check["valid"]:
            raise ValueError(f"Nginx 配置校验失败: {check['errors']}")

        # 静态 Nginx 场景下按证书主域名 + SAN 做匹配：
        # - 源：续签证书的 main_domain 与 sans（含 *. 通配 SAN）
        # - 目标：规则中的 cert_domains（具体域名如 www 或 *. 开头的模式）
        # - 具体域名：与 deploy 云绑定一致，用 match_domain(cert, rule_host)（www 可由 *.apex 覆盖）
        # - 规则以 *. 开头：仍按「apex 与证书各方去 *. 后的 apex」交集，兼容只写 *.example.com 的写法
        all_cert_domains: List[str] = [main_domain, *sans]
        source_candidates = {
            normalize_domain(str(item).lstrip("*."))
            for item in all_cert_domains
            if str(item).strip()
        }
        cert_domain_set = build_cert_domains(main_domain, sans)
        profiles, hosts, groups, rules = self._read_nginx_lists()

        profile_map = {str(p.get("name", "")).strip(): p for p in profiles if p.get("name")}
        host_map = {str(h.get("name", "")).strip(): h for h in hosts if h.get("name")}
        group_map = {str(g.get("name", "")).strip(): g for g in groups if g.get("name")}

        matched_rules: List[Dict[str, Any]] = []
        plans: List[NginxDeployPlanItem] = []
        skipped_rules: List[Dict[str, str]] = []

        for idx, rule in enumerate(rules):
            raw_match_domains = self._as_list(
                rule.get("cert_domains", []),
                f"nginx_deploy_rules[{idx}].cert_domains",
            )
            # cert_domains 语义：本地证书文件名主体或业务域名模式（可带 *. 前缀）。
            # 静态 nginx 场景：当配置项（去掉 *. 前缀）命中证书主域名或 SAN（同样去掉 *. 前缀）时，认为规则命中。
            selected_cert_name: Optional[str] = None
            match_domains: List[str] = []
            is_hit = False
            for original in raw_match_domains:
                raw = str(original).strip()
                # 对比时统一去掉前缀 *.（展示与通配规则 apex 比对用）
                normalized = normalize_domain(raw.lstrip("*."))
                match_domains.append(normalized)
                if raw.startswith("*."):
                    if normalized in source_candidates:
                        selected_cert_name = raw
                        is_hit = True
                elif match_domain(cert_domain_set, raw):
                    selected_cert_name = raw
                    is_hit = True

            if not is_hit:
                skipped_rules.append({"rule_index": str(idx), "reason": "证书域名不匹配"})
                continue

            target_group_name = str(rule.get("target_group", "")).strip()
            group = group_map[target_group_name]
            layout = group.get("cert_layout", {}) or {}
            cert_name_template = str(rule.get("cert_name_template", "{main_domain}"))
            # cert_name_template 支持的占位符：
            #   {main_domain}  - 证书主域名（acme 签发主体），如 sqkm2023.com；与本地存储文件名一致
            #   {cert_name}    - 规则中命中的域名（去掉 *. 前缀），如 api-dev.sqkm2023.com
            # 本地证书文件以主域名命名时，模板应使用 {main_domain}
            cert_name_source = selected_cert_name or main_domain
            matched_cert_name = str(cert_name_source).lstrip("*.").strip()
            resolved_cert_name = cert_name_template.format(
                main_domain=main_domain,
                cert_name=matched_cert_name,
            )

            matched_rules.append(
                {
                    "rule_index": idx,
                    "target_group": target_group_name,
                    "matched_domains": match_domains,
                    "cert_name": resolved_cert_name,
                }
            )

            for host_name in self._as_list(group.get("hosts", []), f"group[{target_group_name}].hosts"):
                host = host_map[str(host_name)]
                profile = profile_map[str(host.get("ssh_profile"))]
                username = str(profile.get("username", "root"))
                base_dir = str(host.get("base_dir", "/etc/nginx/ssl"))
                cert_file_tmpl = str(
                    layout.get(
                        "cert_file",
                        "{base_dir}/{resolved_cert_name}/fullchain.pem",
                    )
                )
                key_file_tmpl = str(
                    layout.get(
                        "key_file",
                        "{base_dir}/{resolved_cert_name}/privkey.pem",
                    )
                )
                file_mode = str(layout.get("mode", "644") or "644")
                plans.append(
                    NginxDeployPlanItem(
                        host_name=str(host_name),
                        host=str(host.get("host")),
                        port=int(host.get("port", 22)),
                        username=username,
                        target_group=target_group_name,
                        cert_name=resolved_cert_name,
                        cert_file=cert_file_tmpl.format(
                            base_dir=base_dir,
                            resolved_cert_name=resolved_cert_name,
                            main_domain=main_domain,
                            cert_name=matched_cert_name,
                        ),
                        key_file=key_file_tmpl.format(
                            base_dir=base_dir,
                            resolved_cert_name=resolved_cert_name,
                            main_domain=main_domain,
                            cert_name=matched_cert_name,
                        ),
                        file_mode=file_mode,
                        test_command=str(host.get("test_command", "nginx -t")),
                        reload_command=str(host.get("reload_command", "nginx -s reload")),
                        reload_use_placeholders=bool(host.get("reload_use_placeholders", False)),
                        sudo=bool(host.get("sudo", False)),
                        backup_dir=host.get("backup_dir"),
                    )
                )

        LOGGER.info(
            "nginx deploy dry-run plan generated: matched_rules=%d targets=%d",
            len(matched_rules),
            len(plans),
        )
        if plans:
            examples = [
                f"{item.host_name}@{item.host}:{item.port} -> cert={item.cert_name} file={item.cert_file}"
                for item in plans[:20]
            ]
            LOGGER.info(
                "nginx deploy target detail: main_domain=%s examples=%s",
                main_domain,
                examples,
            )
        return {
            "mode": "dry-run",
            "cert": {
                "main_domain": main_domain,
                "sans": sorted(list(source_candidates)),
            },
            "matched_rules": matched_rules,
            "targets": [item.__dict__ for item in plans],
            "skipped_rules": skipped_rules,
        }
