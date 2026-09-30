## 1. 配置模型

- [x] 1.1 在 `AcmeConfig` 增加默认值为 `auto` 的 `certificate_path_mode` 枚举配置
- [x] 1.2 在示例配置和配置文档说明三种模式及 Nginx 主域名目标文件名

## 2. 路径选择

- [x] 2.1 重构 acme.sh 路径解析为 custom、记录路径、default 三组成对候选
- [x] 2.2 实现 `auto/default/custom` 的选择、存在性校验、明确日志与错误
- [x] 2.3 统一使用完整链作为 `cert_path` 和部署证书内容，保持 CA 可选读取

## 3. 目标主机命名

- [x] 3.1 更新 Nginx 示例模板，以 `{main_domain}.pem` 和 `{main_domain}.key` 写入目标主机
- [x] 3.2 验证目标文件命名不修改 acme.sh 源文件且不影响云平台 PEM 上传

## 4. 测试与校验

- [x] 4.1 增加配置默认值、合法值和非法值测试
- [x] 4.2 增加三种路径模式、成对回退和失败语义测试
- [x] 4.3 增加 Nginx 主域名目标文件名测试
- [x] 4.4 运行专项测试、完整回归、OpenSpec 严格校验与差异检查
- [x] 4.5 使用真实 acme.sh ECC `--info` 输出样本验证 auto 与 default 路径选择
