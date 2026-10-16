# Design

## Context

参见 proposal.md。项目已有 cryptography/httpx，证书工具位于 app/utils。官方资料：https://letsencrypt.org/certificates/。

## Goals / Non-Goals

提供 API、CLI 和部署前统一预处理；仅自动补全正式 Let's Encrypt / ZeroSSL 证书，不改变其他 CA 的原部署行为，不处理吊销查询。

## Decisions

- 使用官方证书目录及固定 SHA-256 指纹，按叶证书 issuer 的 CN 和组织选择对应自签根的规范路径；不从输入 AIA 任意下载。
- YE 经 Root YE、X2 交叉签名到 X1，YR 经 Root YR 到 X1，R10–R14 直接到 X1，E5–E9 经 X2 到 X1。
- 复用输入中指纹匹配的证书，只下载缺失项。不同交叉签名证书不能按名称合并。多叶证书或损坏 PEM 拒绝处理。
- 使用对应官方自签根构造 Store，输出链末尾包含该根证书，验证时仍将根作为独立信任锚，PolicyBuilder.build_server_verifier 使用默认 WebPKI 策略校验签名、有效期、CA 约束和服务端用途。指定 hostname 时验证目标；省略时取首个 DNS/IP SAN（通配符展开为单标签测试名），仅验证证书本身。验证成功才返回或创建输出文件，CLI 拒绝覆盖已有文件。
- 目录与验证库在模块导入时加载，缺依赖立即报错。HTTP 有超时、大小限制、禁止重定向；只访问固定官方 HTTPS 地址。

## Risks / Trade-offs

- 官方轮换 CA → 指纹失败明确报错，人工审核更新目录。
- 云厂商还有算法、私钥匹配等约束 → 此工具只保证证书链验证，不承诺平台接受。
- WebPKI 策略比旧命令更严格 → 使用标准扩展的测试证书和官方真实叶证书验证。

## Deployment Integration

- apply_deploy 在任何云查询、上传及 Nginx 操作前，通过线程执行一次预处理；dry-run 不写文件。所有平台共享替换后的 RenewedCert。
- 基于已解析的 issuer 筛选 Let's Encrypt、ZeroSSL 和 ZeroSSL GmbH；重新读取实际 fullchain_path，核对加载快照、私钥和域名，补链失败终止部署。
- 完整链按指纹及顺序相同时不写文件、不下载、不备份。变化时保留原编码换行，先备份原文件，再同目录临时文件原子替换；保留权限和所有者。
- 只更新实际选定的 fullchain 文件，不另改独立叶证书、CA 文件及私钥；内存 cert_pem/fullchain_pem/ca_pem 同步。
- 进程内串行预处理避免多个部署同时更新；更新前复查源内容，检测手动续签等外部变动时中止。

## ZeroSSL

旧版 RSA/ECC Domain Secure Site CA 直接补到对应 USERTrust 自签根；新版 RSA/ECC DV SSL CA 2 经 Sectigo Public Server Authentication Root R46/E46 的 USERTrust 交叉签名版补到同类 USERTrust 根。新增独立 zerossl_ca.json，启动时合并目录；从 secure.sectigo.com 官方 HTTPS 下载 DER，按固定指纹验证。未知 ZeroSSL CA 必须报错，不降级跳过。
