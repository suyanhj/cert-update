from __future__ import annotations

from typing import Any

from app.deploys import (
    AliyunDeployer,
    HuaweiDeployer,
    QiniuDeployer,
    TencentDeployer,
    VolcengineDeployer,
)
from app.deploys.base import CertificateDeployer


def build_cloud_deployer(
    provider_type: str,
    provider_name: str,
    credentials: dict[str, Any],
) -> CertificateDeployer:
    if provider_type == "aliyun":
        access_key_id = credentials.get("access_key_id", "")
        access_key_secret = credentials.get("access_key_secret", "")
        region = credentials.get("region", "") or "cn-hangzhou"
        if not access_key_id or not access_key_secret:
            raise RuntimeError(f"aliyun provider {provider_name} missing access credentials")
        return AliyunDeployer(
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
            region=region,
        )

    if provider_type == "huawei":
        access_key_id = credentials.get("access_key_id", "")
        access_key_secret = credentials.get("access_key_secret", "")
        project_id = credentials.get("project_id", "")
        enterprise_project_id = credentials.get("enterprise_project_id", "0") or "0"
        region = credentials.get("region", "") or "cn-north-4"
        if not access_key_id or not access_key_secret:
            raise RuntimeError(f"huawei provider {provider_name} missing access credentials")
        return HuaweiDeployer(
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
            project_id=project_id,
            region=region,
            enterprise_project_id=enterprise_project_id,
        )

    if provider_type == "tencent":
        secret_id = credentials.get("secret_id", "")
        secret_key = credentials.get("secret_key", "")
        region = credentials.get("region", "") or "ap-guangzhou"
        if not secret_id or not secret_key:
            raise RuntimeError(f"tencent provider {provider_name} missing access credentials")
        return TencentDeployer(secret_id=secret_id, secret_key=secret_key, region=region)

    if provider_type == "volcengine":
        access_key_id = credentials.get("access_key_id", "")
        access_key_secret = credentials.get("access_key_secret", "")
        region = credentials.get("region", "") or "cn-north-1"
        if not access_key_id or not access_key_secret:
            raise RuntimeError(f"volcengine provider {provider_name} missing access credentials")
        return VolcengineDeployer(
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
            region=region,
        )

    if provider_type == "qiniu":
        access_key_id = credentials.get("access_key_id", "")
        access_key_secret = credentials.get("access_key_secret", "")
        if not access_key_id or not access_key_secret:
            raise RuntimeError(f"qiniu provider {provider_name} missing access credentials")
        return QiniuDeployer(
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
        )

    raise RuntimeError(f"unsupported deploy provider.type: {provider_type}")
