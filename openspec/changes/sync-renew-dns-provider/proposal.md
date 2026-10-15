# Proposal

## Why

DNS 从阿里云迁移到 Cloudflare 后，现有续签仍由 acme.sh 保存的 Le_Webroot 选择旧插件。仅追加 --dns 不能覆盖 renew 的已保存参数。

## What Changes

- 续签前读取现有证书信息，比较当前 DNS Provider 插件与保存的验证方式。
- 插件一致时继续 --renew；不一致时通过 --issue --dns 当前插件 --force 重新签发。
- 重新签发保留完整 SAN、密钥类型、CA、安装路径和已有 hooks，不直接编辑域名配置。
- 未支持的插件或原证书信息不完整时提前报错，增加插件选择日志和迁移测试。

## Capabilities

### New Capabilities

- `renew-dns-provider`: 续签时跟随当前 DNS Provider 并处理插件迁移。

### Modified Capabilities

无。

## Impact

acme.sh 续签客户端、命令构造测试和操作文档；复用当前 Provider 映射和凭据。
