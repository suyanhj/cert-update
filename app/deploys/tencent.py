"""腾讯云证书部署器。"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, List, Optional, Tuple

from tencentcloud.cdn.v20180606 import models as cdn_models
from tencentcloud.ssl.v20191205 import models as ssl_models

from app.utils.logger import get_logger
from app.utils.tencent_sdk import CdnClient, SslClient, build_tencent_client
from app.utils.time import TimeUtil

from .base import CertificateDeployer, DeployResult, DeployTarget

LOGGER = get_logger("deployer")

_CDN_CERT_ROUTE = "Https.CertInfo.CertId"
_DEPLOY_POLL_INTERVAL_SECONDS = 2.0
_DEPLOY_POLL_TIMEOUT_SECONDS = 300.0


class TencentDeployer(CertificateDeployer):
    """腾讯云证书部署器，支持 CDN、云直播、CLB 和 COS。"""

    def __init__(
        self,
        secret_id: str,
        secret_key: str,
        region: str = "ap-guangzhou",
        deploy_poll_interval_seconds: float = _DEPLOY_POLL_INTERVAL_SECONDS,
        deploy_poll_timeout_seconds: float = _DEPLOY_POLL_TIMEOUT_SECONDS,
    ) -> None:
        self._secret_id = secret_id
        self._secret_key = secret_key
        self._region = region
        self._deploy_poll_interval_seconds = max(0.0, float(deploy_poll_interval_seconds))
        self._deploy_poll_timeout_seconds = max(0.1, float(deploy_poll_timeout_seconds))
        self._ssl_clients_by_region: dict[str, Any] = {}
        self._ssl_client = self._get_ssl_client(self._region)
        self._cdn_client = build_tencent_client(
            secret_id=self._secret_id,
            secret_key=self._secret_key,
            client_cls=CdnClient,
        )

    @property
    def name(self) -> str:
        return "tencent"

    def _get_ssl_client(self, region: str = "") -> Any:
        """CLB/COS 部署时 SSL API 必须带 Region；默认使用 provider 配置地域。"""
        normalized = str(region or "").strip() or str(self._region or "").strip()
        if not normalized:
            raise ValueError("tencent SSL client region must not be empty")

        cached = self._ssl_clients_by_region.get(normalized)
        if cached is not None:
            return cached
        client = build_tencent_client(
            secret_id=self._secret_id,
            secret_key=self._secret_key,
            client_cls=SslClient,
            region=normalized,
        )
        self._ssl_clients_by_region[normalized] = client
        return client

    async def upload_certificate(
        self,
        cert_pem: str,
        key_pem: str,
        main_domain: str | None = None,
    ) -> Optional[str]:
        """上传证书到腾讯云 SSL，一次上传供同一账号下 CDN/云直播/CLB/COS 复用 cert_id。"""
        ts = TimeUtil.now().strftime("%Y%m%d%H%M%S")
        base = (main_domain or "").strip()
        alias = f"{base}-{ts}" if base else f"renew-{ts}"
        return await self._upload_certificate(cert_pem, key_pem, alias)

    async def _upload_certificate(self, cert_pem: str, key_pem: str, alias: str) -> str:
        if not cert_pem or not key_pem:
            raise ValueError("上传腾讯云 SSL 失败：cert_pem/key_pem 不能为空")
        req = ssl_models.UploadCertificateRequest()
        req.CertificatePublicKey = cert_pem
        req.CertificatePrivateKey = key_pem
        req.Alias = alias
        req.Repeatable = False
        resp = self._ssl_client.UploadCertificate(req)
        cert_id_new = str(resp.CertificateId or "").strip()
        cert_id_repeat = str(resp.RepeatCertId or "").strip()
        if cert_id_new:
            upload_mode = "new"
            cert_id = cert_id_new
        elif cert_id_repeat:
            upload_mode = "repeat"
            cert_id = cert_id_repeat
        else:
            raise RuntimeError(f"腾讯云 SSL 上传未返回证书 ID: alias={alias}")
        LOGGER.info(
            "腾讯云证书上传成功: alias=%s upload_mode=%s cert_id=%s",
            alias,
            upload_mode,
            cert_id,
        )
        return cert_id

    async def deploy(
        self,
        cert_pem: str,
        key_pem: str,
        target: DeployTarget,
        cert_id: Optional[str] = None,
    ) -> DeployResult:
        try:
            if not cert_id:
                alias = f"{target.domain or 'cert'}-{TimeUtil.now().strftime('%Y%m%d%H%M%S')}"
                cert_id = await self._upload_certificate(cert_pem, key_pem, alias)

            if target.product_type == "cdn":
                await self._deploy_to_cdn(cert_id, target)
            elif target.product_type == "live":
                await self._deploy_to_live(cert_id, target)
            elif target.product_type == "teo":
                await self._deploy_to_teo(cert_id, target)
            elif target.product_type == "clb":
                await self._deploy_to_clb(cert_id, target)
            elif target.product_type == "oss":
                await self._deploy_to_oss(cert_id, target)
            else:
                raise ValueError(f"不支持的产品类型: {target.product_type}")

            LOGGER.info(
                "腾讯云 %s 证书部署成功: domain=%s cert_id=%s",
                target.product_type,
                target.domain,
                cert_id,
            )
            return DeployResult(
                success=True,
                target=target,
                message=f"{target.product_type} 证书部署成功",
                cert_id=cert_id,
            )
        except Exception as exc:
            LOGGER.error("腾讯云部署失败: target=%s err=%s", target, exc, exc_info=True)
            return DeployResult(success=False, target=target, message=str(exc))

    @staticmethod
    def _deploy_failure_message(resp: Any) -> str:
        details = list(getattr(resp, "DeployRecordDetailList", None) or [])
        messages = [
            str(getattr(item, "ErrorMsg", "") or "").strip()
            for item in details
            if int(getattr(item, "Status", -1)) == 2
            and str(getattr(item, "ErrorMsg", "") or "").strip()
        ]
        return "；".join(messages) or "腾讯云未返回失败详情"

    async def _wait_deploy_record(
        self,
        ssl_client: Any,
        deploy_record_id: int,
        deadline: float,
        *,
        require_success: bool = True,
    ) -> None:
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"腾讯云证书部署等待超时: record={deploy_record_id} "
                    f"timeout={self._deploy_poll_timeout_seconds:g}s"
                )

            req = ssl_models.DescribeHostDeployRecordDetailRequest()
            req.DeployRecordId = str(deploy_record_id)
            req.Offset = 0
            req.Limit = 200
            resp = ssl_client.DescribeHostDeployRecordDetail(req)
            total = int(getattr(resp, "TotalCount", 0) or 0)
            success = int(getattr(resp, "SuccessTotalCount", 0) or 0)
            failed = int(getattr(resp, "FailedTotalCount", 0) or 0)
            running = int(getattr(resp, "RunningTotalCount", 0) or 0)
            pending = int(getattr(resp, "PendingTotalCount", 0) or 0)
            LOGGER.info(
                "腾讯云 SSL 部署进度: record=%s total=%s success=%s failed=%s running=%s pending=%s",
                deploy_record_id,
                total,
                success,
                failed,
                running,
                pending,
            )

            if failed > 0 and require_success:
                raise RuntimeError(
                    f"腾讯云证书部署任务失败: record={deploy_record_id} "
                    f"failed={failed} detail={self._deploy_failure_message(resp)}"
                )
            if total > 0 and running == 0 and pending == 0:
                if not require_success:
                    if failed > 0:
                        LOGGER.warning(
                            "腾讯云已有部署任务已结束但存在失败，继续创建当前任务: record=%s failed=%s",
                            deploy_record_id,
                            failed,
                        )
                    return
                if success == total:
                    return
                raise RuntimeError(
                    f"腾讯云证书部署任务终态异常: record={deploy_record_id} "
                    f"total={total} success={success} failed={failed}"
                )
            await asyncio.sleep(self._deploy_poll_interval_seconds)

    async def _deploy_certificate(
        self,
        cert_id: str,
        resource_type: str,
        instance_id_list: List[str],
        *,
        region: str = "",
    ) -> None:
        deploy_region = str(region or "").strip() if resource_type in ("clb", "cos") else ""
        ssl_client = self._get_ssl_client(deploy_region)
        deadline = time.monotonic() + self._deploy_poll_timeout_seconds

        while True:
            req = ssl_models.DeployCertificateInstanceRequest()
            req.CertificateId = cert_id
            req.InstanceIdList = instance_id_list
            req.ResourceType = resource_type
            LOGGER.info(
                "腾讯云 SSL 创建部署任务: resource=%s region=%s cert_id=%s instances=%s",
                resource_type,
                deploy_region or "-",
                cert_id,
                instance_id_list,
            )
            resp = ssl_client.DeployCertificateInstance(req)
            deploy_status = int(getattr(resp, "DeployStatus", -1))
            deploy_record_id = int(getattr(resp, "DeployRecordId", 0) or 0)
            if deploy_record_id <= 0:
                raise RuntimeError(
                    f"腾讯云证书部署未返回有效记录: resource={resource_type} "
                    f"status={deploy_status} record={deploy_record_id}"
                )
            if deploy_status == 1:
                await self._wait_deploy_record(ssl_client, deploy_record_id, deadline)
                return
            if deploy_status == 0:
                LOGGER.warning(
                    "腾讯云同证书同资源已有部署任务，等待后重试: resource=%s record=%s",
                    resource_type,
                    deploy_record_id,
                )
                await self._wait_deploy_record(
                    ssl_client,
                    deploy_record_id,
                    deadline,
                    require_success=False,
                )
                continue
            raise RuntimeError(
                f"腾讯云证书部署任务创建状态非法: resource={resource_type} "
                f"status={deploy_status} record={deploy_record_id}"
            )

    @staticmethod
    def _cdn_deploy_mode(target: DeployTarget) -> str:
        """旧缓存无 cdn_deploy_mode 时走 ModifyDomainConfig，新扫描标记 ssl。"""
        mode = str((target.metadata or {}).get("cdn_deploy_mode") or "").strip().lower()
        if mode in ("ssl", "modify"):
            return mode
        return "modify"

    async def _deploy_to_cdn(self, cert_id: str, target: DeployTarget) -> None:
        mode = self._cdn_deploy_mode(target)
        if mode == "ssl":
            await self._deploy_to_cdn_ssl(cert_id, target)
        else:
            await self._deploy_to_cdn_modify(cert_id, target)

    async def _deploy_to_cdn_modify(self, cert_id: str, target: DeployTarget) -> None:
        domain = str(target.domain or target.product_id or "").strip()
        if not domain:
            raise ValueError("CDN 部署需要指定域名")
        req = cdn_models.ModifyDomainConfigRequest()
        req.Domain = domain
        req.Route = _CDN_CERT_ROUTE
        req.Value = json.dumps({"update": cert_id}, ensure_ascii=False)
        LOGGER.info(
            "腾讯云 CDN ModifyDomainConfig: domain=%s route=%s cert_id=%s",
            domain,
            _CDN_CERT_ROUTE,
            cert_id,
        )
        self._cdn_client.ModifyDomainConfig(req)

    async def _deploy_to_cdn_ssl(self, cert_id: str, target: DeployTarget) -> None:
        domain = str(target.domain or target.product_id or "").strip()
        if not domain:
            raise ValueError("CDN 部署需要指定域名")
        billing = str((target.metadata or {}).get("https_billing") or "").strip().lower()
        if billing not in ("on", "off"):
            billing = "on"
        await self._deploy_certificate(cert_id, "cdn", [f"{domain}|{billing}"])

    async def _deploy_to_live(self, cert_id: str, target: DeployTarget) -> None:
        domain = str(target.domain or target.product_id or "").strip()
        if not domain:
            raise ValueError("云直播部署需要指定域名")
        await self._deploy_certificate(cert_id, "live", [domain])

    async def _deploy_to_teo(self, cert_id: str, target: DeployTarget) -> None:
        domain = str(target.domain or target.product_id or "").strip()
        if not domain:
            raise ValueError("EdgeOne 部署需要指定域名")
        await self._deploy_certificate(cert_id, "teo", [domain])

    def _parse_clb_target(self, target: DeployTarget) -> Tuple[str, str, str]:
        metadata = target.metadata or {}
        listener_id = str(metadata.get("listener_id") or "").strip()
        product_id = str(target.product_id or "").strip()
        if not product_id:
            raise ValueError("CLB 部署需要 product_id")

        parts = [p.strip() for p in product_id.split(":")]
        if len(parts) >= 3:
            region, lb_id, parsed_listener = parts[0], parts[1], parts[2]
            listener_id = listener_id or parsed_listener
        else:
            region = self._region
            lb_id = product_id

        if not lb_id:
            raise ValueError(f"CLB product_id 非法: {target.product_id}")
        if not listener_id:
            raise ValueError("CLB 部署需要 listener_id")
        return region, lb_id, listener_id

    async def _deploy_to_clb(self, cert_id: str, target: DeployTarget) -> None:
        region, lb_id, listener_id = self._parse_clb_target(target)
        metadata = target.metadata or {}
        sni_switch = bool(metadata.get("sni_switch"))
        if sni_switch:
            if not target.domain:
                raise ValueError("CLB SNI 部署需要 domain")
            instance_id = f"{lb_id}|{listener_id}|{target.domain}"
        else:
            instance_id = f"{lb_id}|{listener_id}"
        await self._deploy_certificate(cert_id, "clb", [instance_id], region=region)

    def _parse_oss_target(self, target: DeployTarget) -> Tuple[str, str, str]:
        """解析 COS 目标，严格要求格式为 <region>:<bucket>:<domain>。"""
        if not target.product_id or ":" not in target.product_id:
            raise ValueError("COS 部署需要 product_id，格式 <region>:<bucket>:<domain>")

        parts = [p.strip() for p in target.product_id.split(":", 2)]
        if len(parts) != 3:
            raise ValueError(f"COS product_id 非法（必须是 <region>:<bucket>:<domain>）: {target.product_id}")

        region, bucket, domain = parts
        if not region or not bucket or not domain:
            raise ValueError(f"COS product_id 非法: {target.product_id}")
        return region, bucket, domain

    async def _deploy_to_oss(self, cert_id: str, target: DeployTarget) -> None:
        region, bucket, domain = self._parse_oss_target(target)
        instance_id = f"{region}|{bucket}|{domain}"
        await self._deploy_certificate(cert_id, "cos", [instance_id], region=region)

    async def list_targets(self) -> List[DeployTarget]:
        return []
