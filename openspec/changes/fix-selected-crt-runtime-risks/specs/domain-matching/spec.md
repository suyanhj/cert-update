# Spec Delta

## ADDED Requirements

### Requirement: TLS 探测单层通配
TLS 探测 SHALL 复用统一单级通配语义，只允许通配符匹配一个完整域名标签；不得通过去除目标域名前缀扩大覆盖范围。

#### Scenario: 深层后缀的直接子域
- **WHEN** 证书域名为 *.xx.hj.com，探测 a.xx.hj.com
- **THEN** 匹配成功

#### Scenario: 父域和多级子域
- **WHEN** 证书域名为 *.xx.hj.com，探测 hj.com、xx.hj.com 或 a.b.xx.hj.com
- **THEN** 均不匹配
