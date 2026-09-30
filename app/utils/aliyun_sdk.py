from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from alibabacloud_alidns20150109 import models as _alidns_models
from alibabacloud_alidns20150109.client import Client as _DnsClient
from alibabacloud_domain20180129 import models as _domain_models
from alibabacloud_domain20180129.client import Client as _DomainClient
from alibabacloud_cdn20180510 import models as _cdn_models
from alibabacloud_cdn20180510.client import Client as _CdnClient
from alibabacloud_ecs20140526 import models as _ecs_models
from alibabacloud_ecs20140526.client import Client as _EcsClient
from alibabacloud_swas_open20200601 import models as _swas_models
from alibabacloud_swas_open20200601.client import Client as _SwasClient
from alibabacloud_slb20140515 import models as _slb_models
from alibabacloud_slb20140515.client import Client as _SlbClient
from alibabacloud_alb20200616 import models as _alb_models
from alibabacloud_alb20200616.client import Client as _AlbClient
from alibabacloud_cas20200407.client import Client as _CasClient
import alibabacloud_oss_v2 as _oss_sdk
from alibabacloud_oss_v2 import models as _oss_models
from alibabacloud_oss_v2.client import Client as _OssClient
from alibabacloud_oss_v2.config import Config as _OssConfig
from alibabacloud_oss_v2.credentials import StaticCredentialsProvider as _StaticCredentialsProvider
from alibabacloud_tea_openapi.models import Config as _Config

# 直接导出，供 deployer/provider 内联使用
alidns_models = _alidns_models
cdn_models = _cdn_models
slb_models = _slb_models
alb_models = _alb_models
cas_models = None  # alibabacloud_cas20200407 没有独立 models 模块，通过 client 直接使用


@dataclass
class AliyunSDK:
    dns_models: Any
    domain_models: Any
    cdn_models: Any
    ecs_models: Any
    swas_models: Any
    slb_models: Any
    oss_models: Any
    alb_models: Any
    oss_sdk: Any
    DomainClient: Any
    DnsClient: Any
    CdnClient: Any
    EcsClient: Any
    SwasClient: Any
    SlbClient: Any
    CasClient: Any
    AlbClient: Any
    OssClient: Any
    Config: Any
    OssConfig: Any
    Credentials: Any


# 向后兼容别名
AliyunProviderSDK = AliyunSDK
AliyunDeployerSDK = AliyunSDK


def _new_openapi_config(
    *,
    Config: Any,
    access_key_id: str,
    access_key_secret: str,
    endpoint: str,
    region_id: str | None = None,
):
    kwargs: dict[str, Any] = {
        "access_key_id": access_key_id,
        "access_key_secret": access_key_secret,
        "endpoint": endpoint,
    }
    if region_id:
        kwargs["region_id"] = region_id
    return Config(**kwargs)


def to_cn_region(region: str) -> str:
    normalized = (region or "").strip().lower()
    if not normalized:
        raise ValueError("aliyun region must not be empty")
    if normalized.startswith("ap-"):
        return f"cn-{normalized[3:]}"
    return normalized


def to_oss_region(region: str) -> str:
    normalized = (region or "").strip().lower()
    if not normalized:
        raise ValueError("aliyun region must not be empty")
    if normalized.startswith("oss-"):
        return normalized[4:]
    if normalized.startswith("ap-"):
        return f"cn-{normalized[3:]}"
    return normalized


def load_aliyun_sdk() -> AliyunSDK:
    return AliyunSDK(
        dns_models=_alidns_models,
        domain_models=_domain_models,
        cdn_models=_cdn_models,
        ecs_models=_ecs_models,
        swas_models=_swas_models,
        slb_models=_slb_models,
        oss_models=_oss_models,
        alb_models=_alb_models,
        oss_sdk=_oss_sdk,
        DomainClient=_DomainClient,
        DnsClient=_DnsClient,
        CdnClient=_CdnClient,
        EcsClient=_EcsClient,
        SwasClient=_SwasClient,
        SlbClient=_SlbClient,
        CasClient=_CasClient,
        AlbClient=_AlbClient,
        OssClient=_OssClient,
        Config=_Config,
        OssConfig=_OssConfig,
        Credentials=_StaticCredentialsProvider,
    )


