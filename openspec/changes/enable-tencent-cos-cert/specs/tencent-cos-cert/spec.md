## ADDED Requirements

### Requirement: COS 仅扫描已绑定证书的自定义域名

`TencentCloudProvider.get_oss_bindings()` SHALL 通过 SSL `DescribeHostCosInstanceList` 翻页读取 COS 自定义域名。仅当 `Status` 为 ENABLED，且 `Domain`、`Bucket`、`Region` 均非空时 MUST 产出绑定；`CertId` MAY 为空，表示 HTTPS 已开启但尚未关联证书。`product_type` MUST 为 `oss`，`product_id` MUST 为 `<region>:<bucket>:<domain>`，`resource_type` MUST 为 `cos`。

#### Scenario: 已绑定证书的源站域名进入绑定

- **WHEN** 接口返回 `Region=ap-guangzhou`、`Bucket=img-1250000000`、`Domain=img.example.com`、`CertId=cert-1`、`Status=ENABLED`
- **THEN** 返回一条 `product_type=oss`、`product_id=ap-guangzhou:img-1250000000:img.example.com`、`domain=img.example.com`、`cert_id=cert-1` 的绑定

#### Scenario: 未绑定证书但已上线的域名进入绑定

- **WHEN** 某条记录 `Status=ENABLED` 且 `CertId` 为空
- **THEN** 返回绑定且 `cert_id` 为空，后续部署可关联新证书

#### Scenario: 已下线的域名不进入绑定

- **WHEN** 某条记录 `Status=DISABLED`
- **THEN** 该记录不出现在绑定列表中

### Requirement: COS 通过 SSL 部署接口关联证书

`TencentDeployer.deploy()` 在 `product_type=oss` 时 SHALL 调用 `DeployCertificateInstance`：`ResourceType=cos`，`InstanceIdList=["<region>|<bucket>|<domain>"]`，SSL 客户端 Region MUST 为存储桶地域。缺少 `product_id` 三段式 MUST 失败。`DeployStatus!=1` MUST 视为失败。传入非空 `cert_id` 时 MUST NOT 再次上传。

#### Scenario: COS 部署实例格式

- **WHEN** 目标 `product_id=ap-hongkong:ssl-server-1251810746:cdn.example.com`，`cert_id=cert-1`
- **THEN** 调用 `DeployCertificateInstance`，`ResourceType=cos`，`InstanceIdList=["ap-hongkong|ssl-server-1251810746|cdn.example.com"]`，且 SSL 客户端使用 `ap-hongkong`
