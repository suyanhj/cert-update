# CRT · Certificate Renewal & Deployment

[![Python Tests](https://github.com/suyanhj/cert-update/actions/workflows/certificate-chain.yml/badge.svg?branch=main)](https://github.com/suyanhj/cert-update/actions/workflows/certificate-chain.yml)
[![Python](https://img.shields.io/badge/Python-3.12-blue.svg)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED.svg)](https://docs.docker.com/compose/)

**CRT（Certificate Renewal & Deployment）是一个面向多云环境的 SSL/TLS 证书监控、签发、续签与部署管理工具。**  
它将域名发现、证书有效期探测、到期告警、ACME 自动续签、云产品证书更新和 Nginx SSH 部署整合到一个 Web 控制台中，帮助运维人员集中管理分散在不同云平台和主机上的证书。

项目基于 Python 3.12、NiceGUI、FastAPI 生态组件及 `acme.sh`，支持 Docker Compose 部署，也可在安装依赖后直接运行。

> **生产环境提醒：** 新部署默认使用 `deploy.mode: dry-run`，自动续签默认关闭。请先完成配置并检查部署计划，确认目标资源无误后，再按需启用 `apply` 和自动续签。

## 目录

- [主要功能](#主要功能)
- [支持的平台与部署目标](#支持的平台与部署目标)
- [运行流程](#运行流程)
- [环境要求](#环境要求)
- [Docker Compose 部署](#docker-compose-部署)
- [配置说明](#配置说明)
- [构建镜像与推送仓库](#构建镜像与推送仓库)
- [升级与备份](#升级与备份)
- [本地开发与测试](#本地开发与测试)
- [安全建议](#安全建议)
- [文档导航](#文档导航)
- [常见问题](#常见问题)

## 主要功能

### 1. 域名发现与统一管理

- 从已配置的云账号发现域名、DNS Zone 和解析记录。
- 按域名归并注册商信息与 DNS 托管信息，尽可能识别真实权威 DNS 服务商。
- 展示域名注册到期时间、剩余天数、注册主体及相关子域名。
- 支持静态录入域名，适用于暂未接入 API 的平台或自建服务。
- 支持 DNS 托管冲突时通过 `domain_dns_overrides` 指定实际账号。

### 2. SSL/TLS 证书扫描与到期告警

- 定时对发现的域名进行 TLS 证书探测，提取证书主体、SAN、签发者、有效期及剩余天数。
- 按配置阈值告警证书即将到期或已进入到期风险窗口的情况。
- 支持 HTTP/HTTPS 可达性探测，并展示异常原因摘要。
- 域名注册到期、ECS 实例到期及续费信息可按对应平台能力采集并告警。
- 告警支持白名单、状态去重与恢复通知，避免每轮扫描重复发送同一问题。

### 3. ACME 证书签发与续签

- 通过 `acme.sh` 执行 DNS-01 方式的证书首次签发和续签。
- Web 控制台支持首次签发时编辑主域名及 SAN 列表。
- 支持手动强制续签、续签冷却时间、失败重试及超时控制。
- 支持自动续签编排；是否启用由 `auto_renew_enabled` 和 `auto_renew_days` 控制。
- 支持配置证书来源策略：ACME 证书、手动同步证书；手动证书只读取、校验并部署，不由程序自动续签。
- 支持检测 DNS 验证插件变化，并在符合条件时按当前 DNS Provider 重新签发，避免简单追加插件参数造成续签方式不一致。

**当前首次签发的 DNS 插件映射：** 阿里云、腾讯云和 Cloudflare。其他平台可根据实际能力使用手动证书来源或已有证书部署流程；请以当前配置及相关文档为准。

### 4. 多云证书部署

- 先生成部署计划（`dry-run`），确认匹配目标后再选择实际执行（`apply`）。
- 按账号分组处理目标，尽量复用平台证书上传结果。
- 支持产品绑定缓存，降低重复扫描云 API 的频率；部署前可配置实时存在性校验。
- 将每个目标的成功、失败、跳过原因及汇总结果记录下来。
- 单个平台或账号失败时尽可能继续处理其他目标；部分成功会保留为部分失败结果，便于后续排查。
- 支持域名操作黑名单，阻止匹配域名进入签发、续签或部署流程；强制续签也不能绕过黑名单。
- 支持对已签发但部署失败的证书执行“仅部署”，避免为了重试部署而重复签发。

### 5. Nginx SSH 部署

- 通过 SSH 将完整证书链和私钥下发到指定主机。
- 支持 SSH Profile 复用、密钥或密码认证、目标主机分组和部署规则。
- 支持证书文件路径模板、权限设置、部署前备份、Nginx 配置测试和重载命令。
- 可先执行 dry-run 检查计划，再执行真实文件更新和服务重载。

### 6. 证书链补全与 CA 目录核对

- 部署前可自动识别并补全已支持的 Let's Encrypt / ZeroSSL 证书链。
- 支持处理缺失、重复或乱序的 PEM 证书链；下载 CA 文件前核对固定官方地址与 SHA-256 指纹。
- 使用 Python `cryptography` 验证证书签名、有效期、服务端用途及 WebPKI 约束。
- 补链成功后采用备份与原子替换策略写回实际 fullchain 文件；链已完整时不重复写入。
- 各云平台和 Nginx 部署复用同一份已验证的证书内容。
- 可定时核对配置目录中已登记的官方 CA 文件，发现异常时告警；此功能不会自动改写 CA 信任目录或官方指纹。

当前支持的 CA 路径、验证边界和已知限制请参阅[证书链说明](docs/certificate-chain.md)。

### 7. Web 控制台与运行状态

- 证书池：查看线上探测结果，执行强制续签或仅部署。
- 托管域名：查看域名及 DNS 信息，发起首次签发。
- 配置中心：校验并保存 YAML 配置，支持运行时重新加载及调度器重启。
- 运行状态使用本地快照持久化，服务重启后可恢复上一轮状态。
- 支持钉钉、Rocket.Chat 和 SMTP 邮件通知，可分别启用或组合使用。

## 支持的平台与部署目标

平台的“域名发现”和“证书部署”是不同能力。下表列出当前代码中的主要接入范围；具体资源是否扫描，还取决于账号权限和 `product_scan` 开关。

| 平台 | 主要发现能力 | 证书部署能力 |
|---|---|---|
| 阿里云 | 域名注册、DNS、CDN、OSS、SLB/ALB、ECS（按配置） | CDN、SLB、ALB、OSS |
| 腾讯云 | 域名注册、DNSPod、CDN、云直播、CLB、COS 等 | CDN、云直播、CLB、COS、EdgeOne |
| 华为云 | DNS、CDN、ELB、云模式 WAF、ECS（按配置） | CDN、ELB、云模式 WAF |
| 七牛云 | CDN 域名、Kodo Bucket 加速域名 | CDN、Kodo Bucket 加速域名 |
| Cloudflare | Zone、DNS 记录 | 不负责 Cloudflare 自身 CDN/边缘证书部署；可作为 ACME DNS-01 验证账号 |
| 火山引擎 | DNS 及部分产品发现能力 | 已有部分 CDN/CLB 部署实现，但当前默认部署编排未启用，不能视为开箱即用的完整自动部署链路 |
| Static / Custom | 手动维护域名及子域列表 | 可与已启用的云部署目标或 Nginx SSH 规则组合使用 |
| Nginx / 自建主机 | 由配置中的部署规则指定主机 | SSH 写入证书文件、执行测试和重载命令 |

**说明：**

- 云平台 API 及可部署资源受账号权限、地域、产品类型和配置开关限制。
- 七牛云当前不提供 DNS 解析记录扫描；其 Kodo 原生源站域名与 CDN 加速域名的能力也有所区别。
- 腾讯云 EdgeOne 通过腾讯云 SSL 证书服务发现并部署匹配目标，需启用 `product_scan.tencent.eo_scan`。
- 华为云 WAF 当前针对云模式 HTTPS 防护域名，需启用 `product_scan.huawei.waf_scan`。
- 不要仅凭平台名称判断所有产品均已启用；请先用测试账号和 dry-run 验证实际资源范围。

## 运行流程

```text
云账号 / 静态域名
        │
        ▼
域名、DNS、产品绑定及 ECS 采集
        │
        ▼
TLS 证书探测、到期状态计算与告警
        │
        ├── 未到自动续签阈值 ──► 等待下一轮采集
        │
        ▼
ACME 签发 / 续签，或加载手动证书
        │
        ▼
证书链补全与部署前校验
        │
        ▼
匹配云产品和 Nginx 目标
        │
        ├── dry-run ──► 仅输出部署计划
        │
        └── apply ────► 实际部署、记录结果并通知
```

自动续签和真实部署是两个独立开关。首次安装建议先使用默认配置完成采集和 dry-run，确认匹配结果后再逐步启用。

## 环境要求

- Docker Engine 与 Docker Compose 插件（推荐部署方式）。
- 或 Python 3.12 及可安装 `requirements.txt` 中依赖的运行环境。
- 使用 ACME 签发或续签时，需要安装并持久化 `acme.sh`，并配置可用的 DNS API 凭据。
- 使用云部署时，需要配置对应平台的 API 凭据和最小必要权限。
- 使用 Nginx SSH 部署时，需要目标主机 SSH 连通性、密钥/密码凭据以及目标路径和重载命令权限。
- 生产环境应确保系统时间准确、日志和数据目录可写，并能访问所需的云 API、DNS 和 CA 官方 HTTPS 地址。

## Docker Compose 部署

以下以 Linux 主机的 `/data/cert-monitor` 为例。

### 1. 获取项目并准备目录

```bash
git clone https://github.com/suyanhj/cert-update.git /data/cert-monitor
cd /data/cert-monitor

mkdir -p conf data logs ssh
cp config.example.yaml conf/config.yaml
```

如果目录中已经有自己的配置文件，不要覆盖它；先备份并按需合并新增配置项。

### 2. 配置服务

编辑 `conf/config.yaml`：

```yaml
deploy:
  mode: dry-run

auto_renew_enabled: false
```

以上是首次验证时建议使用的安全设置。然后按需配置：

1. `providers`：云账号、DNS 凭据或静态域名。
2. `product_scan`：启用需要扫描的云产品。
3. `nginx`：配置 SSH Profile、主机、目标组和部署规则。
4. `alert`：配置证书、域名和 ECS 的到期阈值。
5. `dingding`、`rocketchat` 或 `email`：选择需要的通知通道。
6. `acme`：确认容器内 `acme.sh` 的实际路径及命令超时。
7. `deploy.binding_cache`：按需设置绑定缓存与部署前实时校验。

完整配置示例及字段注释见 [config.example.yaml](config.example.yaml)。

### 3. 准备 ACME 与 SSH 文件

- Compose 默认将宿主机 `/root/.acme.sh` 挂载到容器相同路径。请确认宿主机上存在有效的 `acme.sh` 安装和证书数据；如果安装目录不同，请同步修改 `docker-compose.yml` 与 `acme.acme_sh_path`。
- Compose 将本地 `ssh/` 挂载到容器的 `/app/ssh`。使用 SSH 私钥时，配置中的 `key_path` 应填写容器内路径，例如 `/app/ssh/id_ed25519`，而不是宿主机路径。
- 手动证书的 `fullchain_path`、`key_path` 必须在 CRT 进程所在的容器内可读；请通过卷挂载提供文件。
- 当前 Compose 还挂载了宿主机 `/etc/nginx` 到容器同路径。如果不需要容器直接访问宿主机 Nginx 配置，请删除该挂载或改为只读，减少主机配置暴露面。

### 4. 构建并启动

仓库的 `docker-compose.yml` 默认引用本地镜像 `cert-monitor:latest`，因此首次启动前需要先构建：

```bash
docker build -t cert-monitor:latest .
docker compose up -d
```

查看状态与日志：

```bash
docker compose ps
docker compose logs -f --tail=200 cert-monitor
```

打开浏览器访问：

- 证书管理控制台：`http://<服务器IP>:8080/`
- 配置页面：`http://<服务器IP>:8080/config`

如需从公网访问，请在前置 Nginx / 负载均衡 / 防火墙中限制访问来源，避免将管理控制台直接暴露到公网。

### 5. 检查部署结果

1. 确认容器正常启动，日志中没有配置解析或凭据错误。
2. 在控制台检查域名、证书和到期时间是否符合预期。
3. 先保持 `deploy.mode: dry-run`，检查计划中命中的云产品、域名及 Nginx 目标。
4. 确认无误后再将 `deploy.mode` 改为 `apply`，重启或按当前配置热更新服务。
5. 确认手动操作成功后，再考虑启用 `auto_renew_enabled: true` 并设置合理的 `auto_renew_days`。

## 配置说明

| 配置项 | 作用 | 建议 |
|---|---|---|
| `acme` | acme.sh 路径、证书路径模式和命令超时 | 确认容器内路径与持久化目录一致 |
| `providers` | 云账号及静态域名来源 | 每个账号使用唯一名称，凭据按最小权限配置 |
| `certificate_policies` | ACME/手动证书来源、自动续签策略、指定 DNS 账号 | 共享 ACME 证书优先按 Main_Domain 配置 |
| `product_scan` | 控制域名、CDN、LB、OSS、WAF、ECS 等扫描范围 | 只开启实际使用的产品 |
| `deploy.mode` | `dry-run` 计划模式或 `apply` 实际部署模式 | 初次安装使用 `dry-run` |
| `deploy.binding_cache` | 产品绑定缓存及部署前实时校验 | 生产环境根据 API 配额与数据时效权衡 |
| `auto_renew_enabled` | 是否启用自动续签编排 | 确认 DNS API 权限和部署策略后再开启 |
| `auto_renew_days` | 证书剩余多少天时触发自动续签 | 根据证书周期和签发机构限制设置 |
| `alert` | 证书、域名和 ECS 到期告警阈值 | 结合值班响应时间设置 |
| `whitelist` / `blacklist` | 分别控制告警白名单与证书操作黑名单 | 黑名单可防止指定域名被签发、续签或部署 |
| `ca_catalog_check` | 定期核对已登记的官方 CA 文件 | 建议启用，并持久化 `/app/data` |
| `check_interval_minutes` | 周期采集间隔 | 按云 API 配额和监控需求调整 |

配置支持通过 `extends` 继承其他 YAML 配置；配置保存会执行校验和原子写入。详细语义请见 [配置规则](docs/config-rules.md)。

## 构建镜像与推送仓库

仓库提供 `build.sh`，会构建版本标签和 `latest` 标签并推送到镜像仓库。执行前请确认 Docker 已登录目标仓库。

构建并推送默认镜像名：

```bash
chmod +x build.sh
./build.sh 1.0.0
```

推送到私有仓库：

```bash
docker login registry.example.com
IMAGE_BASE=registry.example.com/namespace/cert-monitor ./build.sh 1.0.0
```

此时会推送：

- `registry.example.com/namespace/cert-monitor:1.0.0`
- `registry.example.com/namespace/cert-monitor:latest`

如果部署环境使用私有镜像，请同步修改 `docker-compose.yml` 中的 `image` 为对应仓库地址和标签，然后执行 `docker compose pull` 与 `docker compose up -d`。

## 升级与备份

### 升级

部署在 Git 工作目录时，先备份配置和数据，再拉取代码、重新构建镜像并重建容器：

```bash
cd /data/cert-monitor

# 先备份（见下方命令），确认配置有独立副本
git pull --ff-only
docker build -t cert-monitor:latest .
docker compose up -d
docker image prune -f
```

使用镜像仓库发布时：

```bash
docker compose pull
docker compose up -d
```

只执行 `docker compose restart` 不会更新镜像中的代码或依赖。升级后请查看日志，并确认控制台与定时采集正常。

### 备份

至少应备份：

- `conf/`：生产配置，包含云 API 配置引用。
- `data/`：状态快照、产品绑定缓存、告警状态及 CA 核对状态。
- `ssh/`：如使用 SSH 部署，其中可能包含私钥。
- 宿主机 `/root/.acme.sh/`：acme.sh 账户、证书及续签元数据（如由本项目使用）。

示例：

```bash
cd /data/cert-monitor
tar --exclude='./logs' -czf ../cert-monitor-backup-$(date +%F).tar.gz conf data ssh
# 如果本项目使用宿主机 /root/.acme.sh，请另外对该目录做受控备份
```

备份包可能包含凭据或私钥，应限制访问权限并加密存放。恢复前先停止服务，恢复配置和数据后再启动容器。

## 本地开发与测试

### 安装依赖

```bash
python -m venv .venv
# Linux/macOS
source .venv/bin/activate
# Windows PowerShell 使用：.venv\\Scripts\\Activate.ps1

python -m pip install -r requirements.txt
python -m pip install "pytest-cov>=5,<8"
cp config.example.yaml config.yaml
python main.py
```

控制台默认监听 8080 端口。也可以显式指定配置文件：

```bash
python main.py --config ./config.yaml
```

### 运行可移植单元测试

```bash
python -m pytest -q -m "not integration and not cloud and not e2e" tests --ignore=tests/e2e --cov=app --cov-report=term-missing:skip-covered --cov-report=html:htmlcov
```

### 运行 ZeroSSL 公网集成测试

```bash
python -m pytest -q -m integration tests/test_zerossl_chain.py
```

此测试会访问官方 CA 下载地址，需要可用的公网网络。默认便携单元测试不依赖该公网请求。

### 测试边界

- GitHub Actions 运行 Python 3.12 的便携测试与 ZeroSSL 公网集成测试。
- CI 不使用生产云账号、不执行真实云部署。
- `tests/e2e/` 中的端到端脚本可能操作真实云资源或 Nginx，必须在确认测试环境与配置后手动执行。
- 本地配置文件、生产凭据、私钥不得提交到仓库。

## 安全建议

1. **先 dry-run 后 apply。** `apply` 会调用云 API 或 SSH 修改真实证书绑定、文件及服务状态。
2. **默认关闭自动续签。** 完成验证后再启用，并设置合理的续签阈值、重试次数和冷却时间。
3. **使用最小权限凭据。** 云账号只授予需要的资源查询、证书上传和绑定权限。
4. **不要把密钥写入 Git。** 生产配置、SSH 私钥和 SMTP 密码应通过受控文件或环境变量提供。
5. **限制控制台访问。** 默认 Web 控制台没有在此项目中配置反向代理身份认证，不建议直接向公网开放 8080。
6. **审慎处理宿主机挂载。** 尤其是 `/root/.acme.sh` 与 `/etc/nginx`，只挂载确实需要的目录，并限制备份权限。
7. **为持久化目录做备份。** 容器重建不应导致证书、状态数据和配置丢失。

## 文档导航

| 文档 | 内容 |
|---|---|
| [配置示例](config.example.yaml) | 完整 YAML 示例与字段注释 |
| [运行流程总览](docs/flow-runtime.md) | 调度、采集、续签和部署主流程 |
| [文档导航与流程图](docs/README.md) | 模块文档索引及架构图 |
| [Provider 说明](docs/providers.md) | 域名、DNS、云产品发现能力 |
| [Deployer 说明](docs/deploys.md) | 云产品和 Nginx 证书下发细节 |
| [服务编排说明](docs/services.md) | 采集、签发、续签、部署及配置热更新 |
| [证书来源策略](docs/certificate-policies.md) | ACME 与手动证书策略 |
| [证书链补全](docs/certificate-chain.md) | Let's Encrypt / ZeroSSL 链路径、验证和写回行为 |
| [通知配置](docs/notifications.md) | 钉钉、Rocket.Chat、SMTP 邮件 |
| [测试指南](docs/testing.md) | 本地、CI、集成与 E2E 测试 |

## 常见问题

### 启动提示找不到配置文件

Compose 使用 `--config /app/conf/config.yaml`，确认宿主机的 `conf/config.yaml` 已创建并挂载。

### 控制台能打开，但没有域名或证书

检查 Provider 是否启用、凭据是否正确、`product_scan` 开关及云账号权限；同时查看 `docker compose logs -f cert-monitor`。采集完成后再核对页面状态。

### dry-run 显示没有部署目标

确认产品绑定缓存已成功刷新，目标产品扫描开关已开启，域名匹配规则正确，并且缓存未过期。若配置了黑名单，命中目标会被跳过。

### 自动续签没有触发

检查 `auto_renew_enabled`、`auto_renew_days`、证书来源策略的 `auto_renew`、续签冷却状态以及域名黑名单。手动来源证书不会自动续签。

### 证书链补全失败

检查容器能否访问官方 HTTPS CA 下载地址、系统时间是否正确、输入证书是否属于已支持的 Let's Encrypt / ZeroSSL 路径，并查看日志中的验证错误。网络故障不代表证书本身失效。

### 更改代码后服务仍运行旧版本

本地构建部署需重新执行 `docker build` 和 `docker compose up -d`；镜像仓库部署需重新推送并 `docker compose pull`。单纯重启容器不会更新镜像内容。

---

如果此项目对你有帮助，欢迎通过 GitHub Issues 反馈问题或提出改进建议。
