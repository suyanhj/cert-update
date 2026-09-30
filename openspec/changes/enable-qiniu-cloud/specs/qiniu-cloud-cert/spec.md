## ADDED Requirements

### Requirement: 七牛 API 使用匹配的域名与鉴权

系统 SHALL 将域名管理和域名配置请求发送到 `api.qiniu.com` 并使用 Qiniu 请求内容签名，将 Kodo Bucket 查询发送到 `uc.qiniuapi.com` 并使用 Qiniu 请求内容签名，将证书管理请求发送到 `fusion.qiniuapi.com` 并使用 QBox 路径签名。系统 MUST 使用官方七牛 Python SDK 的认证实现，且 Qiniu 签名使用的 JSON 字节 MUST 与实际请求体一致。

#### Scenario: 域名接口使用 Qiniu 鉴权

- **WHEN** 系统请求 `api.qiniu.com/domain` 或 `api.qiniu.com/domain/<name>/httpsconf`
- **THEN** 请求头使用 `Authorization: Qiniu ...`，并包含 SDK 生成的时间戳签名信息

#### Scenario: 证书接口使用 QBox 鉴权

- **WHEN** 系统请求 `fusion.qiniuapi.com/sslcert`
- **THEN** 请求头使用 `Authorization: QBox ...`，签名绑定完整路径和查询参数

#### Scenario: 拒绝未知七牛 API 主机

- **WHEN** 通用请求封装收到不是 `api.qiniu.com`、`uc.qiniuapi.com` 或 `fusion.qiniuapi.com` 的 URL
- **THEN** 系统在发送网络请求前失败并指出不支持的主机

### Requirement: CDN 域名分页发现

`QiniuCloudProvider` SHALL 通过 `GET https://api.qiniu.com/domain` 分页读取账号域名，并跟随响应 `marker` 直到结束。返回的 CDN 绑定 MUST 具有非空域名，`product_type` MUST 为 `cdn`，并携带协议、运行状态、CNAME 与 HTTPS 配置元数据；明确属于 DCDN 的条目 MUST 被排除。

#### Scenario: 多页域名合并

- **WHEN** 第一页返回非空 `marker` 且第二页返回空 `marker`
- **THEN** Provider 返回两页中所有合法 CDN 域名且每个 marker 只请求一次

#### Scenario: 排除非法与 DCDN 条目

- **WHEN** 域名列表同时包含空 `name` 条目、`product=dcdn` 条目和合法 CDN 条目
- **THEN** 绑定列表只包含合法 CDN 条目

#### Scenario: marker 不前进

- **WHEN** 服务端连续返回相同的非空 `marker`
- **THEN** 系统失败并记录分页异常，不进入无限循环

### Requirement: 证书上传一次并复用

`QiniuDeployer` SHALL 实现 `upload_certificate()`，把 PEM 私钥和完整证书链上传到 `https://fusion.qiniuapi.com/sslcert` 并返回非空 `certID`。`deploy()` 收到已有 `cert_id` 时 MUST NOT 再上传证书；未收到时 SHALL 先上传再绑定。

#### Scenario: 上层复用证书 ID

- **WHEN** `upload_certificate()` 返回 `cert-1`，随后多个目标调用 `deploy(..., cert_id="cert-1")`
- **THEN** 部署请求均使用 `cert-1` 且不再次请求 `/sslcert`

#### Scenario: 上传响应缺少证书 ID

- **WHEN** 上传接口成功响应中没有非空 `certID`
- **THEN** 上传操作失败并包含证书名称相关日志

### Requirement: 根据当前协议绑定 CDN 证书

`QiniuDeployer` SHALL 仅接受 `product_type=cdn` 的目标。目标协议为 `http` 时 MUST 调用 `/domain/<name>/sslize` 并发送 `certId`、`forceHttps=false`、`http2Enable=true`；目标协议为 `https` 或旧缓存未提供协议时 MUST 调用 `/domain/<name>/httpsconf` 且只更新 `certId`。成功结果 MUST 返回实际使用的 `cert_id`。

