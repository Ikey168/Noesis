"""Offline model-to-registry-records acceptance for the Technology AI models features (AI12, #2804; track #2742).

The pinned Hugging Face Hub, OpenML and Epoch AI fixtures (authored in the documented shapes; every organisation,
repository, sha, id, value and date is fictional) replay through the real ``ai-models`` adapter and the runtime's
projector with sockets blocked, the AI models features selected in the composition plan. The journey takes a model to
cited registry records across sources: revision history, a licence change, a repository turning gated, as-of answers,
cross-source identity review, Literature and OSS links, a refused user namespace and a model with no records, under
the exclusions and the AI01 minimisation decision. Offline evidence only, never live coverage
(``docs/development/ai-models-evidence/``).
"""

from __future__ import annotations

import json
import socket
from datetime import UTC, datetime

import pytest

from src.domains import registry as domain_registry
from src.ingestion.ai_models_sources import EXCLUSIONS, MINIMISATION, fixture_transport
from src.ingestion.source_pack_runtime import PROJECTORS
from src.ingestion.source_packs import SourcePackConformance, SourcePackError
from src.kb.ai_models_identity import AiModelsIdentity
from src.kb.ai_models_links import AiModelsLinks
from src.kb.ai_models_monitoring import AiModelsMonitor
from src.kb.ai_models_queries import AiModelsQueries
from src.kb.ai_models_records import (
    excluded_paths,
    features_enabled,
    personal_data_paths,
    readiness,
)
from tests.unit import ai_models_harness as h
from tests.unit.composition.test_migration import _migrated

A, B, C, D, E = (h.SHAS[k] for k in "ABCDE")
PERSONS = ("Ada Example", "Bob Example", "Cy Example", "Dee Example", "990001")


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


def _clean(value):
    text = json.dumps(value, default=str)
    assert personal_data_paths(value) == [] and excluded_paths(value) == []
    for person in PERSONS:
        assert person not in text, person
    for leaked in ("downloads", "likes", "trendingScore", "authored prose", "never stored either"):
        assert leaked not in text, leaked


