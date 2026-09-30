from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


class AcmeShError(RuntimeError):
    """acme.sh 续签模块通用异常。"""


class AcmeShNotFoundError(AcmeShError):
    """acme.sh 可执行文件不存在或不可用。"""


class AcmeShConfigError(AcmeShError):
    """acme.sh 续签所需配置无效。"""


class AcmeShExecutionError(AcmeShError):
    """acme.sh 命令执行失败。"""


class AcmeShInfoParseError(AcmeShError):
    """`acme.sh --info` 输出解析失败。"""


@dataclass
class RenewedCert:
    """续签后的证书信息。"""

    domain: str
    cert_path: str
    key_path: str
    fullchain_path: str
    ca_path: Optional[str]
    cert_pem: str
    key_pem: str
    fullchain_pem: str
    ca_pem: str
    issued_at: datetime
    expires_at: datetime
    key_length: str
    sans: list[str]
    issuer: str
    profile: Optional[str]


class BaseAcmeRenewer(ABC):
    """acme 续签能力抽象。"""

    @abstractmethod
    def issue(
        self,
        domain: str,
        sans: list[str],
        provider_name: Optional[str] = None,
    ) -> RenewedCert:
        """首次签发指定域名和 SAN 的证书。"""
        raise NotImplementedError

    @abstractmethod
    def renew(
        self,
        domain: str,
        provider_name: Optional[str] = None,
        force: bool = False,
    ) -> RenewedCert:
        """
        续签指定域名证书。

        Args:
            domain: 续签目标域名。
            provider_name: 可选 provider 名称；为空时由实现类自行解析。
            force: 是否强制续签。
        """
        raise NotImplementedError

