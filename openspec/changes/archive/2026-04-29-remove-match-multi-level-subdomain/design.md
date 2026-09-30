## Context

证书部署管线里有 4 个匹配点会用到"证书域名 vs 目标域名"的判定：

1. `app/services/deploy.py::_collect_matches` —— 决定云资源绑定是否被新证书覆盖（候选筛选）。
2. `app/services/nginx_planner.py::plan_for_renewed_cert` —— 决定 Nginx 静态部署规则是否命中新证书。
3. `app/deploys/huawei.py::_new_cert_covers_old_cert` —— 决定旧 SNI 引用是否能被新证书安全替换（已在前一次提交里硬编码 `match_multi_level_subdomain=False`）。
4. `app/utils/domain_match.match_domain` —— 上述三处共用的工具函数。

历史上 `match_domain` 暴露了第三参数 `match_multi_level_subdomain: bool = False`，并由 `DeployConfig.match_multi_level_subdomain` 配置项驱动。配置项设为 `true` 时，`*.example.com` 会被判定能匹配 `a.b.example.com`。但 RFC 6125 §6.4.3 明确通配符只能匹配单一级 label，浏览器/标准库在 TLS 握手时会拒绝。这个开关任何 `true` 行为都是协议非法。

线上事故：续签 `img.nczx2025.com`（SAN: `nczx2025.com, *.nczx2025.com`）时，候选匹配错误地认为这张证书可以覆盖到 `m.nczx2025.com` 这条云资源绑定，进而进入华为 ELB 部署，并误替换 SNI 引用。前次提交已修复 ELB 替换判定，但候选匹配源头未清理。

## Goals / Non-Goals

**Goals:**
- 删除 `DeployConfig.match_multi_level_subdomain`、`match_domain` 形参以及一切传递路径，让"多级通配匹配"成为编译期不可达。
- `match_domain` 行为锁定为 RFC 6125 §6.4.3：精确匹配 + 单级通配匹配（`wildcard-single`），返回值删除 `wildcard-multi`。
- 默认行为对外不变（默认值就是 `false`），无需迁移指引。
- 同步清理示例配置、docstring 与单元测试中所有相关入口。

**Non-Goals:**
- 不调整 `_new_cert_covers_old_cert` 的覆盖判定算法本身（前次提交已落地）。
- 不引入新的 `domain-matching` spec 的额外能力（目前只承载现有匹配语义的明文化）。
- 不为旧配置加任何"提示已废弃"日志。pydantic v2 默认 `extra='ignore'` 已足够；按项目规则不写防御性代码。

## Decisions

### 1. 完全删除参数 vs 保留参数仅允许 False
**决定**：完全删除形参与配置项。
**理由**：保留参数等同于"防御性兼容代码"，违反项目规则；且会留下"未来谁再写 True 就违法"的钩子。完全删除让编译/导入期就阻断违规调用方。
**备选方案**：把参数标记为 `deprecated`、运行时报错 —— 同样属于防御性代码且增加噪音。

### 2. `match_domain` 返回值简化
**决定**：返回值类型保留 `Optional[str]`，只剩 `"exact"` 与 `"wildcard-single"` 两种命中标签；删除 `"wildcard-multi"` 分支。
**理由**：调用方仅以"是否非 None"作为命中判断，`match_type` 字符串只用于日志与跳过原因展示。线上日志里 `wildcard-multi` 标签将不再出现，但因为 spec 里也不允许出现，这是预期。
**备选方案**：把返回值改成 `bool` —— 会带动调用方改字符串拼接日志，超出本次 change 范围。

### 3. capability 命名
**决定**：新建 `domain-matching` 这一 capability，spec 文件 `specs/domain-matching/spec.md` 用 `## ADDED Requirements`。
**理由**：项目此前没有任何 spec，这是首个落地的 capability；后续候选匹配/SNI 替换/Nginx 规则都会复用同一套语义，spec 化便于以后变更。

### 4. 配置兼容
**决定**：依赖 `AppConfig.model_config = ConfigDict(extra="allow")` 与 `DeployConfig` 的 pydantic v2 默认 `extra='ignore'`，不写迁移代码。
**理由**：`config.yaml` 当前未使用，`config.example.yaml` 同步删除即可。任何残留旧配置都会被静默忽略，且行为等同于 `false`，与 SSL 协议合规结果一致。

## Risks / Trade-offs

- [日志读取断层] 历史日志里"`wildcard-multi`"将不再出现 → 不需要迁移，因其在协议层从一开始就属于错误命中。
- [上游若依赖 True 行为的隐式部署] 任何之前依赖 True 命中才能"匹配上"的部署目标，现在会被显式 skip → 这是修复，不是回退；通过 `deploy.py` 的 `skipped` 列表即可观察到，可由用户改为正确的 SAN 设计来覆盖（添加 `*.<sublevel>.example.com` SAN）。
- [测试覆盖] `tests/test_utils_and_schemas.py:42` 当前断言 `match_multi_level_subdomain=True` 行为 → 改为断言"多级子域不命中"，并在去掉形参后保持单元覆盖度。

## Migration Plan

1. 提交本 change（删形参、删配置、删示例、删测试断言、清理 docstring）。
2. 启动验证：跑 `pytest tests/`，全绿后即可发布。
3. 回滚策略：单提交回滚（`git revert`）。无数据迁移、无外部 API 变更。

## Open Questions

无。
