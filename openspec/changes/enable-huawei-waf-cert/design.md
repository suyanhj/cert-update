## Context

`crt` 已通过 `HuaweiCloudProvider` 扫描华为云 CDN/ELB 绑定，通过 `HuaweiDeployer` 执行证书上传和下发；产品绑定统一进入缓存、域名匹配及 apply 前实时校验。WAF 使用独立证书仓库，ELB 证书 ID 不能直接用于 WAF，因此需要在保持现有编排结构的前提下区分产品证书上传。

## Goals / Non-Goals

**Goals:**

- 使用官方 `huaweicloudsdkwaf` SDK 扫描云模式 HTTPS 防护域名及当前证书。
- 将 WAF 绑定接入现有扫描开关、缓存、域名匹配和实时校验。
- 每次部署中分别向需要的华为云证书仓库上传一次证书，并复用于相同产品的多个目标。
- 复用现有 AK/SK、项目、地域及日志/异常处理约定。

**Non-Goals:**

- 不接入 WAF 独享模式（premium host）或新增/删除防护域名。
- 不修改 WAF 源站、TLS、加密套件、策略或防护状态。
- 不自动清理 WAF/ELB 中的历史证书。

## Decisions

1. 扫描使用 `ListHost` 分页取得云模式防护域名，再通过 `ShowHost` 获取协议、证书 ID 和证书名。`ListHost` 返回项不含当前证书字段，不能单独满足绑定扫描要求。
2. WAF 绑定统一表示为 `product_type=waf`、`product_id=<host_id>`，企业项目 ID、协议、防护状态和接入状态写入 metadata。这样可直接复用缓存、匹配及实时校验，不创建旁路数据结构。
3. `CloudProductScanConfig` 新增 `waf_scan`，服务层的产品扫描清单新增 `get_waf_bindings`。默认关闭以避免现有部署在未授权 WAF 权限时改变行为，示例中仅华为云显式开启，其他云由空实现返回空列表。
4. SDK 客户端由 `app/utils/huawei_sdk.py` 统一构建，Provider 和 Deployer 共享相同的区域认证方式，并支持可选 `credentials.enterprise_project_id`，默认使用企业项目 `0`。
5. 在部署器基类增加面向目标集合的证书准备钩子，默认保持原有 `upload_certificate()` 行为。华为部署器按实际命中的产品准备证书：ELB 上传到 ELB，WAF 上传到 WAF，CDN 继续直接推送 PEM/KEY。WAF 部署调用证书绑定接口且每个目标只提交自己的 host ID。

## Risks / Trade-offs

- [每个 WAF host 需要一次详情查询，账号域名多时 API 调用增加] → 使用分页、单 host 失败隔离及现有缓存降低日常调用频率。
- [企业项目权限不足会导致部分 host 无法查询或绑定] → 在 metadata 中保留 host 的企业项目 ID，并记录包含 host/企业项目的清晰日志。
- [证书上传成功但绑定失败会留下未使用证书] → 返回失败并由现有部署状态/通知机制暴露；本次不做自动删除，避免误删被其他域名复用的证书。
- [新增部署准备钩子影响所有云厂商] → 基类默认实现完全委托现有上传方法，只有华为云覆盖该行为。

## Migration Plan

1. 安装新增的 WAF SDK 依赖并部署代码。
2. 确认华为云 IAM 权限包含 WAF host 查询、certificate 创建和绑定权限。
3. 按需配置 `credentials.enterprise_project_id` 与 `product_scan.huawei.waf_scan`，先执行扫描和 dry-run。
4. 验证匹配结果后切换 apply。回滚时关闭 `waf_scan` 或回退代码，不影响现有 CDN/ELB 能力。

## Open Questions

无。
