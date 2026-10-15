# Design

## Context

见 proposal.md。官方 renew() 从 DOMAIN_CONF 加载 Le_Webroot，并传给 issue()，命令行 --dns 不覆盖该字段。用户确认采用官方 --issue --dns --force 切换插件。

## Goals / Non-Goals

让后续自动及手动续签使用当前已识别的 DNS Provider；不手工修改 acme.sh 文件，不扩大证书域名范围。

## Decisions

- 保持当前证书策略、显式账号及发现映射的 Provider 选择优先级，插件和凭据来自同一账号。
- 使用 --info 读取原主域名、Le_Alt 和 Le_Webroot，单一当前插件覆盖保存列表的每项时保持 --renew。混合验证方式也需要重新签发并验证各 SAN 当前账号一致。
- 插件不一致时构造 --issue --dns <plugin> --force，使用原主域名和所有 SAN、Le_Keylength（空值按官方旧 RSA 2048 语义）、Le_API，并传回已有安装路径、hooks、alias、preferred-chain、有效期参数和证书 profile。
- 缺少保存的域名、SAN 字段或验证方式时报错，防止迁移时丢失域名或选择错误 CA。
- 最后按既有 --info 路径解析结果，不缓存迁移决定，重试重新读取保存参数。

## Risks / Trade-offs

- --issue --force 将实际重新签发，可能受到 CA 限额影响；仅插件变化时使用，命令日志明确区分。
- acme.sh 自己更新保存配置；失败后下一次调用重新读取该状态。
- 多个 SAN 已分属不同账号时，拒绝整次迁移，不用单一账号尝试全部 DNS。

## Migration Plan

更新程序后重新采集当前 DNS 归属，再使用既有续签入口。迁移成功后 acme.sh 保存新插件，后续回到 --renew。
