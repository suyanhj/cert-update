## 1. 云直播绑定扫描

- [x] 1.1 `TencentCloudProvider.get_live_bindings()` 调用 `DescribeHostLiveInstanceList`
- [x] 1.2 保留 `Status=1`/`-1` 且域名非空；`CertId` 可为空

## 2. 云直播证书下发

- [x] 2.1 `TencentDeployer.deploy()` 支持 `product_type=live`
- [x] 2.2 `DeployCertificateInstance` 使用 `ResourceType=live`，`InstanceIdList=[domain]`

## 3. 配置、文档、测试

- [x] 3.1 新增 `product_scan.live_scan`，example 对 tencent 开启
- [x] 3.2 更新 `docs/providers.md`、`docs/deploys.md`
- [x] 3.3 补充扫描过滤与部署单测
