## ADDED Requirements

### Requirement: 路径模板使用已解析证书名称
系统 SHALL 在 Nginx `cert_layout` 中使用 `{resolved_cert_name}` 表示当前部署规则的 `cert_name_template` 渲染结果。

#### Scenario: 使用主域名模板生成目标路径
- **WHEN** `cert_name_template` 为 `{main_domain}` 且证书主域名为 `example.com`
- **THEN** `{resolved_cert_name}` MUST 解析为 `example.com`

#### Scenario: 使用命中域名模板生成目标路径
- **WHEN** `cert_name_template` 为 `{cert_name}` 且规则命中 `api.example.com`
- **THEN** `{resolved_cert_name}` MUST 解析为 `api.example.com`

### Requirement: 路径模板识别三种证书名称
系统 MUST 在 `cert_layout.cert_file` 和 `cert_layout.key_file` 中分别识别 `{resolved_cert_name}`、`{main_domain}` 和 `{cert_name}`，并保持三者的数据来源独立。

#### Scenario: 三种名称同时用于路径
- **WHEN** 证书主域名为 `example.com`、规则命中 `api.example.com`，且 `cert_name_template` 渲染为 `release-example.com`
- **THEN** `{main_domain}` MUST 为 `example.com`、`{cert_name}` MUST 为 `api.example.com`、`{resolved_cert_name}` MUST 为 `release-example.com`
