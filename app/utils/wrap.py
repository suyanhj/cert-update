import asyncio


def prevent_double_click(func):
    """防止异步函数在短时间内被重复点击触发（按参数去重）。"""
    locks = {}

    async def wrapper(*args, **kwargs):
        key = str(args) + str(kwargs)
        lock = locks.setdefault(key, asyncio.Lock())

        if lock.locked():
            return

        async with lock:
            return await func(*args, **kwargs)

    return wrapper

