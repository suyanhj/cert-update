from dataclasses import dataclass, field
from typing import Any, List, Optional


@dataclass
class CloudProductBinding:
    """云产品域名绑定信息。"""

    product_type: str  # 如 cdn, clb, slb
    product_id: str  # 产品实例 ID
    domain: str  # 绑定的域名
    provider_name: str = ""  # 配置中的 provider 名称
    resource_type: str = ""  # 资源类型别名，默认与 product_type 一致
    resource_name: str = ""  # 资源展示名（可选）
    status: str = ""  # 产品状态
    cert_id: Optional[str] = None  # 当前绑定的证书 ID
    cert_expire_time: Optional[str] = None  # 当前证书过期时间
    listener_port: Optional[int] = None  # LB 监听端口
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.resource_type:
            self.resource_type = self.product_type


@dataclass
class DeployMatchResult:
    """续签后命中的云产品部署目标。"""

    provider_type: str
    provider_name: str
    product_type: str
    product_id: str
    domain: str
    match_type: str
    current_cert_id: Optional[str]
    listener_port: Optional[int] = None
    metadata: dict = field(default_factory=dict)


def binding_from_cache_dict(raw: dict[str, Any]) -> CloudProductBinding:
    product_type = str(raw.get("product_type", "") or "")
    product_id = str(raw.get("product_id", "") or "")
    domain = str(raw.get("domain", "") or "")
    if not product_type or not product_id or not domain:
        raise ValueError(f"缓存 binding 缺少关键字段: {raw}")

    listener_port = raw.get("listener_port")
    if listener_port is not None:
        try:
            listener_port = int(listener_port)
        except Exception as exc:
            raise ValueError(f"缓存 binding.listener_port 非法: {listener_port}") from exc

    metadata = raw.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise ValueError("缓存 binding.metadata 必须是 dict")
    if not isinstance(metadata, dict):
        metadata = {}

    return CloudProductBinding(
        provider_name=str(raw.get("provider_name", "") or ""),
        product_type=product_type,
        product_id=product_id,
        domain=domain,
        resource_type=str(raw.get("resource_type", "") or ""),
        resource_name=str(raw.get("resource_name", "") or ""),
        status=str(raw.get("status", "") or ""),
        cert_id=str(raw.get("cert_id")) if raw.get("cert_id") else None,
        cert_expire_time=str(raw.get("cert_expire_time")) if raw.get("cert_expire_time") else None,
        listener_port=listener_port,
        metadata=metadata,
    )

