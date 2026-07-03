"""Unit tests for the research domain pack (R7 / Track N1)."""

import pytest

from src.analytics.honesty import validate_analytic_output
from src.domains.research.analytics import (
    citation_graph,
    literature_claims,
    venue_credibility,
)
from src.domains.research.enrichers import (
    citation_enricher,
    concept_enricher,
    venue_enricher,
)


# ---------------------------------------------------------------------------
# Enrichers (#603)
# ---------------------------------------------------------------------------


def test_venue_enricher_normalizes():
    doc = {"metadata": {"journal": "  Nature Climate Change  "}}
    assert venue_enricher(doc) == {"venue": "Nature Climate Change", "enricher": "venue"}
    assert venue_enricher({"metadata": {}}) is None


def test_citation_enricher_counts_refs_and_citations():
    doc = {"metadata": {"references": ["p1", "p2", "p3"], "citations": 42}}
    out = citation_enricher(doc)
    assert out["citations"] == 42
    assert out["reference_count"] == 3
    assert out["refs"] == "p1,p2,p3"
    # References-only falls back to a ref count.
    assert citation_enricher({"metadata": {"references": ["a"]}})["citations"] == 1
    assert citation_enricher({"metadata": {}}) is None


def test_concept_enricher_picks_dominant_term():
    doc = {"title": "Solar subsidy grid renewable", "metadata": {"abstract": "renewable renewable grid"}}
    out = concept_enricher(doc)
    assert out["concept"] in {"renewable", "grid", "solar", "subsidy"}
    assert concept_enricher({"title": "", "metadata": {}}) is None


def test_enrichers_read_dataclass_like_objects():
    from types import SimpleNamespace

    doc = SimpleNamespace(title="AI safety", metadata={"venue": "NeurIPS"})
    assert venue_enricher(doc)["venue"] == "NeurIPS"


# ---------------------------------------------------------------------------
# venue_credibility (#604) — generalized transparency scoring
# ---------------------------------------------------------------------------


def test_venue_credibility_scores_and_is_honesty_valid(seed, conn):
    seed.documents(
        conn,
        [
            {"id": "a", "venue": "Nature", "concept": "climate", "citations": 100},
            {"id": "b", "venue": "Nature", "concept": "energy", "citations": 80},
            {"id": "c", "venue": "Nature", "concept": "policy", "citations": 60},
            {"id": "d", "venue": "arXiv", "concept": "ml", "citations": 5},
            {"id": "e", "venue": "arXiv", "concept": "ml", "citations": 3},
        ],
    )
    seed.claims(
        conn,
        [
            {"claim_id": "c1", "document_id": "a", "attributed": True},
            {"claim_id": "c2", "document_id": "d", "attributed": False},
        ],
    )
    payload = venue_credibility(conn)
    assert validate_analytic_output(payload) == []
    venues = {v["venue"]: v for v in payload["venues"]}
    # Nature: diverse concepts + high citations + attributed -> beats arXiv.
    assert venues["Nature"]["credibility"]["value"] > venues["arXiv"]["credibility"]["value"]
    ci = venues["Nature"]["credibility"]
    assert ci["lo"] <= ci["value"] <= ci["hi"]
    assert payload["n"] == 5


def test_venue_credibility_no_corpus(conn):
    payload = venue_credibility(conn)  # no documents table
    assert payload["n"] == 0
    assert "note" in payload
    assert validate_analytic_output(payload) == []


# ---------------------------------------------------------------------------
# citation_graph (#604)
# ---------------------------------------------------------------------------


def test_citation_graph_builds_edges(seed, conn):
    seed.documents(
        conn,
        [
            {"id": "p1", "title": "root", "concept": "climate", "citations": 50, "refs": ""},
            {"id": "p2", "title": "cites p1", "concept": "climate", "citations": 10, "refs": "p1"},
            {"id": "p3", "title": "cites p1,p2", "concept": "climate", "citations": 5, "refs": "p1,p2"},
        ],
    )
    graph = citation_graph(conn, topic="climate")
    assert graph["node_count"] == 3
    assert {"from": "p2", "to": "p1"} in graph["edges"]
    assert graph["edge_count"] == 3  # p2->p1, p3->p1, p3->p2


def test_citation_graph_no_corpus(conn):
    graph = citation_graph(conn)
    assert graph == {"nodes": [], "edges": [], "note": "no document corpus ingested"}


# ---------------------------------------------------------------------------
# literature_claims (#604)
# ---------------------------------------------------------------------------


def test_literature_claims_scopes_to_papers(seed, conn):
    seed.claims(
        conn,
        [
            {"claim_id": "c1", "claim_text": "AI scales", "source_type": "paper", "attributed": True, "confidence": 0.9},
            {"claim_id": "c2", "claim_text": "news claim", "source_type": "news", "confidence": 0.8},
        ],
    )
    out = literature_claims(conn)
    assert out["count"] == 1
    assert out["claims"][0]["claim_id"] == "c1"
    assert out["claims"][0]["attributed"] is True


def test_literature_claims_no_layer(conn):
    assert literature_claims(conn) == {"claims": [], "note": "no claim layer available"}
