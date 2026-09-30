## 1. 抽出部署通知正文

- [x] 1.1 将 `renew_and_plan_deploy` 内的部署目标摘要拼装抽成 `format_deploy_notify_message`
- [x] 1.2 续签成功通知改为调用该函数，保持原有正文格式

## 2. 仅部署路径发通知

- [x] 2.1 `DingDingNotifier` 新增 `notify_deploy_summary`
- [x] 2.2 `deploy_existing_only` 成功调用 `notify_deploy_summary(success=True)`
- [x] 2.3 `deploy_existing_only` 失败先 `notify_deploy_summary(success=False)` 再抛出原异常
- [x] 2.4 关键点日志：失败 ERROR、成功通知 INFO、通知发送失败 WARNING

## 3. 测试与文档

- [x] 3.1 `test_renew_flow.py` 覆盖仅部署成功/失败通知
- [x] 3.2 `test_notifier_and_alerts.py` 覆盖 `notify_deploy_summary` 标题与 domain
- [x] 3.3 `docs/services.md` 补充 `deploy_existing_only` 通知约定
- [x] 3.4 回归：续签成功/失败/部署失败均不得调用 `deploy_existing_only` 或 `notify_deploy_summary`
- [x] 3.5 回归：`format_deploy_notify_message` 保持历史正文格式
