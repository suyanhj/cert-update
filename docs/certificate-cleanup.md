# 云端过期证书清理

## 安全模型

该功能默认关闭，首次启用必须使用 `report`。一轮完整扫描只会形成候选；真正删除前还会在账号证书操作锁内重新读取证书、重新查询关联资源，调用平台的在用保护删除接口，并在删除后确认证书已经不存在。因此默认 `confirm_scans: 1` 已经包含“扫描一次 + 删除前实时复查”两道检查。

任一分页、地域、项目、Bucket、产品、权限或异步任务查询不完整时，当前独立证书库失败关闭，删除计划为零；其他账号或证书库继续扫描。同一账号的 ELB、WAF、CCM 等独立证书库也会分别初始化和隔离。产品绑定缓存和 `product_scan` 开关不作为删除依据。

删除不可恢复。建议先运行至少一个完整 `report` 周期并人工核对通知与 `data/certificate_cleanup_state.json`，再为单个测试账号配置 `delete_scopes`。

## 配置

```yaml
certificate_cleanup:
  enabled: true
  mode: report                 # report 或 delete
  interval_hours: 24
  expired_grace_days: 30       # 到期后继续保留的天数
  confirm_scans: 1             # 默认一轮完整扫描；删除前仍会实时复查
  max_deletes_per_run: 20      # 所有证书库合计的单轮上限
  delete_scopes: []            # report 阶段保持为空
```

核对完成后，只为明确账号和证书库开放删除：

```yaml
certificate_cleanup:
  enabled: true
  mode: delete
  interval_hours: 24
  expired_grace_days: 30
  confirm_scans: 1
  max_deletes_per_run: 20
  delete_scopes:
    - provider_type: tencent
      provider_name: 腾讯生产
      store: ssl
    - provider_type: qiniu
      provider_name: 七牛生产
      store: fusion_ssl
    - provider_type: huawei
      provider_name: 华为测试
      store: elb
    - provider_type: aliyun
      provider_name: 阿里测试
      store: slb
```

`delete_scopes` 必须与已配置 Provider 的 `type + name` 精确匹配；只写 `mode: delete` 不会触发删除。自动删除还要求平台适配器通过 `safe_delete` 门禁。

## 平台能力

| 平台证书库 | 库名 | 库存/关联报告 | 自动删除 |
|---|---|---:|---:|
| 腾讯云 SSL | `ssl` | 是，使用实时关联资源同步任务 | 是，固定 `IsCheckResource=true` 并轮询异步任务 |
| 七牛 Fusion SSL | `fusion_ssl` | 是，独立盘点 CDN、Bucket 和 Kodo 域名 | 是，平台在用错误码安全跳过 |
| 华为 ELB | `elb` | 是，按配置的 region/project 隔离 | 是，删除前检查监听器并依赖 409 在用保护 |
| 华为 WAF | `waf` | 是，按 enterprise project 隔离 | 否，当前固定仅报告 |
| 华为 CCM | `ccm` | 是，使用 `ListDeployedResources` | 否，当前固定仅报告 |
| 阿里云 CAS | `cas` | 是，使用 `ListCloudResources` 按证书 ID 分页查询全产品部署资源 | 否，删除接口未明确承诺在用拒绝 |
| 阿里云 CLB | `slb` | 是，完整分页盘点配置地域的 HTTPS 监听器与扩展域名 | 是，删除前再次按证书 ID 实时盘点，并依赖平台在用拒绝 |
| 火山引擎证书中心 | `certificate_service` | 库存报告，关联未知 | 否 |
| Cloudflare / custom | - | 跳过，不会伪装成扫描成功 | 否 |

华为 ELB 中 `source=scm` 表示 CCM 推送到 ELB 的产品侧副本；清理 ELB 库只处理该副本，不删除 CCM 源证书。云厂商签发、购买订单和无法识别来源的证书只报告，不进入删除候选。

## 权限与运行状态

运行账号至少需要对应证书库的列表、详情和关联资源查询权限。阿里云 CAS 关联报告需要 `yundun-cert:ListCloudResources`；阿里云 CLB 需要 `slb:DescribeServerCertificates`、`slb:DescribeLoadBalancers`、`slb:DescribeLoadBalancerListeners` 和 `slb:DescribeDomainExtensions`，开放 `slb` 自动删除时还需要 `slb:DeleteServerCertificate`。开启删除的腾讯 SSL、七牛 Fusion SSL、华为 ELB 或阿里云 CLB 还需要各自的删除权限；腾讯关联检查/安全删除需要平台要求的服务角色。库存扫描明确返回 403/Forbidden/NoPermission，或 SDK 明确表示当前地域不支持该证书库时，不会降级为空列表：该库候选会清零、删除数量为零，并以 `WARNING` 和审计摘要记录，但不进入用户通知。分页损坏、网络异常、限流和未知错误仍会发送失败关闭通知；删除阶段的权限错误也仍会告警。

状态保存在 `data/certificate_cleanup_state.json`，包含上次检查时间、活动候选、连续确认次数、有限运行摘要和通知签名。文件不保存私钥、完整 PEM 或云访问密钥；损坏时原文件会先备份为 `.corrupt-*.bak`，然后使用空候选重新确认。

通知按平台、Provider 和证书库汇总扫描数、候选、已确认、删除、关联跳过、仅报告、达到上限延后和失败原因。结果没有变化时不会重复发送。
