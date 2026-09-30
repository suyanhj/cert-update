## Context

证书页面当前将普通续签、强制续签和仅部署并列展示。普通续签要求目标证书已存在于本机 acme.sh，且还会受续签冷却约束，因此不能解决“云端已有证书，但本机尚未接管”的首次签发问题。域名扫描已经把域名到 Provider 名称写入 `DomainProviderStore`，Provider 配置也已经具备 acme.sh DNS 插件所需凭证。

acme.sh 的 DNS API 模式通过 `--issue --dns <plugin> -d <domain>...` 创建和清理 TXT 记录。现有代码已经实现凭证环境构建、命令超时、`--info` 解析、证书路径模式和签发后多云部署所需的 `RenewedCert`，签发应复用这些能力。

## Goals / Non-Goals

**Goals:**

- 从托管域名列表打开可编辑弹窗，对用户确认的域名集合执行一次 DNS-01 首次签发。
- 根据域名扫描映射选择 Provider，支持 `dns_ali`、`dns_tencent` 和 `dns_cf`。
- 签发前完成输入去重、同 Provider 校验和已有 acme.sh 证书冲突检查。
- 签发后按当前 `deploy.mode` 执行 dry-run 或 apply，并提供明确页面与通知结果。
- 保持自动续签和强制续签现有行为不变。

**Non-Goals:**

- 不实现手工 DNS 模式、HTTP/Webroot 签发或跨 Provider SAN 的混合验证。
- 不在服务端暗自增加 SAN；页面仅根据用户点击位置提供直接父域及其通配域名作为默认值，用户可编辑后确认。
- 不直接调用云厂商 DNS 写接口；TXT 生命周期完全交给 acme.sh DNS 插件。
- 不用“签发”覆盖本机已经由 acme.sh 管理的证书；此场景继续使用“强制续签”。

## Decisions

1. 页面“签发”位于托管域名列表。域名组行使用托管根域名作为证书主域名；普通子域行使用所点主机名的直接父域作为默认主域名，但不得越出所属托管根域名。例如 `oss.test.example.com` 默认生成 `test.example.com` 与 `*.test.example.com`。若所点记录本身是通配域名，则直接使用去掉 `*.` 后的域名作为主域名，避免再次向上扩大证书范围。用户可以增加域名或删除通配域名；服务端接收并再次归一化、去重。
2. `AcmeShRenewer.issue()` 与 `renew()` 并列，复用 Provider 解析、凭证环境、命令执行、`--info` 和证书路径解析。`BaseAcmeRenewer` 同步增加抽象方法，避免把签发伪装成一种续签。
3. 签发域名集合以主域名为首项，SAN 归一化并去重；所有域名剥离通配符前缀后必须解析到同一 Provider。若跨 Provider，签发在调用 acme.sh 前失败，避免只写入部分 DNS Zone。
4. DNS 插件由 Provider 类型固定映射：阿里云 `dns_ali`、腾讯云 `dns_tencent`、Cloudflare `dns_cf`。acme.sh 最新 `dns_huaweicloud` 要求 IAM 用户名、密码和账号域，项目现有华为 Provider 使用 AK/SK，因此本次将华为视为不支持，避免生成必然失败的命令。不支持的 Provider 明确报错，不回退到手工 DNS。
5. 首次签发前执行 acme.sh `--list`，只在列表中存在与目标完全相同的 `Main_Domain` 时拒绝 `--issue` 并提示使用强制续签。父级通配证书、其他证书的 SAN 虽可在 TLS 层覆盖目标域名，但不是目标子域的 acme.sh 独立证书，不得阻止签发。续签和仅部署仍保留按 SAN/通配覆盖范围查找现有证书的逻辑。
6. 新增独立 `issue_and_plan_deploy()` 编排，不读取或写入续签冷却状态。签发只尝试一次，降低重复创建 ACME Order 和触发 CA 限流的风险；成功后复用 `DeployService`。
7. 签发使用独立的 `issue_status` 和“证书签发成功/失败”通知，不复用 `renew_status` 文案。部署失败返回 `deploy_failed`，但不会污染续签状态。

## Risks / Trade-offs

- [证书 SAN 跨多个 DNS Provider] → 签发前逐项校验并拒绝，后续如需混合验证再单独设计。
- [用户输入重复或格式不规范] → 服务端再次归一化、去重，且拒绝以通配域名作为首个主域名。
- [页面数据包含空域名、伪后缀或其他 Zone 主机] → 默认范围计算执行点分隔后缀校验并立即拒绝，不退回托管根域名猜测。
- [`certificate_path_mode=custom` 但首次签发没有 Le_Real 路径] → 保持现有路径模式的严格语义并返回明确错误；首次接管建议使用默认 `auto`。
- [签发成功但部署失败] → 返回独立 `deploy_failed`，保留已由 acme.sh 管理的证书，用户可使用“部署”重试。
- [Provider 不支持 acme.sh DNS 插件] → 在运行命令前失败，不尝试手工 DNS 或不受控兼容路径。
- [父级通配证书覆盖待签子域] → 首次签发仅比较 `Main_Domain`，允许建立独立子域证书；同名 `Main_Domain` 仍必须走强制续签。

## Migration Plan

1. 部署代码后刷新域名与证书快照，确保 `DomainProviderStore` 包含目标域名。
2. 在托管域名列表点击“签发”，编辑并确认域名集合后由 acme.sh 完成 DNS-01。
3. 已在本机 acme.sh 管理的证书继续使用“强制续签”；自动续签不变。
4. 回滚时从托管域名列表移除签发入口；已经签发的证书仍由 acme.sh 保存，不会被删除。

## Open Questions

无。
