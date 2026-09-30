## Why

当前证书列表只有普通续签与强制续签，但普通续签受 acme.sh 已有证书和冷却窗口约束，无法为已在云端使用、尚未纳入本机 acme.sh 管理的证书完成首次签发。Cloudflare 等 DNS Provider 已能提供域名归属和 DNS API 凭证，现在可以补齐自动 DNS-01 签发入口。

## What Changes

- 从证书池移除普通续签，证书池仅保留“强制续签”和“部署”；在托管域名列表恢复“签发”入口。
- 点击域名组签发时默认使用托管主域名；点击子域行签发时，默认使用所点主机名的直接父域及其通配域名，允许继续编辑。
- 根据域名扫描结果解析唯一 Provider，并映射阿里云、腾讯云、Cloudflare 的 acme.sh DNS 插件与凭证。
- 签发前校验域名集合、Provider 一致性、DNS 插件支持情况以及本机是否已存在同 `Main_Domain` 的 acme.sh 证书；父级通配证书不阻止独立子域证书签发。
- 签发成功后复用现有证书路径解析和多云部署流程；签发失败发送独立通知，但不读取或写入续签冷却状态。
- 增加签发命令、服务编排、页面事件、文档和测试。

## Capabilities

### New Capabilities

- `certificate-issuance`: 定义从证书列表触发 DNS-01 首次签发、Provider/DNS 插件选择、域名集合校验以及签发后部署行为。

### Modified Capabilities


## Impact

- 修改 `app/utils/acme_sh.py`、`app/services/renew.py`、`app/services/renew_flow.py` 和托管域名页面操作入口。
- 扩展 acme 抽象能力以支持签发，并复用已有 `RenewedCert`、Provider 凭证映射和部署服务。
- UI 的普通“续签”按钮被移除；签发位于托管域名列表，强制续签和自动续签行为保持不变。
- 不新增云厂商 DNS 写入客户端；TXT 创建和删除仍由 acme.sh 官方 DNS 插件完成。
