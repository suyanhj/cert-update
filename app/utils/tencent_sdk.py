from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator

from qcloud_cos import CosConfig, CosS3Client
from tencentcloud.common import credential
from tencentcloud.cdn.v20180606.cdn_client import CdnClient
from tencentcloud.domain.v20180808.domain_client import DomainClient
from tencentcloud.dnspod.v20210323.dnspod_client import DnspodClient
from tencentcloud.clb.v20180317.clb_client import ClbClient
from tencentcloud.live.v20180801.live_client import LiveClient
from tencentcloud.ssl.v20191205.ssl_client import SslClient

# 腾讯产品查询与 SSL 证书部署使用各自官方客户端。


def build_credential(secret_id: str, secret_key: str):
    return credential.Credential(secret_id, secret_key)


def build_tencent_client(
    *,
    secret_id: str,
    secret_key: str,
    client_cls: Any,
    region: str = "",
) -> Any:
    cred = build_credential(secret_id, secret_key)
    return client_cls(cred, region)


COS_CLIENT_LOGGER = "qcloud_cos.cos_client"


@contextmanager
def suppress_cos_client_logs() -> Iterator[None]:
    """临时压制 COS SDK 日志，避免未配置自定义域名时刷屏 ERROR。"""
    cos_logger = logging.getLogger(COS_CLIENT_LOGGER)
    previous_level = cos_logger.level
    cos_logger.setLevel(logging.CRITICAL)
    try:
        yield
    finally:
        cos_logger.setLevel(previous_level)


def build_tencent_cos_client(
    *,
    secret_id: str,
    secret_key: str,
    region: str,
) -> CosS3Client:
    config = CosConfig(
        Region=region,
        SecretId=secret_id,
        SecretKey=secret_key,
        Scheme="https",
    )
    return CosS3Client(config)


__all__ = [
    "CdnClient",
    "ClbClient",
    "COS_CLIENT_LOGGER",
    "DomainClient",
    "DnspodClient",
    "LiveClient",
    "SslClient",
    "build_credential",
    "build_tencent_cos_client",
    "build_tencent_client",
    "suppress_cos_client_logs",
]
