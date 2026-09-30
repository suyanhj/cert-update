# CRT 运行时数据结构

本文只描述关键数据结构，不展开业务流程。

## 1. 证书列表（`state.CERT_LIST`）

来源：`Certificates.get_certificate_list()` + `tls_probe.probe_domains_batch()`。

```json
[
  {
    "domain": "api.example.com",
    "key": "RSA-2048",
    "sans": ["*.example.com", "api.example.com"],
    "ca": "ZeroSSL",
    "exp": "2026-05-01 07:59:59",
    "days": 90,
    "provider": "aliyun"
  }
]
```

字段说明：

- `domain`：本次探测的域名（不是证书 CN）。
- `key`：证书公钥长度。
- `sans`：SAN 列表。
- `ca`：签发机构。
- `exp`：证书到期时间（Asia/Shanghai 字符串）。
- `days`：剩余天数。
- `provider`：DomainProviderStore 解析得到的 provider 名称。

## 2. 域名分组（`state.DOMAIN_GROUPS`）

来源：`DomainDiscoveryService._normalize_domain_group()` 与按根域名执行的注册/DNS 融合。

```json
[
  {
    "domain": "sqxs123.com",
    "registrar_name": "阿里注册账号",
    "registrar_provider": "aliyun",
    "registrant_org": "示例公司",
    "dns_name": "cf-main",
    "dns_provider": "cloudflare",
    "name": "cf-main",
    "provider": "cloudflare",
    "expires_at": "2026-04-20 07:59:59",
    "days": 120,
    "subs": [
      {
        "name": "api.sqxs123.com",
        "status": "在工作",
        "remark": ""
      }
    ]
  }
]
```

字段说明：

- `domain`：根域名。
- `registrar_name` / `registrar_provider`：域名注册账号与注册平台。
- `registrant_org`：注册主体；API 未提供时为空。
- `dns_name` / `dns_provider`：DNS 托管账号与平台，证书签发使用该账号。
- `assigned_nameservers`：DNS Provider 返回的分配 NS（当前 Cloudflare 提供），用于与公网权威 NS 精确比较。
- `dns_authoritative`：Provider 的强证据标记；当前 Cloudflare `active + full` Zone 为 `true`。
- `name` / `provider`：兼容字段，语义固定等同于 DNS Provider。
- `expires_at`：注册商返回的域名到期时间；未知时为 `unknown`。
- `days`：到期剩余天数，未知时为 0。
- `subs`：由 DNS Provider 返回的子域列表。

同一个根域名只产生一条记录。例如域名在阿里云注册、DNS 托管到 Cloudflare 时，到期时间和注册主体来自阿里云，`subs` 和签发 Provider 来自 Cloudflare。若阿里云仍残留旧 Zone，融合层会用公网权威 NS 识别实际托管方；证据不足时 `dns_name` 为空且不会产生签发映射。

## 3. ECS 列表（`state.ECS_LIST`）

来源：`Ecs.discovery_all()`。

```json
[
  {
    "id": "i-abc123",
    "name": "星闪",
    "provider_name": "星闪",
    "provider": "aliyun",
    "days": 30
  }
]
```

字段说明：

- `name` / `provider_name`：provider 名称。
- `provider`：provider 类型。
- 其余字段由各 provider 的 `get_ecs_instances()` 产出并透传。

## 4. 最新采集时间（`state.LAST_UPDATED`）

来源：`collector.collect_all()` 中 `TimeUtil.now()`，随后写入快照时会被 `str()`。

- 运行中可能是带时区的 `datetime`。
- 写入/恢复快照后为字符串（示例：`2026-03-16 10:00:00+08:00`）。

## 5. 产品绑定缓存（`data/product_bindings_cache.json`）

```json
{
  "aliyun:星闪": {
    "updated_at": "2026-02-28T12:00:00+00:00",
    "products": {
      "cdn": {
        "cdn-1": [
          { "domain": "aa.com" },
          { "domain": "www.aa.com" }
        ]
      },
      "slb": {
        "lb-1": [
          { "domain": "api.aa.com" }
        ]
      }
    }
  }
}
```

字段约定：

- 顶层 key：`provider_type:provider_name`（如 `aliyun:星闪`）。
- `products`：`product_type -> product_id -> 绑定项列表`。
- 绑定项仅包含 `domain`（缓存会剔除 `listener_port`、`metadata` 等历史字段）。

## 6. 采集快照（`data/collector_snapshot.json`）

```json
{
  "domain_groups": [],
  "cert_list": [],
  "ecs_list": [],
  "last_updated": "2026-03-16 10:00:00+08:00"
}
```

用途：应用启动时预热首页，避免冷启动空白。

## 7. 域名与 provider 映射（`data/domain_provider_map.json`）

来源：`DomainProviderStore.save()`。

```json
{
  "example.com": "星闪",
  "api.example.com": "星闪"
}
```

说明：仅保存 domain → provider 名称，用于证书采集阶段的 provider 解析。

## 8. 告警状态（`data/alert_*.json`）

来源：`AlertStateStore`。

```json
{
  "example.com": {
    "state": "FIRING",
    "last_change": "2026-03-16T10:00:00"
  }
}
```

文件：

- `data/alert_crt_state.json`（证书告警）
- `data/alert_domain_state.json`（域名到期告警）
- `data/alert_ecs_state.json`（ECS 到期告警）

## 9. 续签状态（`data/renew_state.json`）

来源：`RenewStateStore`，key 为域名小写。

```json
{
  "example.com": {
    "last_status": "success",
    "last_success_at": "2026-03-16T10:00:00",
    "last_fail_at": "",
    "consecutive_failures": 0,
    "total_failures": 2,
    "last_message": "ok",
    "last_provider": "aliyun"
  }
}
```
