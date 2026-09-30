"""scheduler 单测：start_scheduler 后 stop_scheduler 不抛错。"""
import asyncio

from app.services.scheduler import start_scheduler, stop_scheduler


def test_scheduler_start_then_stop_no_error():
    async def _run():
        start_scheduler(interval=60, run_immediately=False)
        await stop_scheduler()
        await stop_scheduler()

    asyncio.run(_run())
