"""Local-only tests for cached cloud-product bindings (no cloud API calls)."""
import json

import pytest

from app.utils.store import ProductBindingStore, CollectorSnapshotStore


def test_bindings_roundtrip_and_case_insensitive_dedup(tmp_path):
    path = tmp_path / "bindings.json"
    cache = ProductBindingStore(str(path))
    assert cache.get_provider("missing") is None
    cache.save_provider("provider", [
        {"product_type": " CDN ", "product_id": "id-1", "domain": "EXAMPLE.COM"},
        {"product_type": "cdn", "product_id": "id-1", "domain": "example.com"},
        {"product_type": "cdn", "product_id": "id-2", "domain": "other.example.com"},
    ], "2026-10-16T00:00:00")
    restored = ProductBindingStore(str(path))
    payload = restored.get_provider("provider")
    assert payload["updated_at"] == "2026-10-16T00:00:00"
    assert len(payload["bindings"]) == 2
    assert restored.get_all()["provider"]["products"]["cdn"]["id-1"] == [{"domain": "EXAMPLE.COM"}]
    copy = restored.get_all()
    copy["provider"]["products"]["cdn"]["id-1"][0]["domain"] = "changed"
    assert restored.get_provider("provider")["bindings"][0]["domain"] == "EXAMPLE.COM"


@pytest.mark.parametrize("bindings", [
    [None],
    [{"product_type": "cdn", "product_id": "id"}],
    [{"product_type": "", "product_id": "id", "domain": "example.com"}],
])
def test_rejects_invalid_legacy_bindings(tmp_path, bindings):
    cache = ProductBindingStore(str(tmp_path / "bindings.json"))
    with pytest.raises(ValueError):
        cache.save_provider("provider", bindings, "now")


@pytest.mark.parametrize("provider,bindings,updated_at,error", [
    ("", [], "now", ValueError),
    ("provider", {}, "now", TypeError),
    ("provider", [], "", ValueError),
])
def test_save_provider_validates_arguments(tmp_path, provider, bindings, updated_at, error):
    cache = ProductBindingStore(str(tmp_path / "bindings.json"))
    with pytest.raises(error):
        cache.save_provider(provider, bindings, updated_at)


@pytest.mark.parametrize("raw", [
    [],
    {"provider": []},
    {"provider": {"updated_at": 123, "products": {}}},
    {"provider": {"bindings": "not-a-list"}},
    {"provider": {"products": []}},
    {"provider": {"products": {"": {}}}},
    {"provider": {"products": {"cdn": []}}},
    {"provider": {"products": {"cdn": {"": []}}}},
    {"provider": {"products": {"cdn": {"id": "not-a-list"}}}},
    {"provider": {"products": {"cdn": {"id": [""]}}}},
    {"provider": {"products": {"cdn": {"id": [42]}}}},
    {"provider": {"products": {"cdn": {"id": [{}]}}}},
])
def test_rejects_invalid_cache_payloads(tmp_path, raw):
    path = tmp_path / "bindings.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError):
        ProductBindingStore(str(path))


def test_accepts_legacy_products_and_discards_old_extra_fields(tmp_path):
    path = tmp_path / "bindings.json"
    path.write_text(json.dumps({
        "provider": {
            "updated_at": "now",
            "products": {"CDN": {"id-1": [
                "example.com",
                {"domain": "api.example.com", "listener_port": 443},
            ]}},
        },
    }), encoding="utf-8")
    cache = ProductBindingStore(str(path))
    assert cache.get_provider("provider")["bindings"] == [
        {"product_type": "cdn", "product_id": "id-1", "domain": "example.com"},
        {"product_type": "cdn", "product_id": "id-1", "domain": "api.example.com"},
    ]


@pytest.mark.parametrize("field", ["domain_groups", "cert_list", "ecs_list"])
def test_snapshot_rejects_non_list_fields(tmp_path, field):
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps({field: "invalid"}), encoding="utf-8")
    with pytest.raises(ValueError):
        CollectorSnapshotStore(str(path))


def test_snapshot_roundtrip_and_defensive_copy(tmp_path):
    path = tmp_path / "snapshot.json"
    snapshot = CollectorSnapshotStore(str(path))
    snapshot.save([{"domain": "example.com"}], [{"cert": "a"}], [{"id": "i-1"}], "now")
    restored = CollectorSnapshotStore(str(path))
    data = restored.get_all()
    assert data["domain_groups"] == [{"domain": "example.com"}]
    assert data["last_updated"] == "now"
    data["domain_groups"][0]["domain"] = "changed"
    assert restored.get_all()["domain_groups"][0]["domain"] == "example.com"
