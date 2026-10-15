# Proposal

## Why

手动同步的证书无法通过现有仅部署入口使用，域名也缺少只关闭自动续签而保留手动操作的配置。

## What Changes

- 增加统一的域名证书策略，明确选择 acme/manual 来源、自动续签开关及 DNS 账号引用。
- 手动来源直接加载完整链和私钥，校验后复用现有部署流程。
- 自动任务遵守域名和实际证书主域名策略，黑名单继续拦截所有操作。
- UI 展示来源与自动续签状态，手动来源提供仅部署入口。

## Capabilities

### New Capabilities

- `certificate-policy`: 域名证书来源、自动续签控制和手动证书部署。

### Modified Capabilities

无；复用现有域名匹配语义。

## Impact

配置模型、证书加载与编排、acme DNS 凭据选择、UI、配置文档与测试。未配置策略保持现有行为，无新依赖。
