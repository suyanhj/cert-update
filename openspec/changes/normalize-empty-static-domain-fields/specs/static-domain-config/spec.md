## Purpose

规范手工录入域名时未知到期时间与注册主体的表达，使用户可以在配置文件中留空，同时让采集流程稳定识别未知信息，避免把空日期当作有效时间进行解析。

## ADDED Requirements

### Requirement: 静态域名未知字段归一化
系统 SHALL 将静态域名 expires_at 和 registrant_org 的缺省、null、空字符串及纯空白统一识别为 unknown，非空值 SHALL 保持原值。

#### Scenario: 留空的域名信息
- **WHEN** 用户在 custom 静态域名中将两个字段留空
- **THEN** 配置加载结果为 unknown，采集不会解析空日期

#### Scenario: 保留已填信息
- **WHEN** 用户填写有效到期时间及注册主体
- **THEN** 配置保留填写值

#### Scenario: 采集入口收到未经归一化的空值
- **WHEN** 自定义 Provider 初始化时收到空字段的配置对象
- **THEN** 在开始探测前完成校验与归一化，多账号域名发现不因空日期失败
