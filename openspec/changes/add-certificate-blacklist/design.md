# Design

## Context

手动和自动续签共用 renew_and_plan_deploy，签发使用 issue_and_plan_deploy，仅部署使用 deploy_existing_only。运行配置使用 Box，schema 默认值不写回 Box；缺省黑名单须用 get 读取。

## Goals / Non-Goals

入口命中即停止整次操作，force 不绕过；不改变部署器内部匹配和已有告警白名单逻辑。

## Decisions

- 顶层 blacklist 默认空列表，普通条目精确匹配、*. 条目匹配直属一级子域；复用 normalize_domain 和 match_domain，配置加载与保存时校验并归一化去重。
- 签发检查全部请求域名；续签/仅部署先检查入口域名，再检查现有解析器得到的实际主域名。检查发生在状态存储、通知器、ACME 和部署器初始化之前。
- 使用共享检查函数返回 matched_domain、reason=blacklist、message，操作状态为 skipped。UI 使用已有通知机制，自动入口将跳过记录为跳过而非完成。
- 选择公共编排入口拦截，避免在 UI 和定时任务重复业务规则。低层测试工具直接调用 ACME 不在本次入口范围。

## Risks / Trade-offs

- 通配规则只阻止直属一级子域，不包括根域或多级子域；在配置说明中明确边界。
- 入口检查不读取真实证书 SAN；签发检查明确提交的 SAN，续签按入口和实际主域名控制整张证书。
- 配置热更新沿用既有机制，操作开始后修改黑名单影响下一次操作。
