## ADDED Requirements

### Requirement: 扫描华为云 WAF 证书绑定
系统 SHALL 使用配置账号的华为云 WAF 客户端分页扫描云模式防护域名，并为启用 HTTPS 的域名生成 `product_type=waf` 的产品绑定。

#### Scenario: 扫描已绑定证书的 HTTPS 域名
- **WHEN** WAF 云模式防护域名启用 HTTPS 且存在当前证书
- **THEN** 系统返回包含 host ID、域名、当前证书 ID、证书名和企业项目 ID 的绑定

#### Scenario: 跳过非 HTTPS 域名
- **WHEN** WAF 防护域名未启用 HTTPS
- **THEN** 系统不生成该域名的 WAF 证书绑定

#### Scenario: 单个域名详情查询失败
- **WHEN** 列表中的一个 WAF host 无法读取详情
- **THEN** 系统记录告警并继续扫描其余 host

### Requirement: WAF 扫描开关
系统 SHALL 通过 `product_scan.<cloud>.waf_scan` 控制 WAF 绑定扫描，并 SHALL 在缓存刷新、实时匹配和 apply 前校验中使用同一开关。

#### Scenario: 关闭 WAF 扫描
- **WHEN** `product_scan.huawei.waf_scan=false`
- **THEN** 系统不调用华为云 WAF 绑定扫描能力

### Requirement: 上传并复用 WAF 证书
系统 SHALL 在一次华为云部署流程中向 WAF 证书仓库上传一次新证书，并将返回的证书 ID 复用于所有匹配的 WAF 目标。

#### Scenario: 多个 WAF 域名命中同一证书
- **WHEN** 同一华为云 provider 下多个 WAF host 与续签证书匹配
- **THEN** 系统只创建一张 WAF 证书并依次绑定到所有命中 host

#### Scenario: 同时命中 ELB 与 WAF
- **WHEN** 同一华为云 provider 同时存在 ELB 和 WAF 目标
- **THEN** 系统分别向 ELB 与 WAF 证书仓库上传证书，并使用各自返回的证书 ID 部署

### Requirement: 安全部署 WAF 证书
系统 SHALL 只更新匹配 WAF host 的证书绑定，不得修改源站、TLS、策略和防护状态等其他配置。

#### Scenario: 绑定新证书
- **WHEN** apply 模式下 WAF host 通过实时校验且证书上传成功
- **THEN** 系统调用 WAF 证书绑定接口，仅提交新证书 ID 和目标 cloud host ID，并返回成功结果

#### Scenario: 上传或绑定失败
- **WHEN** WAF 证书创建或绑定接口失败
- **THEN** 系统记录明确错误并将目标部署结果标记为失败
