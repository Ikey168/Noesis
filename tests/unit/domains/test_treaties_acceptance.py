"""Offline treaty-to-actions acceptance for the Legal treaties provider (TR12, #2640).

The pinned ``legal-research`` 1.5.0 fixtures for the three ``treaties``
sources (UN Treaty Collection, CELLAR agreements, Council of Europe) replay
through the real source-pack runtime (fixture adapters compiled from the
installed pack) with sockets blocked and the ``treaties-untc``, ``treaties-eu``
and ``treaties-coe`` features selected. A treaty and a state reach cited
treaty actions with their revision history, as-of answers, reviewable
participant identity and cross-pack links; a subject with no records is
answered as such. Every treaty, state, act and date is fictional; nothing here
is live evidence.
"""

from __future__ import annotations

import json
import socket

import pytest

from src.domains import registry as domain_registry
from src.ingestion.source_pack_runtime import SourcePackRuntime
from src.ingestion.source_packs import SourcePackStore, validate_source_pack
from src.ingestion.treaties_sources import (
    MINIMISATION,
    TreatiesAdapter,
    fixture_transport,
)
from src.kb.subscriptions import SubscriptionStore
from src.kb.treaties_identity import TreatiesIdentity, place_key
from src.kb.treaties_links import TreatiesLinks
from src.kb.treaties_monitoring import TreatiesMonitor
from src.kb.treaties_queries import TreatiesQueries
from src.kb.treaties_records import (
    TreatiesStore,
    feature_enabled,
    forbidden_keys,
    readiness,
)
from tests.unit import treaties_harness as h
from tests.unit.composition.test_migration import _migrated
from tools.knowledge_engine_mcp.treaties import guard

PACK_ID = "legal-research"
PUBLIC_DNS = lambda _host: ["8.8.8.8"]
FEATURES = ["treaties-untc", "treaties-eu", "treaties-coe"]
EXCLUDED_WORDS = ("is bound by", "must comply", "complies with", "is in breach", "the reservation is invalid",
                  "legal advice:")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("offline acceptance must not open sockets")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = (dict(domain_registry._REGISTRY), set(domain_registry._ENABLED), domain_registry._AUTHORITY)
    yield
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])


class Env:
    def __init__(self) -> None:
        self.conn = h.connection()
        self.clock = 4_102_444_800_000
        _, coordinator, bundles, _ = _migrated(self.conn)
        coordinator.select("legal", bundles["legal"]["version"], features=FEATURES)
        coordinator.activate("treaties-acceptance")
        manifest = validate_source_pack(json.loads(h.PACK.read_text()))
        SourcePackStore(self.conn).install(manifest, principal_id="operator", enable=True, now_ms=1)
        runtime = self.runtime()
        for source_id in h.SOURCES:
            runtime.accept_license(PACK_ID, source_id, principal_id="operator")

    def now(self) -> int:
        self.clock += 1
        return self.clock

    def runtime(self) -> SourcePackRuntime:
        return SourcePackRuntime(self.conn, now=self.now, sleep=lambda _d: None)

    def run(self, source_ids, key, adapters=None) -> dict:
        runtime = self.runtime()
        fixtures = runtime.fixture_adapters(PACK_ID, h.ROOT)
        return runtime.run(
            {"pack_id": PACK_ID, "run_key": key, "operation": "records", "source_ids": list(source_ids),
             "max_results": 5000, "max_bytes": 50_000_000, "timeout_ms": 120_000},
            principal_id="operator", adapters={s: (adapters or {}).get(s) or fixtures[s] for s in source_ids},
            dns_resolver=PUBLIC_DNS, secret_resolver=lambda _ref: None)


def without_notices(value):
    """The answer without its boundary notices, which name the exclusions they refuse."""
    if isinstance(value, dict):
        return {k: without_notices(v) for k, v in value.items()
                if k not in {"notice", "note", "reason", "boundary", "exclusions"}}
    if isinstance(value, list):
        return [without_notices(v) for v in value]
    return value


