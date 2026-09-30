"""阿里云证书部署器。"""

from __future__ import annotations

from typing import Any, List, Optional, Tuple

from alibabacloud_cas20200407 import models as cas_models
from alibabacloud_cdn20180510 import models as cdn_models
from alibabacloud_slb20140515 import models as slb_models

from app.utils.aliyun_sdk import build_aliyun_deployer_clients, to_cn_region
from app.utils.logger import get_logger
from app.utils.time import TimeUtil

from .base import CertificateDeployer, DeployResult, DeployTarget

LOGGER = get_logger("deployer")


class AliyunDeployer(CertificateDeployer):
    """阿里云证书部署器，支持 CDN/SLB/ALB/OSS。"""

    def __init__(
        self,
        access_key_id: str,
        access_key_secret: str,
        region: str = "cn-hangzhou",
    ) -> None:
        self._access_key_id = access_key_id
        self._access_key_secret = access_key_secret
        self._region = to_cn_region(region)

        self._cdn_client = None
        self._slb_client = None
        self._cas_client = None
        self._alb_client = None
        self._alb_models = None
        self._oss_client = None
        self._oss_models = None
        self._oss_sdk = None
        self._regional_clients: dict[str, dict[str, Any]] = {}
        self._build_client()

    @property
    def name(self) -> str:
        return "aliyun"

    def _build_client(self) -> None:
        clients = build_aliyun_deployer_clients(
            access_key_id=self._access_key_id,
            access_key_secret=self._access_key_secret,
            region=self._region,
        )
        self._cdn_client = clients["cdn_client"]
        self._slb_client = clients["slb_client"]
        self._cas_client = clients["cas_client"]
        self._alb_client = clients["alb_client"]
        self._alb_models = clients["alb_models"]
        self._oss_client = clients["oss_client"]
        self._oss_models = clients["oss_models"]
        self._oss_sdk = clients["oss_sdk"]
        self._regional_clients[self._region] = clients

    def _get_region_clients(self, region: str) -> dict[str, Any]:
        normalized = to_cn_region(region or self._region)
        cached = self._regional_clients.get(normalized)
        if cached is not None:
            return cached

        clients = build_aliyun_deployer_clients(
            access_key_id=self._access_key_id,
            access_key_secret=self._access_key_secret,
            region=normalized,
        )
        self._regional_clients[normalized] = clients
        return clients

    def _parse_slb_target(self, target: DeployTarget) -> Tuple[str, str, int]:
        if not target.product_id:
            raise ValueError("SLB 部署需要 product_id")

        parts = [p.strip() for p in target.product_id.split(":")]
        if len(parts) >= 3:
            region = to_cn_region(parts[0])
            lb_id = parts[1]
            port_raw = parts[2]
        elif len(parts) == 2:
            region = self._region
            lb_id = parts[0]
            port_raw = parts[1]
        else:
            region = self._region
            lb_id = target.product_id.strip()
            port_raw = str(target.listener_port or 443)

        if not lb_id:
            raise ValueError(f"SLB product_id 非法: {target.product_id}")

        try:
            port = int(port_raw)
        except ValueError as exc:
            raise ValueError(f"SLB 端口非法: {port_raw}") from exc

        return region, lb_id, port

    def _parse_alb_target(self, target: DeployTarget) -> Tuple[str, str, str]:
        if not target.product_id:
            raise ValueError("ALB 部署需要 product_id")

        parts = [p.strip() for p in target.product_id.split(":")]
        if len(parts) >= 3:
            region = to_cn_region(parts[0])
            lb_id = parts[1]
            listener_id = ":".join(parts[2:])
        elif len(parts) == 2:
            region = self._region
            lb_id, listener_id = parts
        else:
            raise ValueError("ALB 部署需要 product_id，格式 <region>:<lb_id>:<listener_id>")

        if not lb_id or not listener_id:
            raise ValueError(f"ALB product_id 非法: {target.product_id}")
        return region, lb_id, listener_id

    @staticmethod
    def _parse_oss_target(target: DeployTarget) -> Tuple[str, str, str]:
        """
        解析 OSS 目标，严格要求格式为 <region>:<bucket>:<domain>。
        """
        if not target.product_id or ":" not in target.product_id:
            raise ValueError("OSS 部署需要 product_id，格式 <region>:<bucket>:<domain>")

        parts = [p.strip() for p in target.product_id.split(":", 2)]
        if len(parts) != 3:
            raise ValueError(f"OSS product_id 非法（必须是 <region>:<bucket>:<domain>）: {target.product_id}")

        region, bucket, domain = parts
        if not region or not bucket or not domain:
            raise ValueError(f"OSS product_id 非法: {target.product_id}")

        return region, bucket, domain

    async def _upload_certificate(self, cert_pem: str, key_pem: str, alias: str) -> str:
        if not cert_pem or not key_pem:
            raise ValueError("上传 CAS 失败：cert_pem/key_pem 不能为空")
        try:
            req = cas_models.UploadUserCertificateRequest(name=alias, cert=cert_pem, key=key_pem)
            resp = self._cas_client.upload_user_certificate(req)
            cert_id = str(resp.body.cert_id or "")
            if not cert_id:
                raise RuntimeError("CAS 返回 cert_id 为空")
            LOGGER.info("阿里云 CAS 证书上传成功: alias=%s cert_id=%s", alias, cert_id)
            return cert_id
        except Exception as exc:
            raise RuntimeError(f"上传证书到 CAS 失败: {exc}") from exc

    async def upload_certificate(self, cert_pem: str, key_pem: str, main_domain: str | None = None) -> Optional[str]:
        """上传证书到 CAS 一次，供同一 provider 下多目标复用 cert_id。

        alias 命名：优先使用证书主域名，fallback 为 renew 前缀。
        """
        ts = TimeUtil.now().strftime('%Y%m%d%H%M%S')
        base = (main_domain or '').strip()
        if base:
            alias = f"{base}-{ts}"
        else:
            alias = f"renew-{ts}"
        return await self._upload_certificate(cert_pem, key_pem, alias)

    async def _upload_slb_server_certificate(self, cert_pem: str, key_pem: str, name: str, region: str) -> str:
        """上传证书到阿里云 SLB ServerCertificate，并更新名称。"""
        clients = self._get_region_clients(region)
        slb_client = clients["slb_client"]

        try:
            req = slb_models.UploadServerCertificateRequest(
                region_id=region,
                server_certificate=cert_pem,
                private_key=key_pem,
                server_certificate_name=name,
            )
            resp = slb_client.upload_server_certificate(req)
            server_cert_id = str(getattr(getattr(resp, "body", None), "server_certificate_id", "") or "")
            if not server_cert_id:
                raise RuntimeError("SLB UploadServerCertificate 返回 server_certificate_id 为空")

            # 按控制台行为，显式再调用一次 SetServerCertificateName，确保名称生效
            rename_req = slb_models.SetServerCertificateNameRequest(
                server_certificate_id=server_cert_id,
                server_certificate_name=name,
            )
            slb_client.set_server_certificate_name(rename_req)

            LOGGER.info(
                "阿里云 SLB 证书上传成功: region=%s name=%s server_certificate_id=%s",
                region,
                name,
                server_cert_id,
            )
            return server_cert_id
        except Exception as exc:
            raise RuntimeError(f"上传证书到 SLB ServerCertificate 失败: region={region} name={name} err={exc}") from exc

    async def deploy(
        self,
        cert_pem: str,
        key_pem: str,
        target: DeployTarget,
        cert_id: Optional[str] = None,
    ) -> DeployResult:
        try:
            # CDN / ALB / OSS 仍然走 CAS 全局证书；SLB 改为使用 UploadServerCertificate + SetServerCertificateName。
            if target.product_type == "slb":
                # SLB 证书与地域、监听器强相关，这里不复用 CAS cert_id，而是直接在 SLB ServerCertificate 下上传。
                region, _, _ = self._parse_slb_target(target)
                alias = f"{target.domain or 'cert'}-{TimeUtil.now().strftime('%Y%m%d%H%M%S')}"
                slb_cert_id = await self._upload_slb_server_certificate(
                    cert_pem=cert_pem,
                    key_pem=key_pem,
                    name=alias,
                    region=region,
                )
                await self._deploy_to_slb(cert_id=slb_cert_id, target=target)
                LOGGER.info("阿里云 SLB 证书部署成功: product_id=%s server_certificate_id=%s", target.product_id, slb_cert_id)
                return DeployResult(
                    success=True,
                    target=target,
                    message="slb 证书部署成功",
                    cert_id=slb_cert_id,
                )

            if cert_id is None:
                alias = f"{target.domain or 'cert'}-{TimeUtil.now().strftime('%Y%m%d%H%M%S')}"
                cert_id = await self._upload_certificate(cert_pem, key_pem, alias)

            if target.product_type == "cdn":
                await self._deploy_to_cdn(cert_id=cert_id, target=target)
                LOGGER.info("阿里云 CDN 证书部署成功: domain=%s cert_id=%s", target.domain, cert_id)
            elif target.product_type == "alb":
                await self._deploy_to_alb(cert_id=cert_id, target=target)
                LOGGER.info("阿里云 ALB 证书部署成功: product_id=%s cert_id=%s", target.product_id, cert_id)
            elif target.product_type == "oss":
                await self._deploy_to_oss(cert_id=cert_id, cert_pem=cert_pem, key_pem=key_pem, target=target)
                LOGGER.info(
                    "阿里云 OSS 证书部署成功: product_id=%s bucket_region=%s bucket=%s domain=%s cert_id=%s",
                    target.product_id,
                    (target.metadata or {}).get("oss_region", ""),
                    (target.metadata or {}).get("bucket", ""),
                    target.domain,
                    cert_id,
                )
            else:
                raise ValueError(f"不支持的产品类型: {target.product_type}")

            return DeployResult(
                success=True,
                target=target,
                message=f"{target.product_type} 证书部署成功",
                cert_id=cert_id,
            )
        except Exception as exc:
            LOGGER.error("阿里云部署失败: target=%s err=%s", target, exc, exc_info=True)
            return DeployResult(success=False, target=target, message=str(exc))

    async def _deploy_to_cdn(self, cert_id: str, target: DeployTarget) -> None:
        if not target.domain:
            raise ValueError("CDN 部署需要 domain")
        # SetCdnDomainSSLCertificate 需要 DomainName / CertId / SSLProtocol 等参数
        # SDK 模型字段名是 cert_id / domain_name / sslprotocol / cert_type
        req = cdn_models.SetCdnDomainSSLCertificateRequest(
            domain_name=target.domain,
            cert_id=cert_id,
            sslprotocol="on",
            cert_type="cas",
        )
        self._cdn_client.set_cdn_domain_sslcertificate(req)

    async def _deploy_to_slb(self, cert_id: str, target: DeployTarget) -> None:
        region, lb_id, port = self._parse_slb_target(target)
        clients = self._get_region_clients(region)
        req = slb_models.SetLoadBalancerHTTPSListenerAttributeRequest(
            load_balancer_id=lb_id,
            listener_port=port,
            server_certificate_id=cert_id,
        )
        clients["slb_client"].set_load_balancer_httpslistener_attribute(req)

    async def _deploy_to_alb(self, cert_id: str, target: DeployTarget) -> None:
        region, _, listener_id = self._parse_alb_target(target)
        clients = self._get_region_clients(region)
        alb_models = clients["alb_models"]
        certs_payload = [alb_models.UpdateListenerAttributeRequestCertificates(certificate_id=cert_id)]
        req = alb_models.UpdateListenerAttributeRequest(listener_id=listener_id, certificates=certs_payload)
        clients["alb_client"].update_listener_attribute(req)

    async def _deploy_to_oss(self, cert_id: str, cert_pem: str, key_pem: str, target: DeployTarget) -> None:
        # OSS 的 product_id 约定为 region:bucket:domain
        bucket_region, bucket_name, domain = self._parse_oss_target(target)

        # OSS 必须请求到 Bucket 所在地域的 endpoint，否则会返回 AccessDenied + 正确 endpoint 提示
        clients = self._get_region_clients(bucket_region)
        client = clients["oss_client"]
        oss_models = clients["oss_models"]

        old_cert = None
        if target.metadata:
            old_cert = target.metadata.get("previous_cert_id")

        # 参考官方文档：使用 PutCname + BucketCnameConfiguration / Cname / CertificateConfiguration 结构
        try:
            cert_conf = oss_models.CertificateConfiguration(
                cert_id=cert_id,
                certificate=cert_pem,
                private_key=key_pem,
                previous_cert_id=old_cert or "",
                force=True,
                delete_certificate=False,
            )
            bucket_cname_conf = oss_models.BucketCnameConfiguration(
                cname=oss_models.Cname(
                    domain=domain,
                    certificate_configuration=cert_conf,
                )
            )
            req = oss_models.PutCnameRequest()
            # SDK 文档中 PutCnameRequest 使用 bucket + bucket_cname_configuration 字段
            req.bucket = bucket_name
            req.bucket_cname_configuration = bucket_cname_conf
        except Exception as exc:
            raise RuntimeError(
                f"构造 OSS BucketCnameConfiguration 请求失败: bucket={bucket_name} domain={domain} err={exc}"
            ) from exc

        try:
            client.put_cname(req)
        except Exception as exc:
            raise RuntimeError(f"调用 OSS put_cname 失败: bucket={bucket_name} domain={domain} err={exc}") from exc

    async def list_targets(self) -> List[DeployTarget]:
        return []
