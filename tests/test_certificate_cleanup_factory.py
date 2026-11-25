from __future__ import annotations

from types import SimpleNamespace
import asyncio

import pytest

from app.certificate_cleanup import factory
from app.certificate_cleanup.base import InventoryUnavailableError


def _provider(provider_type, name=None, enabled=True):
    return SimpleNamespace(
        type=provider_type,
        name=name or provider_type,
        enabled=enabled,
        credentials={
            "secret_id": "id",
            "secret_key": "key",
            "access_key_id": "ak",
            "access_key_secret": "sk",
            "region": "cn-north-1",
            "project_id": "project",
            "enterprise_project_id": "0",
        },
    )


def test_factory_builds_supported_stores_and_skips_cloudflare_custom(monkeypatch) -> None:
    created = []

    def builder(kind):
        return lambda *args, **kwargs: created.append((kind, args, kwargs)) or kind

    monkeypatch.setattr(factory, "TencentSSLCleanupAdapter", builder("tencent"))
    monkeypatch.setattr(factory, "QiniuFusionCleanupAdapter", builder("qiniu"))
    monkeypatch.setattr(factory, "HuaweiELBCleanupAdapter", builder("huawei-elb"))
    monkeypatch.setattr(factory, "HuaweiWAFCleanupAdapter", builder("huawei-waf"))
    monkeypatch.setattr(factory, "HuaweiCCMCleanupAdapter", builder("huawei-ccm"))
    monkeypatch.setattr(factory, "AliyunCASCleanupAdapter", builder("aliyun-cas"))
    monkeypatch.setattr(factory, "AliyunSLBCleanupAdapter", builder("aliyun-slb"))
    monkeypatch.setattr(
        factory,
        "VolcengineCertificateReportAdapter",
        builder("volcengine"),
    )
    conf = SimpleNamespace(
        providers=[
            _provider("tencent"),
            _provider("qiniu"),
            _provider("huawei"),
            _provider("aliyun"),
            _provider("volcengine"),
            _provider("cloudflare"),
            _provider("custom"),
            _provider("tencent", "disabled", enabled=False),
        ]
    )

    result = factory.build_certificate_cleanup_adapters(conf)

    assert result == [
        "tencent",
        "qiniu",
        "huawei-elb",
        "huawei-waf",
        "huawei-ccm",
        "aliyun-cas",
        "aliyun-slb",
        "volcengine",
    ]
    assert all(item[0] not in {"cloudflare", "custom", "disabled"} for item in created)


def test_factory_isolates_one_account_initialization_failure(monkeypatch) -> None:
    def failing(*_args, **_kwargs):
        raise RuntimeError("access_key_secret=must-not-leak")

    monkeypatch.setattr(factory, "TencentSSLCleanupAdapter", failing)
    monkeypatch.setattr(
        factory,
        "QiniuFusionCleanupAdapter",
        lambda *args, **kwargs: "healthy",
    )
    conf = SimpleNamespace(providers=[_provider("tencent"), _provider("qiniu")])

    result = factory.build_certificate_cleanup_adapters(conf)

    assert result[1] == "healthy"
    with pytest.raises(RuntimeError, match="REDACTED"):
        asyncio.run(result[0].list_certificates())


def test_huawei_store_initialization_is_isolated_and_region_unavailable_is_expected(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        factory,
        "HuaweiELBCleanupAdapter",
        lambda **_kwargs: "huawei-elb",
    )
    monkeypatch.setattr(
        factory,
        "HuaweiWAFCleanupAdapter",
        lambda **_kwargs: "huawei-waf",
    )

    def unsupported_ccm(**_kwargs):
        raise ValueError(
            "region_id 'cn-south-1' is not in the following supported regions "
            "of service 'Scm': [ap-southeast-1, cn-north-4]"
        )

    monkeypatch.setattr(factory, "HuaweiCCMCleanupAdapter", unsupported_ccm)
    provider = _provider("huawei")
    provider.credentials["region"] = "cn-south-1"

    result = factory.build_certificate_cleanup_adapters(
        SimpleNamespace(providers=[provider])
    )

    assert result[:2] == ["huawei-elb", "huawei-waf"]
    assert result[2].store_id.store == "ccm"
    assert result[2].store_id.scope == "cn-south-1/project/0"
    with pytest.raises(InventoryUnavailableError) as exc_info:
        asyncio.run(result[2].list_certificates())
    assert exc_info.value.reason == "unsupported_region"


def test_huawei_unexpected_store_initialization_failure_does_not_drop_other_stores(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        factory,
        "HuaweiELBCleanupAdapter",
        lambda **_kwargs: "huawei-elb",
    )
    monkeypatch.setattr(
        factory,
        "HuaweiCCMCleanupAdapter",
        lambda **_kwargs: "huawei-ccm",
    )

    def failed_waf(**_kwargs):
        raise RuntimeError("WAF 客户端初始化损坏")

    monkeypatch.setattr(factory, "HuaweiWAFCleanupAdapter", failed_waf)

    result = factory.build_certificate_cleanup_adapters(
        SimpleNamespace(providers=[_provider("huawei")])
    )

    assert result[0] == "huawei-elb"
    assert result[1].store_id.store == "waf"
    assert result[2] == "huawei-ccm"
    with pytest.raises(RuntimeError, match="初始化损坏"):
        asyncio.run(result[1].list_certificates())
