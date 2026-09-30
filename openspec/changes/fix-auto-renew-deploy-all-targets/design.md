## Context

Web 强制续签与自动续签已经共用 `renew_and_plan_deploy`。Web 入口传入 `provider_name=None, force=True`，由 acme.sh 根据域名映射解析 DNS provider，并在续签成功后扫描全部部署目标；自动入口当前传入证书所属 provider 且 `force=False`，导致部署服务把 provider 当作部署过滤条件。

## Goals / Non-Goals

**Goals:**

- 自动扫描命中续签阈值后采用与 Web 强制续签一致的编排参数。
- 继续由现有域名映射解析 DNS provider。
- 续签成功后匹配并部署到全部云厂商和 Nginx 目标。
- 通过单元测试防止自动入口参数再次偏离 Web 行为。

**Non-Goals:**

- 不修改 Web 强制续签现有行为。
- 不修改部署匹配、腾讯云 EO 扫描或 SSL 部署实现。
- 不增加配置项或改变续签阈值判断方式。

## Decisions

- 自动入口直接调用现有 `renew_and_plan_deploy(domain, provider_name=None, force=True)`。这是 Web 已验证可用的路径，避免增加新的编排分支。
- 保留自动扫描阶段对域名 provider 映射的校验和日志，确保仅处理已知 DNS provider 的证书；实际续签时由 acme.sh 从同一域名映射再次解析 provider。
- 不改变 `renew_and_plan_deploy` 的公共签名，以免影响手工脚本和其他调用者对 provider 过滤行为的使用。

## Risks / Trade-offs

- [风险] `force=True` 会绕过续签冷却窗口 → 自动流程只在证书剩余天数达到阈值时触发，续签后本地证书有效期会更新；测试继续覆盖每轮域名去重。
- [风险] 域名映射缺失时无法解析 DNS provider → 自动扫描保留现有的 provider 非空校验，底层也会快速报出明确配置错误。

