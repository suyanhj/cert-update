## ADDED Requirements

### Requirement: CDN/CLB/COS/云直播部署扫描走 SSL Host 接口

`TencentCloudProvider` 在 apply 上传证书后 SHALL 对 CDN、CLB、COS、云直播调用 SSL `DescribeHostCdnInstanceList` / `DescribeHostClbInstanceList` / `DescribeHostCosInstanceList` / `DescribeHostLiveInstanceList`。每个请求 MUST 携带待部署的 `CertificateId`、`IsCache=1` 与 `domainMatch=1`。MUST NOT 在没有证书 ID 时调用这些接口。对外绑定字段 MUST 保持兼容：CDN `product_id` 为加速域名且 `metadata` 含 `https_billing`；CLB `product_id` 为 `<region>:<lb_id>:<listener_id>`；COS `product_id` 为 `<region>:<bucket>:<domain>`；云直播 `product_id` 为播放域名。

#### Scenario: SSL 扫描请求包含证书上下文

- **WHEN** apply 已上传证书并获得 `cert-new`
- **THEN** 各个已启用产品的 Host 列表请求包含 `CertificateId=cert-new`、`IsCache=1`、`Filters=[domainMatch=1]`

#### Scenario: 日常缓存刷新不调用证书匹配接口

- **WHEN** 定时任务在没有待部署证书 ID 的情况下刷新腾讯产品绑定缓存
- **THEN** CDN、CLB、COS、云直播使用各自产品的只读查询接口，并且不调用 SSL Host 接口、不产生 `FailedOperation.InvalidParam`

#### Scenario: 云直播已开启 HTTPS 但未绑定证书

- **WHEN** 云直播产品或 SSL Host 接口返回播放域名状态 `-1`
- **THEN** 该域名仍进入绑定列表，`cert_id` 为空，并可在 apply 阶段绑定新证书

#### Scenario: 腾讯侧匹配结果直接成为部署目标

- **WHEN** SSL Host 接口以 `domainMatch=1` 返回资源
- **THEN** 本地只校验资源字段并去重，不再使用 `match_domain` 二次筛选

#### Scenario: SSL Host 扫描失败

- **WHEN** 任一已启用产品的 SSL Host 请求返回参数错误、权限错误或限流错误
- **THEN** apply 失败并发送失败通知，MUST NOT 将异常降级为空目标后报告成功

#### Scenario: CLB 非 SNI 或 TCP_SSL 监听器

- **WHEN** SSL Host 返回 `TCP_SSL` 或 `SniSwitch=0` 的监听器且没有规则域名
- **THEN** 生成监听器级 CLB 目标，部署实例 ID 不附加域名

#### Scenario: apply 没有部署目标

- **WHEN** SSL Host、其它云产品和 Nginx 均没有可部署目标
- **THEN** apply 抛出失败，手动部署或续签流程发送失败通知而不是成功通知

#### Scenario: CDN 在线证书绑定进入列表

- **WHEN** `DescribeHostCdnInstanceList` 返回域名 `cdn.example.com`，`Status=online`，`CertId=cert-1`，`HttpsBillingSwitch=off`
- **THEN** 返回一条 `product_type=cdn`、`product_id=cdn.example.com`、`cert_id=cert-1`、`metadata.https_billing=off` 的绑定

#### Scenario: CDN 在线但未绑证书仍进入列表

- **WHEN** 记录 `Status=online`、`CertId` 为空
- **THEN** 返回绑定且 `cert_id` 为空

#### Scenario: CDN 未在线不进入列表

- **WHEN** 记录 `Status` 不是 online
- **THEN** 该记录不出现在绑定列表中

### Requirement: CDN 双路径下发且兼容旧缓存

CDN 下发 MUST 由 `metadata.cdn_deploy_mode` 决定：`ssl` 走 `DeployCertificateInstance`（`InstanceIdList=<domain>|<https_billing>`，`https_billing` 缺失按 `on`）；`modify` 走 CDN `ModifyDomainConfig` 只更新 `Https.CertInfo.CertId`。未设置 `cdn_deploy_mode` 的旧缓存 MUST 默认 `modify`。新扫描绑定 MUST 写入 `cdn_deploy_mode=ssl`。

#### Scenario: 新扫描 CDN 走 SSL 部署

- **WHEN** 目标 `metadata.cdn_deploy_mode=ssl`、`https_billing=off`，`cert_id=cert-1`
- **THEN** 调用 `DeployCertificateInstance`，`ResourceType=cdn`，`InstanceIdList=["cdn.example.com|off"]`

#### Scenario: 旧缓存 CDN 走 ModifyDomainConfig

- **WHEN** CDN 目标没有 `metadata.cdn_deploy_mode`
- **THEN** 调用 `ModifyDomainConfig`，`Route=Https.CertInfo.CertId`，`Value={"update":"<cert_id>"}`

### Requirement: SSL 部署必须确认异步任务终态

`DeployCertificateInstance` 返回 `DeployStatus=1` 时，部署器 SHALL 使用返回的 `DeployRecordId` 轮询 `DescribeHostDeployRecordDetail`。仅当 `PendingTotalCount=0`、`RunningTotalCount=0`、`FailedTotalCount=0` 且成功数量覆盖全部详情时 SHALL 返回成功。失败详情或轮询超时 MUST 返回失败。

#### Scenario: 部署任务最终成功

- **WHEN** 创建任务成功，详情先返回运行中，随后全部成功
- **THEN** 部署器等待终态后返回成功

#### Scenario: 部署任务最终失败

- **WHEN** 详情终态包含失败项
- **THEN** 部署器返回失败，并在消息中包含失败详情

### Requirement: 同类部署任务冲突必须等待后重试

`DeployCertificateInstance` 返回 `DeployStatus=0` 时，部署器 MUST 将 `DeployRecordId` 视为已有运行任务，等待该任务结束后重新创建当前目标的部署任务。MUST NOT 将该返回值直接视为当前目标成功。

#### Scenario: 前一个同类任务仍在运行

- **WHEN** 当前目标创建任务返回 `DeployStatus=0` 和已有记录 ID
- **THEN** 等待该记录结束，再重试创建当前目标任务并确认其终态
