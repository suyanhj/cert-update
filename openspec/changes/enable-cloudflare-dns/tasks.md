## 1. 依赖与配置

- [x] 1.1 增加官方 Cloudflare Python SDK 5.x 依赖
- [x] 1.2 在配置模型中注册 cloudflare Provider 和 DNS-only 产品扫描默认值
- [x] 1.3 在示例配置增加 API Token、可选 Account ID 和扫描开关说明

## 2. Cloudflare Provider

- [x] 2.1 新增 Cloudflare Provider 并在初始化时校验 Token、创建官方 SDK 客户端
- [x] 2.2 实现 active Zone 的完整分页发现和账号过滤
- [x] 2.3 实现 DNS 记录完整分页、字段归一化和 A/AAAA/CNAME 子域探测
- [x] 2.4 将 Cloudflare Provider 注册到导出、工厂和域名服务

## 3. ACME 与文档

- [x] 3.1 增加 CF_Token 与可选 CF_Account_ID 的 acme.sh 环境变量映射
- [x] 3.2 更新 Provider 和服务文档，说明只读范围与 Zone Read + DNS Edit 权限

## 4. 测试与验证

- [x] 4.1 增加配置、客户端初始化、Zone/DNS 分页与过滤单元测试
- [x] 4.2 增加工厂注册、扫描开关和 acme.sh 凭证映射测试
- [x] 4.3 运行 Cloudflare 专项测试、完整回归、OpenSpec 严格校验与差异检查
