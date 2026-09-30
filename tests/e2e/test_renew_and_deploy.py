"""
续签 + 部署完整流程测试脚本（与正常业务流程一致）。

流程：
  1. 续签证书：renew_certificate()，staging+自签时在 acme_sh 内本地模拟，否则走 acme.sh。
  2. 匹配云：用证书 domain+sans 与各 provider 的 product_bindings 匹配（CDN/OSS/SLB/ELB 等）。
  3. 匹配 Nginx：用证书 domain+sans 与 nginx_deploy_rules.cert_domains 匹配，得到要推的主机列表。
  4. 部署：deploy.mode=apply 时，先按 provider 上传一次证书并部署到云目标，再 SSH 推证书到 Nginx 并 reload。

用法：
  python -m script.py.crt.tests.e2e.test_renew_and_deploy
  python -m script.py.crt.tests.e2e.test_renew_and_deploy --domain vpn.gw6re98mzl.sqxs123.com --force
"""
import argparse
import asyncio
from pprint import pprint

from app.config import load_raw_config
from app.services.renew_flow import renew_and_plan_deploy
from app.utils.logger import get_logger


log = get_logger("test-renew-deploy")


async def main() -> None:
    parser = argparse.ArgumentParser(description="续签 + 部署完整流程测试")
    parser.add_argument(
        "--domain",
        default="vpn.gw6re98mzl.sqxs123.com",
        help="续签并部署的域名（默认: vpn.gw6re98mzl.sqxs123.com）",
    )
    parser.add_argument(
        "--provider",
        default=None,
        help="限定云 provider 名称（config 中的 name），不传则匹配所有",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="强制续签，忽略冷却窗口",
    )
    args = parser.parse_args()

    domain = (args.domain or "").strip()
    if not domain:
        raise ValueError("请提供 --domain 或使用默认域名")

    conf = load_raw_config()
    deploy_mode = conf.deploy.mode.strip().lower()

    log.info(
        "deploy.mode=%s domain=%s provider=%s force=%s（staging/自签由 AcmeShRenewer 按 config 处理）",
        deploy_mode,
        domain,
        args.provider,
        args.force,
    )

    result = await renew_and_plan_deploy(
        domain=domain,
        provider_name=args.provider or None,
        force=args.force,
    )

    # 完整 result 输出（dry-run 含 planned_bind_actions、nginx_plan、summary 等）
    pprint(result)

    renew_status = result.get("renew_status")
    if renew_status == "failed":
        raise RuntimeError(
            f"续签失败: {result.get('error', '未知错误')}，未进入部署流程"
        )
    if renew_status == "skipped":
        log.info("续签因冷却窗口被跳过，无部署结果")
        return

    if deploy_mode == "dry-run":
        # dry-run 结果汇总
        summary = result.get("summary") or {}
        total = int(summary.get("total_matches", 0))
        nginx_plan = result.get("nginx_plan") or {}
        nginx_targets = nginx_plan.get("targets") or []
        nginx_count = len(nginx_targets)
        planned = result.get("planned_bind_actions") or []
        log.info(
            "dry-run 完成: 云目标 %s 个（planned_bind_actions=%s），Nginx 目标 %s 个",
            total,
            len(planned),
            nginx_count,
        )
        if total == 0 and nginx_count == 0:
            log.info("未命中任何部署目标，请检查证书域名与 config 中的绑定/nginx_deploy_rules")
        return

    # apply 模式
    failed = int(result.get("failed_count", 0))
    success = int(result.get("success_count", 0))
    if failed > 0:
        raise RuntimeError(
            f"部署完成但存在 {failed} 个失败目标（成功 {success} 个），详情见上方 result"
        )
    log.info("续签+部署流程结束: 成功 %s 个目标", success)


if __name__ == "__main__":
    asyncio.run(main())

