## 1. SSL 扫描

- [x] 1.1 CDN 用 `DescribeHostCdnInstanceList`，绑定字段与旧 CDN 扫描兼容
- [x] 1.2 CLB 用 `DescribeHostClbInstanceList`，保留多地域与 `product_id` 格式
- [x] 1.3 COS 继续用 `DescribeHostCosInstanceList`，不扩其它产品

## 2. SSL 下发

- [x] 2.1 CDN 改为 `DeployCertificateInstance`，`InstanceIdList=<domain>|<https_billing>`
- [x] 2.2 旧缓存无 `cdn_deploy_mode` 时仍走 ModifyDomainConfig；CLB/COS 路径不变

## 3. 测试与文档

- [x] 3.1 更新扫描/下发单测，覆盖 CDN 缺 `https_billing` 的旧缓存
- [x] 3.2 更新 `docs/providers.md`、`docs/deploys.md`

## 4. 异步部署可靠性修复

- [x] 4.1 `DeployCertificateInstance` 创建任务后轮询 `DescribeHostDeployRecordDetail` 到终态
- [x] 4.2 同证书同资源类型已有运行任务时等待结束并重试创建，避免多目标冲突
- [x] 4.3 严格 Fake SSL 参数和异步状态，覆盖成功、失败、超时、任务冲突与顺序部署
- [x] 4.4 手动部署通知失败不得覆盖原部署异常；同步 COS/OpenSpec 与当前扫描要求

## 5. 证书上下文扫描修复

- [x] 5.1 SSL Host 请求必须携带 `CertificateId` 与 `domainMatch=1`，禁止无证书上下文扫描
- [x] 5.2 日常缓存刷新使用 CDN、CLB、COS、云直播产品原生查询接口生成绑定关系
- [x] 5.3 apply 上传证书后由腾讯 SSL `domainMatch` 直接选出 CDN/COS/CLB/云直播目标，不再依赖本地域名匹配
- [x] 5.4 SSL 扫描失败必须中止 apply；补齐 TCP_SSL、非 SNI CLB 与零目标通知语义
- [x] 5.5 增加产品查询、请求参数、部署发现、去重及旧 CDN 路径回归测试，并做只读真实账号验证
