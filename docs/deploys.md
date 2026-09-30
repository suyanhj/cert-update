# Deploys 模块说明

本文档描述 `app/deploys/*` 的职责、统一接口与当前实现能力。
流程图见 `docs/README.md` 的「文档与流程图索引」。

## 1. 范围与边界

- 负责执行证书下发（把证书写入或绑定到目标资源）。
- 不负责资源发现（由 `app/providers/*` 执行）。
- 不负责业务编排（由 `app/services/deploy.py` 执行）。

## 2. 核心抽象（base.py）

- `DeployTarget`：统一部署目标描述（`provider` / `product_type` / `product_id` / `domain` / `metadata`）。
- `DeployResult`：统一执行结果（`success` / `message` / `cert_id`）。
- `CertificateDeployer`：部署器基类（`deploy(...)` / `list_targets()`）。

说明：`DeployTarget.listener_port` 仅用于历史 LB 端口区分，当前匹配与缓存不依赖该字段。

## 3. 现有 Deployer 能力

- `AliyunDeployer`：`cdn` / `slb` / `alb` / `oss`
- `TencentDeployer`：`cdn` / `live` / `teo`（EdgeOne）/ `clb` / `oss`（COS 自定义域名）
- `HuaweiDeployer`：`cdn` / `elb` / `waf`（云模式）
- `VolcengineDeployer`：`cdn` / `clb`
- `QiniuDeployer`：`cdn` / `oss`（Kodo Bucket 加速域名）
- `NginxSSHDeployer`：`nginx`（SSH 下发）

## 4. 调用关系

- 上游入口：`renew_flow.py` -> `services/deploy.py`
- 构建入口：`services/deployer_factory.py`
- 执行入口：`deployer.deploy(...)`
- 返回结果：`DeployResult`

## 5. 绑定匹配与缓存

配置来源：`deploy.binding_cache`（见 `config-entry.md` / `config-rules.md`）。

默认值：`enabled=true`、`ttl_seconds=3600`、`verify_before_apply=true`。

匹配阶段：
- `enabled=true` 时只读缓存（`cache_only=true`）；缓存缺失或过期会直接返回空，不触发云 API。
- `enabled=false` 时按 `product_scan.<cloud>` 实时拉取云 API 绑定。

部署阶段（仅 apply）：
- `verify_before_apply=true` 时，按 provider 分组做一次实时拉取校验，避免脏缓存导致误下发。
- 校验是“每个 provider 扫一次”，不是每个证书或每个目标各扫一次。
- 腾讯云会先上传证书，再以证书 ID 调用 SSL Host 接口匹配 CDN、云直播、CLB、COS；SSL 返回的同一目标优先走 SSL 下发。未被 SSL 扫描覆盖的旧 CDN `modify` 目标继续走 `ModifyDomainConfig`，不会被整体删除。

缓存结构：`data/product_bindings_cache.json` 使用 `provider_type:provider_name` 作为 key，保存 `updated_at` 与 `products`。

`products` 结构为 `product_type -> product_id -> [{ "domain": "xxx", "metadata": {...} }]`，仅保留匹配所需字段，`listener_port` 等历史字段不再落盘。

## 6. 当前可匹配的云厂商

`DeployService._build_cloud_providers` 当前构建的 provider 为：`aliyun` / `tencent` / `huawei` / `qiniu`。
`volcengine` 当前被注释掉，不参与匹配与部署；如需启用需调整构建列表。

## 7. 失败策略与返回

- `plan_dry_run()` 与 `apply_deploy()` 都会校验 `deploy.mode`；不匹配则抛 `RuntimeError`。
- `apply_deploy()` 汇总失败项后会抛 `RuntimeError`，并在日志中给出失败数量。
- `apply_deploy()` 没有任何云产品或 Nginx 目标时也会抛错，避免误发部署成功通知。
- SDK 异常在 deployer 内转为失败结果或记录日志，由上层统一聚合处理。

## 8. SDK 与工具复用约定

- 阿里云：`app/utils/aliyun_sdk.py`
- 腾讯云：`app/utils/tencent_sdk.py::build_tencent_client`
- 火山引擎：`app/utils/volcengine_sdk.py::build_volcengine_service`
- 华为云：`app/utils/huawei_sdk.py`
- 七牛云：`app/utils/qiniu_api.py`（官方 SDK 双鉴权 + 当前 OpenAPI 地址）
- 日志：统一 `app.utils.logger.get_logger("deployer")`
- 时间：统一 `app.utils.time.TimeUtil.now()`

## 9. 各云证书下发逻辑（上传与复用）

