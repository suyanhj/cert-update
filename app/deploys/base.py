"""证书部署器基类。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class DeployTarget:
    """部署目标。"""
    
    provider: str  # 云平台名称或 nginx
    product_type: str  # cdn, clb, slb, nginx
    product_id: Optional[str] = None  # 产品实例 ID
    domain: Optional[str] = None  # 关联域名
    listener_port: Optional[int] = None  # 监听端口（用于 LB）
    metadata: Optional[dict] = None  # 额外元数据


@dataclass
class DeployResult:
    """部署结果。"""
    
    success: bool
    target: DeployTarget
    message: str
    cert_id: Optional[str] = None  # 云平台证书 ID


class CertificateDeployer(ABC):
    """证书部署器基类。"""

    @property
    @abstractmethod
    def name(self) -> str:
        """部署器名称。"""

    async def upload_certificate(self, cert_pem: str, key_pem: str, main_domain: str | None = None) -> Optional[str]:
        """
        将证书上传到云厂商（如 CAS），返回 cert_id。
        若部署器不支持“先上传再复用”（如 nginx-ssh），返回 None，调用方会对每个目标单独调用 deploy。
        同一 provider 下可先调用一次本方法，再对多个目标调用 deploy(..., cert_id=...) 复用同一 cert_id。
        """
        return None

    async def prepare_certificate(
        self,
        cert_pem: str,
        key_pem: str,
        main_domain: str | None,
        targets: List[DeployTarget],
    ) -> Optional[str]:
        """按本次目标准备证书，默认沿用单证书仓库上传行为。"""
        return await self.upload_certificate(cert_pem, key_pem, main_domain)

    @abstractmethod
    async def deploy(
        self,
        cert_pem: str,
        key_pem: str,
        target: DeployTarget,
        cert_id: Optional[str] = None,
    ) -> DeployResult:
        """
        部署证书到目标。

        Args:
            cert_pem: 证书 PEM 内容
            key_pem: 私钥 PEM 内容
            target: 部署目标
            cert_id: 可选；若提供则不再重复上传证书，直接使用该 ID 绑定到目标（由 upload_certificate 获得）
        Returns:
            部署结果
        """

    @abstractmethod
    async def list_targets(self) -> List[DeployTarget]:
        """
        列出所有可部署的目标。
        
        Returns:
            部署目标列表
        """
