from __future__ import annotations

from typing import Any, Optional

import asyncio

from app.config import load_raw_config
from app.utils.logger import get_logger
from app.utils.time import TimeUtil
from app.utils.domain_match import find_blacklisted_domain
from app.utils.certificate_policy import certificate_operation_skip, get_certificate_policy
from app.utils import notifier, store
from app.services.deploy import DeployService, DeploymentError

from .renew import (
    issue_certificate,
    load_existing_certificate,
    prepare_issue_domains,
    renew_certificate,
    resolve_renew_domain,
)


LOGGER = get_logger("app")


def format_provider_type_summary(summary: dict | None) -> str:
    if not summary:
        return "无命中"
    by_provider_type = summary.get("by_provider_type", {})
    if not by_provider_type:
        return "无命中"

    chunks: list[str] = []
    for provider_type, payload in by_provider_type.items():
        if isinstance(payload, dict):
            success = int(payload.get("success", 0))
            failed = int(payload.get("failed", 0))
            chunks.append(f"{provider_type}:成功{success}/失败{failed}")
            continue
        chunks.append(f"{provider_type}:{int(payload)}")
    return "；".join(chunks)


def _vendor_label(provider_type: str) -> str:
    pt_l = (provider_type or "").lower()
    if pt_l == "aliyun":
        return "阿里云"
    if pt_l == "huawei":
        return "华为云"
    if pt_l == "tencent":
        return "腾讯云"
    if pt_l == "qiniu":
        return "七牛云"
    if pt_l == "nginx":
        return "Nginx"
    return provider_type


def _product_label(product_type: str) -> str:
    p = (product_type or "").lower()
    if p in ("lb", "clb", "slb"):
        return "elb"
    return p or "unknown"


def format_deploy_notify_message(result: dict | None, display_domain: str) -> str:
    """按 provider 维度拼接部署通知正文（域名 + 部署目标列表）。"""
    lines: list[str] = [f"域名: {display_domain}"]
    if not isinstance(result, dict):
        return "\n".join(lines)

    if result.get("mode") == "dry-run":
        lines.append("部署预演完成，尚未部署")
        lines.append(f"计划云目标: {len(result.get('planned_bind_actions') or [])}；Nginx 目标: {result.get('nginx_target_count', 0)}")
        return "\n".join(lines)

    bind_results = result.get("bind_results") or []

    nginx_groups: set[str] = set()
    if isinstance(bind_results, list):
        for item in bind_results:
            if not isinstance(item, dict):
                continue
            if str(item.get("provider_type") or "").lower() != "nginx":
                continue
            group_name = str(item.get("group") or "").strip()
            if group_name:
                nginx_groups.add(group_name)

    providers_summary: dict[str, dict[str, set[str]]] = {}

    if isinstance(bind_results, list) and bind_results:
        for item in bind_results:
            if not isinstance(item, dict):
                continue
            if item.get("status") != "success":
                continue
            name = str(item.get("provider") or "").strip() or "-"
            provider_type = str(item.get("provider_type") or "").strip().lower()
            product_type = str(item.get("product_type") or "").strip().lower()

            entry = providers_summary.setdefault(
                name,
                {"provider_type": provider_type, "product_types": set()},
            )
            if provider_type:
                entry["provider_type"] = provider_type
            if product_type:
                entry["product_types"].add(_product_label(product_type))

    if not providers_summary:
        summary = result.get("summary") or {}
        by_provider = summary.get("by_provider") or {}
        if isinstance(by_provider, dict):
            for key, _stats in by_provider.items():
                raw = str(key or "")
                if ":" in raw:
                    provider_type, name = raw.split(":", 1)
                else:
                    provider_type, name = "", raw
                name = name.strip() or "-"
                provider_type = provider_type.strip().lower()

                entry = providers_summary.setdefault(
                    name,
                    {"provider_type": provider_type, "product_types": set()},
                )
                entry["product_types"].add("*")

    if providers_summary:
        lines.append("部署目标:")
        nginx_added = False
        for name, payload in providers_summary.items():
            provider_type = (payload.get("provider_type") or "").lower()
            products = sorted(payload.get("product_types") or [])

            if provider_type == "nginx":
                if not nginx_added:
                    if nginx_groups:
                        for group_name in sorted(nginx_groups):
                            lines.append(f"  nginx 组 {group_name}")
                    else:
                        lines.append("  nginx 组")
                    nginx_added = True
                continue

            vendor = _vendor_label(provider_type)
            parts = [name]
            if vendor:
                parts.append(vendor)
            parts.extend(products)
            lines.append("  " + " ".join(parts))

    return "\n".join(lines)


