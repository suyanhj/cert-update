import httpx
from app.config import load_raw_config
from .logger import get_logger

logger = get_logger('app')


BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


async def fetch(method: str = "head", url: str | None = None, ua: str = BROWSER_USER_AGENT):
    """发起 HTTP 请求，失败时抛出异常不静默返回。"""
    timeout = int(load_raw_config().http_timeout_seconds)
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": ua},
    ) as client:
        resp = await client.request(method, url)
        return resp

