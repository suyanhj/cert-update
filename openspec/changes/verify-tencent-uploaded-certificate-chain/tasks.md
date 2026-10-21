# Tasks

## 1. 腾讯上传后证书链验证

- [x] 1.1 在腾讯部署器中实现本地与远端 PEM 证书链的 SHA-256 指纹提取、去重和一致性校验，并用单元测试覆盖相同链、缺链、不同链及非法 PEM
- [x] 1.2 在上传返回证书 ID 后调用 `DescribeCertificateDetail`，合并远端公钥证书与根证书进行校验，验证相关单元测试通过
- [x] 1.3 调整上传结果日志，准确记录 `CertificateId`、`RepeatCertId`、`RequestId` 和非敏感链摘要，并验证日志不包含 PEM 或私钥

## 2. 失败阻断与回归验证

- [x] 2.1 补充腾讯重复旧 ID且远端缺链时失败的测试，并验证不会执行 SSL Host 扫描或产品部署
- [x] 2.2 补充远端链一致时继续复用证书 ID并部署的测试，验证现有腾讯 CDN、EO、CLB、COS 路径保持兼容
- [x] 2.3 更新腾讯部署文档说明上传后链校验及权限要求，并运行相关测试与 `openspec validate --strict` 验证变更

## 3. 缺链旧 ID 受控重传

- [x] 3.1 将腾讯上传请求抽取为可显式设置 `Repeatable` 的复用逻辑，保持首次上传为 `false`
- [x] 3.2 首次远端链不一致时以 `Repeatable=true` 强制重传一次，重传结果验证通过才返回新 ID，查询/解析失败不触发重传
- [x] 3.3 补充强制重传成功、重传后仍缺链、最多重传一次及日志无敏感内容的测试
- [x] 3.4 更新腾讯部署文档，运行定向回归、全量测试与 `openspec validate --strict`

## Workflow follow-up

- 项目评审通过后归档该 OpenSpec change。
