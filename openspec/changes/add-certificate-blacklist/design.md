# Design

## Context

手动和自动续签共用 renew_and_plan_deploy，签发使用 issue_and_plan_deploy，仅部署使用 deploy_existing_only。运行配置使用 Box，schema 默认值不写回 Box；缺省黑名单须用 get 读取。

## Goals / Non-Goals

入口命中即停止整次操作，force 不绕过；云产品实际目标也逐个过滤，禁止共享证书扫描间接更新黑名单域名。

## Decisions

- 顶层 blacklist 默认空列表，普通条目精确匹配、*. 条目匹配直属一级子域；复用 normalize_domain 和 match_domain，配置加载与保存时校验并归一化去重。
- 签发检查全部请求域名；续签/仅部署先检查入口域名，再检查现有解析器得到的实际主域名。检查发生在状态存储、通知器、ACME 和部署器初始化之前。
- 使用共享检查函数返回 matched_domain、reason=blacklist、message，操作状态为 skipped。UI 使用已有通知机制，自动入口将跳过记录为跳过而非完成。
- 选择公共编排入口拦截，避免在 UI 和定时任务重复业务规则。低层测试工具直接调用 ACME 不在本次入口范围。

## Risks / Trade-offs

- 通配规则只阻止直属一级子域，不包括根域或多级子域；在配置说明中明确边界。
- 入口检查不读取真实证书 SAN；签发检查明确提交的 SAN，续签按入口和实际主域名控制整张证书。
- 配置热更新沿用既有机制，操作开始后修改黑名单影响下一次操作。

## 云目标执行约束

缓存匹配后过滤黑名单，腾讯 SSL 动态扫描及实时校验后在平台分组前再次过滤。精确和通配目标均检查与黑名单的覆盖关系；同一账号、产品、资源和端口的共享目标命中时整组跳过。无独立域名的 listener_scope 目标按证书 SAN 覆盖范围保守检查。跳过项保留域名、产品、命中规则和 reason=blacklist，不调用目标部署 API。腾讯需先上传获得 CertificateId 才能发现目标，上传证书本身不会绑定黑名单产品。

无目标且无失败记录时返回 skipped/no_targets，保留跳过详情及零成功/失败计数。仅部署编排在通知及成功状态写入前返回，复用 UI 的 skipped 分支。续签后部署跳过不写部署成功冷却状态。
