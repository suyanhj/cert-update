## Context

`QiniuCloudProvider`、`QiniuDeployer` 和 factory 注册已经存在，但目前共用 `Auth.token_of_request()` 与 `QBox` 头访问所有接口，且证书地址写成 `api.qiniu.com/sslcert`。七牛云当前 CDN OpenAPI 明确采用双域名、双鉴权：域名管理/配置属于 `api.qiniu.com`，要求 Qiniu 请求内容签名；证书管理属于 `fusion.qiniuapi.com`，要求 QBox 路径签名。现有实现还使用错误的 `/v2/domains` 地址、未翻页、每目标重复上传证书，并向 `DeployResult` 传入不存在的 `metadata` 参数。

项目已经依赖 `qiniu>=7.12.0` 和 `requests`。官方 SDK 提供 `Auth`、`QiniuMacAuth` 与 `QiniuMacRequestsAuth`，可复用签名实现，无需自行实现 HMAC 算法。

## Goals / Non-Goals

**Goals:**

- 让七牛云 CDN、Kodo 对象存储加速域名发现、证书上传与证书下发可在现有多云流程中真实工作。
- 严格按请求域名选择 Qiniu 或 QBox 鉴权，并保证参与签名的 JSON 字节与实际发送内容一致。
- 同一七牛账号一次上传证书，向多个匹配目标复用同一个 `cert_id`。
- 对分页结果、非法响应、HTTP/API 错误提供可定位的异常和关键日志。

**Non-Goals:**

- 七牛云 DNS、负载均衡、DCDN、对象存储原生源站域名证书下发或证书自动清理。
- 修改证书域名匹配规则或部署编排框架。
- 在仓库中写入真实 AK/SK。

## Decisions

- **鉴权封装**：`build_qiniu_auth()` 返回同时持有 `Auth` 和 `QiniuMacRequestsAuth` 的认证对象。`qiniu_request()` 根据 URL 主机严格路由鉴权，未知主机直接失败。相比继续使用单一 `Auth`，该方案符合官方双鉴权规范；相比自行实现签名，直接复用官方 SDK 可减少签名格式与时间戳错误。
- **请求体发送**：POST/PUT JSON 使用紧凑 UTF-8 字节串，并把同一字节串交给 `requests`。这样 Qiniu 内容签名与线上发送内容字节一致，不使用可能再次序列化的 `json=` 参数。
- **分页发现**：域名列表使用 `GET /domain?limit=1000` 并跟随 `marker` 直到为空，同时检测 marker 不前进，避免异常响应导致死循环。Provider 的域名发现与 CDN 绑定复用同一分页函数。
- **Kodo 扫描**：使用 `uc.qiniuapi.com/buckets` 列举 Bucket，再通过公开的 `uc.qiniuapi.com/v2/domains?tbl=<bucket>` 查询空间域名。UC 请求继续复用官方 `QiniuMacRequestsAuth`，不使用旧 SDK 中以 POST 调用查询接口的兼容实现。
- **产品互斥分类**：CDN Domain 响应中 `source.sourceType=qiniuBucket` 的目标只进入对象存储扫描；其中 Bucket/域名关系能被 Kodo API 确认的目标归为 `oss`，`product_id` 为 `<bucket>:<domain>`。其余 CDN 域名归为 `cdn`。这样两类扫描可同时开启而不会对同一域名重复部署。只出现在 Kodo 域名列表、无法由 CDN Domain API 确认的原生源站域名不进入自动部署，因为公开 API 没有独立的源站域名证书更新接口。
- **绑定范围**：只接受 `product` 缺失或为 `cdn` 且有合法 `name` 的条目；保留 `protocol`、运行状态、CNAME、HTTPS 配置等元数据。DCDN 不在本次范围。
- **上传复用**：公开实现 `upload_certificate()`，名称使用主域名和时间戳；`deploy()` 有 `cert_id` 时直接绑定，没有时才上传。上传地址为 `https://fusion.qiniuapi.com/sslcert`，请求只发送当前文档要求的 `name`、`pri`、`ca`。
- **HTTP/HTTPS 下发分流**：`cdn` 与 `oss` 目标统一按准确加速域名调用 CDN Domain API。目标 `metadata.protocol=http` 时调用 `/sslize` 并显式设置 `forceHttps=false`、`http2Enable=true`；已是 HTTPS 时调用 `/httpsconf` 且只传 `certId`，避免覆盖现有强制跳转、HTTP/2 与 TLS 版本配置。协议缺失按 HTTPS 更新处理，兼容旧绑定缓存。
- **失败语义**：底层 API 对非 2xx、JSON 中 `error` 或非成功 `code` 抛出异常；Provider 继续通过 `_call_provider_api` 统一重试和抛错，Deployer 捕获并返回失败结果，保持现有架构语义。

## Risks / Trade-offs

- [七牛配置变更通常需要 5-10 分钟生效] → API 成功仅代表请求已接受；日志明确记录域名、接口动作和证书 ID，不在本次引入长轮询。
- [旧缓存缺少 `protocol`] → 按已启用 HTTPS 处理并调用 `/httpsconf`；部署前实时校验开启时会刷新元数据。
- [上传证书后绑定全部失败会留下未绑定证书] → 本次不自动删除，避免误删被其它并发流程引用的证书；后续可单独设计清理策略。
- [官方 SDK 的 `DomainManager` 仍使用旧 HTTP 地址与旧字段] → 仅复用官方认证类，接口地址和请求模型按当前官方 OpenAPI 文档实现。
- [Kodo 原生源站域名没有公开的证书更新 API] → 仅自动部署能在 CDN Domain API 中确认的 Bucket 加速域名；其它域名记录跳过并输出日志，不猜测私有接口。

## Migration Plan

1. 合并代码并在本地配置中添加 `type: qiniu`、`access_key_id`、`access_key_secret`。
2. 开启 `product_scan.qiniu.cdn_scan` 与 `oss_scan`，先以 `deploy.mode: dry-run` 采集并确认命中目标。
3. 切换到 `apply` 完成首次真实证书部署，观察七牛云控制台在 5-10 分钟内生效。
4. 回滚时关闭七牛 provider 或七牛 `cdn_scan`/`oss_scan`；代码回滚不会删除已上传证书或改变已生效的 CDN 配置。

## Open Questions

无。强制 HTTPS 与 HTTP/2 策略沿用现有实现：新开启 HTTPS 时不强制跳转并开启 HTTP/2；更新已有 HTTPS 时保持原配置。
