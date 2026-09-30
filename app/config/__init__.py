import errno
import os
import tempfile
from pathlib import Path
from typing import Any
import copy

import yaml
from box import Box

from app.schemas.config import AppConfig
from app.utils.logger import get_logger, set_global_log_level
from app.utils.project_root import resolve_project_path

CONFIG_PATH = resolve_project_path("config.yaml")
_CONFIG_CACHE: Box | None = None

log = get_logger("app")


def set_config_path(path: str | Path) -> None:
    config_path = Path(path).expanduser()
    if not config_path.exists():
        log.error("Config file not found: %s", config_path)
        raise FileNotFoundError(f"Config file not found: {config_path}")
    if not config_path.is_file():
        log.error("Config path is not a file: %s", config_path)
        raise ValueError(f"Config path is not a file: {config_path}")

    global CONFIG_PATH
    CONFIG_PATH = config_path
    log.info("Config path set to: %s", config_path)


def _read_yaml_dict(path: Path) -> dict[str, Any]:
    if not path.exists():
        log.error("Config file not found: %s", path)
        raise FileNotFoundError(f"Config file not found: {path}")
    if not path.is_file():
        log.error("Config path is not a file: %s", path)
        raise ValueError(f"Config path is not a file: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        log.error("Config root must be a mapping object")
        raise ValueError("Config root must be a mapping object")
    return data


def _normalize_extends(raw: Any) -> list[str]:
    if raw in (None, ""):
        return []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        result: list[str] = []
        for item in raw:
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"Invalid extends item: {item}")
            result.append(item.strip())
        return result
    raise ValueError("Config extends must be a string or list[string]")


def _resolve_extends_path(base_path: Path, extends_item: str) -> Path:
    target = Path(extends_item).expanduser()
    if target.is_absolute():
        return target.resolve()

    local_target = (base_path.parent / target).resolve()
    if local_target.exists():
        return local_target

    # 先按当前文件相对路径解析，再按项目根解析，避免测试或迁移后 extends 找不到父文件
    project_target = resolve_project_path(str(target))
    if project_target.exists():
        return project_target.resolve()
    return local_target


def _as_normalized_text(value: Any) -> str:
    return str(value or "").strip()


def _dedupe_scalar_list(items: list[Any]) -> list[Any]:
    seen: set[str] = set()
    result: list[Any] = []
    for item in items:
        key = _as_normalized_text(item)
        if not key:
            continue
        key_lower = key.lower()
        if key_lower in seen:
            continue
        seen.add(key_lower)
        result.append(item)
    return result


def _dedupe_named_list(items: list[Any], key_field: str) -> list[Any]:
    result: list[Any] = []
    index_by_key: dict[str, int] = {}
    for item in items:
        if not isinstance(item, dict):
            result.append(copy.deepcopy(item))
            continue
        key = _as_normalized_text(item.get(key_field))
        if not key:
            result.append(copy.deepcopy(item))
            continue
        key_lower = key.lower()
        if key_lower in index_by_key:
            # 同一 key 以最后一次定义为准
            result[index_by_key[key_lower]] = copy.deepcopy(item)
            continue
        index_by_key[key_lower] = len(result)
        result.append(copy.deepcopy(item))
    return result


def _rule_key(rule: dict[str, Any]) -> str:
    if not isinstance(rule, dict):
        return ""
    target_group = _as_normalized_text(rule.get("target_group")).lower()
    cert_name_template = _as_normalized_text(rule.get("cert_name_template"))
    domains = rule.get("cert_domains", [])
    normalized_domains: list[str] = []
    if isinstance(domains, list):
        for item in domains:
            domain = _as_normalized_text(item).lower()
            if domain:
                normalized_domains.append(domain)
    domain_key = ",".join(sorted(set(normalized_domains)))
    return f"{target_group}|{cert_name_template}|{domain_key}"


def _dedupe_rule_list(items: list[Any]) -> list[Any]:
    result: list[Any] = []
    index_by_key: dict[str, int] = {}
    for item in items:
        if not isinstance(item, dict):
            result.append(copy.deepcopy(item))
            continue
        key = _rule_key(item)
        if not key:
            result.append(copy.deepcopy(item))
            continue
        if key in index_by_key:
            result[index_by_key[key]] = copy.deepcopy(item)
            continue
        index_by_key[key] = len(result)
        result.append(copy.deepcopy(item))
    return result


