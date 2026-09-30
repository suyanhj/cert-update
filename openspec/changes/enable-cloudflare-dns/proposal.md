## Why

部分域名已迁移到 Cloudflare 免费 DNS，但当前系统无法发现这些 Zone、DNS 主机记录或把域名映射到 Cloudflare Provider，后续在线签发也就无法向 Cloudflare 自动添加 ACME TXT 记录。需要先完成只面向 DNS 的最小接入。

## What Changes

- 新增 Cloudflare Provider，使用官方 Python SDK 和 API Token 获取所有可见 Zone。
- 按 Zone 分页获取 DNS 记录，并将启用的 A、AAAA、CNAME 记录转换为现有域名发现页面可用的子域数据。
- 将 Cloudflare Zone 纳入统一域名扫描、Provider 归属映射及 `product_scan.cloudflare.domain_scan` 开关。
- 为 acme.sh 增加 `dns_cf` 所需的 `CF_Token` 和可选 `CF_Account_ID` 环境变量映射，为后续签发任务提供 DNS 写入能力。
- 增加 Cloudflare 配置示例、最小权限说明、文档和单元测试。
- 不接入 Cloudflare CDN、SSL、WAF、负载均衡、存储或证书部署产品。

## Capabilities

### New Capabilities

- `cloudflare-dns-provider`: 定义 Cloudflare Zone/DNS 发现、Provider 归属及 acme.sh DNS 凭证映射行为。

### Modified Capabilities


## Impact

- 新增官方 `cloudflare` Python SDK 依赖和 Cloudflare Provider 模块。
- 修改 Provider 类型、工厂、域名服务注册和产品扫描配置。
- 修改 acme.sh Provider 凭证环境变量构建逻辑。
- 更新 `config.example.yaml`、Provider/服务文档和相关测试。
- Cloudflare Token 应限定目标 Zone，并至少具备 Zone Read 与 DNS Edit 权限。
