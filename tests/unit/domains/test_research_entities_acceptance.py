"""Offline organisation-to-research-records acceptance for the Science research-entities features (RE13, #2644).

The pinned ``research-discovery`` 1.5.0 fixtures for the ROR, ORCID, DataCite and CORDIS sources replay through the real
source-pack runtime (fixture adapters compiled from the installed pack) with sockets blocked and the four
``research-entities-*`` features selected. An organisation reaches its cited ROR records across two releases (a
withdrawal with its successor), its CORDIS participation through reviewed identity with contributions per currency,
its datasets and its Corporate Ownership match; a researcher reaches the ORCID-asserted works and employments of the
record version in force at a date, linked to Scholarly literature papers by DOI; a paper reaches its datasets; a
subject with no records is ``none_on_record``. Every organisation, researcher, dataset and project is fictional; the
personal fields the RE01 minimisation decision excludes carry placeholders only in the native responses, which the
parser discards. Nothing here is live evidence.
"""

from __future__ import annotations

import json
import socket

import pytest

from src.domains import registry as domain_registry
from src.ingestion.research_entities_sources import EXCLUSIONS, FIXTURE_SECRET
from src.ingestion.source_pack_runtime import SourcePackRuntime
from src.ingestion.source_packs import SourcePackStore, validate_source_pack
from src.kb.research_entities_identity import ResearchEntityIdentity
from src.kb.research_entities_links import ResearchEntityLinks
from src.kb.research_entities_monitoring import ResearchEntityMonitor
from src.kb.research_entities_queries import ASSERTED, ResearchEntityQueries
from src.kb.research_entities_records import (
    ResearchEntityError,
    ResearchEntityStore,
    feature_enabled,
    forbidden_keys,
    readiness,
)
from src.kb.subscriptions import SubscriptionStore
from tests.unit import research_entities_harness as h
from tests.unit.composition.test_migration import _migrated

PACK_ID = "research-discovery"
FEATURES = ["research-entities-ror", "research-entities-orcid", "research-entities-datacite",
            "research-entities-cordis"]
PUBLIC_DNS = lambda _host: ["8.8.8.8"]
EXCLUDED_WORDS = ("h-index", "h_index", "ranking", "citation count", "productivity score", "co-author affiliation")


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
        coordinator.select("science", bundles["science"]["version"], features=FEATURES)
        coordinator.activate("research-entities-acceptance")
        manifest = validate_source_pack(json.loads(h.PACK.read_text()))
        SourcePackStore(self.conn).install(manifest, principal_id="operator", enable=True, now_ms=1)
        runtime = self.runtime()
        for item in manifest["sources"]:
            runtime.accept_license(PACK_ID, item["source_id"], principal_id="operator")

    def now(self) -> int:
        self.clock += 1
        return self.clock

    def runtime(self) -> SourcePackRuntime:
        return SourcePackRuntime(self.conn, now=self.now, sleep=lambda _d: None)

    def run(self, source_ids, key, adapters=None) -> dict:
        runtime = self.runtime()
        fixtures = runtime.fixture_adapters(PACK_ID, h.ROOT)
        return runtime.run(
            {"pack_id": PACK_ID, "run_key": key, "operation": "selection", "source_ids": list(source_ids),
             "max_results": 5000, "max_bytes": 100_000_000, "timeout_ms": 120_000},
            principal_id="operator", adapters={s: (adapters or {}).get(s) or fixtures[s] for s in source_ids},
            dns_resolver=PUBLIC_DNS, secret_resolver=lambda _ref: FIXTURE_SECRET)


def personal_data_anywhere(conn) -> list[str]:
    """Every text column of every table that still holds a placeholder personal value (RE01)."""
    found = []
    columns = conn.execute("SELECT table_name, column_name FROM information_schema.columns WHERE data_type IN "
                           "('VARCHAR', 'JSON')").fetchall()
    for table, column in columns:
        for needle in h.PERSONAL:
            hit = conn.execute(f'SELECT count(*) FROM "{table}" WHERE "{column}" LIKE ?', [f"%{needle}%"]).fetchone()
            if hit[0]:
                found.append(f"{table}.{column}: {needle}")
    return found


