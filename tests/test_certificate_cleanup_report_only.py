from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.certificate_cleanup.adapters.report_only import (
    AliyunCASCleanupAdapter,
    AliyunSLBCleanupAdapter,
    VolcengineCertificateReportAdapter,
)
from app.certificate_cleanup.base import (
    AssociationStatus,
    CertificateSource,
    DeleteOutcome,
    IncompleteInventoryError,
)
from app.certificate_cleanup.store import CertificateCleanupStore
from app.schemas.config import CertificateCleanupConfig
from app.services.certificate_cleanup import run_certificate_cleanup


def test_aliyun_cas_strict_pages_and_source_classification() -> None:
    pages = [
        SimpleNamespace(
            body=SimpleNamespace(
                certificate_order_list=[
                    SimpleNamespace(
                        certificate_id=1,
                        name="upload",
                        common_name="upload.example.com",
                        end_date="2025-01-01",
                        upload=True,
                    )
                ],
                total_count=2,
            )
        ),
        SimpleNamespace(
            body=SimpleNamespace(
                certificate_order_list=[
                    SimpleNamespace(
                        certificate_id=2,
                        name="issued",
                        common_name="issued.example.com",
                        end_date="2025-02-01",
                        upload=False,
                    )
                ],
                total_count=2,
            )
        ),
    ]

    class Client:
        def list_user_certificate_order(self, request):
            assert request.order_type == "CERT"
            assert request.status == "EXPIRED"
            return pages.pop(0)

        def list_cloud_resources(self, request):
            assert request.cert_ids == [1]
            return SimpleNamespace(
                body=SimpleNamespace(data=[], total=0, request_id="cas-empty")
            )

    adapter = AliyunCASCleanupAdapter("阿里测试", client=Client())
    result = asyncio.run(adapter.list_certificates())
    association = asyncio.run(adapter.check_associations("1"))

    assert [item.source for item in result] == [
        CertificateSource.UPLOADED,
        CertificateSource.CLOUD_ISSUED,
    ]
    assert adapter.safe_delete is False
    assert association.status is AssociationStatus.UNBOUND
    assert association.complete is True


def test_aliyun_cas_queries_all_deployments_by_certificate_id() -> None:
    pages = {
        1: SimpleNamespace(
            body=SimpleNamespace(
                data=[
                    SimpleNamespace(
                        cert_id=123,
                        cloud_product="CDN",
                        region_id="cn-hangzhou",
                        cloud_region=None,
                        instance_id="cdn-one",
                        listener_id=None,
                        listener_port=None,
                        domain="one.example.com",
                        id=1,
                    )
                ],
                total=2,
                request_id="cas-page-1",
            )
        ),
        2: SimpleNamespace(
            body=SimpleNamespace(
                data=[
                    SimpleNamespace(
                        cert_id=123,
                        cloud_product="OSS",
                        region_id="cn-shenzhen",
                        cloud_region=None,
                        instance_id="bucket-two",
                        listener_id=None,
                        listener_port=None,
                        domain="two.example.com",
                        id=2,
                    )
                ],
                total=2,
                request_id="cas-page-2",
            )
        ),
    }

    class Client:
        def list_cloud_resources(self, request):
            assert request.cloud_name == "aliyun"
            assert request.cert_ids == [123]
            return pages[request.current_page]

    association = asyncio.run(
        AliyunCASCleanupAdapter("阿里测试", client=Client()).check_associations(
            "123"
        )
    )

    assert association.status is AssociationStatus.BOUND
    assert len(association.resources) == 2
    assert association.request_ids == ("cas-page-1", "cas-page-2")


def test_aliyun_cas_incomplete_deployment_page_fails_closed() -> None:
    client = SimpleNamespace(
        list_cloud_resources=lambda _request: SimpleNamespace(
            body=SimpleNamespace(data=[], total=1, request_id="cas-incomplete")
        )
    )

    with pytest.raises(IncompleteInventoryError, match="空页"):
        asyncio.run(
            AliyunCASCleanupAdapter("阿里测试", client=client).check_associations(
                "123"
            )
        )


