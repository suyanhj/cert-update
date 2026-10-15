## Purpose

为云平台发现及手工录入的域名统一管理证书来源和自动续签策略，使运维人员可以使用外部同步的证书进行部署，并能关闭自动续签而继续保留手动操作能力。

## ADDED Requirements

### Requirement: 显式选择证书来源
系统 SHALL 支持按域名配置 acme 或 manual 来源，精确域名优先于单级通配规则；无配置时使用 acme 并开启自动续签。非法字段、来源及 DNS 账号引用 MUST 在配置加载时拒绝。

#### Scenario: 默认与匹配边界
- **WHEN** 域名未匹配策略，或多级子域仅存在不覆盖它的通配规则
- **THEN** 保持默认 acme 行为，不继承父域策略

### Requirement: 自动续签开关独立于手动操作
系统 SHALL 在自动流程对请求域名及实际证书主域名检查 auto_renew，关闭后跳过续签且记录原因，手动签发、续签和仅部署仍允许。manual 来源 MUST 跳过所有签发和续签。

#### Scenario: 主证书关闭自动续签
- **WHEN** 子域名解析到 auto_renew=false 的证书主域名
- **THEN** 自动流程跳过，不产生续签失败记录或调用签发命令

#### Scenario: 黑名单优先
- **WHEN** acme 或 manual 来源域名命中黑名单
- **THEN** 签发、续签和仅部署均跳过

### Requirement: 手动证书仅部署
manual 来源 SHALL 直接读取配置的完整链和私钥，不依赖 acme.sh 或 DNS 凭据。文件缺失、PEM 无效、私钥不匹配、证书未生效或过期、SAN 不覆盖目标时 MUST 在部署前失败，不回退其他来源。

#### Scenario: 无 acme 的手动证书部署
- **WHEN** 本地证书和私钥有效，且未在 acme.sh 托管
- **THEN** 仅部署使用该证书执行现有 dry-run 或 apply 流程

### Requirement: 自定义域名引用 DNS 账号
acme 来源 SHALL 允许引用已有启用的受支持 DNS Provider 进行签发及续签，所有签发域名的 DNS 账号 MUST 一致。

#### Scenario: 静态域名使用阿里云 DNS
- **WHEN** custom 域名策略引用 aliyun 账号
- **THEN** acme 使用该账号的标准凭据映射，部署目标仍遵守原部署范围

### Requirement: UI 显示操作能力
UI SHALL 显示证书来源和自动续签状态；manual 来源隐藏签发和续签并提供仅部署入口，即使没有线上 TLS 探测结果也能操作。

#### Scenario: 手动证书首次部署
- **WHEN** manual 域名没有证书探测记录
- **THEN** UI 仍显示该配置及仅部署入口，部署时重新读取并校验文件
