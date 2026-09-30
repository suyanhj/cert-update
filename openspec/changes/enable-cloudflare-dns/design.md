## Context

系统通过统一 Provider 接口发现根域名和 DNS 主机记录，并把域名到 Provider 的关系写入 `DomainProviderStore`，供 acme.sh 续签/后续签发解析 DNS 凭证。Cloudflare 当前没有 Provider，因此迁移到 Cloudflare 免费 DNS 的域名会从扫描结果和 Provider 映射中消失。

Cloudflare 官方 API 使用 Bearer API Token：`GET /zones` 需要 Zone Read，`GET /zones/{zone_id}/dns_records` 需要 DNS Read 或 DNS Write。acme.sh 的 `dns_cf` 支持 `CF_Token`，并可配合 `CF_Account_ID` 限定账号。后续自动签发需要创建和删除 TXT，因此本项目文档统一建议 Zone Read + DNS Edit 的最小权限组合。

## Goals / Non-Goals

**Goals:**

- 使用 Cloudflare 官方 Python SDK 获取 Token 可见的全部 Zone 和 DNS 记录。
- 将 Cloudflare 域名纳入现有扫描、UI 和 Provider 归属映射。
- 为 acme.sh 输出 `CF_Token` 与可选 `CF_Account_ID`。
- 支持 SDK 自动分页并对单个 Zone 的 DNS 查询失败给出明确失败结果。
- 保持依赖和客户端在 Provider 初始化时加载，缺包或缺 Token 立即失败。

**Non-Goals:**

- 不管理普通 DNS 记录；本变更只读取，ACME TXT 写入由后续签发任务调用 acme.sh 完成。
- 不扫描或部署 Cloudflare CDN、边缘证书、WAF、负载均衡、R2 等产品。
- 不接入 Global API Key + Email 旧认证方式。
- 不查询 Cloudflare Registrar 到期时间，Zone 到期时间保持 `unknown`。

## Decisions

1. 使用官方 `cloudflare` SDK 5.x，并约束主版本 `<6`。相比自行拼接 HTTP，官方 SDK复用认证、响应模型和分页迭代，且符合项目优先复用第三方库的约定。
2. `CloudflareDNSProvider` 初始化时强制读取 `credentials.api_token` 并创建客户端；`credentials.account_id` 可选，用于 Zone 列表过滤和 acme.sh `CF_Account_ID`。
3. Zone 列表通过 SDK 的分页对象完整迭代，只保留名称有效且状态为 `active` 的 Zone。每个 Zone 输出 `domain/zone_id/status/type/account_id/expires_at=unknown`。
4. DNS 记录完整分页读取并归一化为 `record_id/subdomain/full_domain/type/value/ttl/proxied/status/remark`。域名页面只对 A、AAAA、CNAME 记录做现有 HTTP 状态探测，其他记录仍可由 `get_dns_records()` 调用获取。
5. Cloudflare 仅加入域名扫描；所有产品绑定方法沿用基类空实现。`product_scan.cloudflare` 默认关闭 CDN/直播/存储/LB/ECS，只开启 `domain_scan`。
6. acme.sh 内置凭证映射使用 `CF_Token=api_token`，仅在配置存在时增加 `CF_Account_ID=account_id`，不设置单一 `CF_Zone_ID`，避免一个 Provider 管理多个 Zone 时绑定错误。

## Risks / Trade-offs

- [Zone API 不提供域名注册到期时间] → 明确标记 `unknown`，不伪造日期。
- [Token 只有 DNS Read，扫描成功但未来签发失败] → 文档要求 Zone Read + DNS Edit，并在签发任务中再做失败提示。
- [单账号 Zone 数量较多] → 依赖 SDK 分页迭代，不使用固定第一页结果。
- [DNS 记录数量较多导致探测耗时] → 仅探测 A、AAAA、CNAME，并复用现有并发上限。
- [SDK 主版本升级破坏接口] → requirements 限制 `<6`，升级时单独验证。

## Migration Plan

1. 安装新增 Cloudflare SDK 依赖。
2. 创建限定目标 Zone 的 API Token，权限为 Zone Read + DNS Edit。
3. 在 `providers` 增加 `type: cloudflare`、`api_token` 和可选 `account_id`。
4. 启用 `product_scan.cloudflare.domain_scan` 后执行采集，确认 Zone 和 DNS 主机出现在域名页面。
5. 回滚时禁用或删除 Cloudflare Provider；不会修改 Cloudflare 侧任何 DNS 数据。

## Open Questions

无。首次签发按钮与 `--issue` 命令属于后续第二个变更。
