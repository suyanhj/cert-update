## ADDED Requirements

### Requirement: Provider 明确声明域名角色
系统 SHALL 由 Provider 显式声明其提供注册元数据、DNS 托管元数据或两者；系统 MUST NOT 仅根据返回字段是否存在猜测 Provider 角色。

#### Scenario: Cloudflare 仅提供 DNS 数据
- **WHEN** Cloudflare 返回有效 Zone 与 DNS 记录
- **THEN** 系统将其作为 DNS 托管来源，且不把它当作注册到期信息来源

#### Scenario: 七牛 CDN 域名不参与融合
- **WHEN** 七牛云返回 CDN 加速域名
- **THEN** 系统不把该域名作为根域名注册或 DNS Zone 数据

#### Scenario: 腾讯注册域名已移出 DNSPod
- **WHEN** 腾讯注册 API 返回域名但 DNSPod 不再包含该 Zone
- **THEN** 单条结果只声明注册角色，且后续可与其他平台的 DNS Zone 融合

### Requirement: 按根域名融合注册与 DNS 数据
系统 SHALL 对规范化后相同的根域名生成唯一记录，并 SHALL 分别保留注册商账号、注册平台、DNS 账号和 DNS 平台。

#### Scenario: 阿里云注册且 Cloudflare 托管 DNS
- **WHEN** 阿里云返回 `example.com` 的注册信息且 Cloudflare 返回同域名的 DNS Zone
- **THEN** 融合记录使用阿里云的到期时间与注册主体，并使用 Cloudflare 的 Zone、DNS 记录和 DNS Provider

#### Scenario: 阿里云注册且阿里云托管 DNS
- **WHEN** 阿里云同时返回域名注册信息和 DNS 记录
- **THEN** 融合记录的注册商与 DNS 托管商均为对应阿里云账号

#### Scenario: 腾讯云注册且 Cloudflare 托管 DNS
- **WHEN** 腾讯注册 API 返回域名且 DNSPod 已移除该 Zone，同时 Cloudflare 返回同域名 Zone
- **THEN** 融合记录使用腾讯云注册信息和 Cloudflare DNS 数据

#### Scenario: 腾讯云注册且 DNSPod 托管 DNS
- **WHEN** 腾讯注册 API 与 DNSPod 同时返回域名
- **THEN** 融合记录的注册商与 DNS 托管商均为对应腾讯云账号

#### Scenario: 未来其他注册商与 DNS 厂商组合
- **WHEN** 任意新 Provider 分别声明注册角色和 DNS 角色并返回同一根域名
- **THEN** 系统按角色完成融合，无需为厂商组合增加特殊分支

#### Scenario: 只有 DNS 数据
- **WHEN** 系统只发现某域名的 DNS Zone
- **THEN** 系统仍展示该域名和 DNS 记录，并将注册到期时间及注册主体标记为未知

#### Scenario: 只有注册数据
- **WHEN** 系统只发现某域名的注册信息
- **THEN** 系统仍展示其到期时间与注册主体，但不为其保存 DNS Provider 映射

### Requirement: 多来源冲突必须确定且可诊断
系统 MUST 对同一根域名的多个注册来源或多个 DNS 来源执行确定性选择，并 MUST 记录候选来源、判断依据和最终来源。DNS 多候选 MUST 依次使用人工覆盖、公网权威 NS 匹配、平台强证据；无法确认时 MUST NOT 按配置顺序猜测。

#### Scenario: 公网 NS 识别实际 DNS 平台
- **WHEN** 两个 DNS Provider 都返回同一根域名
- **THEN** 系统查询公网 NS，并选择分配 NS 与公网结果匹配的 Provider

#### Scenario: 人工覆盖 DNS Provider
- **WHEN** `domain_dns_overrides` 为冲突域名指定了有效候选账号
- **THEN** 系统优先使用指定账号，不受 Provider 配置顺序影响

#### Scenario: Cloudflare 强证据回退
- **WHEN** 公网 NS 查询失败且唯一 Cloudflare 候选为 `active + full`
- **THEN** 系统选择该 Cloudflare Provider，并记录选择依据

#### Scenario: 冲突无法确认
- **WHEN** 多个 DNS 候选均不匹配公网 NS、没有人工覆盖且没有唯一平台强证据
- **THEN** 系统不写入该域名的 DNS Provider 映射，页面禁用在线签发并记录错误

### Requirement: 证书签发只使用 DNS Provider
系统 MUST 仅把融合记录的 DNS Provider 写入 `DomainProviderStore`，页面签发 MUST 使用同一 DNS Provider；注册商 Provider MUST NOT 覆盖签发映射。

#### Scenario: 跨平台域名首次签发
- **WHEN** 域名在阿里云注册并在 Cloudflare 托管 DNS
- **THEN** 页面签发和 acme.sh 使用 Cloudflare Provider 及 `dns_cf`

#### Scenario: 同平台域名首次签发
- **WHEN** 域名注册和 DNS 均位于阿里云或腾讯云
- **THEN** 系统把对应云账号写入 `DomainProviderStore` 并使用该云 DNS 插件

### Requirement: 到期告警只使用注册信息
系统 SHALL 使用注册来源的到期时间和账号生成域名到期告警；注册信息未知时 MUST 跳过到期告警。

#### Scenario: Cloudflare DNS 补全阿里云到期信息
- **WHEN** 融合记录的 DNS 来源为 Cloudflare、注册来源为阿里云且域名临近到期
- **THEN** 系统按阿里云到期时间告警，并在通知中标识阿里云注册账号

### Requirement: 页面分别展示注册商和 DNS 托管商
域名页面 SHALL 分别展示注册平台/账号和 DNS 平台/账号，并 SHALL 展示融合后的到期时间与注册主体。

#### Scenario: 跨平台域名页面展示
- **WHEN** 用户查看阿里云注册、Cloudflare DNS 的域名
- **THEN** 页面同时显示阿里云注册信息、Cloudflare DNS 信息及 Cloudflare DNS 记录
