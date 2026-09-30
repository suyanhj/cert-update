import json
import threading
import os
from pathlib import Path
from typing import Any
from app.utils import logger, time
from app.utils.project_root import resolve_project_path


class StoreBase:
    def __init__(self, path: str):
        self._lock = threading.RLock()
        self._path = resolve_project_path(path)
        self.logger = logger.get_logger("app")
        self.time = time.TimeUtil()
        self._load()

    def _load(self):
        with self._lock:
            if not self._path.exists():
                self.logger.debug("Store file not found, using empty state: %s", self._path)
                return
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._load_from_raw(raw)
            self.logger.debug("Store loaded: %s", self._path)

    def _persist(self):
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            raw = self._dump_to_raw()

            tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")

            tmp_path.write_text(
                json.dumps(raw, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            os.replace(tmp_path, self._path)
            self.logger.debug("Store persisted: %s", self._path)

    def _load_from_raw(self, raw: Any):
        raise NotImplementedError

    def _dump_to_raw(self) -> Any:
        raise NotImplementedError

    def get_all(self):
        """读取全部数据（子类必须实现）。"""
        raise NotImplementedError(f"{self.__class__.__name__} 必须实现 get_all()")

    def get(self, key: str):
        """读取单条数据（按需由子类实现）。"""
        raise NotImplementedError(f"{self.__class__.__name__} 不支持 get(key)")



class DomainProviderStore(StoreBase):
    """仅维护 domain -> provider_name 映射，不落盘任何敏感凭证。"""

    def __init__(self, path: str = "data/domain_provider_map.json"):
        self._map: dict[str, str] = {}
        super().__init__(path)

    def _load_from_raw(self, raw):
        if isinstance(raw, dict):
            parsed: dict[str, str] = {}
            for k, v in raw.items():
                if isinstance(v, dict):
                    raise ValueError(
                        "domain_provider_map.json value must be provider name string, dict is not allowed"
                    )
                parsed[str(k)] = str(v)
            self._map = parsed

    def _dump_to_raw(self):
        return self._map

    def save(self, groups: list[dict]):
        new_map: dict[str, str] = {}

        for group in groups:
            provider_name = group.get("dns_name")
            domain = group.get("domain")

            if not provider_name or not domain:
                continue

            new_map[domain] = provider_name

            for sub in group.get("subs", []):
                name = sub.get("name")
                if name:
                    new_map[name] = provider_name

        with self._lock:
            self._map = new_map
            self._persist()


    def resolve(self, domain: str) -> str | None:
        with self._lock:
            if domain in self._map:
                return self._map[domain]

            best_len = -1
            best_provider = None

            for mapped, provider in self._map.items():
                base = mapped.lstrip("*.")
                if domain == base or domain.endswith("." + base):
                    if len(base) > best_len:
                        best_len = len(base)
                        best_provider = provider

            return best_provider

    def get_all(self) -> dict[str, str]:
        """读取完整 domain -> provider 映射。"""
        with self._lock:
            return dict(self._map)




class AlertStateStore(StoreBase):
    """告警状态管理，不重复推送"""

    def __init__(self, path: str = "data/alert_crt_state.json"):
        self._states: dict[str, dict] = {}
        super().__init__(path)

    def _load_from_raw(self, raw):
        if isinstance(raw, dict):
            self._states = raw

    def _dump_to_raw(self):
        return self._states

    def is_firing(self, key: str) -> bool:
        with self._lock:
            return self._states.get(key, {}).get("state") == "FIRING"

    def fire(self, key: str) -> bool:
        with self._lock:
            current = self._states.get(key, {})
            if current.get("state") != "FIRING":
                self._states[key] = {
                    "state": "FIRING",
                    "last_change": self.time.now().isoformat(timespec="seconds")
                }
                self._persist()
                return True
        return False

    def recover(self, key: str):
        with self._lock:
            self._states[key] = {
                "state": "NORMAL",
                "last_change": self.time.now().isoformat(timespec="seconds")
            }
            self._persist()

    def get(self, key: str) -> dict | None:
        """读取单个告警状态。"""
        with self._lock:
            payload = self._states.get(key)
            return dict(payload) if isinstance(payload, dict) else None

    def get_all(self) -> dict[str, dict]:
        """读取全部告警状态。"""
        with self._lock:
            return {
                k: dict(v) if isinstance(v, dict) else {}
                for k, v in self._states.items()
            }


class RenewStateStore(StoreBase):
    """证书续签状态存储，用于控制冷却窗口与失败重试。"""

    def __init__(self, path: str = "data/renew_state.json"):
        self._states: dict[str, dict] = {}
        super().__init__(path)

    def _load_from_raw(self, raw: Any):
        if isinstance(raw, dict):
            self._states = raw

    def _dump_to_raw(self):
        return self._states

    def get(self, domain: str) -> dict | None:
        """读取单个域名的续签状态。"""
        key = str(domain or "").strip().lower()
        if not key:
            return None
        with self._lock:
            payload = self._states.get(key)
            return dict(payload) if isinstance(payload, dict) else None

    def record_success(self, domain: str, provider: str, message: str):
        """记录续签成功状态。"""
        key = str(domain or "").strip().lower()
        if not key:
            return
        now = self.time.now_utc()
        with self._lock:
            previous = self._states.get(key) or {}
            total_failures = int(previous.get("total_failures", 0) or 0)
            payload = {
                "last_status": "success",
                "last_success_at": now.isoformat(timespec="seconds"),
                "last_fail_at": previous.get("last_fail_at", ""),
                "consecutive_failures": 0,
                "total_failures": total_failures,
                "last_message": str(message or ""),
                "last_provider": str(provider or ""),
            }
            self._states[key] = payload
            self._persist()

    def record_failure(self, domain: str, provider: str, message: str):
        """记录续签失败状态。"""
        key = str(domain or "").strip().lower()
        if not key:
            return
        now = self.time.now_utc()
        with self._lock:
            previous = self._states.get(key) or {}
            consecutive = int(previous.get("consecutive_failures", 0) or 0) + 1
            total_failures = int(previous.get("total_failures", 0) or 0) + 1
            payload = {
                "last_status": "failed",
                "last_success_at": previous.get("last_success_at", ""),
                "last_fail_at": now.isoformat(timespec="seconds"),
                "consecutive_failures": consecutive,
                "total_failures": total_failures,
                "last_message": str(message or ""),
                "last_provider": str(provider or ""),
            }
            self._states[key] = payload
            self._persist()

    def record_deploy_failure(self, domain: str, provider: str, message: str):
        """记录续签成功但部署失败，等待人工处理。"""
        key = str(domain or "").strip().lower()
        if not key:
            return
        now = self.time.now_utc()
        with self._lock:
            previous = self._states.get(key) or {}
            payload = {
                "last_status": "deploy_failed",
                "last_success_at": previous.get("last_success_at", ""),
                "last_fail_at": now.isoformat(timespec="seconds"),
                "consecutive_failures": int(previous.get("consecutive_failures", 0) or 0),
                "total_failures": int(previous.get("total_failures", 0) or 0),
                "last_message": str(message or ""),
                "last_provider": str(provider or ""),
            }
            self._states[key] = payload
            self._persist()


class DeployStateStore(StoreBase):
    """证书部署状态存储，用于记录各目标最近一次部署结果。"""

    def __init__(self, path: str = "data/deploy_state.json"):
        self._states: dict[str, dict] = {}
        super().__init__(path)

    def _load_from_raw(self, raw: Any):
        if isinstance(raw, dict):
            self._states = raw

    def _dump_to_raw(self):
        return self._states

    def _normalize_key(self, domain: str, provider: str, product_type: str, product_id: str) -> str:
        parts = [
            str(domain or "").strip().lower(),
            str(provider or "").strip().lower(),
            str(product_type or "").strip().lower(),
            str(product_id or "").strip(),
        ]
        return "|".join(parts)

    def record_success(self, domain: str, provider: str, product_type: str, product_id: str, message: str):
        """记录单个部署目标成功状态。"""
        key = self._normalize_key(domain, provider, product_type, product_id)
        if not key:
            return
        now = self.time.now_utc()
        with self._lock:
            previous = self._states.get(key) or {}
            total_failures = int(previous.get("total_failures", 0) or 0)
            payload = {
                "last_status": "success",
                "last_success_at": now.isoformat(timespec="seconds"),
                "last_fail_at": previous.get("last_fail_at", ""),
                "consecutive_failures": 0,
                "total_failures": total_failures,
                "last_message": str(message or ""),
                "provider": str(provider or ""),
                "product_type": str(product_type or ""),
                "product_id": str(product_id or ""),
                "domain": str(domain or ""),
            }
            self._states[key] = payload
            self._persist()

    def record_failure(self, domain: str, provider: str, product_type: str, product_id: str, message: str):
        """记录单个部署目标失败状态。"""
        key = self._normalize_key(domain, provider, product_type, product_id)
        if not key:
            return
        now = self.time.now_utc()
        with self._lock:
            previous = self._states.get(key) or {}
            consecutive = int(previous.get("consecutive_failures", 0) or 0) + 1
            total_failures = int(previous.get("total_failures", 0) or 0) + 1
            payload = {
                "last_status": "failed",
                "last_success_at": previous.get("last_success_at", ""),
                "last_fail_at": now.isoformat(timespec="seconds"),
                "consecutive_failures": consecutive,
                "total_failures": total_failures,
                "last_message": str(message or ""),
                "provider": str(provider or ""),
                "product_type": str(product_type or ""),
                "product_id": str(product_id or ""),
                "domain": str(domain or ""),
            }
            self._states[key] = payload
            self._persist()


class ProductBindingStore(StoreBase):
    """云产品绑定缓存（极简结构）。"""

    def __init__(self, path: str = "data/product_bindings_cache.json"):
        self._providers: dict[str, dict[str, Any]] = {}
        super().__init__(path)

    def _normalize_legacy_bindings(self, bindings: list[Any]) -> dict[str, dict[str, list[dict[str, Any]]]]:
        products: dict[str, dict[str, list[dict[str, Any]]]] = {}
        # 按 (product_type, product_id) 对 domain 去重，避免 SNI 同域名多证书时落盘重复（如 ELB 控制台选了两个同域名证书）
        seen: dict[str, set[str]] = {}
        for item in bindings:
            if not isinstance(item, dict):
                raise ValueError(f"legacy bindings 元素必须是 dict: {item}")
            product_type = str(item.get("product_type", "") or "").strip().lower()
            product_id = str(item.get("product_id", "") or "").strip()
            domain = str(item.get("domain", "") or "").strip()
            if not product_type or not product_id or not domain:
                raise ValueError(f"legacy binding 缺少关键字段: {item}")
            key = f"{product_type}:{product_id}"
            if key not in seen:
                seen[key] = set()
            domain_lower = domain.lower()
            if domain_lower in seen[key]:
                continue
            seen[key].add(domain_lower)
            products.setdefault(product_type, {}).setdefault(product_id, []).append({"domain": domain})
        return products

    def _normalize_products(self, products_raw: Any) -> dict[str, dict[str, list[dict[str, Any]]]]:
        if products_raw is None:
            return {}
        if not isinstance(products_raw, dict):
            raise ValueError("products 必须是 dict")
        parsed: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for product_type, product_map_raw in products_raw.items():
            p_type = str(product_type or "").strip().lower()
            if not p_type:
                raise ValueError("products 下存在空 product_type")
            if not isinstance(product_map_raw, dict):
                raise ValueError(f"products[{p_type}] 必须是 dict")
            parsed[p_type] = {}
            for product_id, domain_items_raw in product_map_raw.items():
                p_id = str(product_id or "").strip()
                if not p_id:
                    raise ValueError(f"products[{p_type}] 存在空 product_id")
                if not isinstance(domain_items_raw, list):
                    raise ValueError(f"products[{p_type}][{p_id}] 必须是 list")
                normalized_items: list[dict[str, Any]] = []
                for domain_item in domain_items_raw:
                    if isinstance(domain_item, str):
                        domain = domain_item.strip()
                        if not domain:
                            raise ValueError(f"products[{p_type}][{p_id}] 存在空域名")
                        normalized_items.append({"domain": domain})
                        continue
                    if not isinstance(domain_item, dict):
                        raise ValueError(f"products[{p_type}][{p_id}] 元素必须是 str 或 dict")
                    domain = str(domain_item.get("domain", "") or "").strip()
                    if not domain:
                        raise ValueError(f"products[{p_type}][{p_id}] 存在缺失 domain 的记录")
                    # products 结构中仅保留域名字段，忽略 listener_port 等历史字段
                    normalized_items.append({"domain": domain})
                parsed[p_type][p_id] = normalized_items
        return parsed

    def _load_from_raw(self, raw: Any):
        if raw is None:
            self._providers = {}
            return
        if not isinstance(raw, dict):
            raise ValueError("product_bindings_cache.json 顶层必须是 dict")

        parsed: dict[str, dict[str, Any]] = {}
        for provider_name, payload in raw.items():
            if not isinstance(payload, dict):
                raise ValueError(f"provider={provider_name} 的缓存内容必须是 dict")
            updated_at = payload.get("updated_at")
            if updated_at is not None and not isinstance(updated_at, str):
                raise ValueError(f"provider={provider_name} updated_at 必须是字符串")

            # 兼容旧结构：provider -> {updated_at, bindings[]}
            # 新结构：provider -> {updated_at, products{type{id:[{domain}]}}}
            if "products" in payload:
                products = self._normalize_products(payload.get("products"))
            else:
                bindings = payload.get("bindings", [])
                if not isinstance(bindings, list):
                    raise ValueError(f"provider={provider_name} bindings 必须是列表")
                products = self._normalize_legacy_bindings(bindings)
            parsed[str(provider_name)] = {
                "updated_at": updated_at or "",
                "products": products,
            }
        self._providers = parsed

    def _dump_to_raw(self):
        return self._providers

    def get_provider(self, provider_name: str) -> dict[str, Any] | None:
        with self._lock:
            payload = self._providers.get(provider_name)
            if payload is None:
                return None
            products = payload.get("products", {})
            if not isinstance(products, dict):
                raise RuntimeError(f"provider={provider_name} products 非法")
            # 读取时回转成统一 bindings 列表，供服务层复用既有匹配逻辑。
            bindings: list[dict[str, Any]] = []
            for product_type, product_map in products.items():
                if not isinstance(product_map, dict):
                    raise RuntimeError(f"provider={provider_name} product_type={product_type} 非法")
                for product_id, domain_items in product_map.items():
                    if not isinstance(domain_items, list):
                        raise RuntimeError(
                            f"provider={provider_name} product_type={product_type} product_id={product_id} 非法"
                        )
                    for domain_item in domain_items:
                        if not isinstance(domain_item, dict):
                            raise RuntimeError(
                                f"provider={provider_name} product_type={product_type} product_id={product_id} 域名项非法"
                            )
                        domain = str(domain_item.get("domain", "") or "").strip()
                        if not domain:
                            raise RuntimeError(
                                f"provider={provider_name} product_type={product_type} product_id={product_id} 缺少 domain"
                            )
                        record: dict[str, Any] = {
                            "product_type": str(product_type),
                            "product_id": str(product_id),
                            "domain": domain,
                        }
                        bindings.append(record)
            return {
                "updated_at": payload.get("updated_at", ""),
                "bindings": bindings,
            }

    def save_provider(self, provider_name: str, bindings: list[dict[str, Any]], updated_at: str):
        if not provider_name:
            raise ValueError("provider_name 不能为空")
        if not isinstance(bindings, list):
            raise TypeError("bindings 必须是 list")
        if not updated_at:
            raise ValueError("updated_at 不能为空")
        products = self._normalize_legacy_bindings(bindings)
        with self._lock:
            self._providers[provider_name] = {
                "updated_at": updated_at,
                "products": products,
            }
            self._persist()

    def get_all(self) -> dict[str, dict[str, Any]]:
        """
        读取全部 provider 缓存（原始存储结构）。
        返回深拷贝，避免调用方修改内部状态。
        """
        with self._lock:
            data: dict[str, dict[str, Any]] = {}
            for provider_name, payload in self._providers.items():
                products = payload.get("products", {})
                if not isinstance(products, dict):
                    raise RuntimeError(f"provider={provider_name} products 非法")
                copied_products: dict[str, dict[str, list[dict[str, Any]]]] = {}
                for product_type, product_map in products.items():
                    if not isinstance(product_map, dict):
                        raise RuntimeError(f"provider={provider_name} product_type={product_type} 非法")
                    copied_products[str(product_type)] = {}
                    for product_id, domain_items in product_map.items():
                        if not isinstance(domain_items, list):
                            raise RuntimeError(
                                f"provider={provider_name} product_type={product_type} product_id={product_id} 非法"
                            )
                        copied_products[str(product_type)][str(product_id)] = [
                            dict(item) if isinstance(item, dict) else {}
                            for item in domain_items
                        ]
                data[str(provider_name)] = {
                    "updated_at": str(payload.get("updated_at", "") or ""),
                    "products": copied_products,
                }
            return data


class CollectorSnapshotStore(StoreBase):
    """首页快照缓存：用于启动后先展示上一轮采集结果。"""

    def __init__(self, path: str = "data/collector_snapshot.json"):
        self._payload: dict[str, Any] = {
            "domain_groups": [],
            "cert_list": [],
            "ecs_list": [],
            "last_updated": "",
        }
        super().__init__(path)

    def _load_from_raw(self, raw: Any):
        if not isinstance(raw, dict):
            raise ValueError("collector_snapshot.json 顶层必须是 dict")
        domain_groups = raw.get("domain_groups", [])
        cert_list = raw.get("cert_list", [])
        ecs_list = raw.get("ecs_list", [])
        last_updated = raw.get("last_updated", "")
        if not isinstance(domain_groups, list):
            raise ValueError("collector_snapshot.domain_groups 必须是 list")
        if not isinstance(cert_list, list):
            raise ValueError("collector_snapshot.cert_list 必须是 list")
        if not isinstance(ecs_list, list):
            raise ValueError("collector_snapshot.ecs_list 必须是 list")
        self._payload = {
            "domain_groups": domain_groups,
            "cert_list": cert_list,
            "ecs_list": ecs_list,
            "last_updated": str(last_updated or ""),
        }

    def _dump_to_raw(self):
        return self._payload

    def save(
        self,
        domain_groups: list[dict],
        cert_list: list[dict],
        ecs_list: list[dict],
        last_updated: str | None,
    ):
        payload = {
            "domain_groups": domain_groups if isinstance(domain_groups, list) else [],
            "cert_list": cert_list if isinstance(cert_list, list) else [],
            "ecs_list": ecs_list if isinstance(ecs_list, list) else [],
            "last_updated": str(last_updated or ""),
        }
        with self._lock:
            self._payload = payload
            self._persist()

    def get_all(self) -> dict[str, Any]:
        with self._lock:
            return {
                "domain_groups": [dict(item) if isinstance(item, dict) else item for item in self._payload.get("domain_groups", [])],
                "cert_list": [dict(item) if isinstance(item, dict) else item for item in self._payload.get("cert_list", [])],
                "ecs_list": [dict(item) if isinstance(item, dict) else item for item in self._payload.get("ecs_list", [])],
                "last_updated": str(self._payload.get("last_updated", "") or ""),
            }
