from __future__ import annotations

from typing import Any

from huaweicloudsdkcore.auth.credentials import BasicCredentials, GlobalCredentials
from huaweicloudsdkcdn.v2 import CdnClient
from huaweicloudsdkcdn.v2.region.cdn_region import CdnRegion
from huaweicloudsdkdns.v2 import DnsClient
from huaweicloudsdkdns.v2.region.dns_region import DnsRegion
from huaweicloudsdkelb.v3 import ElbClient
from huaweicloudsdkelb.v3.region.elb_region import ElbRegion
from huaweicloudsdkecs.v2 import EcsClient
from huaweicloudsdkecs.v2.region.ecs_region import EcsRegion
from huaweicloudsdkscm.v3 import ScmClient
from huaweicloudsdkscm.v3.region.scm_region import ScmRegion
from huaweicloudsdkbss.v2 import BssClient
from huaweicloudsdkbss.v2.region.bss_region import BssRegion
from huaweicloudsdkwaf.v1 import WafClient
from huaweicloudsdkwaf.v1.region.waf_region import WafRegion


def build_basic_credentials(
    access_key_id: str,
    access_key_secret: str,
    project_id: str | None = None,
):
    return BasicCredentials(access_key_id, access_key_secret, project_id or None)


def build_global_credentials(access_key_id: str, access_key_secret: str):
    return GlobalCredentials(access_key_id, access_key_secret)


def build_region_client(
    *,
    client_cls: Any,
    region_cls: Any,
    region: str,
    credentials: Any,
):
    return (
        client_cls.new_builder()
        .with_credentials(credentials)
        .with_region(region_cls.value_of(region))
        .build()
    )


def build_cdn_client(*, region: str, credentials: Any):
    return build_region_client(
        client_cls=CdnClient,
        region_cls=CdnRegion,
        region=region,
        credentials=credentials,
    )


def build_dns_client(*, region: str, credentials: Any):
    return build_region_client(
        client_cls=DnsClient,
        region_cls=DnsRegion,
        region=region,
        credentials=credentials,
    )


def build_elb_client(*, region: str, credentials: Any):
    return build_region_client(
        client_cls=ElbClient,
        region_cls=ElbRegion,
        region=region,
        credentials=credentials,
    )


def build_ecs_client(*, region: str, credentials: Any):
    return build_region_client(
        client_cls=EcsClient,
        region_cls=EcsRegion,
        region=region,
        credentials=credentials,
    )


def build_scm_client(*, region: str, credentials: Any):
    return build_region_client(
        client_cls=ScmClient,
        region_cls=ScmRegion,
        region=region,
        credentials=credentials,
    )


def build_bss_client(*, region: str, credentials: Any):
    """
    构建 BSS 客户运营能力客户端。

    目前 BSS 仅支持少量 Region（如 cn-north-1），当传入不受支持的 region 时由调用方捕获异常并决定是否降级。
    """
    return build_region_client(
        client_cls=BssClient,
        region_cls=BssRegion,
        region=region,
        credentials=credentials,
    )


def build_waf_client(*, region: str, credentials: Any):
    """构建华为云 WAF 客户端。"""
    return build_region_client(
        client_cls=WafClient,
        region_cls=WafRegion,
        region=region,
        credentials=credentials,
    )


class HuaweiBasicCredentialMixin:
    _ak: str
    _sk: str
    _project_id: str
    _credentials: Any

    def _get_credentials(self):
        if self._credentials is None:
            self._credentials = build_basic_credentials(
                self._ak,
                self._sk,
                getattr(self, "_project_id", ""),
            )
        return self._credentials


class HuaweiCdnClientMixin:
    _cdn_client: Any
    _region: str

    def _get_credentials(self):
        raise NotImplementedError

    def _get_cdn_client(self):
        if self._cdn_client is None:
            self._cdn_client = build_cdn_client(
                region="cn-north-1",
                credentials=build_global_credentials(self._ak, self._sk),
            )
        return self._cdn_client
