## Context

`app/ui/pages.py` 的「部署」按钮调用 `deploy_existing_only`。`apply_deploy` 内部明确不发通知。续签流程 `renew_and_plan_deploy` 才调用 `notify_renew_result`。仅部署与续签是两条入口，不能把部署结果伪装成「续期成功/失败」。

## Goals / Non-Goals

**Goals:**

- 点「部署」成功：发一条 INFO「证书部署成功」，正文与续签成功通知同一格式。
- 点「部署」失败（加载证书失败、apply 抛错、dry-run 抛错）：发一条 CRITICAL「证书部署失败」，再抛出原异常，UI toast 仍可用。
- 正文格式化只维护一份。

**Non-Goals:**

- 不改 `notify_deploy_result`（按单个 provider/product 的旧接口）。
- 不在 `DeployService` 里发通知。
- 不改页面 `ui.notify` 行为。

## Decisions

1. **通知落在 `deploy_existing_only`，不落在 UI。**  
   UI 只负责 toast；运维通道与续签流程一样走 service 层，CLI/e2e 调同一入口也会通知。

2. **新增 `notify_deploy_summary`，不复用 `notify_renew_result`。**  
   标题必须是部署语义。成功时 `domain=""`，域名写在 message 里，与续签成功一致，避免 `_format_event` 再拼一行「域名:」。

3. **失败先通知再 `raise`。**  
   保持现有 UI `except` 展示「仅部署失败」。通知失败本身按 notifier 现有逻辑抛错（通道都失败会 `RuntimeError`）。

4. **抽出 `format_deploy_notify_message`。**  
   `renew_and_plan_deploy` 成功分支改为调用该函数，行为与原来一致。

## Risks / Trade-offs

- dry-run 成功也会发「证书部署成功」。与续签流程在 dry-run 下仍发成功通知一致；正文来自 `summary.by_provider` 回退。
- 失败通知的 message 是异常字符串，没有逐目标列表（`apply_deploy` 失败时直接抛错、不返回 `bind_results`）。与续签部署失败路径一致。
