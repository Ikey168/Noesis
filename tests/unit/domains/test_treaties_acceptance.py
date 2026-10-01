"""Offline treaty-to-actions acceptance for the Legal treaties provider (TR12, #2640).

The pinned ``legal-research`` 1.5.0 fixtures for the three ``treaties``
sources replay through the real source-pack runtime (fixture adapters compiled
from the installed pack) with sockets blocked and the ``treaties-untc``,
``treaties-eu`` and ``treaties-coe`` features selected. The UN Treaty
Collection entry is declined and acquires nothing; its authored status page is
then replayed under a *test-only* accepted licence decision, as an operator
holding written permission would. A treaty and a state reach cited treaty
actions with revision history, reviewable identity and cross-pack links; a
subject with no records has none on record. Every treaty, action and text is
fictional; nothing here is live evidence.
"""

from __future__ import annotations

import json
import socket

import pytest

from src.domains import registry as domain_registry
from src.evidence_bundle.verifier import verify_bundle
from src.ingestion.source_pack_runtime import SourcePackRuntime
from src.ingestion.source_packs import SourcePackStore, validate_source_pack
from src.ingestion.treaties_sources import TreatiesAdapter, fixture_transport
from src.kb.subscriptions import SubscriptionStore
from src.kb.treaties_identity import TreatiesIdentity
from src.kb.treaties_links import TreatiesLinks
from src.kb.treaties_monitoring import TreatiesMonitor
from src.kb.treaties_queries import TreatyQueries
from src.kb.treaties_records import (
    feature_enabled,
    forbidden_keys,
    minimisation_violations,
)
from src.kb.treaties_store import readiness
from tests.unit import treaties_harness as h
from tests.unit.composition.test_migration import _migrated

PACK_ID = "legal-research"
PUBLIC_DNS = lambda _host: ["8.8.8.8"]
SOURCES = (h.UNTC, h.CELLAR, h.COE)
EXCLUDED_WORDS = ("is legally bound", "obligation to", "complies with", "in breach", "legal advice:",
                  "the reservation is invalid", "the reservation is permissible")


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
        coordinator.select("legal", bundles["legal"]["version"],
                           features=["treaties-untc", "treaties-eu", "treaties-coe"])
        coordinator.activate("treaties-acceptance")
        manifest = validate_source_pack(json.loads(h.PACK.read_text()))
        SourcePackStore(self.conn).install(manifest, principal_id="operator", enable=True, now_ms=1)
        runtime = self.runtime()
        for source_id in SOURCES:
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
            dns_resolver=PUBLIC_DNS)


def adapter(source_id: str, *, v2: bool = False) -> TreatiesAdapter:
    return TreatiesAdapter(h.item_for(source_id), transport=fixture_transport(h.native_pages(source_id, v2=v2)))


def without_notices(value):
    """The answer without its boundary notices, which name the exclusions they refuse."""
    if isinstance(value, dict):
        return {k: without_notices(v) for k, v in value.items()
                if k not in {"notice", "note", "exclusions", "record_state_basis", "statement_notice", "coverage"}}
    if isinstance(value, list):
        return [without_notices(v) for v in value]
    return value


