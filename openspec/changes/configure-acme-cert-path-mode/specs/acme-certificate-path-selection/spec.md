## ADDED Requirements

### Requirement: 证书路径模式配置
系统 SHALL 在 `acme.certificate_path_mode` 接受 `auto`、`default`、`custom`，未配置时 SHALL 使用 `auto`，其他值 MUST 在配置校验阶段失败。

#### Scenario: 未配置路径模式
- **WHEN** 配置未包含 `acme.certificate_path_mode`
- **THEN** 系统使用 `auto` 模式

#### Scenario: 配置非法路径模式
- **WHEN** `acme.certificate_path_mode` 不是三个允许值之一
- **THEN** 配置校验失败且不进入续签或部署流程

### Requirement: 自动模式成对选择路径
`auto` 模式 SHALL 依次尝试 `Le_RealFullChainPath + Le_RealKeyPath`、`Le_FullchainPath + Le_KeyPath`、从 `DOMAIN_CONF + Le_Domain` 推导的默认路径；系统 MUST 仅采用字段完整且两个文件均存在的同一候选对。

#### Scenario: 自定义路径完整可用
- **WHEN** `Le_RealFullChainPath` 与 `Le_RealKeyPath` 均存在且文件可读
- **THEN** `auto` 使用该自定义路径对

#### Scenario: 自定义路径残缺
- **WHEN** 自定义路径字段或文件只有一项可用且下一候选对完整可用
- **THEN** `auto` 整体跳过自定义路径对并使用下一候选对

#### Scenario: 所有路径对不可用
- **WHEN** 三组候选都不完整或文件不存在
- **THEN** 系统抛出包含模式和候选来源的证书路径错误

### Requirement: 固定默认目录模式
`default` 模式 SHALL 只根据 `DOMAIN_CONF` 父目录和 `Le_Domain` 使用 `fullchain.cer` 与 `<Le_Domain>.key`，MUST NOT 使用任何 `Le_Real*` 或 `Le_*Path` 记录作为回退。

#### Scenario: 默认目录路径可用
- **WHEN** `DOMAIN_CONF`、`Le_Domain` 和推导出的两个文件均存在
- **THEN** 系统读取默认目录中的完整链和私钥

#### Scenario: 默认目录信息缺失
- **WHEN** 推导字段或任一默认文件缺失
- **THEN** 系统立即报告 `default` 模式路径错误

### Requirement: 固定自定义路径模式
`custom` 模式 SHALL 只使用 `Le_RealFullChainPath` 与 `Le_RealKeyPath`，MUST NOT 回退到 acme.sh 记录路径或默认目录。

#### Scenario: 自定义部署路径可用
- **WHEN** 两个 `Le_Real*` 字段和文件均存在
- **THEN** 系统读取该完整链与私钥

#### Scenario: 自定义部署路径缺失
- **WHEN** 任一 `Le_Real*` 字段或文件缺失
- **THEN** 系统立即报告 `custom` 模式路径错误

### Requirement: 目标主机使用主域名文件名
系统 SHALL 在 Nginx SSH 目标路径模板使用 `{main_domain}` 时，把选中的完整链内容写为 `{main_domain}.pem`，把私钥内容写为 `{main_domain}.key`，且 MUST NOT 修改 acme.sh 源文件。

#### Scenario: 默认目录证书部署到目标主机
- **WHEN** 来源文件为 `fullchain.cer` 且目标模板为 `{base_dir}/{main_domain}.pem` 和 `{base_dir}/{main_domain}.key`
- **THEN** 目标主机收到对应 PEM/KEY 文件名，acme.sh 源文件名称和内容保持不变

#### Scenario: 云平台部署证书
- **WHEN** 同一证书部署到云平台
- **THEN** 系统继续传递证书和私钥 PEM 内容且不依赖本地或目标文件名
