"""#2882: WID, ECB DWA and Fed DFA source contracts, bounded acquisition and append-only wealth records."""

from __future__ import annotations

import json

import pytest

from src.ingestion.source_packs import SourcePackConformance, SourcePackError
from src.ingestion.wealth_distribution_sources import (
    BOUNDED_COVERAGE,
    CAPS,
    EXCLUSIONS,
    FIXTURE_SECRET,
    LIVE_VERIFICATION,
    NOT_IMPLEMENTED,
    PDF_PROVIDERS,
    PROVIDER_CONTRACTS,
    PROVIDERS,
    WealthAdapter,
    fixture_transport,
    zip_members,
)
from src.kb.wealth_distribution_records import (
    WealthError,
    check_item,
    readiness,
    register_schemas,
)
from src.kb.wealth_distribution_store import WealthStore
from tests.unit import wealth_distribution_harness as h

AUDIT = h.ROOT / "docs/development/wealth-distribution-evidence/source-audit.md"


def first_item(name: str, native_key: str | None = None) -> dict:
    records = [r for page in h.fetch(name) for r in page]
    items = [r["wealth_item"] for r in records]
    return json.loads(json.dumps(next(i for i in items if native_key in (None, i["native_key"]))))


def test_contracts_coverage_caps_and_live_verification_match_the_audit():
    audit = AUDIT.read_text()
    assert set(PROVIDERS) == set(h.SOURCES.values()) == set(CAPS) == set(BOUNDED_COVERAGE)
    assert set(PROVIDER_CONTRACTS) == set(LIVE_VERIFICATION) == set(PROVIDERS) | set(NOT_IMPLEMENTED)
    for provider in PROVIDERS:
        contract = PROVIDER_CONTRACTS[provider]
        for key in ("publisher", "access", "authentication", "licence", "redistribution", "rate_limits",
                    "revision_model", "definitions", "method", "personal_data", "unavailable_fallback"):
            assert contract[key], (provider, key)
        assert contract["status"] == LIVE_VERIFICATION[provider]["status"] == "unverified-live"
        assert "verify" in contract["licence"]
        assert f"`{provider}`" in audit
        assert contract["method"].startswith("survey" if provider in PDF_PROVIDERS else "model-based")
    for provider in NOT_IMPLEMENTED:
        assert PROVIDER_CONTRACTS[provider]["status"] == LIVE_VERIFICATION[provider]["status"] == "not-implemented"
        assert "never scraped" in PROVIDER_CONTRACTS[provider]["unavailable_fallback"]
        assert f"`{provider}`" in audit
    assert "1 document, 40 series" in audit and "1 document, 20 series" in audit
    assert BOUNDED_COVERAGE["wid-wealth-distribution"]["places"] == {"Germany": "DE", "France": "FR",
                                                                     "United States": "US"}
    assert {"blending WID, DWA and DFA figures into one series",
            "correcting or re-estimating a publisher's model-based estimates"} <= set(EXCLUSIONS)


def test_pinned_fixtures_replay_through_the_real_adapter_and_match_the_pack():
    report = SourcePackConformance(h.ROOT).offline(h.manifest())
    assert report["valid"] is True
    assert {s["source_id"]: s["records"] for s in report["sources"]} == {
        "wid-wealth-distribution": 15, "ecb-distributional-wealth-accounts": 6,
        "fed-distributional-financial-accounts": 5, "ecb-hfcs-statistical-tables": 12,
        "bundesbank-phf-aggregates": 3}
    pack = h.manifest()
    assert {s["wealth"]["live_verification"] for s in pack["sources"]} == {"unverified-live"}
    wid = next(s for s in pack["sources"] if s["source_id"] == "wid-wealth-distribution")
    assert wid["auth"] == {"kind": "required-secret", "secret_ref": "NOESIS_WID_API_KEY"}


def test_wid_keeps_the_variable_code_method_and_extrapolated_years_and_sends_the_key_in_a_header():
    seen = []
    transport = fixture_transport(h.pages("wid"))

    def recording(**kwargs):
        seen.append(kwargs["headers"])
        return transport(**kwargs)

    item = h.fetch("wid", transport=recording)[0]
    assert seen[0]["x-api-key"] == FIXTURE_SECRET
    top1 = next(r["wealth_item"] for r in item if r["wealth_item"]["native_key"] == "DE.shweal_p99p100_992_j")
    assert top1["indicator"]["concept"] == "wealth_share" and top1["group"]["code"] == "p99p100"
    assert top1["population"]["code"] == "992_j" and top1["method"]["code"] == "model-based-dina"
    assert top1["definition"]["method_statement"].startswith("DINA method, fixture version 1")
    statuses = {o["period"]: o["publication_status"] for o in top1["observations"]}
    assert statuses["2096"] == statuses["2097"] == "extrapolated" and statuses["2095"] == "not-stated"
    assert all(o["value_text"] for o in top1["observations"])
    assert FIXTURE_SECRET not in json.dumps(item)  # the key is never part of a record
    adapter = WealthAdapter(h.source("wid"), transport=transport)
    with pytest.raises(SourcePackError) as missing:
        adapter.fetch_page({"operation": "release", "parameters": {}}, cursor=None)
    assert missing.value.code == "credential_missing"


