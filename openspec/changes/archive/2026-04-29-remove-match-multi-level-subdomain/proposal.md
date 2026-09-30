## Why

`deploy.match_multi_level_subdomain` 配置开关一旦置为 `true`，会把通配证书 `*.example.com` 当作能匹配 `a.b.example.com`，进而被选作部署候选并下发到云资源/Nginx。但按 RFC 6125 §6.4.3，TLS 握手时通配符只能匹配单一级 label，浏览器请求 `a.b.example.com` 必然校验失败。也就是说，这条路径的"成功匹配"在协议层面等同于"主动制造线上 SSL 故障"，是危险且不合规的能力。线上已经因为相关误判出现过 SNI 引用被错误覆盖的事故（华为 ELB `*.m.nczx2025.com` 被 `*.nczx2025.com` 替换），上游候选匹配再保留这把刀只会继续埋雷。

## What Changes

- **BREAKING** 删除 `DeployConfig.match_multi_level_subdomain` 配置字段（默认 `false`，无人显式启用，按 pydantic v2 默认 `extra='ignore'` 旧配置会被静默忽略，不会启动失败）。
- **BREAKING** 删除 `app/utils/domain_match.match_domain` 函数的 `match_multi_level_subdomain` 形参，函数行为固定为 RFC 6125 §6.4.3 的单级通配符语义。
- 删除 `app/services/deploy.py`、`app/services/nginx_planner.py` 中所有读取与传递该开关的代码。
- 清理 `app/deploys/huawei.py` 中 `_new_cert_covers_old_cert` docstring 内对该配置的解释（参数没了就不必再说"无关"）。
- 删除 `config.example.yaml` 中 `match_multi_level_subdomain: false` 的示例项。
- 同步清理 `tests/test_utils_and_schemas.py`、`tests/test_services_and_planner.py` 中对该参数/配置项的引用。

## Capabilities

### New Capabilities
- `domain-matching`: 证书域名与目标域名的匹配语义（精确匹配、单级通配匹配），严格遵循 RFC 6125 §6.4.3，被云部署候选匹配、Nginx 静态规则匹配、华为 ELB SNI 覆盖判定共用。

### Modified Capabilities
<!-- 暂无既有 spec，全部为新建 -->

## Impact

- 受影响代码：
  - `script/py/crt/app/utils/domain_match.py`
  - `script/py/crt/app/schemas/config.py`
  - `script/py/crt/app/services/deploy.py`
  - `script/py/crt/app/services/nginx_planner.py`
  - `script/py/crt/app/deploys/huawei.py`（注释清理）
  - `script/py/crt/config.example.yaml`
  - `script/py/crt/tests/test_utils_and_schemas.py`
  - `script/py/crt/tests/test_services_and_planner.py`
- 受影响 API：`match_domain` 公开签名变更（去掉第三参数）。仓内只有内部调用方，外部不暴露。
- 配置兼容性：`extra='ignore'` 兜底，旧 `config.yaml` 即便残留 `match_multi_level_subdomain: true` 也不会启动失败，但该配置完全失效（与协议合规结果一致）。
- 运行行为：默认行为 (`false`) 不变；任何曾经依赖 `true` 行为的部署路径将退化为单级匹配，可能导致原本"勉强匹配上"的多级子域绑定被显式跳过——这正是需要的安全行为。
