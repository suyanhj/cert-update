# Providers 模块说明

本文档描述 `app/providers/*` 的职责、统一接口与当前实现能力。
流程图见 `docs/README.md` 的「文档与流程图索引」。

## 1. 范围与边界

- 负责发现：域名、DNS 记录、云产品绑定关系、ECS 实例信息。
- 不负责部署证书（部署由 `app/deploys/*` 执行）。
- 不负责缓存持久化策略（由 services 层决定）。

## 2. 统一接口（base.py）

核心接口（按调用侧需要实现）：

- `domain_roles`：Provider 显式声明 `registration`、`dns` 或两者；单条结果可用同名字段缩小默认角色，空集合表示不参与根域名融合。
- `get_domain_list()`：返回根域名列表（含必要元数据）。
- `get_dns_records(domain)`：返回 DNS 记录列表。
- `get_cdn_bindings()` / `get_lb_bindings()` / `get_oss_bindings()`：返回对应产品绑定。
- `get_ecs_instances()`：返回 ECS 实例列表（含到期元数据）。
- `get_all_product_bindings()`：聚合 CDN/LB/OSS 绑定（失败会按方法返回空并记录日志）。

通用能力：

- `_call_provider_api(...)`：统一重试包装，失败返回 default 并输出摘要日志。
- `_probe_domain(...)`：域名探测（http → https），返回状态“在工作/异常/通配符跳过”。

## 3. 现有 Provider 能力

- `static.py` (`StaticCloudProvider`)
  - 角色：注册信息 + DNS；使用配置中的静态域名列表进行探测与归一化，适合初始化或无 SDK 场景。

- `aliyun.py` (`AliyunCloudProvider`)
  - 角色：注册信息 + DNS；注册 API 提供到期时间与注册主体，DNS API 提供解析记录。
  - CDN 绑定：仅保留 **在线** 且 **HTTPS 已开启** 且 **能查询到证书 ID** 的域名。
  - OSS 绑定：仅保留 **已配置证书** 的 CNAME 绑定。
  - LB 绑定：扫描 HTTPS 监听器，解析证书与域名关系形成绑定。
  - ECS：多地域采集实例并补全到期与续费信息。

- `tencent.py` (`TencentCloudProvider`)
  - 角色：注册信息 + DNS；域名注册 API 提供到期时间与注册主体，DNSPod 提供 Zone 与记录。
  - 注册 API 与 DNSPod 结果取并集；已移出 DNSPod 的注册域名单独保留为 `registration` 角色。
  - 日常绑定缓存使用产品只读接口：CDN `DescribeDomainsConfig`、云直播 `DescribeLiveDomains` / `DescribeLiveDomainCert`、CLB `DescribeLoadBalancers` / `DescribeListeners`、COS `list_buckets` / `get_bucket_domain`。不会在无证书上下文时调用 SSL Host 接口。
  - CDN：保留在线且 HTTPS 已开启的加速域名，未绑定证书也可进入清单；产品清单标记 `metadata.cdn_deploy_mode=modify`，供旧路径兼容。
  - 云直播：保留已启用 HTTPS 证书的播放域名；`product_type=live`，`product_id` 为域名；需 `product_scan.tencent.live_scan=true`。
  - EdgeOne：仅在 apply 上传新证书后，通过 SSL `DescribeHostTeoInstanceList` 实时扫描匹配域名；`product_type=teo`，`product_id` 为域名；需 `product_scan.tencent.eo_scan=true`。
  - CLB：扫描 `HTTPS` / `TCP_SSL` 监听器。日常清单记录 SNI 规则域名；非 SNI 监听器没有产品域名字段，部署时由 SSL Host 接口按证书匹配。可通过 `credentials.clb_regions` / `lb_regions` / `regions` 限定地域。
  - COS：列出 ENABLED 自定义域名并执行 TLS 探测；`product_type=oss`，`product_id` 为 `<region>:<bucket>:<domain>`。
  - apply 上传证书后，CDN / 云直播 / EdgeOne / CLB / COS 使用 SSL `DescribeHost*InstanceList`，请求包含 `CertificateId`、`IsCache=0`、`domainMatch=1`。腾讯返回结果直接作为部署目标，不再做本地域名二次匹配。

- `huawei.py` (`HuaweiCloudProvider`)
  - 角色：仅 DNS；读取 PublicZone 列表，当前不接入华为域名注册 API。
  - CDN 绑定：仅保留在线域名。
  - ELB 绑定：扫描 `HTTPS` / `TERMINATED_HTTPS` 监听器，过滤 `admin_state_up=true`；解析默认证书与 SNI 证书，反推监听器域名绑定。
  - WAF 绑定：分页扫描云模式防护域名，通过详情接口保留启用 HTTPS 的 host；记录当前证书 ID、证书名、企业项目 ID 和防护/接入状态。需显式启用 `product_scan.huawei.waf_scan=true`。
  - ECS：采集实例基础信息，尽量通过 BSS 补充到期时间。

