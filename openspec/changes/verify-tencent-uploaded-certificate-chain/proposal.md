# Proposal

## Why

腾讯云上传证书时会在 `Repeatable=false` 下复用相同叶证书指纹对应的旧证书 ID；当本地仅补全了证书链时，旧 ID 仍可能保存缺链内容。当前流程未验证返回 ID 的远端证书链便继续部署，可能把缺链证书下发到 EdgeOne 等生产资源并造成 TLS 访问异常。

## What Changes

- 腾讯证书上传取得 ID 后，查询该 ID 的证书详情并对比本地待部署证书链。
- 首次上传返回的远端证书链与本地链不一致时，使用 `Repeatable=true` 强制重传一次并再次验证；重传后仍不一致才终止腾讯账号本轮部署。
- 记录本地与远端证书链的非敏感指纹、证书数量、返回 ID 和请求 ID，避免记录证书私钥或完整 PEM。
- 补充重复证书 ID、完整链一致、远端缺链及详情查询失败的回归测试。

## Capabilities

### New Capabilities

- `tencent-certificate-deployment-safety`: 约束腾讯云上传后证书链校验、失败阻断和安全日志行为。

### Modified Capabilities

无。

## Impact

- 影响 `TencentDeployer.upload_certificate()` 及腾讯证书上传后的上下文准备流程。
- 正常路径新增一次腾讯 SSL `DescribeCertificateDetail` 查询；仅在腾讯复用缺链旧 ID 时额外执行一次允许重复的上传和详情查询，不引入第三方依赖。
- 腾讯账号需要具备查询证书详情权限；权限不足时按安全失败处理。
- 不改变其他云厂商、Nginx 部署以及腾讯多目标复用同一已验证证书 ID 的行为。
