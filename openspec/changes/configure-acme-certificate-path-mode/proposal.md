## Why

当前 acme.sh 证书路径选择是隐式的多级回退，使用者无法明确要求只读取 acme.sh 默认目录或 `--install-cert` 记录的自定义路径。将路径策略配置化可以让异常更早暴露，同时保持现有部署兼容性。

## What Changes

- 在 `acme` 配置中新增 `certificate_path_mode`，允许 `auto`、`default`、`custom` 三个值，默认 `auto`。
- `auto` 保持现有路径发现顺序；`default` 只使用 acme.sh 证书目录；`custom` 只使用 `Le_RealFullChainPath` 与 `Le_RealKeyPath`。
- 每种策略按完整的证书/私钥路径对选择，禁止跨来源混用，并在字段缺失或文件不存在时给出明确错误。
- 保持源文件只读；部署到目标主机时继续由 Nginx `cert_layout` 和 `cert_name_template` 决定目标文件名，可将 `fullchain.cer` 内容写为 `{cert_name}.pem`、私钥写为 `{cert_name}.key`。
- 补充示例配置、文档和单元测试。

## Capabilities

### New Capabilities

- `acme-certificate-path-selection`: 定义 acme.sh 证书来源模式、成对校验、默认兼容行为及目标主机文件命名边界。

### Modified Capabilities

无。

## Impact

- 配置模型：`app/schemas/config.py`
- acme.sh 证书加载：`app/utils/acme_sh.py`
- Nginx 部署规划及目标命名测试：`app/services/nginx_planner.py`、相关测试
- 示例与文档：`config.example.yaml`、`docs/services.md`
- 不新增第三方依赖，不修改 acme.sh 管理目录内的源证书文件。