- `volcengine.py` (`VolcengineCloudProvider`)
  - 角色：仅 DNS；发现 DNS Zone 与记录。
  - CDN 绑定：仅保留在线域名。
  - ELB 绑定：当前未实现，返回空列表。

- `qiniu.py` (`QiniuCloudProvider`)
  - 角色：不参与根域名注册/DNS 融合；通过 `api.qiniu.com/domain` 获取的是 CDN 产品域名。
  - CDN 绑定：返回非 Kodo Bucket 源站的域名级绑定，并保留协议、运行状态、CNAME、当前证书 ID 与 HTTPS 配置元数据。
  - 对象存储绑定：通过 Kodo UC API 列 Bucket 和 Bucket 域名，与 CDN Domain 的 `sourceQiniuBucket` 交叉确认后生成 `product_type=oss`、`product_id=<bucket>:<domain>`；该域名不再进入普通 CDN 绑定，避免重复部署。
  - Kodo 原生源站域名没有公开的证书更新 API，只自动接入可由 CDN Domain API 更新证书的 Bucket 加速域名；`product_scan.qiniu` 开启 `cdn_scan` 与 `oss_scan`。
  - 七牛 CDN/Kodo API 不提供 DNS 解析记录，因此不实现 DNS 记录扫描。

- `cloudflare.py` (`CloudflareDNSProvider`)
  - 角色：仅 DNS；使用官方 SDK 和 API Token 分页读取 active Zone 与全部 DNS 记录。
  - 保留 Zone API 的 `name_servers` 为 `assigned_nameservers`；`status=active` 且 `type=full` 时标记 `dns_authoritative=true`，供公网 NS 查询失败时安全回退。
  - 域名页面只探测 A、AAAA、CNAME；TXT、MX 等记录仍由 `get_dns_records()` 原样提供。
  - Provider 不修改 DNS，也不接入 CDN、边缘证书、WAF、负载均衡、R2 等产品。
  - 建议 Token 限定目标 Zone，并授予 Zone Read + DNS Edit；DNS Edit 是为后续 acme.sh 创建/删除 ACME TXT 记录准备。

## 4. 扫描开关与调用责任

- 顶层配置 `product_scan` 决定是否调用 provider 能力，但**开关只在 services 层生效**。
- WAF 扫描由 `waf_scan` 控制，默认关闭；当前仅华为云实现。
- EdgeOne 扫描由 `product_scan.tencent.eo_scan` 控制，默认关闭；它只参与腾讯云 apply 阶段的证书上下文扫描，不写入日常绑定缓存。
- `providers` 层只实现能力，不读取 `product_scan` 做内部短路。

## 5. 组装与调用关系

- 类型映射：`services/provider_factory.py`
- 运行时注册与索引：`services/provider_registry.py`
- 调用方：
  - `services/domains.py`（域名发现）
  - `services/deploy.py`（部署前绑定匹配）
  - `services/ecs.py`（ECS 采集）

## 6. SDK 与工具复用约定

- 阿里云：`app/utils/aliyun_sdk.py`
- 腾讯云：`app/utils/tencent_sdk.py::build_tencent_client`
- 火山引擎：`app/utils/volcengine_sdk.py::build_volcengine_service`
- 华为云：`app/utils/huawei_sdk.py`
- 七牛云：`app/utils/qiniu_api.py`（Domain/UC API 使用 Qiniu 鉴权，Fusion 证书 API 使用 QBox 鉴权）
- Cloudflare：官方 `cloudflare` Python SDK 5.x（API Token 认证及分页）
- 公网 NS：`dnspython`，只读查询根域名当前公开的 NS 记录并设置短超时
- 日志：统一 `app.utils.logger.get_logger("provider")`
- 时间解析：统一 `app.utils.time.TimeUtil.parse()`

## 7. 新增 Provider 接入清单

1. 在 `app/providers/` 新增类并继承 `Provider`。
2. 至少实现 `get_domain_list()`；参与部署匹配时实现绑定发现方法。
3. 在 `app/providers/__init__.py` 导出。
4. 在 `services/provider_factory.py` 注册映射。
5. 联调 `services/provider_registry.py` 路径。
6. 补充测试用例。

## 8. 关联文档

- 运行流程：`docs/flow-runtime.md`
- 文档与流程图索引：`docs/README.md`
- 服务编排：`docs/services.md`
- 部署执行：`docs/deploys.md`
