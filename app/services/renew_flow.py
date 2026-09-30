from __future__ import annotations

from typing import Any, Optional

import asyncio

from app.config import load_raw_config
from app.utils.logger import get_logger
from app.utils.time import TimeUtil
from app.utils import notifier, store
from app.services.deploy import DeployService

from .renew import (
    issue_certificate,
    load_existing_certificate,
    prepare_issue_domains,
    renew_certificate,
    _resolve_effective_renew_domain,
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
                provider_name=provider_name,
            )
        else:
            result = await deploy_service.plan_dry_run(
                renewed_cert=issued_cert,
                provider_name=provider_name,
            )
    except Exception as exc:
        error = str(exc)
        message = f"证书已签发，但部署失败: {error}"
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
        return {
            "mode": deploy_mode,
            "issue_status": "deploy_failed",
            "domains": issue_domains,
            "error": error,
        }

    payload = result if isinstance(result, dict) else {}
    payload.setdefault("mode", deploy_mode)
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
) -> dict[str, Any]:
    """续签证书后按配置执行部署流程（dry-run 或 apply）。"""
    if not domain:
        raise ValueError("domain 不能为空")

    config = load_raw_config()
    deploy_mode = config.deploy.mode.strip().lower()
    domain_key = str(domain).strip()
    effective_domain = _resolve_effective_renew_domain(domain_key)
    state_key = effective_domain.lower()

    renew_state = store.RenewStateStore()
    dd = notifier.DingDingNotifier()
    now_utc = TimeUtil.now_utc()

    # 冷却窗口：成功续签后，在 deploy.renew_cooldown_days 内再次触发会被跳过（force=True 时忽略）。
    cooldown_days = int(getattr(config.deploy, "renew_cooldown_days", 7) or 7)
    state = renew_state.get(state_key)
    if (
        not force
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

    # 如果上一次是“续签成功但部署失败”，则优先尝试直接加载已有证书，仅重新部署。
    if (
        not force
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

    # 若无法复用已有证书，则按原逻辑执行续签重试。
    if renewed_cert is None:
        # 续签重试：失败时在当前调用内最多尝试 max_retries 次。
        for idx in range(max_retries):
            attempts = idx + 1
            try:
                renewed_cert = await renew_certificate(
                    domain=domain_key,
                    provider_name=provider_name,
                    force=force,
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
        await dd.notify_renew_result(
            domain=effective_domain,
            success=False,
            message=f"证书续签失败，已重试 {attempts} 次: {last_error or '未知错误'}",
            new_expires_at=None,
        )
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
                provider_name=provider_name,
            )
        else:
            result = await deploy_service.plan_dry_run(
                renewed_cert=renewed_cert,
                provider_name=provider_name,
            )
    except Exception as exc:
        # 部署阶段失败：续签本身已成功，但整体流程失败。
        error_msg = f"证书已续期成功，但部署失败: {exc}"
        renew_state.record_deploy_failure(
            domain=state_key,
            provider=str(provider_name or ""),
            message=error_msg,
        )
        # 发送一次汇总告警：说明续签 OK，但部署有错误；不进入冷却窗口。
        await dd.notify_renew_result(
            domain=effective_domain,
            success=False,
            message=error_msg,
            new_expires_at=getattr(renewed_cert, "expires_at", None),
        )
        return {
            "mode": deploy_mode,
            "renew_status": "deploy_failed",
            "retry_count": attempts,
            "error": str(exc),
        }

    # 续签 + 部署均成功后，才记录续签成功状态。
    renew_state.record_success(
        domain=state_key,
        provider=str(provider_name or ""),
        message="renew and deploy success",
    )

    # 续签 + 部署流程整体成功后发送一次成功通知。
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

    if isinstance(result, dict):
        result.setdefault("mode", deploy_mode)
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
    effective_domain = _resolve_effective_renew_domain(domain_key)
    dd = notifier.DingDingNotifier()

    try:
        renewed_cert = await load_existing_certificate(
            domain=domain_key,
            provider_name=provider_name,
        )

        deploy_service = DeployService()
        if deploy_mode == "apply":
            result = await deploy_service.apply_deploy(
                renewed_cert=renewed_cert,
                provider_name=provider_name,
            )
        else:
            result = await deploy_service.plan_dry_run(
                renewed_cert=renewed_cert,
                provider_name=provider_name,
            )
    except Exception as exc:
        error_msg = f"证书部署失败: {exc}"
        LOGGER.error("仅部署失败: domain=%s error=%s", domain_key, exc)
        try:
            await dd.notify_deploy_summary(
                domain=effective_domain,
                success=False,
                message=error_msg,
            )
        except Exception as notify_exc:
            LOGGER.warning(
                "仅部署失败通知发送失败: domain=%s error=%s",
                domain_key,
                notify_exc,
            )
        raise

    try:
        msg = format_deploy_notify_message(
            result if isinstance(result, dict) else None,
            effective_domain,
        )
        await dd.notify_deploy_summary(
            domain=effective_domain,
            success=True,
            message=msg,
        )
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
