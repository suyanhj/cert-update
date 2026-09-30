# CRT 运行流程总览

本文只保留运行主流程与关键入口状态，细节见 `flow-services.md` 与 `flow-overview.svg`。
模块职责详见 `services.md` / `providers.md` / `deploys.md`，配置规则详见 `config-rules.md`。

## 1. 调度与采集主流程（简版）

```mermaid
flowchart TD
    A[main.py 启动] --> B[hydrate_state_from_snapshot]
    B --> C[start_scheduler]
    C --> D[scheduler loop]
    D --> E[collector.collect_all]
    E --> F[collect_domains / collect_ecs_instances]
    E --> G[refresh_product_bindings_cache]
    E --> H[collect_certificates]
    H --> I[_auto_renew_and_deploy_expiring_certificates]
    E --> J[persist_state_snapshot]
```

## 2. 签发/续签编排与部署主流程（简版）

```mermaid
flowchart TD
    A[托管域名列表签发弹窗] --> B[renew_flow.issue_and_plan_deploy]
    C[续签入口（手动/自动）] --> E[renew_flow.renew_and_plan_deploy]
    B --> F[renew.issue_certificate]
    E --> G[renew.renew_certificate]
    F --> D{deploy.mode}
    G --> D
    D -->|dry-run| E[DeployService.plan_dry_run]
    D -->|apply| F[DeployService.apply_deploy]
```

## 3. 细节参考

- 采集链路细节：`flow-services.md`
- 总览视角：`flow-overview.svg`
- 部署细节：`deploys.md`

## 4. 当前开关状态

- `refresh_product_bindings_cache()` 已启用，仅刷新 `product_bindings_cache.json`。
- 自动续签编排入口已启用，受 `auto_renew_enabled=true` 与 `auto_renew_days>0` 共同控制。
- UI 的托管域名列表提供可编辑“签发”弹窗，用于本机 acme.sh 首次接管；证书池仅提供“强制续签 / 部署”。
