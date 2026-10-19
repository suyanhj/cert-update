## Why

托管域名的异常状态只有图标，HTTP/HTTPS 探测失败原因没有传到页面，难以定位连接、超时或服务器错误。

## What Changes

- 探测结果保存 HTTP 和 HTTPS 的失败摘要，状态图标和文字支持悬停查看。
- HTTP 探测默认使用浏览器格式的 User-Agent。
- 可达性判定：任一请求返回小于 500 的状态码或用户指定的 WAF 状态码 567 即视为在工作，567 保留拦截提示。

## Capabilities

### New Capabilities
- `domain-probe-diagnostics`: 托管域名可达性探测及失败诊断展示。

### Modified Capabilities
无。

## Impact

影响 Provider 探测、HTTP 客户端和托管域名页面；新增可选缓存字段 status_reason，无新增依赖。
