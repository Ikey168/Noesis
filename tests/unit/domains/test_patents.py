"""Patent publications in Research/Technical via EPO OPS (authored OPS XML fixtures)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import duckdb
import jsonschema
import pytest

from src.ingestion.patent_sources import (
    PROVIDER_CONTRACTS,
    EpoOpsAdapter,
    fixture_transport,
)
from src.ingestion.source_pack_runtime import SourcePackRuntime
from src.ingestion.source_packs import (
    SourcePackConformance,
    SourcePackError,
    SourcePackStore,
    validate_source_pack,
)
from src.kb.patents import (
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    PatentError,
    PatentStore,
)

ROOT = Path(__file__).resolve().parents[3]
PACK = ROOT / "config/source_packs/research.json"
SCOPES = {READ_SCOPE, WRITE_SCOPE, REVIEW_SCOPE, "namespace:global:write"}
EP, DE = "EP.3999001.A1", "DE.102020000001.A1"


def schema(name):
    return jsonschema.Draft7Validator(json.loads((ROOT / f"contracts/schemas/jsonschema/{name}.json").read_text()))


def source():
    value = validate_source_pack(json.loads(PACK.read_text()))
    return value, copy.deepcopy(next(s for s in value["sources"] if s["source_id"] == "epo-ops-patents"))


def pages():
    return copy.deepcopy(json.loads((ROOT / source()[1]["fixture"]["path"]).read_text())["native_pages"])


def fetch_all(adapter):
    records, receipts, cursor = [], [], None
    for _ in range(20):
        page = adapter.fetch_page({"operation": "publications", "parameters": {}}, cursor=cursor)
        records += page.records
        receipts.append(page.receipt)
        cursor = page.next_cursor
        if cursor is None:
            return records, receipts
    return records, receipts


@pytest.fixture()
def loaded():
    conn = duckdb.connect(":memory:")
    value, _ = source()
    SourcePackStore(conn).install(value, principal_id="operator", enable=True, now_ms=10)
    clock = iter(range(1_000, 10_000_000))
    runtime = SourcePackRuntime(conn, now=lambda: next(clock), sleep=lambda _d: None)
    for item in value["sources"]:
        runtime.accept_license(value["pack_id"], item["source_id"], principal_id="operator")
    receipt = runtime.run({"pack_id": value["pack_id"], "run_key": "fx", "operation": "publications",
                           "source_ids": ["epo-ops-patents"], "max_results": 100, "max_bytes": 10_000_000,
                           "timeout_ms": 60_000},
                          principal_id="operator", adapters=runtime.fixture_adapters(value["pack_id"], ROOT),
                          dns_resolver=lambda _h: ["8.8.8.8"], secret_resolver=lambda _r: "k:s")
    yield conn, receipt, PatentStore(conn)
    conn.close()


def test_one_provider_path_and_offline_conformance():
    assert PROVIDER_CONTRACTS["epo-ops"]["status"] == "unverified-live"
    assert {k: v["status"] for k, v in PROVIDER_CONTRACTS.items() if k != "epo-ops"} == {
        "espacenet": "not-implemented", "wipo-patentscope": "not-implemented"}
    value, _ = source()
    result = SourcePackConformance(ROOT).offline(value)
    assert result["valid"] and next(s for s in result["sources"] if s["source_id"] == "epo-ops-patents")["records"] == 6


def test_adapter_authenticates_classifies_and_bounds():
    _, item = source()
    with pytest.raises(SourcePackError) as caught:
        fetch_all(EpoOpsAdapter(item, transport=fixture_transport(pages()), secret=None))
    assert caught.value.code == "authentication_failed"
    records, receipts = fetch_all(EpoOpsAdapter(item, transport=fixture_transport(pages()), secret="k:s"))
    assert [(r["publication"], r["part"], r["outcome"]) for r in receipts][-2:] == [
        (DE, "legal", "not_available"), (DE, "claims", "not_available")]
    validator = schema("noesis-patent-part-v1")
    assert all(not list(validator.iter_errors(r["patent_record"])) for r in records)
    throttled = pages()
    throttled[0].update(status=403, headers={"X-Throttling-Control": "overloaded (retrieval=black:0)"})
    with pytest.raises(SourcePackError) as caught:
        fetch_all(EpoOpsAdapter(item, transport=fixture_transport(throttled), secret="k:s"))
    assert caught.value.code == "rate_limited"
    broken = pages()
    broken[0]["body"] = "<not-xml"
    with pytest.raises(SourcePackError) as caught:
        fetch_all(EpoOpsAdapter(item, transport=fixture_transport(broken), secret="k:s"))
    assert caught.value.code == "schema_drift"
    bad = copy.deepcopy(item)
    bad["patent"]["publications"] = ["EP 3999001"]
    with pytest.raises(SourcePackError) as caught:
        EpoOpsAdapter(bad, transport=fixture_transport(pages()), secret="k:s")
    assert caught.value.code == "unbounded_source"


def test_publication_family_citations_and_legal_status(loaded):
    _, receipt, store = loaded
    assert receipt["status"] == "complete"
    ep = store.publication("global", EP, scopes=SCOPES)
    assert not list(schema("noesis-patent-publication-v1").iter_errors(ep))
    assert {t["language"] for t in ep["biblio"]["titles"]} == {"en", "de", "fr"}
    priority = ep["biblio"]["priorities"][0]
    assert (priority["country"], priority["number"], priority["date"]) == ("DE", "102019000001", "2019-01-05")
    assert ep["family"]["asserted_by"] == "EPO OPS (INPADOC)" and len(ep["family"]["members"]) == 4
    assert [e["code"] for e in ep["legal_status"]["events"]] == ["EPIDOSNREQ1", "PG25"]
    assert ep["legal_status"]["events"][1]["date"] == "2024-03-01"
    assert [c["number"] for c in ep["claims"]["items"]] == [1, 2]
    [link] = ep["links"]
    assert (link["target_id"], link["basis"], link["state"]) == (
        "doi:10.9999/fixture.patent.1", "patent-npl-citation", "linked")
    de = store.publication("global", DE, scopes=SCOPES)
    assert de["legal_status"]["state"] == "not_available" and de["claims"]["state"] == "not_available"
    family = store.family_members("global", EP, scopes=SCOPES)
    assert family["acquired_members"] == [DE, EP]  # the B1 grant and US member were not acquired
    assert "EP.3999001.B1" in family["not_acquired_members"]
    assert "freedom to operate" in ep["notice"]


def test_changed_biblio_is_a_new_revision(loaded):
    _, _, store = loaded
    record = {"contract": "noesis-patent-part-v1", "provider": "epo-ops", "publication": EP, "part": "biblio",
              "data": {"titles": [{"language": "en", "value": "Corrected title"}], "citations": []},
              "raw_sha256": "f" * 64}
    store.observe_page("global", [{"id": "x", "patent_record": record}], run_id="later", page_receipt={})
    ep = store.publication("global", EP, scopes=SCOPES)
    assert len(ep["biblio_revisions"]) == 2 and ep["biblio"]["titles"][0]["value"] == "Corrected title"


def test_links_are_sourced_or_reviewed(loaded):
    _, _, store = loaded
    candidate = store.propose_link("global", EP, "organization", "lei:FIXTURELEI000000001",
                                   "applicant name matches the LEI legal name", scopes=SCOPES, principal_id="p")
    assert candidate["state"] == "candidate"
    reviewed = store.review_link("global", candidate["link_id"], "accepted", "registry number confirmed",
                                 scopes=SCOPES, principal_id="r")
    assert (reviewed["state"], reviewed["basis"]) == ("linked", "reviewed-assertion")
    assert not list(schema("noesis-patent-link-v1").iter_errors(reviewed))
    sourced = next(link for link in store.links("global") if link["basis"] == "patent-npl-citation")
    with pytest.raises(PatentError) as caught:
        store.review_link("global", sourced["link_id"], "rejected", "x", scopes=SCOPES, principal_id="r")
    assert caught.value.code == "sourced_link"
    with pytest.raises(PatentError) as caught:
        store.publication("global", EP, scopes={READ_SCOPE, "namespace:other:read"})
    assert caught.value.code == "unauthorized"
