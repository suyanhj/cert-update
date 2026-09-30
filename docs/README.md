# CRT 文档导航（去重版）

## 1. 每份文档负责什么

- `flow-runtime.md`：只放运行主流程与入口状态（简版）。
- `flow-services.md`：services 总流程（调度/采集/续签/部署/配置热更新）。
- `services.md`：`app/services/*` 职责、调用链、接入建议。
- `providers.md`：`app/providers/*` 职责、能力、接入建议。
- `deploys.md`：`app/deploys/*` 职责、能力、接入建议。
- `config-entry.md` / `config-rules.md`：配置解析与规则（`extends` / 合并 / 去重 / 原子写入）。
- `runtime-data.md`：运行时数据结构与字段约定。
- `notifications.md`：钉钉、Rocket.Chat 与 SMTP 邮件通知配置及失败语义。

## 2. 推荐阅读顺序

1. `flow-overview.svg`
2. `flow-runtime.md`
3. `flow-services.md`
4. `services.md` / `providers.md` / `deploys.md`
5. `config-rules.md`
6. `runtime-data.md`

## 3. 术语统一（固定写法）

- 编排层：`app/services/*`
- 发现层：`app/providers/*`
- 执行层：`app/deploys/*`
- 自动续签编排：采集后触发续签，再按 `deploy.mode` 执行部署计划/执行
- 运行时配置视图：`extends` 展开 + 合并 + 去重后的最终配置
- 产品绑定缓存：`data/product_bindings_cache.json`

## 4. 文档去重规则

- 同一条规则只在一个文档里做“权威描述”。
- 其他文档只放链接或一句引用，不重复展开。
- 配置语义以 `config-rules.md` 为准。
- 数据结构以 `runtime-data.md` 为准。
- 运行流程以 `flow-runtime.md` / `flow-services.md` 为准。

## 5. 变更后同步建议

1. 先改对应代码。
2. 更新本文档的“当前实现状态”。
3. 按变化类型更新权威文档：
   - 流程变化：`flow-runtime.md` / `flow-services.md`
   - 配置语义变化：`config-rules.md`
   - 数据结构变化：`runtime-data.md`
   - 模块行为变化：`services.md` / `providers.md` / `deploys.md`

## 6. 当前实现状态

## 6.1 架构落点

- Provider 构建与分组：`provider_factory.py` + `provider_registry.py`
- 域名链路拆分：`domains.py` / `domain_discovery.py` / `domain_alert.py`
- 域名注册商与 DNS 托管商独立建模；同一根域名融合为一条页面记录，签发仅使用 DNS Provider。
- 签发/续签部署链路拆分：`renew.py` / `renew_flow.py` / `deploy.py` / `deployer_factory.py` / `nginx_planner.py`
- 配置热更新入口：`config_runtime.py`

## 6.2 当前开关与行为

- 采集主入口：`collector.collect_all()`
- `refresh_product_bindings_cache()`：已启用
- 自动续签编排：受 `auto_renew_enabled` + `auto_renew_days` 控制
- 部署模式：`deploy.mode=dry-run|apply`
- 产品绑定缓存：`data/product_bindings_cache.json`；匹配阶段优先用缓存，apply 前可选实时校验（`deploy.binding_cache.verify_before_apply`，默认建议 `false`，详见 `deploys.md`）。
- ECS 采集：Aliyun Provider 按多地域采集包年包月实例，写入 `collector_snapshot.json`。
- ECS 到期告警：已启用，由 `services/ecs_alert.py` 基于快照聚合告警并通过钉钉发送。

## 6.3 UI 状态

- 页面入口：`app/ui/pages.py`
- 配置保存：解析/校验/原子写入/reload/重启调度器
- 手动操作：托管域名列表提供“签发”弹窗；证书池移除普通续签，仅保留“强制续签 / 部署”

## 7. 文档与流程图索引

| 模块 | 文档 | 流程图 |
|------|------|--------|
| Services 总览 | [services.md](services.md) | [flow-services.md](flow-services.md) |
| 运行总览 | - | [flow-overview.svg](flow-overview.svg) |
| 运行主流程 | [flow-runtime.md](flow-runtime.md) | - |
| Config | [config-entry.md](config-entry.md) | - |
| Providers | [providers.md](providers.md) | [flow-providers.svg](flow-providers.svg) |
| Deployers | [deploys.md](deploys.md) | [flow-deployers.svg](flow-deployers.svg) |
| Notifications | [notifications.md](notifications.md) | - |

## 8. 总览

- [flow-overview.svg](flow-overview.svg)：Main / UI / Services 总览流程。
