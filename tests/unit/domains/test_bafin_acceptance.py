"""Offline acceptance: a German issuer to its cited BaFin notice dossier as of a date (#2106, BF12).

Journey on authored fictional fixtures (Musterwerke AG, ``DE000MSTR014``) with
sockets disabled: acquisition through the source-pack runtime and the real
adapters -> identity review -> projection into the Corporate Ownership graph ->
as-of queries on both sides of every publication date (no look-ahead) ->
dossier and evidence bundle -> alerts. One notifier states a three-level holder
chain and later corrects it, the other states instruments; a managers'
transaction aggregates two trades; a net short position ends; a warning names
a related entity; an authorisation record changes. The answers are identical
with the Market ``bafin-notices`` feature on and off (the feature governs
composition, not stored evidence), and the production pack replays offline
through the runtime.
"""

from __future__ import annotations

import json
import socket

import pytest

from src.domains import registry as domain_registry
from src.domains.market.bafin_identity import (
    BafinIdentity,
    organisation_key,
    resolve_issuer,
)
from src.domains.market.bafin_monitoring import BafinNoticeMonitor, verify_receipt
from src.domains.market.bafin_notices import (
    BafinNoticeStore,
    end_of_day_ms,
    feature_enabled,
)
from src.domains.market.bafin_ownership import BafinOwnershipProjection
from src.domains.market.bafin_queries import BafinQueries, export_dossier_bundle
from src.evidence_bundle.verifier import verify_bundle
from src.ingestion.bafin_sources import BafinNoticeAdapter, fixture_transport
from src.ingestion.source_pack_runtime import SourcePackRuntime
from src.ingestion.source_packs import SourcePackStore, validate_source_pack
from src.kb.ownership_graph import query
from src.kb.ownership_store import OwnershipStore
from src.kb.subscriptions import SubscriptionStore
from tests.unit import bafin_harness as h

FIXED = h.ms("2026-07-02")
# Resolving the issuer through the instrument master needs current market namespace and entitlement access.
MARKET_READER = h.SCOPES | {
    f"namespace:{h.MARKET_NS}:read",
    "market:entitlement:entitlement:fixture:read",
}


def PUBLIC_DNS(
    _host,
):  # the runtime's resolver hook: a public address, so no private-network refusal
    return ["8.8.8.8"]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError(
            "network access attempted during the offline acceptance run"
        )

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = (
        dict(domain_registry._REGISTRY),
        set(domain_registry._ENABLED),
        domain_registry._AUTHORITY,
    )
    yield
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])


FIRST_RUN = {
    "voting": "2026-03-10",
    "dealings": "2026-04-10",
    "shorts": "2026-04-10",
    "company": "2026-04-10",
}


def runtime_first_stage(conn) -> dict:
    """The first stage of every notice source through the source-pack runtime and its projector."""
    production = json.loads(h.PACK.read_text())
    sources = {
        key: h.fictional(key, h.STAGES[key][day]) for key, day in FIRST_RUN.items()
    }
    manifest = validate_source_pack(
        {
            **production,
            "sources": [
                {k: v for k, v in s.items() if k != "source_hash"}
                for s in sources.values()
            ],
        }
    )
    SourcePackStore(conn).install(
        manifest, principal_id="operator", enable=True, now_ms=10
    )
    clock = iter(range(h.ms("2026-04-10"), h.ms("2026-04-10") + 10_000_000))
    runtime = SourcePackRuntime(conn, now=lambda: next(clock), sleep=lambda _d: None)
    installed = {s["source_id"]: s for s in manifest["sources"]}
    adapters = {}
    for key, day in FIRST_RUN.items():
        source = installed[h.SOURCE_IDS[key]]
        runtime.accept_license(
            manifest["pack_id"], source["source_id"], principal_id="operator"
        )
        adapters[source["source_id"]] = BafinNoticeAdapter(
            source, transport=fixture_transport(h.pages(h.STAGES[key][day]))
        )
    return runtime.run(
        {
            "pack_id": manifest["pack_id"],
            "run_key": "bafin-offline",
            "operation": "documents",
            "max_results": 5000,
            "max_bytes": 20_000_000,
            "timeout_ms": 60_000,
        },
        principal_id="operator",
        adapters=adapters,
        dns_resolver=PUBLIC_DNS,
    )


