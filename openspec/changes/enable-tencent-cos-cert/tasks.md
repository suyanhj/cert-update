## 1. COS 绑定扫描

- [x] 1.1 `TencentCloudProvider` 增加 SSL 客户端，实现 `get_oss_bindings()` 翻页调用 `DescribeHostCosInstanceList`
- [x] 1.2 仅保留 ENABLED 且 CertId/域名/桶/地域非空的绑定；`product_id=<region>:<bucket>:<domain>`

## 2. COS 证书下发

- [x] 2.1 `TencentDeployer.deploy()` 支持 `product_type=oss`
- [x] 2.2 `_deploy_certificate` 对 `cos` 使用桶地域 SSL 客户端；InstanceIdList 为 `Region|Bucket|Domain`

## 3. 配置、文档、测试

- [x] 3.1 `config.example.yaml` 保持 `product_scan.tencent.oss_scan=true`，单测对齐实际文件
- [x] 3.2 更新 `docs/providers.md`、`docs/deploys.md`
- [x] 3.3 补充扫描过滤与部署 InstanceIdList / Region 单测
