from app.config import load_raw_config
from app.providers import (
    AliyunCloudProvider,
    HuaweiCloudProvider,
    QiniuCloudProvider,
    TencentCloudProvider,
    VolcengineCloudProvider,
)
from app.services.ecs_alert import EcsAlertService
from app.services.provider_factory import build_cloud_providers, get_provider_builder_map
from app.utils import logger, notifier, store


class Ecs:
    def __init__(self) -> None:
        self.configs = load_raw_config()
        self.logger = logger.get_logger("app")

        provider_builder = get_provider_builder_map(
            include_custom=False,
            overrides={
                "aliyun": AliyunCloudProvider,
                "tencent": TencentCloudProvider,
                "huawei": HuaweiCloudProvider,
                "volcengine": VolcengineCloudProvider,
                "qiniu": QiniuCloudProvider,
            },
        )
        # 不再修改 provider 配置，直接使用顶层 product_scan 在运行时决定是否执行 ECS 采集。
        configs = list(self.configs.providers)
        self.cloud_providers = build_cloud_providers(configs, provider_builder)
        self.notify_dd = notifier.DingDingNotifier()
        self._ecs_state_store = store.AlertStateStore("data/alert_ecs_state.json")
        self._ecs_alert_batch = notifier.AlertBatcher()
        self._ecs_alert_service = EcsAlertService(
            configs=self.configs,
            logger_obj=self.logger,
            notify_dd=self.notify_dd,
            state_store=self._ecs_state_store,
            alert_batch=self._ecs_alert_batch,
        )

    async def discovery_all(self):
        ecs_data = []
        scan_conf = getattr(self.configs, "product_scan", None)

        for provider in self.cloud_providers:
            # 顶层 product_scan.<cloud>.ecs_scan=false 时跳过该云的 ECS 采集
            provider_type = str(getattr(provider.config, "type", "") or "").lower()
            if scan_conf:
                cloud_scan_conf = getattr(scan_conf, provider_type, None)
                if cloud_scan_conf is not None:
                    ecs_scan = getattr(cloud_scan_conf, "ecs_scan", True)
                    if not ecs_scan:
                        self.logger.info(
                            "ecs discovery skipped: provider=%s type=%s ecs_scan=false",
                            provider.name,
                            provider_type,
                        )
                        continue

            items = await provider.get_ecs_instances() or []
            for item in items:
                payload = dict(item)
                payload["name"] = provider.name
                payload["provider_name"] = provider.name
                payload["provider"] = provider.config.type
                if "days" not in payload:
                    payload["days"] = None
                ecs_data.append(payload)

        self.logger.info("ecs discovery done: providers=%s instances=%s", len(self.cloud_providers), len(ecs_data))
        await self._ecs_alert_service.send_ecs_alerts(ecs_data)
        return ecs_data
