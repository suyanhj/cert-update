## Why

腾讯云 CDN 已经能把续签证书关联到加速域名，但 COS 存储桶自定义源站域名没有进入绑定扫描，`TencentDeployer` 也不认识 `oss` 目标。控制台里存储桶证书因此不会被这次部署更新。`config.example.yaml` 已把 `product_scan.tencent.oss_scan` 打开，但 `get_oss_bindings()` 仍返回空列表。

## What Changes

- `TencentCloudProvider.get_oss_bindings()`：用 SSL `DescribeHostCosInstanceList` 翻页拉取 COS 自定义域名，保留 ENABLED 的项，允许 `CertId` 为空。
- `TencentDeployer`：对 `product_type=oss` 调用 `DeployCertificateInstance`，`ResourceType=cos`，`InstanceIdList=["Region|Bucket|Domain"]`，公共参数 Region 用存储桶地域。
- 打开并锁定 `product_scan.tencent.oss_scan=true`；文档与单测同步。

## Capabilities

### New Capabilities

- `tencent-cos-cert`: 腾讯云 COS 自定义域名证书扫描与关联下发。

### Modified Capabilities

- 不改 `domain-matching`。

## Impact

- `app/providers/tencent.py`、`app/deploys/tencent.py`、`tests/test_tencent_cloud.py`
- `docs/providers.md`、`docs/deploys.md`、`config.example.yaml`（oss_scan 已为 true，测试对齐）
- 不新增 COS 对象存储 SDK，复用已有 `tencentcloud-sdk-python-ssl`
- 不影响 CDN 双路径下发
