"""七牛云 CDN OpenAPI 请求工具。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlparse

import requests
from qiniu import Auth
from qiniu.auth import QiniuMacAuth, QiniuMacRequestsAuth


QINIU_API_HOST = "api.qiniu.com"
QINIU_FUSION_HOST = "fusion.qiniuapi.com"
QINIU_UC_HOST = "uc.qiniuapi.com"
QINIU_DOMAIN_API = f"https://{QINIU_API_HOST}/domain"
QINIU_CERT_API = f"https://{QINIU_FUSION_HOST}/sslcert"
QINIU_BUCKETS_API = f"https://{QINIU_UC_HOST}/buckets"
QINIU_BUCKET_DOMAINS_API = f"https://{QINIU_UC_HOST}/v2/domains"


@dataclass(frozen=True)
class QiniuAuthContext:
    """同时保存七牛 CDN 两套官方鉴权对象。"""

    qbox: Auth
    qiniu: QiniuMacRequestsAuth


class QiniuAPIError(RuntimeError):
    """七牛 API 请求或业务响应失败。"""


def build_qiniu_auth(access_key_id: str, access_key_secret: str) -> QiniuAuthContext:
    """构建 Fusion(QBox) 与 Domain(Qiniu) 所需的官方 SDK 鉴权对象。"""
    if not access_key_id or not access_key_secret:
        raise ValueError("七牛云 access_key_id/access_key_secret 不能为空")
    return QiniuAuthContext(
        qbox=Auth(access_key_id, access_key_secret),
        qiniu=QiniuMacRequestsAuth(QiniuMacAuth(access_key_id, access_key_secret)),
    )


def _compact_json(body: dict[str, Any] | None) -> bytes | None:
    if body is None:
        return None
    return json.dumps(
        body,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _response_payload(response: requests.Response, url: str) -> Any:
    try:
        payload = response.json() if response.content else {}
    except ValueError as exc:
        raise QiniuAPIError(
            f"七牛 API 返回非 JSON 响应: status={response.status_code} url={url}"
        ) from exc

    if not isinstance(payload, (dict, list)):
        raise QiniuAPIError(
            f"七牛 API 返回格式非法: status={response.status_code} url={url}"
        )

    code = payload.get("code") if isinstance(payload, dict) else None
    error = str(payload.get("error") or "").strip() if isinstance(payload, dict) else ""
    try:
        code_is_error = code is not None and int(code) != 200
    except (TypeError, ValueError):
        code_is_error = code is not None
    if not response.ok or error or code_is_error:
        detail = error or str(getattr(response, "reason", "") or "未知错误")
        raise QiniuAPIError(
            f"七牛 API 请求失败: status={response.status_code} code={code} "
            f"error={detail} url={url}"
        )
    return payload


def qiniu_request(
    auth: QiniuAuthContext,
    url: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    timeout: int = 30,
) -> Any:
    """按请求主机选择 Qiniu/QBox 鉴权并发送 JSON 请求。"""
    request_method = str(method or "GET").upper()
    if request_method not in {"GET", "POST", "PUT", "DELETE"}:
        raise ValueError(f"不支持的 HTTP 方法: {method}")

    host = (urlparse(url).hostname or "").lower()
    if host not in {QINIU_API_HOST, QINIU_FUSION_HOST, QINIU_UC_HOST}:
        raise ValueError(f"不支持的七牛 API 主机: {host or '-'}")

    body_bytes = _compact_json(body)
    headers: dict[str, str] = {}
    request_auth = None
    if body_bytes is not None:
        headers["Content-Type"] = "application/json"

    if host in {QINIU_API_HOST, QINIU_UC_HOST}:
        request_auth = auth.qiniu
    else:
        token = auth.qbox.token_of_request(
            url,
            body_bytes.decode("utf-8") if body_bytes is not None else None,
            headers.get("Content-Type"),
        )
        headers["Authorization"] = f"QBox {token}"

    response = requests.request(
        request_method,
        url,
        headers=headers,
        data=body_bytes,
        auth=request_auth,
        timeout=timeout,
    )
    return _response_payload(response, url)


def list_qiniu_domains(
    auth: QiniuAuthContext,
    *,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """分页列出七牛云 CDN/DCDN 域名原始数据。"""
    page_limit = int(limit)
    if not 1 <= page_limit <= 1000:
        raise ValueError("七牛域名分页 limit 必须在 1 到 1000 之间")

    domains: list[dict[str, Any]] = []
    marker = ""
    seen_markers: set[str] = set()
    while True:
        params: dict[str, Any] = {"limit": page_limit}
        if marker:
            params["marker"] = marker
        payload = qiniu_request(
            auth,
            f"{QINIU_DOMAIN_API}?{urlencode(params)}",
        )

        page_domains = payload.get("domains", [])
        if not isinstance(page_domains, list):
            raise QiniuAPIError("七牛域名列表响应中的 domains 不是数组")
        domains.extend(item for item in page_domains if isinstance(item, dict))

        next_marker = str(payload.get("marker") or "").strip()
        if not next_marker:
            return domains
        if next_marker in seen_markers:
            raise QiniuAPIError(f"七牛域名分页 marker 未前进: marker={next_marker}")
        seen_markers.add(next_marker)
        marker = next_marker


def list_qiniu_buckets(auth: QiniuAuthContext) -> list[str]:
    """列出当前七牛账号拥有的 Kodo Bucket。"""
    payload = qiniu_request(auth, QINIU_BUCKETS_API)
    if not isinstance(payload, list):
        raise QiniuAPIError("七牛 Bucket 列表响应不是数组")
    return [str(item).strip() for item in payload if str(item).strip()]


def list_qiniu_bucket_domains(
    auth: QiniuAuthContext,
    bucket: str,
) -> list[str]:
    """列出一个 Kodo Bucket 绑定的域名。"""
    bucket_name = str(bucket or "").strip()
    if not bucket_name:
        raise ValueError("查询七牛 Bucket 域名需要 bucket")
    payload = qiniu_request(
        auth,
        f"{QINIU_BUCKET_DOMAINS_API}?{urlencode({'tbl': bucket_name})}",
    )
    if not isinstance(payload, list):
        raise QiniuAPIError(f"七牛 Bucket 域名响应不是数组: bucket={bucket_name}")
    return [str(item).strip() for item in payload if str(item).strip()]
