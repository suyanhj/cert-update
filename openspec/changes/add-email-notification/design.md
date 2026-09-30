## Context

`DingDingNotifier` 虽保留历史类名，但当前已经负责将同一文本并行发送到钉钉与 Rocket.Chat，所有告警和证书业务通知都复用该入口。邮件应作为第三个可选通道接入此分发器，而不是在各业务服务中重复实现发送逻辑。

## Goals / Non-Goals

**Goals:**

- 使用标准 SMTP 发送 UTF-8 纯文本邮件。
- 支持 STARTTLS、SMTP over SSL 和明确配置的无加密模式。
- 支持多个 To/Cc 收件人以及可配置主题前缀。
- 支持通过环境变量注入 SMTP 密码，避免必须在 YAML 中保存明文。
- 保持现有通知格式、业务调用入口和多通道错误语义不变。

**Non-Goals:**

- 不实现 HTML 模板、附件、内嵌图片或邮件队列。
- 不接入特定厂商邮件 API。
- 不重命名历史 `DingDingNotifier` 类，避免扩大兼容性改动。

## Decisions

1. 新增独立 `app/utils/email_notify.py`，仅负责地址校验、邮件构造和同步 SMTP 发送；统一分发器通过 `asyncio.to_thread` 调用，避免阻塞事件循环。
2. 配置使用 `email` 节点：`host`、`port`、`security`、`username`、`password`/`password_env`、`from_address`、`to`、`cc`、`subject_prefix`、`timeout_seconds`、`enabled`。`password_env` 存在时优先读取环境变量，并在缺失时立即报错。
3. `security` 使用 `starttls`、`ssl`、`none` 三态，而不是多个布尔值，避免互斥配置组合。`none` 仅用于明确需要的内网 SMTP。
4. SMTP 连接与登录每封邮件建立一次并在发送后关闭，优先保证连接失效后的恢复能力；当前通知频率低，不引入长连接管理复杂度。
5. 邮件失败加入现有 `errors` 聚合；即使钉钉或 Rocket.Chat 失败，邮件仍会被尝试，反之亦然。任一启用通道失败后整体调用仍抛错，保持告警状态重试语义。
6. 三种通道均未启用表示用户主动关闭通知。通知器初始化时记录一次告警，发送入口按空操作处理，避免通知配置影响证书扫描等核心业务。

## Risks / Trade-offs

- [SMTP 服务不可达会延长通知调用] → 提供正数超时配置，发送在线程中执行并记录明确错误。
- [明文密码泄露] → 支持并推荐 `password_env`，日志不输出密码或完整目标配置。
- [错误地址导致邮件被拒收] → 初始化发送目标时严格校验 From/To/Cc，禁止换行头注入。
- [无加密模式存在凭证风险] → 默认 `starttls`，只有显式配置 `none` 才使用明文连接。

## Migration Plan

1. 在部署环境设置 SMTP 密码环境变量。
2. 添加 `email` 配置并先向测试收件人验证。
3. 启用后现有业务通知会自动增加邮件通道，无需修改业务服务。
4. 回滚时设置 `email.enabled=false` 或删除 `email` 配置。

## Open Questions

无。
