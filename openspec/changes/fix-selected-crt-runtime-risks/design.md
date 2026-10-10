# Design

## Context

见 proposal.md。复用已有 Pydantic、TimeUtil 和 domain_match，不新增依赖。

## Goals / Non-Goals

完成用户指定项；不处理告警升级、信任链、扫描异常等未指定项。

## Decisions

- 配置通过 AppConfig.model_dump(mode="json") 转换后封装 Box，保持原配置文件不展开默认值。
- 在部署服务入口检查到期时间，允许续签器读取过期旧证书用于续签。
- 账号失败写入各命中目标失败记录，继续其他账号；腾讯证书上下文发现也按账号捕获异常。
- TLS 复用 match_domain，仅改变通配语义，不改变 CN、信任链逻辑。
- 调度停止用事件唤醒等待，执行中的 collect_all 完成后退出；配置切换先停止再写入，失败时恢复旧调度。
- JSONDecodeError 使用唯一文件名复制备份，成功后恢复为空；业务数据结构错误仍提前报错。

## Risks / Trade-offs

- 配置保存等待在途自动任务，耗时受已有云调用与 ACME 超时约束；记录等待日志。
- 到期检查仅拒绝过期，未执行私钥匹配或信任链验证。
- 损坏状态恢复为空可能重新触发采集或操作，保留备份及日志供排查。