def _blacklist_skip(config, domains: list[str], status_key: str) -> dict[str, Any] | None:
    """在操作入口检查黑名单，命中时返回统一跳过结果。"""
    matched = find_blacklisted_domain(domains, config.get("blacklist", []))
    if matched is None:
        return None
    message = f"命中黑名单 {matched}，本次操作已跳过"
    LOGGER.info("证书操作跳过: operation=%s domain=%s reason=blacklist", status_key, matched)
    return {
        "mode": config.deploy.mode.strip().lower(),
        status_key: "skipped",
        "reason": "blacklist",
        "matched_domain": matched,
        "message": message,
        "retry_count": 0,
    }


def _certificate_policy_skip(config, domains: list[str], status_key: str, *, automatic: bool = False) -> dict[str, Any] | None:
    """签发与续签入口统一应用证书策略，手动部署保留独立入口。"""
    for domain in domains:
        skipped = certificate_operation_skip(config, domain, automatic=automatic)
        if skipped is None:
            continue
        reason, message = skipped
        LOGGER.info("证书策略跳过: domain=%s automatic=%s reason=%s", domain, automatic, reason)
        return {
            "mode": config.deploy.mode.strip().lower(), status_key: "skipped",
            "reason": reason, "matched_domain": domain, "message": message, "retry_count": 0,
        }
    return None


def _deployment_provider(config, provider_name: str | None) -> str | None:
    """静态域名账号只负责录入，不能作为云产品部署账号过滤条件。"""
    for provider in config.get("providers", []):
        if provider.name == provider_name and provider.type == "custom":
            return None
    return provider_name


async def issue_and_plan_deploy(
    domain: str,
    domains: list[str],
    provider_name: Optional[str] = None,
) -> dict[str, Any]:
    """首次签发证书后按配置执行部署流程。"""
    if not domain:
        raise ValueError("domain 不能为空")

    config = load_raw_config()
    deploy_mode = config.deploy.mode.strip().lower()
    domain_key = str(domain).strip()
    issue_domains = prepare_issue_domains(domain_key, domains)
    skipped = _blacklist_skip(config, issue_domains, "issue_status")
    if skipped is not None:
        skipped["domains"] = issue_domains
        return skipped
    skipped = _certificate_policy_skip(config, issue_domains, "issue_status")
    if skipped is not None:
        skipped["domains"] = issue_domains
        return skipped
    dd = notifier.DingDingNotifier()

    try:
        issued_cert = await issue_certificate(
            domain=issue_domains[0],
            sans=issue_domains[1:],
            provider_name=provider_name,
        )
        LOGGER.info(
            "证书签发成功: domain=%s provider=%s domains=%s",
            domain_key,
            provider_name,
            issue_domains,
        )
    except Exception as exc:
        error = str(exc)
        LOGGER.warning(
            "证书签发失败: domain=%s provider=%s error=%s",
            domain_key,
            provider_name,
            error,
        )
        try:
            await dd.notify_issue_result(
                domain=domain_key,
                success=False,
                message=f"证书签发失败: {error}",
                new_expires_at=None,
            )
        except Exception as notify_exc:
            LOGGER.warning("签发失败通知发送失败: domain=%s error=%s", domain_key, notify_exc)
        return {
            "mode": deploy_mode,
            "issue_status": "failed",
            "domains": issue_domains,
            "error": error,
        }

    deploy_service = DeployService()
    try:
        if deploy_mode == "apply":
            result = await deploy_service.apply_deploy(
                renewed_cert=issued_cert,
                provider_name=_deployment_provider(config, provider_name),
            )
        else:
            result = await deploy_service.plan_dry_run(
                renewed_cert=issued_cert,
                provider_name=_deployment_provider(config, provider_name),
            )
    except Exception as exc:
        error = str(exc)
        action = "部署" if deploy_mode == "apply" else "部署预演"
        message = f"证书已签发，但{action}失败: {error}"
        LOGGER.warning("%s domain=%s", message, domain_key)
        try:
            await dd.notify_issue_result(
                domain=domain_key,
                success=False,
                message=message,
                new_expires_at=getattr(issued_cert, "expires_at", None),
                issued=True,
            )
        except Exception as notify_exc:
            LOGGER.warning("签发部署失败通知发送失败: domain=%s error=%s", domain_key, notify_exc)
        failure_result = dict(exc.result) if isinstance(exc, DeploymentError) else {"deploy_status": "failed"}
        failure_result.update({
            "mode": deploy_mode,
            "issue_status": "deploy_failed",
            "domains": issue_domains,
            "error": error,
        })
        return failure_result

    payload = result if isinstance(result, dict) else {}
    payload.setdefault("mode", deploy_mode)
    payload.setdefault("deploy_status", "success" if deploy_mode == "apply" else "planned")
    payload["issue_status"] = "success"
    payload["domains"] = issue_domains
    try:
        message = format_deploy_notify_message(payload, issue_domains[0])
        await dd.notify_issue_result(
            domain=issue_domains[0],
            success=True,
            message=message,
            new_expires_at=issued_cert.expires_at,
        )
    except Exception as exc:
        LOGGER.warning("签发结果通知发送失败: domain=%s error=%s", domain_key, exc)
    return payload