def test_model_to_cited_registry_records_across_sources_with_revisions_licences_identity_and_links():
    conn, coordinator, bundles, _ = _migrated()
    coordinator.select("technology", bundles["technology"]["version"],
                       features=["ai-models-hub", "ai-models-openml", "ai-models-epoch"])
    assert coordinator.activate("ai-models-acceptance")["status"] == "published"
    assert set(features_enabled(conn).values()) == {True}

    # Pinned fixtures replay offline through the real adapter; the runtime projector owns the record contract.
    manifest = h.manifest()
    assert SourcePackConformance(h.ROOT).offline(manifest)["valid"]
    assert "noesis-ai-model-record-v2" in PROJECTORS
    h.load_all(conn, revisions=True)
    h.load_spdx(conn)

    # Revision history and a licence change, quoted as declared, SPDX only on an exact id.
    queries = AiModelsQueries(conn)
    history = queries.revision_history(h.NS, h.MODEL, scopes=h.READ_ONLY)
    assert [r["sha"] for r in history["revisions"]] == [A, B, E]
    change = history["licence_changes"][0]
    assert (change["before"]["declared"]["license"], change["before"]["spdx"]["spdx_id"]) == ("apache-2.0",
                                                                                              "Apache-2.0")
    assert change["after"]["declared"]["license"] == "other" and change["after"]["spdx"]["status"] == \
        "not_normalised"
    # A repository turning gated is a source-stated revision; history is kept.
    gated = queries.revision_history(h.NS, h.SMALL, scopes=h.READ_ONLY)
    assert [(r["state"], r.get("sha")) for r in gated["revisions"]] == [("published", C), ("withdrawn", None)]

    # Cross-source identity: proposed on stated identifiers, reviewed by another principal, nothing merged.
    identity = AiModelsIdentity(conn)
    proposed = identity.propose(h.NS, principal_id="alice", scopes=h.SCOPES)
    epoch_match = next(m for m in proposed["matches"] if m["pair_kind"] == "epoch-hub-model")
    assert epoch_match["method"] == "stated-repository-id" and epoch_match["merged"] is False
    identity.review(h.NS, epoch_match["match_id"], "accept", "the Epoch row links the repository",
                    principal_id="bob", scopes=h.SCOPES)
    unmatched = {u["label"] for u in identity.unmatched(h.NS, scopes=h.SCOPES)}
    assert {"Fixture Small Model", h.SMALL} <= unmatched  # a shared name is never a match

    # As-of answers: each source's revision current at the date, side by side, cited, never merged.
    answer = queries.records_as_of(h.NS, h.MODEL, scopes=h.READ_ONLY, as_of="2096-06-01")
    hub, epoch = answer["side_by_side"]
    assert hub["citation"]["revision"]["sha"] == B and hub["citation"]["as_of"] == "2096-05-10T08:00:00Z"
    assert epoch["citation"]["revision"]["vintage_id"] and epoch["relation"] == "accepted-match"
    assert hub["reported"][0]["kind"] == "self_reported_result" and epoch["reported"][0]["kind"] == "epoch_estimate"
    assert "never merged" in answer["not_merged"]
    later = queries.records_as_of(h.NS, h.MODEL, scopes=h.READ_ONLY, as_of="2098-06-01")
    assert later["side_by_side"][0]["revision"]["sha"] == E
    dataset = queries.records_as_of(h.NS, "openml:990061", scopes=h.READ_ONLY, as_of="2098-06-01")
    states = {i["run_id"]: i["state"] for i in dataset["side_by_side"][0]["reported"][0]["items"]}
    assert states[99000002] == "not-returned" and dataset["side_by_side"][0]["reported"][0]["reported_by"].startswith(
        "OpenML")

    # Literature and OSS links by stated identifier, pinned to revisions; Literature resolves through the paper
    # connector, the package through the OSS ecosystems records.
    from src.ingestion.connectors.paper.connector import paper_metadata_to_document
    from src.ingestion.connectors.paper.models import PaperMetadata
    from src.ingestion.document_store import DocumentStore
    from src.kb.oss_ecosystem_store import OssEcosystemStore

    DocumentStore(conn).upsert([paper_metadata_to_document(PaperMetadata(
        title="Fixture Model: a fictional paper", arxiv_id="2095.01234",
        published=datetime(2095, 1, 10, tzinfo=UTC)), h.FIRST_RETRIEVAL)])
    OssEcosystemStore(conn).apply(h.NS, [{"record_type": "release_state_revision", "source": "pypi",
                                          "ecosystem": "pypi", "package": "fixture-lib", "version": "1.0.0",
                                          "state": "published"}], run_id="run:oss",
                                  scopes={"knowledge:oss:write", "namespace:global:write"})
    links = AiModelsLinks(conn)
    links.link(h.NS, principal_id="alice", scopes=h.SCOPES)
    every = links.links(h.NS, scopes=h.READ_ONLY)
    paper = next(x for x in every if x["kind"] == "literature" and x["identifier"] == "2095.01234"
                 and x["basis"] == "stated-identifier")
    assert paper["state"] == "linked" and paper["target"]["document_id"] == "arxiv:2095.01234" and \
        paper["revision_id"]
    package = next(x for x in every if x["kind"] == "oss-package")
    assert package["state"] == "linked" and package["target"]["coordinate"] == "pkg:pypi:fixture-lib"
    assert {x["state"] for x in every if x["kind"] == "dataset-doi"} == {"provider_absent"}  # reported, not dropped
    assert {x["state"] for x in every if x["kind"] == "library-name"} == {"stated_text"}

    # A refused user namespace and a model with no records.
    item = json.loads(json.dumps(h.source("hub")))
    item["ai_models"]["selection"]["repositories"] = [{"repo_id": "example-user/fixture-personal-model",
                                                       "kind": "model", "revisions": []}]
    with pytest.raises(SourcePackError) as refused:
        h.fetch("hub", item=item, transport=fixture_transport([
            {"request": "huggingface.co/api/organizations/example-user/overview", "status": 404}]))
    assert refused.value.code == "user_namespace"
    nothing = queries.records_as_of(h.NS, "example-org/no-such-model", scopes=h.READ_ONLY, as_of="2098-01-01")
    assert nothing["status"] == "no_records" and nothing["side_by_side"] == []

    # Evidence bundle and monitoring cite revisions; readiness keeps offline evidence apart from live coverage.
    bundle = queries.evidence_bundle(answer, created_at_ms=h.SECOND_RETRIEVAL)
    assert all(o["payload"].get("as_of") for o in bundle["objects"] if o["type"] == "evidence")
    monitor = AiModelsMonitor(conn, now=lambda: h.SECOND_RETRIEVAL)
    watch = monitor.create(h.NS, "acceptance", target={"repo_id": h.MODEL}, principal_id="alice", scopes=h.SCOPES)
    notices = monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"]
    assert {n["kind"] for n in notices} == {"new_revision", "licence_change"}
    assert monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"] == []
    report = readiness(conn)
    assert {p["live_verification"] for p in report["providers"].values()} == {"unverified-live"}

    # Exclusions and the AI01 minimisation decision hold across every answer.
    for value in (history, gated, answer, later, dataset, every, nothing, notices, bundle):
        _clean(value)
    assert {"leaderboards or rankings", "licence-compliance interpretation"} <= set(answer["exclusions"])
    assert set(answer["exclusions"]) == set(EXCLUSIONS)
    assert MINIMISATION["decision"].startswith("organisation-level registry metadata only")
