## Why

续签期间页面断开或被回收后，回调仍向旧客户端通知，产生 Client has been deleted 警告；配置保存也可能更新失效控件。

## What Changes

- 升级 NiceGUI 到当前稳定版本 3.18.0，并同步其要求的依赖。
- 异步任务后的通知、页面刷新与配置控件更新检查客户端存在且连接有效。
- 页面断开不取消已授权的后台业务；新页面通过现有状态展示结果。

## Capabilities

### New Capabilities
- `ui-client-lifecycle`: 页面异步操作的连接状态及销毁处理。

### Modified Capabilities
无。

## Impact

影响 requirements、UI 回调及相关测试，不改变证书业务规则。
