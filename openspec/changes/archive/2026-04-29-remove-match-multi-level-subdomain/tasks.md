## 1. 工具层与 schema 清理

- [x] 1.1 删除 `app/utils/domain_match.py::match_domain` 的 `match_multi_level_subdomain` 形参与对应分支（含返回值 `wildcard-multi`），仅保留 `exact` 与 `wildcard-single` 两种命中标签
- [x] 1.2 删除 `app/schemas/config.py::DeployConfig.match_multi_level_subdomain` 字段

## 2. 调用方清理

- [x] 2.1 修改 `app/services/deploy.py::_collect_matches`：删除 `match_multi_level = bool(self.conf.deploy.match_multi_level_subdomain)` 局部变量与 `match_domain(...)` 调用中的传参
- [x] 2.2 修改 `app/services/nginx_planner.py`：删除 `match_multi = bool(getattr(getattr(self.conf, "deploy", None), "match_multi_level_subdomain", False))` 与 `match_domain(...)` 调用中的传参
- [x] 2.3 清理 `app/deploys/huawei.py::_new_cert_covers_old_cert` docstring 中关于 `deploy.match_multi_level_subdomain` 配置的解释段落，调用处去掉显式 `match_multi_level_subdomain=False` 关键字参数

## 3. 配置示例清理

- [x] 3.1 删除 `config.example.yaml` 中 `match_multi_level_subdomain: false` 这一行（含其上方注释，若存在）

## 4. 测试清理与回归

- [x] 4.1 修改 `tests/test_utils_and_schemas.py`：删除 `match_multi_level_subdomain=True` 的断言；新增"多级子域不命中"与"匹配 API 仅接受两个参数"两条断言，覆盖 spec 中 ADDED Requirements 的关键场景
- [x] 4.2 修改 `tests/test_services_and_planner.py`：删除三处 `"match_multi_level_subdomain": False` 字典键
- [x] 4.3 运行 `python -m pytest tests/`，确认 28 项 huawei elb 测试 + 其他用例全部 PASS

## 5. 收尾

- [x] 5.1 执行 `openspec validate remove-match-multi-level-subdomain --strict`，全绿
- [ ] 5.2 通知用户审阅本 change，等待确认后进入 apply 阶段
