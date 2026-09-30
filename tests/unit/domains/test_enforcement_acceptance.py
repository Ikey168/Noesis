"""Offline company-to-enforcement-actions acceptance for the Legal enforcement provider (#2651, EN13).

The pinned ``legal-research`` 1.5.0 fixtures for the five ``enforcement``
sources (SEC litigation release and administrative proceeding, FCA final
notices, EPA ECHO case, EDPB Article 60 register entries) replay through the
real source-pack runtime with sockets blocked and the four enforcement
features selected, beside the Corporate Ownership fixtures (the fictional
Exampla group). Reviewable identity, cross-pack links, as-of answers,
revision history, a subscription monitor, a subject with no records and the
evidence bundle then run over them. Every action, respondent and figure is
fictional. This is offline evidence only; it is not live coverage (EN14,
#2720).
"""

from __future__ import annotations

import json
import socket

import pytest

from src.domains import registry as domain_registry
from src.evidence_bundle.verifier import verify_bundle
from src.ingestion.enforcement_sources import (
    LIVE_VERIFICATION,
    EnforcementAdapter,
    fixture_transport,
)
from src.ingestion.source_pack_runtime import SourcePackRuntime
from src.ingestion.source_packs import SourcePackStore, validate_source_pack
from src.kb.enforcement import EnforcementStore, feature_enabled, forbidden_keys
from src.kb.enforcement_identity import EnforcementIdentity
from src.kb.enforcement_links import EnforcementLinks
from src.kb.enforcement_monitoring import EnforcementMonitor
from src.kb.enforcement_queries import (
    action_history,
    actions_by_authority,
    actions_for_entity,
    export_bundle,
)
from src.kb.ownership_identity import OwnershipIdentityService
from src.kb.subscriptions import SubscriptionStore
from tests.unit import enforcement_harness as h
from tests.unit.composition.test_migration import _migrated

PACK_ID = "legal-research"
PUBLIC_DNS = lambda _host: ["8.8.8.8"]
EXCLUDED_WORDS = ("risk score", "compliance score", "guilty", "found liable", "wrongdoing was", "profile of")


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
                           features=["enforcement-sec", "enforcement-fca", "enforcement-epa", "enforcement-edpb"])
        coordinator.activate("enforcement-acceptance")
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

    def run(self, key, adapters=None) -> dict:
        runtime = self.runtime()
        fixtures = runtime.fixture_adapters(PACK_ID, h.ROOT)
        return runtime.run(
            {"pack_id": PACK_ID, "run_key": key, "operation": "records", "source_ids": list(h.SOURCES),
             "max_results": 5000, "max_bytes": 50_000_000, "timeout_ms": 120_000},
            principal_id="operator", adapters={s: (adapters or {}).get(s) or fixtures[s] for s in h.SOURCES},
            dns_resolver=PUBLIC_DNS, secret_resolver=lambda _ref: None)


def without_notices(value):
    """The answer without its boundary notices, which name the exclusions they refuse."""
    if isinstance(value, dict):
        return {k: without_notices(v) for k, v in value.items()
                if k not in {"notice", "note", "reason", "message", "totals_note", "coverage"}}
    if isinstance(value, list):
        return [without_notices(v) for v in value]
    return value


def v2_adapter(source_id: str) -> EnforcementAdapter:
    return EnforcementAdapter(h.source(source_id), transport=fixture_transport(h.v2_pages(source_id)))


