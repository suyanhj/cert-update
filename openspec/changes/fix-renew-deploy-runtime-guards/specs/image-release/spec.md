## Purpose

确保证书管理服务每次构建后发布的版本标签和 latest 标签对应本次构建结果，防止本地遗留的旧 latest 覆盖新版本，让发布脚本的成功提示与实际镜像内容保持一致。

## ADDED Requirements

### Requirement: Publish the newly built image under both tags
构建脚本 SHALL 将本次新构建的版本镜像标记为 latest，并推送版本与 latest 标签；不依赖本地预先存在 latest。

#### Scenario: Old latest exists locally
- **WHEN** 本地 latest 指向旧镜像并执行新版本构建
- **THEN** 两个待推送标签均指向新构建镜像

#### Scenario: No latest exists locally
- **WHEN** 首次构建且本地没有 latest
- **THEN** 从新版本镜像创建 latest 并正常推送
