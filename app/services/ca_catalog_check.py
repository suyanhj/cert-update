"""周期核对官方 CA，保留本地信任目录并持久化检查和通知状态。"""

import asyncio
from datetime import datetime, timedelta, timezone
import json

from cryptography.exceptions import InvalidSignature

from app.config import load_raw_config
from app.schemas.config import CACatalogCheckConfig
from app.utils import certificate_chain as chain
from app.utils.logger import get_logger
from app.utils.notifier import DingDingNotifier
from app.utils.store import StoreBase

LOGGER = get_logger("app.ca_catalog_check")
_check_lock = asyncio.Lock()


class CACatalogCheckStore(StoreBase):
    """检查时间与已发送通知一起持久化，避免重启后重复下载和通知。"""

    def __init__(self, path="data/ca_catalog_check.json"):
        self.state = {}
        super().__init__(path)

    def _load_from_raw(self, raw):
        if not isinstance(raw, dict):
            raise ValueError("CA 检查状态必须是对象")
        self.state = raw

    def _dump_to_raw(self):
        return self.state

    def save(self):
        self._persist()


def audit_catalog(options: CACatalogCheckConfig, now: datetime) -> dict:
    """核对已有条目；下载复用固定官方地址、大小限制、CA 身份及指纹校验。"""
    certificates, details, issues = {}, {}, []
    with chain.httpx.Client() as client:
        for name, entry in chain.CA_CATALOG.items():
            try:
                cert = chain._download_ca(client, name, options.timeout_seconds)
            except chain.CertificateChainError as exc:
                issues.append(f"{name}: 核对失败: {exc}")
                continue
            certificates[name] = cert
            details[name] = {
                "url": entry["url"], "sha256": chain._fingerprint(cert),
                "not_before": cert.not_valid_before_utc.isoformat(),
                "not_after": cert.not_valid_after_utc.isoformat(),
            }
            if now < cert.not_valid_before_utc:
                issues.append(f"{name}: 尚未生效: {cert.not_valid_before_utc.isoformat()}")
            elif now >= cert.not_valid_after_utc:
                issues.append(f"{name}: 已过期: {cert.not_valid_after_utc.isoformat()}")
            elif cert.not_valid_after_utc <= now + timedelta(days=options.warn_days):
                issues.append(f"{name}: 即将过期: {cert.not_valid_after_utc.isoformat()}")
    edges = set()
    for path in chain.CA_PATHS.values():
        edges.update(zip(path, path[1:]))
        edges.add((path[-1], path[-1]))
    for child, parent in sorted(edges):
        if child not in certificates or parent not in certificates:
            continue
        try:
            certificates[child].verify_directly_issued_by(certificates[parent])
        except (ValueError, TypeError, InvalidSignature) as exc:
            issues.append(f"{child} -> {parent}: 签名关系无效: {type(exc).__name__}")
    issues.sort()
    LOGGER.info("官方 CA 核对结束: total=%s verified=%s issues=%s",
                len(chain.CA_CATALOG), len(details), len(issues))
    for issue in issues:
        LOGGER.warning("官方 CA 核对: %s", issue)
    return {"checked_at": now.isoformat(), "certificates": details, "issues": issues}


async def check_ca_catalog_if_due():
    """由现有调度调用；网络检查在线程池执行，通知失败下一轮重试。"""
    async with _check_lock:
        options = CACatalogCheckConfig.model_validate(load_raw_config().get("ca_catalog_check", {}))
        if not options.enabled:
            return
        store = CACatalogCheckStore()
        now = datetime.now(timezone.utc)
        report = store.state.get("report")
        last = datetime.fromisoformat(report["checked_at"]) if report else None
        if last is None or now - last >= timedelta(hours=options.interval_hours):
            report = await asyncio.to_thread(audit_catalog, options, now)
            store.state["report"] = report
            store.save()
        signature = json.dumps(report["issues"], ensure_ascii=False)
        previous = store.state.get("notified_signature", "[]")
        if signature == previous:
            return
        title = "官方 CA 目录核对异常" if report["issues"] else "官方 CA 目录核对恢复正常"
        body = "\n".join(report["issues"]) or "已有 CA 的官方指纹、有效期和链签名检查通过。"
        await DingDingNotifier().notify(title, body)
        store.state["notified_signature"] = signature
        store.save()
