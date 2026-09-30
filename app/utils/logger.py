import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
from app.utils.project_root import get_project_root, resolve_project_path


PROJECT_ROOT = get_project_root()
LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

FORMAT = (
    "%(asctime)s | %(levelname)s | "
    "%(name)s | %(filename)s:%(lineno)d | %(message)s"
)

GLOBAL_LOG_LEVEL = logging.INFO
_CONSOLE_ENCODING_READY = False
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


class _ShanghaiFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=SHANGHAI_TZ)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]


def _ensure_utf8_console() -> None:
    global _CONSOLE_ENCODING_READY
    if _CONSOLE_ENCODING_READY:
        return
    _log = logging.getLogger("app.utils.logger")
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception as e:
                _log.warning("Console reconfigure to utf-8 failed: %s", e)
    _CONSOLE_ENCODING_READY = True


def _parse_log_level(level: str | int) -> int:
    if isinstance(level, int):
        return level

    normalized = level.strip().upper()
    if not normalized:
        raise ValueError("Log level must not be empty")

    value = getattr(logging, normalized, None)
    if not isinstance(value, int):
        raise ValueError(f"Invalid log level: {level}")
    return value


def set_global_log_level(level: str | int) -> None:
    parsed_level = _parse_log_level(level)

    global GLOBAL_LOG_LEVEL
    GLOBAL_LOG_LEVEL = parsed_level

    # 更新已创建 logger 及其 handlers 的级别，确保全局级别立即生效
    for logger_obj in logging.Logger.manager.loggerDict.values():
        if isinstance(logger_obj, logging.Logger):
            logger_obj.setLevel(parsed_level)
            for handler in logger_obj.handlers:
                handler.setLevel(parsed_level)


def get_logger(
    name: str,
    filename: str | None = None,
    level: int | None = None,
    console_level: int | None = None,
) -> logging.Logger:
    """
    获取项目 logger
    - 同时输出到文件和控制台
    - 防止重复 handler
    """

    logger = logging.getLogger(name)
    effective_level = GLOBAL_LOG_LEVEL if level is None else level
    logger.setLevel(effective_level)
    _ensure_utf8_console()

    # 已初始化过就直接返回
    if getattr(logger, "_initialized", False):
        return logger

    formatter = _ShanghaiFormatter(FORMAT)

    # ===== 文件 handler =====
    log_file = filename or f"{name}.log"
    log_path = LOG_DIR / log_file

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(effective_level)
    file_handler.setFormatter(formatter)

    # ===== 控制台 handler =====
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(console_level or effective_level)
    stream_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)

    # 不向 root logger 传播，避免 uvicorn / fastapi 重复打印
    logger.propagate = False

    # 标记已初始化
    logger._initialized = True  # type: ignore

    return logger



def setup_server_console_logging(log: logging.Logger) -> None:
    """关闭 uvicorn/socket.io 控制台噪声日志，并将请求日志写入 access.log。"""
    file_handlers = [
        h for h in log.handlers if isinstance(h, logging.FileHandler)
    ]
    if not file_handlers:
        log.error("No file handler found on app logger")
        raise RuntimeError("No file handler found on app logger")

    for logger_name in ("uvicorn", "uvicorn.error"):
        server_logger = logging.getLogger(logger_name)
        server_logger.handlers = [
            h for h in server_logger.handlers
            if not (isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler))
        ]

        for handler in file_handlers:
            if handler not in server_logger.handlers:
                server_logger.addHandler(handler)

        server_logger.setLevel(log.level)
        server_logger.propagate = False

    access_logger = logging.getLogger("uvicorn.access")
    access_logger.handlers = [
        h for h in access_logger.handlers
        if not (isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler))
    ]
    access_log_path = resolve_project_path("logs/access.log")
    access_log_path.parent.mkdir(parents=True, exist_ok=True)
    access_handler = RotatingFileHandler(
        access_log_path,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    access_handler.setFormatter(_ShanghaiFormatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    ))
    access_handler.setLevel(log.level)
    access_logger.handlers = [access_handler]
    access_logger.setLevel(log.level)
    access_logger.propagate = False

    # 将 httpx 请求日志写入 request.log，避免打印到控制台
    httpx_logger = logging.getLogger("httpx")
    httpx_logger.handlers = [
        h for h in httpx_logger.handlers
        if isinstance(h, logging.FileHandler)
    ]
    request_log_path = resolve_project_path("logs/request.log")
    request_log_path.parent.mkdir(parents=True, exist_ok=True)
    request_handler = RotatingFileHandler(
        request_log_path,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    request_handler.setFormatter(_ShanghaiFormatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    ))
    request_handler.setLevel(log.level)
    httpx_logger.addHandler(request_handler)
    httpx_logger.setLevel(log.level)
    httpx_logger.propagate = False

    # 关闭 NiceGUI 底层 socket.io / engine.io 握手与心跳日志
    for noisy_logger_name in (
        "socketio",
        "socketio.server",
        "engineio",
        "engineio.server",
    ):
        noisy_logger = logging.getLogger(noisy_logger_name)
        noisy_logger.handlers = []
        noisy_logger.setLevel(logging.WARNING)
        noisy_logger.propagate = False