#### Scenario: HTTP 域名开启 HTTPS

- **WHEN** CDN 目标 `metadata.protocol=http` 且证书 ID 为 `cert-1`
- **THEN** 系统调用 `/sslize`，请求体为 `certId=cert-1`、`forceHttps=false`、`http2Enable=true`

#### Scenario: HTTPS 域名保持现有配置

- **WHEN** CDN 目标 `metadata.protocol=https` 且证书 ID 为 `cert-1`
- **THEN** 系统调用 `/httpsconf` 且请求体只包含 `certId=cert-1`

#### Scenario: 不支持的产品类型

- **WHEN** 目标的 `product_type` 不是 `cdn`
- **THEN** 部署结果失败且不上传、不发送绑定请求

### Requirement: Kodo Bucket 与加速域名扫描

`QiniuCloudProvider` SHALL 通过 Kodo UC API 列举账号 Bucket，并查询每个 Bucket 的绑定域名。系统 MUST 将同时满足以下条件的域名产出为对象存储绑定：该域名存在于 Bucket 域名列表、CDN Domain API 显示其源站类型为 `qiniuBucket`、源站 Bucket 与当前 Bucket 一致。对象存储绑定的 `product_type` MUST 为 `oss`，`product_id` MUST 为 `<bucket>:<domain>`。

#### Scenario: Bucket 加速域名成为 OSS 绑定

- **WHEN** Bucket `assets` 绑定 `img.example.com`，且 CDN Domain API 返回该域名的 `sourceQiniuBucket=assets`
- **THEN** 系统返回 `product_type=oss`、`product_id=assets:img.example.com`、`domain=img.example.com` 的绑定

#### Scenario: 原生源站域名不进入自动部署

- **WHEN** Bucket 域名列表包含某域名，但 CDN Domain API 中不存在该域名
- **THEN** 系统不生成该域名的 OSS 部署绑定

#### Scenario: CDN 与 OSS 分类互斥

- **WHEN** `img.example.com` 被识别为 Bucket `assets` 的加速域名
- **THEN** 该域名只出现在 OSS 绑定列表，不出现在普通 CDN 绑定列表

### Requirement: 对象存储加速域名部署

`QiniuDeployer` SHALL 接受 `product_type=oss` 且 `product_id` 格式为 `<bucket>:<domain>` 的目标，并与 CDN 目标复用同一次证书上传结果。系统 MUST 按目标协议对准确域名调用 `sslize` 或 `httpsconf`，成功结果 MUST 返回实际使用的 `cert_id`。

#### Scenario: OSS 目标复用证书并部署

- **WHEN** 对象存储目标为 `assets:img.example.com`、协议为 HTTPS，且上层传入 `cert_id=cert-1`
- **THEN** 系统不重复上传证书，并调用 `/domain/img.example.com/httpsconf` 绑定 `cert-1`

#### Scenario: OSS product_id 非法

- **WHEN** 对象存储目标缺少 Bucket、域名或 `product_id` 分隔符
- **THEN** 部署失败且不调用域名配置 API

### Requirement: 七牛配置与文档可直接使用

示例配置 SHALL 提供七牛 provider 的 AK/SK 字段说明，并将 `product_scan.qiniu.cdn_scan` 与 `oss_scan` 设为 true，其他未支持的七牛产品扫描保持关闭。Provider 与 Deployer 文档 MUST 描述双鉴权、分页发现、Kodo 扫描、上传复用及 HTTP/HTTPS 下发行为。

#### Scenario: 示例配置启用 CDN 与对象存储扫描

- **WHEN** 加载 `config.example.yaml` 的 `product_scan.qiniu`
- **THEN** `cdn_scan` 与 `oss_scan` 为 true，`lb_scan`、`ecs_scan` 与 `domain_scan` 为 false
