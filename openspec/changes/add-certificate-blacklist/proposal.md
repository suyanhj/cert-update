# Proposal

## Why

证书签发、手动续签和自动续签缺少按域名跳过的入口控制，无法阻止指定证书进入 ACME 和后续匹配部署流程。

## What Changes

- 增加顶层 blacklist 配置和共享域名检查：普通域名精确匹配，*. 条目匹配直属一级子域，在证书操作入口命中时返回 skipped。
- 强制续签同样遵守黑名单；跳过时记录原因，UI 显示跳过提示。
- 补充配置说明及入口回归测试。

## Capabilities

### New Capabilities

- certificate-blacklist: 证书操作入口按域名黑名单跳过。

### Modified Capabilities

无。

## Impact

影响配置模型、renew_flow 公共入口、自动续签结果日志、UI 结果提示及相关测试；复用现有域名归一化和配置热更新机制。
