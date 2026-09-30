from __future__ import annotations

from app.config import load_raw_config
from app import state
from app.utils import logger, store, time

from .certs import Certificates
from .domains import Domains
from .ecs import Ecs
from .bindings import BindingCacheService
from .renew_flow import renew_and_plan_deploy
import asyncio

logger = logger.get_logger("app")
timeutils = time.TimeUtil()
snapshot_store = store.CollectorSnapshotStore()


def hydrate_state_from_snapshot() -> bool:
    """启动时加载上次采集快照，避免首页冷启动空白。"""
    try:
        payload = snapshot_store.get_all()
    except Exception:
        logger.exception("load collector snapshot failed")
        return False

    domain_groups = payload.get("domain_groups", [])
    cert_list = payload.get("cert_list", [])
    ecs_list = payload.get("ecs_list", [])
    last_updated = payload.get("last_updated", "")

    if not domain_groups and not cert_list and not ecs_list and not last_updated:
        return False

    if isinstance(domain_groups, list):
        state.DOMAIN_GROUPS = domain_groups
    if isinstance(cert_list, list):
        state.CERT_LIST = cert_list
    if isinstance(ecs_list, list):
        state.ECS_LIST = ecs_list
    if last_updated:
        state.LAST_UPDATED = str(last_updated)

    # 允许首轮在已有快照基础上继续证书采集。
    state.DOMAIN_READY.set()
    logger.info(
        "collector snapshot loaded: domains=%s certs=%s ecs=%s last_updated=%s",
        len(state.DOMAIN_GROUPS),
        len(state.CERT_LIST),
        len(state.ECS_LIST),
        state.LAST_UPDATED,
    )
    return True


def persist_state_snapshot() -> None:
    try:
        snapshot_store.save(
            domain_groups=state.DOMAIN_GROUPS,
            cert_list=state.CERT_LIST,
            ecs_list=state.ECS_LIST,
            last_updated=state.LAST_UPDATED,
        )
    except Exception:
        logger.exception("save collector snapshot failed")


async def collect_domains() -> None:
    d = Domains()
    domains_info = await d.discovery_all()

    state.DOMAIN_READY.clear()
    state.DOMAIN_GROUPS = domains_info
    state.DOMAIN_READY.set()
    logger.info("domain discovery done: total=%s", len(state.DOMAIN_GROUPS))


async def collect_ecs_instances() -> None:
    ecs = Ecs()
    ecs_info = await ecs.discovery_all()
    state.ECS_LIST = ecs_info
    logger.info("ecs discovery done: total=%s", len(state.ECS_LIST))


async def collect_ecs_instances_in_thread() -> None:
    """
    在线程池中执行 ECS 采集，避免阻塞事件循环。
    说明：
    - Ecs.discovery_all 内部可能调用同步云 SDK，多账号串行时会占用事件循环较长时间；
    - 这里通过 asyncio.to_thread 在线程中跑原有异步逻辑（内部自建事件循环），
      主事件循环只负责 await 线程结果，不阻塞 NiceGUI 的 WebSocket。
    """

    def _run():
        asyncio.run(collect_ecs_instances())

    await asyncio.to_thread(_run)


async def refresh_product_bindings_cache() -> None:
    # 仅发现并缓存产品绑定，不在此处执行证书部署
    binding_cache_service = BindingCacheService()
    result = await binding_cache_service.refresh_binding_cache()
    logger.info(
        "product binding cache refresh done: providers=%s bindings=%s cache_enabled=%s",
        result.get("refreshed_provider_count", 0),
        result.get("total_bindings", 0),
        result.get("cache_enabled", False),
    )
    failed_providers = result.get("failed_providers", [])
    if failed_providers:
        details = "; ".join(
            f"{item.get('provider', 'unknown')}: {item.get('error', 'unknown error')}"
            for item in failed_providers
        )
        raise RuntimeError(f"产品绑定缓存刷新部分失败: {details}")


