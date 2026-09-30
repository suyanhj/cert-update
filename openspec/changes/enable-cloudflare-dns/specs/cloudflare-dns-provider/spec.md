## ADDED Requirements

### Requirement: Cloudflare API Token 配置
系统 SHALL 接受 `type: cloudflare` Provider，并 MUST 要求 `credentials.api_token` 非空；`credentials.account_id` SHALL 为可选字段。客户端依赖或必需凭证缺失时 MUST 在 Provider 初始化阶段失败。

#### Scenario: 使用 API Token 初始化
- **WHEN** Cloudflare Provider 配置有效的 `api_token`
- **THEN** 系统使用 Bearer Token 初始化官方 Cloudflare 客户端

#### Scenario: Token 缺失
- **WHEN** Cloudflare Provider 未配置有效的 `api_token`
- **THEN** Provider 初始化立即失败且错误指出缺少凭证

### Requirement: 分页发现 Cloudflare Zone
系统 SHALL 完整迭代 Cloudflare Zone 列表分页，并 SHALL 只把名称有效且状态为 `active` 的 Zone 作为根域名返回。配置 `account_id` 时 SHALL 用其过滤 Zone。

#### Scenario: 多页 Zone 列表
- **WHEN** Token 可见的 Zone 跨越多个 API 页面
- **THEN** 系统返回所有页面中的 active Zone，且每个结果包含 `domain` 和 `zone_id`

#### Scenario: 非活动 Zone
- **WHEN** Zone 状态为 pending、moved 或 initializing
- **THEN** 系统不把该 Zone 纳入可签发域名发现结果

### Requirement: 分页获取 DNS 记录
系统 SHALL 完整迭代指定 Zone 的 DNS 记录，并归一化记录 ID、相对主机名、完整域名、类型、值、TTL、代理状态和备注。`get_dns_records()` MUST 保留非主机类记录供调用方读取。

#### Scenario: 根记录和子域记录
- **WHEN** Cloudflare 返回 Zone apex 与子域 DNS 记录
- **THEN** apex 的 `subdomain` 为 `@`，子域记录包含相对名称和完整域名

#### Scenario: 域名页面子域发现
- **WHEN** DNS 记录类型为 A、AAAA 或 CNAME
- **THEN** 系统将其送入现有域名状态探测并作为 Zone 的 `subs` 返回

#### Scenario: TXT 或 MX 记录
- **WHEN** DNS 记录不是 A、AAAA、CNAME
- **THEN** `get_dns_records()` 仍返回该记录，但域名页面不对其执行 HTTP 探测

### Requirement: 统一域名扫描注册
系统 SHALL 在配置模型、Provider 工厂和域名服务中注册 `cloudflare`，并 SHALL 通过 `product_scan.cloudflare.domain_scan` 控制发现。Cloudflare 的其他产品扫描 SHALL 默认关闭且返回空绑定。

#### Scenario: 启用 Cloudflare 域名扫描
- **WHEN** Provider 已启用且 `product_scan.cloudflare.domain_scan=true`
- **THEN** Cloudflare Zone 进入统一域名结果并写入 Provider 归属映射

#### Scenario: 关闭 Cloudflare 域名扫描
- **WHEN** `product_scan.cloudflare.domain_scan=false`
- **THEN** 系统不调用该 Provider 的 Zone API

### Requirement: acme.sh Cloudflare DNS 凭证映射
系统 SHALL 为 Cloudflare Provider 将 `credentials.api_token` 映射到 `CF_Token`，并在 `credentials.account_id` 非空时映射到 `CF_Account_ID`。系统 MUST NOT 输出空的 `CF_Account_ID` 或设置固定 `CF_Zone_ID`。

#### Scenario: Token 与账号 ID 均存在
- **WHEN** acme.sh 为 Cloudflare 域名构建 Provider 环境
- **THEN** 环境包含 `CF_Token` 和 `CF_Account_ID`

#### Scenario: 只配置 Token
- **WHEN** Cloudflare Provider 没有配置 `account_id`
- **THEN** 环境仅新增 `CF_Token`，且 acme.sh 自行按挑战域名查找 Zone

### Requirement: Cloudflare 接入保持只读
系统 SHALL 只通过 Cloudflare Provider 调用 Zone 和 DNS Record 列表接口，MUST NOT 修改 DNS 记录或调用其他 Cloudflare 产品接口。

#### Scenario: 执行域名采集
- **WHEN** 系统刷新 Cloudflare 域名和 DNS 数据
- **THEN** 仅发起读取请求，不产生 Cloudflare 侧状态变更
