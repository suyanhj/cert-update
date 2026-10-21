# Design

## Context

腾讯部署器先调用 `UploadCertificate(Repeatable=false)`，优先使用响应中的 `CertificateId`，否则使用 `RepeatCertId`，随后立即以该 ID执行 SSL Host 扫描。部署前补链会保持叶证书不变并增加缺失 CA，因此腾讯可能按相同叶证书指纹返回旧 ID，而旧 ID 的证书内容没有随本地补链更新。

现有项目已经依赖 `cryptography`，部署前也使用它解析本地 PEM 和计算 SHA-256 指纹，可以复用同一实现方式。腾讯 `DescribeCertificateDetail` 返回 `CertificatePublicKey`、`RootCert.Standard`、`CertFingerprint` 和 `CertChainInfo`，无需增加依赖。

## Goals / Non-Goals

**Goals:**

- 在证书 ID进入腾讯资源发现和部署流程前验证其远端证书链。
- 对链缺失、链内容不同及详情不可验证采用安全失败。
- 保留同一账号一次上传、多个腾讯产品目标复用一个已验证 ID 的现有结构。

**Non-Goals:**

- 不无条件重复上传，不原地修改腾讯旧证书 ID。
- 不删除腾讯云已有证书，不自动回滚已存在的部署。
- 不改变其他云厂商的证书上传和部署流程。

## Decisions

### 上传方法内部完成远端验证

`TencentDeployer._upload_certificate()` 在选择响应 ID 后立即查询 `DescribeCertificateDetail` 并完成验证，只有验证成功才返回 ID。这样所有直接或间接使用腾讯上传入口的调用方都不会拿到未经验证的 ID，且现有上层异常处理会自然阻断该账号的 SSL Host 扫描。

备选方案是在 `DeployService` 中验证，但会让上传安全依赖单一调用路径，`deploy()` 自行上传时仍可能绕过验证，因此不采用。

### 使用解析后的 SHA-256 指纹集合比较

本地对待上传 `cert_pem` 中全部证书计算 SHA-256 指纹。远端将 `CertificatePublicKey` 与可选的 `RootCert.Standard` 合并解析、按指纹去重后比较证书数量及指纹集合。忽略 PEM 换行、空白和腾讯重新序列化造成的文本差异，但不允许少证书、多证书或替换证书。

不直接比较 PEM 文本，因为相同证书的换行和编码格式可能不同；不只比较叶证书，因为本次故障正是叶证书相同而链内容不同。

### 详情查询或解析失败一律阻断

查询异常、空 `CertificatePublicKey` 和 PEM 解析失败不能证明腾讯复用了缺链 ID，因此不做重传而直接阻断。指纹集合不一致时允许进入一次受控重传；重传后查询或解析失败、指纹仍不一致则抛出明确异常。异常由现有腾讯账号上下文准备逻辑记录为失败，并移除该账号匹配项，确保不会调用 `get_ssl_deploy_bindings()` 或 `DeployCertificateInstance()`。

### 证书链不一致时只强制重传一次

首次继续使用 `Repeatable=false`，便于远端已有完整链时直接复用。若详情回查确认返回 ID 缺链或链不同，则使用相同 PEM 和私钥、`Repeatable=true` 再上传一次。强制重传返回的 ID 仍必须通过同一套证书链验证才能返回上层。

不采用始终 `Repeatable=true`，避免每次部署都创建冗余证书；不采用循环重试，避免腾讯持续返回旧 ID 时不受控地创建或请求。

### 日志只记录非敏感摘要

上传日志同时记录响应中的 `CertificateId`、`RepeatCertId` 和 `RequestId`，并根据 `RepeatCertId` 是否存在标记重复结果。链验证日志只记录证书 ID、链长度和缩短后的 SHA-256 指纹，不输出公钥 PEM 或私钥。

## Risks / Trade-offs

- [腾讯详情接口可能把根证书拆到 `RootCert.Standard`] → 合并公钥证书与根证书后按指纹去重比较。
- [腾讯返回内容存在短暂最终一致性] → 本次先采用单次详情查询并安全失败，避免在不确定状态下部署；操作者可重试。
- [新增一次 API 调用和权限要求] → 仅每个腾讯账号每次上传调用一次，开销有限；缺少详情权限时明确失败。
- [严格集合一致可能拒绝腾讯自动规范化过的额外链] → 安全性优先，错误日志会输出双方链摘要供确认后调整策略。
- [`Repeatable=true` 仍可能被腾讯强制去重] → 强制重传后继续回查，链不一致则终止，不使用未验证 ID。

## Migration Plan

1. 发布包含验证逻辑和测试的版本。
2. 先在测试域名执行仅部署，确认日志显示腾讯远端链验证成功后再用于生产域名。
3. 若详情权限不足，为运行账号增加 `DescribeCertificateDetail` 权限后重试。
4. 回滚只需恢复旧版本；此变更不修改或删除腾讯云已有证书。
