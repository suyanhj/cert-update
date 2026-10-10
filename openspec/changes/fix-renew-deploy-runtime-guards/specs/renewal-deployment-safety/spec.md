## Purpose

为证书自动续签、采集依赖和多目标部署提供可靠的状态保护与结果反馈，确保用户能够区分已完成部署、部分失败和仅生成部署计划，并防止旧证书持续触发重复签发。

## ADDED Requirements

### Requirement: Automatic renewal respects persisted protection
系统 SHALL 在自动续签中遵守成功冷却期和部署失败待人工处理状态，同时保留阈值内执行续签命令及全云部署范围；手工强制续签 SHALL 可绕过该保护。

#### Scenario: Deployment previously failed
- **WHEN** 自动任务遇到已续签但部署失败的证书
- **THEN** 不重新签发，返回待人工处理状态

#### Scenario: Successful renewal is cooling down
- **WHEN** 自动任务遇到成功冷却期内的证书
- **THEN** 跳过续签，不调用签发命令

#### Scenario: SAN names share a certificate
- **WHEN** 同一轮多个阈值内域名解析为同一张实际证书
- **THEN** 该证书最多触发一次续签流程

### Requirement: Failed domain collection does not block future rounds
系统 SHALL 在当前域名采集失败后跳过证书采集和自动续签，继续独立采集并返回失败，不无限等待域名就绪，也不使用旧快照执行自动操作。

#### Scenario: Initial collection fails without a snapshot
- **WHEN** 首次域名采集失败且没有就绪事件
- **THEN** 本轮及时报错，调度可以继续下一轮

#### Scenario: Collection fails with old snapshot
- **WHEN** 当前域名采集失败而历史快照已就绪
- **THEN** 不基于旧清单自动续签

### Requirement: Deployment failures remain visible
系统 SHALL 将认证、计划目标配置和实时校验接口失败计入部署失败，保留已成功目标和失败原因；存在成功及失败目标时 SHALL 表示部分失败，不记录整体成功。

#### Scenario: One cloud succeeds while verification fails elsewhere
- **WHEN** 一个部署目标成功而另一账号实时校验接口失败
- **THEN** 保留两者结果，汇总为部分失败并发送失败通知

#### Scenario: Nginx credentials are unavailable
- **WHEN** 已匹配 Nginx 目标但凭据解析失败
- **THEN** 将目标记录为失败，不将其无声丢弃

#### Scenario: A target was actually removed
- **WHEN** 实时查询成功确认原目标已不存在
- **THEN** 正常跳过该目标，不将删除等同于接口失败

### Requirement: Dry run is distinct from deployment success
系统 SHALL 为预演返回计划完成状态，明确尚未部署，不写入成功部署冷却状态，也不发送已完成部署的成功说明。

#### Scenario: Renewal produces a deployment plan
- **WHEN** 续签或仅部署在 dry-run 模式完成计划
- **THEN** 返回 planned 部署状态，不记录整体部署成功
