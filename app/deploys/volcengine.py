"""火山引擎 (Volcengine) 证书部署器 - 支持部署到 CDN 和 CLB。"""

from __future__ import annotations

from app.utils.logger import get_logger
from typing import List, Optional

from .base import CertificateDeployer, DeployResult, DeployTarget
from app.utils.volcengine_sdk import build_volcengine_service
from app.utils.time import TimeUtil

LOGGER = get_logger("deployer")


class VolcengineDeployer(CertificateDeployer):
    """火山引擎证书部署器，支持 CDN 和 CLB。"""

    def __init__(
        self,
        access_key_id: str,
        access_key_secret: str,
        region: str = "cn-north-1",
    ) -> None:
        self._ak = access_key_id
        self._sk = access_key_secret
        self._region = region
        self._cdn_service = None
        self._clb_service = None
        self._cert_service = None

    @property
    def name(self) -> str:
        return "volcengine"

    def _get_cdn_service(self):
        """Lazy-init CDN service client."""
        if self._cdn_service is None:
            self._cdn_service = build_volcengine_service(
                access_key_id=self._ak,
                access_key_secret=self._sk,
                service="cdn",
                region=self._region,
                module_path="volcenginesdkcdn",
                service_class_name="CDNApi",
            )
        return self._cdn_service

    def _get_clb_service(self):
        """Lazy-init CLB service client."""
        if self._clb_service is None:
            self._clb_service = build_volcengine_service(
                access_key_id=self._ak,
                access_key_secret=self._sk,
                service="clb",
                region=self._region,
                module_path="volcenginesdkclb",
                service_class_name="CLBApi",
            )
        return self._clb_service

    def _get_cert_service(self):
        """Lazy-init certificate service client."""
        if self._cert_service is None:
            self._cert_service = build_volcengine_service(
                access_key_id=self._ak,
                access_key_secret=self._sk,
                service="certificate_service",
                region=self._region,
                module_path="volcenginesdkcertificateservice",
                service_class_name="CERTIFICATESERVICEApi",
            )
        return self._cert_service

    async def _upload_certificate(
        self, cert_pem: str, key_pem: str, name: str
    ) -> Optional[str]:
        """上传证书到火山引擎证书中心。"""
        try:
            service = self._get_cert_service()
            
            response = service.import_certificate({
                "CertificateName": name,
                "PublicKey": cert_pem,
                "PrivateKey": key_pem,
            })
            
            cert_id = response.get("CertificateId")
            LOGGER.info("火山引擎证书上传成功: %s (ID: %s)", name, cert_id)
            return cert_id
            
        except Exception as e:
            LOGGER.error("火山引擎证书上传失败: %s", e)
            return None

    async def _deploy_to_cdn(
        self, cert_pem: str, key_pem: str, domain: str
    ) -> bool:
        """部署证书到火山引擎 CDN。"""
        try:
            service = self._get_cdn_service()
            cert_name = f"{domain}-{TimeUtil.now().strftime('%Y%m%d%H%M%S')}"
            
            # 先添加证书
            add_cert_response = service.add_certificate({
                "Certificate": cert_pem,
                "PrivateKey": key_pem,
                "CertName": cert_name,
            })
            
            cert_id = add_cert_response.get("CertId")
            
            # 更新域名使用该证书
            service.update_cdn_config({
                "Domain": domain,
                "HTTPS": {
                    "Switch": True,
                    "CertInfo": {
                        "CertId": cert_id,
                    },
                    "HTTP2": True,
                },
            })
            
            LOGGER.info("火山引擎 CDN 证书部署成功: %s (证书ID: %s)", domain, cert_id)
            return True
            
        except Exception as e:
            LOGGER.error("火山引擎 CDN 证书部署失败: %s", e)
            return False

    async def _deploy_to_clb(
        self, cert_id: str, listener_id: str
    ) -> bool:
        """部署证书到火山引擎 CLB 监听器。"""
        try:
            service = self._get_clb_service()
            
            service.modify_listener_attributes({
                "ListenerId": listener_id,
                "CertificateId": cert_id,
            })
            
            LOGGER.info("火山引擎 CLB 证书部署成功: listener=%s", listener_id)
            return True
            
        except Exception as e:
            LOGGER.error("火山引擎 CLB 证书部署失败: %s", e)
            return False

    async def deploy(
        self,
        cert_pem: str,
        key_pem: str,
        target: DeployTarget,
        cert_id: Optional[str] = None,
    ) -> DeployResult:
        """部署证书到火山引擎 CDN 或 CLB。"""
        domain = target.domain or "unknown"
        product_type = target.product_type
        
        try:
            if product_type == "cdn":
                success = await self._deploy_to_cdn(cert_pem, key_pem, domain)
                return DeployResult(
                    success=success,
                    target=target,
                    message="CDN 证书部署成功" if success else "CDN 证书部署失败",
                )
            
            elif product_type == "clb":
                # CLB 需要先上传证书
                cert_name = f"{domain}-{TimeUtil.now().strftime('%Y%m%d%H%M%S')}"
                cert_id = await self._upload_certificate(cert_pem, key_pem, cert_name)
                
                if not cert_id:
                    return DeployResult(
                        success=False,
                        target=target,
                        message="证书上传失败",
                    )
                
                listener_id = target.product_id
                if not listener_id:
                    return DeployResult(
                        success=False,
                        target=target,
                        message="未指定 CLB 监听器 ID",
                    )
                
                success = await self._deploy_to_clb(cert_id, listener_id)
                return DeployResult(
                    success=success,
                    target=target,
                    message="CLB 证书部署成功" if success else "CLB 证书部署失败",
                )
            
            else:
                return DeployResult(
                    success=False,
                    target=target,
                    message=f"不支持的产品类型: {product_type}",
                )
                
        except Exception as e:
            LOGGER.error("火山引擎证书部署异常: %s", e)
            return DeployResult(
                success=False,
                target=target,
                message=str(e),
            )

    async def list_targets(self) -> List[DeployTarget]:
        """列出所有可部署目标（基类抽象方法）。"""
        return await self.list_bindable_resources()

    async def list_bindable_resources(self) -> List[DeployTarget]:
        """列出可绑定证书的资源。"""
        targets = []
        
        # 获取 CDN 域名
        try:
            service = self._get_cdn_service()
            response = service.list_cdn_domains({
                "PageNum": 1,
                "PageSize": 100,
            })
            
            for domain in response.get("Data", []):
                targets.append(
                    DeployTarget(
                        provider="volcengine",
                        product_type="cdn",
                        product_id=domain.get("Domain"),
                        domain=domain.get("Domain"),
                    )
                )
        except Exception as e:
            LOGGER.warning("获取火山引擎 CDN 域名失败: %s", e)
        
        # 获取 CLB 监听器
        try:
            service = self._get_clb_service()
            response = service.describe_listeners({
                "PageNumber": 1,
                "PageSize": 100,
            })
            
            for listener in response.get("Listeners", []):
                if listener.get("Protocol") == "HTTPS":
                    targets.append(
                        DeployTarget(
                            provider="volcengine",
                            product_type="clb",
                            product_id=listener.get("ListenerId"),
                            domain=listener.get("ListenerName"),
                            metadata={
                                "protocol": listener.get("Protocol"),
                                "port": listener.get("Port"),
                            },
                        )
                    )
        except Exception as e:
            LOGGER.warning("获取火山引擎 CLB 监听器失败: %s", e)
        
        return targets


