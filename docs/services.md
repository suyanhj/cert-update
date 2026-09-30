# Services 模块说明

本文档描述 `app/services/*` 的职责边界、关键入口与主流程。
各模块流程图见 `docs/README.md` 的「文档与流程图索引」，总览见 `docs/flow-runtime.md` 与 `docs/flow-overview.svg`。

## 1. 范围与边界

- 负责业务编排：调度、采集、续签流程、部署流程、配置热更新。
- 不直接实现云厂商 SDK 细节（下沉到 `providers` / `deploys`）。

## 2. 关键入口

- 周期采集入口：`collector.collect_all()`
- 续签+部署编排入口：`renew_flow.renew_and_plan_deploy()`
- 配置热更新入口：`config_runtime.apply_config_text()`

## 3. 调度与快照

- 调度器通过 `start_scheduler(interval, run_immediately=False)` 定期调用 `collect_all()`；异常会记录日志并跳过当前轮次。
- 启动时可调用 `hydrate_state_from_snapshot()` 恢复上次快照，避免首页冷启动为空。
- 仅当整轮采集完整成功后调用 `persist_state_snapshot()` 并更新 `state.LAST_UPDATED`。

## 4. collect_all 主流程

- 顺序：collect_domains → collect_ecs_instances → refresh_product_bindings_cache → collect_certificates → persist_state_snapshot。
- 每步独立 try/except，单步失败后继续执行剩余步骤并收集错误。
- 任一步失败时整轮返回聚合异常，不更新 `state.LAST_UPDATED`，UI 不显示成功。

## 5. 域名发现（Domains）

- 入口：`Domains.discovery_all()`；按 `product_scan.<cloud>.domain_scan` 过滤云厂商。
- Cloudflare 仅参与 Zone/DNS 域名发现；关闭 `product_scan.cloudflare.domain_scan` 后不会请求其 Zone API。
- 发现顺序：先 static，再 cloud；每个 provider 调用 `get_domain_list()`。
- Provider 通过 `domain_roles` 声明注册信息或 DNS 托管能力。七牛 CDN 产品域名不参与根域名融合。
- `DomainDiscoveryService` 按规范化根域名融合：到期时间、剩余天数和注册主体来自注册角色；Zone、`subs` 和签发账号来自 DNS 角色。
- 同一根域名出现多个 DNS 来源时，依次按 `domain_dns_overrides` 人工覆盖、公网权威 NS 与 Provider 分配 NS 精确匹配、平台强证据选择；只有一个候选时直接使用。
- 公网 NS 查询成功但没有候选精确匹配时，不使用平台强证据覆盖公网结果；无法确认时保留注册信息，但清空 `dns_name`、不写 `DomainProviderStore`，页面签发按钮保持禁用。
- 公网 NS 查询失败或返回空时，唯一的 Cloudflare `active + full` Zone 可作为强证据回退；全部 Provider 成功后才写入 `DomainProviderStore.save(...)`。
- `DomainProviderStore` 只保存 `dns_name`，确保跨平台域名签发使用 DNS 托管账号而不是注册商账号。
- 域名到期告警使用 `registrar_name`；只有 DNS 数据且注册信息未知时跳过到期告警。
- 状态写入：`collect_domains` 更新 `state.DOMAIN_GROUPS`，并 clear/set `state.DOMAIN_READY`。

**流程图**：[flow-domains.svg](flow-domains.svg)

## 6. ECS 主机发现（ECS）

- 入口：`Ecs.discovery_all()`；按 `product_scan.<cloud>.ecs_scan` 过滤云厂商。
- 采集：调用 `provider.get_ecs_instances()` 并补全 `name/provider/provider_name/days`。
- 状态写入：`collect_ecs_instances` 更新 `state.ECS_LIST` 并触发 ECS 告警聚合发送。

**流程图**：[flow-ecs.svg](flow-ecs.svg)

## 7. 证书探测（Certificates）

