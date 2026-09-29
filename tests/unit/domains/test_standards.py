"""Standards editions (ISO Open Data) in the Technical model and certificate records."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import duckdb
import jsonschema
import pytest

from src.ingestion.source_pack_runtime import SourcePackRuntime
from src.ingestion.source_packs import (
    SourcePackConformance,
    SourcePackError,
    SourcePackStore,
    validate_source_pack,
)
from src.ingestion.standards_sources import (
    PROVIDER_CONTRACTS,
    IsoOpenDataAdapter,
    fixture_transport,
    stage_status,
)
from src.kb.standards import (
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    StandardsError,
    StandardsStore,
)

ROOT = Path(__file__).resolve().parents[3]
PACK = ROOT / "config/source_packs/technical.json"
SCOPES = {READ_SCOPE, WRITE_SCOPE, REVIEW_SCOPE, "namespace:global:write"}


def schema(name):
    return jsonschema.Draft7Validator(json.loads((ROOT / f"contracts/schemas/jsonschema/{name}.json").read_text()))


def source():
    value = validate_source_pack(json.loads(PACK.read_text()))
    return value, copy.deepcopy(next(s for s in value["sources"] if s["source_id"] == "iso-open-data"))


@pytest.fixture()
def loaded():
    conn = duckdb.connect(":memory:")
    value, _ = source()
    SourcePackStore(conn).install(value, principal_id="operator", enable=True, now_ms=10)
    clock = iter(range(1_000, 10_000_000))
    runtime = SourcePackRuntime(conn, now=lambda: next(clock), sleep=lambda _d: None)
    for item in value["sources"]:
        runtime.accept_license(value["pack_id"], item["source_id"], principal_id="operator")
    receipt = runtime.run({"pack_id": value["pack_id"], "run_key": "fx", "operation": "catalogue",
                           "source_ids": ["iso-open-data"], "max_results": 500, "max_bytes": 100_000_000,
                           "timeout_ms": 120_000},
                          principal_id="operator", adapters=runtime.fixture_adapters(value["pack_id"], ROOT),
                          dns_resolver=lambda _h: ["8.8.8.8"])
    yield conn, receipt, StandardsStore(conn)
    conn.close()


def test_contracts_conformance_and_stage_mapping():
    assert {k: v["status"] for k, v in PROVIDER_CONTRACTS.items()} == {
        "iso-open-data": "unverified-live", "etsi": "not-implemented", "certification-registries": "not-implemented"}
    assert [stage_status(s) for s in (3020, 6060, 9060, 9093, 9599)] == [
        "under-development", "published", "under-review", "published", "withdrawn"]
    value, _ = source()
    result = SourcePackConformance(ROOT).offline(value)
    assert result["valid"] and next(s for s in result["sources"] if s["source_id"] == "iso-open-data")["records"] == 3


def test_adapter_needs_a_bounded_selection_and_never_fetches_text():
    _, item = source()
    bad = copy.deepcopy(item)
    bad["standards"]["selection"] = {"max_records": 10}
    with pytest.raises(SourcePackError) as caught:
        IsoOpenDataAdapter(bad)
    assert caught.value.code == "unbounded_source"
    pages = json.loads((ROOT / item["fixture"]["path"]).read_text())["native_pages"]
    capped = copy.deepcopy(item)
    capped["standards"]["selection"]["max_records"] = 1
    page = IsoOpenDataAdapter(capped, transport=fixture_transport(pages)).fetch_page({"operation": "catalogue"},
                                                                                     cursor=None)
    assert page.receipt["truncated"] is True and page.receipt["malformed_lines"] == 1 and len(page.records) == 1
    validator = schema("noesis-standard-catalogue-v1")
    for record in IsoOpenDataAdapter(item, transport=fixture_transport(pages)).fetch_page(
            {"operation": "catalogue"}, cursor=None).records:
        assert not list(validator.iter_errors(record["standard_record"]))
        assert "scope" not in record.get("content", "")


def test_editions_supersession_and_amendments_in_the_technical_model(loaded):
    conn, receipt, store = loaded
    assert receipt["status"] == "complete"
    second = store.standard("global", "ISO 99901:2022", scopes=SCOPES)
    assert not list(schema("noesis-standard-edition-v1").iter_errors(second))
    assert second["status"] == "published" and second["content_access"] == "protected-link-only"
    assert {"relation": "supersedes", "object_id": "standard:iso:900001"} in second["relations"]
    assert {"relation": "amends", "subject_id": "standard:iso:900003"} in second["inbound_relations"]
    first = store.standard("global", "ISO 99901:2015", scopes=SCOPES)
    assert first["status"] == "withdrawn"
    row = conn.execute("SELECT object_type, status, version FROM technical_objects WHERE object_id='standard:iso:900002'"
                       ).fetchone()
    assert row == ("standard", "published", "2")
    with pytest.raises(StandardsError) as caught:
        store.standard("global", "ISO 99950:2020", scopes=SCOPES)  # out of the pinned selection
    assert caught.value.code == "not_found"


def test_certificates_are_distinct_dated_and_conflicts_visible(loaded):
    _, _, store = loaded
    base = {"kind": "certificate", "issuer": "Fictional Cert Body", "scheme": "FIX-SEC",
            "certificate_number": "FCB-0001", "valid_from": "2023-01-01", "valid_until": "2025-12-31",
            "standards": ["ISO 99901:2022"], "products": [{"brand": "Exampla", "designation": "EX-27Q4"}],
            "locator": "https://registry.example.org/FCB-0001"}
    store.import_certificates("global", "registry-a", [{**base, "status": "valid"}], scopes=SCOPES, principal_id="p")
    store.import_certificates("global", "registry-b", [{**base, "status": "withdrawn",
                                                       "locator": "https://other.example.org/FCB-0001"}],
                              scopes=SCOPES, principal_id="p")
    now = store.certificate("global", "FCB-0001", scopes=SCOPES, as_of="2026-09-26")
    assert now["status_conflict"] is True
    assert {r["effective_status"] for r in now["records"]} == {"expired", "withdrawn"}
    earlier = store.certificate("global", "FCB-0001", scopes=SCOPES, as_of="2024-06-01")
    assert {r["source"]: r["effective_status"] for r in earlier["records"]} == {"registry-a": "valid",
                                                                               "registry-b": "withdrawn"}
    validator = schema("noesis-certificate-record-v1")
    assert all(not list(validator.iter_errors(r)) for r in now["records"])
    assert len(store.standard("global", "ISO 99901:2022", scopes=SCOPES)["certificates"]) == 2
    with pytest.raises(StandardsError) as caught:
        store.import_certificates("global", "x", [{**base, "kind": "specification"}], scopes=SCOPES, principal_id="p")
    assert caught.value.code == "invalid_record"


def test_certificates_link_to_products_only_by_explicit_identifier(loaded):
    conn, _, store = loaded
    from src.kb.products import ProductStore

    ProductStore(conn)
    conn.execute("INSERT INTO product_identities VALUES ('product-model:x','global','model',NULL,'eprel','Exampla',"
                 "'EX-27Q4',NULL,NULL,'{}','{}','electronic displays','r',1)")
    store.import_certificates("global", "registry-a", [{
        "kind": "certificate", "issuer": "Fictional Cert Body", "certificate_number": "FCB-0002", "status": "valid",
        "products": [{"brand": "exampla", "designation": "EX-27Q4"}, {"brand": "Exampla", "designation": "EX-99"}],
        "locator": "https://registry.example.org/FCB-0002"}], scopes=SCOPES, principal_id="p")
    [link_id] = store.propose_product_links("global", scopes=SCOPES, principal_id="p")["candidates"]
    assert store.certificate("global", "FCB-0002", scopes=SCOPES)["product_links"][0]["state"] == "candidate"
    assert store.review_product_link("global", link_id, "accepted", scopes=SCOPES)["state"] == "linked"
    with pytest.raises(StandardsError) as caught:
        store.certificate("global", "FCB-0002", scopes={READ_SCOPE, "namespace:other:read"})
    assert caught.value.code == "unauthorized"
