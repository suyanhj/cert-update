# Spec Delta

## Purpose

提供证书操作的域名黑名单入口控制，使指定域名在手动签发、手动续签、自动续签和仅部署时可以明确跳过整次操作，避免进入后续证书申请与部署流程，同时向操作者提供可识别的跳过原因和日志。

## ADDED Requirements

### Requirement: 黑名单配置及精确匹配
系统 SHALL 提供顶层 blacklist 字符串列表，默认空列表。匹配 MUST 忽略大小写、首尾空格、末尾点，普通条目精确匹配域名；*. 条目匹配同名通配域名及直属一级子域名，不匹配根域或多级子域。非法或空条目 MUST 在配置校验时失败。

#### Scenario: 精确匹配和边界
- **WHEN** 黑名单为 example.com 和 *.example.com
- **THEN** EXAMPLE.COM.、*.example.com 和 api.example.com 命中，deep.api.example.com 不命中

#### Scenario: 缺省与非法配置
- **WHEN** 配置省略 blacklist 或提供非法条目
- **THEN** 省略时保留原有行为，非法条目在启动或保存配置时明确报错

### Requirement: 证书操作入口跳过
系统 SHALL 在签发、手动/自动续签及仅部署入口命中时跳过整次操作。签发检查全部请求域名；续签和仅部署检查入口域名及实际主域名。force MUST NOT 绕过检查。跳过 MUST 不申请证书、不匹配部署、不更新续签状态、不发送结果告警。

#### Scenario: 签发 SAN 命中
- **WHEN** 签发请求中任意域名命中黑名单
- **THEN** 整次签发和后续部署跳过，返回 issue_status=skipped 和 reason=blacklist

#### Scenario: 手动强制与自动续签命中
- **WHEN** 手动或自动续签的入口域名或实际主域名命中，包括 force=true
- **THEN** 返回 renew_status=skipped、retry_count=0 和 reason=blacklist，不进入续签和部署

#### Scenario: 仅部署命中
- **WHEN** 仅部署请求入口域名或实际主域名命中
- **THEN** 返回 renew_status=skipped 和 reason=blacklist，不加载证书或执行部署

#### Scenario: 通配黑名单在证书解析前拦截
- **WHEN** 黑名单为 *.test2.megao.cn，自动续签或仅部署目标为 pc.test2.megao.cn
- **THEN** 返回 skipped、reason=blacklist 和 matched_domain=*.test2.megao.cn，不调用 ACME 解析或续签，不写状态或发送失败告警

### Requirement: 跳过反馈
系统 SHALL 为黑名单跳过返回命中域名和可读消息，UI 显示跳过原因，自动续签日志明确记录跳过；不得显示签发或部署成功。

#### Scenario: UI 及自动续签反馈
- **WHEN** 操作因黑名单跳过
- **THEN** UI 显示黑名单跳过提示，自动入口记录跳过日志
