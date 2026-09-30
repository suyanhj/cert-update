## ADDED Requirements

### Requirement: 续签与部署失败状态分离
系统 MUST 只对续签动作本身进行自动重试。续签成功但部署失败时 MUST 保存 `deploy_failed` 状态、发送失败告警并停止自动操作；后续自动续签 MUST 保留现有证书并等待人工处理。

#### Scenario: 续签成功但部署失败
- **WHEN** 新证书已经签发且自动部署返回失败
- **THEN** 系统保存 `deploy_failed`，不自动重试部署，也不在下一轮重复续签

#### Scenario: 续签动作失败
- **WHEN** acme.sh 签发证书失败
- **THEN** 系统按 `max_renew_retries` 和重试间隔执行续签重试

### Requirement: 腾讯 CDN 双路径保持可达
系统 MUST 保留 SSL `DeployCertificateInstance` 和 CDN `ModifyDomainConfig` 两条路径。SSL 扫描结果 SHALL 覆盖同一资源的旧目标，但不得删除未被 SSL 扫描返回的旧 `modify` 目标。

#### Scenario: SSL 扫描未返回旧 CDN 目标
- **WHEN** 缓存存在 `cdn_deploy_mode=modify` 的 CDN 目标且 SSL 扫描没有返回同一域名
- **THEN** 系统仍使用 `ModifyDomainConfig` 部署该目标

### Requirement: 查询失败不得伪装为空结果
系统 MUST 区分成功的空列表与查询异常。产品或域名查询失败时 MUST 保留上一次有效缓存，并向采集入口返回失败。

#### Scenario: 云产品接口失败
- **WHEN** 云厂商产品查询因权限、参数或限流失败
- **THEN** 系统不覆盖该 provider 的绑定缓存，并报告该 provider 刷新失败

#### Scenario: 单个域名 provider 失败
- **WHEN** 域名发现中的任一 provider 查询失败
- **THEN** 系统继续扫描其余 provider，但不覆盖完整域名映射，并报告失败

### Requirement: 采集完成状态真实反映结果
系统 SHALL 仅在所有关键采集阶段成功时更新时间并向 UI 返回成功。任一阶段失败 MUST 返回聚合错误。

#### Scenario: 部分采集失败
- **WHEN** 域名、产品绑定或证书采集中任一阶段失败
- **THEN** 系统不更新本轮成功时间，UI 显示失败及失败阶段

### Requirement: 告警状态在发送成功后提交
系统 MUST 在通知成功后才把告警状态保存为 FIRING。通知失败 MUST 保持可重试状态。

#### Scenario: 通知发送失败
- **WHEN** 告警事件已经生成但通知渠道返回失败
- **THEN** 系统不提交 FIRING，下一轮扫描继续尝试发送

### Requirement: 外部命令执行有界
系统 MUST 同时消费 acme.sh 的 stdout 和 stderr，并设置可配置或明确的最长执行时间。

#### Scenario: acme.sh 超时
- **WHEN** acme.sh 超过最长执行时间仍未退出
- **THEN** 系统终止子进程并返回可诊断的超时错误

### Requirement: 未知 ECS 到期时间不触发到期告警
系统 MUST 将缺失或无法解析的 ECS 到期时间视为未知，不得折叠为零天。

#### Scenario: ECS 到期时间缺失
- **WHEN** 云接口未返回有效到期时间
- **THEN** 系统跳过该实例的到期告警并记录诊断日志
