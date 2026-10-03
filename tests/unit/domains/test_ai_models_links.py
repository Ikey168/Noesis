"""AI07 (#2780, track #2742): links to Literature, dataset DOIs and OSS packages by citation or accepted match."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from src.kb.ai_models_identity import AiModelsIdentity
from src.kb.ai_models_links import AiModelsLinks, literature_document_ids
from src.kb.ai_models_records import AiModelsError, personal_data_paths
from tests.unit import ai_models_harness as h

DOI = "10.99999/fixture-corpus-2095"


def hold_literature(conn) -> None:
    """A paper through the paper connector's document mapping (fictional arXiv id) in the shared documents table."""
    from src.ingestion.connectors.paper.connector import paper_metadata_to_document
    from src.ingestion.connectors.paper.models import PaperMetadata
    from src.ingestion.document_store import DocumentStore

    paper = PaperMetadata(title="Fixture Model: a fictional paper", arxiv_id="2095.01234",
                          abstract="A fictional abstract.", published=datetime(2095, 1, 10, tzinfo=UTC))
    summary = DocumentStore(conn).upsert([paper_metadata_to_document(paper, h.FIRST_RETRIEVAL)])
    assert summary.inserted == 1


def hold_datacite(conn) -> None:
    """The fixture corpus DOI through the research-entities DataCite path."""
    from src.ingestion.research_entities_sources import parse_datacite_doi
    from src.kb.research_entities_records import ResearchEntityStore

    raw = json.dumps({"data": {"id": DOI, "attributes": {
        "doi": DOI, "state": "findable", "types": {"resourceTypeGeneral": "Dataset"},
        "titles": [{"title": "Fixture Corpus"}], "publisher": "Example Org", "publicationYear": 2095,
        "updated": "2095-11-12T00:00:00Z", "metadataVersion": 1}}}).encode()
    ResearchEntityStore(conn).project(h.NS, [parse_datacite_doi(raw, DOI)], run_id="run:datacite",
                                      source_id="research-entities-datacite", observed_at_ms=h.FIRST_RETRIEVAL)


def hold_package(conn) -> None:
    from src.kb.oss_ecosystem_store import OssEcosystemStore

    OssEcosystemStore(conn).apply(h.NS, [{"record_type": "release_state_revision", "source": "pypi",
                                          "ecosystem": "pypi", "package": "fixture-lib", "version": "1.0.0",
                                          "state": "published", "published_at": "2095-01-01"}],
                                  run_id="run:oss", scopes={"knowledge:oss:write", "namespace:global:write"},
                                  observed_at_ms=h.FIRST_RETRIEVAL)


def by(links, **match):
    return [x for x in links if all(x[k] == v for k, v in match.items())]


def test_absent_providers_are_reported_and_library_name_stays_stated_text():
    conn = h.connection()
    h.load_all(conn)
    links = AiModelsLinks(conn)
    result = links.link(h.NS, principal_id="alice", scopes=h.SCOPES)
    assert result["linked"] == [] and result["target_not_held"] == []
    every = links.links(h.NS, scopes=h.READ_ONLY)
    assert {x["state"] for x in every} == {"provider_absent", "stated_text"}
    model = h.record(conn, "hub-model", h.MODEL)["record_id"]
    library = by(every, record_id=model, kind="library-name")
    assert [x["identifier"] for x in library] == ["fixture-lib"] and library[0]["target"] is None
    assert by(every, record_id=model, kind="literature")[0]["evidence"]["reason"] == \
        "science.literature is not installed (documents)"
    assert by(every, kind="oss-package")[0]["evidence"]["reason"] == "oss.registries is not installed (oss_records)"
    # Every link points at a specific record revision.
    revisions = {r[0] for r in conn.execute("SELECT revision_id FROM ai_revisions").fetchall()}
    assert all(x["revision_id"] in revisions for x in every)


def test_stated_identifiers_resolve_through_the_paper_connector_datacite_and_oss_records():
    conn = h.connection()
    h.load_all(conn)
    hold_literature(conn)
    hold_datacite(conn)
    hold_package(conn)
    links = AiModelsLinks(conn)
    with pytest.raises(AiModelsError):
        links.link(h.NS, principal_id="alice", scopes={"knowledge:technical:ai-models:write", "namespace:global:write"})
    links.link(h.NS, principal_id="alice", scopes=h.SCOPES)
    every = links.links(h.NS, scopes=h.READ_ONLY)
    model = h.record(conn, "hub-model", h.MODEL)["record_id"]
    paper = by(every, record_id=model, kind="literature", identifier="2095.01234")
    assert paper[0]["state"] == "linked" and paper[0]["target"]["document_id"] == "arxiv:2095.01234"
    assert paper[0]["basis"] == "stated-identifier" and paper[0]["target"]["resolved_by"] == \
        "src.ingestion.connectors.paper"
    corpus = h.record(conn, "hub-dataset", h.CORPUS)["record_id"]
    dataset = by(every, record_id=corpus, kind="dataset-doi", identifier=DOI)[0]
    assert dataset["state"] == "linked" and dataset["target"]["record_key"] == f"research-entities:doi:{DOI}"
    assert dataset["target"]["revision_id"]
    unheld_paper = by(every, record_id=corpus, kind="literature", identifier=DOI)[0]
    assert unheld_paper["state"] == "target_not_held"
    small = h.record(conn, "hub-model", h.SMALL)["record_id"]
    package = by(every, record_id=small, kind="oss-package")[0]
    assert package["state"] == "linked" and package["target"]["coordinate"] == "pkg:pypi:fixture-lib"
    assert package["target"]["revision_id"].startswith("oss")
    assert literature_document_ids("doi", DOI)[0] == f"doi:{DOI}"
    assert personal_data_paths(every) == []  # no paper author ever reaches a link
    assert "Ada" not in json.dumps(every)


def test_an_accepted_match_carries_the_counterparts_identifiers_with_that_basis():
    conn = h.connection()
    h.load_all(conn)
    hold_literature(conn)
    identity = AiModelsIdentity(conn)
    identity.propose(h.NS, principal_id="alice", scopes=h.SCOPES)
    epoch_id = h.record(conn, "epoch-model", "Fixture Model")["record_id"]
    match = identity.matches(h.NS, scopes=h.SCOPES, record_id=epoch_id)[0]
    links = AiModelsLinks(conn)
    links.link(h.NS, principal_id="alice", scopes=h.SCOPES)
    assert not by(links.links(h.NS, scopes=h.SCOPES, record_id=epoch_id), basis="accepted-match")
    identity.review(h.NS, match["match_id"], "accept", "the Epoch row links the repository", principal_id="bob",
                    scopes=h.SCOPES)
    links.link(h.NS, principal_id="alice", scopes=h.SCOPES)
    inherited = by(links.links(h.NS, scopes=h.SCOPES, record_id=epoch_id), basis="accepted-match")
    assert inherited and inherited[0]["identifier"] == "2095.01234" and inherited[0]["state"] == "linked"
    assert inherited[0]["evidence"]["match_id"] == match["match_id"]
    again = links.link(h.NS, principal_id="alice", scopes=h.SCOPES)
    assert all(not v for k, v in again.items() if k != "note")  # idempotent
