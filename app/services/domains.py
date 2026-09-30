from app.config import load_raw_config
from app.providers import (
    AliyunCloudProvider,
    CloudflareDNSProvider,
    HuaweiCloudProvider,
    QiniuCloudProvider,
    StaticCloudProvider,
    TencentCloudProvider,
    VolcengineCloudProvider,
)
from app.services.domain_alert import DomainAlertService
from app.services.domain_discovery import DomainDiscoveryService
from app.services.provider_factory import build_provider, get_provider_builder_map
from app.services.provider_registry import ProviderRegistry
from app.utils import logger, notifier, store


class Domains:
    def __init__(self) -> None:
        self.configs = load_raw_config()
        self.logger = logger.get_logger("app")
        self._registry = ProviderRegistry(
            provider_configs=self.configs.providers,
            builder=self._build_provider,
        )
        self._discovery_service = DomainDiscoveryService(
            self.logger,
            dns_overrides=dict(self.configs.domain_dns_overrides or {}),
        )
        self.static_domains = self._registry.static_domains
        self.notify_dd = notifier.DingDingNotifier()
        self._domain_state_store = store.AlertStateStore("data/alert_domain_state.json")
        self.alert_batch = notifier.AlertBatcher()
        self._alert_service = DomainAlertService(
            configs=self.configs,
            logger_obj=self.logger,
            notify_dd=self.notify_dd,
            state_store=self._domain_state_store,
            alert_batch=self.alert_batch,
        )

    @property
    def providers(self):
        return self._registry.providers

    @property
    def static_providers(self):
        return self._registry.static_providers

    @property
    def cloud_providers(self):
        return self._registry.cloud_providers

    @property
    def static_nums(self):
        return self._registry.static_nums

    @property
    def cloud_nums(self):
        return self._registry.cloud_nums

    def _build_provider(self, cfg):
        provider_builder = get_provider_builder_map(
            include_custom=True,
            overrides={
                "custom": StaticCloudProvider,
                "aliyun": AliyunCloudProvider,
                "tencent": TencentCloudProvider,
                "huawei": HuaweiCloudProvider,
                "volcengine": VolcengineCloudProvider,
                "qiniu": QiniuCloudProvider,
                "cloudflare": CloudflareDNSProvider,
            },
        )
        return build_provider(cfg, provider_builder)

    def get_provider(self, name: str):
        return self._registry.get_provider(name)

    def get_static_provider(self, name: str):
        return self._registry.get_static_provider(name)

    def get_cloud_provider(self, name: str):
        return self._registry.get_cloud_provider(name)

    async def get_domain_provider(self, domain: str, mode: str):
        if mode == "static":
            for provider in self.static_providers:
                if await provider.is_domain_provider(domain):
                    return provider
        else:
            for provider in self.cloud_providers:
                if await provider.is_domain_provider(domain):
                    return provider
        return None

    async def domain_alert(self, domains):
        await self._alert_service.send_domain_alerts(domains)

    async def discovery_all(self):
        # 顶层 product_scan.<cloud>.domain_scan=false 时跳过对应云厂商的域名发现
        scan_conf = getattr(self.configs, "product_scan", None)
        cloud_providers = []
        for provider in self.cloud_providers:
            provider_type = str(getattr(provider.config, "type", "") or "").lower()
            if scan_conf:
                cloud_scan_conf = getattr(scan_conf, provider_type, None)
                if cloud_scan_conf is not None:
                    domain_scan = getattr(cloud_scan_conf, "domain_scan", True)
                    if not domain_scan:
                        self.logger.info(
                            "domain discovery skipped: provider=%s type=%s domain_scan=false",
                            provider.name,
                            provider_type,
                        )
                        continue
            cloud_providers.append(provider)

        domain_data = await self._discovery_service.discover_all(
            static_providers=self.static_providers,
            cloud_providers=cloud_providers,
            static_domain_count=len(self.static_domains),
        )
        await self.domain_alert(domain_data)
        return domain_data
