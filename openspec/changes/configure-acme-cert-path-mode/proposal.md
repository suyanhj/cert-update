## Why

当前 acme.sh 证书路径选择规则隐含在代码中，无法明确要求只使用 acme.sh 默认目录或 `--install-cert` 记录的自定义路径，而且证书与私钥分别回退时存在混用不同来源文件的风险。需要把路径策略配置化，并明确源文件名不影响部署到目标主机时的文件名。

## What Changes

- 在 `acme` 配置中增加 `certificate_path_mode`，允许 `auto`、`default`、`custom` 三个值，默认 `auto`。
- `auto` 按现有兼容顺序成对选择自定义路径、acme.sh 记录路径、默认目录路径；仅当一对证书与私钥均可用时才采用。
- `default` 仅使用 `DOMAIN_CONF` 所在的 acme.sh 证书目录中的 `fullchain.cer` 与 `<Le_Domain>.key`。
- `custom` 仅使用 `Le_RealFullChainPath` 与 `Le_RealKeyPath`，字段或文件缺失时立即失败。
- 读取 `fullchain.cer` 时不修改 acme.sh 源文件；Nginx SSH 目标可按现有模板机制落盘为 `{main_domain}.pem` 与 `{main_domain}.key`，示例配置默认展示该命名。
- 增加配置、路径选择、失败行为和目标主机命名测试，并更新文档。

## Capabilities

### New Capabilities

- `acme-certificate-path-selection`: 定义 acme.sh 证书来源模式、成对路径校验、兼容回退和目标主机文件命名行为。

### Modified Capabilities


## Impact

- 配置模型与示例：`app/schemas/config.py`、`config.example.yaml`
- acme.sh 证书加载：`app/utils/acme_sh.py`
- Nginx SSH 部署示例与相关文档
- acme.sh、配置和 Nginx 规划单元测试
- 不修改 acme.sh 源证书文件，不影响云平台按 PEM 内容上传证书的接口
