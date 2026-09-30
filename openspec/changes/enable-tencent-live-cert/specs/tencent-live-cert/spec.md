## ADDED Requirements

### Requirement: 云直播扫描 HTTPS 已开启的播放域名

`TencentCloudProvider.get_live_bindings()` SHALL 调用 SSL `DescribeHostLiveInstanceList`。`Status=1`（HTTPS 已开启）或 `Status=-1`（HTTPS 已开未关联证书）且 `Domain` 非空时 MUST 产出绑定；`CertId` 可为空。`product_type` MUST 为 `live`，`product_id` MUST 为播放域名。

#### Scenario: HTTPS 已开启且已有证书的播放域名进入绑定

- **WHEN** 接口返回 `Domain=live.example.com`、`CertId=cert-1`、`Status=1`
- **THEN** 返回一条 `product_type=live`、`product_id=live.example.com`、`cert_id=cert-1` 的绑定

#### Scenario: HTTPS 已开启但未关联证书仍进入绑定

- **WHEN** 记录 `Domain=nocert.example.com`、`CertId` 为空、`Status=1` 或 `Status=-1`
- **THEN** 返回绑定且 `cert_id` 为空，部署时由上层传入新 `cert_id`

#### Scenario: HTTPS 未开启不进入绑定

- **WHEN** 记录 `Status=0`
- **THEN** 该记录不出现在绑定列表中

### Requirement: 云直播通过 SSL 部署接口关联证书

`TencentDeployer.deploy()` 在 `product_type=live` 时 SHALL 调用 `DeployCertificateInstance`：`ResourceType=live`，`InstanceIdList=["<domain>"]`。传入非空 `cert_id` 时 MUST NOT 再次上传。

#### Scenario: 云直播部署实例格式

- **WHEN** 目标域名 `live.example.com`，`cert_id=cert-1`
- **THEN** 调用 `DeployCertificateInstance`，`ResourceType=live`，`InstanceIdList=["live.example.com"]`

### Requirement: 运行配置启用腾讯云云直播扫描

`product_scan.tencent.live_scan=true` 时 services 层 SHALL 调用 `get_live_bindings()`；为 false 时 MUST 跳过。

#### Scenario: 扫描开关

- **WHEN** 加载 `config.example.yaml` 的 `product_scan.tencent`
- **THEN** `live_scan` 为 true
