"""
Tests for claim extraction and the knowledge layer (#519).

Covers the exit criterion: claims are extracted with provenance, stored as cited
Claim nodes, linked across documents with SUPPORTS/CONTRADICTS, and
"what does the literature say about X" returns synthesized, cited claims with
corroborating and contradicting evidence.
"""

import json
from pathlib import Path

import pytest

from src.knowledge_graph.claim_extractor import ClaimExtractor, ExtractedClaim
from src.knowledge_graph.claim_graph import (
    build_claim_graph,
    link_claims,
    literature_on,
)
from src.knowledge_graph.foundation import (
    EntityType,
    KnowledgeGraphStore,
    Node,
    RelationType,
)


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #


def test_extract_subject_predicate_object():
    claims = ClaimExtractor().extract(
        "Transformers outperform recurrent networks on translation.", source_doc="doc:1"
    )
    assert len(claims) == 1
    c = claims[0]
    assert c.subject == "Transformers"
    assert c.predicate == "outperform"
    assert "recurrent networks" in c.object
    assert c.negated is False
    assert c.source_doc == "doc:1"


def test_extract_detects_negation():
    claims = ClaimExtractor().extract("The model does not improve accuracy.", source_doc="doc:1")
    assert len(claims) == 1
    assert claims[0].subject == "The model"
    assert claims[0].predicate == "improve"
    assert claims[0].negated is True


def test_questions_and_verbless_sentences_are_not_claims():
    extractor = ClaimExtractor()
    assert extractor.extract("What is attention?", source_doc="d") == []
    assert extractor.extract("A short fragment.", source_doc="d") == []


def test_extract_requires_source_doc():
    with pytest.raises(ValueError):
        ClaimExtractor().extract("Attention improves results.", source_doc="")


def test_extract_from_chunks_carries_path():
    chunks = [
        {"text": "Attention improves translation quality.", "path": ["4.2 Methods"]},
        {"text": "What is attention?", "path": ["1 Introduction"]},
    ]
    claims = ClaimExtractor().extract_from_chunks(chunks, source_doc="arxiv:X")
    assert len(claims) == 1
    assert claims[0].chunk_path == ["4.2 Methods"]


# --------------------------------------------------------------------------- #
# Claim graph
# --------------------------------------------------------------------------- #


def _doc(store, node_id, name):
    return store.add_node(Node(EntityType.DOCUMENT, name, node_id=node_id))


def test_build_claim_graph_cites_claims_to_document():
    store = KnowledgeGraphStore()
    _doc(store, "arxiv:A", "Paper A")
    claims = ClaimExtractor().extract_from_chunks(
        [{"text": "Attention improves machine translation quality.", "path": ["Abstract"]}],
        source_doc="arxiv:A",
    )
    nodes = build_claim_graph(store, "arxiv:A", claims)

    assert len(nodes) == 1
    claim_node = nodes[0]
    assert claim_node.type == EntityType.CLAIM
    # Document SUPPORTS Claim, cited with the chunk path as provenance.
    supports = store.triples(subject="arxiv:A", predicate=RelationType.SUPPORTS, object=claim_node.node_id)
    assert len(supports) == 1
    assert supports[0].provenance.source_doc == "arxiv:A"
    assert supports[0].provenance.chunk_id == "Abstract"


def test_build_claim_graph_requires_existing_document():
    store = KnowledgeGraphStore()
    claims = [ExtractedClaim(text="X improves Y.", subject="X", predicate="improves",
                             object="Y", source_doc="missing")]
    with pytest.raises(ValueError):
        build_claim_graph(store, "missing", claims)


# --------------------------------------------------------------------------- #
# Cross-document linking + literature query (exit criterion)
# --------------------------------------------------------------------------- #


@pytest.fixture
def literature_store():
    store = KnowledgeGraphStore()
    extractor = ClaimExtractor()
    papers = {
        "arxiv:A": ("Paper A", "Attention improves machine translation quality."),
        "arxiv:B": ("Paper B", "Attention does not improve machine translation quality."),
        "arxiv:C": ("Paper C", "Attention improves machine translation quality significantly."),
    }
    for doc_id, (name, text) in papers.items():
        _doc(store, doc_id, name)
        build_claim_graph(store, doc_id, extractor.extract(text, source_doc=doc_id))
    return store


def test_link_claims_creates_support_and_contradiction(literature_store):
    added = link_claims(literature_store)
    assert added == 3  # A-C supports, A-B contradicts, B-C contradicts

    contradicts = literature_store.triples(predicate=RelationType.CONTRADICTS)
    supports_claim_claim = [
        t for t in literature_store.triples(predicate=RelationType.SUPPORTS)
        if literature_store.get_node(t.subject).type == EntityType.CLAIM
    ]
    assert len(contradicts) == 2
    assert len(supports_claim_claim) == 1


def test_literature_on_returns_cited_claims_with_evidence(literature_store):
    link_claims(literature_store)
    results = literature_on(literature_store, "machine translation")

    assert len(results) == 3
    # Every returned claim is cited to a document.
    assert all(r["supported_by"] for r in results)

    positive = next(r for r in results if r["claim"].startswith("Attention improves machine translation quality."))
    assert "Paper A" in positive["supported_by"]
    # The negated claim contradicts the positive one.
    assert any("does not improve" in c for c in positive["contradicted_by"])
    # The "significantly" claim corroborates it.
    assert any("significantly" in c for c in positive["corroborated_by"])


# --------------------------------------------------------------------------- #
# Eval golden set
# --------------------------------------------------------------------------- #


def test_paper_eval_golden_set_is_valid():
    path = Path(__file__).resolve().parents[2] / "legacy" / "evals" / "qa_papers.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    assert len(rows) >= 3
    for row in rows:
        assert row["query"] and row["answers"] and row["must_have_terms"]
        assert row["source_type"] == "paper"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
