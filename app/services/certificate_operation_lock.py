"""云账号证书上传、部署与清理共用的进程内互斥锁。"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from typing import AsyncIterator, Iterable


_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}


def get_certificate_operation_lock(provider_type: str, provider_name: str) -> asyncio.Lock:
    key = (str(provider_type).strip().lower(), str(provider_name).strip())
    if not key[0] or not key[1]:
        raise ValueError("证书操作锁需要 provider_type 和 provider_name")
    lock = _LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[key] = lock
    return lock


@asynccontextmanager
async def certificate_operation_lock(
    provider_type: str,
    provider_name: str,
) -> AsyncIterator[None]:
    lock = get_certificate_operation_lock(provider_type, provider_name)
    async with lock:
        yield


@asynccontextmanager
async def certificate_operation_locks(
    providers: Iterable[tuple[str, str]],
) -> AsyncIterator[None]:
    """按稳定顺序获取多账号锁，避免并发部署发生死锁。"""
    keys = sorted(
        {
            (str(provider_type).strip().lower(), str(provider_name).strip())
            for provider_type, provider_name in providers
        }
    )
    async with AsyncExitStack() as stack:
        for provider_type, provider_name in keys:
            await stack.enter_async_context(
                certificate_operation_lock(provider_type, provider_name)
            )
        yield
