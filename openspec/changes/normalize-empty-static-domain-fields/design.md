# Design

## Context

静态 Provider 根据 expires_at 决定是否解析日期，unknown 已有明确跳过逻辑。

## Goals / Non-Goals

在配置模型中统一处理空值；不修改采集器及已填字段的业务含义。

## Decisions

复用 Pydantic 前置字段校验器，None、空字符串和纯空白返回 unknown，registrant_org 的缺省值改为 unknown。有效非空值保持原样。

静态 Provider 初始化时也使用同一个 StaticDomainConfig 模型校验收到的域名条目，采集统一消费校验后的条目，避免未经归一化的配置对象把空字符串传入日期解析。初始化日志确认此校验路径已执行；不缓存日期解析结果。

## Risks / Trade-offs

未知注册主体展示为 unknown；与用户要求一致。以 Provider 实际采集测试验证空日期不再进入日期解析。
