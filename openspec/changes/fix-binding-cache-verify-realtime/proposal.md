## Why

`deploy.binding_cache` 与 `deploy.binding_cache.verify_before_apply` 两个开关组合本应表达"缓存匹配 + 部署前实时校验"的部署语义，但当前实现里 `_get_provider_bindings` 在 `cache_enabled=True` 时只读缓存，连 `force_refresh=True` 都被忽略；导致 `_verify_matches_before_apply` 表面上请求"实时绑定"做对比，实际上只是把同一份缓存数据再 lookup 一次。结果是 verify 阶段无法发现"云上已经被人手工撤掉的 binding"，部署期才会因数据陈旧引发误操作（线上事故里 SNI 引用被错误覆盖即源于此类未及时校验的差异）。本 change 把这层"假实时"实落，确保三态行为可信。

此外 `ProductBindingStore` 当前只落盘 `(product_type, product_id, domain)` 三元组，并不存 `cert_id` / `listener_port` / `metadata`；本 change 因此把 verify 的"差异判定"对齐到缓存能力——"存在性级别"严格判定（按 `(product_type, product_id, domain)` 这把 key 看实时数据中是否存在），同时让 verify 阶段的实时数据接管字段填充。

## What Changes

- 修复 `_get_provider_bindings`：`cache_enabled=True && force_refresh=True` 时绕过缓存读取，按 `product_scan.<cloud>` 配置走云端实时扫描，**仅返回结果，不回写缓存**（缓存落盘仍由 `app/services/bindings.py::ProductBindingService.refresh_all` 这条独立路径专管）。
- 重构 `_verify_matches_before_apply` 为"存在性级别 strict 校验 + 字段填充 + 告警"：实时拉取每个命中 provider 的最新绑定，逐条 match 在实时数据中按 `(product_type, product_id, domain)` 查找；不存在该 key → skip + 钉钉 WARNING；存在 → 用实时 binding 的 `cert_id` / `listener_port` / `metadata` 覆盖 match 字段后放行。实时扫描整体抛异常 → 该 provider 下所有命中目标全部 skip + 钉钉 WARNING（**不**沿用缓存继续部署）。
- 落地三态行为矩阵：
  - `binding_cache.enabled=true && verify_before_apply=true`：缓存匹配 → 部署前实时存在性校验 → 缺失 skip+告警 / 存在则用实时字段填充后部署。verify 拉到的实时数据**仅用于本次部署**，不回写缓存。
  - `binding_cache.enabled=true && verify_before_apply=false`：完全以缓存为准，部署前不实时校验。
  - `binding_cache.enabled=false`：直接实时扫描匹配，**忽略** `verify_before_apply` 配置，跳过 verify 阶段，直接部署。
- `app/services/deploy.py::apply_for_certificate` / `plan_dry_run` 调用 verify 前先判定 `cache_enabled`，未启用缓存时直接绕过 verify。
- 钉钉告警接入：复用 `DingDingNotifier`，新增"部署 skip 告警"消息（source=`deploy`, level=`WARNING`），消息体含 provider、product_type、product_id、domain、跳过原因。
- 同步更新 `config.example.yaml` 中 `binding_cache` 注释，描述三态行为。

## Capabilities

### New Capabilities
- `binding-cache-verify`: 云产品绑定缓存与部署前实时校验能力，定义三态语义（启用/关闭缓存、启用/关闭实时校验）以及差异处置策略。

### Modified Capabilities
<!-- 当前 specs 仅有 domain-matching，本次不改它的需求 -->

## Impact

- 受影响代码：
  - `script/py/crt/app/services/deploy.py`（`_get_provider_bindings`、`_verify_matches_before_apply`、`apply_for_certificate`、`plan_dry_run` 调用路径）
  - `script/py/crt/config.example.yaml`（`binding_cache` 注释）
  - `script/py/crt/tests/test_services_and_planner.py`（verify 行为单测）
  - `script/py/crt/tests/test_binding_cache_service.py`（如已覆盖 verify，追加用例）
- 不受影响：`ProductBindingStore` 落盘结构、`app/services/bindings.py` 的 product_scan 路径、缓存文件 `data/product_bindings_cache.json` 的 schema。
- 配置兼容性：`BindingCacheConfig` 字段不变；旧配置无须迁移。
- 告警通道：复用现有 `DingDingNotifier.notify_alert`，不新增独立 webhook。
- 行为变化：开启 verify 时，凡云上已被手工撤掉的 binding 都会触发 skip+告警，而非沿用陈旧缓存继续部署 —— 这是修复，不是回退；用户拿到告警后可手工跑 product_scan 刷新缓存或修复云上配置后重试。
