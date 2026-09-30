## ADDED Requirements

### Requirement: 仅部署成功发送部署汇总通知

`deploy_existing_only` 在 dry-run 或 apply 成功返回后 SHALL 调用 `DingDingNotifier.notify_deploy_summary(success=True)`。通知标题 MUST 为「证书部署成功」，source MUST 为 `deploy`。正文 MUST 使用 `format_deploy_notify_message`，与续签成功通知同一格式（首行 `域名: <domain>`，随后 `部署目标:` 列表）。MUST NOT 使用「证书续期成功」标题。

#### Scenario: 首页点部署且 apply 成功

- **WHEN** 调用 `deploy_existing_only(domain)`，`deploy.mode=apply`，`apply_deploy` 返回含阿里云 CDN 与 nginx 组的成功 `bind_results`
- **THEN** 发送一次成功通知，标题为「证书部署成功」，正文含该域名、`奇裕 阿里云 cdn` 与 `nginx 组` 行

#### Scenario: 续签成功通知格式不变

- **WHEN** `renew_and_plan_deploy` apply 成功
- **THEN** 仍发送「证书续期成功」，正文仍由 `format_deploy_notify_message` 生成

### Requirement: 仅部署失败发送失败通知并抛出原异常

`deploy_existing_only` 在加载证书或部署阶段抛错时 SHALL 先发送 `notify_deploy_summary(success=False)`，标题 MUST 为「证书部署失败」，message MUST 包含失败原因，然后 MUST 重新抛出原异常。

#### Scenario: apply 存在失败目标

- **WHEN** `apply_deploy` 抛出 `RuntimeError("证书部署存在失败: 2 个目标失败")`
- **THEN** 发送一次失败通知（标题「证书部署失败」，正文含该错误），并且该 `RuntimeError` 继续向上抛出
