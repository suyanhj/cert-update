## Purpose

确保域名 DNS 托管厂商变化后，证书续签采用当前配置或扫描确认的 DNS 账号及插件，通过官方强制重新签发完成验证方式迁移，同时保留证书原有域名范围及相关配置。

## ADDED Requirements

### Requirement: 按当前 DNS Provider 选择续签方式
系统 SHALL 在续签前比较原验证插件与当前 DNS Provider；一致时使用 renew，不一致时使用 issue、当前 DNS 插件及 force。凭据 MUST 属于选择的同一个 Provider。

#### Scenario: 阿里云迁移至 Cloudflare
- **WHEN** 原证书保存 dns_ali，当前识别的 Provider 是 Cloudflare
- **THEN** 使用 dns_cf 和当前账号凭据强制重新签发，之后读取新证书

#### Scenario: DNS Provider 不变
- **WHEN** 原验证插件与当前 Provider 相同
- **THEN** 使用现有 renew 语义，不额外强制重新签发

### Requirement: 迁移保留原证书配置
重新签发 SHALL 保留原证书完整域名列表、密钥类型、CA、安装路径及已有 hooks；信息不完整或各 SAN 当前 DNS 账号不一致时 MUST 提前失败，不能静默删减域名或回退旧插件。

#### Scenario: 多 SAN 的 ECC 证书
- **WHEN** ECC 证书迁移插件且包含根域、通配及其他 SAN
- **THEN** 强制签发包含全部原域名、原 CA、ECC 类型和配置参数
