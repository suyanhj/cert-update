# Tasks

## 1. 清理与验证

- [x] 1.1 删除自定义映射分支、专用解析方法及 json 导入，检查差异与剩余引用。
- [x] 1.2 运行现有 acme 测试及 OpenSpec 校验，确认内置映射和签发行为未回归。

验证结果：项目虚拟环境执行 tests/test_acme_sh.py，33 项通过；OpenSpec 严格校验与 git diff --check 通过，原文件 UTF-8 编码和 LF 换行保留。
