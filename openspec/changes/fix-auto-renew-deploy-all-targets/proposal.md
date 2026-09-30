## Why

自动续签在达到阈值后会携带 DNS provider 进入续签编排，但该参数同时被错误地用作部署范围过滤，导致续签成功后只部署到同名云厂商，无法像 Web 强制续签一样覆盖全部匹配目标。

## What Changes

- 将证书签发/续签所需的 DNS provider 与证书部署范围解耦。
- 自动续签成功后复用 Web 强制续签的全量匹配部署语义，覆盖全部云厂商和 Nginx 目标。
- 保留手工指定单一部署 provider 的能力，并增加自动续签跨 provider 部署的回归测试。

## Capabilities

### New Capabilities

- `auto-renew-deployment`: 定义自动续签成功后对全部匹配目标执行证书部署的行为。

### Modified Capabilities

无。

## Impact

- 影响 `app/services/renew_flow.py` 中续签与部署的参数边界。
- 影响自动续签调用链，但不改变 Web 强制续签当前可用行为。
- 增加续签编排和自动扫描相关测试，不引入新依赖或配置项。
