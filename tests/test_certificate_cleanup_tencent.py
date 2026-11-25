from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.certificate_cleanup.adapters.tencent import TencentSSLCleanupAdapter
from app.certificate_cleanup.base import (
    AssociationStatus,
    CertificateSource,
    DeleteOutcome,
    IncompleteInventoryError,
)


def _certificate(cert_id: str, source: str = "upload", *, package: bool = False):
    return SimpleNamespace(
        CertificateId=cert_id,
        Alias=f"证书-{cert_id}",
        Domain="example.com",
        CertEndTime="2025-01-01 00:00:00",
        From=source,
        IsPackage=package,
    )


class FakeTencentClient:
    def __init__(self) -> None:
        self.pages = []
        self.bind_create = None
        self.bind_results = []
        self.delete_response = None
        self.delete_results = []
        self.describe_requests = []
        self.delete_requests = []

    def DescribeCertificates(self, request):
        self.describe_requests.append(request)
        if request.CertIds:
            wanted = request.CertIds[0]
            all_items = [item for page in self.pages for item in page[1]]
            items = [item for item in all_items if item.CertificateId == wanted]
            return SimpleNamespace(
                TotalCount=len(items), Certificates=items, RequestId="get-request"
            )
        total, items = self.pages.pop(0)
        return SimpleNamespace(TotalCount=total, Certificates=items, RequestId="list-request")

    def CreateCertificateBindResourceSyncTask(self, request):
        self.bind_create_request = request
        return self.bind_create

    def DescribeCertificateBindResourceTaskResult(self, request):
        return self.bind_results.pop(0)

    def DeleteCertificate(self, request):
        self.delete_requests.append(request)
        return self.delete_response

    def DescribeDeleteCertificatesTaskResult(self, request):
        return self.delete_results.pop(0)


def _adapter(client, **kwargs):
    poll_interval_seconds = kwargs.pop("poll_interval_seconds", 0)
    poll_timeout_seconds = kwargs.pop("poll_timeout_seconds", 0.01)
    return TencentSSLCleanupAdapter(
        "腾讯测试",
        client=client,
        page_size=2,
        poll_interval_seconds=poll_interval_seconds,
        poll_timeout_seconds=poll_timeout_seconds,
        **kwargs,
    )


def _task_ref(cert_id="cert-1", task_id="task-1"):
    return SimpleNamespace(CertId=cert_id, TaskId=task_id)


def _bind_result(status, bindings=None, task_id="task-1", error=None):
    return SimpleNamespace(
        TaskId=task_id,
        Status=status,
        BindResourceResult=bindings,
        Error=error,
    )


def _resource(resource_type, *regions):
    return SimpleNamespace(
        ResourceType=resource_type,
        BindResourceRegionResult=[
            SimpleNamespace(Region=name, TotalCount=count, Error=error)
            for name, count, error in regions
        ],
    )


def test_tencent_strict_pagination_and_source_classification() -> None:
    client = FakeTencentClient()
    client.pages = [
        (3, [_certificate("upload"), _certificate("buy", "buy", package=True)]),
        (3, [_certificate("unknown", "new-source")]),
    ]

    result = asyncio.run(_adapter(client).list_certificates())

    assert [item.certificate_id for item in result] == ["upload", "buy", "unknown"]
    assert [item.source for item in result] == [
        CertificateSource.UPLOADED,
        CertificateSource.PURCHASED,
        CertificateSource.UNKNOWN,
    ]
    assert [request.Offset for request in client.describe_requests] == [0, 2]


@pytest.mark.parametrize(
    "pages",
    [
        [(2, [_certificate("one")]), (2, [])],
        [(2, [_certificate("same")]), (2, [_certificate("same")])],
        [(1, None)],
    ],
)
def test_tencent_incomplete_or_repeated_pages_fail_closed(pages) -> None:
    client = FakeTencentClient()
    client.pages = pages

    with pytest.raises(IncompleteInventoryError):
        asyncio.run(_adapter(client).list_certificates())