- 入口：`Certificates.get_certificate_list(domains)`。
- 域名集合：从 `state.DOMAIN_GROUPS` 提取待探测域名，支持子域与扩展映射。
- 探测与解析：使用 `tls_probe.probe_domains_batch`，补全 `provider` 与剩余天数。
- 告警与恢复：按 `cert_warn_days/cert_expiry_days` 触发告警；通知发送成功后才提交 FIRING，发送失败时下一轮重试。
- 自动续签编排：完成后触发 `_auto_renew_and_deploy_expiring_certificates`（受开关控制）。

**流程图**：[flow-certs.svg](flow-certs.svg)

## 8. 签发、续签与部署流程（Issue + Renew + RenewFlow）

- `issue_certificate(domain, sans, provider_name=None)`：通过同一 Provider 的 acme.sh DNS 插件首次签发，不自动增加 SAN。
- `issue_and_plan_deploy(domain, domains, provider_name=None)`：显式接收用户确认的域名集合；签发只尝试一次，不读写续签冷却状态，成功后复用现有 dry-run/apply 部署流程。
- `renew_certificate(domain, provider_name=None, force=False)`：单域续签，domain 为空抛 `ValueError`。
- `renew_and_plan_deploy(...)`：只对续签失败按配置重试；续签成功但部署失败时保存 `deploy_failed` 并等待人工处理，后续自动任务不重试部署也不重复续签。
- `deploy_existing_only(...)`：不续签，只加载已有证书后走 dry-run 或 apply；成功/失败都会发送部署通知（标题为「证书部署成功/失败」，正文格式与续签成功通知一致）。
- 与采集链路关系：`collect_certificates` 完成后会对 `days <= auto_renew_days` 的证书按域名去重触发。

### 8.1 DNS-01 首次签发

- 托管域名组行“签发”默认填写域名组主域名与 `*.主域名`；普通子域行使用所点主机名的直接父域，例如 `oss.test.gzyys26.com` 默认填写 `test.gzyys26.com` 与 `*.test.gzyys26.com`。点击 `*.test.gzyys26.com` 等通配记录时直接使用去掉 `*.` 后的域名，不再扩大到上一层。弹窗仍可增加域名或移除通配域名。
- acme.sh 以弹窗第一项作为主域名。例如从 `www.test.hj.com` 所属的 `test.hj.com` 域名组发起时，默认执行 `-d test.hj.com -d *.test.hj.com`，证书主文件名保持 `test.hj.com`。
- 所有签发域名必须由同一个 Provider 托管；跨 Provider 或缺少域名映射时在执行 acme.sh 前失败。
- DNS 插件映射：阿里云 `dns_ali`、腾讯云 `dns_tencent`、Cloudflare `dns_cf`。华为云官方插件使用 IAM 用户名/密码/账号域，与项目现有 AK/SK 配置不兼容，本次不启用。
- 本机 acme.sh 已存在同名 `Main_Domain` 时拒绝首次签发，不执行 `--issue`，应到证书池使用“强制续签”。父级通配证书或其他证书 SAN 能覆盖目标子域时，仍允许建立独立子域证书；续签/部署查找已有证书时继续使用覆盖范围匹配。TXT 记录的创建与清理由 acme.sh 插件负责。

### 8.2 acme.sh 证书路径模式

- `acme.certificate_path_mode=auto`（默认）：依次成对检查 `Le_Real*`、`Le_FullchainPath/Le_KeyPath`、`DOMAIN_CONF` 所在默认目录。
- `acme.certificate_path_mode=default`：只读取默认目录中的 `fullchain.cer` 与 `<Le_Domain>.key`。
- `acme.certificate_path_mode=custom`：只读取 `Le_RealFullChainPath` 与 `Le_RealKeyPath`，缺失时立即失败。
- 证书与私钥始终从同一来源成对选择，避免不同路径来源混用。部署过程只读取源文件，不会重命名或修改 acme.sh 管理的文件。
- Nginx 目标文件名由 `cert_layout` 决定。路径模板支持 `{resolved_cert_name}`（命中规则的 `cert_name_template` 渲染结果）、`{main_domain}`（证书主域名）和 `{cert_name}`（规则实际命中域名，去除 `*.`）。例如 `cert_name_template: '{main_domain}'` 配合 `{base_dir}/{resolved_cert_name}.pem` 和 `{base_dir}/{resolved_cert_name}.key`，会把源 `fullchain.cer` 以主域名 PEM/KEY 文件名写入目标主机。云平台部署仍直接上传 PEM 内容。

