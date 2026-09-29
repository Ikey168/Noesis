"""GLEIF LEI identities, ownership assertions and links to existing OpenCorporates records."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import duckdb
import jsonschema
import pytest

from src.ingestion.lei_sources import (
    PROVIDER_CONTRACTS,
    GleifAdapter,
    fixture_transport,
    lei_valid,
)
from src.ingestion.source_pack_runtime import SourcePackRuntime
from src.ingestion.source_packs import (
    SourcePackConformance,
    SourcePackError,
    SourcePackStore,
    validate_source_pack,
)
from src.kb.lei import READ_SCOPE, REVIEW_SCOPE, WRITE_SCOPE, LeiError, LeiStore

ROOT = Path(__file__).resolve().parents[3]
PACK = ROOT / "config/source_packs/economic.json"
FIXTURE = json.loads((ROOT / "tests/fixtures/source_packs/companies-gleif.json").read_text())
CHILD, PARENT, LAPSED = (FIXTURE["leis"][k] for k in ("child", "parent", "lapsed"))
SCOPES = {READ_SCOPE, WRITE_SCOPE, REVIEW_SCOPE, "namespace:global:write"}


def schema(name):
    return jsonschema.Draft7Validator(json.loads((ROOT / f"contracts/schemas/jsonschema/{name}.json").read_text()))


def source():
    value = validate_source_pack(json.loads(PACK.read_text()))
    return value, copy.deepcopy(next(s for s in value["sources"] if s["source_id"] == "gleif-lei"))


@pytest.fixture()
def loaded():
    conn = duckdb.connect(":memory:")
    value, _ = source()
    SourcePackStore(conn).install(value, principal_id="operator", enable=True, now_ms=10)
    clock = iter(range(1_000, 10_000_000))
    runtime = SourcePackRuntime(conn, now=lambda: next(clock), sleep=lambda _d: None)
    for item in value["sources"]:
        runtime.accept_license(value["pack_id"], item["source_id"], principal_id="operator")
    receipt = runtime.run({"pack_id": value["pack_id"], "run_key": "fx", "operation": "entities",
                           "source_ids": ["gleif-lei"], "max_results": 100, "max_bytes": 10_000_000, "max_pages": 20,
                           "timeout_ms": 60_000},
                          principal_id="operator", adapters=runtime.fixture_adapters(value["pack_id"], ROOT),
                          dns_resolver=lambda _h: ["8.8.8.8"])
    yield conn, receipt, LeiStore(conn)
    conn.close()


def test_contracts_and_offline_conformance():
    assert PROVIDER_CONTRACTS["official-registers"]["status"] == "not-implemented"
    assert PROVIDER_CONTRACTS["opencorporates"]["status"] == "reused"
    assert lei_valid(CHILD) and not lei_valid(CHILD[:-2] + "00")
    value, _ = source()
    result = SourcePackConformance(ROOT).offline(value)
    assert result["valid"] and next(s for s in result["sources"] if s["source_id"] == "gleif-lei")["records"] == 7


def test_adapter_validates_selection_and_classifies():
    _, item = source()
    bad = copy.deepcopy(item)
    bad["lei"]["leis"] = [CHILD[:-2] + "00"]
    with pytest.raises(SourcePackError) as caught:
        GleifAdapter(bad, transport=fixture_transport(FIXTURE["native_pages"]))
    assert caught.value.code == "unbounded_source"
    pages = copy.deepcopy(FIXTURE["native_pages"])
    pages[0]["body"]["data"]["attributes"]["lei"] = PARENT
    adapter = GleifAdapter(item, transport=fixture_transport(pages))
    with pytest.raises(SourcePackError) as caught:
        adapter.fetch_page({"operation": "entities"}, cursor=None)
    assert caught.value.code == "schema_drift"
    pages = copy.deepcopy(FIXTURE["native_pages"])
    pages[0]["status"] = 429
    with pytest.raises(SourcePackError) as caught:
        GleifAdapter(item, transport=fixture_transport(pages)).fetch_page({"operation": "entities"}, cursor=None)
    assert caught.value.code == "rate_limited"


def test_entities_keep_places_register_pointer_parents_and_exceptions(loaded):
    _, receipt, store = loaded
    assert receipt["status"] == "complete"
    child = store.entity("global", CHILD, scopes=SCOPES)
    assert not list(schema("noesis-lei-entity-v1").iter_errors(child))
    assert child["places"]["legal_address"]["city"] == "Berlin" and child["places"]["headquarters"]["city"] == "München"
    assert child["official_register"] == {"authority_id": "RA000242", "registered_as": "HRB 123456 B",
                                          "note": child["official_register"]["note"]}
    assert child["other_names"][0]["type"] == "PREVIOUS_LEGAL_NAME"
    assert child["parents"]["direct"]["latest"]["parent_lei"] == PARENT
    assert {r["kind"] for r in child["reporting"]} == {"no_exception_reported"}
    parent = store.entity("global", PARENT, scopes=SCOPES)
    assert {(r["level"], r["reason"]) for r in parent["reporting"] if r["kind"] == "reporting_exception"} == {
        ("direct", "NATURAL_PERSONS"), ("ultimate", "NATURAL_PERSONS")}
    assert {r["kind"] for r in parent["reporting"]} >= {"no_relationship_reported"}
    lapsed = store.entity("global", LAPSED, scopes=SCOPES)
    assert lapsed["registration"]["stale"] is True and lapsed["successor"]["lei"] == FIXTURE["leis"]["successor"]


def test_conflicting_parent_assertions_are_kept_and_as_of(loaded):
    _, _, store = loaded
    first = store.parents("global", CHILD)["direct"]["history"][0]["observed_at_ms"]
    other = {"contract": "noesis-lei-part-v1", "provider": "gleif", "lei": CHILD, "part": "direct-parent-relationship",
             "attributes": {"relationship": {"endNode": {"id": LAPSED}, "status": "ACTIVE", "periods": []}},
             "raw_sha256": "e" * 64}
    store.observe_page("global", [{"lei_record": other}], run_id="later", page_receipt={})
    direct = store.parents("global", CHILD)["direct"]
    assert direct["conflicting_parents"] is True and direct["latest"]["parent_lei"] == LAPSED
    earlier = store.parents_as_of("global", CHILD, first, scopes=SCOPES)
    assert earlier["parents"]["direct"]["latest"]["parent_lei"] == PARENT


def test_links_to_existing_opencorporates_records_are_reviewable(loaded):
    conn, _, store = loaded
    from src.ingestion.provider_execution import CapturedResponse
    from src.ingestion.regional_providers import RegionalClient, RegionalEvidenceStore

    record = RegionalClient._company_record({"jurisdiction_code": "de", "company_number": "HRB 123456 B",
                                             "name": "Exampla Berlin GmbH (fiktiv)"})
    raw = b"{}"
    import hashlib

    RegionalEvidenceStore(conn).ingest(
        [record], CapturedResponse(raw, {"digest": hashlib.sha256(raw).hexdigest(), "observed_at_ms": 5}),
        namespace="global", principal_id="p", scopes={"operator"}, reuse_notice="authored fixture")
    [candidate] = store.propose_registry_links("global", scopes=SCOPES, principal_id="p")["candidates"]
    assert (candidate["lei"], candidate["state"], candidate["basis"]) == (CHILD, "candidate", "registry-number-match")
    assert "not the official register" in candidate["evidence"]["note"]
    reviewed = store.review_link("global", candidate["link_id"], "accepted", "same HRB and court", scopes=SCOPES,
                                 principal_id="r")
    assert reviewed["state"] == "linked"
    assert not list(schema("noesis-company-identity-link-v1").iter_errors(reviewed))
    with pytest.raises(LeiError) as caught:
        store.entity("global", CHILD, scopes={READ_SCOPE, "namespace:other:read"})
    assert caught.value.code == "unauthorized"
