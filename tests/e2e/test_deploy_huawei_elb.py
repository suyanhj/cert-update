import asyncio
from datetime import datetime, timedelta
from pathlib import Path

from app.schemas.acme import RenewedCert
from app.services.deploy import DeployService
from app.config import load_raw_config
from app.utils.logger import get_logger


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CERTS_DIR = PROJECT_ROOT / "certs"

# 根据你实际的华为云 ELB 证书文件名调整下面两行
ELB_CERT_PATH = CERTS_DIR / "vpn.gw6re98mzl.sqxs123.com.pem"
ELB_KEY_PATH = CERTS_DIR / "vpn.gw6re98mzl.sqxs123.com.key"

log = get_logger("test-deploy-huawei-elb")


def build_renewed_cert_from_files(
    domain: str,
    cert_path: Path,
    key_path: Path,
) -> RenewedCert:
    """使用本地证书文件构造一个仅用于华为云 ELB 部署测试的 RenewedCert。"""
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
        # 如证书中有其他域名或通配符，可按需补充
        sans=["*.vpn.gw6re98mzl.sqxs123.com"],
        issuer="manual-test-huawei-elb",
        profile=None,
    )


async def main() -> None:
    # 1. 测试的 ELB 相关域名（要能命中你在华为云 ELB 上绑定的域名）
    #    例如 ELB 对外暴露的 HTTPS 域名
    domain = "vpn.gw6re98mzl.sqxs123.com"

    # 2. provider_name 建议直接指定到华为云这家，例如你 config.yaml 里的名称是“准游hw”
    # provider_name = "准游hw"
    provider_name = None

    conf = load_raw_config()
    deploy_mode = conf.deploy.mode.strip().lower()
    log.info("当前 deploy.mode=%s", deploy_mode)

    renewed_cert = build_renewed_cert_from_files(
        domain=domain,
        cert_path=ELB_CERT_PATH,
        key_path=ELB_KEY_PATH,
    )

    service = DeployService()

    if deploy_mode == "apply":
        log.info("以 APPLY 模式执行华为云 ELB 部署（会真实调用华为云 API）")
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
                f"华为云 ELB 部署完成，但存在 {failed} 个失败目标（成功 {success} 个），详情见上方 result"
            )
        if success == 0:
            raise RuntimeError(
                "没有任何华为云 ELB 部署成功记录，请检查是否命中 ELB 绑定或 provider 配置是否正确"
            )


if __name__ == "__main__":
    asyncio.run(main())

