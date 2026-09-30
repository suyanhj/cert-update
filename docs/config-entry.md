# 配置模块（Config）

配置加载、合并、校验与原子更新的唯一入口，由 `app.config` 提供。

## 1. 入口与职责

- **CONFIG_PATH**：默认由 `resolve_project_path("config.yaml")` 得到，指向项目根下 `config.yaml`。
- **set_config_path(path)**：测试或 CLI 指定配置文件路径；不存在则抛 `FileNotFoundError`，不是文件则抛 `ValueError`；成功后更新全局 `CONFIG_PATH` 并打日志。
- **get_config(force_reload=False)**：读取运行时配置视图，返回 `Box`；带内存缓存，`force_reload=True` 时强制重新加载并覆盖缓存。
- **reload_config()**：等价于 `get_config(force_reload=True)`。
- **load_raw_config()**：兼容旧调用入口，等价于 `get_config()`。
- **validate_config_dict(data)**：只做 schema 校验（`AppConfig(**data)`）；不写盘、不改缓存。

## 2. 加载流程（get_config）

1. **读取与展开**：从 `CONFIG_PATH` 读取 YAML，递归展开 `extends` 并合并（详见 `config-rules.md`）。
2. **列表去重**：对 providers/ssh/whitelist/nginx 等列表做归一化去重。
3. **校验**：使用 `AppConfig` 校验；失败统一包装为 `ValueError("Config validation failed: ...")`。
4. **Box 与缓存**：转为 `Box(default_box=True, frozen_box=True)`，写入 `_CONFIG_CACHE`。
5. **应用日志级别**：从 `log_level` 更新全局日志级别；非法值会抛 `ValueError("Invalid log_level in config")`。

## 3. 原子更新（update_config_atomic）

用于 UI/接口提交的子配置（完整或部分）。

1. **归一化**：对 `new_config` 执行列表去重。
2. **校验视图**：用「父配置 + 子配置」合并后的视图做校验，不要求子配置重写父文件所有字段。
3. **原子写盘**：在 `CONFIG_PATH` 同目录创建临时文件，`yaml.safe_dump` 写入后 `os.replace` 覆盖；失败不覆盖原文件。
4. **重新加载**：写盘后调用 `reload_config()` 返回新 `Box`。

## 4. 运行时配置（ConfigRuntime）

UI 或 API 提交 YAML 文本后的解析、校验、落盘与调度重启，由 `app.services.config_runtime` 提供；校验与写盘仍由本模块完成。

- **parse_config_text(config_text) -> dict**：YAML 解析为 dict；非法或根非 dict 抛 `ValueError`（带行号/列号）。
- **apply_config_dict(new_config)**（async）：加锁后依次 校验 → update_config_atomic → reload_config → stop_scheduler → start_scheduler(interval, run_immediately=False)。
- **apply_config_text(config_text)**（async）：parse_config_text 后 apply_config_dict。

**流程图**：[flow-config.svg](flow-config.svg)（配置加载）、[flow-config-runtime.svg](flow-config-runtime.svg)（运行时应用）。

## 5. 华为云 WAF 配置

- Provider 凭证沿用 `access_key_id`、`access_key_secret`、`project_id` 与 `region`。
- `credentials.enterprise_project_id` 可选，默认 `0`；扫描多个已授权企业项目时可配置 `all_granted_eps`，实际部署会使用扫描结果中每个 host 的企业项目 ID。
- `product_scan.huawei.waf_scan` 默认关闭；设置为 `true` 后，WAF 云模式 HTTPS 防护域名会进入产品绑定缓存、dry-run 匹配与 apply 前实时校验。
- IAM 用户至少需要 WAF 防护域名查询、证书创建和证书绑定权限。

## 6. 腾讯云 EdgeOne 配置

- `product_scan.tencent.eo_scan` 默认关闭；设置为 `true` 后，apply 会在证书上传腾讯云 SSL 后实时扫描域名匹配的 EdgeOne 目标。
- EdgeOne 不进入日常产品绑定缓存，部署继续使用腾讯云 SSL `DeployCertificateInstance`，无需配置 EdgeOne 产品 API 凭证或 SDK。
- 腾讯云子账号需要 SSL 证书上传、EdgeOne 可部署实例查询和 SSL 证书部署权限。

## 7. 邮件通知配置

- `email` 为可选 SMTP 通知配置；完整字段与安全建议见 [notifications.md](notifications.md)。
- 推荐设置 `password_env` 并在进程环境变量中保存 SMTP 密码；若同时存在 `password_env` 和 `password`，环境变量优先。
- 未配置 `email` 或设置 `email.enabled=false` 时，不改变钉钉和 Rocket.Chat 的现有行为。
