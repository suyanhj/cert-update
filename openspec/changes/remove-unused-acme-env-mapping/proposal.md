# Proposal

## Why

ProviderConfig 未定义 metadata，正常配置加载后无法使用 acme_sh_env 分支，容易误导凭据配置排查。

## What Changes

- 删除未接入配置模型的自定义映射分支、专用解析方法和 json 导入。
- 保留云平台内置凭据映射和子进程 env 传递。

## Capabilities

### New Capabilities

无。

### Modified Capabilities

无；仅清理不可达代码，设置 skip_specs: true。

## Impact

仅影响 app/utils/acme_sh.py，不改变配置格式或正常签发、续签行为。
