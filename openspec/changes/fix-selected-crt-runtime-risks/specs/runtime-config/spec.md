# Spec Delta

## Purpose

定义运行配置采用校验结果的可观察行为，确保运维人员能够获得明确且一致的运行结果，并通过失败条件和隔离场景验证系统可靠性，避免后台异常被静默忽略。

## ADDED Requirements

### Requirement: 运行配置采用校验结果
系统 SHALL 使用校验后的类型、默认值和归一化结果运行；TLS 扫描并发必须大于零。

#### Scenario: 布尔类型转换和默认值
- **WHEN** 配置 auto_renew_enabled 为字符串 false 且未设置命令超时
- **THEN** 运行值为布尔 false，命令超时为模型默认值，TLS 并发零和负数被拒绝
