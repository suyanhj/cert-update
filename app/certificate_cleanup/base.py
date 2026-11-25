"""证书清理平台适配器的公共契约。"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Sequence


class CertificateSource(str, Enum):
    """远端证书来源分类。"""

    UPLOADED = "uploaded"
    MANAGED = "managed"
    CLOUD_ISSUED = "cloud_issued"
    PURCHASED = "purchased"
    UNKNOWN = "unknown"

    @property
    def allows_delete(self) -> bool:
        return self in {CertificateSource.UPLOADED, CertificateSource.MANAGED}


class AssociationStatus(str, Enum):
    """证书与云资源的关联状态。"""

    UNBOUND = "unbound"
    BOUND = "bound"
    UNKNOWN = "unknown"


class DeleteOutcome(str, Enum):
    """平台删除调用的标准结果。"""

    DELETED = "deleted"
    IN_USE = "in_use"
    NOT_FOUND = "not_found"


class IncompleteInventoryError(RuntimeError):
    """证书或关联资源盘点无法证明完整。"""


class InventoryUnavailableError(IncompleteInventoryError):
    """平台明确表明当前证书库能力不可用。"""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


_PERMISSION_CODES = {
    "accessdenied",
    "forbidden",
    "nopermission",
    "operationdenied",
    "permissiondenied",
}
_STATUS_403 = re.compile(
    r"(?i)(?:status(?:_code|code)?|code)\s*['\"]?\s*[:=]\s*['\"]?403\b"
)


def _exception_attribute_values(error: BaseException) -> list[Any]:
    values: list[Any] = []
    for name in ("status_code", "statusCode", "status", "code", "error_code"):
        value = getattr(error, name, None)
        if value is not None:
            values.append(value)
    for name in ("get_status_code", "get_code"):
        getter = getattr(error, name, None)
        if not callable(getter):
            continue
        try:
            value = getter()
        except Exception:
            continue
        if value is not None:
            values.append(value)
    return values


def classify_inventory_unavailable(error: BaseException) -> str | None:
    """识别可预期的库存能力缺失；不用于删除阶段错误降级。"""
    current: BaseException | None = error
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, InventoryUnavailableError):
            return current.reason

        text = str(current)
        lowered = text.lower()
        if (
            "is not in the following supported regions of service" in lowered
            or "unsupported region" in lowered
        ):
            return "unsupported_region"

        values = _exception_attribute_values(current)
        if any(str(value).strip() == "403" for value in values):
            return "permission_denied"
        normalized_codes = {
            re.sub(r"[^a-z0-9]", "", str(value).strip().lower())
            for value in values
        }
        if normalized_codes & _PERMISSION_CODES:
            return "permission_denied"
        if _STATUS_403.search(text):
            return "permission_denied"

        current = current.__cause__ or current.__context__
    return None


@dataclass(frozen=True)
class CertificateStoreId:
    """一个独立证书库的稳定标识。"""

    provider_type: str
    provider_name: str
    store: str
    scope: str = "global"

    @property
    def key(self) -> str:
        return ":".join(
            (self.provider_type, self.provider_name, self.store, self.scope)
        )

    @property
    def delete_scope_key(self) -> tuple[str, str, str]:
        return self.provider_type, self.provider_name, self.store


@dataclass(frozen=True)
class RemoteCertificate:
    """清理流程所需的最小远端证书元数据。"""

    certificate_id: str
    name: str
    expires_at: datetime
    source: CertificateSource
    request_id: str = ""

    def __post_init__(self) -> None:
        if not str(self.certificate_id or "").strip():
            raise ValueError("远端证书 ID 不能为空")
        if self.expires_at.tzinfo is None:
            raise ValueError("远端证书到期时间必须包含时区")


@dataclass(frozen=True)
class AssociationCheck:
    """一次权威关联检查结果。"""

    status: AssociationStatus
    complete: bool
    resources: tuple[str, ...] = ()
    request_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status is AssociationStatus.UNBOUND and not self.complete:
            raise ValueError("不完整的关联检查不能声明证书未关联")
        if self.status is AssociationStatus.BOUND and not self.resources:
            raise ValueError("已关联结果必须包含至少一个资源标识")


@dataclass(frozen=True)
class DeleteReceipt:
    """平台安全删除结果。"""

    outcome: DeleteOutcome
    request_id: str = ""
    task_id: str = ""
    message: str = ""


@dataclass
class CleanupRunSummary:
    """一个证书库在一轮清理中的审计摘要。"""

    store_id: CertificateStoreId
    scanned: int = 0
    candidates: int = 0
    confirmed: int = 0
    deleted: int = 0
    bound: int = 0
    report_only: int = 0
    deferred: int = 0
    unavailable: bool = False
    unavailable_reason: str = ""
    failed: bool = False
    error: str = ""
    items: list[dict[str, str]] = field(default_factory=list)


class CertificateCleanupAdapter(ABC):
    """严格证书库适配器；失败必须抛错，禁止用空列表兜底。"""

    store_id: CertificateStoreId
    safe_delete: bool = False

    @abstractmethod
    async def list_certificates(self) -> Sequence[RemoteCertificate]:
        """完整列出当前证书库中的证书。"""

    @abstractmethod
    async def check_associations(self, certificate_id: str) -> AssociationCheck:
        """权威检查证书是否被云资源使用。"""

    @abstractmethod
    async def get_certificate(self, certificate_id: str) -> RemoteCertificate | None:
        """实时读取证书；不存在时返回 None。"""

    async def delete_certificate(self, certificate_id: str) -> DeleteReceipt:
        """安全删除证书；只报告的适配器不得实现删除。"""
        raise RuntimeError(f"证书库仅支持报告，禁止删除: {self.store_id.key}")