def sources(rows):
    return [r["source"]["source_id"] for r in rows]


def journey(conn) -> dict:
    def now():  # fixed record time so answers compare across runs
        return FIXED

    answers: dict = {}
    receipt = runtime_first_stage(conn)
    answers["runtime"] = {s["source_id"]: s["status"] for s in receipt["sources"]}
    h.instruments(conn)
    h.ownership_entities(conn)
    monitor = BafinNoticeMonitor(conn, now=now)
    watch = monitor.create(
        h.NS,
        "accept",
        watch="issuer",
        isin=h.ISSUER,
        principal_id=h.PRINCIPAL,
        scopes=h.SCOPES,
    )
    subscriptions = SubscriptionStore(conn)
    subscriptions.commit_watermark(h.NS, 1, kind="ingestion")
    answers["baseline"] = monitor.run(
        watch["subscription_id"], principal_id=h.PRINCIPAL, scopes=h.SCOPES
    )
    projection = BafinOwnershipProjection(conn, now=now)
    answers["projected_first"] = projection.project(
        h.NS, h.OWN_NS, principal_id=h.PRINCIPAL, scopes=h.SCOPES
    )
    # Later publications: the correction and a second notifier, dealings, positions, warnings, licences.
    h.acquire(conn, "voting", "2026-04-20")
    h.acquire(conn, "shorts", "2026-05-01")
    h.acquire(conn, "warnings", "2026-05-15")
    h.acquire(conn, "dealings", "2026-06-01")
    h.acquire(conn, "shorts", "2026-06-01")
    h.acquire(conn, "company", "2026-07-01")
    # Identity review, then projection with the reviewed holder.
    identity = BafinIdentity(conn, now=now)
    proposed = identity.propose(
        h.NS, principal_id=h.PRINCIPAL, scopes=h.SCOPES, ownership_namespace=h.OWN_NS
    )
    holding = next(
        c
        for c in proposed["candidates"]
        if sorted(c["records"])
        == [organisation_key("Fiktiva Holding SE"), "register:fiktiva-holding"]
    )
    identity.service.review(
        h.NS,
        holding["candidate_id"],
        "accept",
        "register name and seat country agree",
        principal_id="reviewer",
        scopes=h.SCOPES,
    )
    answers["candidates"] = sorted(
        (tuple(sorted(c["records"])), c["basis"]) for c in proposed["candidates"]
    )
    answers["issuer"] = resolve_issuer(
        conn,
        {"isin": h.ISSUER, "lei": h.ISSUER_LEI, "name": "Musterwerke AG"},
        on="2026-03-05",
        market_namespace=h.MARKET_NS,
        principal_id=h.PRINCIPAL,
        scopes={"operator"},
    )
    answers["projected"] = projection.project(
        h.NS, h.OWN_NS, principal_id=h.PRINCIPAL, scopes=h.SCOPES
    )
    answers["graph"] = query(
        conn,
        h.OWN_NS,
        "direct_parents",
        "bafin-issuer:" + h.ISSUER,
        principal_id=h.PRINCIPAL,
        scopes=h.SCOPES,
        as_of="2026-05-01",
    )
    # Point-in-time queries on both sides of each publication date.
    q = BafinQueries(conn, now=now)
    answers["holders"] = {
        day: q.holders_above_thresholds(h.NS, h.ISSUER, day, scopes=h.READ_ONLY)
        for day in (
            "2026-03-04",
            "2026-03-05",
            "2026-03-19",
            "2026-03-20",
            "2026-04-13",
            "2026-04-14",
        )
    }
    answers["dealings"] = {
        day: q.managers_transactions(
            h.NS, scopes=h.READ_ONLY, isin=h.ISSUER, date_from="2026-01-01", date_to=day
        )
        for day in ("2026-03-11", "2026-03-12", "2026-05-19", "2026-05-20")
    }
    answers["shorts"] = {
        day: q.net_short_positions(h.NS, h.ISSUER, day, scopes=h.READ_ONLY)
        for day in (
            "2026-04-09",
            "2026-04-10",
            "2026-04-30",
            "2026-05-01",
            "2026-06-01",
        )
    }
    answers["warnings"] = {
        day: q.warnings_for_entity(
            h.NS, scopes=h.READ_ONLY, name="Fiktiva Invest GmbH", as_of=day
        )
        for day in ("2026-05-03", "2026-05-04")
    }
    answers["authorisation"] = {
        day: q.authorisation_status(
            h.NS, scopes=h.READ_ONLY, bafin_id="123456", as_of=day
        )
        for day in ("2026-04-09", "2026-04-10", "2026-07-01")
    }
    answers["dossier"] = q.dossier(
        h.NS,
        h.ISSUER,
        "2026-07-02",
        scopes=MARKET_READER,
        principal_id=h.PRINCIPAL,
        market_namespace=h.MARKET_NS,
    )
    answers["bundle"] = export_dossier_bundle(
        conn, answers["dossier"], created_at_ms=FIXED
    )
    subscriptions.commit_watermark(h.NS, 2, kind="ingestion")
    answers["alerts"] = monitor.run(
        watch["subscription_id"], principal_id=h.PRINCIPAL, scopes=h.SCOPES
    )
    answers["receipts_valid"] = [
        verify_receipt(conn, n["receipt"])["valid"]
        for n in answers["alerts"]["notifications"]
    ]
    return answers


