# Proposal

## Why

续签先根据采集快照改写域名，再查询 acme.sh，可能将 `api.xx.hj.com` 逐级映射到 `hj.com`，续错证书。自动续签、手动续签和已有证书部署需要统一使用真实的 acme.sh 证书标识。

## What Changes

- 删除采集快照推导续签主域名的逻辑，直接匹配原始目标。
- 按同名 Main_Domain、精确 SAN、单级通配覆盖顺序选择已有证书。
- 无覆盖证书立即报错，停止该目标的续签与部署。
- 固定解析结果，重试、冷却、通知和手动部署补偿统一使用该 Main_Domain。
- 保留原始目标和解析后主域名的黑名单检查。

## Capabilities

### New Capabilities

无。

### Modified Capabilities

- `domain-matching`: 增加续签证书选择、无匹配失败和证书标识一致性要求。

## Impact

影响 `app/utils/acme_sh.py`、`app/services/renew.py`、`app/services/renew_flow.py` 及相关测试与流程文档。复用现有匹配工具，无新增运行依赖，不执行真实证书操作。
