## Why

首页「部署」按钮走 `deploy_existing_only`，该路径只执行加载证书 + dry-run/apply，**从不调用** `DingDingNotifier`。`DeployService.apply_deploy` 注释写明「通知由上层统一处理」，但上层仅在 `renew_and_plan_deploy` 里发了通知。结果是：点「部署」无论成功还是失败，钉钉 / Rocket.Chat 都没有消息；页面上的 `ui.notify` 只是浏览器 toast，不是运维通道。

## What Changes

- 抽出 `format_deploy_notify_message`，续签成功通知与仅部署通知共用同一套正文（域名 + 部署目标）。
- `DingDingNotifier` 新增 `notify_deploy_summary`：成功标题「证书部署成功」，失败标题「证书部署失败」，避免误用「证书续期成功/失败」。
- `deploy_existing_only`：apply/dry-run 成功后发成功汇总；加载证书或部署抛错时先发失败通知，再把原异常抛给 UI。

## Capabilities

### New Capabilities

- `manual-deploy-notify`: 仅部署（不续签）路径的成功/失败汇总通知。

### Modified Capabilities

<!-- 当前 specs 仅有 domain-matching，本次不改它的需求 -->

## Impact

- 受影响代码：
  - `app/services/renew_flow.py`（抽出正文格式化、`deploy_existing_only` 发通知）
  - `app/utils/notifier.py`（新增 `notify_deploy_summary`）
  - `tests/test_renew_flow.py`、`tests/test_notifier_and_alerts.py`
  - `docs/services.md`
- 不受影响：`renew_and_plan_deploy` 的续签标题与冷却/重试逻辑；`notify_deploy_result` 单目标接口保持原样。
- 告警通道：复用现有钉钉 / Rocket.Chat，不新增 webhook。
