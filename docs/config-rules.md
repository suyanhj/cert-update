# 配置解析与去重规则

本文说明 `app/config/__init__.py` 的运行时配置处理规则。
术语固定写法见 `README.md` 的“术语统一（固定写法）”。

## 1. 运行时配置视图生成顺序

1. 读取当前配置文件（`CONFIG_PATH`）。
2. 递归展开 `extends`。
3. 深度合并 `dict` 字段。
4. 对非 `dict` 字段（含 `list`）使用“子配置覆盖父配置”。
5. 对合并结果执行列表去重归一化。
6. 使用 `AppConfig` 进行校验。

## 2. `extends` 解析规则

- **格式**：允许字符串或字符串列表；为空或空字符串时视为无 extends；列表内非字符串会抛 `ValueError`。
- **路径解析顺序**：
  1. 绝对路径：直接使用。
  2. 当前文件同级存在：使用同级相对路径。
  3. 否则回退到项目根目录解析（`resolve_project_path`）。
- **找不到父文件**：若同级与项目根都不存在，会按同级路径返回，最终在读取父文件时抛 `FileNotFoundError`。
- **循环检测**：发现循环引用时抛 `ValueError("Config extends cycle detected: ...")`。
- **合并时机**：父配置先合并，再覆盖当前文件内容（`extends` 字段本身不参与合并）。

## 3. 合并规则

- **dict 与 dict**：递归合并。
- **其他类型（含 list）**：子配置覆盖父配置。

## 4. 去重规则（后定义覆盖前定义）

以下列表字段在合并后会去重：

- `providers`：按 `name`（大小写不敏感）
- `providers[].static_domains`：按 `domain`（大小写不敏感）
- `providers[].sub_extensions`：按 `domain`（大小写不敏感）
- `providers[].static_domains[].sub_domains`：按文本去重（大小写不敏感）
- `ssh_profiles`：按 `name`（大小写不敏感）
- `whitelist`：按文本去重（大小写不敏感）
- `nginx.nginx_hosts`：按 `name`（大小写不敏感）
- `nginx.nginx_target_groups`：按 `name`（大小写不敏感）
- `nginx.nginx_deploy_rules`：按 `(target_group.lower, cert_name_template, 排序后的 cert_domains.lower)` 生成规则键

同一逻辑键重复时，后出现的配置覆盖前面的配置。

## 5. 原子更新行为

`update_config_atomic` 的处理顺序：

1. 对传入的子配置先做归一化与去重。
2. 仅用于校验时展开父配置，生成运行时配置视图。
3. 最终只把归一化后的子配置原子写回磁盘。

这样可避免父级示例配置被误写入运行配置文件。

## 6. DNS 托管冲突人工覆盖

- `domain_dns_overrides` 是根域名到 `providers[].name` 的映射，域名会自动转为小写并移除末尾点，Provider 名称会移除首尾空格。
- 仅在同一域名被多个 DNS Provider 同时发现，且需要人工固定实际托管账号时配置。例如：`domain_dns_overrides: {example.com: cloudflare-demo}`。
- 人工覆盖优先于公网 NS 判断；指定账号必须是当前扫描结果中的唯一 DNS 候选，否则采集失败并保留上一轮完整映射，避免把证书签发到错误平台。

## 7. 常见排障

- 现象：启动时报 `InvalidAccessKeyId.NotFound`。
- 常见原因：当前生效子配置中存在无效 provider 凭证。
- 排查方式：查看运行时配置视图，不要只看父级示例文件。

## 8. 关联文档

- 当前实现状态：`docs/README.md`
- 流程总览：`docs/flow-runtime.md`
