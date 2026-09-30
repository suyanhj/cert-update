## Why

七牛云 Provider/Deployer 虽已注册，但当前实现仍是未验证的占位代码：证书接口域名和鉴权方式错误、域名接口使用了错误鉴权、证书无法按账号上传一次后复用，实际无法可靠完成 CDN 和对象存储加速域名的证书更新。现在已有明确的官方 Fusion/Kodo API 文档和历史可用逻辑，可将七牛云常用的 CDN、对象存储两类产品补齐到现有多云证书流程。

## What Changes

- 按七牛云当前双域名、双鉴权规范封装 API 请求：域名管理/配置使用 `api.qiniu.com` + Qiniu 鉴权，证书管理使用 `fusion.qiniuapi.com` + QBox 鉴权。
- 补齐 CDN 域名分页发现与绑定元数据，仅产出可用于证书部署的 CDN 域名。
- 扫描 Kodo Bucket 及其绑定域名，把以七牛 Bucket 为源站的 CDN 加速域名作为对象存储目标，并与普通 CDN 目标互斥分类。
- 补齐证书上传一次并复用 `cert_id`，根据目标当前协议选择开启 HTTPS 或更新 HTTPS 证书。
- 修正证书列表、删除接口及部署结果字段，增加清晰的请求失败日志和异常信息。
- 启用示例配置中的七牛云 CDN 与对象存储扫描，并补充配置、实现文档和单元测试。

## Capabilities

### New Capabilities

- `qiniu-cloud-cert`: 七牛云 CDN/Kodo 对象存储域名发现、证书上传复用，以及两类产品 HTTP/HTTPS 加速域名的证书绑定。

### Modified Capabilities

无。`domain-matching` 匹配语义不变。

## Impact

- 代码：`app/utils/qiniu_api.py`、`app/providers/qiniu.py`、`app/deploys/qiniu.py`。
- 配置：`config.example.yaml` 的 `product_scan.qiniu.cdn_scan` 与 `oss_scan`。
- 文档：`docs/providers.md`、`docs/deploys.md`。
- 测试：新增七牛云 API、Provider 与 Deployer 单元测试。
- 依赖：复用已有 `qiniu` 与 `requests`，不新增依赖。
- 外部系统：七牛云 CDN Domain API、Kodo UC Bucket API 与 Fusion SSL Certificate API。
