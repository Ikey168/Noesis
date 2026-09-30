"""Offline organisation-to-research-records acceptance for the Science research-entities features (RE13, #2644).

The pinned ROR, ORCID, DataCite and CORDIS fixtures (authored in the documented shapes; every organisation, person,
identifier and value is fictional) replay through the real ``research-entities`` adapter and the runtime's projector,
with all four features selected in the Science composition plan and sockets blocked. An organisation and a researcher
reach cited registry records, asserted works, datasets and projects with record versions; identity is reviewed, links
cross into Scholarly, Funding and Ownership records, and a subject with no records answers none_on_record. Offline
evidence only, never live coverage (``docs/development/research-entities-evidence/``).
"""

from __future__ import annotations

import json
import socket

import pytest

from src.domains import registry as domain_registry
from src.ingestion.research_entities_sources import EXCLUSIONS, LIVE_VERIFICATION
from src.ingestion.source_pack_runtime import PROJECTORS
from src.ingestion.source_packs import SourcePackConformance
from src.kb.research_entities_identity import ResearchEntitiesIdentity
from src.kb.research_entities_links import ResearchEntitiesLinks
from src.kb.research_entities_monitoring import ResearchEntitiesMonitor
from src.kb.research_entities_queries import ResearchEntitiesQueries
from src.kb.research_entities_records import (
    FEATURES,
    ResearchEntitiesError,
    ResearchEntitiesProjector,
    feature_enabled,
    forbidden_keys,
    readiness,
)
from tests.unit import research_entities_harness as h
from tests.unit.composition.test_migration import _migrated


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


