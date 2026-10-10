# Spec Delta

## ADDED Requirements

### Requirement: 原始目标的续签证书选择

系统 SHALL 以原始目标匹配 acme.sh 已有证书，优先选择同名 Main_Domain，其次选择精确 SAN 覆盖，再选择单级通配覆盖。采集快照 SHALL 不改写目标；选中的 Main_Domain SHALL 不再参与覆盖匹配。

#### Scenario: 子域使用所属通配证书
- **WHEN** 目标为 `api.xx.hj.com`，已有 `hj.com / *.hj.com` 和 `xx.hj.com / *.xx.hj.com` 证书
- **THEN** 续签使用 `-d xx.hj.com`，不会继续选择 `hj.com`

#### Scenario: 同名证书优先于父通配
- **WHEN** 目标为 `xx.hj.com`，同名证书存在且父证书包含 `*.hj.com`，采集快照中同名证书没有通配 SAN
- **THEN** 续签使用 `-d xx.hj.com`

#### Scenario: 精确 SAN 优先于通配覆盖
- **WHEN** 目标只作为某证书的精确 SAN 存在，同时另有单级通配证书覆盖目标
- **THEN** 选择精确 SAN 所属证书的实际 Main_Domain

### Requirement: 无覆盖证书立即失败

系统 SHALL 在 acme.sh 没有合法覆盖原始目标的证书时立即报告失败，停止该目标的续签和部署；SHALL 不通过父域回退或自动签发继续操作。

#### Scenario: 只有父域通配证书
- **WHEN** 目标为 `api.xx.hj.com`，acme.sh 只有 `hj.com / *.hj.com`，采集快照声称有子域证书
- **THEN** 报告原始目标无已有证书，不执行续签或部署，也不重复重试同一匹配失败

#### Scenario: 自动续签继续处理其他证书
- **WHEN** 自动续签中一个目标没有覆盖证书而后续目标有合法证书
- **THEN** 失败目标被记录，后续目标仍按正常流程处理

### Requirement: 证书标识一致性

系统 SHALL 在一次续签或已有证书部署流程中固定所选 Main_Domain，续签命令、信息查询、冷却、失败状态、通知和手动部署补偿 SHALL 使用同一标识。原始目标与所选 Main_Domain SHALL 均接受黑名单检查。

#### Scenario: 重试期间保持同一证书
- **WHEN** 目标 `api.xx.hj.com` 选中 `xx.hj.com`，续签命令暂时失败
- **THEN** 重试继续使用 `xx.hj.com`，不再次查询覆盖关系或转到 `hj.com`

#### Scenario: 多个子域共享冷却记录
- **WHEN** `api.xx.hj.com` 和 `web.xx.hj.com` 均选中 `xx.hj.com`，此前该证书续签部署成功且仍在冷却期
- **THEN** 非强制续签查询 `xx.hj.com` 的成功状态并跳过

#### Scenario: 手动部署补偿使用原证书
- **WHEN** `api.xx.hj.com` 对应的 `xx.hj.com` 已续签但部署失败，用户手动部署
- **THEN** 加载 `xx.hj.com` 的已有证书，成功后更新 `xx.hj.com` 的状态

#### Scenario: 主域名命中黑名单
- **WHEN** 原始目标未被禁用但所选 Main_Domain 在黑名单中
- **THEN** 返回跳过结果，不执行续签或部署
