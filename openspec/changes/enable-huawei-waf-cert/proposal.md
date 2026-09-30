## Why

当前证书管理只会发现并部署华为云 CDN 与 ELB 证书，华为云 WAF 中已接入的 HTTPS 防护域名仍需人工检查和换证，容易遗漏即将过期的证书。

## What Changes

- 增加华为云 WAF 云模式 HTTPS 防护域名及其当前证书绑定扫描。
- 增加独立的 `waf_scan` 产品扫描开关，并将 WAF 绑定纳入现有缓存、证书域名匹配和部署前实时校验流程。
- 使用华为云 WAF SDK 上传新证书，并将其绑定到匹配的云模式防护域名。
- 补充依赖、示例配置、运维文档和自动化测试。

## Capabilities

### New Capabilities

- `huawei-waf-cert`: 扫描华为云 WAF 云模式证书绑定，并将续签后的证书安全部署到匹配域名。

### Modified Capabilities

无。

## Impact

- 影响 `app/providers/huawei.py`、`app/deploys/huawei.py`、华为云 SDK 工具层和产品扫描编排。
- 新增 `huaweicloudsdkwaf` 运行时依赖。
- `product_scan.<cloud>` 新增向后兼容的 `waf_scan` 开关；华为云示例配置默认启用。
- 需要调用方具备 WAF 防护域名与证书的查询、创建和绑定权限。
