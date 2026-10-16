# Tasks

## 1. 补链工具

- [x] 1.1 实现官方 CA 目录、补链 API 和 CLI，测试链识别、去重、下载失败及 OpenSSL 验证。
- [x] 1.2 提供使用文档并验证 CLI 帮助与调用示例。

## 2. 集成验证

- [x] 2.1 使用官方 CA 和真实 OpenSSL 验证链关系，执行相关测试与 OpenSpec 严格校验。

验证记录：38 项测试通过；OpenSSL 3.5.7 验证 16 条官方 CA 路径全部通过；官方 valid.ye.test-certs.letsencrypt.org 的 YE2 叶证书完成在线补链并通过域名验证；CLI 帮助和 OpenSpec 严格校验通过。

## 3. Python 库验证

- [x] 3.1 替换 OpenSSL 命令为 cryptography 服务端验证器，更新依赖、测试、文档并完成离线和真实证书验证。

库验证记录：cryptography 50.0.2 下 44 项测试通过，包含无 PATH 可执行文件环境；官方 YE2 真实叶证书在线补链及域名验证通过。

## 4. 输出根证书

- [x] 4.1 fullchain 和 ca_pem 默认包含唯一自签 X1，更新规范和文档，通过测试及用户样本验证。

## 5. 部署入口集成

- [x] 5.1 实现部署前准备、备份原子写回和内存同步，覆盖幂等、失败不写及并发变化测试。
- [x] 5.2 接入 apply_deploy 平台分发前，验证调用一次、失败阻止上传及部署回归，更新文档。

部署集成验证：106 项测试通过，包含两个云平台及 Nginx 共用一次准备结果、原文件备份、CRLF 保留、完整链幂等、下载/私钥/域名/替换失败以及外部变更保护；OpenSpec 严格校验通过。

## 6. ZeroSSL 扩展

- [x] 6.1 增加官方 ZeroSSL 新旧 RSA/ECC 目录、DER 下载解析和按 CA 选择信任根，测试所有链和下载失败场景。
- [x] 6.2 将 ZeroSSL 接入部署预处理，验证写回幂等、多平台共享及真实官方链，更新文档并通过回归和规范校验。

ZeroSSL 验证记录：
- 官方 ZeroSSL 文档确认当前新证书使用 `ZeroSSL RSA DV SSL CA 2` / `ZeroSSL ECC DV SSL CA 2`，分别经 `Sectigo Public Server Authentication Root R46` / `E46`，再由对应 `USERTrust RSA/ECC Certification Authority` 建立信任链。
- 目录固定 8 个 DER CA，SHA-256 指纹与官方公开证书资料核对；旧 `Domain Secure Site CA` 与新 `DV SSL CA 2` 均有独立路径。
- 新增 ZeroSSL 专项测试：目录/路径回归、DER 解析、未知 ZeroSSL CA 拒绝、可选真实 TLS 端点在线补链。
- 部署入口覆盖 ZeroSSL 的写回、备份、幂等及多平台共享；dry-run 不触发补链。
- 真实在线验证使用环境变量 `ZERO_SSL_TEST_DOMAIN` 指定当前实际 ZeroSSL 站点，避免将第三方站点硬编码进测试；未设置时测试自动跳过。


CI 验证：
- GitHub Actions 已增加 `.github/workflows/certificate-chain.yml`，Python 3.12 自动执行 ZeroSSL 单元回归和官方 CA 下载集成测试。
- 2026-10-16 最新 `dev` CI run #2 成功，两个 ZeroSSL 测试步骤均通过。