def test_tencent_association_task_covers_all_returned_resource_types() -> None:
    client = FakeTencentClient()
    client.bind_create = SimpleNamespace(CertTaskIds=[_task_ref()], RequestId="create")
    client.bind_results = [
        SimpleNamespace(
            SyncTaskBindResourceResult=[_bind_result(0)], RequestId="poll-1"
        ),
        SimpleNamespace(
            SyncTaskBindResourceResult=[
                _bind_result(
                    1,
                    [
                        _resource("cdn", ("global", 1, "")),
                        _resource("clb", ("ap-guangzhou", 0, "")),
                        _resource("live", ("global", 2, "")),
                        _resource("edgeOne", ("global", 1, "")),
                        _resource("cos", ("ap-guangzhou", 1, "")),
                    ],
                )
            ],
            RequestId="poll-2",
        ),
    ]

    result = asyncio.run(_adapter(client).check_associations("cert-1"))

    assert client.bind_create_request.IsCache == 0
    assert result.status is AssociationStatus.BOUND
    assert {item.split(":")[0] for item in result.resources} == {
        "cdn",
        "live",
        "edgeOne",
        "cos",
    }


@pytest.mark.parametrize(
    "response",
    [
        SimpleNamespace(SyncTaskBindResourceResult=None, RequestId="bad"),
        SimpleNamespace(
            SyncTaskBindResourceResult=[_bind_result(2, error="缺少服务角色")],
            RequestId="failed",
        ),
        SimpleNamespace(
            SyncTaskBindResourceResult=[
                _bind_result(1, [_resource("clb", ("ap", 0, "无权限"))])
            ],
            RequestId="partial",
        ),
    ],
)
def test_tencent_association_partial_or_failed_result_never_unbound(response) -> None:
    client = FakeTencentClient()
    client.bind_create = SimpleNamespace(CertTaskIds=[_task_ref()], RequestId="create")
    client.bind_results = [response]

    with pytest.raises(IncompleteInventoryError):
        asyncio.run(_adapter(client).check_associations("cert-1"))


def test_tencent_delete_always_checks_resources_and_polls_success() -> None:
    client = FakeTencentClient()
    client.delete_response = SimpleNamespace(TaskId="delete-1", RequestId="delete-request")
    client.delete_results = [
        SimpleNamespace(
            DeleteTaskResult=[
                SimpleNamespace(
                    TaskId="delete-1", CertId="cert-1", Status=1, Error="", Domains=[]
                )
            ],
            RequestId="poll-request",
        )
    ]

    result = asyncio.run(_adapter(client).delete_certificate("cert-1"))

    assert client.delete_requests[0].IsCheckResource is True
    assert result.outcome is DeleteOutcome.DELETED
    assert result.task_id == "delete-1"


def test_tencent_delete_classifies_in_use_and_unknown_failure() -> None:
    client = FakeTencentClient()
    client.delete_response = SimpleNamespace(TaskId="delete-1", RequestId="delete-request")
    client.delete_results = [
        SimpleNamespace(
            DeleteTaskResult=[
                SimpleNamespace(
                    TaskId="delete-1",
                    CertId="cert-1",
                    Status=2,
                    Error="certificate in use",
                    Domains=["cdn.example.com"],
                )
            ],
            RequestId="poll-request",
        )
    ]
    assert (
        asyncio.run(_adapter(client).delete_certificate("cert-1")).outcome
        is DeleteOutcome.IN_USE
    )

    client.delete_response = SimpleNamespace(TaskId="delete-2", RequestId="delete-request")
    client.delete_results = [
        SimpleNamespace(
            DeleteTaskResult=[
                SimpleNamespace(
                    TaskId="delete-2", CertId="cert-2", Status=3, Error="missing role", Domains=[]
                )
            ],
            RequestId="poll-request",
        )
    ]
    with pytest.raises(RuntimeError, match="missing role"):
        asyncio.run(_adapter(client).delete_certificate("cert-2"))


def test_tencent_delete_timeout_fails_closed() -> None:
    client = FakeTencentClient()
    client.delete_response = SimpleNamespace(TaskId="delete-1", RequestId="delete-request")
    pending = SimpleNamespace(
        DeleteTaskResult=[
            SimpleNamespace(
                TaskId="delete-1", CertId="cert-1", Status=0, Error="", Domains=[]
            )
        ],
        RequestId="poll",
    )
    client.delete_results = [pending] * 1000

    with pytest.raises(TimeoutError):
        asyncio.run(
            _adapter(client, poll_interval_seconds=0.02).delete_certificate("cert-1")
        )