def check(answers: dict) -> None:
    assert set(answers["runtime"].values()) == {"complete"}
    assert answers["baseline"]["baseline"] is True
    assert answers["projected_first"]["counts"]["inserted"] == 6
    assert answers["issuer"]["instrument"]["status"] == "resolved"
    # Holders: no look-ahead, the correction supersedes from its own publication date.
    holders = {
        day: [(r["source"]["source_id"], r["percentages"]["s39"]) for r in a["holders"]]
        for day, a in answers["holders"].items()
    }
    assert holders == {
        "2026-03-04": [],
        "2026-03-05": [("VR-2026-0001", "5.12")],
        "2026-03-19": [("VR-2026-0001", "5.12")],
        "2026-03-20": [("VR-2026-0007", "5.21")],
        "2026-04-13": [("VR-2026-0007", "5.21")],
        "2026-04-14": [("VR-2026-0007", "5.21"), ("VR-2026-0009", "4.05")],
    }
    chain = answers["holders"]["2026-04-14"]["holders"][0]["chain"]
    assert [m["name"] for m in chain] == [
        "Fiktiva Holding SE",
        "Fiktiva Beteiligungs GmbH",
        "Fiktiva Invest GmbH",
    ]
    assert answers["holders"]["2026-04-14"]["holders"][0]["corrections"]
    # Managers' transactions: published by the window end only; the aggregate is kept.
    dealings = {
        day: sources(a["transactions"]) for day, a in answers["dealings"].items()
    }
    assert dealings == {
        "2026-03-11": [],
        "2026-03-12": ["DD-2026-0101"],
        "2026-05-19": ["DD-2026-0101", "DD-2026-0102"],
        "2026-05-20": ["DD-2026-0101", "DD-2026-0102", "DD-2026-0110"],
    }
    aggregated = answers["dealings"]["2026-03-12"]["transactions"][0]
    assert (
        len(aggregated["trades"]) == 2 and aggregated["aggregate"]["price"] == "12.5333"
    )
    # Net short positions: first observed on 2026-04-10, the Kurzfrist position ends, Zeitwert leaves the list.
    shorts = {
        day: (
            [(p["holder"]["name"], p["position_pct"]) for p in a["positions"]],
            sorted(p["holder"]["name"] for p in a["no_longer_published"]),
        )
        for day, a in answers["shorts"].items()
    }
    assert shorts == {
        "2026-04-09": ([], []),
        "2026-04-10": (
            [("Kurzfrist Capital LLP", "0.55"), ("Zeitwert Partners Ltd", "0.71")],
            [],
        ),
        "2026-04-30": (
            [("Kurzfrist Capital LLP", "0.55"), ("Zeitwert Partners Ltd", "0.71")],
            [],
        ),
        "2026-05-01": ([("Zeitwert Partners Ltd", "0.71")], ["Kurzfrist Capital LLP"]),
        "2026-06-01": ([], ["Kurzfrist Capital LLP", "Zeitwert Partners Ltd"]),
    }
    assert [len(a["name_equal_unreviewed"]) for a in answers["warnings"].values()] == [
        0,
        1,
    ]
    assert [a["status"] for a in answers["authorisation"].values()] == [
        "not in the acquired company database as of the date",
        "listed",
        "listed",
    ]
    ended = answers["authorisation"]["2026-07-01"]["entities"][0]["licences"]
    assert {licence["type"]: licence["as_of_state"] for licence in ended} == {
        "Anlageberatung": "ended",
        "Finanzportfolioverwaltung": "active as published",
    }
    # The ownership graph shows the notifications as voting_rights control assertions, keyed after review.
    edges = answers["graph"]["other_holdings"]["minority_as_stated"]
    assert {e["assertion_kind"] for e in edges} == {"voting_rights"}
    keyed = {e["holder"]["name"]: e["holder"].get("key") for e in edges}
    assert (
        keyed["Fiktiva Holding SE"] == "register:fiktiva-holding"
        and keyed["Fiktiva Invest GmbH"] is None
    )
    assert all(e["source"]["statement_id"].startswith("bafin-rev:") for e in edges)
    assert (
        answers["projected"]["counts"]["revised"] >= 5
    )  # the correction revised the projected assertions
    # Dossier and bundle.
    dossier = answers["dossier"]
    assert dossier["issuer_resolution"]["instrument"]["status"] == "resolved"
    assert [w["entity"] for w in dossier["warnings_and_measures"]] == [
        "Fiktiva Invest GmbH"
    ]
    assert dossier["authorisations"][0]["entities"][0]["bafin_id"] == "123456"
    assert all(
        item["publication_date"] or item["publication_basis"] == "first-observed"
        for item in dossier["holders"]["holders"]
        + dossier["managers_transactions"]["transactions"]
    )
    assert verify_bundle(answers["bundle"]).valid
    # Alerts cite the notice revisions.
    kinds = sorted(n["kind"] for n in answers["alerts"]["notifications"])
    assert {
        "corrected_notification",
        "new_notification",
        "short_position_ended",
        "new_managers_transaction",
    } <= set(kinds)
    assert all(answers["receipts_valid"])

    def keys(value):
        if isinstance(value, dict):
            return set(value) | {k for v in value.values() for k in keys(v)}
        if isinstance(value, list):
            return {k for v in value for k in keys(v)}
        return set()

    # Answers state their semantics and carry no advice, signal or sentiment field.
    produced = keys(
        [answers[name] for name in ("holders", "dealings", "shorts", "dossier")]
    )
    assert not produced & {"recommendation", "signal", "sentiment", "score", "advice"}
    assert "no investment advice" in answers["dossier"]["notice"].lower()


