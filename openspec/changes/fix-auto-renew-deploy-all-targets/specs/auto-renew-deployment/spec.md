## ADDED Requirements

### Requirement: 自动续签复用强制续签部署流程
系统 SHALL 在证书剩余天数达到自动续签阈值时，通过与 Web 强制续签相同的编排入口执行强制续签，并在成功后匹配全部部署目标。

#### Scenario: 阈值内证书触发强制续签和全量部署
- **WHEN** 自动续签已启用且证书剩余天数小于或等于配置阈值
- **THEN** 系统以 `force=True` 调用续签编排，并且不指定部署 provider

#### Scenario: DNS provider 由域名映射解析
- **WHEN** 自动续签调用编排时未显式传入 provider
- **THEN** 系统 MUST 使用现有域名映射解析续签所需的 DNS provider

#### Scenario: 未达到阈值时不触发
- **WHEN** 证书剩余天数大于配置阈值
- **THEN** 系统 SHALL 不调用续签部署编排
