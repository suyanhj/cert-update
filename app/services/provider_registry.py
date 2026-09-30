from __future__ import annotations

from typing import Any, Callable


class ProviderRegistry:
    """Provider 注册表：负责构建、分组、索引。"""

    def __init__(self, provider_configs: list[Any], builder: Callable[[Any], Any]) -> None:
        self._configs = [cfg for cfg in provider_configs if cfg.enabled]

        built: list[tuple[Any, Any]] = []
        for cfg in self._configs:
            built.append((cfg, builder(cfg)))

        self._providers = [provider for _, provider in built]
        self._static_providers = [
            provider for cfg, provider in built if str(cfg.type).strip().lower() == "custom"
        ]
        self._cloud_providers = [
            provider for cfg, provider in built if str(cfg.type).strip().lower() != "custom"
        ]
        self._static_provider_map = self._to_provider_map(self._static_providers)
        self._cloud_provider_map = self._to_provider_map(self._cloud_providers)
        self._provider_map = {**self._static_provider_map, **self._cloud_provider_map}
        self.static_domains = self._collect_static_domains(self._configs)

    @property
    def configs(self) -> list[Any]:
        return self._configs

    @property
    def providers(self) -> list[Any]:
        return self._providers

    @property
    def static_providers(self) -> list[Any]:
        return self._static_providers

    @property
    def cloud_providers(self) -> list[Any]:
        return self._cloud_providers

    @property
    def static_nums(self) -> int:
        return sum(1 for cfg in self._configs if str(cfg.type).strip().lower() == "custom")

    @property
    def cloud_nums(self) -> int:
        return sum(1 for cfg in self._configs if str(cfg.type).strip().lower() != "custom")

    def get_provider(self, name: str):
        return self._provider_map.get(name)

    def get_static_provider(self, name: str):
        return self._static_provider_map.get(name)

    def get_cloud_provider(self, name: str):
        return self._cloud_provider_map.get(name)

    @staticmethod
    def _to_provider_map(data: list[Any]) -> dict[str, Any]:
        return {p.name: p for p in data if p.name}

    @staticmethod
    def _collect_static_domains(configs: list[Any]) -> list[str]:
        result: list[str] = []
        for cfg in configs:
            if str(cfg.type).strip().lower() != "custom":
                continue
            for item in cfg.static_domains or []:
                domain = item.get("domain") if isinstance(item, dict) else item.domain
                if domain:
                    result.append(str(domain))
        return result

