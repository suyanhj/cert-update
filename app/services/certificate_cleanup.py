"""云端过期证书清理的失败关闭编排。"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import asyncio
import json
from typing import Any, Iterable, Sequence

from app.certificate_cleanup.base import (
    AssociationStatus,
    CertificateCleanupAdapter,
    CertificateSource,
    CleanupRunSummary,
    DeleteOutcome,
    IncompleteInventoryError,
    RemoteCertificate,
    classify_inventory_unavailable,
)
from app.certificate_cleanup.store import (
    CertificateCleanupStore,
    sanitize_cleanup_data,
    sanitize_cleanup_text,
)
from app.certificate_cleanup.factory import build_certificate_cleanup_adapters
from app.config import load_raw_config
from app.schemas.config import CertificateCleanupConfig
from app.services.certificate_operation_lock import certificate_operation_lock
from app.utils.logger import get_logger
from app.utils.notifier import DingDingNotifier


LOGGER = get_logger("app.certificate_cleanup")
_cleanup_lock = asyncio.Lock()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def is_expired_beyond_grace(
    certificate: RemoteCertificate,
    now: datetime,
    grace_days: int,
) -> bool:
    """判断证书是否已超过到期保留期；只使用远端真实到期时间。"""
    if now.tzinfo is None:
        raise ValueError("清理基准时间必须包含时区")
    return certificate.expires_at <= now - timedelta(days=grace_days)


def is_deletable_source(source: CertificateSource) -> bool:
    """只有用户上传或本系统管理的副本允许进入删除候选。"""
    return source.allows_delete


def cleanup_summary_to_dict(summary: CleanupRunSummary) -> dict[str, Any]:
    raw = asdict(summary)
    raw["store_id"] = asdict(summary.store_id)
    return sanitize_cleanup_data(raw)


def _candidate_key(adapter: CertificateCleanupAdapter, certificate_id: str) -> str:
    return f"{adapter.store_id.key}:{certificate_id}"


def _candidate_record(
    adapter: CertificateCleanupAdapter,
    certificate: RemoteCertificate,
    now: datetime,
    existing: dict[str, Any] | None,
) -> dict[str, Any]:
    stable = bool(
        existing
        and existing.get("expires_at") == certificate.expires_at.isoformat()
        and existing.get("source") == certificate.source.value
    )
    return {
        "store_key": adapter.store_id.key,
        "provider_type": adapter.store_id.provider_type,
        "provider_name": adapter.store_id.provider_name,
        "store": adapter.store_id.store,
        "scope": adapter.store_id.scope,
        "certificate_id": certificate.certificate_id,
        "name": certificate.name,
        "expires_at": certificate.expires_at.isoformat(),
        "source": certificate.source.value,
        "first_seen_at": (
            str(existing.get("first_seen_at")) if stable else now.isoformat()
        ),
        "last_seen_at": now.isoformat(),
        "confirm_count": int(existing.get("confirm_count", 0)) + 1 if stable else 1,
    }


def _reset_store_candidates(
    candidates: dict[str, dict[str, Any]],
    store_key: str,
) -> None:
    for key in [
        key
        for key, value in candidates.items()
        if value.get("store_key") == store_key
    ]:
        candidates.pop(key, None)


def _validate_inventory(certificates: Sequence[RemoteCertificate]) -> None:
    seen: set[str] = set()
    for certificate in certificates:
        if certificate.certificate_id in seen:
            raise IncompleteInventoryError(
                f"证书列表包含重复 ID，无法证明分页完整: {certificate.certificate_id}"
            )
        seen.add(certificate.certificate_id)


async def _scan_store(
    adapter: CertificateCleanupAdapter,
    options: CertificateCleanupConfig,
    store: CertificateCleanupStore,
    now: datetime,
) -> tuple[CleanupRunSummary, list[dict[str, Any]]]:
    summary = CleanupRunSummary(store_id=adapter.store_id)
    candidates = store.get_candidates()
    store_key = adapter.store_id.key
    new_keys: set[str] = set()
    plans: list[dict[str, Any]] = []

    try:
        certificates = list(await adapter.list_certificates())
        _validate_inventory(certificates)
        summary.scanned = len(certificates)
        for certificate in certificates:
            key = _candidate_key(adapter, certificate.certificate_id)
            if not is_expired_beyond_grace(
                certificate,
                now=now,
                grace_days=options.expired_grace_days,
            ):
                continue

            association = await adapter.check_associations(certificate.certificate_id)
            if not association.complete or association.status is AssociationStatus.UNKNOWN:
                if not adapter.safe_delete:
                    summary.report_only += 1
                    summary.items.append(
                        {
                            "certificate_id": certificate.certificate_id,
                            "action": "association_unknown_report_only",
                            "source": certificate.source.value,
                        }
                    )
                    continue
                raise IncompleteInventoryError(
                    f"证书关联资源查询不完整: certificate_id={certificate.certificate_id}"
                )
            if association.status is AssociationStatus.BOUND:
                summary.bound += 1
                summary.items.append(
                    {
                        "certificate_id": certificate.certificate_id,
                        "action": "bound",
                        "resource_count": str(len(association.resources)),
                    }
                )
                continue
            if not is_deletable_source(certificate.source):
                summary.report_only += 1
                summary.items.append(
                    {
                        "certificate_id": certificate.certificate_id,
                        "action": "source_report_only",
                        "source": certificate.source.value,
                    }
                )
                continue

            record = _candidate_record(adapter, certificate, now, candidates.get(key))
            candidates[key] = record
            new_keys.add(key)
            summary.candidates += 1
            if int(record.get("confirm_count", 0)) >= options.confirm_scans:
                summary.confirmed += 1
            plans.append(record)

        for key in [
            key
            for key, value in candidates.items()
            if value.get("store_key") == store_key and key not in new_keys
        ]:
            candidates.pop(key, None)
        store.set_candidates(candidates)
        store.save()
        return summary, plans
    except Exception as exc:
        safe_error = sanitize_cleanup_text(str(exc))
        unavailable_reason = classify_inventory_unavailable(exc)
        _reset_store_candidates(candidates, store_key)
        store.set_candidates(candidates)
        store.save()
        summary.error = safe_error
        if unavailable_reason:
            summary.unavailable = True
            summary.unavailable_reason = unavailable_reason
            LOGGER.warning(
                "云证书库能力不可用，已安全跳过且删除计划清零: store=%s reason=%s error=%s",
                store_key,
                unavailable_reason,
                safe_error,
            )
        else:
            summary.failed = True
            LOGGER.error(
                "云证书库扫描失败关闭，删除计划已清零: store=%s error=%s",
                store_key,
                safe_error,
            )
        return summary, []


def _delete_allowed(
    adapter: CertificateCleanupAdapter,
    options: CertificateCleanupConfig,
) -> bool:
    scopes = {
        (item.provider_type, item.provider_name, item.store)
        for item in options.delete_scopes
    }
    return (
        options.mode == "delete"
        and adapter.safe_delete
        and adapter.store_id.delete_scope_key in scopes
    )


async def _delete_candidate(
    adapter: CertificateCleanupAdapter,
    record: dict[str, Any],
    summary: CleanupRunSummary,
    store: CertificateCleanupStore,
) -> bool:
    """在账号锁内完成实时复查、平台安全删除和删除后验证。"""
    certificate_id = str(record["certificate_id"])
    key = _candidate_key(adapter, certificate_id)
    async with certificate_operation_lock(
        adapter.store_id.provider_type,
        adapter.store_id.provider_name,
    ):
        current = await adapter.get_certificate(certificate_id)
        if current is None:
            candidates = store.get_candidates()
            candidates.pop(key, None)
            store.set_candidates(candidates)
            store.save()
            summary.items.append(
                {"certificate_id": certificate_id, "action": "already_absent"}
            )
            return True
        if (
            current.expires_at.isoformat() != record.get("expires_at")
            or current.source.value != record.get("source")
            or not current.source.allows_delete
        ):
            candidates = store.get_candidates()
            candidates.pop(key, None)
            store.set_candidates(candidates)
            store.save()
            summary.items.append(
                {"certificate_id": certificate_id, "action": "metadata_changed"}
            )
            return True

        association = await adapter.check_associations(certificate_id)
        if not association.complete or association.status is AssociationStatus.UNKNOWN:
            raise IncompleteInventoryError(
                f"删除前关联复查不完整: certificate_id={certificate_id}"
            )
        if association.status is AssociationStatus.BOUND:
            candidates = store.get_candidates()
            candidates.pop(key, None)
            store.set_candidates(candidates)
            store.save()
            summary.bound += 1
            summary.items.append(
                {"certificate_id": certificate_id, "action": "newly_bound"}
            )
            return True

        receipt = await adapter.delete_certificate(certificate_id)
        if receipt.outcome is DeleteOutcome.IN_USE:
            candidates = store.get_candidates()
            candidates.pop(key, None)
            store.set_candidates(candidates)
            store.save()
            summary.bound += 1
            summary.items.append(
                {"certificate_id": certificate_id, "action": "provider_in_use"}
            )
            return True
        if receipt.outcome not in {DeleteOutcome.DELETED, DeleteOutcome.NOT_FOUND}:
            raise RuntimeError(
                f"平台返回未知删除结果: certificate_id={certificate_id} outcome={receipt.outcome}"
            )
        if await adapter.get_certificate(certificate_id) is not None:
            raise RuntimeError(f"删除后证书仍存在: certificate_id={certificate_id}")

        candidates = store.get_candidates()
        candidates.pop(key, None)
        store.set_candidates(candidates)
        store.save()
        summary.deleted += 1
        summary.items.append(
            {
                "certificate_id": certificate_id,
                "action": "deleted",
                "request_id": receipt.request_id,
                "task_id": receipt.task_id,
            }
        )
        LOGGER.info(
            "云端过期证书已安全删除: store=%s certificate_id=%s request_id=%s task_id=%s",
            adapter.store_id.key,
            certificate_id,
            receipt.request_id or "-",
            receipt.task_id or "-",
        )
        return True


async def run_certificate_cleanup(
    options: CertificateCleanupConfig,
    adapters: Iterable[CertificateCleanupAdapter],
    store: CertificateCleanupStore,
    *,
    now: datetime | None = None,
) -> list[CleanupRunSummary]:
    """扫描所有独立证书库，并在满足全部门禁后执行有限删除。"""
    started_at = now or utc_now()
    if started_at.tzinfo is None:
        raise ValueError("清理运行时间必须包含时区")

    adapter_list = list(adapters)
    summaries: dict[str, CleanupRunSummary] = {}
    plans: list[tuple[CertificateCleanupAdapter, dict[str, Any]]] = []
    for adapter in adapter_list:
        summary, store_plans = await _scan_store(adapter, options, store, started_at)
        summaries[adapter.store_id.key] = summary
        if not summary.failed and not summary.unavailable and _delete_allowed(adapter, options):
            plans.extend(
                (adapter, record)
                for record in store_plans
                if int(record.get("confirm_count", 0)) >= options.confirm_scans
            )

    plans.sort(key=lambda item: (str(item[1].get("expires_at", "")), item[0].store_id.key))
    executable = plans[: options.max_deletes_per_run]
    for adapter, _record in plans[options.max_deletes_per_run :]:
        summaries[adapter.store_id.key].deferred += 1

    stopped_stores: set[str] = set()
    for adapter, record in executable:
        store_key = adapter.store_id.key
        if store_key in stopped_stores:
            summaries[store_key].deferred += 1
            continue
        summary = summaries[store_key]
        try:
            await _delete_candidate(adapter, record, summary, store)
        except Exception as exc:
            safe_error = sanitize_cleanup_text(str(exc))
            stopped_stores.add(store_key)
            summary.failed = True
            summary.error = safe_error
            summary.items.append(
                {
                    "certificate_id": str(record.get("certificate_id", "")),
                    "action": "delete_failed_closed",
                    "error": safe_error,
                }
            )
            LOGGER.error(
                "云证书删除失败关闭，本库停止后续删除: store=%s certificate_id=%s error=%s",
                store_key,
                record.get("certificate_id", ""),
                safe_error,
            )

    result = [summaries[adapter.store_id.key] for adapter in adapter_list]
    for summary in result:
        store.append_run(
            {
                "started_at": started_at.isoformat(),
                **cleanup_summary_to_dict(summary),
            }
        )
    store.save()
    return result


def _notification_payload(summaries: Sequence[CleanupRunSummary]) -> tuple[str, str, str]:
    material = [
        cleanup_summary_to_dict(item)
        for item in summaries
        if item.failed
        or item.candidates
        or item.confirmed
        or item.deleted
        or item.bound
        or item.report_only
        or item.deferred
    ]
    signature = json.dumps(material, ensure_ascii=False, sort_keys=True)
    failed = any(item.failed for item in summaries)
    deleted = sum(item.deleted for item in summaries)
    title = "云端证书清理异常" if failed else (
        f"云端证书清理完成（删除 {deleted} 张）" if deleted else "云端证书清理报告"
    )
    lines: list[str] = []
    for item in summaries:
        if not (
            item.failed
            or item.candidates
            or item.confirmed
            or item.deleted
            or item.bound
            or item.report_only
            or item.deferred
        ):
            continue
        state = "失败关闭" if item.failed else "完成"
        lines.append(
            f"- {item.store_id.provider_type}/{item.store_id.provider_name}/"
            f"{item.store_id.store}/{item.store_id.scope}: {state}，"
            f"扫描 {item.scanned}，候选 {item.candidates}，已确认 {item.confirmed}，删除 {item.deleted}，"
            f"关联跳过 {item.bound}，仅报告 {item.report_only}，延后 {item.deferred}"
        )
        if item.error:
            lines.append(f"  原因：{item.error}")
    return title, sanitize_cleanup_text("\n".join(lines)), signature


async def cleanup_if_due(
    *,
    store: CertificateCleanupStore | None = None,
    notifier: DingDingNotifier | None = None,
    now: datetime | None = None,
) -> list[CleanupRunSummary] | None:
    """按持久化周期执行清理；禁用或未到期时不创建云客户端。"""
    async with _cleanup_lock:
        conf = load_raw_config()
        options = CertificateCleanupConfig.model_validate(
            dict(conf.certificate_cleanup)
        )
        if not options.enabled:
            return None

        state_store = store or CertificateCleanupStore()
        pending = state_store.state.get("pending_notification")
        if isinstance(pending, dict):
            await (notifier or DingDingNotifier()).notify(
                str(pending.get("title") or "云端证书清理通知"),
                sanitize_cleanup_text(str(pending.get("body") or "")),
            )
            state_store.state["notified_signature"] = str(
                pending.get("signature") or ""
            )
            state_store.state["pending_notification"] = None
            state_store.save()

        current = now or utc_now()
        last_text = str(state_store.state.get("last_checked_at") or "").strip()
        if last_text:
            try:
                last_checked = datetime.fromisoformat(last_text)
            except ValueError:
                last_checked = None
            if last_checked is not None and last_checked.tzinfo is None:
                last_checked = None
            if last_checked is not None and current - last_checked < timedelta(
                hours=options.interval_hours
            ):
                return None

        adapters = build_certificate_cleanup_adapters(conf)
        summaries = await run_certificate_cleanup(
            options,
            adapters,
            state_store,
            now=current,
        )
        state_store.state["last_checked_at"] = current.isoformat()
        state_store.save()

        title, body, signature = _notification_payload(summaries)
        previous_signature = str(state_store.state.get("notified_signature") or "")
        if signature == previous_signature:
            return summaries
        if not body and not previous_signature:
            return summaries
        if not body and any(item.unavailable for item in summaries):
            return summaries
        if not body:
            title = "云端证书清理恢复正常"
            body = "本轮未发现过期未关联候选，所有启用证书库扫描完成。"
        state_store.state["pending_notification"] = {
            "title": title,
            "body": body,
            "signature": signature,
        }
        state_store.save()
        await (notifier or DingDingNotifier()).notify(title, body)
        state_store.state["notified_signature"] = signature
        state_store.state["pending_notification"] = None
        state_store.save()
        return summaries