def comparable(answers: dict) -> dict:
    keep = {
        k: answers[k]
        for k in (
            "holders",
            "dealings",
            "shorts",
            "warnings",
            "authorisation",
            "candidates",
        )
    }
    keep["dossier_hash"] = answers["dossier"]["dossier_hash"]
    keep["alerts"] = sorted(
        (n["kind"], n["receipt"]["revision_id"])
        for n in answers["alerts"]["notifications"]
    )
    return json.loads(json.dumps(keep, sort_keys=True, default=str))


def test_offline_journey_with_the_feature_on_and_off():
    from tests.unit.composition.test_migration import _migrated

    results = {}
    for selected in ([], ["bafin-notices"]):
        conn, coordinator, bundles, _ = _migrated()
        coordinator.select("market", bundles["market"]["version"], features=selected)
        assert coordinator.activate(f"market-{len(selected)}")["status"] == "published"
        assert feature_enabled(conn) is bool(selected)
        answers = journey(conn)
        check(answers)
        results[bool(selected)] = comparable(answers)
    assert results[True] == results[False]


def test_reacquisition_is_idempotent_and_restart_keeps_answers(tmp_path):
    import duckdb

    path = str(tmp_path / "bafin.duckdb")
    conn = duckdb.connect(path)
    answers = journey(conn)
    tables = (
        "bafin_notices",
        "bafin_notice_revisions",
        "bafin_listing_observations",
        "bafin_persons",
        "ownership_record_revisions",
    )
    counts = {
        t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables
    }
    for key, stages in h.STAGES.items():
        h.acquire(conn, key, sorted(stages)[-1], run_id=f"again:{key}")
    BafinOwnershipProjection(conn, now=lambda: FIXED).project(
        h.NS, h.OWN_NS, principal_id=h.PRINCIPAL, scopes=h.SCOPES
    )
    assert {
        t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables
    } == counts
    conn.close()
    reopened = duckdb.connect(path, read_only=True)
    again = BafinQueries(reopened).holders_above_thresholds(
        h.NS, h.ISSUER, "2026-04-14", scopes=h.READ_ONLY
    )
    assert again["answer_hash"] == answers["holders"]["2026-04-14"]["answer_hash"]
    assert (
        BafinNoticeStore(reopened, initialize=False).visible(
            h.NS,
            public_cutoff_ms=end_of_day_ms("2026-03-04"),
            kinds=("voting_rights_notification",),
        )["notices"]
        == []
    )
    assert OwnershipStore(reopened, initialize=False).records(
        h.OWN_NS, principal_id=h.PRINCIPAL, scopes=h.SCOPES
    )