def test_company_to_cited_enforcement_actions_with_outcome_and_appeal_history():
    env = Env()
    conn = env.conn
    assert all(feature_enabled(conn, f) for f in ("enforcement-sec", "enforcement-fca", "enforcement-epa",
                                                  "enforcement-edpb"))
    first = env.run("enforcement-1")
    assert first["status"] == "complete"
    assert {s["source_id"]: s["counts"]["quarantined"] for s in first["sources"]} == dict.fromkeys(h.SOURCES, 0)
    store = EnforcementStore(conn)
    assert len(store.views(h.NS, ("enforcement_action",))) == 7
    # The individual-only FCA notice is withheld; no natural person's name or reference number is stored.
    assert store.by_key(h.NS, h.FCA_PERSON) is None
    stored = json.dumps([v["record"] for v in store.views(h.NS)])
    assert "Jordan" not in stored and "JXE01001" not in stored and "natural person 2" in stored
    # A replay adds nothing.
    replay = env.run("enforcement-1b")
    assert replay["status"] == "complete"
    assert [v["revision"] for v in store.views(h.NS)] == [1] * len(json.loads(stored))

    # Reviewable identity: identifiers first, names low evidence, nothing accepted before review.
    h.ownership(conn)
    identity = EnforcementIdentity(conn)
    proposed = identity.propose(h.NS, ownership_namespace=h.OWN_NS, principal_id="analyst", scopes=h.SCOPES)
    assert {c["state"] for c in proposed["candidates"]} == {"proposed"}
    assert actions_for_entity(conn, h.NS, h.HOLD_ENTITY, ownership_namespace=h.OWN_NS,
                              scopes=h.SCOPES)["status"] == "no_action_on_record"
    clusters = OwnershipIdentityService(conn).clusters(h.OWN_NS)
    good = {clusters.get(h.HOLD_ENTITY), clusters.get(h.UK_ENTITY), clusters.get(h.INT_ENTITY), h.SEC_CIK_ENTITY}
    for view in proposed["candidates"]:
        ok = clusters.get(view["ownership_key"], view["ownership_key"]) in good
        identity.review(h.NS, view["candidate_id"], "accept" if ok else "reject", "identifiers and names checked",
                        principal_id="reviewer", scopes=h.REVIEW_SCOPES)
    unmatched = [u["name_as_published"] for u in identity.unmatched(h.NS, scopes=h.SCOPES)]
    assert unmatched == ["Northwind Payments Limited"]

    # Cross-pack links by citation, published identifier and accepted match; missing targets reported.
    works = h.seed_legal(conn)
    links = EnforcementLinks(conn).link(h.NS, ownership_namespace=h.OWN_NS, scopes=h.SCOPES)["links"]
    assert {link["target_key"] for link in links if link["status"] == "resolved"} >= set(works.values())
    assert {link["status"] for link in links if link["target_kind"] == "court_docket"} == {"provider_unavailable"}

    # The company and its group as of dates, with outcome and appeal history, every revision cited.
    answer = actions_for_entity(conn, h.NS, h.HOLD_ENTITY, ownership_namespace=h.OWN_NS, scopes=h.SCOPES,
                                group=True, as_of="2099-12-31")
    assert sorted(r["action_key"] for r in answer["actions"]) == sorted(
        [h.SEC_LR, h.SEC_AP, h.FCA_EXAMPLA, h.EPA, h.EDPB_IE])
    sec = next(r for r in answer["actions"] if r["action_key"] == h.SEC_LR)
    assert sec["outcome"]["admission_wording"] == "Without admitting or denying the allegations in the complaint"
    assert all(r["cite"]["revision_id"] and r["matched_through"]["decision_id"] for r in answer["actions"])
    early = actions_for_entity(conn, h.NS, h.HOLD_ENTITY, ownership_namespace=h.OWN_NS, scopes=h.SCOPES,
                               group=True, as_of="2099-03-20")
    assert {e["action_key"] for e in early["excluded"]} == {h.SEC_AP, h.EDPB_IE}
    fca = actions_by_authority(conn, h.NS, scopes=h.SCOPES, authority="uk-fca")
    northwind = next(r for r in fca["actions"] if r["action_key"] == h.FCA_NORTHWIND)
    assert northwind["appeals"][0]["reference"] == "FS/2099/0007"
    for value in (answer, early, fca):
        assert forbidden_keys(value) == []
        text = json.dumps(without_notices(value)).lower()
        assert not any(word in text for word in EXCLUDED_WORDS)
    assert "sum" not in fca["penalties_by_authority_and_currency"][0]

    # A subscription sees the publishers' later revisions: a judgment, a correction and a removal.
    monitor = EnforcementMonitor(conn, now=env.now)
    subs = SubscriptionStore(conn)
    subs.commit_watermark(h.NS, 1)
    sub = monitor.create(h.NS, "group", watch="entity", key=h.HOLD_ENTITY, ownership_namespace=h.OWN_NS,
                         group=True, principal_id="alice", scopes=h.SCOPES)["subscription_id"]
    authority = monitor.create(h.NS, "nl", watch="authority", key="eu-sa-nl", principal_id="alice",
                               scopes=h.SCOPES)["subscription_id"]
    monitor.run(sub, 1, principal_id="alice", scopes=h.SCOPES)
    monitor.run(authority, 1, principal_id="alice", scopes=h.SCOPES)
    second = env.run("enforcement-2", adapters={s: v2_adapter(s) for s in h.SOURCES})
    assert second["status"] == "complete"
    subs.commit_watermark(h.NS, 2)
    notices = monitor.run(sub, 2, principal_id="alice", scopes=h.SCOPES)["notifications"]
    assert {"action_revised", "action_corrected"} <= {n["kind"] for n in notices}
    assert all(n["cites"]["previous"] is None or n["cites"]["previous"]["revision_id"] for n in notices)
    removed = monitor.run(authority, 2, principal_id="alice", scopes=h.SCOPES)["notifications"]
    assert [n["kind"] for n in removed if n["action_key"] == h.EDPB_NL] == ["action_removed_by_source"]

    # Revision history and as-of by record time.
    history = action_history(conn, h.NS, h.FCA_EXAMPLA, scopes=h.SCOPES)
    statuses = [r["publication_status"] for r in history["revisions"][h.FCA_EXAMPLA]]
    assert statuses == ["published", "corrected"]
    first_time = history["revisions"][h.FCA_EXAMPLA][0]["observed_at_ms"]
    pinned = action_history(conn, h.NS, h.FCA_EXAMPLA, scopes=h.SCOPES, known_at_ms=first_time)
    assert pinned["action"]["publication_status"] == "published"

    # A subject with no records, and the evidence bundle.
    none = actions_for_entity(conn, h.NS, "gleif:lei:213800EXAMPLATRADE88", ownership_namespace=h.OWN_NS,
                              scopes=h.SCOPES)
    assert none["status"] == "no_action_on_record" and "not a statement" in none["message"]
    bundle = export_bundle(answer)
    assert verify_bundle(bundle).valid
    assert all(LIVE_VERIFICATION[p]["status"] == "unverified-live" for p in LIVE_VERIFICATION)
