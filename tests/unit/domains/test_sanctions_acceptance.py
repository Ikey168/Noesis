"""Offline acceptance: a party or control code to a cited, per-list listing history (#1989, S11).

The pinned ``legal-research`` fixtures run through the real source-pack runtime
(first snapshots of the four lists, the sanctions act and two dual-use
editions); later snapshots replay through the same list adapter and
projector; a Comext vintage goes through the dataset connector. The journey
then answers a party as of three dates and a control code across two editions,
exercises proposed, accepted and reverted identity decisions, restarts the
warehouse, and replays subscriptions. Nothing touches the network and every
party is fictional; this is offline fixture evidence, never live coverage.
"""

from __future__ import annotations

import json
import socket

import duckdb
import pytest

from src.ingestion.source_pack_runtime import SourcePackRuntime
from src.ingestion.source_packs import SourcePackStore
from src.kb.sanctions import SanctionsStore
from src.kb.sanctions_identity import SanctionsIdentity
from src.kb.sanctions_monitoring import SanctionsMonitor
from src.kb.sanctions_queries import SanctionsQueries
from src.kb.sanctions_trade import SanctionsTrade
from src.kb.subscriptions import SubscriptionStore
from tests.unit import sanctions_harness as h

PUBLIC_DNS = lambda _host: ["8.8.8.8"]  # noqa: E731 - preflight resolver; no request is ever sent
SNAPSHOT_TABLES = (
    "sanctions_snapshots",
    "sanctions_designations",
    "sanctions_revisions",
    "sanctions_aliases",
    "sanctions_snapshot_members",
    "sanctions_programmes",
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError(
            "the acceptance journey must not open a network connection"
        )

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def records(conn):
    return {
        t: conn.execute(f"SELECT * FROM {t} ORDER BY ALL").fetchall()
        for t in SNAPSHOT_TABLES
    }


def run_pack(conn, key, **controls):
    value = h.manifest()
    store = SourcePackStore(conn)
    if not conn.execute(
        "SELECT 1 FROM source_pack_versions WHERE pack_id=?", [value["pack_id"]]
    ).fetchone():
        store.install(value, principal_id="operator", enable=True, now_ms=10)
    clock = iter(range(2_000, 10_000_000))
    runtime = SourcePackRuntime(conn, now=lambda: next(clock), sleep=lambda _d: None)
    for item in value["sources"]:
        runtime.accept_license(
            value["pack_id"], item["source_id"], principal_id="operator"
        )
    return runtime.run(
        {
            "pack_id": value["pack_id"],
            "run_key": key,
            "operation": "records",
            "max_results": 1000,
            "max_bytes": 100_000_000,
            "timeout_ms": 120_000,
            **controls,
        },
        principal_id="operator",
        adapters=runtime.fixture_adapters(value["pack_id"], h.ROOT),
        dns_resolver=PUBLIC_DNS,
    )


def designation(conn, list_id, entry_id):
    return conn.execute(
        "SELECT designation_id FROM sanctions_designations WHERE list_id=? AND list_entry_id=?",
        [list_id, entry_id],
    ).fetchone()[0]


def first_edition_only():
    """The dual-use CELLAR fixture restricted to the 2024 edition (the later one arrives with the runtime run)."""
    from src.ingestion.legal_sources import replay_native_fixture

    item = h.source("cellar-dual-use-2021-821")
    fixture = json.loads((h.ROOT / item["fixture"]["path"]).read_text())
    for page in fixture["native_pages"]:
        if isinstance(page["body"], dict):
            page["body"]["results"]["bindings"] = [
                b
                for b in page["body"]["results"]["bindings"]
                if "20251115" not in b["celex"]["value"]
            ]
    return item, replay_native_fixture(item, fixture)


def test_party_and_control_code_to_a_cited_per_list_history(tmp_path):
    from src.kb.legal import LegalStore

    path = str(tmp_path / "sanctions-acceptance.duckdb")
    conn = duckdb.connect(path)
    # Before the pack run: the first EU snapshot and the first dual-use edition, then monitors take a baseline.
    item, edition = first_edition_only()
    LegalStore(conn).project(
        h.NS, edition, run_id="edition-2024", source_id=item["source_id"]
    )
    h.apply(conn, "eu", "eu_fsf_2026-01-15.xml")
    monitor = SanctionsMonitor(conn)
    watches = {
        "eu-programme": monitor.create(
            h.NS,
            "ukr",
            watch="programme",
            key="UKR",
            list_id="eu",
            principal_id="analyst",
            scopes=h.SCOPES,
        )["subscription_id"],
        "ofac-programme": monitor.create(
            h.NS,
            "fixture-eo",
            watch="programme",
            key="FIXTURE-EO",
            list_id="ofac",
            principal_id="analyst",
            scopes=h.SCOPES,
        )["subscription_id"],
        "control": monitor.create(
            h.NS,
            "1c350",
            watch="control-code",
            key="1C350",
            principal_id="analyst",
            scopes=h.SCOPES,
        )["subscription_id"],
    }
    SubscriptionStore(conn).commit_watermark(h.NS, 1, kind="ingestion")
    baseline = {k: monitor_run(conn, v, 1) for k, v in watches.items()}
    assert (
        kinds(baseline["eu-programme"]),
        kinds(baseline["ofac-programme"]),
        kinds(baseline["control"]),
    ) == (["listed", "listed"], [], ["edition_in_view"])

    # The pinned pack through the real runtime: the lists' next snapshots, the legal act and both editions.
    receipt = run_pack(conn, "acceptance-1")
    assert receipt["status"] == "complete"
    runtime_sources = {s["source_id"] for s in receipt["sources"]}
    assert (
        set(h.SOURCES.values())
        | {"cellar-sanctions-acts-eng", "cellar-dual-use-2021-821"}
        <= runtime_sources
    )
    SubscriptionStore(conn).commit_watermark(h.NS, 2, kind="ingestion")
    second = {k: monitor_run(conn, v, 2) for k, v in watches.items()}
    assert (
        kinds(second["eu-programme"]),
        kinds(second["ofac-programme"]),
        kinds(second["control"]),
    ) == (["listed"], ["listed", "listed"], ["new_edition"])

    # Later snapshots through the same adapter and projector.
    for list_id, name in (
        ("eu", "eu_fsf_2026-06-01.xml"),
        ("un", "un_sc_2026-06-10.xml"),
        ("ofac", "ofac_sdn_2026-06-05.xml"),
        ("uk", "uk_sanctions_2026-06-15.xml"),
    ):
        h.apply(conn, list_id, name)
    trade = SanctionsTrade(conn)
    table = json.loads((h.FIXTURES / "dual_use_cn_correlation.json").read_text())
    trade.record_correlations(h.NS, table, principal_id="analyst", scopes=h.SCOPES)
    body = (h.FIXTURES / "comext_ds045409_v1.json").read_text()
    vintage = trade.acquire(
        [
            {
                "dataset": "DS-045409",
                "geography": "DE",
                "partner": "CN",
                "product": "29309098",
                "flow": "2",
                "indicators": "VALUE_IN_EUROS",
                "freq": "M",
            }
        ],
        http_get=lambda _url: body,
        fetched_at_ms=1_781_000_000_000,
    )
    assert vintage[0]["status"] == "new_vintage"

    # Re-ingesting the same snapshots is idempotent (a full backfill re-acquisition, then a direct replay).
    before = records(conn)
    again = run_pack(conn, "acceptance-2", mode="backfill", backfill={"from_ms": 0})
    assert again["status"] == "complete" and records(conn) == before
    assert (
        h.apply(conn, "eu", "eu_fsf_2026-06-01.xml", run_id="again")["status"]
        == "unchanged"
    )
    assert records(conn) == before
    conn.close()

    # Restart: every record and subscription survives.
    conn = duckdb.connect(path)
    assert records(conn) == before
    assert {
        s["subscription_id"]
        for s in SubscriptionStore(conn).list(
            principal_id="analyst", scopes=h.SCOPES, namespace=h.NS
        )
    } == set(watches.values())
    SubscriptionStore(conn).commit_watermark(h.NS, 3, kind="ingestion")
    third = {k: monitor_run(conn, v, 3) for k, v in watches.items()}
    assert (
        kinds(third["eu-programme"]),
        kinds(third["ofac-programme"]),
        kinds(third["control"]),
    ) == (["amended", "delisted", "listed"], ["amended"], [])
    delisting = next(
        n for n in third["eu-programme"]["notifications"] if n["kind"] == "delisted"
    )
    assert [
        c["publication_date"] for c in delisting["cites"]["snapshots_compared"]
    ] == ["2026-03-01", "2026-06-01"]
    assert all(
        monitor_run(conn, v, 3)["status"] == "replayed" for v in watches.values()
    )
    SubscriptionStore(conn).commit_watermark(h.NS, 4, kind="ingestion")
    assert all(monitor_run(conn, v, 4)["notifications"] == [] for v in watches.values())

    queries = SanctionsQueries(conn)
    # A party as of three dates: before listing, while listed, after delisting.
    answers = {
        day: queries.history_as_of(h.NS, day, scopes=h.SCOPES, identifier="IMO 9999991")
        for day in ("2026-01-15", "2026-03-01", "2026-06-20")
    }
    assert {k: v["lists"]["eu"][0]["status"] for k, v in answers.items()} == {
        "2026-01-15": "not_listed_in_snapshot",
        "2026-03-01": "listed",
        "2026-06-20": "not_listed_in_snapshot",
    }
    assert answers["2026-01-15"]["lists"]["eu"][0]["delisting"] is None
    assert (
        answers["2026-01-15"]["lists"]["ofac"][0]["status"] == "unknown"
    )  # no OFAC snapshot yet
    listed = answers["2026-03-01"]["lists"]["eu"][0]
    snapshot = listed["coverage"]["snapshot"]
    assert (
        snapshot["publication_date"] == "2026-03-01"
        and len(snapshot["file_sha256"]) == 64
    )
    revision = listed["statement"]["listing_revision"]
    assert revision["change"] == "listed" and revision["source_revision"] == snapshot
    assert [s["publication_date"] for s in revision["compared_snapshots"]] == [
        "2026-01-15",
        "2026-03-01",
    ]
    basis = listed["statement"]["legal_basis"][0]
    work = conn.execute(
        "SELECT work_id FROM legal_works WHERE identifiers_json LIKE '%32014R0269%'"
    ).fetchone()[0]
    assert (
        basis["status"] == "resolved"
        and basis["work_id"] == work
        and basis["expression_id"]
    )
    assert basis["passages"]["passages"][0]["locator"] == {
        "id": None,
        "index_in_headings_and_paragraphs": 6,
        "kind": "xhtml-paragraph",
        "precision": "element selection; not byte offsets",
        "tag": "p",
    }
    after = answers["2026-06-20"]["lists"]["eu"][0]
    assert [
        s["publication_date"] for s in after["delisting"]["compared_snapshots"]
    ] == ["2026-03-01", "2026-06-01"]
    assert sorted(answers["2026-06-20"]["lists"]) == [
        "eu",
        "ofac",
        "uk",
    ]  # never merged into one party
    assert answers["2026-06-20"]["lists"]["uk"][0]["status"] == "listed"

    # A control code across two editions, with the passage locator of each.
    early = queries.control_entry_as_of(h.NS, "1C350", "2025-06-01", scopes=h.SCOPES)
    late = queries.control_entry_as_of(h.NS, "1C350", "2026-02-01", scopes=h.SCOPES)
    assert early["edition"]["entry"]["edition"] == "02021R0821-20241115"
    assert late["edition"]["entry"]["edition"] == "02021R0821-20251115"
    assert early["edition"]["entry"]["locator"]["official_norm_id"] == "1C350"
    changed = queries.compare_control_entry(
        h.NS,
        "1C350",
        early["edition"]["version_id"],
        late["edition"]["version_id"],
        scopes=h.SCOPES,
    )
    assert changed["status"] == "changed"
    context = SanctionsTrade(conn, initialize=False).context(
        h.NS, "1C350", scopes=h.SCOPES
    )
    assert (
        context["flows"][0]["vintages"][0]["vintage_id"]
        and context["correlations"][0]["status"] == "lookup-aid"
    )

    # Identity: proposed, accepted and reverted; list records unchanged at every step.
    identity = SanctionsIdentity(conn)
    proposed = identity.propose(h.NS, principal_id="analyst", scopes=h.SCOPES)
    imo = next(
        c
        for c in proposed["candidates"]
        if c["records"] == ["sanctions:eu:EU.9002.02", "sanctions:ofac:99001"]
    )
    similar = identity.propose_link(
        h.NS,
        designation(conn, "un", "QDe.901"),
        target_key="entity:examplar",
        target_entity="ent-examplar",
        evidence={
            "kind": "name",
            "value": "Examplar Freight LLC",
            "target_source": "fixture document",
        },
        principal_id="analyst",
        scopes=h.SCOPES,
    )
    assert records(conn) == before
    identity.service.review(
        h.NS,
        imo["candidate_id"],
        "accept",
        "same IMO number",
        principal_id="reviewer",
        scopes=h.REVIEW_SCOPES,
    )
    assert records(conn) == before
    linked = queries.history_as_of(
        h.NS, "2026-03-05", scopes=h.SCOPES, identifier="9999991"
    )
    link = linked["lists"]["ofac"][0]["identity"]["links"][0]
    assert (
        link["decision_id"]
        and link["reviewer"] == "reviewer"
        and link["reason"] == "same IMO number"
    )
    assert link["source_revisions_compared"]
    un = queries.history_as_of(
        h.NS, "2026-06-15", scopes=h.SCOPES, list_id="un", list_entry_id="QDe.901"
    )
    assert [
        c["candidate_id"] for c in un["lists"]["un"][0]["identity"]["candidates"]
    ] == [similar["candidate_id"]]
    identity.service.revert(
        h.NS,
        imo["candidate_id"],
        "reopen",
        principal_id="reviewer",
        scopes=h.REVIEW_SCOPES,
    )
    assert records(conn) == before
    assert (
        queries.history_as_of(
            h.NS, "2026-03-05", scopes=h.SCOPES, identifier="9999991"
        )["lists"]["ofac"][0]["identity"]["links"]
        == []
    )

    for answer in (
        *answers.values(),
        early,
        late,
        changed,
        context,
        linked,
        un,
        *baseline.values(),
        *second.values(),
        *third.values(),
    ):
        assert h.forbidden_keys(answer) == []
    conn.close()


def monitor_run(conn, subscription_id, watermark):
    return SanctionsMonitor(conn).run(
        subscription_id, watermark, principal_id="analyst", scopes=h.SCOPES
    )


def kinds(result):
    return sorted(n["kind"] for n in result["notifications"])


def test_store_reads_are_revision_addressable_after_the_journey():
    conn = h.connection()
    for name in h.FILES["eu"]:
        h.apply(conn, "eu", name)
    store = SanctionsStore(conn)
    history = store.history(h.NS, designation(conn, "eu", "EU.9001.01"))
    first, second = (store.revision(h.NS, r["revision_id"]) for r in history)
    assert (
        first["statement"] != second["statement"]
        and first["revision_no"] == 1
        and second["revision_no"] == 2
    )
    assert first["source_revision"]["publication_date"] == "2026-01-15"