def test_treaty_and_state_to_cited_actions_with_revisions_identity_and_links():
    env = Env()
    conn = env.conn
    assert all(feature_enabled(conn, f) for f in ("treaties-untc", "treaties-eu", "treaties-coe"))

    # The pinned pack: CELLAR and the Council of Europe acquire; the declined UNTC entry acquires nothing.
    first = env.run(SOURCES, "treaties-1")
    assert first["status"] == "complete"
    state = readiness(conn)
    assert state["providers"]["untc"]["revisions_acquired"] == 0
    assert state["providers"]["untc"]["licence_decision"]["status"] == "declined"
    queries = TreatyQueries(conn, now=env.now)
    declined = queries.status_as_of(h.NS, "cets:990", "France", "2099-06-01", scopes=h.READ_ONLY)
    assert declined["not_acquired"] == [{"provider": "untc",
                                         "reason": "declined licence decision (written permission required)"}]
    before = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
              for t in ("treaty_revisions", "treaty_action_revisions")}
    assert env.run(SOURCES, "treaties-replay")["status"] == "complete"
    assert {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in before} == before

    # An operator holding written permission (test-only decision) replays the authored UNTC status page.
    assert env.run([h.UNTC], "treaties-untc-permitted", {h.UNTC: adapter(h.UNTC)})["status"] == "complete"

    # Monitors start before the later depositary revisions arrive.
    subscriptions = SubscriptionStore(conn)
    subscriptions.commit_watermark(h.NS, 1)
    monitor = TreatiesMonitor(conn, now=env.now)
    treaty_sub = monitor.create(h.NS, "untc", watch="treaty", key="untc:XXIX-99", principal_id="alice",
                                scopes=h.SCOPES)["subscription_id"]
    state_sub = monitor.create(h.NS, "germany", watch="participant", key="Germany", principal_id="alice",
                               scopes=h.SCOPES)["subscription_id"]
    monitor.run(treaty_sub, 1, principal_id="alice", scopes=h.SCOPES)
    monitor.run(state_sub, 1, principal_id="alice", scopes=h.SCOPES)
    later = env.run(SOURCES, "treaties-2", {s: adapter(s, v2=True) for s in SOURCES})
    assert later["status"] == "complete"
    subscriptions.commit_watermark(h.NS, 2)
    treaty_notices = {n["kind"] for n in monitor.run(treaty_sub, 2, principal_id="alice",
                                                     scopes=h.SCOPES)["notifications"]}
    assert {"depositary_correction", "action_removed_by_source", "new_action"} <= treaty_notices
    state_notices = monitor.run(state_sub, 2, principal_id="alice", scopes=h.SCOPES)["notifications"]
    assert any(n["summary"]["action_type"] == "denunciation" for n in state_notices)

    # Treaty and state to the cited action chain as of a date, with the revision history.
    germany = queries.status_as_of(h.NS, "cets:990", "Germany", "2100-03-01", scopes=h.READ_ONLY)
    (coe,) = germany["sources"]
    assert coe["record_state"] == "denunciation-or-withdrawal-deposited-effective-later"
    assert coe["pending"][0]["citation"]["depositary_date"] == "2100-04-15"
    assert all(item["citation"]["revision_id"] and item["citation"]["retrieved_at_ms"] for item in coe["chain"])
    corrected = queries.status_as_of(h.NS, "untc:XXIX-99", "Germany", "2099-01-01", scopes=h.READ_ONLY)
    ratification = next(c for c in corrected["sources"][0]["chain"] if c["action_type"] == "ratification")
    assert ratification["action_date"] == "2098-09-04" and ratification["citation"]["change"] == "revised"
    history = queries.history(h.NS, "untc:XXIX-99", scopes=h.READ_ONLY)
    changes = {r["record_key"]: [x["citation"]["change"] for x in r["revisions"]] for r in history["records"]}
    assert changes["treaties:action:untc:XXIX-99:germany:ratification:table"] == ["new", "revised"]
    assert changes["treaties:action:untc:XXIX-99:examplonia:signature:table"] == ["new", "removed-by-source"]

    # Reservations and objections verbatim, linked as the source links them.
    statements = queries.treaty_statements(h.NS, "untc:XXIX-99", scopes=h.READ_ONLY,
                                           kinds=["reservation", "objection"])
    objection = next(s for s in statements["statements"] if s["action_type"] == "objection")
    assert objection["objected"]["objected_statement"]["text_verbatim"].startswith("Reservation: Examplestan")

    # Reviewable identity: nothing is used until a reviewer accepts it.
    places = h.seed_places(conn)
    assert queries.status_as_of(h.NS, "cets:990", "iso3166:DE", "2100-03-01",
                                scopes=h.READ_ONLY)["status"] == "no_action_on_record"
    identity = TreatiesIdentity(conn, now=env.now)
    proposed = identity.propose(h.NS, principal_id="alice", scopes=h.SCOPES)
    assert all(c["state"] == "proposed" for c in proposed["candidates"])
    assert "treaties:participant:untc:examplestan" in {u["subject"] for u in proposed["unmatched"]}
    for candidate in proposed["candidates"]:
        if candidate["subject"].endswith(":germany") or candidate["kind"] == "treaty":
            identity.review(h.NS, candidate["candidate_id"], "accept", "published code / citation checked",
                            principal_id="bob", scopes=h.REVIEW_SCOPES)
    by_code = queries.status_as_of(h.NS, "celex:22099A0101(01)", "iso3166:DE", "2100-03-01", scopes=h.READ_ONLY)
    assert {s["treaty"]["treaty_key"] for s in by_code["sources"]} == {h.CELLAR_TREATY, h.COE_TREATY}
    assert by_code["status"] == "answered"
    actions = queries.participant_actions(h.NS, "iso3166:DE", scopes=h.READ_ONLY, date_from="2098-01-01",
                                          date_to="2100-12-31")
    assert {a["provider"] for a in actions["actions"]} == {"untc", "coe-treaty-office"}

    # Cross-pack links by citation and accepted match; missing targets are reported.
    h.seed_legal_act(conn)
    h.seed_sanctions_bases(conn)
    h.seed_trade_area(conn, places["DE"])
    links = TreatiesLinks(conn, now=env.now).link_all(h.NS, principal_id="alice", scopes=h.SCOPES)
    kinds = {(x["link_kind"], x["status"]) for x in links["links"]}
    assert {("eu-act", "linked"), ("eu-act", "missing_target"), ("sanctions-legal-basis", "linked"),
            ("sanctions-legal-basis", "missing_target"), ("participant-trade-reporter", "linked")} <= kinds

    # A subject with no records.
    nobody = queries.participant_actions(h.NS, "Atlantis", scopes=h.READ_ONLY)
    assert nobody["status"] == "no_action_on_record"
    assert queries.status_as_of(h.NS, "cets:1", "France", "2100-01-01",
                                scopes=h.READ_ONLY)["status"] == "no_treaty_on_record"

    # The evidence bundle cites every item with source, record revision and as-of time.
    bundle = queries.export_bundle(germany, created_at_ms=1)
    assert verify_bundle(bundle).errors == []
    evidence = [o["payload"] for o in bundle["objects"] if o["type"] == "evidence"]
    assert len(evidence) == 1 + len(coe["chain"]) + len(coe["pending"])
    assert all(e["source"]["source_id"] and e["record_revision"]["revision_id"] and e["as_of"]["retrieved_at_ms"]
               for e in evidence)

    # Exclusions and the minimisation decision.
    for result in (germany, corrected, statements, by_code, actions, nobody):
        assert forbidden_keys(result) == [] and minimisation_violations(result) == []
        text = json.dumps(without_notices(result)).casefold()
        assert not any(word in text for word in EXCLUDED_WORDS)
        assert "no legal advice" in result["exclusions"]
    stored = [json.loads(r[0]) for r in conn.execute("SELECT record_json FROM treaty_revisions").fetchall()]
    assert {r["fields"]["text_policy"] for r in stored} == {"linked, not stored"}
    assert all("treaty_text" not in r["fields"] for r in stored)
