# domain-matching Specification

## Purpose

定义证书域名与目标域名的匹配能力，统一被云部署候选匹配（`app/services/deploy.py`）、Nginx 静态规则匹配（`app/services/nginx_planner.py`）以及华为 ELB SNI 安全替换判定（`app/deploys/huawei.py`）共享。语义严格遵循 RFC 6125 §6.4.3 的单级通配符规则，禁止暴露任何"放宽到多级子域"的开关，从而避免在部署期产出事实上无效的 TLS 证书引用。

## Requirements
### Requirement: 单级通配符匹配语义

证书域名集合与目标域名的匹配 SHALL 严格遵循 RFC 6125 §6.4.3 的单级通配符语义：通配符 `*.<suffix>` 仅匹配 `<suffix>` 之上紧邻的一级 label，不匹配任何深于一级的子域。

#### Scenario: 精确匹配命中
- **WHEN** 证书域名集合为 `{"example.com", "*.example.com"}` 且目标域名为 `example.com`
- **THEN** 匹配结果非空，命中标签为 `exact`

#### Scenario: 单级通配命中
- **WHEN** 证书域名集合包含 `*.example.com` 且目标域名为 `api.example.com`
- **THEN** 匹配结果非空，命中标签为 `wildcard-single`

#### Scenario: 多级子域不命中
- **WHEN** 证书域名集合包含 `*.example.com` 且目标域名为 `a.b.example.com`
- **THEN** 匹配结果为空（None），不命中

#### Scenario: 多级泛域名不被父级泛域名覆盖
- **WHEN** 证书域名集合为 `{"nczx2025.com", "*.nczx2025.com"}` 且目标域名为 `*.m.nczx2025.com`（作为另一证书的代表泛域名）
- **THEN** 匹配结果为空（None），父级泛域名不视为覆盖更深一级的泛域名

#### Scenario: 域名归一化大小写与尾点
- **WHEN** 证书域名集合为 `{"example.com", "*.example.com"}` 且目标域名为 `API.Example.com.`
- **THEN** 匹配结果非空，命中标签为 `wildcard-single`

### Requirement: 匹配 API 不暴露多级通配开关

`app/utils/domain_match.match_domain` 函数签名 SHALL 仅接受 `cert_domains` 与 `target_domain` 两个参数，禁止暴露任何"启用多级通配"的开关；上层调用方（云部署候选匹配、Nginx 规则匹配、SNI 覆盖判定）SHALL 通过此函数获取唯一一致的匹配语义。

#### Scenario: 调用方仅需提供两个参数
- **WHEN** 调用 `match_domain(cert_domains, target_domain)`
- **THEN** 函数返回 `Optional[str]`，命中时为 `"exact"` 或 `"wildcard-single"` 之一，未命中时为 `None`

#### Scenario: 不接受额外开关参数
- **WHEN** 任意调用方尝试传入 `match_multi_level_subdomain=True`
- **THEN** Python 解释器在导入或调用时立即抛出 `TypeError`，使违规调用在编译/启动期暴露，而不是运行时静默放行

### Requirement: 配置文件不暴露多级通配开关

应用配置 SHALL 不再提供 `deploy.match_multi_level_subdomain` 字段；任何遗留写法将被 pydantic v2 默认行为静默忽略，且不会改变实际匹配语义。

#### Scenario: 标准配置不再列出该字段
- **WHEN** 用户参考 `config.example.yaml` 编写部署配置
- **THEN** 文件内不出现 `match_multi_level_subdomain` 任何形式的键

#### Scenario: 历史配置中残留键被忽略
- **WHEN** 旧 `config.yaml` 中遗留 `deploy.match_multi_level_subdomain: true`
- **THEN** 应用启动成功，加载到的 `DeployConfig` 实例不包含该属性，运行行为与未配置等价（即遵守 RFC 6125 §6.4.3 单级通配语义）