def test_aliyun_slb_ids_are_not_mixed_with_cas_ids() -> None:
    item = SimpleNamespace(
        server_certificate_id="slb-cert-1",
        server_certificate_name="slb",
        expire_time_stamp=1_700_000_000_000,
        expire_time=None,
        is_ali_cloud_certificate=0,
    )
    client = SimpleNamespace(
        describe_server_certificates=lambda _request: SimpleNamespace(
            body=SimpleNamespace(
                server_certificates=SimpleNamespace(server_certificate=[item])
            )
        )
    )
    adapter = AliyunSLBCleanupAdapter("阿里测试", "cn-hangzhou", client=client)

    result = asyncio.run(adapter.list_certificates())

    assert result[0].certificate_id == "slb-cert-1"
    assert adapter.store_id.store == "slb"
    assert adapter.store_id.scope == "cn-hangzhou"
    assert adapter.safe_delete is True


def _slb_association_client(
    *,
    default_certificate_id: str = "other-cert",
    extensions: list[SimpleNamespace] | None = None,
    listener_total: int = 1,
):
    extension_items = extensions if extensions is not None else []

    class Client:
        def describe_load_balancers(self, request):
            assert request.region_id == "cn-hangzhou"
            return SimpleNamespace(
                body=SimpleNamespace(
                    load_balancers=SimpleNamespace(
                        load_balancer=[SimpleNamespace(load_balancer_id="lb-1")]
                    ),
                    total_count=1,
                    request_id="lb-list",
                )
            )

        def describe_load_balancer_listeners(self, request):
            assert request.load_balancer_id == ["lb-1"]
            assert request.listener_protocol == "https"
            return SimpleNamespace(
                body=SimpleNamespace(
                    listeners=[
                        SimpleNamespace(
                            load_balancer_id="lb-1",
                            listener_port=443,
                            httpslistener_config=SimpleNamespace(
                                server_certificate_id=default_certificate_id
                            ),
                        )
                    ],
                    total_count=listener_total,
                    next_token=None,
                    request_id="listener-list",
                )
            )

        def describe_domain_extensions(self, request):
            assert request.load_balancer_id == "lb-1"
            assert request.listener_port == 443
            return SimpleNamespace(
                body=SimpleNamespace(
                    domain_extensions=SimpleNamespace(
                        domain_extension=extension_items
                    ),
                    request_id="domain-extensions",
                )
            )

    return Client()


def test_aliyun_slb_checks_listener_and_domain_extension_certificate_ids() -> None:
    extension = SimpleNamespace(
        server_certificate_id="target-cert",
        domain_extension_id="ext-1",
        domain="api.example.com",
    )
    adapter = AliyunSLBCleanupAdapter(
        "阿里测试",
        "cn-hangzhou",
        client=_slb_association_client(
            default_certificate_id="target-cert",
            extensions=[extension],
        ),
    )

    association = asyncio.run(adapter.check_associations("target-cert"))

    assert association.status is AssociationStatus.BOUND
    assert association.resources == (
        "listener:cn-hangzhou:lb-1:443",
        "domain_extension:cn-hangzhou:lb-1:443:api.example.com",
    )
    assert association.request_ids == (
        "lb-list",
        "listener-list",
        "domain-extensions",
    )


def test_aliyun_slb_unbound_requires_complete_listener_inventory() -> None:
    adapter = AliyunSLBCleanupAdapter(
        "阿里测试",
        "cn-hangzhou",
        client=_slb_association_client(),
    )

    association = asyncio.run(adapter.check_associations("target-cert"))

    assert association.status is AssociationStatus.UNBOUND
    assert association.complete is True


def test_aliyun_slb_incomplete_listener_page_fails_closed() -> None:
    adapter = AliyunSLBCleanupAdapter(
        "阿里测试",
        "cn-hangzhou",
        client=_slb_association_client(listener_total=2),
    )

    with pytest.raises(IncompleteInventoryError, match="数量不完整"):
        asyncio.run(adapter.check_associations("target-cert"))


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("CertificateRelatedDomainExtensionExist", DeleteOutcome.IN_USE),
        ("CertificateAndPrivateKeyIsRefered", DeleteOutcome.IN_USE),
        ("InvalidServerCertificateId.NotFound", DeleteOutcome.NOT_FOUND),
    ],
)
def test_aliyun_slb_delete_maps_platform_protection(code, expected) -> None:
    class SDKError(RuntimeError):
        def __init__(self):
            super().__init__(code)
            self.code = code

    client = SimpleNamespace(
        delete_server_certificate=lambda _request: (_ for _ in ()).throw(SDKError())
    )
    adapter = AliyunSLBCleanupAdapter(
        "阿里测试", "cn-hangzhou", client=client
    )

    receipt = asyncio.run(adapter.delete_certificate("target-cert"))

    assert receipt.outcome is expected


