# Spec Delta

## Purpose

定义损坏 JSON 备份后恢复的可观察行为，确保运维人员能够获得明确且一致的运行结果，并通过失败条件和隔离场景验证系统可靠性，避免后台异常被静默忽略。

## ADDED Requirements

### Requirement: 损坏 JSON 备份后恢复
系统 SHALL 对损坏或截断的状态 JSON 先创建保留原字节的备份，再使用空状态继续启动；备份失败必须报错。

#### Scenario: 截断状态文件
- **WHEN** 状态 JSON 内容无法解析
- **THEN** 保存损坏文件副本并恢复空状态，后续写入可正常工作
