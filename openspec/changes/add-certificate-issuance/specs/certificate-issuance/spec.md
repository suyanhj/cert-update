## ADDED Requirements

### Requirement: 从托管域名列表编辑签发域名集合
系统 SHALL 在托管域名列表提供签发入口。域名组行 SHALL 默认填入托管主域名与 `*.主域名`；子域行 SHALL 默认填入所点主机名的直接父域与 `*.直接父域`，且默认主域名 MUST NOT 越出所属托管根域名。用户 MUST 能增加域名或移除通配域名；系统 MUST 对确认后的域名归一化、去重，并以首个非通配域名作为 acme.sh 主域名。

#### Scenario: 默认签发主域名和通配域名
- **WHEN** 用户在 `example.com` 托管域名组点击签发
- **THEN** 系统使用 `example.com` 和 `*.example.com` 作为签发域名集合

#### Scenario: 用户移除通配域名
- **WHEN** 用户从弹窗删除 `*.example.com` 后确认
- **THEN** 系统只签发用户确认的域名，不重新增加通配域名

#### Scenario: 从子域名行发起签发
- **WHEN** 用户在 `www.test.hj.com` 子域名行点击签发，而所属托管域名组为 `test.hj.com`
- **THEN** 弹窗默认首项为 `test.hj.com`，签发后的 acme.sh 主证书文件名以 `test.hj.com` 为准

#### Scenario: 从多级子域名行发起签发
- **WHEN** 用户在 `oss.test.gzyys26.com` 子域名行点击签发，而所属托管域名组为 `gzyys26.com`
- **THEN** 弹窗默认填写 `test.gzyys26.com` 与 `*.test.gzyys26.com`，DNS Provider 仍由所属托管域名映射解析

#### Scenario: 从通配子域记录发起签发
- **WHEN** 用户在 `*.test.gzyys26.com` 子域名行点击签发，而所属托管域名组为 `gzyys26.com`
- **THEN** 弹窗默认填写 `test.gzyys26.com` 与 `*.test.gzyys26.com`，不再向上退到 `gzyys26.com`

#### Scenario: 所选主机不属于托管域名
- **WHEN** 页面数据中的所选主机不是托管根域名本身或其点分隔子域
- **THEN** 系统拒绝生成默认签发范围，不使用字符串伪后缀或其他 Zone

### Requirement: 通过域名 Provider 选择 DNS 插件
系统 SHALL 从 `DomainProviderStore` 解析签发域名的 Provider，并 SHALL 将阿里云、腾讯云和 Cloudflare 分别映射到 `dns_ali`、`dns_tencent` 和 `dns_cf`。系统 MUST 在执行 acme.sh 前拒绝不支持的 Provider。

#### Scenario: Cloudflare 域名签发
- **WHEN** 目标域名映射到 Cloudflare Provider
- **THEN** 系统使用 `CF_Token` 凭证环境执行 `acme.sh --issue --dns dns_cf`

#### Scenario: Provider 不支持 DNS 签发
- **WHEN** 目标域名映射到没有签发插件映射的 Provider
- **THEN** 系统返回明确错误且不执行 acme.sh 签发命令

### Requirement: 签发域名必须属于同一 Provider
系统 MUST 验证主域名及所有 SAN 均解析到同一 Provider；任一域名缺少映射或映射到其他 Provider 时 MUST 在写入 DNS 前失败。

#### Scenario: SAN 跨 Provider
- **WHEN** 主域名由 Cloudflare 托管但某个 SAN 映射到其他 Provider
- **THEN** 系统拒绝签发并指出 Provider 不一致

### Requirement: 首次签发不得覆盖已有 acme.sh 证书
系统 MUST 在签发前查询 acme.sh 证书列表；仅当已有条目的 `Main_Domain` 与所选主域名完全相同时，系统 MUST 拒绝首次签发并提示使用强制续签。父级通配证书或其他条目的 SAN 覆盖所选主域名时 MUST NOT 阻止建立独立子域证书。

#### Scenario: 本机已有同主域证书
- **WHEN** acme.sh 已管理 `Main_Domain` 与目标主域名相同的证书
- **THEN** 系统不执行 `--issue`，并提示用户使用强制续签

#### Scenario: 父级通配证书已覆盖子域
- **WHEN** acme.sh 已有 `gzyys26.com + *.gzyys26.com`，但列表中没有 `Main_Domain=test.gzyys26.com`
- **THEN** 系统允许签发 `test.gzyys26.com + *.test.gzyys26.com`

#### Scenario: 目标仅是其他证书的 SAN
- **WHEN** 目标主域名出现在其他 `Main_Domain` 条目的 SAN 中，但不存在同名 `Main_Domain`
- **THEN** 系统允许为目标主域名建立独立证书

#### Scenario: 本机没有同主域证书
- **WHEN** acme.sh 列表中没有同名 `Main_Domain`
- **THEN** 系统执行一次 DNS-01 签发

### Requirement: 签发命令和证书加载
系统 SHALL 为每个签发域名增加一个 `-d` 参数，SHALL 使用选定的 DNS 插件，并 SHALL 按配置追加 `--dnssleep`。命令成功后系统 MUST 通过 `--info` 和现有证书路径规则构造 `RenewedCert`。

#### Scenario: 多域名签发
- **WHEN** 主域名和两个 SAN 通过校验
- **THEN** acme.sh 命令包含三个去重后的 `-d` 参数，并在成功后加载完整证书链和私钥

### Requirement: 签发后执行现有部署流程
系统 SHALL 在签发成功后按当前 `deploy.mode` 调用现有 dry-run 或 apply 部署流程，并 SHALL 返回独立的 `issue_status`。签发失败时 MUST NOT 进入部署。

#### Scenario: 签发并部署成功
- **WHEN** acme.sh 签发成功且部署流程成功
- **THEN** 系统返回 `issue_status=success` 并发送证书签发成功通知

#### Scenario: 签发失败
- **WHEN** acme.sh 签发命令失败
- **THEN** 系统返回 `issue_status=failed`、保留错误原因并发送签发失败通知

#### Scenario: 签发成功但部署失败
- **WHEN** 证书已签发但部署流程抛出异常
- **THEN** 系统返回 `issue_status=deploy_failed`，不写入续签冷却状态，并允许用户之后使用仅部署重试

### Requirement: 证书池移除普通续签并由域名列表提供签发
证书池 SHALL 仅显示“强制续签”和“部署”；托管域名列表 SHALL 显示“签发”并打开可编辑域名弹窗。

#### Scenario: 用户点击签发
- **WHEN** 用户点击托管域名行的“签发”按钮并确认弹窗
- **THEN** 页面使用用户确认的域名集合触发首次签发并展示签发或部署结果，不经过普通续签冷却判断

#### Scenario: 用户点击强制续签
- **WHEN** 用户点击“强制续签”按钮
- **THEN** 页面继续调用现有 `force=true` 续签与部署流程