**流程图**：[flow-renew.svg](flow-renew.svg)、[flow-renew-flow.svg](flow-renew-flow.svg)

## 9. 产品绑定扫描与缓存（Bindings）

- 入口：`BindingCacheService.refresh_binding_cache()`（由 `collector.refresh_product_bindings_cache` 调用）。
- 职责：按 `product_scan.<cloud>.cdn_scan/lb_scan/oss_scan` 调用各云 Provider 的 `get_cdn_bindings/get_lb_bindings/get_oss_bindings`，并将极简绑定信息写入 `ProductBindingStore` 供部署流程匹配使用。
- 不做任何证书部署，只负责“扫描 + 落盘”，部署阶段仅从缓存读取绑定。
- 查询异常与成功空结果严格区分；单个 provider 失败时保留其上次有效缓存，并向采集入口报告失败。

## 10. 部署编排入口

- 由 `DeployService.plan_dry_run()` 与 `DeployService.apply_deploy()` 承接签发或续签后的部署流程。
- 行为约定：
  - 当 `deploy.binding_cache.enabled=true` 时，**只从 `ProductBindingStore` 读取绑定，不主动调云厂商扫描接口**；若缓存缺失或过期，则视为当前 provider 无绑定。
  - 当 `deploy.binding_cache.enabled=false` 时，`DeployService` 会直接调用 Provider 的绑定发现接口做一次性匹配，但不会落盘（主要用于测试或显式禁用缓存场景）。
- 具体匹配策略、按 provider/group 归并规则与失败校验详见 `docs/deploys.md`。

## 11. 配置热更新

- `config_runtime.parse_config_text()` 解析 YAML 文本并格式化错误。
- `config_runtime.apply_config_dict()` 加锁后依次执行校验、原子写盘、reload、重启调度器。
- 配置解析与规则详见 `docs/config-entry.md` 与 `docs/config-rules.md`。

## 12. 关键开关与配置点

- 自动续签：`auto_renew_enabled` + `auto_renew_days`。
- 续签冷却与重试：`deploy.renew_cooldown_days`、`deploy.max_renew_retries`。
- 产品扫描：`product_scan.<cloud>.domain_scan/ecs_scan/cdn_scan/lb_scan/oss_scan`。
- Cloudflare 凭证：`credentials.api_token` 必填，`credentials.account_id` 可选；签发/续签环境映射为 `CF_Token` / `CF_Account_ID`。
- DNS 冲突人工覆盖：`domain_dns_overrides.<根域名>=<providers[].name>`；人工覆盖优先级最高，且配置账号必须是该域名的唯一 DNS 候选，否则采集明确失败。
- 部署模式：`deploy.mode`。

## 13. 失败策略与日志

- 采集主流程会执行完剩余步骤，但任一步失败都会返回聚合异常且不更新时间。
- 续签失败按配置重试；部署失败只告警并等待人工处理。
- `acme.command_timeout_seconds` 限制单次 acme.sh 命令执行时间，避免签发或续签永久阻塞。

## 14. 新增服务模块接入清单

1. 明确边界：仅做编排，不直接耦合厂商 SDK。
2. 返回结构稳定：便于 UI 与测试复用。
3. 先补测试，再接入 UI/调度入口。
4. 同步更新 `docs/README.md` 的“当前实现状态”。

## 15. 关联文档

- 运行流程：`docs/flow-runtime.md`
- Provider 职责：`docs/providers.md`
- Deployer 职责：`docs/deploys.md`
- 配置语义：`docs/config-rules.md`
- 文档与流程图索引：`docs/README.md`