async def renew_and_plan_deploy(
    domain: str,
    provider_name: Optional[str] = None,
    force: bool = False,
    *,
    automatic: bool = False,
    processed_certificates: set[str] | None = None,
) -> dict[str, Any]:
    """续签证书并部署；自动调用始终遵守冷却与部署失败保护。"""
    if not domain:
        raise ValueError("domain 不能为空")

    config = load_raw_config()
    deploy_mode = config.deploy.mode.strip().lower()
    domain_key = str(domain).strip()
    skipped = _blacklist_skip(config, [domain_key], "renew_status")
    if skipped is not None:
        return skipped
    skipped = _certificate_policy_skip(config, [domain_key], "renew_status", automatic=automatic)
    if skipped is not None:
        return skipped
    try:
        effective_domain = await resolve_renew_domain(domain_key)
    except Exception as exc:
        error = str(exc)
        LOGGER.error("续签证书解析失败，停止当前目标: requested=%s error=%s", domain_key, error)
        store.RenewStateStore().record_failure(
            domain=domain_key.lower().rstrip("."),
            provider=str(provider_name or ""),
            message=error,
        )
        try:
            await notifier.DingDingNotifier().notify_renew_result(
                domain=domain_key,
                success=False,
                message=f"续签证书解析失败: {error}",
                new_expires_at=None,
            )
        except Exception as notify_exc:
            LOGGER.warning("续签解析失败通知发送失败: domain=%s error=%s", domain_key, notify_exc)
        return {
            "mode": deploy_mode,
            "renew_status": "failed",
            "retry_count": 0,
            "error": error,
        }
    skipped = _blacklist_skip(config, [effective_domain], "renew_status")
    if skipped is not None:
        return skipped
    skipped = _certificate_policy_skip(config, [effective_domain], "renew_status", automatic=automatic)
    if skipped is not None:
        return skipped
    state_key = effective_domain.lower().rstrip(".")
    if processed_certificates is not None:
        if state_key in processed_certificates:
            message = f"本轮已处理证书 {effective_domain}，跳过重复域名"
            LOGGER.info("%s: requested=%s", message, domain_key)
            return {
                "mode": deploy_mode, "renew_status": "skipped",
                "reason": "duplicate_certificate", "message": message, "retry_count": 0,
            }
        processed_certificates.add(state_key)
    LOGGER.info("续签证书已确定: requested=%s main_domain=%s", domain_key, effective_domain)

    renew_state = store.RenewStateStore()
    dd = notifier.DingDingNotifier()
    now_utc = TimeUtil.now_utc()

    # 自动流程仅强制执行续签命令；只有手工强制操作可以绕过状态保护。
    respect_state = automatic or not force
    # 冷却窗口：成功部署后，在 deploy.renew_cooldown_days 内跳过自动续签。
    cooldown_days = int(getattr(config.deploy, "renew_cooldown_days", 7) or 7)
    state = renew_state.get(state_key)
    if (
        respect_state
        and state
        and state.get("last_status") == "success"
        and state.get("last_success_at")
    ):
        try:
            last_success = TimeUtil.parse(state["last_success_at"])
            if TimeUtil.add_days(last_success, cooldown_days) > now_utc:
                LOGGER.info(
                    "跳过续签：命中冷却窗口 domain=%s cooldown_days=%s last_success_at=%s",
                    domain_key,
                    cooldown_days,
                    state["last_success_at"],
                )
                return {
                    "mode": deploy_mode,
                    "renew_status": "skipped",
                    "retry_count": 0,
                    "cooldown_days": cooldown_days,
                }
        except Exception as exc:
            LOGGER.warning("解析续签冷却时间失败，忽略冷却窗口继续续签: %s", exc)

    max_retries = int(getattr(config.deploy, "max_renew_retries", 5) or 5)
    if max_retries <= 0:
        max_retries = 1

    retry_interval_seconds = int(
        getattr(config.deploy, "renew_retry_interval_seconds", 0) or 0
    )
    if retry_interval_seconds < 0:
        retry_interval_seconds = 0

    attempts = 0
    last_error: Optional[str] = None
    renewed_cert = None

    # 如果上一次是“续签成功但部署失败”，等待用户通过仅部署入口处理。
    if (
        respect_state
        and state
        and state.get("last_status") == "deploy_failed"
    ):
        LOGGER.warning(
            "证书已续签但部署失败，等待人工处理: domain=%s provider=%s last_fail_at=%s",
            domain_key,
            provider_name,
            state.get("last_fail_at", ""),
        )
        return {
            "mode": deploy_mode,
            "renew_status": "deploy_failed_pending",
            "retry_count": 0,
            "error": state.get("last_message", "部署失败，等待人工处理"),
        }

    # 证书已确定，命令重试始终复用本轮主域名。
    if renewed_cert is None:
        # 续签重试：失败时在当前调用内最多尝试 max_retries 次。
        for idx in range(max_retries):
            attempts = idx + 1
            try:
                renewed_cert = await renew_certificate(
                    domain=domain_key,
                    provider_name=provider_name,
                    force=force,
                    main_domain=effective_domain,
                )
                LOGGER.info(
                    "证书续签成功: domain=%s provider=%s attempts=%s",
                    domain_key,
                    provider_name,
                    attempts,
                )
                break
            except Exception as exc:
                last_error = str(exc)
                renew_state.record_failure(
                    domain=state_key,
                    provider=str(provider_name or ""),
                    message=last_error,
                )
                LOGGER.warning(
                    "证书续签失败，将重试: domain=%s provider=%s attempts=%s/%s error=%s",
                    domain_key,
                    provider_name,
                    attempts,
                    max_retries,
                    last_error,
                )
                if (
                    retry_interval_seconds > 0
                    and idx < max_retries - 1
                ):
                    LOGGER.info(
                        "证书续签失败，等待 %s 秒后重试: domain=%s provider=%s",
                        retry_interval_seconds,
                        domain_key,
                        provider_name,
                    )
                    await asyncio.sleep(retry_interval_seconds)

    if renewed_cert is None:
        # 重试上限后仍失败，发送钉钉告警并返回失败结果，不进入部署流程。
        try:
            await dd.notify_renew_result(
                domain=effective_domain,
                success=False,
                message=f"证书续签失败，已重试 {attempts} 次: {last_error or '未知错误'}",
                new_expires_at=None,
            )
        except Exception as notify_exc:
            LOGGER.warning("续签失败通知发送失败: domain=%s error=%s", domain_key, notify_exc)
        return {
            "mode": deploy_mode,
            "renew_status": "failed",
            "retry_count": attempts,
            "error": last_error or "renew failed",
        }

    deploy_service = DeployService()
    try:
        if deploy_mode == "apply":
            result = await deploy_service.apply_deploy(
                renewed_cert=renewed_cert,
                provider_name=_deployment_provider(config, provider_name),
            )
        else:
            result = await deploy_service.plan_dry_run(
                renewed_cert=renewed_cert,
                provider_name=_deployment_provider(config, provider_name),
            )
    except Exception as exc:
        # 部署阶段失败：续签本身已成功，但整体流程失败。
        action = "部署" if deploy_mode == "apply" else "部署预演"
        error_msg = f"证书已续期成功，但{action}失败: {exc}"
        if deploy_mode == "apply":
            renew_state.record_deploy_failure(
                domain=state_key,
                provider=str(provider_name or ""),
                message=error_msg,
            )
        # 通知失败不能覆盖部署失败结果；预演也不能修改待人工处理状态。
        try:
            await dd.notify_renew_result(
                domain=effective_domain,
                success=False,
                message=error_msg,
                new_expires_at=getattr(renewed_cert, "expires_at", None),
            )
        except Exception as notify_exc:
            LOGGER.warning("部署失败通知发送失败: domain=%s error=%s", domain_key, notify_exc)
        failure_result = dict(exc.result) if isinstance(exc, DeploymentError) else {"deploy_status": "failed"}
        failure_result.update({
            "mode": deploy_mode,
            "renew_status": "deploy_failed",
            "retry_count": attempts,
            "error": str(exc),
        })
        return failure_result

    # 只有真实部署全部成功才进入冷却；预演不覆盖待人工处理等既有状态。
    if deploy_mode == "apply":
        renew_state.record_success(
            domain=state_key,
            provider=str(provider_name or ""),
            message="renew and deploy success",
        )

    result.setdefault("mode", deploy_mode)
    result.setdefault("deploy_status", "success" if deploy_mode == "apply" else "planned")
    # 续签完成通知：预演明确说明尚未部署。
    try:
        msg = format_deploy_notify_message(
            result if isinstance(result, dict) else None,
            effective_domain,
        )
        await dd.notify_renew_result(
            domain=effective_domain,
            success=True,
            message=msg,
            new_expires_at=renewed_cert.expires_at,
        )
    except Exception as exc:
        LOGGER.warning("续签结果通知发送失败: domain=%s error=%s", domain_key, exc)

    result["renew_status"] = "success"
    result["retry_count"] = attempts
    return result