def _dedupe_config_lists(data: dict[str, Any]) -> dict[str, Any]:
    # 对列表类配置去重，避免重复的 providers/hosts/rules 导致重复扫描或部署
    normalized = copy.deepcopy(data)

    providers = normalized.get("providers")
    if isinstance(providers, list):
        providers = _dedupe_named_list(providers, "name")
        for provider in providers:
            if not isinstance(provider, dict):
                continue
            static_domains = provider.get("static_domains")
            if isinstance(static_domains, list):
                static_domains = _dedupe_named_list(static_domains, "domain")
                for item in static_domains:
                    if isinstance(item, dict) and isinstance(item.get("sub_domains"), list):
                        item["sub_domains"] = _dedupe_scalar_list(item["sub_domains"])
                provider["static_domains"] = static_domains
            sub_extensions = provider.get("sub_extensions")
            if isinstance(sub_extensions, list):
                provider["sub_extensions"] = _dedupe_named_list(sub_extensions, "domain")
        normalized["providers"] = providers

    ssh_profiles = normalized.get("ssh_profiles")
    if isinstance(ssh_profiles, list):
        normalized["ssh_profiles"] = _dedupe_named_list(ssh_profiles, "name")

    whitelist = normalized.get("whitelist")
    if isinstance(whitelist, list):
        normalized["whitelist"] = _dedupe_scalar_list(whitelist)

    nginx = normalized.get("nginx")
    if isinstance(nginx, dict):
        nginx_hosts = nginx.get("nginx_hosts")
        if isinstance(nginx_hosts, list):
            nginx["nginx_hosts"] = _dedupe_named_list(nginx_hosts, "name")
        target_groups = nginx.get("nginx_target_groups")
        if isinstance(target_groups, list):
            nginx["nginx_target_groups"] = _dedupe_named_list(target_groups, "name")
        deploy_rules = nginx.get("nginx_deploy_rules")
        if isinstance(deploy_rules, list):
            nginx["nginx_deploy_rules"] = _dedupe_rule_list(deploy_rules)
        normalized["nginx"] = nginx
    return normalized


def _deep_merge_dict(
    base: dict[str, Any],
    override: dict[str, Any],
    path: tuple[str, ...] = (),
) -> dict[str, Any]:
    # 字典深合并；非字典（含 list）以子配置覆盖为准
    result = copy.deepcopy(base)
    for key, value in override.items():
        current_path = (*path, key)
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = _deep_merge_dict(result[key], value, current_path)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _load_with_extends(path: Path, stack: tuple[Path, ...] = ()) -> dict[str, Any]:
    current = path.resolve()
    if current in stack:
        chain = " -> ".join(str(p) for p in (*stack, current))
        raise ValueError(f"Config extends cycle detected: {chain}")

    data = _read_yaml_dict(current)
    extends_items = _normalize_extends(data.get("extends"))

    merged: dict[str, Any] = {}
    for extends_item in extends_items:
        parent_path = _resolve_extends_path(current, extends_item)
        parent_data = _load_with_extends(parent_path, (*stack, current))
        merged = _deep_merge_dict(merged, parent_data)

    local_data = {k: v for k, v in data.items() if k != "extends"}
    merged = _deep_merge_dict(merged, local_data)
    return merged


def _read_raw_dict() -> dict[str, Any]:
    # 运行时配置 = extends 展开 + 列表去重规范化
    raw = _load_with_extends(Path(CONFIG_PATH))
    return _dedupe_config_lists(raw)


def _to_box(data: dict[str, Any]) -> Box:
    return Box(data, default_box=True, frozen_box=True)


def validate_config_dict(data: dict[str, Any]) -> None:
    """使用 AppConfig 作为配置 schema 校验的唯一来源。"""
    AppConfig(**data)


def get_expanded_config_for_validation(raw: dict[str, Any]) -> dict[str, Any]:
    """返回子配置与 extends 父配置合并后的完整配置，供热更新校验使用。继承场景下子配置不含 acme 等字段时，需用合并结果校验。"""
    normalized = _dedupe_config_lists(raw)
    return _expand_config_for_validation(normalized, Path(CONFIG_PATH))


