## Purpose

为托管域名的 HTTP 与 HTTPS 可达性探测提供可查看的失败诊断，让运维人员能从页面直接了解探测失败的协议、状态码或连接异常，并区分该结果与独立的 TLS 证书扫描结果。

## ADDED Requirements

### Requirement: 保存失败诊断
系统 SHALL 在两种协议探测均失败时保存每种协议的失败摘要；HTTP 5xx（除 567）保存状态码，连接异常保存异常类型与有界描述。任一协议返回小于 500 的状态码 SHALL 保持在工作状态且清空失败原因。

#### Scenario: 超时与服务器错误
- **WHEN** HTTP 返回 502 且 HTTPS 连接超时
- **THEN** 域名状态为异常，原因同时包含 HTTP 502 与 HTTPS 超时类型

#### Scenario: 回退成功
- **WHEN** HTTP 失败但 HTTPS 返回 200
- **THEN** 域名状态为在工作且无失败原因

### Requirement: 页面展示失败原因
系统 SHALL 在异常图标与状态文字上提供悬停提示；旧缓存无原因时 SHALL 提示重新扫描获取详细原因。

#### Scenario: 查看异常
- **WHEN** 用户悬停在异常感叹号或状态文字上
- **THEN** 展示该次探测原因；若缓存缺少原因则展示重新扫描提示

### Requirement: WAF 567 视为可达
系统 SHALL 将用户指定的 WAF 状态码 567 判定为在工作，无论响应使用 HTTP/1.1 还是 HTTP/2，并保存与展示 WAF 拦截提示；其他 5xx SHALL 保持失败处理。

#### Scenario: WAF 拦截响应
- **WHEN** HTTP 或 HTTPS 请求返回 567
- **THEN** 状态为在工作，图标及状态文字提示 WAF 拦截且服务器可响应

### Requirement: 浏览器格式请求标识
HTTP 可达性探测 SHALL 默认携带桌面浏览器格式的 User-Agent。

#### Scenario: 默认探测
- **WHEN** 使用默认参数发送可达性请求
- **THEN** HTTP 与 HTTPS 均使用浏览器格式的 User-Agent
