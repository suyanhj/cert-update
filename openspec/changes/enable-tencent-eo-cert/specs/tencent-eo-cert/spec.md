## ADDED Requirements

### Requirement: EO 扫描可独立配置
系统 SHALL 提供 `product_scan.tencent.eo_scan` 开关，并仅在该开关启用时执行腾讯云 EO 证书目标扫描。

#### Scenario: 开启 EO 扫描
- **WHEN** 腾讯云 provider 可用且 `eo_scan` 为 `true`
- **THEN** apply 流程上传证书后调用 EO SSL Host 扫描并合并返回目标

#### Scenario: 关闭 EO 扫描
- **WHEN** `eo_scan` 为 `false`
- **THEN** 系统不调用 EO SSL Host 扫描，也不产生 EO 部署目标

### Requirement: 通过腾讯云 SSL 扫描匹配的 EO 域名
系统 SHALL 使用已上传证书 ID 调用 `DescribeHostTeoInstanceList`，请求 MUST 使用实时查询、`domainMatch=1` 并遍历分页结果。系统 SHALL 将每个非空且唯一的 `Host` 转换为 `product_type=teo` 的部署绑定。

#### Scenario: 扫描多个 EO 页面
- **WHEN** EO SSL Host 接口返回多页匹配实例，其中包含重复域名或空 Host
- **THEN** 系统遍历所有页面，仅为每个唯一非空 Host 生成一个 EO 绑定

#### Scenario: EO 扫描失败
- **WHEN** 腾讯云 SSL EO 扫描接口调用失败
- **THEN** apply 流程 SHALL 报告失败而不是静默忽略 EO 目标

### Requirement: 通过 SSL 服务部署 EO 证书
系统 SHALL 复用已上传到腾讯云 SSL 的证书 ID，通过 `DeployCertificateInstance` 将证书部署到 EO 域名，并等待部署记录达到成功终态。

#### Scenario: 成功部署 EO 证书
- **WHEN** EO 绑定进入部署阶段
- **THEN** 系统以 `ResourceType=teo`、`InstanceIdList=[<domain>]` 和已上传证书 ID 创建 SSL 部署任务

#### Scenario: EO 部署失败
- **WHEN** SSL 部署记录返回失败或等待超时
- **THEN** 系统返回失败结果并由上层统一聚合报告

### Requirement: 保持现有腾讯云产品行为
系统 MUST 在增加 EO 能力后保持 CDN、云直播、CLB 和 COS 的扫描及部署请求格式不变。

#### Scenario: EO 与其他产品同时启用
- **WHEN** EO 与任一现有腾讯云产品扫描开关同时启用
- **THEN** 系统合并各产品目标，并分别使用其既有 SSL 资源类型和实例格式部署
