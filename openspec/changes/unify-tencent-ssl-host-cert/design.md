## Context

腾讯云 SSL 控制台用同一套 Host 实例接口发现证书绑定，再用 `DeployCertificateInstance` 覆盖到产品。现有对外模型已经稳定：CDN `product_id=域名` + `metadata.https_billing`；CLB `product_id=<region>:<lb_id>:<listener_id>`；COS `product_id=<region>:<bucket>:<domain>`。

## Goals / Non-Goals

**Goals:**

- 日常缓存通过 CDN、云直播、CLB、COS 产品接口获得域名与产品关系。
- apply 上传证书后，通过 SSL Host 接口完成证书匹配并下发。
- 绑定结构和部署入参兼容已有缓存。

**Non-Goals:**

- 不接 VOD、WAF、TEO 等其它 SSL ResourceType。
- 不改 product_scan 默认开关。
- 不改 CLB 多地域配置方式。

## Decisions

- 日常扫描：CDN 使用 `DescribeDomainsConfig`，云直播使用 `DescribeLiveDomains` 和 `DescribeLiveDomainCert`，CLB 使用 `DescribeLoadBalancers` 和 `DescribeListeners`，COS 使用 `list_buckets` 和 `get_bucket_domain`，并通过 TLS 探测确认自定义域名可用 HTTPS。
- 部署扫描：上传证书后按产品调用 SSL `DescribeHost*InstanceList`，统一携带 `CertificateId`、`IsCache=1`、`domainMatch=1`。
- CLB：支持 `HTTPS`、`TCP_SSL`；SNI 监听器使用规则域名，非 SNI 监听器作为监听器级目标部署。
- 下发：CLB/COS 走 `DeployCertificateInstance`。CDN 双路径：`cdn_deploy_mode=ssl` 走 SSL（实例 `domain|https_billing`）；未标记的旧缓存默认 `modify` 走 `ModifyDomainConfig`。
- 兼容：新扫描写入 `cdn_deploy_mode=ssl`；旧缓存无标记仍用 ModifyDomainConfig；CDN 可用 `product_id` 当域名。
- 异步终态：`DeployStatus=1` 仅表示任务创建成功，必须轮询 `DescribeHostDeployRecordDetail`，仅全部详情成功才返回成功。
- 任务冲突：`DeployStatus=0` 表示同证书同资源类型已有任务运行，等待该记录结束后重试当前目标；逐目标调用因此保持串行且不冲突。
- 扫描上下文：`DescribeHost*InstanceList` 是按待部署证书匹配资源的接口，请求必须带 `CertificateId` 和 `domainMatch=1`。不能把它用于无证书上下文的全账号缓存刷新。
- 两阶段发现：日常缓存刷新使用 CDN、CLB、COS、云直播产品原生查询接口；部署 apply 在证书上传后统一调用 SSL Host 接口发现 CDN/COS/CLB/云直播目标。COS 产品查询只能确认已启用的自定义域名规则，HTTPS 准入由 apply 阶段 SSL 扫描最终确认。
- 匹配归属：腾讯 SSL `domainMatch=1` 的返回结果就是腾讯侧完成证书域名匹配后的目标，本地不再重复执行域名匹配，只负责字段校验和资源去重。
- 错误语义：apply 阶段任一已启用产品的 SSL 扫描失败必须抛错，禁止把参数错误、权限不足或限流降级成空目标并发送成功通知。
- 合并优先级：证书上下文 SSL 扫描结果是同一腾讯账号已启用产品的权威部署目标，替换这些产品的缓存匹配结果。`ModifyDomainConfig` 只保留给未进入新扫描流程的旧目标兼容调用。
- 零目标：apply 在云产品和 Nginx 均没有目标时按失败处理，避免发送成功通知。

## Risks / Trade-offs

- SSL CDN 列表与旧 `DescribeDomainsConfig` 字段不同，但对外绑定字段对齐，旧缓存仍可部署。
- CLB 产品清单按地域查询；任一地域失败时整次扫描失败，避免用部分结果覆盖完整缓存。
- 腾讯 SSL 部署是异步任务；轮询默认最长 300 秒，每 2 秒一次。超时按失败处理，避免误发成功通知。
- dry-run 不上传证书，因此只能展示缓存/产品原生接口可发现的目标；COS 与云直播的证书匹配目标仅在 apply 阶段可准确发现。
