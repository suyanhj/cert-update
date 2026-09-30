## ADDED Requirements

### Requirement: DNSPod 域名发现带解析探测

`TencentCloudProvider.get_domain_list()` SHALL 通过 DNSPod 列出账号下域名，并对每个根域名拉取解析记录；ENABLE 且类型为 A 或 CNAME 的记录 MUST 经过 HTTP 探测后写入该域名的 `subs`。`is_domain_provider()` MUST 只根据根域名判断归属，不得触发探测。

#### Scenario: 根域名带探测后的子域

- **WHEN** DNSPod 返回根域名 `example.com`，且存在 ENABLE 的 A/CNAME 记录 `www`
- **THEN** `get_domain_list()` 返回项含 `domain=example.com`，`subs` 中含探测结果（含 `name` 与 `status`）

#### Scenario: 归属判断不探测

- **WHEN** 调用 `is_domain_provider("www.example.com")` 且根域名列表含 `example.com`
- **THEN** 返回 true，且不调用 HTTP 探测

### Requirement: CDN 仅扫描在线 HTTPS 证书绑定

`get_cdn_bindings()` SHALL 翻页读取 CDN 域名配置，仅返回同时满足以下条件的绑定：`Status` 为 online、`Https.Switch` 为 on、`Https.CertInfo.CertId` 非空。`product_type` MUST 为 `cdn`，`product_id` MUST 为加速域名。`metadata` MUST 包含 `https_billing`（取自 `HttpsBilling.Switch`，缺失时为 `on`）。

#### Scenario: 过滤未开 HTTPS 的 CDN

- **WHEN** 某加速域名在线但 `Https.Switch` 不为 `on`，或没有 `CertId`
- **THEN** 该域名不出现在绑定列表中

#### Scenario: 在线 HTTPS CDN 进入绑定

- **WHEN** 加速域名 `cdn.example.com` 状态 online、HTTPS 开启且 `CertId=abc`
- **THEN** 返回一条 `product_type=cdn`、`product_id=cdn.example.com`、`domain=cdn.example.com`、`cert_id=abc` 的绑定

### Requirement: CLB 仅扫描带域名的 HTTPS 规则

`get_lb_bindings()` SHALL 扫描配置 region 下的 CLB HTTPS 监听器。有转发规则域名时 MUST 按域名生成绑定；没有域名的监听器 MUST 丢弃，不得用 VIP 充当 `domain`。`product_type` MUST 为 `clb`，`product_id` MUST 为 `<region>:<lb_id>:<listener_id>`。同一 listener 与同一域名的多条 URL 规则 MUST 去重为一条绑定。`metadata` MUST 包含 `listener_id` 与 `sni_switch`。

#### Scenario: SNI 规则域名成为绑定

- **WHEN** CLB `lb-1` 的 HTTPS 监听器 `lbl-1` 开启 SNI，规则域名为 `api.example.com`
- **THEN** 返回绑定 `product_id=<region>:lb-1:lbl-1`、`domain=api.example.com`，`metadata.sni_switch` 为真

#### Scenario: 无域名监听器不产出绑定

- **WHEN** HTTPS 监听器没有任何规则域名
- **THEN** 绑定列表不含该监听器，也不出现 VIP 字符串作为 `domain`

### Requirement: 证书上传一次并复用

`TencentDeployer` SHALL 实现 `upload_certificate()`：调用 SSL `UploadCertificate`（`Repeatable=false`），返回 `CertificateId`；若为空则返回 `RepeatCertId`；两者都空 MUST 抛错。`deploy()` 在传入非空 `cert_id` 时 MUST NOT 再次上传。

#### Scenario: 上层复用 cert_id

- **WHEN** `upload_certificate()` 返回 `cert-1`，随后 `deploy(..., cert_id="cert-1")`
- **THEN** 不再调用 `UploadCertificate`，部署请求使用 `cert-1`

#### Scenario: 重复证书返回已有 ID

- **WHEN** `UploadCertificate` 返回空 `CertificateId` 且 `RepeatCertId=cert-old`
- **THEN** `upload_certificate()` 返回 `cert-old`

### Requirement: CDN 与 CLB 通过 SSL 部署接口下发

CDN 与 CLB 下发 SHALL 调用 `DeployCertificateInstance`，不得用只含部分字段的 `UpdateDomainConfig.Https` 覆盖整份 HTTPS 配置。CDN 的 `InstanceIdList` MUST 为 `<domain>|<https_billing>`。CLB 开启 SNI 时 MUST 为 `<lb_id>|<listener_id>|<domain>`，未开启 SNI 时 MUST 为 `<lb_id>|<listener_id>`。缺少 `domain`（CDN）或 `listener_id`（CLB）MUST 失败。`DeployStatus=0` MUST 视为失败。

#### Scenario: CDN 部署实例格式

- **WHEN** 目标为 CDN 域名 `cdn.example.com`，`metadata.https_billing=off`，`cert_id=cert-1`
- **THEN** 调用 `DeployCertificateInstance`，`ResourceType=cdn`，`InstanceIdList=["cdn.example.com|off"]`，`CertificateId=cert-1`

#### Scenario: CLB SNI 部署实例格式

- **WHEN** 目标 `product_id=ap-guangzhou:lb-1:lbl-1`，`domain=api.example.com`，`metadata.sni_switch` 为真
- **THEN** 调用 `DeployCertificateInstance`，`ResourceType=clb`，`InstanceIdList=["lb-1|lbl-1|api.example.com"]`

### Requirement: 运行配置启用腾讯云扫描

`product_scan.tencent` MUST 开启 `domain_scan`、`cdn_scan`、`lb_scan`，并关闭 `oss_scan` 与 `ecs_scan`。运行配置 SHALL 允许 `type: tencent` 的 provider，凭证键为 `secret_id`、`secret_key`，可选 `region`。

#### Scenario: 扫描开关

- **WHEN** 加载 `config.example.yaml` 的 `product_scan.tencent`
- **THEN** `cdn_scan` 与 `lb_scan` 与 `domain_scan` 为 true，`oss_scan` 与 `ecs_scan` 为 false
