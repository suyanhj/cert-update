## Context

`DomainDiscoveryService` 当前把每个 Provider 的 `get_domain_list()` 结果直接追加，并用同一组 `name/provider/expires_at/org/subs` 字段同时表示注册商与 DNS 托管商。同一个根域名同时出现在阿里云注册域名列表和 Cloudflare Zone 列表时会形成重复行；后写入 `DomainProviderStore` 的 Provider 决定签发凭证，但 Cloudflare 行没有注册到期信息。

阿里云与腾讯云当前能提供注册元数据；阿里云、腾讯 DNSPod、华为云、火山引擎和 Cloudflare 能提供 DNS Zone。七牛云的 `get_domain_list()` 实际返回 CDN 加速域名，不应参与根域名注册/DNS 聚合。

## Goals / Non-Goals

**Goals:**

- 明确区分 Provider 的 `registration` 与 `dns` 域名角色。
- 按规范化根域名生成唯一融合记录。
- 保持页面、快照和告警需要的扁平字段，同时增加明确的注册商与 DNS Provider 字段。
- 确保证书签发只使用 DNS Provider。
- 支持只有注册信息或只有 DNS 信息的域名。

**Non-Goals:**

- 不调用 WHOIS/RDAP 补全信息。
- 不在本次新增华为云域名注册 API；当前华为云仅作为 DNS 数据源。
- 不通过写入临时 TXT 记录探测权威平台；只执行只读公网 NS 查询。
- 不把 CDN/对象存储绑定域名当作根域名注册或 DNS Zone。

## Decisions

1. 在 Provider 基类增加显式 `domain_roles` 能力集合。阿里云、腾讯云和静态 Provider 标记 `registration + dns`；Cloudflare、华为云、火山引擎标记 `dns`；七牛云保持空集合。单条结果可用同名字段缩小默认角色，例如腾讯注册 API 存在但 DNSPod 已移除的域名只标记 `registration`。相比根据 `expires_at`、`zone_id` 等字段猜测角色，显式能力不会因 API 缺字段而误判。
2. 保留现有 `get_domain_list()` 调用，先在融合层拆分角色，避免同时重写所有云 API。融合输出增加 `registrar_name/registrar_provider` 与 `dns_name/dns_provider`，并为现有消费者继续提供 `expires_at/days/org/subs/name/provider` 扁平字段。
3. 同一根域名的注册字段来自最后一个具有有效注册元数据的注册角色 Provider。DNS 多候选依次按人工 `domain_dns_overrides`、公网 NS 与平台分配 NS 精确匹配、平台强证据选择；只有一个候选时直接使用。仍无法确认时不选择 DNS Provider，也不写签发映射。
4. `name/provider` 兼容字段明确指向 DNS Provider；注册信息使用 `registrar_*` 字段。`DomainProviderStore.save()` 只读取 `dns_name`，不再从模糊的 `name` 推断。
5. 域名告警以 `registrar_name` 作为账号显示，以注册数据的 `expires_at/days` 判定；只有 DNS 数据时 `expires_at=unknown` 并跳过到期告警。
6. 页面分别显示注册商和 DNS 托管商，签发弹窗显式传 `dns_name`。注册主体字段继续兼容 `org`，同时新增语义清晰的 `registrant_org`。
7. 使用 `dnspython` 查询根域名 NS，设置短超时并捕获解析异常。Cloudflare Provider 保留 Zone API 返回的 `name_servers`；`status=active` 且 `type=full` 标记为平台强证据，用于公网 NS 查询失败时的安全回退。

## Risks / Trade-offs

- [多个 DNS 平台仍保留同名 Zone] → 优先使用人工覆盖或公网 NS 匹配；无法确认时不写 DNS 映射并禁用页面签发。
- [运行环境无法查询公网 NS] → 使用 Cloudflare `active + full` 等平台强证据；仍无证据时要求配置人工覆盖。
- [注册商 API 暂时失败] → 保留 DNS 行并显示注册信息未知，不影响 DNS 扫描和首次签发。
- [DNS API 暂时失败] → 保留注册信息但不写入 DNS Provider 映射，签发在缺少映射时明确失败。
- [旧快照缺少新字段] → UI 与存储读取时回退现有字段；下一轮采集会生成完整新结构。
- [七牛 CDN 域名从域名栏目消失] → 七牛绑定继续由产品扫描展示和部署，不再错误混入根域名栏目。

## Migration Plan

1. 发布后执行一次完整采集，生成按根域名融合的新快照和 DNS Provider 映射。
2. 检查跨平台域名显示的注册商、DNS 托管商、到期时间与注册主体。
3. 使用跨平台域名执行 dry-run 签发，确认选择 DNS 托管账号。
4. 回滚时恢复旧的直接追加逻辑；运行时快照可由旧版本下一次采集覆盖。

## Open Questions

无。
