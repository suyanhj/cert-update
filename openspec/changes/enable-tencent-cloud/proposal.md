## Why

腾讯云 Provider/Deployer 骨架已在仓库中，但 `product_scan.tencent` 全部关闭，运行配置没有腾讯云账号，部署器未实现 `upload_certificate()` 且 `deploy()` 会忽略传入的 `cert_id` 重复上传。现在可以接入腾讯云账号，需要把域名发现、CDN/CLB 绑定扫描和证书下发打通到与阿里云/华为云同等可用。

## What Changes

- 打开 `product_scan.tencent` 的 `domain_scan` / `cdn_scan` / `lb_scan`（`oss_scan`、`ecs_scan` 保持关闭）。
- 补齐 `TencentCloudProvider`：DNSPod 域名+解析探测、CDN 仅保留在线且 HTTPS 已开且有证书 ID 的绑定、CLB 仅保留带域名的 HTTPS 监听规则。
- 补齐 `TencentDeployer`：实现 `upload_certificate()` 并复用 `cert_id`；CDN/CLB 走 SSL `DeployCertificateInstance`。
- 在 `config.yaml` / `config.prod.yaml` 增加腾讯云 provider；`config.example.yaml` 补充凭证示例。
- 补充单测与 `docs/providers.md` / `docs/deploys.md`。

## Capabilities

### New Capabilities

- `tencent-cloud-cert`: 腾讯云账号的 DNSPod 域名发现、CDN/CLB HTTPS 绑定扫描，以及证书上传后部署到 CDN/CLB。

### Modified Capabilities

无。`domain-matching` 匹配语义不变。

## Impact

- 代码：`app/providers/tencent.py`、`app/deploys/tencent.py`、`app/utils/tencent_sdk.py`（若需导出 SSL 部署相关客户端）。
- 配置：`config.example.yaml`、`config.yaml`、`config.prod.yaml`。
- 文档：`docs/providers.md`、`docs/deploys.md`。
- 测试：新增腾讯云 Provider/Deployer 单测。
- 依赖：继续使用已有 `tencentcloud-sdk-python-dnspod/cdn/clb/ssl`，不新增 SDK 包。
- 不实现 COS、CVM、EdgeOne。