def test_organisation_and_researcher_to_cited_registry_records_works_datasets_and_projects():
    conn, coordinator, bundles, _ = _migrated(h.connection())
    coordinator.select("science", bundles["science"]["version"], features=sorted(FEATURES.values()))
    assert coordinator.activate("research-entities-acceptance")["status"] == "published"
    assert all(feature_enabled(conn, f) for f in FEATURES.values())
    assert PROJECTORS["noesis-research-entity-record-v1"](conn).__class__ is ResearchEntitiesProjector

    # Pinned fixtures replay offline through the real adapter and match their recorded output hashes.
    manifest = h.manifest()
    ours = [s for s in manifest["sources"] if s["connector"] == "research-entities"]
    replay = SourcePackConformance(h.ROOT).offline({**manifest, "sources": ours})
    assert replay["valid"] and replay["coverage"]["verified"] == 4

    # A monitor on the organisation from the start; first releases, then the later ones.
    monitor = ResearchEntitiesMonitor(conn, now=lambda: h.SECOND_RETRIEVAL + 10)
    watch = monitor.create(h.NS, "uni", watch={"ror": h.A1}, principal_id="alice", scopes=h.SCOPES)
    h.load_all(conn)
    first_notices = monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"]
    assert {n["kind"] for n in first_notices} == {"new_record", "new_dataset"}
    for name in h.SOURCES:
        h.apply(conn, name, later=True, retrieved_at_ms=h.SECOND_RETRIEVAL)
    status = readiness(conn)
    assert {p["live_verification"] for p in status["providers"].values()} == {"unverified-live"}
    assert all(p["selected"] and p["records"] for p in status["providers"].values())

    # Reviewable identity: proposals only, a reviewer accepts, rejects and reverts; nothing merged.
    h.seed_science_and_funding(conn)
    own = h.seed_ownership(conn)
    identity = ResearchEntitiesIdentity(conn)
    proposed = identity.propose(h.NS, principal_id="analyst", scopes=h.SCOPES, ownership_namespace=h.OWN_NS)
    assert {m["state"] for m in proposed["matches"]} == {"proposed"}
    by = {(m["left"]["key"].rsplit(":", 1)[-1], m["right"]["key"]): m for m in proposed["matches"]}
    uni_pic = by[("0re1ab101", "research-entities:pic:999999901")]
    poly_pic = by[("0re1ab303", "research-entities:pic:999999902")]
    uni_own = by[("0re1ab101", own["Universitaet Beispielstadt"])]
    identity.review(h.NS, uni_pic["match_id"], "accept", "same website and country", principal_id="rev",
                    scopes=h.SCOPES)
    identity.review(h.NS, uni_own["match_id"], "accept", "ISNI agrees", principal_id="rev", scopes=h.SCOPES)
    identity.review(h.NS, poly_pic["match_id"], "accept", "same website", principal_id="rev", scopes=h.SCOPES)
    identity.revert(h.NS, poly_pic["match_id"], "polytechnic merged; reviewer withdraws", principal_id="rev",
                    scopes=h.SCOPES)
    unmatched = identity.unmatched(h.NS, scopes=h.SCOPES)
    assert h.A2 in {o["ror_id"] for o in unmatched["organisations"]}
    assert {"999999902", "999999903"} <= {p["pic"] for p in unmatched["participants"]}
    links = ResearchEntitiesLinks(conn).build(h.NS, principal_id="analyst", scopes=h.SCOPES,
                                              ownership_namespace=h.OWN_NS)
    assert {v["target_status"] for v in links["missing"]} == {"target_missing"}

    queries = ResearchEntitiesQueries(conn)
    # Organisation: the ROR record of the release in force, lineage, projects, datasets, ownership, all cited.
    uni = queries.organisation(h.NS, h.A1, scopes=h.READ_ONLY, as_of="2099-07-01")
    assert uni["status"] == "answered" and uni["record_revision"]["revision_marker"] == "v9.2-2099-03-15"
    assert ("predecessor", h.M1) in {(r["type"], r["id"]) for r in uni["organisation"]["relationships"]}
    assert uni["lineage"][0]["to_record"]["status"] == "inactive"
    assert [p["project"]["project_id"] for p in uni["projects"]] == ["101999001"]
    assert uni["contributions_by_currency"]["ec_contribution"] == {"EUR": "2050000"}
    assert {d["doi"] for d in uni["datasets"]} == {"10.9999/rent.data1", "10.9999/rent.data3"}
    assert uni["ownership_links"][0]["basis"]["method"] == "accepted-match"
    assert all({"provider", "revision", "revision_marker", "as_of", "release_id"} <= set(c)
               for c in uni["citations"])
    earlier = queries.organisation(h.NS, h.A1, scopes=h.READ_ONLY, as_of="2099-02-01")
    assert earlier["record_revision"]["revision_marker"] == "v9.1-2099-01-15" and earlier["lineage"] == []
    assert earlier["contributions_by_currency"]["ec_contribution"] == {"EUR": "2000000"}
    # The reverted participant match is not used: the polytechnic's project stays with no organisation.
    poly = queries.organisation(h.NS, h.M1, scopes=h.READ_ONLY)
    assert poly["projects"] == [] and poly["identity"]["status"] == "candidates_pending"

    # Researcher: asserted works and employments of the version in force, minimised, cited.
    with pytest.raises(ResearchEntitiesError):
        queries.researcher(h.NS, h.R1, scopes=h.READ_ONLY)
    ada = queries.researcher(h.NS, h.R1, scopes=h.SCOPES, as_of="2099-03-01")
    assert ada["record_revision"]["revision"] == 1 and {p["target_status"] for p in ada["linked_papers"]} == {
        "resolved"}
    assert {w["assertion"] for w in ada["researcher"]["works"]} == {"orcid-asserted"}
    text = json.dumps(ada)
    assert "ada@example.invalid" not in text and "fictional biography" not in text and "Scopus" not in text
    latest = queries.researcher(h.NS, h.R1, scopes=h.SCOPES)
    assert latest["record_revision"]["revision"] == 2
    assert "10.9999/rent.paper3" in {i["value"] for w in latest["researcher"]["works"] for i in w["identifiers"]}
    missing_paper = next(p for p in latest["linked_papers"] if p["doi"] == "10.9999/rent.paper3")
    assert missing_paper["target_status"] == "target_missing"
    ben = queries.researcher(h.NS, h.R2, scopes=h.SCOPES)
    assert ben["status"] == "removed" and ben["researcher"] is None
    # Datasets related to a paper, with relation types as published.
    related = queries.datasets_for_paper(h.NS, "10.9999/rent.paper1", scopes=h.READ_ONLY)
    assert {d["doi"] for d in related["datasets"]} == {"10.9999/rent.data1", "10.9999/rent.data3"}
    history = queries.record_history(h.NS, "organisation", h.M1, scopes=h.READ_ONLY)
    assert [r["status"] for r in history["revisions"]] == ["active", "inactive"]

    # A subject with no records.
    assert queries.researcher(h.NS, h.R3, scopes=h.SCOPES)["status"] == "none_on_record"
    assert queries.organisation(h.NS, "https://ror.org/0zzzzzz99", scopes=h.READ_ONLY)["status"] == "none_on_record"

    # Evidence bundle, exclusions and minimisation.
    bundle = queries.export_bundle(uni, created_at_ms=1)
    assert bundle["completeness"]["status"] == "complete" and len(bundle["roots"]) == 1
    for answer in (uni, ada, related, history):
        assert forbidden_keys(answer) == [] and list(EXCLUSIONS) == answer["exclusions"]
    assert all(v["status"] == "unverified-live" for v in LIVE_VERIFICATION.values())

    # The monitor reports the later release, and a restart replays nothing.
    later = monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"]
    assert {n["kind"] for n in later} == {"registry_change", "new_project"}
    assert monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"] == []
    for name in h.SOURCES:
        assert {r["status"] for r in h.apply(conn, name, later=True, retrieved_at_ms=h.SECOND_RETRIEVAL + 1)} == {
            "unchanged"}