def test_dwa_keeps_status_and_confidential_cells_and_dfa_reads_one_archive_member():
    median_fr = first_item("dwa", "Q.FR.S14.N.LE.NWA._Z.EUR_MD.S.N")
    cell = median_fr["observations"][0]
    assert cell["status"] == "confidential" and cell["value"] is None and cell["flags"]["OBS_CONF"] == "C"
    share = first_item("dwa", "Q.DE.S14.N.LE.NWA.D10.PT.S.N")
    assert {o["period"]: o["publication_status"] for o in share["observations"]}["2097-Q4"] == "estimated"
    assert [n["value"] for n in share["source_notes"]] == ["wid-wealth-distribution"]
    top = first_item("dfa", "TopPt1.Net worth")
    assert next(o["period"] for o in top["observations"]) == "2096-Q1"  # 2095:Q4 is before the declared start
    assert top["observations"][0]["attributes"] == {"period_as_published": "2096:Q1"}
    page = h.pages("dfa")[0]
    other = dict(page, body_zip={"something-else.csv": "Date,Category,Net worth\n"})
    with pytest.raises(SourcePackError) as drift:
        h.fetch("dfa", transport=fixture_transport([other]))
    assert drift.value.code == "schema_drift"


def test_declarations_refuse_other_hosts_unbounded_documents_and_undeclared_controls():
    item = h.source("dwa")
    item["endpoint"] = "https://example.org/service/data"
    with pytest.raises(SourcePackError) as host:
        WealthAdapter(item)
    assert host.value.code == "unsafe_endpoint"
    item = h.source("dwa")
    item["wealth"]["documents"][0]["key"] = "Q.*.S14"
    with pytest.raises(SourcePackError) as unbounded:
        WealthAdapter(item)
    assert unbounded.value.code == "invalid_source"
    adapter = WealthAdapter(h.source("dfa"), transport=fixture_transport(h.pages("dfa")))
    with pytest.raises(SourcePackError) as control:
        adapter.fetch_page({"operation": "release", "parameters": {"area": "DE"}}, cursor=None)
    assert control.value.code == "parameter_forbidden"
    assert zip_members({"a.csv": "x"}) == zip_members({"a.csv": "x"})  # authored archives are deterministic


@pytest.mark.parametrize("mutation, code", [
    (lambda i: i.update(household_id="H1"), "personal_data"),
    (lambda i: i["observations"][0].update(survey_weight="1.0"), "personal_data"),
    (lambda i: i.update(rich_list_name="someone"), "personal_data"),
    (lambda i: i.update(pareto_fit="1.5"), "derived_value"),
    (lambda i: i["observations"][0].update(origin="interpolated"), "derived_value"),
    (lambda i: i["observations"][0].update(value="0.5"), "recomputed_value"),
    (lambda i: i.update(provider="not-a-wealth-provider"), "invalid_record"),
])
def test_person_household_and_derived_fields_are_refused_at_write(mutation, code):
    item = first_item("wid")
    mutation(item)
    with pytest.raises(WealthError) as refused:
        check_item(item)
    assert refused.value.code == code


def test_releases_append_vintages_never_overwrite_and_record_removals_and_failures():
    conn = h.connection()
    first = h.apply(conn, "dwa")
    assert first[0]["status"] == "applied" and first[0]["vintages"] == 6
    assert h.apply(conn, "dwa")[0]["status"] == "unchanged"  # the same release adds nothing
    revised = h.apply(conn, "dwa", revised=True)[0]
    assert revised["vintages"] == 5 and revised["removed_series"] == 1
    store = WealthStore(conn)
    removed = h.series(conn, "ecb-distributional-wealth-accounts", "Q.FR.S14.N.LE.NWA._Z.EUR_MD.S.N")
    assert removed["current_status"] == "removed" and removed["vintage_count"] == 2
    share = h.series(conn, "ecb-distributional-wealth-accounts", "Q.DE.S14.N.LE.NWA.D10.PT.S.N")
    vintages = store.vintage_rows(h.NS, share["series_id"])
    assert [v["sequence"] for v in vintages] == [1, 2]
    assert vintages[1]["changes"]["new_periods"] == ["2098-Q1"]
    assert {r["period"] for r in vintages[1]["changes"]["revised"]} == {"2097-Q3", "2097-Q4"}
    assert store.observations(h.NS, vintages[0]["vintage_id"])[-1]["publication_status"] == "estimated"
    # Numeric values also live in the Economics series storage (domain society).
    assert conn.execute("SELECT count(*) FROM economic_series_map WHERE domain='society'").fetchone()[0] == 6
    store.record_failure(h.NS, "ecb-distributional-wealth-accounts", code="schema_drift", run_id="r",
                         source_id="ecb-distributional-wealth-accounts", scopes=h.SCOPES)
    assert store.provider_state(h.NS, "ecb-distributional-wealth-accounts")["stale"] is True
    assert h.series(conn, "ecb-distributional-wealth-accounts",
                    "Q.DE.S14.N.LE.NWA.D10.PT.S.N")["current_vintage_id"] == vintages[1]["vintage_id"]
    status = readiness(conn)
    assert status["providers"]["ecb-distributional-wealth-accounts"]["status"] == "stale"
    assert status["providers"]["ecb-hfcs-statistical-tables"]["status"] == "unavailable"  # selected, nothing acquired
    with pytest.raises(WealthError):
        store.apply_release(h.NS, {"provider": "wid-wealth-distribution", "format": "wid-api-json",
                                   "item_count": 0}, [], source_id=None, run_id="x", principal_id="p",
                            scopes=h.READ_ONLY)


def test_the_record_schema_registers():
    conn = h.connection()
    modules = register_schemas(conn, principal_id="svc", scopes={"knowledge:schema:register", "operator"})
    assert modules and modules[0]["name"] == "noesis-wealth-distribution-record-v2"
