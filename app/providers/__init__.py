"""云平台提供器集合。"""
from .static import StaticCloudProvider
from .tencent import TencentCloudProvider
from .aliyun import AliyunCloudProvider
from .huawei import HuaweiCloudProvider
from .volcengine import VolcengineCloudProvider
from .qiniu import QiniuCloudProvider
from .cloudflare import CloudflareDNSProvider

__all__ = [
    "StaticCloudProvider",
    "TencentCloudProvider",
    "AliyunCloudProvider",
    "HuaweiCloudProvider",
    "VolcengineCloudProvider",
    "QiniuCloudProvider",
    "CloudflareDNSProvider",
]
