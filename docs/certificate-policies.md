# 证书来源与自动续签

`providers[].type: custom` 表示手工录入域名。证书如何获取由顶层 `certificate_policies` 决定，对云平台发现和手工录入的域名都有效。

## 手动同步的证书

```yaml
providers:
  - name: 手工域名
    type: custom
    enabled: true
    static_domains:
      - domain: example.com
        sub_domains: ['@', www]

certificate_policies:
  example.com:
    source: manual
    fullchain_path: /etc/nginx/certs/example.com.pem
    key_path: /etc/nginx/certs/example.com.key
  '*.example.com':
    source: manual
    fullchain_path: /etc/nginx/certs/example.com.pem
    key_path: /etc/nginx/certs/example.com.key
```

文件必须在 **CRT 进程所在环境** 可读；容器运行时需要挂载相应目录。相对路径以 `script/py/crt` 为基准，推荐绝对路径。完整链 PEM 第一张必须是域名证书，后面跟随中间证书；私钥使用未加密 PEM。

保存配置后刷新大盘，证书池会显示“手动证书 · 仅部署”。即使没有线上 TLS 探测结果，也会显示配置行；时间等探测信息显示“未探测”。点击“仅部署”时才读取本次同步的文件，检查 SAN 覆盖、有效期、公私钥匹配及 PEM 格式，失败立即报错，不回退 acme.sh。现有证书池时间和 SAN 来自线上探测，不代表本地待部署文件。

部署使用现有 Nginx/云产品匹配规则；`deploy.mode: dry-run` 生成计划，`apply` 执行部署。manual 自动续签固定关闭，UI 隐藏签发和续签，后端也会跳过。来源切换只需显式修改配置，程序不会按文件存在与否猜测。

## 只关闭自动续签

```yaml
certificate_policies:
  example.com:
    source: acme
    auto_renew: false
```

此设置允许 UI 手动签发、强制续签、仅部署，自动任务记录跳过原因。默认未配置时是 `source: acme`、`auto_renew: true`；全局 `auto_renew_enabled: false` 仍关闭全部自动续签。到期探测和告警继续正常执行。

**共享 ACME 证书应以 acme.sh 的 Main_Domain 配置开关。** 自动任务会先检查所探测域名，再检查实际 Main_Domain；若只关闭某个子域名，其他子域名仍可能触发共享证书续签。

黑名单独立且优先，命中后所有签发、续签、仅部署入口都跳过。

## 自定义域名通过 ACME 签发

```yaml
certificate_policies:
  example.com:
    source: acme
    auto_renew: true
    dns_provider: aliyun-dns
```

`dns_provider` 引用 `providers` 中已有启用的 aliyun/tencent/cloudflare 账号，复用该账号的标准凭据。该账号必须实际拥有域名 DNS 的操作权限。通配 SAN `*.example.com` 的 DNS 账号未单独指定时，使用基础域名 `example.com` 的策略账号；其他 SAN 要各自能解析到相同账号。策略 DNS 账号优先于自动发现映射，不将 DNS 账号自动作为部署目标限制。

## DNS 厂商迁移后的续签

先采集确认当前 DNS 账号，或通过 `dns_provider` 明确引用账号。续签会比较 acme.sh 保存的 `Le_Webroot` 与当前插件：一致时继续 `--renew`；不一致时执行 `--issue --dns <当前插件> --force`，显式传入原主域名和全部 SAN，其余配置交由 acme.sh 管理；签发成功后使用 --install-cert 将新证书安装到原 Le_RealKeyPath、Le_RealFullChainPath。单纯在 `--renew` 中追加 `--dns` 无法切换保存的验证方式。

迁移会实际重新签发，日志提示旧插件和新插件。原信息不完整或各 SAN 对应不同 DNS 账号时提前失败。迁移成功后 acme.sh 保存新插件，后续调用回到正常续签。程序不直接编辑 acme.sh 配置文件。

## 匹配及校验

策略域名忽略大小写及末尾点，精确规则优先；`*.example.com` 只覆盖直属一级子域及该通配项，不覆盖 `example.com` 或 `a.b.example.com`。普通根域策略不隐式继承到子域，应按需要显式配置。

manual 必须配置两个文件路径，不能开启自动续签或指定 DNS 账号；acme 不接受手动路径。未知字段、归一化后重复域名、无效 DNS 账号和引用账号缺少凭据均在加载配置时拒绝。文件内容在每次部署时读取校验，配置保存不触发签发或部署。
