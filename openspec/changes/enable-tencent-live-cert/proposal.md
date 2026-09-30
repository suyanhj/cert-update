## Why

腾讯云 SSL 控制台支持云直播播放域名的证书扫描与下发，但 CRT 尚未接入。需要在不扩其它产品的前提下，用同一套 SSL Host 接口补齐云直播。

## What Changes

- `TencentCloudProvider.get_live_bindings()`：SSL `DescribeHostLiveInstanceList`，保留 HTTPS 已开启的播放域名（含未关联证书项）。
- `TencentDeployer`：对 `product_type=live` 调用 `DeployCertificateInstance`，`ResourceType=live`，`InstanceIdList=[domain]`。
- 新增 `product_scan.<cloud>.live_scan` 开关；`config.example.yaml` 对腾讯云默认开启。
- 文档与单测同步。

## Capabilities

### New Capabilities

- `tencent-live-cert`: 腾讯云云直播播放域名 SSL 扫描与下发。

### Modified Capabilities

- 不改 `domain-matching`。

## Impact

- `app/providers/tencent.py`、`app/deploys/tencent.py`、`app/schemas/config.py`
- `app/services/bindings.py`、`app/services/deploy.py`
- `tests/test_tencent_cloud.py`、`config.example.yaml`、`docs/providers.md`、`docs/deploys.md`
