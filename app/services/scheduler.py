"""定时采集调度：按间隔调用 collect_all。"""
import asyncio
from app.services.collector import collect_all
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

    async def _loop():
        logger.info("scheduler started (interval=%s min, run_immediately=%s)", interval, run_immediately)
        try:
            first_round = True
            while not _stop_event.is_set():
                if first_round and not run_immediately:
                    try:
                        await asyncio.sleep(interval * 60)
                    except asyncio.CancelledError:
                        pass
                    if _stop_event.is_set():
                        break
                first_round = False

                try:
                    await collect_all()
                except Exception:
                    # 单次失败 ≠ 停调度
                    logger.exception("collect_all failed, skip this round")

                # 可被 cancel 的 sleep
                try:
                    await asyncio.sleep(interval * 60)
                except asyncio.CancelledError:
                    raise

        except asyncio.CancelledError:
            logger.info("scheduler cancelled")
            raise
        finally:
            logger.info("scheduler stopped")

    _scheduler_task = asyncio.create_task(_loop())


async def stop_scheduler():
    global _scheduler_task

    if not _scheduler_task:
        return

    logger.info("stopping scheduler...")
    _stop_event.set()
    _scheduler_task.cancel()

    try:
        await _scheduler_task
    except asyncio.CancelledError:
        pass

    _scheduler_task = None

