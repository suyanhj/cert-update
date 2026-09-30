## Why

腾讯云控制台是在 SSL 证书页扫描并覆盖到 CDN、COS、CLB 等产品。当前 CDN 扫描/下发仍走 CDN 产品接口改域名配置，和 CLB/COS 的 SSL 路径不一致。我们只用 CDN、COS、CLB，不需要 Live/VOD/WAF 等。

## What Changes

- CDN/CLB/COS 扫描统一走 SSL `DescribeHost*InstanceList`，只覆盖这三类。
- 新扫描 CDN 下发走 `DeployCertificateInstance`；旧缓存默认仍走 `ModifyDomainConfig`。
- 对外绑定字段保持兼容：`product_type`/`product_id`/`metadata.https_billing`/`listener_id` 不变；新扫描写入 `cdn_deploy_mode=ssl`。
- 不改 DNSPod 域名发现、product_scan 开关、阿里云/华为云。

## Capabilities

### New Capabilities

- `tencent-ssl-host-cert`: 腾讯云 SSL 主机扫描与 CDN/CLB/COS 证书下发对齐。

### Modified Capabilities

- 不改 `domain-matching`。

## Impact

- `app/providers/tencent.py`、`app/deploys/tencent.py`、`tests/test_tencent_cloud.py`
- `docs/providers.md`、`docs/deploys.md`
- 仍保留 CDN/CLB SDK 依赖，扫描与下发运行时只走 SSL
