# Proposal

## Why

自动续签绕过状态保护，首次域名采集失败可能阻塞调度，部署跳过或预演可能被记录为成功；镜像标签方向错误可能发布旧版本。修复用户批准的审查项 5、6、7、8。

## What Changes

- 自动续签保留阈值强制执行及全云部署，但遵守冷却和部署失败保护，并按实际证书去重。
- 域名采集失败时跳过依赖阶段，保留独立采集和下一轮重试。
- 认证或实时校验失败必须反映为失败，保留目标结果；预演不记录部署成功。
- 修正新版本镜像向 latest 的标签方向。

## Capabilities

### New Capabilities

- `renewal-deployment-safety`: 自动续签状态保护、采集依赖失败与部署结果语义。
- `image-release`: 新构建镜像版本及 latest 标签发布。

### Modified Capabilities

无。

## Impact

影响 collector、renew_flow、DeployService、UI 结果提示、通知标题、build.sh 和相关测试。无新增依赖，不修改审查项 1～4、9 及并发存储与 SSH 实现。
