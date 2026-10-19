"""定时采集调度：按间隔调用 collect_all。"""
import asyncio
from app.services.collector import collect_all
from app.services.ca_catalog_check import check_ca_catalog_if_due
from app.utils.logger import get_logger

logger = get_logger("app")

_scheduler_task: asyncio.Task | None = None
_stop_event = asyncio.Event()


def start_scheduler(interval: int = 60, run_immediately: bool = True):
    global _scheduler_task

    if _scheduler_task and not _scheduler_task.done():
        logger.info("scheduler already running")
        return

    _stop_event.clear()

    async def _wait_interval():
        try:
            await asyncio.wait_for(_stop_event.wait(), timeout=interval * 60)
        except asyncio.TimeoutError:
            pass

    async def _loop():
        logger.info("scheduler started (interval=%s min, run_immediately=%s)", interval, run_immediately)
        try:
            if not run_immediately:
                await _wait_interval()
            while not _stop_event.is_set():
                try:
                    await collect_all()
                except Exception:
                    # 单次失败继续下一轮，停止请求等待当前采集及部署完整结束。
                    logger.exception("collect_all failed, skip this round")
                if not _stop_event.is_set():
                    try:
                        await check_ca_catalog_if_due()
                    except Exception:
                        logger.exception("官方 CA 目录检查失败，下一轮调度继续处理")
                    await _wait_interval()
        finally:
            logger.info("scheduler stopped")

    _scheduler_task = asyncio.create_task(_loop())


async def stop_scheduler():
    global _scheduler_task

    if not _scheduler_task:
        return

    logger.info("停止调度，等待当前采集及证书操作完成...")
    _stop_event.set()

    task = _scheduler_task
    # 调用方取消等待时不取消后台采集，避免遗留仍在执行的续签线程。
    await asyncio.shield(task)
    if _scheduler_task is task:
        _scheduler_task = None

