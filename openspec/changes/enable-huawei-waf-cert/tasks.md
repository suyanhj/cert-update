## 1. SDK 与扫描配置

- [x] 1.1 增加华为云 WAF SDK 依赖与统一客户端构建函数
- [x] 1.2 增加 `waf_scan` 配置项并接入缓存刷新、实时匹配和聚合扫描

## 2. WAF 绑定发现

- [x] 2.1 在 `HuaweiCloudProvider` 中实现云模式 HTTPS host 分页及详情扫描
- [x] 2.2 为 WAF 扫描补充分页、过滤、企业项目和单 host 失败隔离测试

## 3. WAF 证书部署

- [x] 3.1 增加按目标集合准备不同产品证书的部署器扩展点
- [x] 3.2 在 `HuaweiDeployer` 中实现 WAF 证书上传、复用与 cloud host 绑定
- [x] 3.3 为 WAF 上传复用、ELB/WAF 双证书仓库和失败结果补充测试

## 4. 文档与验证

- [x] 4.1 更新示例配置及 Provider/Deployer 文档
- [x] 4.2 运行相关单元测试与完整测试套件并修复回归