def test_aliyun_slb_rechecks_association_by_id_before_delete(tmp_path) -> None:
    certificate = SimpleNamespace(
        server_certificate_id="target-cert",
        server_certificate_name="target",
        expire_time_stamp=1_700_000_000_000,
        expire_time=None,
        is_ali_cloud_certificate=0,
    )

    class Client:
        def __init__(self) -> None:
            self.listener_queries = 0
            self.delete_calls = 0

        def describe_server_certificates(self, request):
            assert request.server_certificate_id in {None, "target-cert"}
            return SimpleNamespace(
                body=SimpleNamespace(
                    server_certificates=SimpleNamespace(
                        server_certificate=[certificate]
                    ),
                    request_id="certificate-list",
                )
            )

        def describe_load_balancers(self, _request):
            return SimpleNamespace(
                body=SimpleNamespace(
                    load_balancers=SimpleNamespace(
                        load_balancer=[SimpleNamespace(load_balancer_id="lb-1")]
                    ),
                    total_count=1,
                    request_id="lb-list",
                )
            )

        def describe_load_balancer_listeners(self, _request):
            self.listener_queries += 1
            certificate_id = (
                "other-cert" if self.listener_queries == 1 else "target-cert"
            )
            return SimpleNamespace(
                body=SimpleNamespace(
                    listeners=[
                        SimpleNamespace(
                            load_balancer_id="lb-1",
                            listener_port=443,
                            httpslistener_config=SimpleNamespace(
                                server_certificate_id=certificate_id
                            ),
                        )
                    ],
                    total_count=1,
                    next_token=None,
                    request_id=f"listener-{self.listener_queries}",
                )
            )

        def describe_domain_extensions(self, _request):
            return SimpleNamespace(
                body=SimpleNamespace(
                    domain_extensions=SimpleNamespace(domain_extension=[]),
                    request_id="domain-extensions",
                )
            )

        def delete_server_certificate(self, _request):
            self.delete_calls += 1
            return SimpleNamespace(body=SimpleNamespace(request_id="delete"))

    client = Client()
    adapter = AliyunSLBCleanupAdapter(
        "阿里测试", "cn-hangzhou", client=client
    )
    options = CertificateCleanupConfig(
        enabled=True,
        mode="delete",
        expired_grace_days=30,
        confirm_scans=1,
        max_deletes_per_run=20,
        delete_scopes=[
            {
                "provider_type": "aliyun",
                "provider_name": "阿里测试",
                "store": "slb",
            }
        ],
    )

    summaries = asyncio.run(
        run_certificate_cleanup(
            options,
            [adapter],
            CertificateCleanupStore(str(tmp_path / "state.json")),
            now=datetime(2026, 10, 23, tzinfo=timezone.utc),
        )
    )

    assert client.listener_queries == 2
    assert client.delete_calls == 0
    assert summaries[0].bound == 1
    assert summaries[0].deleted == 0


def test_volcengine_report_inventory_unknown_association_never_deletes() -> None:
    class Service:
        def certificate_get_instance_list(self, request):
            assert request["PageNumber"] == 1
            return {
                "Instances": [
                    {
                        "InstanceId": "volc-1",
                        "CommonName": "example.com",
                        "NotAfter": "2025-01-01T00:00:00Z",
                        "InstanceType": "imported",
                    }
                ],
                "TotalCount": 1,
            }

    adapter = VolcengineCertificateReportAdapter(
        "火山测试", "cn-north-1", service=Service()
    )

    result = asyncio.run(adapter.list_certificates())
    association = asyncio.run(adapter.check_associations("volc-1"))

    assert result[0].source is CertificateSource.UPLOADED
    assert association.status is AssociationStatus.UNKNOWN
    assert adapter.safe_delete is False
