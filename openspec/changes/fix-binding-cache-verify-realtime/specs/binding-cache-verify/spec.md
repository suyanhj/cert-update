## ADDED Requirements

### Requirement: 三态行为矩阵

云产品绑定的匹配与部署前校验 SHALL 遵循以下三态行为矩阵，由 `deploy.binding_cache.enabled` 与 `deploy.binding_cache.verify_before_apply` 两个开关组合决定：

| `enabled` | `verify_before_apply` | 行为 |
| --- | --- | --- |
| `true` | `true` | 匹配阶段读缓存；apply 前对每个命中目标拉取实时绑定做存在性校验，缺失即跳过+告警，存在则用实时字段填充后部署。verify 拉到的实时数据**仅用于本次部署**，不回写缓存 |
| `true` | `false` | 匹配阶段读缓存；apply 前不做实时校验，直接部署 |
| `false` | （忽略） | 匹配阶段直接调用云端实时扫描；不调用 verify 流程，直接部署 |

#### Scenario: 启用缓存且启用校验，实时数据中存在该目标
- **WHEN** `binding_cache.enabled=true && verify_before_apply=true`，匹配阶段从缓存命中目标 T，且 apply 前实时拉取的 binding 中按 `(product_type, product_id, domain)` 能查到 T
- **THEN** 目标 T 进入部署流程；其 `current_cert_id` / `listener_port` / `metadata` MUST 取自实时 binding，而非缓存视图

#### Scenario: 启用缓存且启用校验，实时数据缺失目标
- **WHEN** `binding_cache.enabled=true && verify_before_apply=true`，apply 前实时数据中按 `(product_type, product_id, domain)` 查不到该 match
- **THEN** 目标 T 不进入部署，被记入 `skipped` 列表（reason 含"apply 前实时校验未命中，已跳过"），且发送一条 `WARNING` 级钉钉告警，告警体含 provider、product_type、product_id、domain、跳过原因

#### Scenario: 启用缓存但关闭校验
- **WHEN** `binding_cache.enabled=true && verify_before_apply=false`，匹配阶段从缓存命中目标 T
- **THEN** 不调用 verify 流程，目标 T 直接进入部署，无论实时数据是否与缓存一致

#### Scenario: 关闭缓存
- **WHEN** `binding_cache.enabled=false`，无论 `verify_before_apply` 取值
- **THEN** 匹配阶段直接调用云端实时扫描；不调用 verify 流程；命中的目标直接进入部署

### Requirement: `force_refresh` 实落语义且不回写缓存

`_get_provider_bindings(force_refresh=True)` SHALL 真正绕过本地缓存，从云端实时扫描 provider 的全量绑定，并 MUST NOT 调用 `ProductBindingStore.save_provider` 把结果回写缓存。任何调用方传入 `force_refresh=True` 时，函数返回值 MUST 反映云端最新状态，但缓存文件 `data/product_bindings_cache.json` MUST 保持不变。

#### Scenario: cache_enabled=True 且 force_refresh=True
- **WHEN** 调用 `_get_provider_bindings(provider, cache_enabled=True, ttl_seconds=N, force_refresh=True)`
- **THEN** 函数调用云端 binding 扫描接口（CDN/LB/OSS 按 `product_scan.<cloud>` 配置筛选），返回与云端实时一致的 binding 列表；本地缓存内容 MUST NOT 被修改

#### Scenario: cache_enabled=True 且 force_refresh=False
- **WHEN** 调用 `_get_provider_bindings(provider, cache_enabled=True, ttl_seconds=N, force_refresh=False)`
- **THEN** 函数仅读取本地缓存，不调用云端 API；缓存缺失或过期时返回空列表

#### Scenario: cache_enabled=False
- **WHEN** 调用 `_get_provider_bindings(provider, cache_enabled=False, ...)`
- **THEN** 函数直接调用云端实时扫描，不读取也不写入缓存，`force_refresh` 参数无影响

### Requirement: 存在性级别 strict 校验与字段填充

apply 前 verify 阶段 SHALL 对每条命中 match 在实时 binding 集合中按 `(product_type, product_id, domain)` 查找：

- 实时集合中**不存在**该 key → 视为差异，跳过该 match 并发 WARNING 告警；
- 实时存在 → 视为通过，用实时 binding 的 `cert_id` / `listener_port` / `metadata` 覆盖原 match 字段后放行进入部署。

字段级差异（`cert_id`、`listener_port`、`metadata` 在缓存视图与实时数据之间的不同）MUST NOT 视为校验失败——因为缓存层不保存这些字段，缓存视图中它们恒为空或缺省。

#### Scenario: 实时数据中存在 binding 且字段补全
- **WHEN** apply 前实时数据中存在某 match 的 `(product_type, product_id, domain)`
- **THEN** 该 match 进入 `verified` 列表；`current_cert_id` 取自实时 binding 的 `cert_id`，`listener_port` 取自实时 binding 的 `listener_port`，`metadata` 取自实时 binding 的 `metadata`

#### Scenario: 实时数据缺失目标 binding
- **WHEN** apply 前实时绑定列表中按 `(product_type, product_id, domain)` 查不到匹配项
- **THEN** 该 match 被加入 `skipped`（reason 含"apply 前实时校验未命中，已跳过"），并发出一条 `WARNING` 级钉钉告警；告警体含 provider、product_type、product_id、domain、跳过原因

### Requirement: 实时数据获取失败的退回策略

当 verify 阶段调用云端实时扫描接口抛出异常（API 不可达、限流、鉴权失败等），SHALL 将该 provider 下所有命中目标全部跳过（reason 含"apply 前实时校验失败，已跳过"），并发出一条 `WARNING` 级钉钉告警；MUST NOT 沿用缓存数据继续部署。

#### Scenario: provider 实时扫描接口抛异常
- **WHEN** verify 阶段对某 provider 调用 `_get_provider_bindings(force_refresh=True)` 抛出异常
- **THEN** 该 provider 下的所有命中 match 全部加入 `skipped`，发出一条 `WARNING` 级钉钉告警（告警体含 provider 与异常摘要），且这些目标不进入部署

### Requirement: dry-run 不调用 verify 流程

`plan_dry_run` 入口 SHALL 仅执行匹配阶段（`_collect_matches`）和 Nginx 规划，MUST NOT 调用 `_verify_matches_before_apply`。任何"实时校验/字段填充/告警"行为只在 `apply_deploy` 路径上发生，dry-run 因此天然不会触发钉钉告警。

#### Scenario: dry-run 入口的执行路径
- **WHEN** 调用 `plan_dry_run(renewed_cert, ...)`，`deploy.mode=dry-run`
- **THEN** 函数 MUST NOT 调用 `_verify_matches_before_apply`；返回的 `planned_bind_actions` 来源于缓存或实时扫描的匹配结果（取决于 `binding_cache.enabled`），不经过 apply 前实时校验
