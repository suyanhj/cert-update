## Why

当前证书系统只支持钉钉和 Rocket.Chat 通知，无法覆盖以邮件作为标准运维告警渠道的团队。增加 SMTP 邮件通知后，现有证书、域名、主机、签发、续签和部署结果都可以复用同一通知链路送达邮箱。

## What Changes

- 增加可选的 SMTP 邮件通知配置，支持多个收件人、抄送、主题前缀、STARTTLS、SSL 和无加密模式。
- 支持通过配置值或环境变量提供 SMTP 密码，环境变量优先。
- 将邮件通道接入现有统一通知分发器，与钉钉、Rocket.Chat 并行发送。
- 单个通知通道失败时继续尝试其他已启用通道，并沿用现有聚合错误语义。
- 补充示例配置、运维文档及自动化测试。

## Capabilities

### New Capabilities

- `email-notification`: 通过标准 SMTP 发送所有现有通知内容，并提供安全、可校验的邮件配置。

### Modified Capabilities

无。

## Impact

- 影响 `app/schemas/config.py`、`app/utils/notifier.py`，并新增独立 SMTP 发送工具。
- 使用 Python 标准库 `smtplib`、`email` 和 `ssl`，不增加第三方依赖。
- 现有未配置邮件的部署行为保持不变。
