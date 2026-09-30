## Why

Nginx `cert_layout` 原先使用 `{cert_name}` 表示 `nginx_deploy_rules[].cert_name_template` 的渲染结果，无法同时表达证书主域名、规则命中域名和模板渲染结果，语义容易混淆。

## What Changes

- 为 `cert_layout.cert_file/key_file` 提供 `{resolved_cert_name}`、`{main_domain}` 和 `{cert_name}` 三个明确占位符。
- `{resolved_cert_name}` 表示当前命中规则的 `cert_name_template` 渲染结果。
- `{main_domain}` 和 `{cert_name}` 分别保持证书主域名和规则实际命中域名的语义。
- 同步配置模型默认值、运行配置、示例、文档和测试。

## Capabilities

### New Capabilities

- `nginx-cert-layout`: 定义 Nginx 证书目标路径占位符及其与部署规则名称模板的关系。

### Modified Capabilities

无。

## Impact

- 影响 Nginx 部署规划器、Nginx 配置模型和 CRT 配置说明。
- 仓库运行配置改用 `{resolved_cert_name}` 明确继承关系；需要直接使用命中域名的配置仍可使用 `{cert_name}`。
- 不影响证书匹配、签发、云平台部署及 SSH 写入流程。
