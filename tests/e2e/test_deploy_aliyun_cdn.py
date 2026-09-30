import asyncio
from datetime import datetime, timedelta
from pathlib import Path

from app.schemas.acme import RenewedCert
from app.services.deploy import DeployService
from app.config import load_raw_config
from app.utils.logger import get_logger


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CERTS_DIR = PROJECT_ROOT / "certs"

# 根据你实际的 CDN 证书文件名调整下面两行
CDN_CERT_PATH = CERTS_DIR / "vpn.gw6re98mzl.sqxs123.com.pem"
CDN_KEY_PATH = CERTS_DIR / "vpn.gw6re98mzl.sqxs123.com.key"

log = get_logger("test-deploy-aliyun-cdn")


def build_renewed_cert_from_files(
    domain: str,
    cert_path: Path,
    key_path: Path,
) -> RenewedCert:
    """使用本地证书文件构造一个仅用于 CDN 部署测试的 RenewedCert。"""
    if not cert_path.exists():
        raise FileNotFoundError(f"证书文件不存在: {cert_path}")
    if not key_path.exists():
        raise FileNotFoundError(f"私钥文件不存在: {key_path}")

    cert_pem = cert_path.read_text("utf-8")
    key_pem = key_path.read_text("utf-8")

    now = datetime.utcnow()
    # issued_at / expires_at / issuer 仅用于日志展示，占位即可
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
        # 如果证书里还有其他 CDN 域名或通配符，按需要补充到这里
        sans=["*.vpn.gw6re98mzl.sqxs123.com"],
        issuer="manual-test-cdn",
        profile=None,
    )


async def main() -> None:
    # 1. 测试的 CDN 域名（要和阿里 CDN 上的加速域名一致）
    domain = "vpn.gw6re98mzl.sqxs123.com"

    # 2. 如需只限制在某个 provider 上测试（比如某个阿里云账号），
    #    在这里填 config.yaml 中的 provider.name；保持为 None 则使用所有已启用的云 provider。
    provider_name = None  # 比如改成 "奇裕"

    conf = load_raw_config()
    deploy_mode = conf.deploy.mode.strip().lower()
    log.info("当前 deploy.mode=%s", deploy_mode)

    renewed_cert = build_renewed_cert_from_files(
        domain=domain,
        cert_path=CDN_CERT_PATH,
        key_path=CDN_KEY_PATH,
    )

    service = DeployService()

    if deploy_mode == "apply":
        log.info("以 APPLY 模式执行部署（会真实调用云厂商 API）")
        result = await service.apply_deploy(
            renewed_cert=renewed_cert,
            provider_name=provider_name,
        )
    else:
        log.info("以 DRY-RUN 模式执行，只看匹配结果，不真正部署")
        result = await service.plan_dry_run(
            renewed_cert=renewed_cert,
            provider_name=provider_name,
        )

    from pprint import pprint

    pprint(result)

    if deploy_mode == "apply":
        failed = int(result.get("failed_count", 0))
        success = int(result.get("success_count", 0))
        if failed > 0:
            raise RuntimeError(
                f"部署完成，但存在 {failed} 个失败目标（成功 {success} 个），详情见上方 result"
            )
        if success == 0:
            raise RuntimeError(
                "没有任何部署成功记录，请检查是否命中 CDN 目标或 provider 配置是否正确"
            )


if __name__ == "__main__":
    asyncio.run(main())

