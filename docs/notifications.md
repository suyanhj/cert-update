# 通知模块

通知统一由 `app.utils.notifier.DingDingNotifier` 分发。该类名为历史兼容名称，当前可同时启用钉钉、Rocket.Chat 和 SMTP 邮件；证书、域名、ECS、签发、续签与部署流程不需要分别配置通知实现。

## 1. 多通道行为

- 每条通知会依次尝试所有已启用通道。
- 钉钉、Rocket.Chat 和邮件可任选一种或同时启用多种，分别通过各自的 `enabled` 控制。
- 一个通道失败不会阻止后续通道发送。
- 所有通道尝试结束后，只要存在失败就抛出错误，供告警状态机在后续轮次重试。
- 未启用任何通道表示关闭通知：初始化时记录一次告警，后续发送直接跳过，不影响证书扫描等业务流程。

## 2. SMTP 邮件配置

```yaml
email:
  enabled: true
  host: smtp.example.com
  port: 587
  security: starttls
  username: monitor@example.com
  password_env: CRT_SMTP_PASSWORD
  from_address: "CRT 监控 <monitor@example.com>"
  to:
    - ops@example.com
  cc:
    - owner@example.com
  subject_prefix: "[CRT] "
  timeout_seconds: 10
```

字段说明：

- `security`：`starttls`（默认）、`ssl` 或 `none`。`none` 仅建议用于可信内网 SMTP。
- `username`：为空时不执行 SMTP 登录。
- `password_env`：SMTP 密码环境变量名；存在时优先于 `password`。推荐使用此方式，避免密码写入 YAML。
- `from_address`：发件人，可使用 `显示名 <address@example.com>`。
- `to`：至少一个主收件人；字符串和列表都可用。
- `cc`：可选抄送列表。
- `subject_prefix`：主题前缀，默认 `[CRT] `。
- `timeout_seconds`：连接与发送超时，必须大于 0。

启用认证且使用环境变量时，需在启动服务前设置密码：

```bash
export CRT_SMTP_PASSWORD='replace-me'
```

Windows 服务环境请在服务账户的环境变量中设置同名值。若配置了 `password_env` 但环境变量不存在，程序会在初始化通知器时立即报错。

## 3. 邮件内容

- 邮件主题为 `subject_prefix + 通知标题`。
- 邮件正文为 UTF-8 纯文本，内容与其他通知通道一致。
- 当前不支持 HTML、附件或邮件队列。
