from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.certificate_cleanup.adapters.huawei import (
    HuaweiCCMCleanupAdapter,
    HuaweiELBCleanupAdapter,
    HuaweiWAFCleanupAdapter,
)
from app.certificate_cleanup.base import (
    AssociationStatus,
    CertificateSource,
    DeleteOutcome,
)


class HttpError(RuntimeError):
    def __init__(self, status_code):
        super().__init__(str(status_code))
        self.status_code = status_code


def _elb_cert(cert_id="elb-1", source=""):
    return SimpleNamespace(
        id=cert_id,
        name=cert_id,
        expire_time="2025-01-01T00:00:00Z",
        source=source,
    )


class FakeElbClient:
    def __init__(self):
        self.cert_pages = []
        self.listener_pages = []
        self.certs = {"elb-1": _elb_cert()}
        self.delete_error = None

    def list_certificates(self, request):
        return self.cert_pages.pop(0)

    def list_listeners(self, request):
        return self.listener_pages.pop(0)

    def show_certificate(self, request):
        item = self.certs.get(request.certificate_id)
        if item is None:
            raise HttpError(404)
        return SimpleNamespace(certificate=item, request_id="show")

    def delete_certificate(self, request):
        if self.delete_error:
            raise self.delete_error
        self.certs.pop(request.certificate_id, None)
        return SimpleNamespace(request_id="delete")


def _elb_adapter(client):
    return HuaweiELBCleanupAdapter(
        "华为测试", "cn-north-4", "project-1", client=client
    )


def test_huawei_elb_inventory_association_and_scm_copy_source() -> None:
    client = FakeElbClient()
    client.cert_pages = [
        SimpleNamespace(
            certificates=[_elb_cert("elb-1", "scm")],
            page_info=SimpleNamespace(next_marker=""),
            request_id="list",
        )
    ]
    client.listener_pages = [
        SimpleNamespace(
            listeners=[
                SimpleNamespace(
                    id="listener-1",
                    default_tls_container_ref="/certificates/elb-1",
                    sni_container_refs=[],
                )
            ],
            page_info=SimpleNamespace(next_marker=""),
            request_id="listeners",
        )
    ]
    adapter = _elb_adapter(client)

    certificates = asyncio.run(adapter.list_certificates())
    association = asyncio.run(adapter.check_associations("elb-1"))

    assert certificates[0].source is CertificateSource.MANAGED
    assert association.status is AssociationStatus.BOUND
    assert association.resources == ("listener:listener-1",)


def test_huawei_elb_delete_relies_on_platform_409_protection() -> None:
    client = FakeElbClient()
    client.delete_error = HttpError(409)
    adapter = _elb_adapter(client)

    result = asyncio.run(adapter.delete_certificate("elb-1"))

    assert adapter.safe_delete is True
    assert result.outcome is DeleteOutcome.IN_USE


def test_huawei_waf_remains_report_only_and_reads_bind_hosts() -> None:
    item = SimpleNamespace(
        id="waf-1",
        name="waf",
        expire_time=1_700_000_000_000,
        bind_host=[SimpleNamespace(id="host-1", hostname="example.com")],
    )
    client = SimpleNamespace(
        list_certificates=lambda _request: SimpleNamespace(items=[item], total=1)
    )
    adapter = HuaweiWAFCleanupAdapter(
        "华为测试", "cn-north-4", "project-1", "0", client=client
    )

    certificates = asyncio.run(adapter.list_certificates())
    association = asyncio.run(adapter.check_associations("waf-1"))

    assert len(certificates) == 1
    assert adapter.safe_delete is False
    assert association.status is AssociationStatus.BOUND


def test_huawei_ccm_uploaded_source_and_deployed_resource_check() -> None:
    cert = SimpleNamespace(
        id="ccm-1",
        name="ccm",
        expire_time="2025-01-01T00:00:00Z",
        status="UPLOAD",
    )

    class Client:
        def list_certificates(self, _request):
            return SimpleNamespace(certificates=[cert], total_count=1)

        def list_deployed_resources(self, _request):
            return SimpleNamespace(
                results=[
                    SimpleNamespace(
                        certificate_id="ccm-1",
                        total_num=2,
                        deployed_resources=[
                            SimpleNamespace(service="ELB", resource_num=1),
                            SimpleNamespace(service="CDN", resource_num=1),
                        ],
                    )
                ]
            )

    adapter = HuaweiCCMCleanupAdapter(
        "华为测试", "cn-north-4", "project-1", "0", client=Client()
    )

    certificates = asyncio.run(adapter.list_certificates())
    association = asyncio.run(adapter.check_associations("ccm-1"))

    assert certificates[0].source is CertificateSource.UPLOADED
    assert adapter.safe_delete is False
    assert association.status is AssociationStatus.BOUND
    assert set(association.resources) == {"ELB:1", "CDN:1"}
