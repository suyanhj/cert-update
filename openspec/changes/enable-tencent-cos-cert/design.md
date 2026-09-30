## Context

上一轮 `enable-tencent-cloud` 明确不接 COS。CDN 用 `ModifyDomainConfig` 改 `Https.CertInfo.CertId`，CLB 用 SSL `DeployCertificateInstance`。COS 自定义源站域名的证书绑定走 SSL 证书服务，不走 COS PutBucketDomain。

## Goals / Non-Goals

**Goals:**

- 扫描 HTTPS 已启用的 COS 自定义域名，写入与阿里云 OSS 同结构的绑定（`product_type=oss`），允许尚未关联证书。
- 部署时把已上传的 SSL `cert_id` 关联到 `Region|Bucket|Domain`。

**Non-Goals:**

- 不给 HTTPS 尚未启用的自定义域名首次开通 HTTPS。
- 不接 COS 对象读写 SDK，不扫无证书的 CNAME。
- 不改 CDN 下发路径。

## Decisions

1. **扫描用 `DescribeHostCosInstanceList`。**  
   返回 `Domain` / `CertId` / `Status` / `Bucket` / `Region`。全量扫描使用 `IsCache=1`，不传 `CertificateId`。只保留 `Status=ENABLED` 且域名、桶、地域都非空的项，`CertId` 可为空。

2. **`product_type` 用 `oss`。**  
   与 `oss_scan` / `get_oss_bindings` / 阿里云对象存储一致。`product_id` 为 `<region>:<bucket>:<domain>`。`resource_type` 为 `cos`。

3. **下发用 `DeployCertificateInstance`。**  
   `ResourceType=cos`，`InstanceIdList=["ap-guangzhou|bucket-1250000000|cdn.example.com"]`。COS 与 CLB 一样必须带 Region，SSL 客户端按桶地域构建。`DeployStatus=1` 后轮询部署记录到终态；`DeployStatus=0` 等待已有任务结束后重试。

4. **失败策略与现有腾讯路径一致。**  
   扫描走 `_call_provider_api`（失败返回空列表）；部署异常由 `deploy()` 转成 `DeployResult(success=False)`。

## Risks / Trade-offs

- `DescribeHostCosInstanceList` 不传 CertificateId 时，若账号 COS 域名很多，需翻页；Limit=100。
- 部署任务异步，必须以 `DescribeHostDeployRecordDetail` 的最终结果为准。
