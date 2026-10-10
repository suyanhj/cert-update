# Proposal

## Why

仅修复用户指定的到期校验、配置运行值、账号失败隔离、TLS 单级通配、TLS 并发校验、配置切换生命周期和状态文件损坏恢复问题。

## What Changes

- 部署及预演前拒绝已过期证书，仅检查到期时间。
- 运行配置使用模型校验结果和默认值；TLS 并发必须大于零。
- 单个账号构造部署器、上传失败记入该账号目标结果，并继续其他账号和 Nginx。
- TLS 域名匹配复用已有单层通配实现。
- 配置保存先等待自动采集完成，再切换配置；停止调度唤醒等待但不取消执行中的任务。
- 损坏 JSON 先备份再恢复为空状态，备份失败提前报错。

## Capabilities

### New Capabilities

- `certificate-deployment-validation`: 部署前到期校验。
- `runtime-config`: 校验后的配置运行值和 TLS 并发约束。
- `account-deployment-isolation`: 多账号失败隔离和完整结果。
- `scheduler-config-transition`: 等待在途自动任务后切换配置。
- `state-store-recovery`: 损坏 JSON 的备份与恢复。

### Modified Capabilities

- `domain-matching`: TLS 探测也复用已有单级通配语义。

## Impact

仅修改相关配置、部署、TLS、调度、状态存储和回归测试，不改其他审计项，不新增依赖，不操作生产资源。
