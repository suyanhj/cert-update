"""七牛云 CDN 证书部署器。"""

from __future__ import annotations

from typing import Any, List, Optional
from urllib.parse import quote, urlencode

from app.utils.logger import get_logger
from app.utils.qiniu_api import (
    QINIU_CERT_API,
    QINIU_DOMAIN_API,
    QiniuAPIError,
    build_qiniu_auth,
    list_qiniu_domains,
    qiniu_request,
)
from app.utils.time import TimeUtil

from .base import CertificateDeployer, DeployResult, DeployTarget

LOGGER = get_logger("deployer")


class QiniuDeployer(CertificateDeployer):
    """上传证书并部署到七牛云 CDN 域名。"""

    def __init__(self, access_key_id: str, access_key_secret: str) -> None:
        self._ak = access_key_id
        self._sk = access_key_secret
        self._auth = None

    @property
    def name(self) -> str:
        return "qiniu"

    def _get_auth(self):
        """构建并缓存七牛云双鉴权对象。"""
        if self._auth is None:
            self._auth = build_qiniu_auth(self._ak, self._sk)
        return self._auth

    def _make_request(
        self,
        url: str,
        method: str = "GET",
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return qiniu_request(
            auth=self._get_auth(),
            url=url,
            method=method,
            body=body,
        )

    async def upload_certificate(
        self,
        cert_pem: str,
        key_pem: str,
        main_domain: str | None = None,
    ) -> Optional[str]:
        """上传一次证书，供同一账号下多个 CDN 目标复用。"""
        base_name = str(main_domain or "").strip() or "renew"
        cert_name = f"{base_name}-{TimeUtil.now().strftime('%Y%m%d%H%M%S')}"
        return await self._upload_certificate(cert_pem, key_pem, cert_name)

    async def _upload_certificate(
        self,
        cert_pem: str,
        key_pem: str,
        name: str,
    ) -> str:
        if not cert_pem or not key_pem:
            raise ValueError("上传七牛云证书失败：cert_pem/key_pem 不能为空")

        response = self._make_request(
            QINIU_CERT_API,
            "POST",
            {
                "name": name,
                "pri": key_pem,
                "ca": cert_pem,
            },
        )
        cert_id = str(response.get("certID") or "").strip()
        if not cert_id:
            raise RuntimeError(f"七牛云证书上传未返回 certID: name={name}")

        LOGGER.info("七牛云证书上传成功: name=%s cert_id=%s", name, cert_id)
        return cert_id

    async def _deploy_to_cdn(
        self,
        cert_id: str,
        target: DeployTarget,
        domain: str,
    ) -> None:
        protocol = str((target.metadata or {}).get("protocol") or "").strip().lower()
        encoded_domain = quote(domain, safe=".*-")
        if protocol == "http":
            action = "sslize"
            body: dict[str, Any] = {
                "certId": cert_id,
                "forceHttps": False,
                "http2Enable": True,
            }
        else:
            action = "httpsconf"
            body = {"certId": cert_id}

        LOGGER.info(
            "七牛云 CDN 准备更新证书: domain=%s protocol=%s action=%s cert_id=%s",
            domain,
            protocol or "unknown",
            action,
            cert_id,
        )
        self._make_request(
            f"{QINIU_DOMAIN_API}/{encoded_domain}/{action}",
            "PUT",
            body,
        )

    @staticmethod
    def _validate_target(target: DeployTarget) -> str:
        product_type = str(target.product_type or "").strip().lower()
        if product_type == "cdn":
            domain = str(target.domain or target.product_id or "").strip()
            if not domain:
                raise ValueError("七牛云 CDN 部署需要指定域名")
            return domain
        if product_type == "oss":
            product_id = str(target.product_id or "").strip()
            if ":" not in product_id:
                raise ValueError("七牛云对象存储 product_id 必须为 <bucket>:<domain>")
            bucket, product_domain = [part.strip() for part in product_id.split(":", 1)]
            if not bucket or not product_domain:
                raise ValueError("七牛云对象存储 product_id 必须为 <bucket>:<domain>")
            target_domain = str(target.domain or product_domain).strip()
            if target_domain != product_domain:
                raise ValueError(
                    "七牛云对象存储 target.domain 与 product_id 中的域名不一致"
                )
            return product_domain
        raise ValueError(f"七牛云不支持产品类型: {target.product_type}")

    async def deploy(
        self,
        cert_pem: str,
        key_pem: str,
        target: DeployTarget,
        cert_id: Optional[str] = None,
    ) -> DeployResult:
        """部署证书到一个七牛云 CDN 目标。"""
        product_type = str(target.product_type or "").strip().lower()
        if product_type not in {"cdn", "oss"}:
            return DeployResult(
                success=False,
                target=target,
                message=f"七牛云仅支持 CDN/对象存储，不支持 {target.product_type}",
            )

        try:
            domain = self._validate_target(target)
            if not cert_id:
                cert_id = await self.upload_certificate(
                    cert_pem,
                    key_pem,
                    domain,
                )
            if not cert_id:
                raise RuntimeError("七牛云证书上传后未返回 certID")

            await self._deploy_to_cdn(cert_id, target, domain)
            LOGGER.info(
                "七牛云 %s 证书部署请求成功: domain=%s cert_id=%s",
                product_type,
                domain,
                cert_id,
            )
            return DeployResult(
                success=True,
                target=target,
                message=f"{product_type} 证书部署成功",
                cert_id=cert_id,
            )
        except Exception as exc:
            LOGGER.error(
                "七牛云 %s 证书部署失败: domain=%s error=%s",
                product_type,
                target.domain or target.product_id,
                exc,
                exc_info=True,
            )
            return DeployResult(
                success=False,
                target=target,
                message=str(exc),
                cert_id=cert_id,
            )

    async def list_targets(self) -> List[DeployTarget]:
        return await self.list_bindable_resources()

    async def list_bindable_resources(self) -> List[DeployTarget]:
        """列出账号下可绑定证书的 CDN 域名。"""
        targets: List[DeployTarget] = []
        try:
            for item in list_qiniu_domains(self._get_auth()):
                domain_name = str(item.get("name") or "").strip()
                product = str(item.get("product") or "cdn").strip().lower()
                if not domain_name or product != "cdn":
                    continue
                targets.append(
                    DeployTarget(
                        provider="qiniu",
                        product_type="cdn",
                        product_id=domain_name,
                        domain=domain_name,
                        metadata={
                            "type": item.get("type"),
                            "status": item.get("operatingState"),
                            "protocol": str(item.get("protocol") or "").lower(),
                            "cname": item.get("cname"),
                            "https": item.get("https") if isinstance(item.get("https"), dict) else {},
                        },
                    )
                )
            LOGGER.info("七牛云发现 %d 个可部署 CDN 目标", len(targets))
        except Exception as exc:
            LOGGER.warning("获取七牛云 CDN 域名失败: %s", exc, exc_info=True)
        return targets

    async def list_certificates(self) -> List[dict]:
        """分页列出已上传的七牛云证书。"""
        certs: List[dict] = []
        marker = ""
        seen_markers: set[str] = set()
        try:
            while True:
                params: dict[str, Any] = {"limit": 100}
                if marker:
                    params["marker"] = marker
                response = self._make_request(
                    f"{QINIU_CERT_API}?{urlencode(params)}"
                )
                page_certs = response.get("certs", [])
                if not isinstance(page_certs, list):
                    raise QiniuAPIError("七牛证书列表响应中的 certs 不是数组")
                certs.extend(item for item in page_certs if isinstance(item, dict))

                next_marker = str(response.get("marker") or "").strip()
                if not next_marker:
                    break
                if next_marker in seen_markers:
                    raise QiniuAPIError(
                        f"七牛证书分页 marker 未前进: marker={next_marker}"
                    )
                seen_markers.add(next_marker)
                marker = next_marker
            LOGGER.info("七牛云获取证书列表成功: count=%d", len(certs))
            return certs
        except Exception as exc:
            LOGGER.warning("获取七牛云证书列表失败: %s", exc, exc_info=True)
            return []

    async def delete_certificate(self, cert_id: str) -> bool:
        """删除一个未绑定的七牛云证书。"""
        cert_id_value = str(cert_id or "").strip()
        if not cert_id_value:
            raise ValueError("删除七牛云证书需要 cert_id")
        try:
            encoded_cert_id = quote(cert_id_value, safe="")
            self._make_request(
                f"{QINIU_CERT_API}/{encoded_cert_id}",
                "DELETE",
            )
            LOGGER.info("七牛云证书删除成功: cert_id=%s", cert_id_value)
            return True
        except Exception as exc:
            LOGGER.error(
                "七牛云证书删除失败: cert_id=%s error=%s",
                cert_id_value,
                exc,
                exc_info=True,
            )
            return False
