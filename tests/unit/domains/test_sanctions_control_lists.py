"""Dual-use control-list editions as legal works and Comext trade context with vintages (#1936, #1943)."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from src.domains.economic.queries import economic_research
from src.ingestion.legal_sources import (
    CellarLegalAdapter,
    fixture_transport,
    parse_control_list_annex,
)
from src.ingestion.source_packs import SourcePackError
from src.kb.legal import READ_SCOPE, LegalStore, consolidated_of
from src.kb.sanctions import SanctionsError, SanctionsStore
from src.kb.sanctions_queries import SanctionsQueries
from src.kb.sanctions_trade import COMEXT_SELECTION, SanctionsTrade, guarded_http_get
from tests.unit import sanctions_harness as h

LEGAL = {READ_SCOPE, "namespace:global:read"}


@pytest.fixture()
def editions():
    conn = h.connection()
    h.load_legal(conn, "cellar-dual-use-2021-821")
    yield conn, LegalStore(conn), SanctionsQueries(conn)
    conn.close()


def consolidated_work(store):
    result = store.lookup("global", scopes=LEGAL, identifier="02021R0821-20241115")
    assert result["status"] == "found"
    return result["works"][0]


def test_the_annex_parser_locates_entries_by_control_code():
    raw = (h.FIXTURES / "cellar_02021R0821-20251115_eng.xhtml").read_bytes()
    sections = parse_control_list_annex(raw)
    entries = {
        s["locator"]["official_norm_id"]: s
        for s in sections
        if s["locator"]["kind"] == "control-entry"
    }
    assert sorted(entries) == ["1A001", "1C350", "5A002", "5A004"]
    assert entries["1C350"]["text"].splitlines() == [
        "1C350 Chemicals which may be used as precursors for toxic chemical agents, as follows:",
        "1. Fixture chemical A (CAS 000-00-1);",
        "2. Fixture chemical B (CAS 000-00-2);",
    ]
    assert (
        entries["1C350"]["locator"]["category"] == "1"
        and entries["5A004"]["locator"]["product_group"] == "A"
    )
    headings = [
        s["text"] for s in sections if s["locator"]["kind"] == "xhtml-paragraph"
    ]
    assert (
        "CATEGORY 5 — TELECOMMUNICATIONS AND INFORMATION SECURITY" in headings
    )  # never glued to an entry


def test_editions_are_versions_of_one_consolidated_work_and_the_base_act_stays_findable(
    editions,
):
    _, store, _ = editions
    assert consolidated_of("02021R0821-20251115") == ("32021R0821", "2025-11-15")
    assert (
        consolidated_of("32021R0821") is None
        and consolidated_of("02021R0821-20251399") is None
    )
    work = consolidated_work(store)
    assert work["work_kind"] == "consolidated"
    assert work["identifiers"]["consolidated_celex"] == [
        "02021R0821-20241115",
        "02021R0821-20251115",
    ]
    base = store.lookup("global", scopes=LEGAL, identifier="32021R0821")
    assert base["status"] == "found" and base["works"][0]["work_kind"] == "normative"
    versions = store.versions("global", work["work_id"])
    assert (
        len(versions) == 3
    )  # two editions; the first also as a metadata-only PDF manifestation
    assert sorted(
        {
            f["value"]
            for v in versions
            for f in v["facts"]
            if f["fact"] == "consolidation"
        }
    ) == ["2024-11-15", "2025-11-15"]


def test_as_of_selection_picks_the_edition_in_force_and_supersedes_older_ones(editions):
    _, store, _ = editions
    work = consolidated_work(store)["work_id"]
    before = store.select_as_of("global", work, "2024-01-01", scopes=LEGAL)
    assert before["status"] == "none_applies"
    first = store.select_as_of("global", work, "2025-01-01", scopes=LEGAL)
    second = store.select_as_of("global", work, "2026-01-01", scopes=LEGAL)
    assert (
        first["status"] == second["status"] == "selected"
        and first["selected_version_id"] != second["selected_version_id"]
    )
    superseded = [c for c in second["candidates"] if c["state"] == "superseded"]
    assert superseded and all(
        c["evidence"]["superseded_from"] == "2025-11-15" for c in superseded
    )
    assert "acquired consolidated versions only" in second["consolidation_notice"]


def test_equal_consolidation_dates_stay_ambiguous(editions):
    conn, store, _ = editions
    work = consolidated_work(store)["work_id"]
    # A second, different text for the same consolidation date: conflicting evidence, never a pick.
    conn.execute(
        "UPDATE legal_facts SET value='2025-11-15' WHERE work_id=? AND fact_kind='consolidation'",
        [work],
    )
    result = store.select_as_of("global", work, "2026-01-01", scopes=LEGAL)
    assert result["status"] == "ambiguous" and result["selected_version_id"] is None


def test_control_entry_as_of_reads_the_edition_and_lists_every_edition(editions):
    _, _, queries = editions
    old = queries.control_entry_as_of("global", "5A004", "2025-06-01", scopes=h.SCOPES)
    assert old["status"] == "code_not_in_edition"
    new = queries.control_entry_as_of("global", "5A004", "2026-01-01", scopes=h.SCOPES)
    assert new["status"] == "entry_in_edition"
    entry = new["edition"]["entry"]
    assert (
        entry["edition"] == "02021R0821-20251115"
        and entry["locator"]["official_norm_id"] == "5A004"
    )
    assert entry["record_type"] == "control_list_entry"
    assert (
        queries.control_entry_as_of("global", "1C350", "2024-01-01", scopes=h.SCOPES)[
            "status"
        ]
        == "none_applies"
    )
    assert h.forbidden_keys(new) == []


def test_comparing_editions_shows_the_code_level_passage_change_only(editions):
    _, store, queries = editions
    work = consolidated_work(store)["work_id"]
    first = store.select_as_of("global", work, "2025-01-01", scopes=LEGAL)
    second = store.select_as_of("global", work, "2026-01-01", scopes=LEGAL)
    texts = queries.control_entry_as_of(
        "global", "1C350", "2025-01-01", scopes=h.SCOPES
    )["edition"]["version_id"]
    later = queries.control_entry_as_of(
        "global", "1C350", "2026-01-01", scopes=h.SCOPES
    )["edition"]["version_id"]
    assert later == second["selected_version_id"] and texts in {
        first["selected_version_id"],
        *first["equivalent_manifestations"],
    }
    compared = queries.compare_control_entry(
        "global", "1C350", texts, later, scopes=h.SCOPES
    )
    assert compared["status"] == "changed" and len(compared["changes"]) == 1
    assert compared["changes"][0]["after"].endswith(
        "2. Fixture chemical B (CAS 000-00-2);"
    )
    assert "not a legal-effect assessment" in compared["notice"]
    unchanged = queries.compare_control_entry(
        "global", "1A001", texts, later, scopes=h.SCOPES
    )
    assert unchanged["status"] == "unchanged_or_absent_in_both"


def test_control_entries_persist_idempotently(editions):
    conn, _, _ = editions
    store = SanctionsStore(conn)
    first = store.sync_control_entries("global")
    assert first["editions"] == 2 and first["entries_added"] == first["entries"] == 7
    assert store.sync_control_entries("global")["entries_added"] == 0


def test_cellar_text_fetches_stay_on_the_declared_host_and_are_bounded():
    item = h.source("cellar-dual-use-2021-821")
    fixture = json.loads((h.ROOT / item["fixture"]["path"]).read_text())
    pages = fixture["native_pages"]
    adapter = CellarLegalAdapter(item, transport=fixture_transport(pages))
    page = adapter.fetch_page(
        {"operation": "records", "parameters": {}, "limit": 100}, cursor=None
    )
    assert [t["outcome"] for t in page.receipt["texts"]] == ["captured", "captured"]
    missing = [p for p in pages if "sparql" in p["request"]] + [
        {**pages[1], "status": 404, "body": ""},
        pages[2],
    ]
    page = CellarLegalAdapter(item, transport=fixture_transport(missing)).fetch_page(
        {"operation": "records", "parameters": {}, "limit": 100}, cursor=None
    )
    assert sorted(t["outcome"] for t in page.receipt["texts"]) == [
        "captured",
        "not_found",
    ]
    broken = h.source("cellar-dual-use-2021-821")
    broken["legal"]["selection"]["text"]["max_items"] = 50
    with pytest.raises(SourcePackError) as caught:
        CellarLegalAdapter(broken, transport=fixture_transport(pages))
    assert caught.value.code == "invalid_mapping"


# ------------------------------------------------------------------ Comext (S06)


def comext_get(*bodies):
    queue = [(h.FIXTURES / b).read_text() for b in bodies]
    seen = []

    def get(url):
        seen.append(url)
        return queue.pop(0)

    return get, seen


class Backing:
    backing_type = "corpus-view"

    def __init__(self, conn):
        self.conn = conn
        self.definition = SimpleNamespace(name="economics", tags=["economics"])

    def documents(self, limit=50):
        return []

    def claims(self, limit=50):
        return []

    def entities(self):
        return []

    def coverage(self):
        return {
            "domain": "economics",
            "backing": self.backing_type,
            "ready": True,
            "documents": 0,
        }


SPEC = {
    "dataset": "DS-045409",
    "geography": "DE",
    "partner": "CN",
    "product": "29309098",
    "flow": "2",
    "indicators": "VALUE_IN_EUROS",
    "freq": "M",
}


def test_comext_vintages_are_acquired_through_the_dataset_connector_and_compared():
    conn = h.connection()
    trade = SanctionsTrade(conn)
    get, seen = comext_get(
        "comext_ds045409_v1.json", "comext_ds045409_v1.json", "comext_ds045409_v2.json"
    )
    first = trade.acquire([SPEC], http_get=get, fetched_at_ms=1_781_000_000_000)
    assert first[0]["status"] == "new_vintage"
    assert (
        seen[0].startswith(COMEXT_SELECTION["endpoint"] + "/DS-045409?")
        and "reporter=DE" in seen[0]
    )
    again = trade.acquire([SPEC], http_get=get, fetched_at_ms=1_782_000_000_000)
    assert (
        again[0]["status"] == "unchanged"
    )  # same provider update: no new vintage, first retrieval kept
    retrieved = conn.execute("SELECT retrieved_at_ms FROM economic_vintages").fetchall()
    assert retrieved == [(1_781_000_000_000,)]
    second = trade.acquire([SPEC], http_get=get, fetched_at_ms=1_783_000_000_000)
    assert second[0]["status"] == "new_vintage"
    series_id = second[0]["series_id"]
    assert series_id == (
        "estat-comext:DS-045409:DE:flow=2:freq=M:indicators=VALUE_IN_EUROS:partner=CN:"
        "product=29309098"
    )
    compared = economic_research(
        Backing(conn), query_type="vintage_comparison", series_ids=[series_id]
    )
    revised = {r["period"]: r for r in compared["results"][0]["observations"]}
    assert (
        revised["2026-02"]["revision"] == pytest.approx(2500.0)
        and revised["2026-01"]["revision"] == 0
    )
    assert (
        compared["results"][0]["initial_vintage"]["vintage_id"]
        != compared["results"][0]["latest_vintage"]["vintage_id"]
    )
    assert all(c["provider_vintage_ms"] for c in compared["citations"])


def test_correlations_are_a_revisioned_lookup_aid_and_context_cites_vintages():
    conn = h.connection()
    trade = SanctionsTrade(conn)
    table = json.loads((h.FIXTURES / "dual_use_cn_correlation.json").read_text())
    assert (
        trade.record_correlations("global", table, principal_id="p", scopes=h.SCOPES)[
            "revision"
        ]
        == 1
    )
    assert (
        trade.record_correlations("global", table, principal_id="p", scopes=h.SCOPES)[
            "status"
        ]
        == "unchanged"
    )
    table["rows"] = table["rows"][:1]
    assert (
        trade.record_correlations("global", table, principal_id="p", scopes=h.SCOPES)[
            "revision"
        ]
        == 2
    )
    get, _ = comext_get("comext_ds045409_v1.json", "comext_ds045409_v2.json")
    trade.acquire([SPEC], http_get=get, fetched_at_ms=1_781_000_000_000)
    trade.acquire([SPEC], http_get=get, fetched_at_ms=1_783_000_000_000)
    context = trade.context("global", "1c350", scopes=h.SCOPES, backing=Backing(conn))
    assert context["status"] == "found" and all(
        c["status"] == "lookup-aid" for c in context["correlations"]
    )
    assert context["correlations"][0]["table_revision"] == 2
    flow = context["flows"][0]
    assert (
        len(flow["vintages"]) == 2
        and flow["trend"]["citations"][0]["series_id"] == flow["series_id"]
    )
    assert "different classifications" in context["lookup_aid_notice"]
    with pytest.raises(SanctionsError):
        trade.record_correlations(
            "global",
            {"table_id": "x", "source": {}, "rows": []},
            principal_id="p",
            scopes=h.SCOPES,
        )


def test_comext_transport_policy_refuses_other_hosts_and_redirects():
    get = guarded_http_get(
        transport=lambda **kw: {"status": 200, "content": b"{}", "final_url": kw["url"]}
    )
    with pytest.raises(SourcePackError) as caught:
        get("https://example.org/eurostat")
    assert caught.value.code == "network_policy"
    moved = guarded_http_get(
        transport=lambda **kw: {
            "status": 200,
            "content": b"{}",
            "final_url": "https://mirror.example.org/x",
        }
    )
    with pytest.raises(SourcePackError) as caught:
        moved(COMEXT_SELECTION["endpoint"] + "/DS-045409?format=JSON")
    assert caught.value.code == "network_policy"
    assert get(COMEXT_SELECTION["endpoint"] + "/DS-045409") == "{}"
    pinned = h.ROOT / COMEXT_SELECTION["fixture"]
    assert hashlib.sha256(pinned.read_bytes()).hexdigest()  # the pinned fixture exists


SYNTHETIC_ANNEXES = """<html><body>
<p>ANNEX I</p>
<p>CATEGORY 1 — SPECIAL MATERIALS AND RELATED EQUIPMENT</p>
<p>1A Systems, equipment and components</p>
<p>1A001 Components made from fluorinated compounds, as follows:</p>
<p>a. Seals and gaskets.</p>
<p>{heading}</p>
<p>1B001 Equipment for the production of fibres, as follows:</p>
<p>ANNEX IV</p>
<p>List of items subject to intra-Community transfer controls</p>
<p>1A001 Items referred to in Annex I that also need an intra-Union licence.</p>
</body></html>"""


def annex_entries(heading="1B Test, inspection and production equipment"):
    sections = parse_control_list_annex(
        SYNTHETIC_ANNEXES.format(heading=heading).encode()
    )
    return sections, {
        (s["locator"]["annex"], s["locator"]["official_norm_id"]): s
        for s in sections
        if s["locator"]["kind"] == "control-entry"
    }


def test_a_code_repeated_in_another_annex_is_a_separate_entry():
    _, entries = annex_entries()
    assert sorted(entries) == [("i", "1A001"), ("i", "1B001"), ("iv", "1A001")]
    annex_i = entries[("i", "1A001")]
    assert annex_i["text"].splitlines() == [
        "1A001 Components made from fluorinated compounds, as follows:",
        "a. Seals and gaskets.",
    ]
    assert annex_i["locator"]["path"] == "annex-i/1A001"
    annex_iv = entries[("iv", "1A001")]
    assert (
        annex_iv["locator"]["path"] == "annex-iv/1A001"
        and "intra-Union" in annex_iv["text"]
    )


def test_group_headings_rendered_as_paragraphs_end_the_previous_entry():
    sections, entries = annex_entries()
    headings = [
        s["text"] for s in sections if s["locator"]["kind"] == "xhtml-paragraph"
    ]
    assert "1B Test, inspection and production equipment" in headings
    assert "1A Systems, equipment and components" in headings
    _, renamed = annex_entries("1B Test, inspection and production equipment (renamed)")
    assert (
        renamed[("i", "1A001")]["text"] == entries[("i", "1A001")]["text"]
    )  # a heading change is not 1A001's


def test_only_annex_i_passages_are_control_list_entries(editions):
    conn, store, _ = editions
    work = consolidated_work(store)["work_id"]
    version = conn.execute(
        "SELECT version_id FROM legal_versions WHERE work_id=? AND "
        "content_coverage='captured-text' ORDER BY version_id LIMIT 1",
        [work],
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO legal_passages VALUES (?, 999, 'annex-iv-1C350', ?, ?)",
        [
            version,
            json.dumps(
                {
                    "kind": "control-entry",
                    "official_norm_id": "1C350",
                    "annex": "iv",
                    "path": "annex-iv/1C350",
                }
            ),
            "1C350 Annex IV wording",
        ],
    )
    texts = [e["text"] for e in SanctionsStore(conn).control_entries("global", "1C350")]
    assert "1C350 Annex IV wording" not in texts and len(texts) == 2
