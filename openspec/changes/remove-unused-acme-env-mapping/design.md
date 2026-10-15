# Design

## Context

配置经 ProviderConfig 校验后导出，metadata 不会保留。见 proposal.md。

## Goals / Non-Goals

清理不可达分支；不扩展配置或更改内置凭据映射。

## Decisions

直接删除分支及专用方法，不保留注释掉的冗余代码。按字节编辑已有文件，保留编码与换行。

## Risks / Trade-offs

手工构造配置对象的调用可能使用此分支；仓库检索未发现此类调用，正常加载路径不受影响。运行现有 acme 测试验证。
