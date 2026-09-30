## Context

腾讯云部署流程会先把新证书上传到 SSL 服务，再按证书 ID 调用 `DescribeHost*InstanceList` 获取域名匹配的云资源，最后统一调用 `DeployCertificateInstance`。EO 已由腾讯云 SSL 提供同构接口，因此可在现有证书上下文扫描中直接接入，无需引入 EO 产品 SDK，也不需要把 EO 放入日常绑定缓存。

## Goals / Non-Goals

**Goals:**

- 允许通过独立配置开关控制腾讯云 EO 扫描。
- 使用 SSL `DescribeHostTeoInstanceList` 实时扫描与新证书匹配的 EO 域名并支持分页。
- 复用现有证书上传、部署任务轮询、错误聚合和通知链路。
- 保持现有腾讯云 CDN、云直播、CLB、COS 行为不变。

**Non-Goals:**

- 不调用 EdgeOne 产品 API 管理站点、域名或 HTTPS 配置。
- 不新增 EO 日常绑定缓存采集。
- 不改变腾讯云证书上传、重复证书复用或部署任务轮询策略。

## Decisions

1. 配置字段使用 `product_scan.tencent.eo_scan`，默认关闭，并在示例中显式开启。这样既符合用户对 EO 的称呼，也避免旧配置升级后意外扩大部署范围。
2. 内部产品类型和 SSL 资源类型统一使用 `teo`。这是腾讯云 SSL API 的正式 `ResourceType`，可减少扫描结果到部署请求间的映射分歧。
3. 扫描通过现有 `_iter_ssl_host_instances` 实现分页、`CertificateId`、`IsCache=0` 和 `domainMatch=1`。EO 返回的 `Host` 作为 `domain` 与 `product_id`；空 Host 丢弃并按域名去重。
4. 不依据 EO 的证书状态过滤非空 Host。该接口本身返回可部署且已完成证书域名匹配的实例，状态只作为当前绑定状态保留；失败或处理中目标仍应交由 SSL 部署接口给出权威结果。
5. 部署器增加 `teo` 分支，实例 ID 直接使用域名，并复用 `_deploy_certificate(cert_id, "teo", [domain])` 及现有异步任务轮询。

## Risks / Trade-offs

- [EO API 返回状态语义可能扩展] → 不对状态做枚举硬编码，只保留原值并依赖 SSL 部署结果。
- [开启 EO 扫描会扩大实际部署范围] → 新开关默认关闭，示例和文档要求显式配置。
- [旧版 SSL SDK 缺少 EO 模型或方法] → 项目依赖已采用无上限的 `tencentcloud-sdk-python-ssl>=3.0`；启动时保持直接导入和调用，版本不满足时尽早报错。

## Migration Plan

1. 发布代码后，在需要接入的环境配置 `product_scan.tencent.eo_scan: true`。
2. 先用 `deploy.mode=dry-run` 验证常规缓存目标；EO 只在 apply 上传证书后实时发现，因此生产启用前应使用受控证书执行一次 apply 验证。
3. 如需回滚，仅关闭 `eo_scan` 即可停止 EO 扫描与部署，不影响其他腾讯云产品。

## Open Questions

无。
