"""Products pack: adapters, projection, identities, matching, refresh, documents and comparison.

Everything replays the authored Open Icecat and EPREL envelopes in
``tests/fixtures/source_packs/products-*.json`` (a fictional brand in the
documented provider shapes) through the real adapters and source-pack
runtime. No test touches the network; live acceptance is a separate script.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import duckdb
import jsonschema
import pytest

from src.domains.pack_format import PackManifest, validate_manifest
from src.domains.pack_install import install_manifest, installed_packs, uninstall
from src.ingestion.product_sources import (
    EprelProductAdapter,
    IcecatProductAdapter,
    fixture_transport,
    gtin_state,
)
from src.ingestion.source_pack_runtime import SourcePackRuntime
from src.ingestion.source_packs import (
    SUPPORTED_CONNECTORS,
    SourcePackConformance,
    SourcePackError,
    SourcePackStore,
    validate_source_pack,
)
from src.kb.products import (
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    ProductError,
    ProductStore,
    acquire_documents,
    designation_key,
    document_policy,
    readiness,
)

ROOT = Path(__file__).resolve().parents[3]
PACK = ROOT / "config/source_packs/products.json"
SCHEMAS = ROOT / "contracts/schemas/jsonschema"
SCOPES = {READ_SCOPE, WRITE_SCOPE, REVIEW_SCOPE, "namespace:global:write"}
PUBLIC_DNS = lambda _host: ["8.8.8.8"]
FIXTURES = {
    "icecat-displays": ROOT / "tests/fixtures/source_packs/products-icecat.json",
    "eprel-displays": ROOT / "tests/fixtures/source_packs/products-eprel.json",
}


def schema(name: str) -> dict:
    return json.loads((SCHEMAS / f"{name}.json").read_text())


def manifest() -> dict:
    return validate_source_pack(json.loads(PACK.read_text()))


def source(value: dict, source_id: str) -> dict:
    return next(item for item in value["sources"] if item["source_id"] == source_id)


def pages(source_id: str) -> list[dict]:
    return copy.deepcopy(json.loads(FIXTURES[source_id].read_text())["native_pages"])


def install(conn, value: dict | None = None):
    value = value or manifest()
    SourcePackStore(conn).install(value, principal_id="operator", enable=True, now_ms=10)
    clock = iter(range(1_000, 10_000_000))
    runtime = SourcePackRuntime(conn, now=lambda: next(clock), sleep=lambda _d: None)
    for item in value["sources"]:
        runtime.accept_license(value["pack_id"], item["source_id"], principal_id="operator")
    return value, runtime


def run(runtime, value, key, *, adapters=None, fault=None, source_ids=None):
    request = {"pack_id": value["pack_id"], "run_key": key, "operation": "models", "max_results": 50,
               "max_bytes": 5_000_000, "timeout_ms": 60_000}
    if source_ids:
        request["source_ids"] = source_ids
    return runtime.run(
        request, principal_id="operator",
        adapters=adapters if adapters is not None else runtime.fixture_adapters(value["pack_id"], ROOT),
        dns_resolver=PUBLIC_DNS, secret_resolver=lambda _ref: "fixture-credential", fault=fault,
    )


def compiled(runtime, value, source_id, native_pages, *, secret="fixture-credential"):
    return runtime.factory.compile(source(value, source_id), transport=fixture_transport(native_pages),
                                   secret=secret)


@pytest.fixture()
def loaded():
    conn = duckdb.connect(":memory:")
    value, runtime = install(conn)
    receipt = run(runtime, value, "fixture-1")
    yield conn, value, runtime, receipt, ProductStore(conn)
    conn.close()


def variant(store, provider, designation):
    return next(v for v in store.lookup("global", scopes=SCOPES, limit=100)["variants"]
                if v["provider"] == provider and v["designation"] == designation)


def accept_proposed(store):
    candidates = store.propose_matches("global", scopes=SCOPES, principal_id="matcher")["candidates"]
    for candidate in candidates:
        if candidate["candidate_state"] == "proposed":
            store.review_match("global", candidate["match_id"], "accepted",
                               "designation, diagonal and resolution agree", scopes=SCOPES, principal_id="reviewer")
    return candidates


# ------------------------------------------------------------------ adapters


def test_pack_declares_implemented_connectors_and_passes_offline_conformance():
    value = manifest()
    assert {item["connector"] for item in value["sources"]} == {"icecat", "eprel"} <= SUPPORTED_CONNECTORS
    result = SourcePackConformance(ROOT).offline(value)
    assert result["valid"]
    assert {item["source_id"]: item["records"] for item in result["sources"]} == {
        "icecat-displays": 6, "eprel-displays": 4}


def test_gtin_states_keep_the_original_string():
    assert gtin_state("04012345000016") == {"value": "04012345000016", "state": "valid"}
    assert gtin_state("4012345000029")["state"] == "invalid_checksum"
    assert gtin_state("12-34")["state"] == "invalid_format"
    assert gtin_state(None) == {"value": None, "state": "absent"}


def _adapter(cls, source_id, native_pages, *, secret="fixture-credential", **overrides):
    value = manifest()
    item = copy.deepcopy(source(value, source_id))
    item["product"].update(overrides)
    return cls(item, transport=fixture_transport(native_pages), secret=secret)


def _one(adapter, cursor=None):
    return adapter.fetch_page({"operation": "models", "parameters": {}, "limit": 20}, cursor=cursor)


@pytest.mark.parametrize(("status", "body", "headers", "code"), [
    (401, {"msg": "Error", "Message": "Invalid token"}, {}, "authentication_failed"),
    (429, {"msg": "Error"}, {"Retry-After": "7"}, "rate_limited"),
    (503, None, {}, "source_unavailable"),
    (200, {"msg": "OK", "data": "not an object"}, {}, "schema_drift"),
    (200, "{not json", {}, "schema_drift"),
    (200, {"msg": "OK", "data": {"GeneralInfo": {"Title": "no id"}}}, {}, "schema_drift"),
])
def test_icecat_failures_are_classified_not_swallowed(status, body, headers, code):
    native = pages("icecat-displays")[:1]
    native[0].update({"status": status, "body": body, "headers": headers})
    with pytest.raises(SourcePackError) as caught:
        _one(_adapter(IcecatProductAdapter, "icecat-displays", native, selection=[native[0]["selector"]]))
    assert caught.value.code == code
    if code == "rate_limited":
        assert caught.value.details["retry_after_ms"] == 7000


def test_coverage_outcomes_are_receipts_not_failures():
    native = pages("icecat-displays")
    adapter = _adapter(IcecatProductAdapter, "icecat-displays", native)
    outcomes, cursor = [], None
    while True:
        page = _one(adapter, cursor)
        outcomes.append((page.receipt["selector"].get("label"), page.receipt["model_outcome"], len(page.records)))
        cursor = page.next_cursor
        if cursor is None:
            break
    assert outcomes[-2:] == [("unknown GTIN (not found)", "not_found", 0),
                             ("non-sponsoring brand (outside Open Icecat)", "outside_open_catalogue", 0)]
    assert all(outcome == "returned" for _, outcome, _ in outcomes[:-2])


def test_selection_is_bounded_explicit_and_checkpointed():
    native = pages("icecat-displays")
    with pytest.raises(SourcePackError) as caught:
        _adapter(IcecatProductAdapter, "icecat-displays", native, selection=[])
    assert caught.value.code == "unbounded_source"
    with pytest.raises(SourcePackError) as caught:
        _adapter(IcecatProductAdapter, "icecat-displays", native, selection=[{"title": "Exampla monitor"}])
    assert caught.value.code == "invalid_mapping"
    adapter = _adapter(IcecatProductAdapter, "icecat-displays", native)
    with pytest.raises(SourcePackError) as caught:
        adapter.fetch_page({"operation": "models", "parameters": {"q": "monitor"}}, cursor=None)
    assert caught.value.code == "parameter_forbidden"
    cursor = _one(adapter).next_cursor
    narrowed = _adapter(IcecatProductAdapter, "icecat-displays", native, selection=native[0:2] and
                        [native[1]["selector"], native[0]["selector"]])
    with pytest.raises(SourcePackError) as caught:
        _one(narrowed, cursor)
    assert caught.value.code == "cursor_drift"
    big = copy.deepcopy(native[:1])
    big[0]["body"]["data"]["GeneralInfo"]["Title"] = "x" * 3_000_000
    with pytest.raises(SourcePackError) as caught:
        _one(_adapter(IcecatProductAdapter, "icecat-displays", big, selection=[big[0]["selector"]]))
    assert caught.value.code == "response_too_large"


def test_eprel_needs_an_api_key_and_tolerates_missing_category_fields():
    native = pages("eprel-displays")
    with pytest.raises(SourcePackError) as caught:
        _one(_adapter(EprelProductAdapter, "eprel-displays", native, secret=None))
    assert caught.value.code == "authentication_failed"
    sparse = copy.deepcopy(native[:1])
    for field in ("powerOnModeHDR", "energyClassHDR", "diagonalInch"):
        sparse[0]["body"].pop(field)
    page = _one(_adapter(EprelProductAdapter, "eprel-displays", sparse, selection=[sparse[0]["selector"]]))
    names = {item["native_name"] for item in page.records[0]["product_record"]["attributes"]}
    assert "powerOnModeHDR" not in names and "powerOnModeSDR" in names
    drift = copy.deepcopy(native[:1])
    drift[0]["body"].pop("eprelRegistrationNumber")
    with pytest.raises(SourcePackError) as caught:
        _one(_adapter(EprelProductAdapter, "eprel-displays", drift, selection=[drift[0]["selector"]]))
    assert caught.value.code == "schema_drift"


def test_records_follow_the_product_record_contract():
    validator = jsonschema.Draft7Validator(schema("noesis-product-record-v1"))
    for source_id, cls in (("icecat-displays", IcecatProductAdapter), ("eprel-displays", EprelProductAdapter)):
        adapter = _adapter(cls, source_id, pages(source_id))
        cursor = None
        while True:
            page = _one(adapter, cursor)
            for record in page.records:
                assert not list(validator.iter_errors(record["product_record"]))
            cursor = page.next_cursor
            if cursor is None:
                break


# ------------------------------------------------------ runtime + identities


def test_fixture_run_projects_both_providers_with_selection_outcomes(loaded):
    conn, _, _, receipt, store = loaded
    assert receipt["status"] == "complete"
    projection = {item["source_id"]: item["projection"] for item in receipt["sources"]}
    assert projection == {
        "icecat-displays": {"refresh_advanced": True, "reason": None, "projected_models": 8, "selection_size": 8},
        "eprel-displays": {"refresh_advanced": True, "reason": None, "projected_models": 5, "selection_size": 5},
    }
    outcomes = store.selection_outcomes("global", receipt["run_id"])
    assert {o["outcome"] for o in outcomes} == {"returned", "not_found", "outside_open_catalogue"}
    variants = store.lookup("global", scopes=SCOPES, limit=100)
    assert variants["count"] == 10
    assert not list(jsonschema.Draft7Validator(schema("noesis-product-identity-v1")).iter_errors(variants))
    assert conn.execute("SELECT count(*) FROM source_pack_quarantine").fetchone()[0] == 0


def test_identities_keep_variants_identifiers_and_conflicts_distinct(loaded):
    _, _, _, _, store = loaded
    q4 = variant(store, "icecat", "EX-27Q4")
    assert q4["identifiers"]["gtin"] == [{"value": "04012345000016", "state": "valid"}]
    assert variant(store, "icecat", "EX-32U8")["identifiers"]["gtin"][0]["state"] == "invalid_checksum"
    assert variant(store, "icecat", "EX-32U8UK")["identifiers"]["gtin"] == []
    uk, eu = variant(store, "icecat", "EX-32U8UK"), variant(store, "icecat", "EX-32U8")
    assert uk["model_id"] != eu["model_id"]  # regional version is its own model
    wide = variant(store, "icecat", "EX-34Q4")
    assert wide["model_id"] != q4["model_id"] and wide["family"] == q4["family"] == "ProView"
    bundle, base = variant(store, "icecat", "EX-24F1B"), variant(store, "icecat", "EX-24F1")
    assert bundle["model_id"] != base["model_id"]
    assert bundle["identifier_conflicts"] == [
        {"identifier_kind": "gtin", "value": "4012345000030", "other_variants": [base["variant_id"]]}]
    # The same designation from two providers is two provider-scoped models.
    assert variant(store, "eprel", "EX-27Q4")["model_id"] != q4["model_id"]
    assert designation_key("EX-27Q4") == designation_key("ex 27q4") != designation_key("EX-027Q4")
    assert [v["designation"] for v in store.lookup("global", scopes=SCOPES, designation="ex27q4")["variants"]] \
        == ["EX-27Q4", "EX-27Q4"]


def test_repeat_runs_are_idempotent(loaded):
    conn, value, runtime, _, store = loaded
    before = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
              for t in ("product_identities", "product_revisions", "product_assertions", "product_document_links")}
    again = run(runtime, value, "fixture-2")
    assert again["status"] == "complete"
    after = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in before}
    assert after == before
    assert conn.execute("SELECT count(DISTINCT run_id) FROM product_revision_observations").fetchone()[0] == 2
    assert store.lookup("global", scopes=SCOPES)["count"] == 10


def test_crash_between_projection_and_checkpoint_recovers_without_duplicates():
    conn = duckdb.connect(":memory:")
    value, runtime = install(conn)

    def crash(source_id, pages_done):
        if source_id == "icecat-displays" and pages_done == 3:
            raise RuntimeError("injected crash after page 3 checkpoint")

    with pytest.raises(RuntimeError):
        run(runtime, value, "crashy", fault=crash, source_ids=["icecat-displays"])
    history = conn.execute("SELECT count(*) FROM product_refresh").fetchone()[0]
    assert history == 0  # the interrupted run never advanced a refresh marker
    partial = conn.execute("SELECT count(*) FROM product_revisions").fetchone()[0]
    assert partial == 3
    resumed = run(runtime, value, "after-crash", source_ids=["icecat-displays"])
    assert resumed["status"] == "complete"
    assert conn.execute("SELECT count(*) FROM product_revisions").fetchone()[0] == 6
    assert conn.execute(
        "SELECT count(*) FROM (SELECT variant_id, attribute, mode, native_name, count(*) c FROM product_assertions "
        "GROUP BY ALL HAVING c > 1)").fetchone()[0] == 0
    assert conn.execute("SELECT completed_run_id FROM product_refresh").fetchone()[0] == resumed["run_id"]
    conn.close()


# ------------------------------------------------------------- normalization


def test_normalization_keeps_native_values_modes_and_receipts(loaded):
    conn, _, _, _, store = loaded
    eprel = store.assertions("global", variant(store, "eprel", "EX-27Q4")["variant_id"])
    by = {(a["attribute"], a["native_name"]): a for a in eprel}
    inch = by[("diagonal", "diagonalInch")]
    assert (inch["native_value"], inch["native_unit"], inch["normalized_value"], inch["normalized_unit"]) == (
        "27", "in", "68.6", "cm")
    receipt = conn.execute("SELECT request_json, result_json FROM quantitative_calculations WHERE calculation_id=?",
                           [inch["calculation_id"]]).fetchone()
    assert json.loads(receipt[1])["value"] == "68.6" and json.loads(receipt[0])["value"] == "27"
    assert by[("on_mode_power", "powerOnModeSDR")]["mode"] == "sdr"
    assert by[("on_mode_power", "powerOnModeHDR")]["mode"] == "hdr"
    assert by[("energy_class", "energyClassSDR")]["conditions"]["label_scheme"] == "EU_2019_2013"
    assert by[("resolution", "resolutionHorizontalPixels+resolutionVerticalPixels")]["normalized_value"] == "2560x1440"
    assert all(a["assertion_kind"] == "supplier-registration" for a in eprel)
    icecat = store.assertions("global", variant(store, "icecat", "EX-27Q4")["variant_id"])
    typical = next(a for a in icecat if a["attribute"] == "on_mode_power")
    assert typical["mode"] == "typical" and typical["locator"]["json_pointer"].startswith("/data/FeaturesGroups/")
    assert {a["assertion_kind"] for a in icecat} == {"brand-authorised-content"}


def test_unknown_or_incompatible_units_stay_explicit():
    conn = duckdb.connect(":memory:")
    store = ProductStore(conn)
    assert store._normalize("global", {"attribute": "diagonal", "native_value": "68,6", "native_unit": None})["state"] \
        == "unit_unknown"
    assert store._normalize("global", {"attribute": "width", "native_value": "612", "native_unit": "kWh"})["state"] \
        == "dimensional_error"
    assert store._normalize("global", {"attribute": "diagonal", "native_value": "about", "native_unit": "cm"})["state"] \
        == "unparseable"
    assert store._normalize("global", {"attribute": "diagonal", "native_value": "68,6", "native_unit": "cm"})["value"] \
        == "68.6"
    conn.close()


# ------------------------------------------------------------------ matching


def test_matching_proposes_only_corroborated_identifier_matches(loaded):
    _, _, _, _, store = loaded
    candidates = store.propose_matches("global", scopes=SCOPES, principal_id="matcher")["candidates"]
    validator = jsonschema.Draft7Validator(schema("noesis-product-match-v1"))
    assert all(not list(validator.iter_errors(c)) for c in candidates)
    models = {v["model_id"]: v["designation"] for v in store.lookup("global", scopes=SCOPES, limit=100)["variants"]}
    summary = sorted((models[c["left_model_id"]], models[c["right_model_id"]], c["candidate_state"]) for c in candidates)
    assert summary == [
        ("EX-24F1", "EX-24F1", "proposed"),
        ("EX-24F1B", "EX-24F1", "ambiguous"),
        ("EX-27Q4", "EX-27Q4", "proposed"),
        ("EX-32U8", "EX-32U8", "proposed"),
        ("EX-32U8UK", "EX-32U8", "ambiguous"),
    ]
    assert all(c["review_state"] == "unreviewed" for c in candidates)
    # Rerunning proposal is deterministic and adds nothing.
    assert [c["match_id"] for c in store.propose_matches("global", scopes=SCOPES, principal_id="m")["candidates"]] \
        == [c["match_id"] for c in candidates]


def test_review_is_append_only_and_reversible(loaded):
    _, _, _, _, store = loaded
    candidate = next(c for c in store.propose_matches("global", scopes=SCOPES, principal_id="m")["candidates"]
                     if c["candidate_state"] == "ambiguous")
    with pytest.raises(ProductError) as caught:
        store.review_match("global", candidate["match_id"], "accepted", "ok", scopes=SCOPES - {REVIEW_SCOPE},
                           principal_id="r")
    assert caught.value.code == "unauthorized"
    store.review_match("global", candidate["match_id"], "deferred", "need the UK label", scopes=SCOPES, principal_id="r1")
    store.review_match("global", candidate["match_id"], "accepted", "same panel", scopes=SCOPES, principal_id="r2")
    reversed_ = store.review_match("global", candidate["match_id"], "rejected", "UK SKU differs", scopes=SCOPES,
                                   principal_id="r3")
    assert [h["decision"] for h in reversed_["review_history"]] == ["deferred", "accepted", "rejected"]
    assert reversed_["review_state"] == "rejected"
    assert store.accepted_equivalents("global", candidate["left_model_id"]) == set()


def test_contradicting_evidence_blocks_acceptance():
    conn = duckdb.connect(":memory:")
    value, runtime = install(conn)
    icecat = [p for p in pages("icecat-displays") if p["selector"].get("product_code") == "EX-34Q4"]
    eprel = copy.deepcopy(pages("eprel-displays")[:1])
    eprel[0]["body"].update({"modelIdentifier": "EX-34Q4", "diagonalCm": 60.5, "diagonalInch": 23.8})
    for item in value["sources"]:
        item["product"]["selection"] = [(icecat if item["connector"] == "icecat" else eprel)[0]["selector"]]
    value = validate_source_pack({k: v for k, v in value.items() if k not in {"manifest_hash", "contract"}}
                                 | {"sources": [{k: v for k, v in s.items() if k != "source_hash"}
                                                for s in value["sources"]]})
    SourcePackStore(conn).install({**value, "version": "1.0.1"}, principal_id="operator", enable=True, now_ms=11)
    installed = runtime._manifest(value["pack_id"])[0]
    adapters = {"icecat-displays": compiled(runtime, installed, "icecat-displays", icecat),
                "eprel-displays": compiled(runtime, installed, "eprel-displays", eprel)}
    for item in installed["sources"]:
        runtime.accept_license(installed["pack_id"], item["source_id"], principal_id="operator")
    assert run(runtime, installed, "conflict", adapters=adapters)["status"] == "complete"
    store = ProductStore(conn)
    [candidate] = store.propose_matches("global", scopes=SCOPES, principal_id="m")["candidates"]
    assert candidate["candidate_state"] == "contradicted" and "diagonal differs" in candidate["reasons"][0]
    with pytest.raises(ProductError) as caught:
        store.review_match("global", candidate["match_id"], "accepted", "looks same", scopes=SCOPES, principal_id="r")
    assert caught.value.code == "contradicted_match"
    conn.close()


# ------------------------------------------------------------------- refresh


def _eprel_only(runtime, value, native):
    return {"eprel-displays": compiled(runtime, value, "eprel-displays", native)}


def test_changed_specification_supersedes_but_keeps_history(loaded):
    _, value, runtime, _, store = loaded
    native = pages("eprel-displays")
    native[1]["body"].update({"versionNumber": 3, "energyConsumption1000hSDR": 34})
    assert run(runtime, value, "update", adapters=_eprel_only(runtime, value, native),
               source_ids=["eprel-displays"])["status"] == "complete"
    u8 = variant(store, "eprel", "EX-32U8")
    current = {a["native_name"]: a["native_value"] for a in store.assertions("global", u8["variant_id"])}
    assert current["energyConsumption1000hSDR"] == "34"
    inspected = store.inspect("global", u8["variant_id"], scopes=SCOPES)["variants"][0]
    assert [r["provider_revision"] for r in inspected["revisions"]] == ["version 2", "version 3"]
    assert {a["native_value"] for a in inspected["superseded_assertions"]
            if a["native_name"] == "energyConsumption1000hSDR"} == {"36"}
    # An older revision delivered later never replaces the newer one.
    assert run(runtime, value, "stale-replay", source_ids=["eprel-displays"])["status"] == "complete"
    assert variant(store, "eprel", "EX-32U8")["provider_revision"] == "version 3"


def test_only_explicit_provider_status_withdraws(loaded):
    conn, value, runtime, first, store = loaded
    assert variant(store, "eprel", "EX-22E0")["record_state"] == "withdrawn"
    assert store.coverage("global", variant(store, "eprel", "EX-22E0")["variant_id"])["status_basis"] \
        == "provider-declared"
    # Vanished access: the run fails, nothing is withdrawn, the marker stays.
    denied = {"eprel-displays": compiled(runtime, value, "eprel-displays", pages("eprel-displays"), secret=None)}
    failed = run(runtime, value, "no-key", adapters=denied, source_ids=["eprel-displays"])
    assert failed["sources"][0]["status"] == "failed"
    assert failed["sources"][0]["projection"]["refresh_advanced"] is False
    assert variant(store, "eprel", "EX-27Q4")["record_state"] == "published"
    marker = conn.execute("SELECT completed_run_id FROM product_refresh WHERE source_id='eprel-displays'").fetchone()
    assert marker[0] == first["run_id"]
    # Partial pagination (page budget smaller than the selection) never advances the marker.
    item = source(value, "eprel-displays")
    short = copy.deepcopy(item)
    short["budgets"]["max_pages"] = 2
    short.pop("source_hash")
    from src.ingestion.source_packs import _digest

    short["source_hash"] = _digest(short)
    partial = EprelProductAdapter(short, transport=fixture_transport(pages("eprel-displays")), secret="k")
    outcome = store.finish_source("partial-run", "eprel-displays", "global", "complete", 5)
    assert outcome == {"refresh_advanced": False, "reason": "no_projected_selection", "projected_models": 0,
                       "selection_size": 5}
    assert partial.describe()["limits"]["max_pages"] == 2


def test_narrowed_selection_is_not_discontinuation(loaded):
    _, value, runtime, _, store = loaded
    narrowed = [p for p in pages("eprel-displays") if p["selector"]["registration_number"] == "1100001"]
    SourcePackStore(runtime.conn).install(
        validate_source_pack(_with_selection(value, "eprel-displays", [narrowed[0]["selector"]])),
        principal_id="operator", enable=True, now_ms=20)
    installed = runtime._manifest(value["pack_id"])[0]
    for item in installed["sources"]:
        runtime.accept_license(installed["pack_id"], item["source_id"], principal_id="operator")
    released = runtime.release_stale_checkpoint(installed["pack_id"], "eprel-displays", principal_id="operator")
    assert released["released"] and released["released_version"] == "1.0.0"
    result = run(runtime, installed, "narrow", adapters=_eprel_only(runtime, installed, narrowed),
                 source_ids=["eprel-displays"])
    assert result["sources"][0]["projection"]["refresh_advanced"] is True
    with pytest.raises(SourcePackError) as caught:
        runtime.release_stale_checkpoint(installed["pack_id"], "eprel-displays", principal_id="operator")
    assert caught.value.code == "checkpoint_current"
    dropped = variant(store, "eprel", "EX-32U8")
    assert dropped["record_state"] == "published"
    coverage = store.coverage("global", dropped["variant_id"])
    assert coverage["in_current_selection"] is False
    assert coverage["status_basis"] == "not-observed-in-latest-complete-refresh"
    kept = store.coverage("global", variant(store, "eprel", "EX-27Q4")["variant_id"])
    assert kept["status_basis"] == "observed"


def _with_selection(value, source_id, selection):
    raw = json.loads(PACK.read_text())
    raw["version"] = "1.0.1"
    for item in raw["sources"]:
        if item["source_id"] == source_id:
            item["product"]["selection"] = selection
    return raw


# ----------------------------------------------------------------- documents


class DocumentServer:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, *, url, params, headers, timeout):
        self.calls.append(url)
        response = self.responses.get(url, {"status": 404, "headers": {}, "content": b""})
        return {"final_url": url, **response}


def test_documents_respect_retention_terms_and_hosts(loaded):
    conn, _, _, _, store = loaded
    q4 = variant(store, "icecat", "EX-27Q4")
    policy = document_policy(conn, "global", q4["variant_id"])
    assert policy["retain"] is False
    server = DocumentServer({})
    result = acquire_documents(store, "global", q4["variant_id"], scopes=SCOPES, principal_id="op",
                               policy=policy, transport=server)
    assert [d["state"] for d in result["documents"]] == ["link_only"] and server.calls == []
    eprel = variant(store, "eprel", "EX-27Q4")
    result = acquire_documents(store, "global", eprel["variant_id"], scopes=SCOPES, principal_id="op",
                               policy={"retain": True, "allowed_hosts": ["objects.icecat.biz"]}, transport=server)
    assert {d["state"] for d in result["documents"]} == {"not_permitted"} and server.calls == []


def test_document_versions_dedup_failures_and_extraction(loaded):
    _, _, _, _, store = loaded
    eprel = variant(store, "eprel", "EX-27Q4")
    links = {d["kind"]: d["url"] for d in store.documents("global", eprel["variant_id"])}
    pdf = {"status": 200, "headers": {"Content-Type": "application/pdf"}, "content": b"%PDF-1.4 fiche"}
    policy = {"retain": True, "allowed_hosts": ["eprel.ec.europa.eu"], "max_bytes": 1000}
    failing_extractor = lambda content, media: (_ for _ in ()).throw(ValueError("bad pdf"))
    first = acquire_documents(store, "global", eprel["variant_id"], scopes=SCOPES, principal_id="op", policy=policy,
                              transport=DocumentServer({links["product-information-sheet"]: pdf,
                                                        links["energy-label"]: pdf}),
                              extractor=failing_extractor)
    states = {d["kind"]: (d["state"], d["deduplicated_asset"], d.get("extraction_state")) for d in first["documents"]}
    assert states["energy-label"][0] == states["product-information-sheet"][0] == "retained"
    assert {states["energy-label"][1], states["product-information-sheet"][1]} == {False, True}  # one asset, two links
    assert {s[2] for s in states.values()} == {"failed"}
    assert variant(store, "eprel", "EX-27Q4")["record_state"] == "published"  # records untouched
    changed = {**pdf, "content": b"%PDF-1.4 fiche v2"}
    later = acquire_documents(
        store, "global", eprel["variant_id"], scopes=SCOPES, principal_id="op", policy=policy,
        transport=DocumentServer({
            links["product-information-sheet"]: changed,
            links["energy-label"]: {"status": 200, "headers": {"Content-Type": "text/html"}, "content": b"<html>"},
        }))
    assert {d["kind"]: d["state"] for d in later["documents"]} == {
        "product-information-sheet": "retained", "energy-label": "unsupported"}
    sheet = next(d for d in store.documents("global", eprel["variant_id"]) if d["kind"] == "product-information-sheet")
    assert sheet["distinct_contents"] == 2 and sheet["availability"] == "retained"
    assert not list(jsonschema.Draft7Validator(schema("noesis-product-document-v1")).iter_errors(sheet))
    big = acquire_documents(store, "global", eprel["variant_id"], scopes=SCOPES, principal_id="op", policy=policy,
                            transport=DocumentServer({links["product-information-sheet"]: {**pdf, "content": b"x" * 2000},
                                                      links["energy-label"]: {"status": 404, "headers": {}}}))
    assert {d["kind"]: (d["state"], d["reason"]) for d in big["documents"]} == {
        "product-information-sheet": ("too_large", "response_too_large"), "energy-label": ("inaccessible", "not_found")}
    redirected = acquire_documents(
        store, "global", eprel["variant_id"], scopes=SCOPES, principal_id="op", policy=policy,
        transport=lambda **kw: {**pdf, "final_url": "https://mirror.example.org/x.pdf"})
    assert {d["state"] for d in redirected["documents"]} == {"not_permitted"}


# ---------------------------------------------------------------- comparison


def test_three_model_comparison_links_every_value_to_evidence(loaded):
    _, _, _, _, store = loaded
    accept_proposed(store)
    models = [variant(store, "icecat", d)["model_id"] for d in ("EX-27Q4", "EX-32U8", "EX-24F1")]
    result = store.compare("global", models, scopes=SCOPES)
    assert not list(jsonschema.Draft7Validator(schema("noesis-product-comparison-v1")).iter_errors(result))
    rows = {(r["attribute"], r["mode"]): r for r in result["rows"]}
    assert rows[("diagonal", None)]["comparable"] is True
    assert [c["value"] for c in rows[("diagonal", None)]["cells"]] == ["68.6", "80.0", "60.5"]
    assert rows[("resolution", None)]["comparable"] is True
    energy = rows[("energy_consumption_1000h", "sdr")]
    assert energy["comparable"] is False and energy["cells"][1]["state"] == "conflict"
    assert sorted(v["native_value"] for v in energy["cells"][1]["values"]) == ["34", "36"]
    assert rows[("depth", "with-stand")]["cells"][2]["state"] == "missing"
    assert rows[("energy_class", "sdr")]["not_comparable_reason"] == "label scheme unknown or different"
    assert ("on_mode_power", "typical") in rows and ("on_mode_power", "sdr") in rows  # modes never merged
    for row in result["rows"]:
        for cell in row["cells"]:
            for item in cell["values"]:
                assert item["evidence"]["revision_id"].startswith("product-revision:")
                assert item["evidence"]["locator"]
    assert all(len(column["members"]) == 2 for column in result["columns"])
    assert "not a buying recommendation" in result["notice"]
    assert any(d["kind"] == "datasheet" for d in result["columns"][0]["documents"])


def test_comparison_requires_resolved_models_and_honours_reviews_and_namespaces(loaded):
    _, _, _, _, store = loaded
    candidates = store.propose_matches("global", scopes=SCOPES, principal_id="m")["candidates"]
    q4 = variant(store, "icecat", "EX-27Q4")
    with pytest.raises(ProductError) as caught:
        store.compare("global", [q4["variant_id"], variant(store, "icecat", "EX-32U8")["model_id"]], scopes=SCOPES)
    assert caught.value.code == "unresolved_identity"
    match = next(c for c in candidates if c["left_model_id"] == q4["model_id"])
    store.review_match("global", match["match_id"], "rejected", "different panel revision", scopes=SCOPES,
                       principal_id="r")
    result = store.compare("global", [q4["model_id"], variant(store, "icecat", "EX-32U8")["model_id"]], scopes=SCOPES)
    assert result["columns"][0]["members"] == [q4["model_id"]]
    assert result["columns"][0]["rejected_matches"] == [match["match_id"]]
    assert result["columns"][1]["pending_matches"]
    other = {READ_SCOPE, "namespace:other:read"}
    with pytest.raises(ProductError) as caught:
        store.compare("global", [q4["model_id"], q4["model_id"]], scopes=other)
    assert caught.value.code == "unauthorized"
    assert store.lookup("other", scopes=other)["count"] == 0
    with pytest.raises(ProductError) as caught:
        store.inspect("other", q4["model_id"], scopes=other)
    assert caught.value.code == "not_found"


# ---------------------------------------------------------------- packaging


def test_domain_pack_installs_and_readiness_is_per_provider(loaded):
    conn, value, _, _, store = loaded
    data = json.loads((ROOT / "packs/products/pack.json").read_text())
    assert validate_manifest(data) == []
    try:
        first = install_manifest(PackManifest.from_dict(data))
        assert install_manifest(PackManifest.from_dict(data)) == first
        assert installed_packs()["products"] == "1.0.0"
    finally:
        assert uninstall("products")
    state = readiness(conn, secrets=lambda _name: None)
    assert state["providers"]["icecat"]["live"] == "ready"
    assert state["providers"]["eprel"]["live"] == "blocked"
    assert state["providers"]["eprel"]["blockers"][0]["code"] == "credential_missing"
    assert state["cross_source_validation"] == "outstanding"
    SourcePackStore(conn).set_enabled(value["pack_id"], False, principal_id="operator")
    disabled = readiness(conn, secrets=lambda _name: "k")
    assert {p["fixture"] for p in disabled["providers"].values()} == {"blocked"}
    assert store.lookup("global", scopes=SCOPES)["count"] == 10  # disabling keeps shared evidence
    SourcePackStore(conn).set_enabled(value["pack_id"], True, principal_id="operator")
    assert readiness(conn, secrets=lambda _name: "k")["providers"]["eprel"]["live"] == "ready"
