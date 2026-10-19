## Purpose

为证书上传前提供独立的 Let's Encrypt / ZeroSSL 证书链补全能力，识别缺失的中间 CA 和交叉签名证书，生成顺序正确、去重且通过验证的证书链，并提供明确的失败信息。

## ADDED Requirements

### Requirement: 自动补全官方证书链
工具 SHALL 识别 YE1–YE3、YR1–YR3、R10–R14 和 E5–E9，将唯一叶证书补到 X1；只从固定官方 HTTPS 地址下载缺失 CA 并校验指纹，返回缺失项。

#### Scenario: YE 链缺少交叉签名 CA
- **WHEN** 输入只有 YE1 叶证书与中间证书
- **THEN** 补入 Root YE 和 X2 交叉签名证书，以官方 X1 验证

#### Scenario: 下载失败或证书不匹配
- **WHEN** 下载超时、HTTP 失败或官方 CA 指纹不匹配
- **THEN** 明确报错且不生成成功输出

### Requirement: 规范化输出
工具 SHALL 按指纹去重并输出叶证书到对应自签根证书的有序 PEM，末尾必须包含且仅包含一份对应官方自签根；拒绝非法输入和多叶证书，CLI 不覆盖已有文件。

#### Scenario: 乱序且重复的完整链
- **WHEN** 输入证书顺序混乱且包含重复证书与自签根
- **THEN** 输出唯一有序的 fullchain，保留所需交叉签名证书并以唯一的对应自签根结束

### Requirement: 输出前验证
工具 SHALL 使用 Python 库、对应独立官方自签根信任锚及 TLS 服务端用途验证，可选验证域名；证书过期、签名无效或扩展约束不满足时必须报错，不依赖 OpenSSL 命令。

#### Scenario: 签名不匹配
- **WHEN** 叶证书 issuer 名称正确但签名不匹配
- **THEN** 验证失败且不输出补链成功结果

#### Scenario: 正确链
- **WHEN** 叶证书有效且所有 CA 正确
- **THEN** 返回 fullchain、缺失 CA 列表及 库验证结果

### Requirement: 部署前统一准备本地证书
系统 SHALL 在正式部署入口对已识别的 Let's Encrypt / ZeroSSL 证书执行一次补链和私钥匹配验证，使用实际选定的本地 fullchain 文件；验证失败或文件已被外部修改时终止上传。完整链无需下载、写回或备份；发生变化时先备份再原子更新，所有平台共享更新后的内存证书。其他 CA 保持原部署行为，dry-run 不写回。

#### Scenario: 手动续签后的本地链缺少根
- **WHEN** 仅部署已有证书且实际 fullchain 缺少 X1
- **THEN** 备份并补齐该文件，所有平台上传相同的完整链

#### Scenario: 后续部署使用已补齐文件
- **WHEN** 本地链已完整且验证通过
- **THEN** 不再次下载、备份或更新文件

#### Scenario: 文件发生并发变化
- **WHEN** 本地链与加载快照不同或写回前发生外部变动
- **THEN** 中止本轮部署且不覆盖外部变动

### Requirement: ZeroSSL 新旧版证书链
工具 SHALL 支持 ZeroSSL RSA/ECC Domain Secure Site CA 和 ZeroSSL RSA/ECC DV SSL CA 2；旧版补到对应 USERTrust 自签根，新版经 R46/E46 交叉签名证书补到对应 USERTrust 自签根。只从固定官方 HTTPS 地址下载并校验 DER 指纹；未知 ZeroSSL 签发 CA 必须报错。

#### Scenario: 新版 ZeroSSL 缺少交叉签名 CA
- **WHEN** 输入仅含 ZeroSSL ECC DV SSL CA 2 签发的叶证书
- **THEN** 补入 ECC DV SSL CA 2、E46 交叉签名证书和 USERTrust ECC 自签根，验证通过后输出

#### Scenario: 两类 CA 部署行为一致
- **WHEN** 部署已识别的 ZeroSSL 或 Let's Encrypt 本地证书
- **THEN** 均执行一次预处理，仅有变化时备份写回，并让全部平台共享完整链

### Requirement: 集中存放补链备份
系统 SHALL 在需要写回时将原证书备份到实际证书所在目录的 bak 子目录，文件名包含原名和唯一标识；目录或备份创建失败 MUST 保持原证书不变。已完整链不创建新备份。

#### Scenario: 首次补齐和再次部署
- **WHEN** 首次补齐后重新从磁盘读取同一完整证书部署
- **THEN** bak 下仅保留首次备份，后续不下载 CA、不写回证书且不新增备份

### Requirement: 官方 CA 目录周期检查
系统 SHALL 按可配置周期核对已有 CA 条目的官方证书指纹、有效期与路径签名，仅保存检查报告，不自动改写 CA JSON。相同问题通知去重，恢复后通知；网络失败 MUST 标为核对失败且不影响正常采集。

#### Scenario: 官方证书变化或即将过期
- **WHEN** 官方下载证书与固定指纹不符或进入有效期预警窗口
- **THEN** 保存问题并通过现有通知渠道提醒，不改写目录，相同问题后续不重复通知

#### Scenario: 周期内重启
- **WHEN** 上一次检查距今未达到配置周期
- **THEN** 不重复下载检查，使用持久化时间控制周期
