# Let's Encrypt / ZeroSSL 证书链补全

在 crt 项目目录运行，需要 requirements.txt 中的 cryptography>=50.0.2 和 httpx，无需安装 OpenSSL 命令。

```sh
python -m app.utils.certificate_chain cert.pem fullchain.fixed.pem --hostname example.com
```

输入可以是叶证书，也可以是不完整、重复或乱序的 PEM 链。输出必须是新文件，验证失败不会创建输出，不修改原文件。域名参数可省略；省略时仍检查链签名、有效期和 TLS 服务端用途。

```python
from app.utils.certificate_chain import complete_certificate_chain

result = complete_certificate_chain(pem_text, hostname="example.com")
fullchain_pem = result.fullchain_pem
print(result.missing_ca)
print(result.verify_output)
```

API 抛出 `CertificateChainError` 表示输入、下载或验证失败。可传 `timeout` 控制 HTTP 网络操作超时。

支持的路径（最左端之前均为叶证书）：

| 签发 CA | 补全路径 |
| --- | --- |
| YE1 / YE2 / YE3 | YE → Root YE（X2 交叉签名）→ X2（X1 交叉签名）→ X1 |
| YR1 / YR2 / YR3 | YR → Root YR（X1 交叉签名）→ X1 |
| R10–R14 | R → X1 |
| E5–E9 | E → X2（X1 交叉签名）→ X1 |
| ZeroSSL RSA Domain Secure Site CA | ZeroSSL RSA Domain Secure Site CA → USERTrust RSA Certification Authority |
| ZeroSSL ECC Domain Secure Site CA | ZeroSSL ECC Domain Secure Site CA → USERTrust ECC Certification Authority |
| ZeroSSL RSA DV SSL CA 2 | ZeroSSL RSA DV SSL CA 2 → Sectigo Public Server Authentication Root R46 → USERTrust RSA Certification Authority |
| ZeroSSL ECC DV SSL CA 2 | ZeroSSL ECC DV SSL CA 2 → Sectigo Public Server Authentication Root E46 → USERTrust ECC Certification Authority |

Let's Encrypt 路径依据 [Let's Encrypt 官方 CA 目录](https://letsencrypt.org/certificates/)；ZeroSSL 路径依据 [ZeroSSL 官方链说明](https://help.zerossl.com/hc/en-us/articles/360060198034-Legacy-Client-Compatibility-Cross-Signed-Root-Certificates) 及 Sectigo 官方公开根层级说明。目录和 SHA-256 指纹核对日期为 2026-10-16。ZeroSSL 新证书当前使用 RSA/ECC `DV SSL CA 2`，分别经 R46/E46 再到 USERTrust 同类自签根；旧证书仍可能使用 `Domain Secure Site CA`。只有缺失的规范路径证书才会下载；仅访问目录内固定官方 HTTPS 地址，不跟随重定向。官方更换证书后需审核并更新对应 CA JSON 的 URL 和 SHA-256 指纹。

`fullchain_pem` 包含叶证书及所需中间/交叉签名证书，末尾包含自签 X1；`ca_pem` 包含中间链和自签 X1，`root_pem` 是用于验证的 X1。`chain_names` 和 `missing_ca` 包括验证根，因此缺失 X1 也会记录并下载，并放到输出链末尾。`duplicates_removed` 统计同指纹重复项，`unused_certificates` 统计未采用的其他 CA。不同签名版本不会按名称错误合并。

验证使用 [cryptography.x509.verification](https://cryptography.io/en/50.0.0/x509/verification/) 的 `PolicyBuilder`、`Store` 和服务端验证器，检查签名、有效期、服务端用途及 WebPKI 扩展约束。仅信任已固定指纹的官方 X1。全程在内存中验证，不启动子进程、不创建验证临时文件。

指定 `--hostname` 时支持具体 DNS 名称或 IP 地址，拒绝空值和通配符目标。未指定时，以证书首个 DNS/IP SAN 作为验证对象；通配符 SAN 展开为单标签测试名称。这只证明证书链有效，不代表匹配某个外部部署域名。无 DNS/IP SAN 的证书拒绝处理。新验证器使用 WebPKI 默认策略，比旧命令的扩展检查更严格。

工具提供独立 API/CLI，并已接入 DeployService.apply_deploy 的平台分发之前。独立工具不检查私钥；部署入口会额外检查本地及已加载私钥匹配。均不检查吊销状态，也不能解决云平台不支持特定算法或信任根的问题。非 Let's Encrypt 链、未知签发 CA、多叶证书、含私钥或其他文本的输入会明确报错。

测试（测试中生成临时 CA，实际调用 cryptography 服务端验证器，不需要网络）：

```sh
python -m pytest tests/test_certificate_chain.py -q
```

验证不依赖系统 PATH 或外部命令。

## 部署前自动处理

项目自动续签后部署、手动 CLI 续签后的“仅部署”、手动证书来源，均通过正式部署入口处理一次。只读 dry-run 不下载补链或写回。自动处理针对已识别的正式 Let's Encrypt / ZeroSSL 证书，其他 CA 保留原部署行为。

写回的是本轮路径选择实际得到的 `RenewedCert.fullchain_path`（手动来源即配置的 fullchain_path），跟随符号链接到实际文件；不会另猜默认目录，也不会修改独立叶证书、独立 CA 文件或私钥文件。

链不完整时，验证成功后生成同目录 `原文件名.chain-随机标识.bak`，再原子替换原文件，保留换行、权限及所有者。链已完整时不下载、不备份、不写回。备份保留供人工恢复。

同一轮各云平台及 Nginx 共享更新后的内存证书，避免写回后仍上传旧链；以后再次部署读取完整链，只做验证，不再次更新文件。手动续签覆盖文件后，下一轮部署会重新检测并补齐。补链、私钥校验或写回失败时终止上传；发现源文件与加载快照不符或验证期间被外部更新时，报错要求重新加载，不覆盖外部修改。


## ZeroSSL 真实证书回归

仓库包含一组 2026-09-04 签发的 ZeroSSL ECC DV SSL CA 2 证书样本，以及对应的用户现场三证书不完整链样本。后者包含叶证书、ZeroSSL ECC DV SSL CA 2 和 USERTrust ECC 自签根，实际缺失的是 `Sectigo Public Server Authentication Root E46` 交叉签名证书。

运行：

```sh
python -m pytest -q tests/test_zerossl_chain.py
```

其中 `integration` 用例才会访问 Sectigo 官方 HTTPS CA 下载地址；默认 portable 测试不会访问公网。ZeroSSL 官方文档明确说明 ECC DV SSL CA 2 经 E46，并可通过 USERTrust ECC 根建立兼容链。