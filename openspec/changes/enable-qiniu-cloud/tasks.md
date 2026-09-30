## 1. API 与鉴权

- [x] 1.1 重构 `app/utils/qiniu_api.py`，使用官方 SDK 分别实现 Qiniu/QBox 鉴权、紧凑 JSON 请求与明确错误处理
- [x] 1.2 实现七牛 CDN 域名 marker 分页工具并防止 marker 不前进

## 2. Provider 与 Deployer

- [x] 2.1 修正 `QiniuCloudProvider` 的域名发现与 CDN 绑定扫描，复用分页工具并过滤非法/DCDN 条目
- [x] 2.2 实现 `QiniuDeployer.upload_certificate()`，使用 Fusion 证书接口并支持同账号证书 ID 复用
- [x] 2.3 修正 CDN 下发逻辑，按 HTTP/HTTPS 选择 `sslize`/`httpsconf` 并返回标准 `DeployResult`
- [x] 2.4 修正证书分页列表与删除接口，补齐关键日志和异常处理

## 3. 配置与文档

- [x] 3.1 在 `config.example.yaml` 增加七牛 provider 凭证示例并启用 CDN 与对象存储扫描
- [x] 3.2 更新 `docs/providers.md` 与 `docs/deploys.md` 的七牛云行为说明

## 4. 测试与验证

- [x] 4.1 增加 API 双鉴权、紧凑请求体、错误响应和分页保护单元测试
- [x] 4.2 增加 Provider 过滤/分页与 Deployer 上传复用/协议分流单元测试
- [x] 4.3 运行七牛相关测试、OpenSpec 校验和完整 `tests/` 回归测试

## 5. Kodo 对象存储接入

- [x] 5.1 扩展七牛 API 工具，支持 UC Qiniu 鉴权的 Bucket 与 Bucket 域名查询
- [x] 5.2 实现 `QiniuCloudProvider.get_oss_bindings()`，按 Bucket/CDN 源站关系生成互斥的 OSS 与 CDN 目标
- [x] 5.3 扩展 `QiniuDeployer` 支持 OSS 加速域名校验、证书复用与协议分流部署
- [x] 5.4 启用七牛 `oss_scan` 并更新 Provider/Deployer 文档
- [x] 5.5 增加 Kodo API、产品分类和 OSS 部署单元测试
- [x] 5.6 运行七牛专项测试、完整回归与 OpenSpec 严格校验
