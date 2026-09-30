## Context

部署管线把"匹配候选"和"部署执行"分成两步，原意：

- **匹配阶段** (`_collect_matches`)：从缓存中扫一遍 provider 全量 binding，找出本次续签证书覆盖的目标。优势是不打云 API，匹配速度只取决于本地 store。
- **校验阶段** (`_verify_matches_before_apply`)：apply 前再核一次"目标是否还在云上"，避免照着陈旧数据部署。

但 `_get_provider_bindings(cache_enabled=True, force_refresh=True)` 没有真正绕过缓存：

```187:200:script/py/crt/app/services/deploy.py
        if cache_enabled:
            record = self._binding_store.get_provider(provider_cache_key)
            if not record:
                return []
            ...
```

→ verify 拿到的是同一份缓存。线上现象是"verify 通过、部署却出问题"，因为整个 verify 链路是在自查自，根本没有触达云端。

更深一层的限制：`ProductBindingStore` 的落盘结构只保留 `(product_type, product_id, domain)` 三元组（见 `app/utils/store.py::_normalize_legacy_bindings` 与 `get_provider`），`cert_id` / `listener_port` / `metadata` 在落盘时被丢弃。也就是说：基于缓存做"字段级 strict diff" 的设计前提不成立——缓存里这些字段始终为空，与实时数据必然不一致。

`BindingCacheConfig` 当前定义：`enabled / ttl_seconds / verify_before_apply`，三个字段互不组合，文档也没明确"关 cache 时还要不要 verify"。本 change 把语义钉死。

## Goals / Non-Goals

**Goals:**
- 让 `force_refresh=True` 真正绕过缓存读云端，**仅返回结果，不回写缓存**（缓存落盘仍是 `bindings.py` 的独立职责，避免双写来源）。
- 把"verify"实落为"存在性级别 strict 校验 + 字段填充"：实时数据按 `(product_type, product_id, domain)` 查不到 → skip+告警；查得到 → 用实时数据覆盖 `cert_id / listener_port / metadata` 后进入部署。
- 把三态行为矩阵写入 spec（`binding-cache-verify`），代码与配置注释一致。
- 告警字段足够定位差异（provider / product_type / product_id / domain / 跳过原因）。

**Non-Goals:**
- 不实施 C 方案（无法安全替换 → 追加 SNI 条目），下次单独提 change。
- 不升级 `ProductBindingStore` 落盘结构以支持字段级 strict diff（如要做，单独立项 `expand-binding-cache-fields`）。
- 不在 verify 路径内回写缓存——保持"缓存落盘只由 product_scan 触发"的职责单一。
- 不改造 `_collect_matches` 的匹配语义（继续按缓存为唯一源）。

## Decisions

### 1. `force_refresh` 的实现位置与回写策略

**决定**：
- 在 `_get_provider_bindings` 内根据 `force_refresh` 选择数据源；
- 实时分支仅返回 binding 列表，**不调用 `ProductBindingStore.save_provider`**；
- 调用方继续传 `cache_enabled / force_refresh / cache_only` 三个独立含义的开关。

**理由**：
- 用户决策："verify 拉的实时数据仅用于本次部署，不回写缓存"。
- 保持单一入口、对调用方透明，缓存落盘职责仍由 `app/services/bindings.py::ProductBindingService.refresh_all` 独占，避免双写来源。
- `cache_only=True` 与 `force_refresh=True` 互斥时优先 `cache_only`（保留"只读缓存"的硬约束），但本次实现里 verify 分支不传 `cache_only`，匹配分支也只传 `cache_only=cache_enabled` 表示"启用缓存就只读缓存"，两者不会同时为 True。

**备选方案**：
- 实时分支顺手回写缓存 → 用户已否决；理由是 verify 是临时取数，不应污染"由 product_scan 主导"的缓存生命周期。
- 拆成 `_load_cached_bindings()` + `_fetch_realtime_bindings()` 两个函数 → 调用方需要自己决定何时刷缓存，反而增加复杂度。

### 2. 差异判定粒度（受缓存结构约束）

**决定**：差异判定下沉到"存在性级别"——按 `(product_type, product_id, domain)` 查实时数据：
- 实时不存在该 key → 视为差异（"目标已被手工撤掉"）；
- 实时存在 → 视为一致，用实时 binding 的 `cert_id` / `listener_port` / `metadata` 覆盖 match 字段后放行。

**理由**：
- 用户最初提的 strict 字段级 diff 在缓存层不可行：缓存里没存 `cert_id` / `listener_port` / `metadata`。
- 真正危险的两类问题——"binding 已不存在"和"云端不可达"——已被精确拦截。
- 部署期那些"云上 cert_id 被人手工换了 / 端口换了"的场景，本次部署的目的本来就是改 cert_id；继续用实时字段填充进 match 后下发是正确路径，不应 skip 触发误伤。

**备选方案**：扩展缓存结构存 `cert_id` / `listener_port` / `metadata`，再做字段级 strict diff → 显著扩大改动面，建议另起 change `expand-binding-cache-fields` 处理。

### 3. 差异处置：skip + 告警（用户已决策）

