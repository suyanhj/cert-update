## Context

Provider._request 只返回状态字符串，页面无法取得失败原因。HTTP 客户端使用 HEAD、跟随重定向，默认 UA 为 curl。

## Goals / Non-Goals

提供采集时的失败摘要；维持状态判定、超时配置与 TLS 证书扫描行为。

## Decisions

- _request 返回状态及原因元组，_probe_domain 写入 status_reason，与已有子域字典一起持久化。
- 汇总两种协议的 HTTP 5xx 或异常类型与简短描述，限制异常文本长度；普通成功及通配符跳过的原因清空。567 按用户的 WAF 约定视为可达，保留拦截提示，不受 HTTP 版本影响。
- 页面复用 NiceGUI tooltip，图标与徽章均可查看；可达但带有 WAF 提示的结果也展示 tooltip。旧缓存提示重新扫描获取原因。
- HTTP 客户端默认 UA 使用桌面浏览器格式，沿用现有 ua 参数可覆盖。

## Risks / Trade-offs

- 历史缓存没有原因 → 提示重新扫描。
- WAF 返回 403 仍属于现有可达性成功语义 → 保留判定，提示描述为可达性检查而非业务可用性。
- 浏览器 UA 无法模拟 TLS 指纹 → 不保证绕过所有 WAF 规则。
