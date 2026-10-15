# Proposal

## Why

自定义域名到期时间为空时可能触发日期解析错误，用户希望未知到期时间和注册主体可以直接留空。

## What Changes

- 静态域名 expires_at 和 registrant_org 缺省、null、空字符串及纯空白统一为 unknown。
- 已填写的日期和注册主体保持原值。

## Capabilities

### New Capabilities

- `static-domain-config`: 手工录入域名的未知字段归一化。

### Modified Capabilities

无。

## Impact

StaticDomainConfig、配置示例、针对性测试；不改云平台 API 采集结果。
