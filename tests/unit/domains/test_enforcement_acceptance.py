"""Offline company-to-enforcement-actions acceptance for the Legal enforcement features (#2651, EN13).

The pinned ``legal-research`` 1.5.0 fixtures for the four ``enforcement``
sources (SEC releases, FCA final notices, EPA ECHO cases, EDPB Article 60
decisions) replay through the real source-pack runtime (fixture adapters
compiled from the installed pack) with sockets blocked and the four enforcement
features selected, beside the Corporate Ownership fixtures. A company reaches
cited enforcement actions across regulators with outcome and appeal history,
revision history, as-of answers, reviewable respondent identity, cross-pack
links and a subject with no records; the exclusions and the EN01 minimisation
decision are asserted on every answer. Every action, respondent and figure is
fictional. This is offline evidence only; it is not live coverage (EN14, #2720).
"""

from __future__ import annotations

import json
import socket

import pytest

from src.domains import registry as domain_registry
from src.ingestion.enforcement_sources import (
    LIVE_VERIFICATION,
    EnforcementAdapter,
    fixture_transport,
)
from src.ingestion.source_pack_runtime import SourcePackRuntime
from src.ingestion.source_packs import SourcePackStore, validate_source_pack
from src.kb.enforcement import (
    FEATURES,
    EnforcementStore,
    feature_enabled,
    forbidden_keys,
    readiness,
)
from src.kb.enforcement_identity import EnforcementIdentity
from src.kb.enforcement_links import EnforcementLinks
from src.kb.enforcement_monitoring import EnforcementMonitor
from src.kb.enforcement_queries import (
    action_history,
    actions_by_authority,
    actions_for_entity,
    evidence_bundle,
)
from src.kb.ownership_identity import OwnershipIdentityService
from src.kb.subscriptions import SubscriptionStore
from tests.unit import enforcement_harness as h
from tests.unit.composition.test_migration import _migrated

PACK_ID = "legal-research"
PUBLIC_DNS = lambda _host: ["8.8.8.8"]
INDIVIDUALS = ("Jordan Placeholder", "Casey Placeholder", "Alex Placeholder", "Placeholder")
EXCLUDED_WORDS = ("risk score", "compliance score", "guilty", "found liable", "wrongdoing was found")


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
        coordinator.select("legal", bundles["legal"]["version"], features=list(FEATURES))
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

    def run(self, source_ids, key, adapters=None) -> dict:
        runtime = self.runtime()
        fixtures = runtime.fixture_adapters(PACK_ID, h.ROOT)
        return runtime.run(
            {"pack_id": PACK_ID, "run_key": key, "operation": "records", "source_ids": list(source_ids),
             "max_results": 5000, "max_bytes": 50_000_000, "timeout_ms": 120_000},
            principal_id="operator", adapters={s: (adapters or {}).get(s) or fixtures[s] for s in source_ids},
            dns_resolver=PUBLIC_DNS, secret_resolver=lambda _ref: None)


def v2_adapter(source_id: str) -> EnforcementAdapter:
    return EnforcementAdapter(h.source(source_id), transport=fixture_transport(
        h.native_pages(source_id, h.V2.get(source_id), gone=True)))


def without_notices(value):
    """The answer without its boundary notices, which name the exclusions they refuse."""
    if isinstance(value, dict):
        return {k: without_notices(v) for k, v in value.items()
                if k not in {"notice", "note", "coverage", "message", "group_basis", "exclusions"}}
    if isinstance(value, list):
        return [without_notices(v) for v in value]
    return value


def assert_excluded(answer) -> None:
    assert forbidden_keys(answer) == []
    text = json.dumps(without_notices(answer)).lower()
    for word in EXCLUDED_WORDS:
        assert word not in text, word
    raw = json.dumps(answer)
    for name in INDIVIDUALS:
        assert name not in raw, name