def test_organisation_and_researcher_to_cited_registry_records_asserted_works_datasets_and_projects():
    env = Env()
    assert all(feature_enabled(env.conn, feature) for feature in FEATURES)
    first = env.run(h.SOURCES, "research-entities")
    assert first["status"] == "complete", first
    store = ResearchEntityStore(env.conn)
    assert len(store.records(h.NS, scopes=h.SCOPES)) == 12  # 5 organisations, 3 researchers, 2 datasets, 2 projects
    receipts = store.receipts(h.NS, scopes=h.SCOPES)
    assert {r["source_id"] for r in receipts} == set(h.SOURCES)
    assert all(r["receipt"]["evidence_origin"] == "fixture" for r in receipts)
    assert personal_data_anywhere(env.conn) == []  # RE01: nothing excluded reached documents, records or receipts
    report = readiness(env.conn)
    assert {p["live_verification"] for p in report["providers"].values()} == {"unverified-live",
                                                                              "documented-not-acquired"}
    h.load_ownership(env.conn)
    h.seed_papers(env.conn)
    h.seed_funding(env.conn)

    # --- revision history: two ROR releases, withdrawal kept with its successor ------------------------------
    history = store.history(h.NS, "research-entities:ror:0zznwd303", scopes=h.SCOPES)
    assert [(v["native_revision"], v["status"]) for v in history] == [("release:v9.1", "active"),
                                                                      ("release:v9.2", "withdrawn")]

    # --- reviewable identity: identifiers proposed, reviewed; names stay low evidence --------------------------
    identity = ResearchEntityIdentity(env.conn, now=env.now)
    proposed = identity.propose(h.NS, principal_id="alice", scopes=h.SCOPES, ownership_namespace=h.OWN_NS)
    assert {c["state"] for c in proposed["candidates"]} == {"proposed"}
    assert not [c for c in proposed["candidates"] if "orcid" in c["subject_key"] + c["target_key"]]
    for candidate in proposed["candidates"]:
        decision = "reject" if candidate["low_evidence"] else "accept"
        identity.review(h.NS, candidate["candidate_id"], decision, "fixture review", principal_id="reviewer",
                        scopes=h.REVIEW_SCOPES)
    assert "research-entities:ror:0zzexa505" in {u["key"] for u in identity.unmatched(h.NS, scopes=h.SCOPES)}

    # --- cross-pack links by citation, shared identifier and accepted match ----------------------------------
    linked = ResearchEntityLinks(env.conn, now=env.now).link(h.NS, principal_id="alice", scopes=h.SCOPES,
                                                             ownership_namespace=h.OWN_NS)
    assert linked["providers"] == {"literature": "present", "funding": "present", "ownership": "present",
                                   "researchers": "linked"}
    kinds = {link["kind"] for link in linked["links"] if link["status"] == "resolved"}
    assert {"researcher-asserted-work", "dataset-related-work", "dataset-funded-by-project", "project-funding-record",
            "organisation-ownership-entity", "participant-organisation"} <= kinds
    assert any(link["status"] == "target_missing" for link in linked["links"])  # reported, not dropped

    ask = ResearchEntityQueries(env.conn)

    # --- organisation -> lineage per release, projects, datasets, ownership, each version cited --------------
    april = ask.organisation(h.NS, h.NORTHWIND_POLY, scopes=h.SCOPES, as_of="2099-04-01")
    july = ask.organisation(h.NS, h.NORTHWIND_POLY, scopes=h.SCOPES, as_of="2099-07-01")
    assert april["record_status"] == "active" and july["record_status"] == "withdrawn"
    assert july["lineage"]["successors"][0]["ror_id"] == h.NORTHWIND_TECH
    exampla = ask.organisation(h.NS, h.EXAMPLA, scopes=h.SCOPES, as_of="2099-07-01")
    assert [(p["project_id"], p["participant"]["role"]) for p in exampla["projects"]] == [
        (h.EXAMPLAR, "coordinator"), (h.NORTHWAVE, "participant")]
    assert exampla["contribution_totals"]["currencies"] == ["EUR"]
    assert [d["doi"] for d in exampla["datasets"]] == [h.DS1, h.DS2]  # DS2 names NORTHWAVE, where Exampla participates
    assert exampla["ownership"][0]["target_key"] == "lei:5299EXAMPLAUNIV00001"
    bundle = ask.evidence_bundle(exampla)
    assert {exampla["citation"]["revision_id"]} | {p["citation"]["revision_id"] for p in exampla["projects"]} <= {
        b["id"] for b in bundle["bibliography"]}

    # --- researcher -> ORCID-asserted works and employments of the version in force ----------------------------
    researcher = ask.researcher(h.NS, h.ADA, scopes=h.SCOPES, as_of="2099-03-01")
    assert researcher["record_version"]["last_modified"] == "2099-02-14T09:00:00+00:00"
    assert {w["assertion"] for w in researcher["works"]} == {ASSERTED}
    paper = next(w for w in researcher["works"] if w["put_code"] == 2201)
    assert paper["linked_records"][0]["target_key"] == "doc:exampla-paper-1"
    with pytest.raises(ResearchEntityError):
        ask.researcher(h.NS, h.ADA, scopes=h.NO_RESEARCHERS)
    assert ask.researcher(h.NS, h.BO, scopes=h.SCOPES)["record_status"] == "active"
    revised = env.run([h.ORCID_SOURCE], "research-entities-orcid-v2",
                      adapters={h.ORCID_SOURCE: h.adapter(h.ORCID_SOURCE, "v2")})
    assert revised["status"] == "complete"
    assert ask.researcher(h.NS, h.BO, scopes=h.SCOPES)["record_status"] == "deactivated"  # a removal is a revision
    assert ask.researcher(h.NS, h.BO, scopes=h.SCOPES, as_of="2099-03-10")["record_status"] == "active"
    later = ask.researcher(h.NS, h.ADA, scopes=h.SCOPES, as_of="2099-06-01")
    assert [w["external_ids"][0]["value"] for w in later["works"]][-1] == h.PAPER2

    # --- paper -> datasets; a subject with no records -----------------------------------------------------------
    assert [d["doi"] for d in ask.datasets_for_paper(h.NS, h.PAPER1, scopes=h.SCOPES)["datasets"]] == [h.DS1, h.DS2]
    assert ask.organisation(h.NS, "https://ror.org/0zzqqq111", scopes=h.SCOPES)["status"] == "none_on_record"
    assert ask.researcher(h.NS, "0000-0009-9999-0046", scopes=h.SCOPES)["status"] == "none_on_record"

    # --- exclusions and minimisation ---------------------------------------------------------------------------
    answers = [exampla, july, researcher]
    text = json.dumps([{k: v for k, v in a.items() if k != "exclusions"} for a in answers]).lower()
    assert not [w for w in EXCLUDED_WORDS if w in text] and not any(forbidden_keys(a) for a in answers)
    assert set(exampla["exclusions"]) == set(EXCLUSIONS)
    assert not [p for p in h.PERSONAL if p in json.dumps(answers)]

    # --- monitor: a re-run of the same fixtures changes nothing; idempotent re-acquisition ---------------------
    monitor = ResearchEntityMonitor(env.conn, now=env.now)
    watch = monitor.create(h.NS, "exampla", watch="organisation", key=h.EXAMPLA, principal_id="alice",
                           scopes=h.SCOPES)
    SubscriptionStore(env.conn).commit_watermark(h.NS, 1)
    assert monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"]
    again = env.run(h.SOURCES, "research-entities-again")
    assert again["status"] == "complete"
    assert len(store.history(h.NS, "research-entities:ror:0zzexa101", scopes=h.SCOPES)) == 2
    SubscriptionStore(env.conn).commit_watermark(h.NS, 2)
    assert monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"] == []
