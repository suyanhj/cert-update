# Proposal

## Why

上传云平台的证书可能缺少 Let's Encrypt / ZeroSSL 中间 CA 或交叉签名证书，导致验证失败。需要独立的补链工具识别缺失环节并在输出前验证。

## What Changes

- 在 utils 增加可复用 Python API 和命令行工具，识别 YE1–YE3、YR1–YR3、R10–R14、E5–E9，以及 ZeroSSL 新旧版 RSA/ECC DV 链。
- 从官方 HTTPS 下载缺失 CA，按证书指纹去重，构建到各自官方自签根的完整链。
- 使用 cryptography 服务端证书验证器验证，失败明确报错；提供缺失 CA 和验证结果。

## Capabilities

### New Capabilities
- `certificate-chain-completion`: Let's Encrypt / ZeroSSL 证书链补全、去重与验证。

### Modified Capabilities

无。

## Impact

增加 utils 工具、测试和使用说明，复用 cryptography/httpx。cryptography 最低版本提升至 50.0.2，无需 OpenSSL 命令；接入 apply_deploy 统一入口，在分发前补链、验证并备份写回实际 fullchain 文件。
