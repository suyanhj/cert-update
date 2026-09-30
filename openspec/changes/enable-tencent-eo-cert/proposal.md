## Why

当前腾讯云证书部署已通过 SSL 服务扫描 CDN、云直播、CLB 和 COS 可部署目标，但未扫描 EdgeOne（EO）域名，导致 EO 上的证书无法随统一续签流程自动更新。

## What Changes

- 为腾讯云产品扫描配置新增独立的 EO 扫描开关。
- 在证书上传腾讯云 SSL 后，通过 `DescribeHostTeoInstanceList` 扫描与证书域名匹配的 EO 实例。
- 将 EO 扫描结果纳入现有部署匹配，并继续通过 SSL `DeployCertificateInstance` 下发证书。
- 补充 EO 扫描、部署、配置和回归测试，以及相关配置与能力文档。

## Capabilities

### New Capabilities

- `tencent-eo-cert`: 定义腾讯云 EdgeOne 证书目标的扫描、匹配和通过 SSL 服务部署的行为。

### Modified Capabilities

无。

## Impact

- 受影响代码：腾讯云 provider/deployer、部署编排和产品扫描配置模型。
- 受影响配置：`product_scan.tencent` 新增 EO 扫描开关。
- 外部 API：腾讯云 SSL `DescribeHostTeoInstanceList` 与 `DeployCertificateInstance(ResourceType=teo)`。
- 依赖：继续复用现有 `tencentcloud-sdk-python-ssl`，不新增 EdgeOne SDK 依赖。