def v2_adapter(source_id: str) -> TreatiesAdapter:
    return TreatiesAdapter(h.source(source_id), transport=fixture_transport(h.native_pages(source_id, v2=True)))


def test_treaty_and_state_to_cited_actions_with_revision_history_identity_and_links():
    env = Env()
    conn = env.conn
    assert all(feature_enabled(conn, f) for f in FEATURES)
    first = env.run(h.SOURCES, "treaties-1")
    assert first["status"] == "complete"
    store = TreatiesStore(conn)
    count = conn.execute("SELECT count(*) FROM treaty_revisions").fetchone()[0]
    assert count == 42  # 17 UNTC + 8 CELLAR + 17 Council of Europe records
    assert env.run(h.SOURCES, "treaties-replay")["status"] == "complete"
    assert conn.execute("SELECT count(*) FROM treaty_revisions").fetchone()[0] == count  # a replay adds nothing
    ready = readiness(conn)
    assert ready["enabled"] == {"untc": True, "eu-cellar": True, "coe-treaty-office": True}
    assert {p["live"] for p in ready["providers"].values()} == {"unverified-live"}

    # Monitors start before the later depositary statuses arrive.
    subs = SubscriptionStore(conn)
    subs.commit_watermark(h.NS, 1)
    monitor = TreatiesMonitor(conn, now=env.now)
    treaty_sub = monitor.create(h.NS, "wetlands", watch="treaty", key="XXVII-99", principal_id="alice",
                                scopes=h.SCOPES)["subscription_id"]
    monitor.run(treaty_sub, 1, principal_id="alice", scopes=h.SCOPES)
    assert env.run(h.SOURCES, "treaties-2", adapters={s: v2_adapter(s) for s in h.SOURCES})["status"] == "complete"
    subs.commit_watermark(h.NS, 2)
    notices = monitor.run(treaty_sub, 2, principal_id="alice", scopes=h.SCOPES)["notifications"]
    assert {"action_recorded", "depositary_correction", "removed_by_source"} <= {n["kind"] for n in notices}

    # Revision history: the corrected accession date is a new revision naming its predecessor.
    accession = "treaties:untc:action:XXVII-99:northwind-republic:accession:1"
    history = store.history(h.NS, accession, scopes=h.READ_ONLY)
    assert [(r["change"], r["record"]["fields"]["deposit_date"]) for r in history] == [
        ("new", "2092-05-10"), ("revised", "2092-05-11")]
    assert history[1]["previous_revision_id"] == history[0]["revision_id"]
    oldland = store.history(h.NS, "treaties:untc:action:XXVII-99:oldland:succession:1", scopes=h.READ_ONLY)
    assert [r["change"] for r in oldland] == ["new", "removed-by-source"]  # kept, never deleted
    eif = store.records(h.NS, scopes=h.READ_ONLY, record_keys=[h.EU])[0]["record"]["fields"]["entry_into_force"]
    assert eif["date"] == "2093-06-01"

    # Treaty to cited actions as of a date, current and as the depositary published it earlier.
    ask = TreatiesQueries(conn)
    status = ask.status_as_of(h.NS, "XXVII-99", "Northwind Republic", "2095-01-01", scopes=h.READ_ONLY)
    (answer,) = status["answers"]
    assert answer["chain"][0]["deposit_date"] == "2092-05-11"
    assert answer["depositary_revision_used"] == "2099-06-20T10:00:00"
    (objection,) = answer["statements"]
    assert objection["objects_to"]["link"] == "linked by the source"
    earlier = ask.status_as_of(h.NS, "XXVII-99", "Northwind Republic", "2095-01-01", scopes=h.READ_ONLY,
                               depositary_as_of="2099-02-01")
    assert earlier["answers"][0]["chain"][0]["deposit_date"] == "2092-05-10"
    pending = ask.status_as_of(h.NS, "CETS 999", "Southland", "2099-06-01", scopes=h.READ_ONLY)
    assert pending["status"] == "pending" and pending["answers"][0]["pending"][0]["source_text"] == "01/09/2099"
    bundle = ask.evidence_bundle(status)
    assert bundle["bibliography"] and all("as of" in b["text"] and "revision" in b["text"]
                                          for b in bundle["bibliography"])

    # Reviewable identity: a state reaches its actions under both depositaries only through accepted matches.
    places = h.seed_places(conn)
    identity = TreatiesIdentity(conn, now=env.now)
    proposed = identity.propose(h.NS, principal_id="analyst", scopes=h.SCOPES, geo_namespace=h.NS)
    assert {c["state"] for c in proposed["candidates"]} == {"proposed"}
    unreviewed = ask.participant_actions(h.NS, place_key(places["XEA"]), scopes=h.SCOPES)
    assert unreviewed["status"] == "participant_not_on_record"  # nothing reached before review
    for candidate in proposed["candidates"]:
        if place_key(places["XEA"]) in candidate["records"]:
            identity.review(h.NS, candidate["candidate_id"], "accept", "reviewed against the source",
                            principal_id="reviewer", scopes=h.REVIEW_SCOPES)
    reached = ask.participant_actions(h.NS, place_key(places["XEA"]), scopes=h.SCOPES)
    assert {p["participant_key"] for p in reached["participants"]} == {
        "treaties:untc:participant:exampland", "treaties:coe:participant:exampland",
        "treaties:eu-cellar:participant:xea"}
    assert {a["provider"] for a in reached["actions"]} == {"untc", "coe-treaty-office"}
    assert all(a["citation"]["revision_id"] for a in reached["actions"])
    assert identity.unmatched(h.NS, scopes=h.SCOPES)["participants"]  # the rest stay visible as unmatched

    # Cross-pack links by citation, shared identifier or accepted match; missing targets reported.
    h.seed_legal_work(conn, "32093D0202")
    h.seed_sanctions_basis(conn, "Measures referring to the Convention on Example Data Cooperation (CETS No. 999)")
    h.seed_trade_reporter(conn, "XEA")
    links = TreatiesLinks(conn, now=env.now).link_all(h.NS, principal_id="analyst", scopes=h.SCOPES)
    kinds = {(link["target_kind"], link["basis"], link["status"]) for link in links["links"]}
    assert {("legal-work", "citation", "resolved"), ("legal-work", "citation", "target-missing"),
            ("sanctions-legal-basis", "citation", "resolved"), ("trade-reporter", "shared-identifier", "resolved"),
            ("trade-reporter", "accepted-match", "resolved")} <= kinds

    # A subject with no records is answered as such.
    nobody = ask.status_as_of(h.NS, "XXVII-99", "Nowhereland", "2095-01-01", scopes=h.READ_ONLY)
    assert nobody["status"] == "no_action_on_record" and nobody["answers"] == []
    assert ask.treaty_statements(h.NS, "XXVII-1", scopes=h.READ_ONLY)["status"] == "treaty_not_on_record"

    # Exclusions and the minimisation decision hold across every answer.
    answers = [status, earlier, pending, reached, links, bundle, notices]
    assert forbidden_keys(answers) == [] and guard({"answers": answers})
    text = json.dumps(without_notices(answers)).casefold()
    assert not [w for w in EXCLUDED_WORDS if w in text]
    stored = json.dumps(conn.execute("SELECT record_json FROM treaty_revisions").fetchall())
    assert "central.authority@example.org" not in stored and "123 456 789" not in stored
    assert "[contact details withheld: TR01]" in stored
    assert MINIMISATION["policy"] == "treaties-minimisation-v1"
    assert not conn.execute("SELECT count(*) FROM treaty_revisions WHERE record_json LIKE '%signatory_name%'"
                            ).fetchone()[0]
