"""华为云证书部署器 - 支持部署到 CDN、ELB 和 WAF。"""

from __future__ import annotations

import ipaddress
from typing import List, Optional, Set, Tuple
from app.utils.logger import get_logger
from huaweicloudsdkcdn.v2 import model as cdn_models
from huaweicloudsdkelb.v3 import model as elb_models
from huaweicloudsdkwaf.v1 import model as waf_models

from .base import CertificateDeployer, DeployResult, DeployTarget
from app.utils.cert_parser import parse_cert_info_from_pem
from app.utils.domain_match import match_domain, normalize_domain
from app.utils.huawei_sdk import (
    HuaweiBasicCredentialMixin,
    HuaweiCdnClientMixin,
    build_elb_client,
    build_waf_client,
)
from app.utils.time import TimeUtil

LOGGER = get_logger("deployer")


class HuaweiDeployer(HuaweiBasicCredentialMixin, HuaweiCdnClientMixin, CertificateDeployer):
    """华为云证书部署器，支持 CDN、ELB 和 WAF。"""

    def __init__(
        self,
        access_key_id: str,
        access_key_secret: str,
        project_id: str = "",
        region: str = "cn-north-4",
        enterprise_project_id: str = "0",
    ) -> None:
        self._ak = access_key_id
        self._sk = access_key_secret
        self._project_id = project_id
        self._region = region
        self._enterprise_project_id = enterprise_project_id or "0"
        self._credentials = None
        credentials = self._get_credentials()
        self._cdn_client = None  # lazy via mixin
        self._elb_client = build_elb_client(region=region, credentials=credentials)
        self._waf_client = build_waf_client(region=region, credentials=credentials)
        self._waf_certificate_id: Optional[str] = None
        self._waf_certificate_name = ""
        self._waf_certificate_prepared = False

    @property
    def name(self) -> str:
        return "huawei"

    async def upload_certificate(self, cert_pem: str, key_pem: str, main_domain: str | None = None) -> Optional[str]:
        """上传证书到华为云 ELB 证书管理，一次上传供同一账号下多个 ELB 监听器复用。

        alias 命名：优先使用证书主域名，fallback 为 renew 前缀。
        """
        ts = TimeUtil.now().strftime('%Y%m%d%H%M%S')
        base = (main_domain or '').strip()
        if base:
            alias = f"{base}-{ts}"
        else:
            alias = f"renew-{ts}"
        return await self._upload_certificate(cert_pem, key_pem, alias)

    async def prepare_certificate(
        self,
        cert_pem: str,
        key_pem: str,
        main_domain: str | None,
        targets: List[DeployTarget],
    ) -> Optional[str]:
        """按命中产品分别准备 ELB 与 WAF 证书，避免跨产品误用证书 ID。"""
        product_types = {str(item.product_type or "").strip().lower() for item in targets}
        ts = TimeUtil.now().strftime("%Y%m%d%H%M%S")
        cert_name = f"{(main_domain or 'renew').strip() or 'renew'}-{ts}"

        if "waf" in product_types:
            self._waf_certificate_id = await self._upload_waf_certificate(
                cert_pem,
                key_pem,
                cert_name,
            )
            self._waf_certificate_prepared = True

        if "elb" in product_types:
            return await self._upload_certificate(cert_pem, key_pem, cert_name)
        return None

    async def _upload_waf_certificate(
        self,
        cert_pem: str,
        key_pem: str,
        name: str,
    ) -> Optional[str]:
        """上传证书到华为云 WAF 证书仓库。"""
        try:
            request = waf_models.CreateCertificateRequest(
                enterprise_project_id=self._enterprise_project_id,
                verify_cert_key=True,
                body=waf_models.CreateCertificateRequestBody(
                    name=name,
                    content=cert_pem,
                    key=key_pem,
                ),
            )
            response = self._waf_client.create_certificate(request)
            cert_id = str(response.id or "").strip()
            if not cert_id:
                LOGGER.error("华为云 WAF 证书上传未返回证书 ID: name=%s", name)
                return None
            self._waf_certificate_name = str(response.name or name).strip() or name
            LOGGER.info(
                "华为云 WAF 证书上传成功: name=%s cert_id=%s enterprise_project_id=%s",
                self._waf_certificate_name,
                cert_id,
                self._enterprise_project_id,
            )
            return cert_id
        except Exception as exc:
            LOGGER.error("华为云 WAF 证书上传失败: name=%s err=%s", name, exc)
            return None

    async def _deploy_to_waf(
        self,
        cert_id: str,
        host_id: str,
        enterprise_project_id: str,
        domain: str,
    ) -> bool:
        """将 WAF 证书绑定到单个云模式防护域名。"""
        request = waf_models.ApplyCertificateToHostRequest(
            enterprise_project_id=enterprise_project_id or "0",
            certificate_id=cert_id,
            body=waf_models.ApplyCertificateToHostRequestBody(
                cloud_host_ids=[host_id],
            ),
        )
        self._waf_client.apply_certificate_to_host(request)
        LOGGER.info(
            "华为云 WAF 证书部署成功: domain=%s host_id=%s cert_id=%s enterprise_project_id=%s",
            domain,
            host_id,
            cert_id,
            enterprise_project_id,
        )
        return True

    async def _upload_certificate(
        self,
        cert_pem: str,
        key_pem: str,
        name: str,
    ) -> Optional[str]:
        """上传证书到华为云 ELB 证书管理，并根据证书解析域名以支持 SNI。"""
        try:
            # 解析证书中的所有域名（优先使用 SAN）
            parsed = parse_cert_info_from_pem(cert_pem)
            domains = sorted(set(parsed.sans or []))
            domain_field = ",".join(domains) if domains else ""

            client = self._elb_client
            request = elb_models.CreateCertificateRequest(
                body=elb_models.CreateCertificateRequestBody(
                    certificate=elb_models.CreateCertificateOption(
                        name=name,
                        type="server",
                        certificate=cert_pem,
                        private_key=key_pem,
                        # domain 字段用于 ELB SNI 校验，需显式写入证书支持的域名列表
                        domain=domain_field or None,
                    )
                )
            )
            
            response = client.create_certificate(request)
            cert = response.certificate
            cert_id = str(cert.id or "").strip() if cert else ""
            if not cert_id:
                return None

            LOGGER.info(
                "华为云 ELB 证书上传成功: %s (ID: %s, domains=%s)",
                name,
                cert_id,
                domain_field,
            )
            return cert_id
            
        except Exception as e:
            LOGGER.error("华为云 ELB 证书上传失败: %s", e)
            return None

    async def _deploy_to_cdn(
        self, cert_pem: str, key_pem: str, domain: str
    ) -> bool:
        """部署证书到华为云 CDN。"""
        try:
            client = self._get_cdn_client()
            cert_name = f"{domain}-{TimeUtil.now().strftime('%Y%m%d%H%M%S')}"
            
            request = cdn_models.UpdateDomainFullConfigRequest(
                domain_name=domain,
                body=cdn_models.ModifyDomainConfigRequestBody(
                    configs=cdn_models.Configs(
                        https=cdn_models.HttpPutBody(
                            https_status="on",
                            certificate_name=cert_name,
                            certificate_value=cert_pem,
                            private_key=key_pem,
                            certificate_source=0,
                            http2_status="on",
                        )
                    )
                )
            )
            
            client.update_domain_full_config(request)
            LOGGER.info("华为云 CDN 证书部署成功: %s", domain)
            return True
            
        except Exception as e:
            LOGGER.error("华为云 CDN 证书部署失败: %s", e)
            return False

    @staticmethod
    def _split_domain_candidates(raw: str) -> List[str]:
        text = str(raw or "").strip()
        if not text:
            return []
        text = text.replace(";", ",")
        return [item.strip() for item in text.split(",") if item.strip()]

    def _get_cert_domain_info(self, cert_id: str) -> Tuple[Set[str], Set[str]]:
        """查询证书覆盖的域名，返回 (具体域名集合, 通配符后缀集合)。失败返回 (set(), set())。"""

        def _norm(s: str) -> Optional[str]:
            v = str(s or "").strip().lower().rstrip(".")
            if not v or v.startswith("*."):
                return None
            try:
                ipaddress.ip_address(v)
                return None
            except ValueError:
                return v

        try:
            req = elb_models.ShowCertificateRequest(certificate_id=cert_id)
            resp = self._elb_client.show_certificate(req)
            cert = resp.certificate
        except Exception:
            return (set(), set())

        concrete: Set[str] = set()
        wildcard_suffixes: Set[str] = set()
        sources: List[str] = [cert.common_name or ""]
        sources.extend(cert.subject_alternative_names or [])
        sources.extend(self._split_domain_candidates(cert.domain or ""))

        for raw in sources:
            raw = str(raw or "").strip().lower()
            d = _norm(raw)
            if d:
                concrete.add(d)
            if raw.startswith("*.") and len(raw) > 2:
                wildcard_suffixes.add(raw[2:].rstrip("."))

        return (concrete, wildcard_suffixes)

    def _cert_covers_domain(self, cert_id: str, target_domain: str) -> bool:
        """判断证书是否覆盖目标域名（含通配符匹配）。无法查询证书时返回 False。"""
        if not (cert_id and target_domain):
            return False
        concrete, wildcard_suffixes = self._get_cert_domain_info(cert_id)
        if not concrete and not wildcard_suffixes:
            return False
        t = target_domain.strip().lower().rstrip(".")
        if t in concrete:
            return True
        for suffix in wildcard_suffixes:
            if t == suffix or t.endswith("." + suffix):
                return True
        return False

    def _certs_san_overlap(self, cert_id_a: str, cert_id_b: str) -> bool:
        """两台证书是否在命名空间上存在交集（用于同监听器 default 与 SNI 一并换证）。"""
        if not cert_id_a or not cert_id_b or cert_id_a == cert_id_b:
            return False
        ca, wa = self._get_cert_domain_info(cert_id_a)
        cb, wb = self._get_cert_domain_info(cert_id_b)
        if (not ca and not wa) or (not cb and not wb):
            return False
        for d in ca:
            if d and self._cert_covers_domain(cert_id_b, d):
                return True
        for d in cb:
            if d and self._cert_covers_domain(cert_id_a, d):
                return True
        for sfx in wa:
            if self._wildcard_suffix_touches_cert(sfx, cb, wb):
                return True
        for sfx in wb:
            if self._wildcard_suffix_touches_cert(sfx, ca, wa):
                return True
        return False

    @staticmethod
    def _wildcard_suffix_touches_cert(suffix: str, concrete: Set[str], wild: Set[str]) -> bool:
        """通配 *.suffix 是否与另一张证书的 SAN 有公共可服务名。"""
        sfx = (suffix or "").strip().lower().rstrip(".")
        if not sfx:
            return False
        for d in concrete:
            dd = (d or "").strip().lower().rstrip(".")
            if not dd:
                continue
            if dd == sfx or dd.endswith("." + sfx):
                rest = dd[: -len(sfx) - 1] if len(dd) > len(sfx) else ""
                if "." not in rest:
                    return True
        for w in wild:
            if w == sfx:
                return True
        return False

    def _new_cert_covers_old_cert(
        self,
        new_cert_domains: Set[str],
        old_cert_id: str,
        previous_cert_id: Optional[str] = None,
    ) -> bool:
        """判断新证书是否覆盖旧证书的全部域名。

        始终通过 ShowCertificate 查询旧证书域名，逐一验证新证书是否覆盖。
        仅当 ShowCertificate 不可用（查不到旧证书信息）且 old_cert_id 恰好是上游匹配
        确认过的 previous_cert_id 时，才作为兜底允许替换。

        覆盖判定遵循 RFC 6125 §6.4.3 单级通配符语义：例如新证书的 ``*.example.com``
        不视为能覆盖旧证书的 ``*.api.example.com``，因为换证后浏览器实际请求
        ``foo.api.example.com`` 仍会因证书不匹配失败。

        Args:
            new_cert_domains: 新证书域名集合（含通配符，如 ``{"qq.com", "*.qq.com"}``）
            old_cert_id: 当前引用的旧证书 ID
            previous_cert_id: 上游匹配环节明确要替换的旧证书 ID（仅用于 API 不可用时兜底）
        Returns:
            True 表示可以安全替换
        """
        if not old_cert_id:
            return False
        # 查询旧证书的域名信息
        old_concrete, old_wildcard = self._get_cert_domain_info(old_cert_id)
        if not old_concrete and not old_wildcard:
            # ShowCertificate 不可用或旧证书无域名信息
            if previous_cert_id and old_cert_id == previous_cert_id:
                LOGGER.warning(
                    "旧证书域名信息不可用，基于上游匹配兜底允许替换: old_cert=%s",
                    old_cert_id,
                )
                return True
            return False
        # 每个具体域名都必须被新证书覆盖
        for d in old_concrete:
            if not match_domain(new_cert_domains, d):
                return False
        # 每个通配符也必须被新证书覆盖（*.suffix → 必须命中新证书中的 *.suffix，
        # 不接受 *.parent_suffix 这种降级匹配）
        for suffix in old_wildcard:
            wildcard = f"*.{suffix}"
            if not match_domain(new_cert_domains, wildcard):
                return False
        return True

    async def _deploy_to_elb(
        self,
        cert_id: str,
        listener_id: str,
        new_cert_domains: Set[str],
        previous_cert_id: Optional[str] = None,
        domain: str = "",
    ) -> bool:
        """部署证书到华为云 ELB 监听器。

        基于新证书域名覆盖范围，统一判断并更新默认证书和 SNI 列表中需要替换的引用。
        幂等：如果所有引用已经指向新证书，视为成功。

        Args:
            cert_id: 新上传的证书 ID
            listener_id: ELB 监听器 ID
            new_cert_domains: 新证书的域名集合（从 PEM 解析，不依赖 API）
            previous_cert_id: 上游匹配确认的旧证书 ID（用于 ShowCertificate 不可用时的兜底）
            domain: 触发本次部署的域名（仅用于日志）
        """
        try:
            client = self._elb_client

            show_req = elb_models.ShowListenerRequest(listener_id=listener_id)
            show_resp = client.show_listener(show_req)
            listener = show_resp.listener

            default_ref = str(listener.default_tls_container_ref or "").strip()
            sni_refs = [str(item or "").strip() for item in (listener.sni_container_refs or [])]

            def _parse_cert_id(cert_ref: str) -> str:
                value = str(cert_ref or "").strip().rstrip("/")
                if not value:
                    return ""
                if "/" not in value:
                    return value
                return value.rsplit("/", 1)[-1]

            def _format_like(ref: str, new_cert_id: str) -> str:
                ref_text = str(ref or "").strip()
                if "/" not in ref_text:
                    return new_cert_id
                base = ref_text.rstrip("/").rsplit("/", 1)[0]
                return f"{base}/{new_cert_id}"

            # --- 判断默认证书是否需要替换 ---
            need_update_default = False
            new_default_ref = default_ref
            default_cid = _parse_cert_id(default_ref)

            if default_ref and default_cid:
                if default_cid == cert_id:
                    LOGGER.debug("默认证书已是新证书: listener=%s", listener_id)
                elif self._new_cert_covers_old_cert(new_cert_domains, default_cid, previous_cert_id):
                    need_update_default = True
                    new_default_ref = _format_like(default_ref, cert_id)
                    LOGGER.info(
                        "华为云 ELB 默认证书域名匹配，将更新: listener=%s old=%s new=%s",
                        listener_id, default_cid, cert_id,
                    )
                else:
                    LOGGER.debug(
                        "华为云 ELB 默认证书域名不匹配，跳过: listener=%s default_cert=%s",
                        listener_id, default_cid,
                    )

            # --- 判断 SNI 列表中哪些证书需要替换 ---
            new_sni_refs: List[str] = []
            sni_changed = False
            for ref in sni_refs:
                ref_cid = _parse_cert_id(ref)
                if not ref_cid:
                    new_sni_refs.append(ref)
                    continue
                if ref_cid == cert_id:
                    new_sni_refs.append(ref)
                    continue
                if self._new_cert_covers_old_cert(new_cert_domains, ref_cid, previous_cert_id):
                    new_sni_refs.append(_format_like(ref, cert_id))
                    sni_changed = True
                    LOGGER.info(
                        "华为云 ELB SNI 证书域名匹配，将更新: listener=%s old=%s new=%s",
                        listener_id, ref_cid, cert_id,
                    )
                else:
                    new_sni_refs.append(ref)

            # --- 没有任何需要更新的引用 ---
            if not need_update_default and not sni_changed:
                all_already_new = (
                    (not default_cid or default_cid == cert_id)
                    and all(_parse_cert_id(r) == cert_id for r in sni_refs)
                )
                if all_already_new:
                    LOGGER.info("华为云 ELB 监听器证书已是最新: listener=%s", listener_id)
                    return True
                LOGGER.warning(
                    "华为云 ELB 没有需要更新的证书引用: listener=%s new_cert=%s domain=%s",
                    listener_id, cert_id, domain,
                )
                return True

            # --- 构建 UpdateListener 请求 ---
            update_kwargs: dict = {}
            if need_update_default:
                update_kwargs["default_tls_container_ref"] = new_default_ref
            if sni_changed:
                update_kwargs["sni_container_refs"] = new_sni_refs
            elif need_update_default and sni_refs:
                # 只改了默认证书，但提交完整 SNI 列表以防控制台显示不一致
                update_kwargs["sni_container_refs"] = new_sni_refs

            LOGGER.info(
                "华为云 ELB 更新监听器: listener=%s update_default=%s sni_changed=%s sni_count=%s",
                listener_id, need_update_default, sni_changed, len(new_sni_refs),
            )

            update_opt = elb_models.UpdateListenerOption(**update_kwargs)
            request = elb_models.UpdateListenerRequest(
                listener_id=listener_id,
                body=elb_models.UpdateListenerRequestBody(listener=update_opt),
            )
            client.update_listener(request)
            LOGGER.info("华为云 ELB 证书部署成功: listener=%s", listener_id)
            return True

        except Exception as e:
            LOGGER.error("华为云 ELB 证书部署失败: listener=%s err=%s", listener_id, e)
            raise

    @staticmethod
    def _build_new_cert_domains(cert_pem: str) -> Set[str]:
        """从 PEM 解析新证书覆盖的所有域名（含通配符），用于部署时的域名覆盖判断。"""
        parsed = parse_cert_info_from_pem(cert_pem)
        domains: Set[str] = set()
        for san in (parsed.sans or []):
            v = str(san or "").strip().lower().rstrip(".")
            if v:
                domains.add(v)
        return domains

    async def deploy(
        self,
        cert_pem: str,
        key_pem: str,
        target: DeployTarget,
        cert_id: Optional[str] = None,
    ) -> DeployResult:
        """部署证书到华为云 CDN、ELB 或 WAF。"""
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
            
            elif product_type == "elb":
                if cert_id is None:
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
                        message="未指定 ELB 监听器 ID",
                    )

                previous_cert_id = None
                if target.metadata:
                    previous_cert_id = target.metadata.get("previous_cert_id")

                # 从 PEM 解析新证书域名，不依赖 ShowCertificate API
                new_cert_domains = self._build_new_cert_domains(cert_pem)

                success = await self._deploy_to_elb(
                    cert_id=cert_id,
                    listener_id=listener_id,
                    new_cert_domains=new_cert_domains,
                    previous_cert_id=previous_cert_id,
                    domain=domain,
                )
                return DeployResult(
                    success=success,
                    target=target,
                    message="ELB 证书部署成功" if success else "ELB 证书部署失败",
                    cert_id=cert_id,
                )

            elif product_type == "waf":
                host_id = str(target.product_id or "").strip()
                if not host_id:
                    return DeployResult(
                        success=False,
                        target=target,
                        message="未指定 WAF 防护域名 ID",
                    )

                waf_cert_id = self._waf_certificate_id
                if not self._waf_certificate_prepared:
                    cert_name = f"{domain}-{TimeUtil.now().strftime('%Y%m%d%H%M%S')}"
                    waf_cert_id = await self._upload_waf_certificate(
                        cert_pem,
                        key_pem,
                        cert_name,
                    )
                    self._waf_certificate_id = waf_cert_id
                    self._waf_certificate_prepared = True
                if not waf_cert_id:
                    return DeployResult(
                        success=False,
                        target=target,
                        message="WAF 证书上传失败",
                    )

                metadata = target.metadata or {}
                enterprise_project_id = str(
                    metadata.get("enterprise_project_id") or self._enterprise_project_id
                ).strip() or "0"
                success = await self._deploy_to_waf(
                    cert_id=waf_cert_id,
                    host_id=host_id,
                    enterprise_project_id=enterprise_project_id,
                    domain=domain,
                )
                return DeployResult(
                    success=success,
                    target=target,
                    message="WAF 证书部署成功" if success else "WAF 证书部署失败",
                    cert_id=waf_cert_id,
                )
            
            else:
                return DeployResult(
                    success=False,
                    target=target,
                    message=f"不支持的产品类型: {product_type}",
                )
                
        except Exception as e:
            LOGGER.error("华为云证书部署异常: %s", e)
            return DeployResult(
                success=False,
                target=target,
                message=str(e),
            )

    async def list_targets(self) -> List[DeployTarget]:
        """列出所有可部署目标（基类抽象方法）。"""
        return self.list_bindable_resources()

    def list_bindable_resources(self) -> List[DeployTarget]:
        """列出可绑定证书的资源。"""
        targets = []
        
        # 获取 CDN 域名
        try:
            client = self._get_cdn_client()
            request = cdn_models.ListDomainsRequest()
            response = client.list_domains(request)
            
            for domain in response.domains or []:
                targets.append(
                    DeployTarget(
                        provider="huawei",
                        product_type="cdn",
                        product_id=domain.id,
                        domain=domain.domain_name,
                    )
                )
        except Exception as e:
            LOGGER.warning("获取华为云 CDN 域名失败: %s", e)
        
        # 获取 ELB 监听器
        try:
            client = self._elb_client
            request = elb_models.ListListenersRequest()
            response = client.list_listeners(request)
            
            for listener in response.listeners or []:
                if listener.protocol in ("HTTPS", "TERMINATED_HTTPS"):
                    targets.append(
                        DeployTarget(
                            provider="huawei",
                            product_type="elb",
                            product_id=listener.id,
                            domain=listener.name,
                            metadata={
                                "protocol": listener.protocol,
                                "port": listener.protocol_port,
                            },
                        )
                    )
        except Exception as e:
            LOGGER.warning("获取华为云 ELB 监听器失败: %s", e)
        
        return targets
