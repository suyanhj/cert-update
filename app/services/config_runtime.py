import asyncio
from typing import Any

import yaml
from pydantic import ValidationError

from app.config import (
    get_expanded_config_for_validation,
    reload_config,
    update_config_atomic,
    validate_config_dict as _validate_config_dict,
)
from app.services.scheduler import start_scheduler, stop_scheduler
from app.utils.logger import get_logger

log = get_logger("app")
_apply_lock = asyncio.Lock()


def parse_config_text(config_text: str) -> dict[str, Any]:
    try:
        data = yaml.safe_load(config_text) or {}
    except yaml.YAMLError as exc:
        log.error("YAML parse failed", exc_info=True)
        mark = getattr(exc, "problem_mark", None)
        if mark is not None:
            line = int(getattr(mark, "line", 0)) + 1
            col = int(getattr(mark, "column", 0)) + 1
            problem = getattr(exc, "problem", None) or str(exc)
            raise ValueError(f"YAML parse failed at line {line}, column {col}: {problem}") from exc
        raise ValueError(f"YAML parse failed: {exc}") from exc
    except Exception as exc:
        log.error("YAML parse failed", exc_info=True)
        raise ValueError(f"YAML parse failed: {exc}") from exc
    if not isinstance(data, dict):
        log.error("Config root must be a mapping object")
        raise ValueError("Config root must be a mapping object")
    return data


def validate_config_dict(new_config: dict[str, Any]) -> None:
    # 复用 app.config 的统一 schema 校验入口，仅在这里格式化错误信息供 UI 展示。
    try:
        _validate_config_dict(new_config)
    except ValidationError as exc:
        log.error("Config validation failed", exc_info=True)
        lines: list[str] = []
        for item in exc.errors():
            loc = ".".join(str(p) for p in item.get("loc", [])) or "root"
            msg = str(item.get("msg", "invalid value"))
            lines.append(f"- {loc}: {msg}")
        detail = "\n".join(lines)
        raise ValueError(f"Config validation failed:\n{detail}") from exc
    except Exception as exc:
        log.error("Config validation failed", exc_info=True)
        raise ValueError(f"Config validation failed: {exc}") from exc


async def apply_config_dict(new_config: dict[str, Any]) -> None:
    async with _apply_lock:
        # 继承场景下子配置不含 acme 等字段，需用「父 + 子」合并后的完整配置校验。
        expanded = get_expanded_config_for_validation(new_config)
        validate_config_dict(expanded)
        update_config_atomic(new_config)
        conf = reload_config()
        await stop_scheduler()
        start_scheduler(int(conf.check_interval_minutes), run_immediately=False)


async def apply_config_text(config_text: str) -> None:
    new_config = parse_config_text(config_text)
    await apply_config_dict(new_config)