**决定**：差异即跳过该 match，并将该项追加到 `skipped` 列表（reason 含具体原因），同时调用 `DingDingNotifier.notify_alert` 发一条 `WARNING` 级告警。

**告警内容形态**：
```
⚠️ 部署校验差异（已跳过）
provider: <provider_name>
product: <product_type> / <product_id>
domain: <domain>
原因: 实时绑定中已不存在该目标 / 实时扫描接口异常: <exc>
```

**理由**：
- skip 是最保守的策略；放行可能造成事故，自动 fallback 缓存重写也不在本次范围。
- 钉钉告警立即可见，与现有续签/部署告警通道一致，便于人工跟进。

### 4. `cache_enabled=False` 时跳过 verify

**决定**：未启用缓存时，匹配阶段已是实时数据，verify 阶段无意义，无论 `verify_before_apply` 配置如何都跳过 verify 调用，保留 `verify_before_apply` 字段语义只在 `cache_enabled=True` 时生效。

**理由**：
- 用户决策：`binding_cache 关闭 → 不用检查 verify_before_apply 参数`。
- 避免重复打云 API（实时扫描后再 verify 等于 N+N 次调用，无收益）。

### 5. 告警接入复用 vs 新增方法

**决定**：直接在 `_verify_matches_before_apply` 内构造 `AlertEvent(level=WARNING, source="deploy", ...)`，调 `notifier.notify_alert(event)`。不在 `DingDingNotifier` 里加专属方法。

**理由**：
- `AlertEvent` schema 已支持 `source="deploy"`、`title`、`message`、`provider`、`domain` 字段（见 `app/schemas/alert.py`）。
- 一次性的告警形态，不需要业务封装；现有 `notify_deploy_result` 是"成功/失败"语义，与"verify skip"语义不同，不复用。
- 单点构造避免 notifier 接口膨胀。

### 6. 实时数据获取失败的退回路径

**决定**：当 `_get_provider_bindings(force_refresh=True)` 抛异常时，**整 provider 的命中目标全部 skip**（reason="apply 前实时校验失败，已跳过"），并发一条 WARNING 钉钉告警，不沿用缓存数据继续部署。

**理由**：
- 与"缺失即 skip"原则一致 — 不能确定云端状态时，宁可不动。
- 当前实现 `_verify_matches_before_apply` 在异常时是"沿用缓存继续部署"（见现有代码 `verified.extend(provider_matches); continue`）—— 这是隐患：云端 API 不可达时反而绕过了校验，与开关意图相反。本次顺手修。

### 7. dry-run 不调用 verify

**决定**：`plan_dry_run` 入口仅走 `_collect_matches` + `_plan_nginx_deploy`，**不调用** `_verify_matches_before_apply`。verify/告警语义只对 `apply_deploy` 路径有效。

**理由**：
- 阅读现有代码确认：`plan_dry_run` 已用 `deploy.mode != 'dry-run'` 锁定入口，且函数体内根本没有 verify 调用——dry-run 永不会触发告警是架构事实，不需要在 verify 内部加 mode 判断（属于防御性代码）。
- 把这条架构事实写进 spec，避免未来有人在 dry-run 路径里塞 verify 调用导致告警风暴。
- 如果将来需要"dry-run 也做实时核对（例如生成更准确的 dry-run 计划）"，应当走单独的 change 设计：那时再决定告警是否发出。

## Risks / Trade-offs

- [告警噪声] 缓存 TTL 内云资源被人手工撤掉会触发 skip+告警 → 这是符合预期的。但**首次启用本变更**且生产缓存陈旧时，可能瞬间产生大量告警；缓解：发布前手工跑一次 product_scan 刷新缓存。
- [云 API 限流] verify 阶段对每个有命中的 provider 触发一次实时扫描；若一次续签命中多个 provider，扫描调用会成倍增长 → 缓解：现有 product_scan 限流逻辑已存在；扫描接口本身就是按 provider 分组，不会形成 N×M 风暴。
- [运维介入成本] strict 缺失即 skip 意味着需要人工判断"云端是不是真的撤了 / 是不是缓存陈旧" → 缓解：钉钉告警含完整字段，运维只需"刷缓存"或"修云端"二选一即可恢复。
- [缓存与实时不同步] verify 拉到的实时数据不回写缓存——如果有"云上撤掉"和"缓存仍有"的偏差，下次匹配阶段仍可能命中并再次触发 verify skip → 缓解：人工触发 product_scan 刷新；这是设计选择（缓存生命周期由 scan 独占），不是 bug。

## Migration Plan

1. 合并本 change 后，第一次部署前**手工触发一次 `product_scan`** 刷新所有 provider 缓存（避免 TTL 内的陈旧数据触发告警风暴）。
2. 之后正常运行；遇到 skip 告警，根据告警内容选择：刷缓存 / 修云端配置 / 手工部署。
3. 回滚策略：`git revert`。无数据迁移；缓存文件结构不变。

## Open Questions

无（用户已对差异判定粒度从"字段级 strict"调整为"存在性 strict"、verify 不回写缓存、告警通道、dry-run 不告警等关键决策点拍板）。
