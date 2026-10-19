# 测试指南（Windows 本地 / GitHub Actions）

## 安装

```powershell
python -m pip install -r requirements.txt
python -m pip install "pytest-cov>=5,<8"
```

本地继续使用自己的 `config.yaml`；GitHub Actions 则从 `config.example.yaml` 生成临时配置，不读取任何生产凭据。不要把本地配置、私钥提交到仓库。

## 可移植测试与覆盖率

```powershell
python -m pytest -q -m "not integration and not cloud and not e2e" tests --ignore=tests/e2e --cov=app --cov-report=term-missing:skip-covered --cov-report=html:htmlcov
```

报告位于 `htmlcov/index.html`。GitHub Actions 在 `python-coverage` artifact 中保存 HTML 与 XML 报告 14 天。覆盖率暂不设置强制阈值，先根据真实基线识别未覆盖模块。

## 官方 CA 网络集成测试

```powershell
python -m pytest -q -m integration tests/test_zerossl_chain.py
```

此命令访问公网 CA 下载地址，网络受限时可能失败。单元测试不依赖该网络请求。

## 真实部署 E2E

`tests/e2e/` 不属于默认 pytest 发现范围。仅在明确准备好测试环境、配置和凭据后，手动执行对应脚本，例如：

```powershell
python tests/e2e/test_deploy_nginx.py
```

**警告**：部分脚本在 `deploy.mode=apply` 下可能操作真实云资源或 Nginx，运行前务必检查目标环境。CI 默认不执行。

## 测试分类

- `integration`：公网 CA 等外部网络集成测试。
- `cloud`：需要云平台凭据或真实云 API 的测试。
- `e2e`：依赖本地配置、基础设施或可能实际部署的端到端测试。

现有历史测试的分类仍需持续审核；新增外部依赖测试应显式添加对应 marker。


## 云 SDK 测试边界

CI 不使用真实云账号、云平台凭据或实际资源操作。第三方 SDK 自身不属于本项目的覆盖率范围。

暂不为了提高覆盖率而给 `app/utils/*_sdk.py` 等薄封装编写模拟 SDK 内部实现的测试。与云平台无关的参数处理、证书解析、部署规划、异常分支和状态管理，仍然应使用 Mock 或本地测试数据覆盖。

当前 `--cov=app` 的 71% 是全项目基线（2026-10-16）；在决定是否排除任何适配层之前，应先审核其是否包含本项目的业务逻辑。排除项必须明确列出并保留原始全项目覆盖率供对比。

固定 ZeroSSL 证书样本的回归测试使用 2026-10-16 UTC 作为验证时间，避免样本到期导致测试自然失效；生产证书验证仍使用当前时间。