async def deploy_existing_only(
    domain: str,
    provider_name: Optional[str] = None,
) -> dict[str, Any]:
    """仅基于已有证书触发多云部署（不执行续签）。"""
    if not domain:
        raise ValueError("domain 不能为空")

    config = load_raw_config()
    deploy_mode = config.deploy.mode.strip().lower()
    domain_key = str(domain).strip()
    skipped = _blacklist_skip(config, [domain_key], "renew_status")
    if skipped is not None:
        return skipped
    effective_domain = domain_key
    dd = None
    try:
        policy = get_certificate_policy(config, domain_key)
        if policy.source == "acme":
            effective_domain = await resolve_renew_domain(domain_key)
        skipped = _blacklist_skip(config, [effective_domain], "renew_status")
        if skipped is not None:
            return skipped
        LOGGER.info("已有证书已确定: requested=%s main_domain=%s", domain_key, effective_domain)
        dd = notifier.DingDingNotifier()
        renewed_cert = await load_existing_certificate(
            domain=domain_key,
            provider_name=provider_name,
            main_domain=effective_domain,
        )

        deploy_service = DeployService()
        if deploy_mode == "apply":
            result = await deploy_service.apply_deploy(
                renewed_cert=renewed_cert,
                provider_name=_deployment_provider(config, provider_name),
            )
        else:
            result = await deploy_service.plan_dry_run(
                renewed_cert=renewed_cert,
                provider_name=_deployment_provider(config, provider_name),
            )
    except Exception as exc:
        error_msg = f"证书部署失败: {exc}"
        LOGGER.error("仅部署失败: domain=%s error=%s", domain_key, exc)
        try:
            if dd is None:
                dd = notifier.DingDingNotifier()
            notify_args = {"domain": effective_domain, "success": False, "message": error_msg}
            if deploy_mode == "dry-run":
                notify_args["dry_run"] = True
            await dd.notify_deploy_summary(**notify_args)
        except Exception as notify_exc:
            LOGGER.warning(
                "仅部署失败通知发送失败: domain=%s error=%s",
                domain_key,
                notify_exc,
            )
        raise

    result.setdefault("mode", deploy_mode)
    result.setdefault("deploy_status", "success" if deploy_mode == "apply" else "planned")
    try:
        msg = format_deploy_notify_message(
            result if isinstance(result, dict) else None,
            effective_domain,
        )
        notify_args = {"domain": effective_domain, "success": True, "message": msg}
        if deploy_mode == "dry-run":
            notify_args["dry_run"] = True
        await dd.notify_deploy_summary(**notify_args)
        LOGGER.info("仅部署成功通知已发送: domain=%s mode=%s", domain_key, deploy_mode)
    except Exception as exc:
        LOGGER.warning("仅部署结果通知发送失败: domain=%s error=%s", domain_key, exc)

    if isinstance(result, dict):
        if deploy_mode == "apply":
            store.RenewStateStore().record_success(
                domain=effective_domain.lower(),
                provider=str(provider_name or ""),
                message="manual deploy success",
            )
        result.setdefault("mode", deploy_mode)
        result.setdefault("renew_status", "deploy_only")
        result.setdefault("retry_count", 0)
    return result
