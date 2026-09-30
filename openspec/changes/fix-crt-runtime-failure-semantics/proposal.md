## Why

当前续签、部署、采集和告警链路会在部分失败场景中保存错误状态、覆盖有效缓存或向用户报告成功，导致真实故障被隐藏。需要统一失败语义，并保留腾讯 CDN 的旧部署路径，避免自动化在不确定状态下继续操作。

## What Changes

- 续签成功但部署失败时保留已签发证书，记录“等待人工处理”状态并发送告警；后续自动任务不重试部署，也不重复续签。
- 腾讯云证书上下文扫描不得删除未被 SSL 扫描结果覆盖的旧 CDN `ModifyDomainConfig` 目标。
- 云产品和域名扫描失败时保留上一次有效缓存，并把失败结果传递给采集入口。
- 采集入口区分完整成功与部分失败，不再把部分失败显示为成功或更新时间。
- 告警状态只在通知成功后进入 FIRING，通知失败允许下次重试。
- acme.sh 子进程并发消费输出并设置超时，避免管道阻塞和永久挂起。
- ECS 到期时间未知时不生成“0 天到期”误报。

## Capabilities

### New Capabilities

- `runtime-failure-semantics`: 定义续签、部署、扫描、采集、告警和外部命令失败时的状态与重试边界。

### Modified Capabilities

## Impact

- `app/services/renew_flow.py`、`app/services/deploy.py`、`app/services/collector.py`
- `app/services/domain_discovery.py`、告警服务、云厂商扫描实现
- `app/utils/store.py`、`app/utils/acme_sh.py`、UI 通知
- 相关单元测试和运维文档
