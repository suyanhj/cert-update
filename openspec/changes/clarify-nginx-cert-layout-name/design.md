## Context

Nginx 部署分两步计算目标文件名：`nginx_deploy_rules[].cert_name_template` 先根据证书主域名或命中域名生成名称，`nginx_target_groups[].cert_layout` 再把这个结果插入目标路径。两步当前都使用 `cert_name` 一词，无法从路径模板看出值来自前一步。

## Goals / Non-Goals

**Goals:**

- 让路径模板占位符明确表示它是名称模板的已解析结果。
- 允许路径模板直接使用证书主域名和规则命中域名。
- 统一代码默认值、运行配置、示例、文档和测试。
- 保持 `cert_name_template` 内部 `{cert_name}` 的“规则实际命中域名”语义不变。

**Non-Goals:**

- 不修改证书与 Nginx 规则的匹配算法。
- 不修改 `base_dir` 或 reload 命令占位符。

## Decisions

- 路径模板采用 `{resolved_cert_name}`，表示 `cert_name_template.format(...)` 的输出；不采用 `{cert_name_template}`，因为传入路径的是渲染结果而不是模板字符串本身。
- `cert_layout` 与 `cert_name_template` 同时支持 `{main_domain}` 和 `{cert_name}`：其中 `{cert_name}` 指规则实际命中的域名（去除 `*.`），而 `{resolved_cert_name}` 仅在 `cert_layout` 中表示最终渲染名称。
- 运行配置一次性迁移为新占位符，避免新旧名称并存。

## Risks / Trade-offs

- [风险] `{cert_name}` 与 `{resolved_cert_name}` 在部分规则中结果相同 → 用三者结果不同的测试固定各自的数据来源。
- [风险] 名称更长 → 换取配置语义清晰，减少把匹配域名与最终目标名称混淆的概率。

## Migration Plan

仓库运行配置使用 `{resolved_cert_name}` 表示继承 `cert_name_template`；需要绕过名称模板、直接采用规则命中域名时使用 `{cert_name}`。完成后运行配置校验和 Nginx 规划测试。
