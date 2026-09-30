## Why

同一根域名可能在阿里云、腾讯云等平台注册，却把权威 DNS 托管到 Cloudflare 或其他平台。当前扫描模型只允许一个 Provider 同时代表注册商和 DNS 托管商，导致 Cloudflare 记录缺少到期时间与注册主体，或者证书签发错误使用注册商的 DNS 凭证。

## What Changes

- 将域名注册元数据与 DNS Zone/记录元数据拆为两个独立角色。
- 对各 Provider 扫描结果按规范化根域名融合，允许注册商和 DNS 托管商来自不同平台及不同账号。
- 域名到期时间、剩余天数、注册主体和到期告警使用注册商数据。
- DNS 记录、页面签发入口和 `DomainProviderStore` 使用 DNS 托管 Provider。
- 页面分别展示注册商与 DNS 托管商；缺失任一角色时明确显示未知，不伪造数据。
- 对重复注册信息、重复 DNS Zone 和不完整数据应用人工覆盖、公网权威 NS、平台强证据的确定性选择规则，并在无法确认时停止 DNS 映射。

## Capabilities

### New Capabilities

- `domain-registration-dns-separation`: 定义注册元数据、DNS 托管元数据、按根域名融合、页面展示、告警和证书签发 Provider 归属。

### Modified Capabilities


## Impact

- 修改 Provider 域名发现返回语义、`DomainDiscoveryService` 融合逻辑和 `DomainProviderStore` 保存输入。
- 调整阿里云、腾讯云、华为云、Cloudflare 等 Provider 的域名角色标记。
- 修改域名页面、域名到期告警以及相关运行时快照字段。
- 不改变 acme.sh DNS 插件本身；签发继续通过 DNS 托管 Provider 选择凭证。
- 新增 `dnspython` 用于只读公网 NS 查询；不使用 WHOIS/RDAP 补全注册信息。