是否能“上传一次并复用”，由 deployer 的 `prepare_certificate()` 决定；默认实现继续调用 `upload_certificate()`。
华为云会依据本次实际目标分别准备 ELB 与 WAF 证书，避免跨产品误用证书 ID；其他 deployer 保持同一 provider 上传一次并复用的行为。

### 9.1 阿里云

阿里云实现了 `upload_certificate()`，上传到 CAS 并复用 `cert_id`。

CDN 使用 CAS `cert_id` 调用 `SetCdnDomainSSLCertificate`。
ALB 使用 CAS `cert_id` 更新监听器。
OSS 使用 CAS `cert_id`，`product_id` 格式为 `<region>:<bucket>:<domain>`。
SLB 不复用 CAS，改为上传 SLB ServerCertificate 并绑定监听器。

### 9.2 华为云

华为云实现了 `upload_certificate()`，上传到 ELB 证书管理并写入 `domain` 字段支持 SNI。

CDN 直接推送 PEM/KEY。
ELB 更新监听器证书引用，SNI 更新时只替换匹配条目，保持原有结构。
WAF 使用独立证书仓库：同次部署只创建一张 WAF 证书，再通过证书绑定接口逐个绑定匹配的云模式 host；仅更新证书关系，不修改源站、TLS、策略或防护状态。企业项目由 binding metadata 传递，账号级默认值可配置为 `credentials.enterprise_project_id`。

### 9.3 腾讯云

实现了 `upload_certificate()`，上传到 SSL 证书服务并复用 `cert_id`（`Repeatable=false`，重复证书用 `RepeatCertId`）。

CDN 支持两种下发，由绑定 `metadata.cdn_deploy_mode` 决定：
- `ssl`（新扫描默认）：SSL `DeployCertificateInstance`，实例 `<domain>|<https_billing>`；缺 `https_billing` 时按 `on`。
- `modify`（旧缓存默认）：CDN `ModifyDomainConfig` 只更新 `Https.CertInfo.CertId`。
云直播使用 SSL `DeployCertificateInstance`，`ResourceType=live`，实例为 `<domain>`。
EdgeOne 使用 SSL `DeployCertificateInstance`，`ResourceType=teo`，实例为 `<domain>`。
CLB / COS 使用 SSL `DeployCertificateInstance`。
CLB SNI 为 `<lb_id>|<listener_id>|<domain>`，非 SNI 为 `<lb_id>|<listener_id>`；`product_id` 仍支持旧格式（无地域时用部署器默认 region）。
COS 实例为 `<region>|<bucket>|<domain>`，`product_id` 为 `<region>:<bucket>:<domain>`。
SSL 下发接口只创建异步任务；部署器会轮询 `DescribeHostDeployRecordDetail`，确认全部目标成功后才返回成功。同证书同资源类型已有任务时，等待该任务结束后重试当前目标；默认等待上限 300 秒。

### 9.4 火山引擎

未实现 `upload_certificate()`，CLB 在部署时上传并绑定，CDN 直接推送证书并更新域名配置。

### 9.5 七牛云

实现了 `upload_certificate()`，证书通过 `fusion.qiniuapi.com/sslcert` 使用 QBox 鉴权上传一次，同一账号下多个 CDN 目标复用一个 `cert_id`。

CDN 域名管理与配置通过 `api.qiniu.com` 使用 Qiniu 请求内容签名。普通 CDN 使用 `product_type=cdn`；Kodo Bucket 加速域名使用 `product_type=oss`、`product_id=<bucket>:<domain>`，两类目标互斥但共享同一套证书下发接口和上传结果。

目标当前协议为 HTTP 时调用 `sslize` 开启 HTTPS（不强制跳转、启用 HTTP/2）；目标已经是 HTTPS 时调用 `httpsconf` 且只传新证书 ID，保持原有强制跳转、HTTP/2 和 TLS 版本配置。七牛配置变更通常需要 5-10 分钟生效。

### 9.6 Nginx SSH

不走证书中心，直接写文件并测试/重载 Nginx。

## 10. 新增 Deployer 接入清单

1. 在 `app/deploys/` 新增实现并继承 `CertificateDeployer`。
2. 在 `app/deploys/__init__.py` 导出类。
3. 在 `services/deployer_factory.py` 注册 `provider_type -> deployer`。
4. 在 `services/deploy.py` 的 `apply` 路径做联调。
5. 补充测试用例。

## 11. 关联文档

- 运行流程：`docs/flow-runtime.md`
- 文档与流程图索引：`docs/README.md`
- 服务编排：`docs/services.md`
- 资源发现：`docs/providers.md`