def test_production_sources_replay_offline_through_the_runtime():
    conn = h.connection()
    manifest = validate_source_pack(json.loads(h.PACK.read_text()))
    SourcePackStore(conn).install(
        manifest, principal_id="operator", enable=True, now_ms=10
    )
    clock = iter(range(1_000, 10_000_000))
    runtime = SourcePackRuntime(conn, now=lambda: next(clock), sleep=lambda _d: None)
    for source in manifest["sources"]:
        runtime.accept_license(
            manifest["pack_id"], source["source_id"], principal_id="operator"
        )
    receipt = runtime.run(
        {
            "pack_id": manifest["pack_id"],
            "run_key": "bafin-production-offline",
            "operation": "documents",
            "max_results": 5000,
            "max_bytes": 50_000_000,
            "timeout_ms": 60_000,
        },
        principal_id="operator",
        adapters=runtime.fixture_adapters(manifest["pack_id"], h.ROOT),
        dns_resolver=PUBLIC_DNS,
    )
    assert {s["source_id"]: s["status"] for s in receipt["sources"]} == {
        s["source_id"]: "complete" for s in manifest["sources"]
    }
    store = BafinNoticeStore(conn)
    kinds = {v["notice"]["kind"] for v in store.visible("market-bafin")["notices"]}
    # The production issuer set is out of scope for the authored rows; only the unscoped warnings are stored.
    assert kinds == {"bafin_warning", "bafin_measure"}
    receipts = {r["provider"]: r["receipt"] for r in store.receipts("market-bafin")}
    assert receipts["bafin-voting-rights"]["counts"]["out_of_scope"] == 3