async def collect_certificates() -> None:
    c = Certificates()
    await state.DOMAIN_READY.wait()
    certs = await c.get_certificate_list(state.DOMAIN_GROUPS)
    state.CERT_LIST = certs
    await _auto_renew_and_deploy_expiring_certificates(certs)


async def _auto_renew_and_deploy_expiring_certificates(certs: list[dict]) -> None:
    """
    自动续签编排入口：
    - 受 auto_renew_enabled 开关控制
    - 仅处理 days <= auto_renew_days 的证书
    """
    if not certs:
        return

    conf = load_raw_config()
    if not bool(conf.auto_renew_enabled):
        logger.info("auto_renew_enabled=false，跳过自动续签编排")
        return

    try:
        auto_renew_days = int(conf.auto_renew_days)
    except Exception:
        logger.warning(
            "auto_renew_days 非法，跳过自动续签编排: %s",
            conf.auto_renew_days,
        )
        return

    if auto_renew_days <= 0:
        logger.info("auto_renew_days<=0，跳过自动续签编排")
        return

    # 每轮采集按域名去重，避免同一证书被重复触发续签流程。
    seen_domains: set[str] = set()
    triggered = 0
    for cert in certs:
        domain = str(cert.get("domain", "")).strip()
        if not domain:
            continue

        domain_key = domain.lower()
        if domain_key in seen_domains:
            continue
        seen_domains.add(domain_key)

        provider_name = str(cert.get("provider", "")).strip()
        if not provider_name:
            logger.warning("跳过自动续签: 域名 %s 未解析到 provider", domain)
            continue

        days_raw = cert.get("days")
        try:
            days = int(days_raw)
        except Exception:
            logger.warning(
                "跳过自动续签: 证书剩余天数格式非法 domain=%s days=%s",
                domain,
                days_raw,
            )
            continue

        if days > auto_renew_days:
            continue

        logger.info(
            "触发自动续签编排: domain=%s provider=%s days=%s threshold=%s",
            domain,
            provider_name,
            days,
            auto_renew_days,
        )
        try:
            result = await renew_and_plan_deploy(
                domain=domain,
                provider_name=None,
                force=True,
            )
            mode = "unknown"
            if isinstance(result, dict):
                mode = str(result.get("mode", "unknown") or "unknown")
            logger.info(
                "自动续签编排完成: domain=%s provider=%s mode=%s",
                domain,
                provider_name,
                mode,
            )
            triggered += 1
        except Exception:
            logger.exception("自动续签编排失败: domain=%s provider=%s", domain, provider_name)

    logger.info(
        "自动续签编排扫描完成: certs=%s triggered=%s threshold=%s",
        len(certs),
        triggered,
        auto_renew_days,
    )


async def collect_all() -> None:
    logger.info("collect_all START")

    errors = []
    try:
        await collect_domains()
    except Exception as exc:
        errors.append(("domains", exc))
        logger.exception("collect_domains failed")

    # ECS 采集在线程池执行同步 SDK，并等待完成以保证快照属于当前轮次。
    try:
        await collect_ecs_instances_in_thread()
    except Exception as exc:
        errors.append(("ecs_instances", exc))
        logger.exception("collect_ecs_instances failed")

    try:
        await refresh_product_bindings_cache()
    except Exception as exc:
        errors.append(("product_bindings_cache", exc))
        logger.exception("refresh_product_bindings_cache failed")

    try:
        await collect_certificates()
    except Exception as exc:
        errors.append(("certificates", exc))
        logger.exception("collect_certificates failed")

    if errors:
        details = "; ".join(f"{stage}: {exc}" for stage, exc in errors)
        logger.warning("collect_all finished with %d errors: %s", len(errors), details)
        raise RuntimeError(f"采集部分失败: {details}")

    state.LAST_UPDATED = timeutils.now()
    persist_state_snapshot()

    logger.info("collect_all END")
