#!/usr/bin/env python3
"""腾讯云 DescribeHostCosInstanceList 探测脚本（对齐 tccli 参数）。

用法:
    cd script/py/crt
    python tools/describe_host_cos_demo.py --config config.prod-yy.yaml

    # 与 tccli configure 相同凭证时，也可直接传 AK
    python tools/describe_host_cos_demo.py --secret-id AKIDxxx --secret-key xxx

验证通过标准:
    case=tccli_min_is_cache_0 的 total_count > 0
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from tencentcloud.ssl.v20191205 import models as ssl_models

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.config import load_raw_config, set_config_path
from app.utils.tencent_sdk import SslClient, build_tencent_client


@dataclass
class CaseResult:
    name: str
    total_count: int
    instance_count: int
    request_id: str
    instances: List[Dict[str, Any]]
    request_body: Dict[str, Any]
    error: Optional[str] = None


def _instance_to_dict(item: Any) -> Dict[str, Any]:
    return {
        "Domain": getattr(item, "Domain", None),
        "CertId": getattr(item, "CertId", None),
        "Status": getattr(item, "Status", None),
        "Bucket": getattr(item, "Bucket", None),
        "Region": getattr(item, "Region", None),
    }


def _request_body(req: ssl_models.DescribeHostCosInstanceListRequest) -> Dict[str, Any]:
    body: Dict[str, Any] = {}
    for key in (
        "CertificateId",
        "IsCache",
        "ResourceType",
        "OldCertificateId",
        "Offset",
        "Limit",
        "AsyncCache",
    ):
        value = getattr(req, key, None)
        if value is not None:
            body[key] = value
    filters = getattr(req, "Filters", None)
    if filters:
        body["Filters"] = [
            {"FilterKey": item.FilterKey, "FilterValue": item.FilterValue}
            for item in filters
        ]
    return body


def _filter_domain_match(value: str) -> ssl_models.Filter:
    item = ssl_models.Filter()
    item.FilterKey = "domainMatch"
    item.FilterValue = value
    return item


def _call_cos_list(
    client: Any,
    *,
    label: str,
    build_request: Callable[[], ssl_models.DescribeHostCosInstanceListRequest],
) -> CaseResult:
    req = build_request()
    request_body = _request_body(req)
    try:
        resp = client.DescribeHostCosInstanceList(req)
        instances = [_instance_to_dict(item) for item in (resp.InstanceList or [])]
        return CaseResult(
            name=label,
            total_count=int(resp.TotalCount or 0),
            instance_count=len(instances),
            request_id=str(resp.RequestId or ""),
            instances=instances,
            request_body=request_body,
        )
    except Exception as exc:
        return CaseResult(
            name=label,
            total_count=0,
            instance_count=0,
            request_id="",
            instances=[],
            request_body=request_body,
            error=str(exc),
        )


def _build_cases(
    new_cert_id: str,
    old_cert_id: str,
) -> List[tuple[str, Callable[[], ssl_models.DescribeHostCosInstanceListRequest]]]:
    def _base() -> ssl_models.DescribeHostCosInstanceListRequest:
        return ssl_models.DescribeHostCosInstanceListRequest()

    cases: List[tuple[str, Callable[[], ssl_models.DescribeHostCosInstanceListRequest]]] = []

    def add(name: str, builder: Callable[[], ssl_models.DescribeHostCosInstanceListRequest]) -> None:
        cases.append((name, builder))

    # 用户在 VM 上验证成功的 tccli 命令
    add("tccli_min_is_cache_0", lambda: (
        (lambda r: (
            setattr(r, "CertificateId", new_cert_id),
            setattr(r, "IsCache", 0),
            r,
        )[-1])(_base())
    ))
    add("tccli_min_is_cache_1", lambda: (
        (lambda r: (
            setattr(r, "CertificateId", new_cert_id),
            setattr(r, "IsCache", 1),
            r,
        )[-1])(_base())
    ))

    if old_cert_id:
        add("tccli_with_old_cert", lambda: (
            (lambda r: (
                setattr(r, "CertificateId", new_cert_id),
                setattr(r, "IsCache", 0),
                setattr(r, "ResourceType", "cos"),
                setattr(r, "OldCertificateId", old_cert_id),
                setattr(r, "Limit", 10),
                setattr(r, "AsyncCache", 0),
                r,
            )[-1])(_base())
        ))

    # 我们当前代码实际发送的参数（对照用）
    add("current_code_style", lambda: (
        (lambda r: (
            setattr(r, "CertificateId", new_cert_id),
            setattr(r, "ResourceType", "cos"),
            setattr(r, "IsCache", 0),
            setattr(r, "Limit", 100),
            setattr(r, "Filters", [_filter_domain_match("1")]),
            r,
        )[-1])(_base())
    ))

    return cases


def _load_tencent_credentials(
    config_path: Optional[Path],
    provider_name: str,
    secret_id: str,
    secret_key: str,
    region: str,
) -> Dict[str, str]:
    if secret_id and secret_key:
        return {
            "secret_id": secret_id,
            "secret_key": secret_key,
            "region": region or "ap-guangzhou",
        }

    if config_path is None:
        raise ValueError("请提供 --config 或 --secret-id/--secret-key")

    set_config_path(config_path)
    raw = load_raw_config()
    for item in raw.get("providers") or []:
        if str(item.get("name", "")) != provider_name:
            continue
        if str(item.get("type", "")).lower() != "tencent":
            continue
        cred = item.get("credentials") or {}
        loaded_secret_id = str(cred.get("secret_id", "") or "").strip()
        loaded_secret_key = str(cred.get("secret_key", "") or "").strip()
        loaded_region = str(cred.get("region", "ap-guangzhou") or "ap-guangzhou").strip()
        if not loaded_secret_id or not loaded_secret_key:
            raise ValueError(f"provider {provider_name} 缺少 secret_id/secret_key")
        return {
            "secret_id": loaded_secret_id,
            "secret_key": loaded_secret_key,
            "region": loaded_region,
        }
    raise ValueError(f"未找到腾讯 provider: {provider_name}")


def main() -> int:
    parser = argparse.ArgumentParser(description="探测 DescribeHostCosInstanceList（对齐 tccli）")
    parser.add_argument("--config", default="config.prod-yy.yaml", help="配置文件路径")
    parser.add_argument("--provider", default="游逸思", help="腾讯 provider 名称")
    parser.add_argument("--secret-id", default="", help="可选，直接指定 SecretId（与 tccli 对齐时用）")
    parser.add_argument("--secret-key", default="", help="可选，直接指定 SecretKey")
    parser.add_argument("--region", default="", help="SSL 客户端 Region，tccli 会传 configure 里的 region")
    parser.add_argument("--new-cert", default="aSKhKn8K", help="待部署证书 ID")
    parser.add_argument("--old-cert", default="ZHHeV62u", help="旧证书 ID（对照用）")
    args = parser.parse_args()

    config_path: Optional[Path] = None
    if args.config:
        config_path = Path(args.config)
        if not config_path.is_absolute():
            config_path = (_ROOT / config_path).resolve()

    cred = _load_tencent_credentials(
        config_path,
        args.provider,
        str(args.secret_id or "").strip(),
        str(args.secret_key or "").strip(),
        str(args.region or "").strip(),
    )
    masked_id = cred["secret_id"][:6] + "..." + cred["secret_id"][-4:] if cred["secret_id"] else ""
    print(json.dumps({
        "credential_source": "cli" if args.secret_id else "config",
        "secret_id": masked_id,
        "ssl_client_region": cred["region"],
    }, ensure_ascii=False))

    # tccli 源码: SslClient(cred, g_param[Region], profile)，会带 X-TC-Region
    ssl_client = build_tencent_client(
        secret_id=cred["secret_id"],
        secret_key=cred["secret_key"],
        client_cls=SslClient,
        region=cred["region"],
    )

    # 对照：我们业务代码当前用的空 region 客户端
    ssl_client_no_region = build_tencent_client(
        secret_id=cred["secret_id"],
        secret_key=cred["secret_key"],
        client_cls=SslClient,
        region="",
    )

    print("\n=== DescribeHostCosInstanceList（region=配置值，对齐 tccli） ===")
    cases = _build_cases(args.new_cert, args.old_cert)
    passed = False
    for name, builder in cases:
        result = _call_cos_list(ssl_client, label=name, build_request=builder)
        payload = {
            "client_region": cred["region"],
            "case": result.name,
            "request": result.request_body,
            "total_count": result.total_count,
            "instance_count": result.instance_count,
            "request_id": result.request_id,
            "error": result.error,
            "instances": result.instances,
        }
        print(json.dumps(payload, ensure_ascii=False))
        if result.name == "tccli_min_is_cache_0" and result.total_count > 0:
            passed = True

    print("\n=== 对照：region=''（我们业务代码当前 SSL 客户端） ===")
    result = _call_cos_list(
        ssl_client_no_region,
        label="tccli_min_is_cache_0_no_region",
        build_request=lambda: (
            (lambda r: (
                setattr(r, "CertificateId", args.new_cert),
                setattr(r, "IsCache", 0),
                r,
            )[-1])(ssl_models.DescribeHostCosInstanceListRequest())
        ),
    )
    print(json.dumps({
        "client_region": "",
        "case": result.name,
        "total_count": result.total_count,
        "instances": result.instances,
        "request_id": result.request_id,
    }, ensure_ascii=False))

    print("\n=== 结论 ===")
    if passed:
        print(json.dumps({"ok": True, "message": "tccli_min_is_cache_0 已拿到数据，可以改业务代码"}, ensure_ascii=False))
        return 0

    print(json.dumps({
        "ok": False,
        "message": "tccli_min_is_cache_0 仍为空；请确认 demo 使用的 AK 与 VM 上 tccli configure 一致",
        "hint": "python tools/describe_host_cos_demo.py --secret-id <tccli的id> --secret-key <tccli的key>",
    }, ensure_ascii=False))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