# 向后兼容
load_aliyun_provider_sdk = load_aliyun_sdk
load_aliyun_deployer_sdk = load_aliyun_sdk


def build_aliyun_provider_clients(
    *,
    access_key_id: str,
    access_key_secret: str,
    region: str,
    sdk: AliyunProviderSDK | None = None,
) -> dict[str, Any]:
    current_sdk = sdk or load_aliyun_provider_sdk()

    domain_client = current_sdk.DomainClient(
        _new_openapi_config(
            Config=current_sdk.Config,
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
            endpoint="domain.aliyuncs.com",
        )
    )
    dns_client = current_sdk.DnsClient(
        _new_openapi_config(
            Config=current_sdk.Config,
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
            endpoint="alidns.aliyuncs.com",
        )
    )
    cdn_client = current_sdk.CdnClient(
        _new_openapi_config(
            Config=current_sdk.Config,
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
            endpoint="cdn.aliyuncs.com",
        )
    )
    cn_region = to_cn_region(region)
    oss_region = to_oss_region(region)

    slb_client = current_sdk.SlbClient(
        _new_openapi_config(
            Config=current_sdk.Config,
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
            endpoint=f"slb.{cn_region}.aliyuncs.com",
        )
    )
    ecs_client = current_sdk.EcsClient(
        _new_openapi_config(
            Config=current_sdk.Config,
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
            endpoint=f"ecs.{cn_region}.aliyuncs.com",
        )
    )
    cas_client = current_sdk.CasClient(
        _new_openapi_config(
            Config=current_sdk.Config,
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
            endpoint="cas.aliyuncs.com",
        )
    )
    alb_client = current_sdk.AlbClient(
        _new_openapi_config(
            Config=current_sdk.Config,
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
            endpoint=f"alb.{cn_region}.aliyuncs.com",
            region_id=cn_region,
        )
    )

    oss_client = current_sdk.OssClient(
        current_sdk.OssConfig(
            credentials_provider=current_sdk.Credentials(
                access_key_id=access_key_id,
                access_key_secret=access_key_secret,
            ),
            region=oss_region,
            endpoint=f"oss-{oss_region}.aliyuncs.com",
        )
    )

    return {
        "domain_client": domain_client,
        "dns_client": dns_client,
        "cdn_client": cdn_client,
        "ecs_client": ecs_client,
        "slb_client": slb_client,
        "cas_client": cas_client,
        "alb_client": alb_client,
        "oss_client": oss_client,
    }


def build_aliyun_deployer_clients(
    *,
    access_key_id: str,
    access_key_secret: str,
    region: str,
    sdk: AliyunDeployerSDK | None = None,
) -> dict[str, Any]:
    current_sdk = sdk or load_aliyun_deployer_sdk()

    cn_region = to_cn_region(region)
    oss_region = to_oss_region(region)

    cdn_client = current_sdk.CdnClient(
        _new_openapi_config(
            Config=current_sdk.Config,
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
            endpoint="cdn.aliyuncs.com",
        )
    )
    slb_client = current_sdk.SlbClient(
        _new_openapi_config(
            Config=current_sdk.Config,
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
            endpoint=f"slb.{cn_region}.aliyuncs.com",
        )
    )
    cas_client = current_sdk.CasClient(
        _new_openapi_config(
            Config=current_sdk.Config,
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
            endpoint="cas.aliyuncs.com",
        )
    )
    alb_client = current_sdk.AlbClient(
        _new_openapi_config(
            Config=current_sdk.Config,
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
            endpoint=f"alb.{cn_region}.aliyuncs.com",
            region_id=cn_region,
        )
    )

    oss_client = current_sdk.OssClient(
        current_sdk.OssConfig(
            credentials_provider=current_sdk.Credentials(
                access_key_id=access_key_id,
                access_key_secret=access_key_secret,
            ),
            region=oss_region,
            endpoint=f"oss-{oss_region}.aliyuncs.com",
        )
    )

    return {
        "cdn_client": cdn_client,
        "slb_client": slb_client,
        "cas_client": cas_client,
        "alb_client": alb_client,
        "alb_models": current_sdk.alb_models,
        "oss_client": oss_client,
        "oss_models": current_sdk.oss_models,
        "oss_sdk": current_sdk.oss_sdk,
    }
