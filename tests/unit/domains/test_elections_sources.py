"""Election result acquisition: formats, vintages, rules, identifiers and the runtime transport (#1918, #1930, #1939)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.ingestion import source_packs as sp
from src.ingestion.election_sources import (
    CONNECTOR,
    PROVIDER_CONTRACTS,
    ElectionFormatError,
    ElectionResultsAdapter,
    normalize_unit_id,
    parse_release,
)
from src.ingestion.source_packs import SourcePackConformance, SourcePackError
from tests.unit import elections_harness as h


def _contest(page, native, ballot):
    return next(
        r["election_contest"]
        for r in page.records
        if r["election_contest"]["unit"]["native_id"] == native
        and r["election_contest"]["ballot"] == ballot
    )


def test_access_decisions_record_every_source_and_keep_aggregators_out():
    assert set(PROVIDER_CONTRACTS) == {
        "bundeswahlleiterin",
        "berlin-landeswahlleitung",
        "daten-berlin",
        "uk-electoral-commission",
        "mit-election-lab",
        "parlgov",
        "wahlrecht-de",
        "poll-publisher-release",
    }
    decisions = {k: v["access_decision"] for k, v in PROVIDER_CONTRACTS.items()}
    assert decisions["wahlrecht-de"] == decisions["parlgov"] == "not-implemented"
    assert (
        "verified-live" not in decisions.values()
    )  # nothing is live until a dated run
    for contract in PROVIDER_CONTRACTS.values():
        assert contract["reason"] and contract["delivers"] and "terms" in contract
    audit = (h.ROOT / "docs/roadmaps/political-elections-source-audit.md").read_text()
    assert "_verify_" in audit and "Unavailable-access fallback" in audit


def test_the_source_pack_carries_four_pinned_result_sources_that_replay_offline():
    pack = json.loads(h.PACK.read_text())
    assert pack["version"] == "1.4.0"  # 1.3.0 legislation (#2208), 1.4.0 campaign finance (#2209); ours unchanged
    ours = [s for s in pack["sources"] if s.get("connector") == CONNECTOR]
    assert {s["source_id"] for s in ours} == set(h.SOURCES.values())
    assert all(
        s["auth"] == {"kind": "none"} and s["budgets"]["max_pages"] == 1 for s in ours
    )
    report = SourcePackConformance(h.ROOT).offline(pack)
    assert all(item["valid"] for item in report["sources"]), report
    assert (
        sp.native_connector_module(CONNECTOR).__name__
        == "src.ingestion.election_sources"
    )


def test_federal_preliminary_file_keeps_first_and_second_votes_as_published():
    page, _ = h.page("de-btw", h.DE_PRELIMINARY)
    header = page.records[0]["election_release"]
    assert (
        header["vintage_kind"] == "preliminary"
        and header["published_on"] == "2099-03-02"
    )
    assert (
        header["evidence_origin"] == "fixture"
        and header["elections"][0]["election_id"] == h.DE_ELECTION
    )
    first = _contest(page, "001", "first-vote")
    second = _contest(page, "001", "second-vote")
    assert first["figures"]["totals"] == {
        "electorate": 140000,
        "voters": 104000,
        "invalid": 1000,
        "valid": 103000,
    }
    assert (
        second["figures"]["totals"]["invalid"] == 1500
    )  # invalid counts per vote, as published
    votes = {
        e["key"]: (e["votes"], e["share_published"], e["kind"])
        for e in first["figures"]["entries"]
    }
    assert votes["party:beispielpartei"] == (41000, "39.8", "party")
    assert votes["candidate:erika-beispiel"][2] == "candidate"  # an Einzelbewerber
    assert first["unit"]["parent"] == {"scheme": "de-land", "native_id": "01"}
    kinds = {(r["election_contest"]["unit"]["scheme"]) for r in page.records}
    assert kinds == {"de-bt-wahlkreis", "de-land", "de-bund"}


def test_final_file_states_certified_and_a_contradicting_declaration_is_schema_drift():
    page, item = h.page("de-btw", h.DE_FINAL)
    assert page.records[0]["election_release"]["vintage_kind"] == "certified"
    item["elections"]["vintage"] = "preliminary"
    with pytest.raises(SourcePackError) as exc:
        h.page("de-btw", h.DE_FINAL, item=item)
    assert exc.value.code == "schema_drift"
    body = (
        (h.FIXTURES / h.DE_PRELIMINARY)
        .read_text()
        .replace("# Bundestagswahl 2099 - Vorläufiges Ergebnis\n", "")
    )
    with pytest.raises(SourcePackError, match="vintage"):
        h.page("de-btw", "x", body=body)


def test_berlin_rows_state_their_ergebnisstand_and_party_columns():
    page, _ = h.page("de-be", h.BERLIN)
    header = page.records[0]["election_release"]
    assert (
        header["vintage_kind"] == "preliminary"
        and header["published_on"] == "2099-03-02"
    )
    first = _contest(page, "0101", "first-vote")
    assert (
        first["unit"]["scheme"] == "de-be-wahlkreis"
        and first["figures"]["totals"]["invalid"] == 300
    )
    second = _contest(page, "01", "second-vote")
    assert second["unit"]["scheme"] == "de-be-bezirk"
    mixed = (
        (h.FIXTURES / h.BERLIN)
        .read_text()
        .replace(
            "vorläufig;AGH;01.03.2099;02.03.2099;Zweitstimme",
            "endgültig;AGH;01.03.2099;02.03.2099;Zweitstimme",
        )
    )
    with pytest.raises(SourcePackError, match="both a preliminary and a final"):
        h.page("de-be", "x", body=mixed)


def test_uk_results_keep_ons_codes_rules_and_take_the_date_from_last_modified():
    page, _ = h.page("gb", h.UK)
    header = page.records[0]["election_release"]
    assert (
        header["vintage_kind"] == "certified" and header["published_on"] == "2099-05-15"
    )
    assert header["rules"]["system"] == "first-past-the-post" and header["rules"][
        "source_url"
    ].startswith("https://")
    assert header["geometry"]["gb-ons-pcon"]["vintage"] == "pcon2024"
    contest = _contest(page, "E14099901", "single")
    assert contest["election_id"] == h.UK_ELECTION
    winner = next(e for e in contest["figures"]["entries"] if e["elected_published"])
    assert winner["name"] == "Alex Sample" and winner["party"] == "Example Party"
    with pytest.raises(SourcePackError, match="publication date"):
        h.page("gb", h.UK, headers={"Content-Type": "text/csv"})


def test_mit_county_returns_keep_fips_modes_and_never_sum_them():
    page, _ = h.page("us", h.US)
    header = page.records[0]["election_release"]
    assert header["published_on"] == "2099-01-15" and header["dataset_versions"] == [
        "20990115"
    ]
    assert header["elections"][0]["election_date"] == "2099-11-03"
    split = _contest(page, "99003", "office")
    assert split["figures"]["totals"] == {
        "total_votes_by_mode": {"ABSENTEE": 500, "ELECTION DAY": 2600}
    }
    modes = sorted(
        (e["key"], e["mode"], e["votes"]) for e in split["figures"]["entries"]
    )
    assert ("candidate:alex-example", "ABSENTEE", 300) in modes and len(modes) == 4
    unknown = next(
        r["election_contest"]
        for r in page.records
        if r["election_contest"]["unit"]["scheme"] == "us-medsl-county-name"
    )
    assert unknown["unit"]["native_id"] == "EX:STATEWIDE WRITEIN"


def test_identifiers_are_normalised_the_same_way_everywhere():
    assert (
        normalize_unit_id("de-bt-wahlkreis", "75")
        == normalize_unit_id("de-bt-wahlkreis", "075")
        == "075"
    )
    assert normalize_unit_id("us-fips-county", "1001.0") == "01001"
    assert normalize_unit_id("us-fips-county", "NA") is None
    assert normalize_unit_id("gb-ons-pcon", " e14099901") == "E14099901"
    assert normalize_unit_id("gb-ons-pcon", "99901") is None


def test_default_transport_refuses_redirects_to_another_host_and_budgets_hold():
    with pytest.raises(SourcePackError) as exc:
        h.page("us", h.US, final_url="https://storage.example.org/file.csv")
    assert exc.value.code == "network_policy"
    with pytest.raises(SourcePackError) as exc:
        h.page("de-btw", h.DE_PRELIMINARY, limit=3)
    assert exc.value.code == "budget_exhausted"  # never a truncated release
    item = h.source("de-btw")
    adapter = ElectionResultsAdapter(item)
    from src.ingestion.source_pack_runtime import HTTPSPageAdapter

    assert adapter.transport.func is HTTPSPageAdapter._request
    assert adapter.transport.keywords == {"max_bytes": item["budgets"]["max_bytes"]}
    with pytest.raises(SourcePackError) as exc:
        adapter.fetch_page(
            {"operation": "release", "parameters": {"q": "x"}}, cursor=None
        )
    assert exc.value.code == "parameter_forbidden"


def test_manifest_declarations_are_checked():
    item = h.source("gb")
    item["elections"].pop("vintage")
    with pytest.raises(SourcePackError, match="declares one"):
        ElectionResultsAdapter(item)
    item = h.source("de-btw")
    item["elections"]["rules"] = {"system": "x"}
    with pytest.raises(SourcePackError, match="cite"):
        ElectionResultsAdapter(item)


def test_malformed_files_are_schema_drift():
    with pytest.raises(ElectionFormatError):
        parse_release(
            "de-btw-kerg-csv", b"# Vorl\xc3\xa4ufiges Ergebnis\na;b\n1;2\n", declared={}
        )
    bad = (h.FIXTURES / h.US).read_text().replace("5100", "5,1x")
    with pytest.raises(ElectionFormatError):
        parse_release(
            "us-medsl-county-csv", bad.encode(), declared={"vintage": "certified"}
        )
    assert Path(h.FIXTURES / h.UK).read_text().count("Example Party") == 3


def test_the_source_pack_runtime_runs_a_result_source_into_the_record_owner():
    from src.ingestion.source_pack_runtime import SourcePackRuntime
    from src.ingestion.source_packs import SourcePackStore
    from src.kb.elections import ElectionStore

    conn = h.connection()
    manifest = h.manifest()
    SourcePackStore(conn).install(
        manifest, principal_id="operator", enable=True, now_ms=1
    )
    clock = iter(range(10, 10_000))
    runtime = SourcePackRuntime(conn, now=lambda: next(clock), sleep=lambda _d: None)
    source_id = h.SOURCES["de-btw"]
    runtime.accept_license(manifest["pack_id"], source_id, principal_id="operator")
    adapters = runtime.fixture_adapters(manifest["pack_id"], h.ROOT)
    request = {
        "pack_id": manifest["pack_id"],
        "run_key": "btw",
        "operation": "release",
        "source_ids": [source_id],
        "max_results": 1000,
        "max_bytes": 20_000_000,
        "timeout_ms": 60_000,
    }
    result = runtime.run(
        request,
        principal_id="operator",
        adapters={source_id: adapters[source_id]},
        dns_resolver=lambda _host: ["8.8.8.8"],
    )
    assert result["status"] == "complete", result
    store = ElectionStore(conn, initialize=False)
    (release,) = conn.execute(
        "SELECT release_id, source_id, evidence_origin FROM election_releases"
    ).fetchall()
    assert release[1] == source_id and release[2] == "fixture"
    assert len(store.contests("global", election_id=h.DE_ELECTION)) == 8
    conn.close()
