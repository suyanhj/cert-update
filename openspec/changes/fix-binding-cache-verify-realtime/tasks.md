## 1. `_get_provider_bindings` 实落 force_refresh（不回写缓存）

- [x] 1.1 修改 `app/services/deploy.py::_get_provider_bindings`：当 `cache_enabled=True && force_refresh=True` 时，跳过缓存读取分支，按 `product_scan.<cloud>` 配置走云端实时扫描；**仅返回结果，不调用 `ProductBindingStore.save_provider`**
- [x] 1.2 保持 `cache_only=True` 仍然只读缓存的硬约束；与 `force_refresh=True` 同时为真时优先 `cache_only`
- [x] 1.3 在关键节点加 INFO 日志：`force_refresh: provider=%s 已从云端拉取 binding 数=%d（不回写缓存）`

## 2. `_verify_matches_before_apply` 重构为存在性 strict + 字段填充 + 告警

- [x] 2.1 重写 `app/services/deploy.py::_verify_matches_before_apply`：
  - per provider 调用 `_get_provider_bindings(force_refresh=True)`；调用抛异常 → 该 provider 下全部 match 加入 `skipped`（reason="apply 前实时校验失败，已跳过"），并发一条 WARNING 钉钉告警，**不**沿用缓存继续；
  - per match 在实时索引中按 `(product_type, product_id, domain)` 查 key，**找不到 → `skipped`（reason="apply 前实时校验未命中，已跳过"）+ WARNING 告警**；
  - 找到 → 通过，使用实时 binding 字段构造 `DeployMatchResult`（`current_cert_id` / `listener_port` / `metadata` 取自实时数据）
- [x] 2.2 告警发送：构造 `AlertEvent(level=WARNING, source="deploy", title="部署校验差异（已跳过）", message=...含 product_type/product_id/domain/原因, provider=..., domain=...)`，调 `DingDingNotifier.notify_alert(event)`
- [~] 2.3 dry-run 不发钉钉告警：spec 改写为"`plan_dry_run` 不调用 verify 流程"作为架构事实，无需在 verify 内部加 mode 判定（防御性代码）

## 3. apply / dry-run 入口分发逻辑

- [x] 3.1 修改 `apply_deploy`：在调用 `_verify_matches_before_apply` 之前判断 `cache_enabled`，未启用缓存时直接绕过 verify（无论 `verify_before_apply` 取值），并补一条 INFO 日志说明
- [x] 3.2 通过阅读 `plan_dry_run` 实现确认其不走 verify 路径；架构事实已落入 spec，本任务无代码改动

## 4. 配置文档同步

- [x] 4.1 修改 `config.example.yaml` 中 `deploy.binding_cache` 区块的中文注释，明确写出三态行为矩阵（与 spec 一致），并注明"verify 拉的实时数据不回写缓存，缓存仅由 product_scan 刷新"

## 5. 测试

- [x] 5.1 为 `_get_provider_bindings` 新增/补强单测（`tests/test_services_and_planner.py`）：
  - `cache_enabled=True && force_refresh=True` → 走实时扫描分支并返回云端数据；**`save_provider` 未被调用**（断言 `_FakeBindingStore.save_calls == []`）
  - `cache_only=True && force_refresh=True` → 仍只读缓存
  - `cache_enabled=False` → 直接实时扫描，`force_refresh` 无影响
- [x] 5.2 为 `_verify_matches_before_apply` 新增单测（`tests/test_services_and_planner.py`）：
  - 实时存在目标 → match 通过，`current_cert_id` / `listener_port` / `metadata` 取实时值
  - 实时缺失目标 → match 进 `skipped`（reason 包含"未命中"），告警接口被调用一次
  - 实时扫描抛异常 → 该 provider 全部 match 进 `skipped`，告警一次
  - `cache_enabled=False` 时 verify 不被调用
  - `plan_dry_run` 入口路径上 verify 函数从未被调用（断言现有架构）
- [x] 5.3 运行 `python -m pytest tests/`，全部 PASS（117 passed）

## 6. 收尾

- [x] 6.1 执行 `openspec validate fix-binding-cache-verify-realtime --strict` 全绿
- [ ] 6.2 通知用户审阅本 change，等待确认后进入 archive 阶段
