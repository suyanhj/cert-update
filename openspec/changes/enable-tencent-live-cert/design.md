## Context

云直播与 CDN 类似，证书绑定在 SSL 证书服务侧统一管理。SSL API 提供 `DescribeHostLiveInstanceList` 与 `DeployCertificateInstance(ResourceType=live)`。

## Decisions

- 扫描：`Status=1` 或 `Status=-1` 且域名非空；`CertId` 可为空；`product_type=live`，`product_id=domain`。
- 下发：实例 ID 仅为域名，不带计费开关。
- 开关：`live_scan` 默认 false（其它云不受影响），腾讯云 example 配置为 true。

## Non-Goals

- 不接 VOD、WAF、TEO 等其它 SSL ResourceType。
- 不走云直播产品侧 `ModifyLiveDomainCertBindings`，统一 SSL 路径。
