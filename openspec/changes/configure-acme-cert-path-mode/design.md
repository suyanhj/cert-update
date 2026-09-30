## Context

`AcmeShRenewer` 当前固定优先读取 `Le_Real*`，再读取 acme.sh 的 `Le_*Path`，最后通过 `DOMAIN_CONF` 推导默认目录。各字段独立回退可能组合出不同来源的证书和私钥。部署链路实际传递 PEM 内容；Nginx SSH 规划器已经支持 `{main_domain}`、`{cert_name}` 和自定义目标路径模板。

## Goals / Non-Goals

**Goals:**

- 通过强类型配置显式选择 `auto/default/custom`，默认保持兼容。
- 证书和私钥始终作为一对候选路径选择，并在读取前检查两者存在。
- 让 `fullchain.cer` 在目标主机按 `{main_domain}.pem` 落盘，而不修改源文件。
- 对选择过程、采用来源和失败原因记录清晰日志。

**Non-Goals:**

- 不移动、复制或重命名 acme.sh 管理目录内的文件。
- 不改变云厂商证书上传接口和 PEM 内容。
- 不新增另一套 Nginx 文件传输机制。

## Decisions

1. 在 `AcmeConfig` 增加 `certificate_path_mode: Literal["auto", "default", "custom"] = "auto"`，由 Pydantic 在启动或配置更新时拒绝非法值。
2. 把路径解析拆成完整候选对：
   - `custom` 候选为 `Le_RealFullChainPath + Le_RealKeyPath`；
   - acme.sh 记录候选为 `Le_FullchainPath + Le_KeyPath`；
   - `default` 候选由 `DOMAIN_CONF` 父目录和 `Le_Domain` 推导为 `fullchain.cer + <Le_Domain>.key`。
3. `auto` 依次选择 custom、记录、default；候选的两个字段必须同时存在且两个文件都存在，否则继续下一候选。`custom/default` 不跨模式回退，缺失时立即抛出带模式和路径信息的错误。
4. `cert_path` 与 `fullchain_path` 统一指向选中的完整链文件，避免 `Le_CertPath` 的 leaf-only 内容被发往 Nginx 或云平台。CA 文件保持可选，仅在对应路径存在时读取。
5. Nginx SSH 仍使用规划器产生的目标路径。示例使用 `{base_dir}/{main_domain}.pem` 和 `{base_dir}/{main_domain}.key`，因此读取源 `fullchain.cer` 后会以 PEM/KEY 名称写入目标主机。

## Risks / Trade-offs

- [旧配置依赖 `Le_CertPath` 的 leaf-only 内容] → 完整链是当前部署目标所需的正确内容，统一使用 fullchain 并用测试固化。
- [`auto` 遇到残缺 custom 记录时转用下一对路径] → 记录警告，且绝不把两种来源混成一对。
- [目标主机已有旧目录式文件名] → 只调整示例和模板建议；用户已有显式 `cert_layout` 保持不变。
- [`default` 无 `DOMAIN_CONF` 或 `Le_Domain`] → 明确失败，不从 `acme_sh_path` 猜测证书目录。

## Migration Plan

1. 未配置时使用 `auto`，保持现有正常配置的优先顺序。
2. 希望固定使用 acme.sh 内部目录的环境设置 `certificate_path_mode: default`。
3. 已执行 `--install-cert` 且希望强制使用部署路径的环境设置 `certificate_path_mode: custom`。
4. Nginx 目标需要主域名平铺命名时，将 `cert_layout` 设置为 `{base_dir}/{main_domain}.pem` 与 `{base_dir}/{main_domain}.key`。

## Open Questions

无。
