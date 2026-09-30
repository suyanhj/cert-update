## 1. Provider 发现

- [x] 1.1 重写 `app/providers/tencent.py`：DNSPod 翻页列域、解析翻页、`get_domain_list` 对 A/CNAME 探测写入 `subs`，`is_domain_provider` 只比对根域名
- [x] 1.2 CDN 用 `DescribeDomainsConfig` 翻页，只保留 online + HTTPS on + CertId；写入 `https_billing`
- [x] 1.3 CLB 只扫配置 region，按规则域名出绑定，丢弃无域名监听器；`product_id` 为 `<region>:<lb_id>:<listener_id>`

## 2. Deployer 下发

- [x] 2.1 `TencentDeployer.upload_certificate()` 调用 `UploadCertificate(Repeatable=false)`，复用 `CertificateId`/`RepeatCertId`
- [x] 2.2 `deploy()` 有 `cert_id` 则不再上传；CDN/CLB 走 `DeployCertificateInstance`，按 spec 拼 InstanceIdList

## 3. 配置与文档

- [x] 3.1 `config.example.yaml` 打开 `product_scan.tencent` 的 domain/cdn/lb，关闭 oss/ecs，并补充 tencent provider 示例
- [x] 3.2 `config.yaml` 与 `config.prod.yaml` 增加腾讯云 provider
- [x] 3.3 更新 `docs/providers.md` 与 `docs/deploys.md`

## 4. 测试

- [x] 4.1 补充 Provider 单测：域名 subs、CDN 过滤、CLB 去 VIP/去重
- [x] 4.2 补充 Deployer 单测：上传复用、CDN/CLB InstanceIdList、DeployStatus=0 失败
- [x] 4.3 运行 `python -m pytest tests/` 通过