def test_company_to_cited_enforcement_actions_with_outcome_and_appeal_history():
    env = Env()
    conn = env.conn
    assert all(feature_enabled(conn, f) for f in FEATURES)
    assert all(p["live"]["status"] == "unverified-live" for p in readiness(conn)["providers"].values())
    assert all(v["status"] == "unverified-live" for v in LIVE_VERIFICATION.values())

    first = env.run(h.SOURCES, "enforcement-1")
    assert first["status"] == "complete"
    before = conn.execute("SELECT count(*) FROM enforcement_record_revisions").fetchone()[0]
    assert env.run(h.SOURCES, "enforcement-replay")["status"] == "complete"
    assert conn.execute("SELECT count(*) FROM enforcement_record_revisions").fetchone()[0] == before  # idempotent

    # Minimisation: no individual is stored anywhere, only counted.
    stored = json.dumps(conn.execute("SELECT payload_json FROM enforcement_record_revisions").fetchall())
    for name in INDIVIDUALS:
        assert name not in stored
    counts = {json.loads(row[0]).get("natural_person_respondents") for row in conn.execute(
        "SELECT payload_json FROM enforcement_record_revisions").fetchall()}
    assert 1 in counts

    # Ownership fixtures, reviewed identity: identifiers first, names as low evidence, nothing automatic.
    h.ownership(conn)
    from tests.unit import competition_harness as ch

    ch.apply(conn, "ec-competition-cases")
    identity = EnforcementIdentity(conn)
    proposed = identity.propose(h.NS, ownership_namespace=h.OWN_NS, principal_id="analyst", scopes=h.SCOPES)
    assert proposed["candidates"] and all(c["state"] == "proposed" for c in proposed["candidates"])
    clusters = OwnershipIdentityService(conn).clusters(h.OWN_NS)
    good = {clusters.get(k, k) for k in (h.HOLD_ENTITY, h.INT_ENTITY, h.UK_ENTITY, h.TRADE_ENTITY)}
    for view in proposed["candidates"]:
        ok = clusters.get(view["ownership_key"], view["ownership_key"]) in good or view["ownership_key"] == h.SEC_FILER
        identity.review(h.NS, view["candidate_id"], "accept" if ok else "reject", "register identifiers checked",
                        principal_id="reviewer", scopes=h.REVIEW_SCOPES)
    unmatched = {u["name_as_published"] for u in identity.unmatched(h.NS, scopes=h.SCOPES)}
    assert {"Northwind Securities LLC", "Northwind Brokers Limited", "Northwind Chemicals Inc."} == unmatched

    # Cross-pack links: Legal works and the competition case by citation, the SEC filer by CIK, accepted matches;
    # the courts provider is absent and reported, not dropped.
    works = h.seed_legal(conn)
    links = EnforcementLinks(conn).link(h.NS, scopes=h.SCOPES)
    resolved = {(link["target_pack"], link["target_record"]) for link in links["links"] if link["status"] == "resolved"}
    assert {("legal.works", works["usc15"]), ("legal.works", works["gdpr"]),
            ("ownership.competition", "competition:case:ec:M.99001"), ("market.filings", h.SEC_FILER)} <= resolved
    assert links["providers_unavailable"] == ["legal.courts"]

    # Monitors start before the later publisher revisions arrive.
    subs = SubscriptionStore(conn)
    subs.commit_watermark(h.NS, 1)
    monitor = EnforcementMonitor(conn, now=env.now)
    company = monitor.create(h.NS, "company", watch="entity", key=h.HOLD_ENTITY, ownership_namespace=h.OWN_NS,
                             group=True, principal_id="alice", scopes=h.SCOPES)["subscription_id"]
    assert "new_action" in {n["kind"] for n in monitor.run(company, 1, principal_id="alice",
                                                            scopes=h.SCOPES)["notifications"]}

    # The company and its group as of 1 June 2025: only the FCA notice was decided by then.
    early = actions_for_entity(conn, h.NS, h.HOLD_ENTITY, ownership_namespace=h.OWN_NS, scopes=h.SCOPES,
                               group=True, as_of="2025-06-01")
    assert [r["action_key"] for r in early["actions"]] == [h.FCA_EX]
    assert_excluded(early)

    # Later revisions: an amended FCA notice, a settled EPA case, a withdrawn EDPB entry.
    later = ("fca-final-notices", "epa-echo-enforcement-cases", "edpb-art60-final-decisions")
    assert env.run(later, "enforcement-2", adapters={s: v2_adapter(s) for s in later})["status"] == "complete"
    subs.commit_watermark(h.NS, 2)
    changes = monitor.run(company, 2, principal_id="alice", scopes=h.SCOPES)["notifications"]
    corrected = next(n for n in changes if n["kind"] == "decision_corrected")
    assert corrected["cites"]["previous"]["revision"] == 1 and corrected["changes"]["amended_on"]["after"] == \
        "2025-05-02"

    # Now, across regulators, with outcomes, settlement wording, penalties, appeals and cited revisions.
    answer = actions_for_entity(conn, h.NS, h.HOLD_ENTITY, ownership_namespace=h.OWN_NS, scopes=h.SCOPES,
                                group=True)
    assert set(answer["by_authority"]) == {"us-sec", "uk-fca", "eu-dpa-nl"}
    sec = next(r for r in answer["actions"] if r["action_key"] == h.SEC_LR)
    assert sec["settlement_as_published"]["admission_as_published"] == \
        "Without admitting or denying the allegations in the complaint"
    assert sec["matched_through"]["method"] in {"exact-identifier", "name-jurisdiction"}
    assert all(r["cites"]["action_revision_id"] and r["cites"]["identity_decision"] for r in answer["actions"])
    assert_excluded(answer)
    bundle = evidence_bundle(answer)
    assert bundle["bibliography"] and all(b["record_revision"]["revision_id"] and b["as_of"]["record_time_ms"]
                                          for b in bundle["bibliography"])

    # Revision history of the amended notice and the settled EPA case (Exampla Trading).
    history = action_history(conn, h.NS, h.FCA_EX, scopes=h.SCOPES)
    assert [r["source_status"] for r in history["action_revisions"]] == ["published", "corrected"]
    trading = actions_for_entity(conn, h.NS, h.TRADE_ENTITY, ownership_namespace=h.OWN_NS, scopes=h.SCOPES)
    (epa,) = trading["actions"]
    assert epa["status_as_published"] == "Closed" and epa["natural_person_respondents"] == 1
    assert {p["penalty_type"]: p["amount_as_published"] for p in epa["penalties"]}["supplemental_environmental_project"] \
        == "$60,000"
    at_filing = actions_for_entity(conn, h.NS, h.TRADE_ENTITY, ownership_namespace=h.OWN_NS, scopes=h.SCOPES,
                                   as_of="2025-06-01")
    assert at_filing["actions"][0]["outcome_at_date"] == "no published decision by the date (decided later)"
    assert at_filing["actions"][0]["outcome_as_published"] is None

    # Appeal history by authority, penalties never summed, the withdrawn entry kept as a revision.
    fca = actions_by_authority(conn, h.NS, scopes=h.SCOPES, authority="uk-fca")
    appeal = next(r for r in fca["actions"] if r["action_key"] == h.FCA_NW)["appeals"][0]
    assert (appeal["reference"], appeal["decided_on"]) == ("FS/2023/0017", "2024-09-30")
    assert "sum" not in json.dumps(fca["penalty_figures"]["by_authority_and_currency"])
    removed = actions_by_authority(conn, h.NS, scopes=h.SCOPES, authority="eu-dpa-ie")
    assert removed["actions"] == [] and removed["removed_by_source"][0]["action_key"] == h.EDPB_ANON
    assert_excluded(fca)

    # A subject with no records is not a clean bill.
    nothing = actions_for_entity(conn, h.NS, h.UK_ENTITY, ownership_namespace=h.OWN_NS, scopes=h.SCOPES,
                                 as_of="2024-01-01")
    assert nothing["status"] == "no_action_on_record" and "not a clean bill" in nothing["message"]

    # Everything above ran on fixture evidence; nothing is reported as live.
    assert {v["record"]["source"]["evidence_origin"] for v in EnforcementStore(conn).views(h.NS)} == {"fixture"}