def _validate_config(data: dict[str, Any]) -> None:
    try:
        validate_config_dict(data)
    except Exception as exc:
        log.error("Config validation failed")
        raise ValueError(f"Config validation failed: {exc}") from exc


def _expand_config_for_validation(raw: dict[str, Any], base_path: Path) -> dict[str, Any]:
    # 用「父配置 + 当前」合并后的视图做校验，不要求重写父文件内容
    extends_items = _normalize_extends(raw.get("extends"))
    merged: dict[str, Any] = {}
    for extends_item in extends_items:
        parent_path = _resolve_extends_path(base_path, extends_item)
        parent_data = _load_with_extends(parent_path)
        merged = _deep_merge_dict(merged, parent_data)

    local_data = {k: v for k, v in raw.items() if k != "extends"}
    expanded = _deep_merge_dict(merged, local_data)
    return _dedupe_config_lists(expanded)


def _apply_log_level(conf: Box) -> None:
    try:
        log_level = str(conf.log_level or "INFO").strip() or "INFO"
        set_global_log_level(log_level)
    except Exception as exc:
        log.error("Failed to apply global log level from config")
        raise ValueError("Invalid log_level in config") from exc


def get_config(force_reload: bool = False) -> Box:
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None and not force_reload:
        return _CONFIG_CACHE

    data = _read_raw_dict()
    _validate_config(data)
    conf = _to_box(data)
    _apply_log_level(conf)
    _CONFIG_CACHE = conf
    log.info("Config loaded from: %s", CONFIG_PATH)
    return conf


def reload_config() -> Box:
    log.info("Reloading config from: %s", CONFIG_PATH)
    return get_config(force_reload=True)


def load_raw_config() -> Box:
    # 兼容旧调用入口
    return get_config()


def update_config_atomic(new_config: dict[str, Any]) -> Box:
    # 持久化规范化后的子配置，校验时使用「父 + 子」合并后的视图
    normalized_new_config = _dedupe_config_lists(new_config)
    config_for_validate = _expand_config_for_validation(normalized_new_config, Path(CONFIG_PATH))
    _validate_config(config_for_validate)
    parent = CONFIG_PATH.parent
    if not parent.exists():
        log.error("Config parent directory not found: %s", parent)
        raise FileNotFoundError(f"Config parent directory not found: {parent}")

    cfg_str = str(CONFIG_PATH)
    if not os.access(cfg_str, os.W_OK):
        log.error("Config file is not writable (read-only mount or permission): %s", CONFIG_PATH)
        raise RuntimeError(
            "配置文件不可写：当前路径无写权限，常见于 Docker/K8s 将 config.yaml 以 :ro 只读挂载。"
            "页面保存需要可写文件：去掉 :ro、改为挂载可写目录、或使用启动参数 -c/--config 指向容器内可写副本（如 /data/config.yaml）。"
        )

    fd: int | None = None
    temp_path: str | None = None
    try:
        fd, temp_path = tempfile.mkstemp(prefix="config-", suffix=".yaml", dir=str(parent))
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            fd = None
            yaml.safe_dump(normalized_new_config, tmp, allow_unicode=True, sort_keys=False)
        os.replace(temp_path, str(CONFIG_PATH))
    except OSError as exc:
        log.error("Atomic config write failed: %s", exc, exc_info=True)
        err = getattr(exc, "errno", None)
        if err in (errno.EBUSY, errno.EROFS, errno.EACCES, errno.EPERM):
            raise RuntimeError(
                "配置保存失败：无法在目标路径上完成原子替换（常见于单文件只读挂载、ConfigMap 挂成文件等，Linux 上常表现为 EBUSY）。"
                "请勿对 config.yaml 使用只读挂载；需要可写卷或改用容器内可写 CONFIG_PATH。"
            ) from exc
        raise RuntimeError(f"Atomic config write failed: {exc}") from exc
    except Exception as exc:
        log.error("Atomic config write failed: %s", exc, exc_info=True)
        raise RuntimeError(f"Atomic config write failed: {exc}") from exc
    finally:
        if fd is not None:
            os.close(fd)
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)

    log.info("Config updated atomically: %s", CONFIG_PATH)
    return reload_config()
