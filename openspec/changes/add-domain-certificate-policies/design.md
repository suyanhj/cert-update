# Design

## Context

见 proposal.md。现有仅部署依赖 acme.sh --list/--info，证书池来自线上 TLS 探测；自动编排已有 automatic 标志和黑名单入口。

## Goals / Non-Goals

统一配置所有域名的证书来源及自动续签开关，手动文件复用现有部署服务。此次不增加文件同步、自动部署手动证书或新的 DNS 插件。

## Decisions

- 顶层 certificate_policies 以域名为键，字段 source、auto_renew、dns_provider、fullchain_path、key_path；不在 static_domains 中再维护第二套策略。
- 精确域名优先，其次 RFC 单级通配；普通域名不隐式继承到子域。默认 acme/auto_renew=true。manual 默认关闭自动续签，拒绝显式开启及 DNS 账号字段。
- DNS 账号引用选择已有启用的 aliyun/tencent/cloudflare Provider；在签发 SAN 校验及凭据选择时生效，部署目标不使用 DNS 账号作为筛选条件。
- 自动续签对请求域名及解析后的 Main_Domain 分别检查策略。共享证书要整体禁用自动续签时，应给 Main_Domain 配置开关。
- 手动来源在编排层直接读取路径，绝不实例化 acme 客户端。使用 cryptography 验证完整链 PEM、叶证书时间、SAN 覆盖及公私钥匹配，返回现有 RenewedCert。
- 证书池保留线上探测信息，增加来源/自动状态；手动配置未被探测的域名也显示占位行和仅部署入口，避免首次部署无入口。
- UI 手动来源隐藏签发/续签，后端也拦截。黑名单优先级最高。配置中心继续使用 YAML 编辑及现有热更新。

## Risks / Trade-offs

- 多个域名共享 acme 证书时，只关闭某个探测域名不能阻止其他域名触发同一证书续签 → 文档说明按 Main_Domain 关闭，解析后再次检查。
- 手动同步过程中证书与私钥可能不同步 → 每次部署重新读取并校验，不缓存文件内容。
- 路径位于运行进程所在文件系统 → 文档说明容器需要挂载对应目录。

## Migration Plan

旧配置无需修改。按域名逐步添加策略；删除策略恢复默认 acme 行为。保留现有编码与换行，不删除或重建已有文件。
