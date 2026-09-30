"""
测试 Nginx 静态部署：证书通过 SSH 推到 nginx_hosts，并按 nginx_deploy_rules 匹配。
需在 config.yaml 中配置好 nginx.nginx_hosts、nginx_target_groups、nginx_deploy_rules 及 ssh_profiles。
"""
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

from app.schemas.acme import RenewedCert
from app.services.deploy import DeployService
from app.config import load_raw_config
from app.utils.logger import get_logger


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CERTS_DIR = PROJECT_ROOT / "certs"

# 证书文件名：需与 nginx_deploy_rules.cert_domains 中某条能匹配上的域名一致（或主域名）
NGINX_CERT_PATH = CERTS_DIR / "vpn.gw6re98mzl.sqxs123.com.pem"
NGINX_KEY_PATH = CERTS_DIR / "vpn.gw6re98mzl.sqxs123.com.key"

log = get_logger("test-deploy-nginx")


def build_renewed_cert_from_files(
    domain: str,
    cert_path: Path,
    key_path: Path,
    sans: Optional[List[str]] = None,
) -> RenewedCert:
    """使用本地证书文件构造一个仅用于 Nginx 部署测试的 RenewedCert。"""
    if not cert_path.exists():
        raise FileNotFoundError(f"证书文件不存在: {cert_path}")
    if not key_path.exists():
        raise FileNotFoundError(f"私钥文件不存在: {key_path}")

    cert_pem = cert_path.read_text("utf-8")
    key_pem = key_path.read_text("utf-8")

    now = datetime.utcnow()
    return RenewedCert(
        domain=domain,
        cert_path=str(cert_path),
        key_path=str(key_path),
        fullchain_path=str(cert_path),
        ca_path=None,
        cert_pem=cert_pem,
        key_pem=key_pem,
        fullchain_pem=cert_pem,
        ca_pem="",
        issued_at=now - timedelta(days=1),
        expires_at=now + timedelta(days=90),
        key_length="2048",
        sans=list(sans) if sans else [],
        issuer="manual-test-nginx",
        profile=None,
    )


async def main() -> None:
    # 1. 主域名：必须与 config 里某条 nginx_deploy_rules.cert_domains 匹配（去掉 *. 后一致即命中）
    domain = "vpn.gw6re98mzl.sqxs123.com"

    # 2. 若只想看 Nginx 规划、不推云上，可指定一个该证书未绑定的 provider 以跳过云部署；
    #    为 None 时会按证书域名匹配所有云 + Nginx。
    provider_name = None

    conf = load_raw_config()
    deploy_mode = conf.deploy.mode.strip().lower()
    log.info("当前 deploy.mode=%s", deploy_mode)

    renewed_cert = build_renewed_cert_from_files(
        domain=domain,
        cert_path=NGINX_CERT_PATH,
        key_path=NGINX_KEY_PATH,
        sans=["*.vpn.gw6re98mzl.sqxs123.com"],
    )

    service = DeployService()

    if deploy_mode == "apply":
        log.info("以 APPLY 模式执行（云部署 + Nginx 静态部署）")
        result = await service.apply_deploy(
            renewed_cert=renewed_cert,
            provider_name=provider_name,
        )
    else:
        log.info("以 DRY-RUN 模式执行，只看匹配结果与 Nginx 规划，不真正部署")
        result = await service.plan_dry_run(
            renewed_cert=renewed_cert,
            provider_name=provider_name,
        )

    from pprint import pprint

    pprint(result)

    if deploy_mode == "apply":
        failed = int(result.get("failed_count", 0))
        success = int(result.get("success_count", 0))
        nginx_count = int(result.get("nginx_target_count", 0))
        if failed > 0:
            raise RuntimeError(
                f"部署完成，但存在 {failed} 个失败目标（成功 {success} 个，Nginx {nginx_count} 个），详情见上方 result"
            )
        if nginx_count == 0 and success == 0:
            raise RuntimeError(
                "没有任何部署成功记录，请检查 nginx_deploy_rules.cert_domains 是否命中证书域名及 ssh 配置"
            )


if __name__ == "__main__":
    asyncio.run(main())

