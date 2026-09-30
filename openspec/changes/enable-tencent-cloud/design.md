## Context

`TencentCloudProvider` / `TencentDeployer` 已经注册到 factory，但运行时扫描开关关闭，且部署路径与阿里云不一致：基类 `upload_certificate()` 返回 `None`，`deploy()` 又无条件重新上传，忽略上层传入的 `cert_id`。CDN 使用 `UpdateDomainConfig` 只传部分 `Https` 字段，会把 HSTS/HTTP2 等复杂配置打回默认值。CLB 无规则时用 VIP 当域名，无法被证书匹配。

凭证字段沿用现有约定：`secret_id` / `secret_key` / `region`（默认 `ap-guangzhou`）。ACME DNS-01 已映射 `Tencent_SecretId` / `Tencent_SecretKey`。

## Goals / Non-Goals

**Goals:**

- 启用腾讯云域名发现与 CDN/CLB HTTPS 绑定扫描。
- 证书上传一次、同一账号下多目标复用 `cert_id`。
- CDN/CLB 下发走 SSL 官方部署接口，不改写 CDN 其它 HTTPS 配置。
- 配置层可直接加入腾讯云账号并开始采集。

**Non-Goals:**

- COS、CVM、EdgeOne、WAF 等其它产品。
- 多地域自动扫遍所有 CLB region（只扫 `credentials.region`）。
- 给尚未开启 HTTPS 的 CDN 域名自动打开 HTTPS。

## Decisions

- **扫描开关**：`domain_scan=true`、`cdn_scan=true`、`lb_scan=true`；`oss_scan=false`、`ecs_scan=false`。与华为云类似，不实现未接入产品。
- **域名发现**：DNSPod `DescribeDomainList` + `DescribeRecordList`。`get_domain_list` 对 ENABLE 的 A/CNAME 做 HTTP 探测后写入 `subs`，对齐阿里云，证书监控才能覆盖子域。`is_domain_provider` 只比对根域名列表，不触发探测。
- **CDN 绑定**：用 `DescribeDomainsConfig` 翻页。仅保留 `Status=online`、`Https.Switch=on` 且 `Https.CertInfo.CertId` 非空的域名。`product_id` 为域名。`metadata.https_billing` 取 `HttpsBilling.Switch`，缺省为 `on`（这些域名已开 HTTPS）。
- **CLB 绑定**：只扫配置 `region`。HTTPS 监听器按规则域名出绑定；无域名的监听器丢弃，不用 VIP。`product_id` 为 `<region>:<lb_id>:<listener_id>`。`metadata` 含 `listener_id`、`sni_switch`。同一 listener+domain 多条 URL 规则去重。
- **证书上传**：`UploadCertificate(Repeatable=false)`。优先 `CertificateId`，为空则用 `RepeatCertId`；两者都空则抛错。公开 `upload_certificate()` 用主域名拼 alias。
- **证书下发**：`deploy()` 有 `cert_id` 则不再上传。调用 `DeployCertificateInstance`：CDN 实例为 `domain|https_billing`；CLB SNI 为 `lb_id|listener_id|domain`，非 SNI 为 `lb_id|listener_id`。`Status=1`，`IsCache=0`。`DeployStatus=0`（已有部署任务在跑）视为失败并抛出，交给上层重试。
- **失败策略**：发现路径继续走 `_call_provider_api`（失败返回空列表）。部署路径内部抛错，由 `deploy()` 转成 `DeployResult(success=False)`，与阿里云一致。
- **配置**：`config.yaml` 与 `config.prod.yaml` 增加 `type: tencent` 账号，凭证与日志告警 Agent 使用的腾讯云密钥一致。

## Risks / Trade-offs

- [DNSPod 到期时间是套餐 VipEndAt，不是注册局到期] → 域名告警可能不准；保持现有字段，不另接 Domain 注册 API。
- [CLB 只扫一个 region] → 其它地域监听器不会被匹配；需要时在 credentials.region 调整。
- [DeployCertificateInstance 异步出任务] → 接口成功不代表 CDN 已切换完成；失败由下次续签/重试覆盖。
- [非 SNI 监听器多域名共用一张证书] → 多次命中会重复部署同一 listener；可接受。

## Migration Plan

1. 合并代码并打开 `product_scan.tencent`。
2. 配置写入腾讯云 provider 后跑一次采集，确认 DNSPod/CDN/CLB 绑定入缓存。
3. `deploy.mode` 保持现有值；首次真实下发前可用 dry-run 看匹配。
4. 回滚：关掉该 provider 的 `enabled` 或把 `product_scan.tencent` 扫开关回 false。

## Open Questions

无。账号展示名暂用「腾讯云」，如需改成业务线名称可只改配置。
