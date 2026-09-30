## ADDED Requirements

### Requirement: SMTP 邮件配置
系统 SHALL 提供可选 SMTP 邮件配置，并 MUST 在启用时校验服务器、端口、发件人、至少一个主收件人、安全模式及超时。

#### Scenario: 未配置邮件
- **WHEN** 配置中不存在 `email` 或 `email.enabled=false`
- **THEN** 系统不初始化邮件通道且现有通知通道行为不变

#### Scenario: 无主收件人
- **WHEN** 邮件通知启用但 `to` 为空
- **THEN** 配置校验失败并给出明确错误

### Requirement: 安全获取 SMTP 密码
系统 SHALL 支持从 YAML 配置或环境变量读取 SMTP 密码，并 MUST 在同时配置时优先使用环境变量。

#### Scenario: 使用环境变量密码
- **WHEN** 配置了 `password_env` 且对应环境变量存在
- **THEN** SMTP 登录使用环境变量值而不是 YAML 中的 `password`

#### Scenario: 密码环境变量缺失
- **WHEN** 配置了 `password_env` 但对应环境变量不存在
- **THEN** 通知器初始化失败且错误中标明缺失的环境变量名

### Requirement: 发送 UTF-8 纯文本邮件
系统 SHALL 将现有通知标题作为邮件主题、通知正文作为 UTF-8 纯文本内容，并发送到所有 To/Cc 收件人。

#### Scenario: STARTTLS 认证发送
- **WHEN** 邮件安全模式为 `starttls` 且配置了用户名和密码
- **THEN** 系统建立 SMTP 连接、执行 EHLO 和 STARTTLS、登录并发送邮件

#### Scenario: SSL 发送
- **WHEN** 邮件安全模式为 `ssl`
- **THEN** 系统通过 SMTP over SSL 建立连接并发送邮件

#### Scenario: 无认证发送
- **WHEN** 未配置 SMTP 用户名
- **THEN** 系统不执行登录并直接发送邮件

### Requirement: 多通道通知分发
系统 SHALL 将每条通知发送到所有已启用通道，且单个通道失败 MUST NOT 阻止后续通道发送。

#### Scenario: 钉钉失败但邮件成功
- **WHEN** 钉钉发送抛出异常且邮件通道已启用
- **THEN** 系统仍尝试发送邮件，并在全部通道尝试完成后报告聚合发送失败

#### Scenario: 仅启用邮件
- **WHEN** 钉钉和 Rocket.Chat 均未启用但邮件已启用
- **THEN** 系统正常发送邮件且不报告“未配置任何通知通道”

#### Scenario: 未启用任何通知通道
- **WHEN** 钉钉、Rocket.Chat 和邮件均未启用
- **THEN** 系统记录通知被跳过的提示且不抛出异常，不影响证书扫描等业务流程
