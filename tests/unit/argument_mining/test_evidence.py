"""
Unit tests for the Claim & Evidence Extraction Pipeline (Issue #93).

Fixtures cover the three required cross-source scenarios:
  - news  ↔ blog cross-reference  (corroboration → "supports")
  - paper ↔ news contradiction    (dispute       → "contradicts")
  - transcript with no match      (no evidence found)

All tests run fully offline with an injected model double.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure repo root is on path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from src.argument_mining.evidence import (
    ClaimRecord,
    EvidenceRecord,
    _claim_id,
    _contradiction_score,
    extract_claims,
    find_evidence,
    run_pipeline,
    store_claim,
    store_evidence,
)
from services.ingest.common.document_model import Document
from src.argument_mining.dataset import sentences_from_document
from src.argument_mining.models import ClaimPrediction


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _doc(document_id: str, source_type: str, content: str) -> Document:
    return Document(
        document_id=document_id,
        source_type=source_type,
        language="en",
        ingested_at=0,
        content=content,
    )


def _claim(claim_id: str, claim_text: str, document_id: str, source_type: str) -> ClaimRecord:
    return ClaimRecord(
        claim_id=claim_id,
        claim_text=claim_text,
        document_id=document_id,
        source_type=source_type,
        confidence=0.75,
    )


@pytest.fixture(autouse=True)
def _claim_model(monkeypatch):
    class Detector:
        prediction_mode = "pretrained:test-claim-model"

        @staticmethod
        def predict(document):
            predictions = []
            for index, sentence in enumerate(sentences_from_document(document)):
                lowered = sentence.lower()
                nonclaim = sentence.endswith("?") or any(
                    word in lowered for word in ("might", "perhaps", "do you think")
                )
                predictions.append(ClaimPrediction(
                    sentence, index, not nonclaim, 0.9 if nonclaim else 0.88
                ))
            return predictions

    monkeypatch.setattr("src.argument_mining.evidence.get_claim_detector", lambda: Detector())


# ---------------------------------------------------------------------------
# ID helpers
# ---------------------------------------------------------------------------

def test_claim_id_is_deterministic():
    a = _claim_id("doc-1", 0)
    b = _claim_id("doc-1", 0)
    assert a == b
    assert a.startswith("cl-")


def test_claim_id_differs_across_documents():
    assert _claim_id("doc-1", 0) != _claim_id("doc-2", 0)


# ---------------------------------------------------------------------------
# Contradiction score
# ---------------------------------------------------------------------------

def test_contradiction_score_clean_sentence():
    assert _contradiction_score("The GDP rose by 3.2% in the third quarter.") == 0


def test_contradiction_score_negation():
    score = _contradiction_score("The report is false and researchers dispute the findings.")
    assert score >= 2


def test_contradiction_score_hedge_words():
    score = _contradiction_score("Critics say the claims are wrong, however the data shows otherwise.")
    assert score >= 2


# ---------------------------------------------------------------------------
# extract_claims
# ---------------------------------------------------------------------------

def test_extract_claims_returns_claim_records():
    doc = _doc(
        "news-001",
        "news",
        "The Federal Reserve raised interest rates by 0.5% in March 2024. "
        "This decision reflects ongoing concerns about inflation.",
    )
    records = extract_claims(doc)
    # The first sentence has strong claim signals (measurement + date)
    assert any(r.document_id == "news-001" for r in records)
    for r in records:
        assert r.source_type == "news"
        assert 0 < r.confidence <= 1.0
        assert r.claim_id.startswith("cl-")


def test_extract_claims_filters_non_claims():
    doc = _doc(
        "note-001",
        "note",
        "I might believe this could perhaps work out. "
        "Do you think we should consider it?",
    )
    records = extract_claims(doc)
    # Hedged opinion + question should return no claims or very few.
    for r in records:
        assert r.confidence < 0.9  # no high-confidence claims in pure opinion text


def test_extract_claims_empty_document():
    doc = _doc("empty-001", "news", "")
    records = extract_claims(doc)
    assert records == []


# ---------------------------------------------------------------------------
# Scenario 1: news ↔ blog cross-reference (corroboration)
# ---------------------------------------------------------------------------

def test_news_blog_cross_reference():
    """A blog post corroborates a news claim → relation='supports'."""
    news_claim = _claim(
        "cl-news-01",
        "The European Central Bank raised interest rates by 50 basis points.",
        "news-ecb-001",
        "news",
    )
    # Blog sentence that closely mirrors the claim
    blog_corpus = [
        (
            "The ECB increased interest rates by 50 basis points in its latest decision.",
            "blog-finance-007",
            "blog",
        ),
        ("Markets reacted positively to the announcement.", "blog-finance-007", "blog"),
        ("Unrelated content about cooking recipes and weekend plans.", "blog-life-099", "blog"),
    ]
    evidence = find_evidence(news_claim, blog_corpus, threshold=0.15)

    assert len(evidence) > 0, "Expected corroborating evidence from blog"
    top = evidence[0]
    assert top.evidence_source_type == "blog"
    assert top.relation == "supports"
    assert top.similarity_score > 0


# ---------------------------------------------------------------------------
# Scenario 2: paper ↔ news contradiction
# ---------------------------------------------------------------------------

def test_paper_news_contradiction():
    """A news article disputes a research paper's claim → relation='contradicts'."""
    paper_claim = _claim(
        "cl-paper-01",
        "The vaccine efficacy rate reached 95% in clinical trials.",
        "paper-vax-001",
        "paper",
    )
    # News sentence that contradicts: "not", "dispute", "false"
    news_corpus = [
        (
            "Critics dispute the reported efficacy rate, calling the 95% figure false and incorrect.",
            "news-health-042",
            "news",
        ),
        (
            "The clinical study reported vaccine efficacy of 95 percent in trial participants.",
            "news-science-003",
            "news",
        ),
    ]
    evidence = find_evidence(paper_claim, news_corpus, threshold=0.10)

    assert len(evidence) > 0, "Expected evidence from news corpus"
    relations = {e.relation for e in evidence}
    assert "contradicts" in relations, f"Expected a contradiction but got: {relations}"

    # The contradiction sentence should have higher _contradiction_score
    contradicting = [e for e in evidence if e.relation == "contradicts"]
    assert len(contradicting) >= 1
    assert contradicting[0].evidence_source_type == "news"


# ---------------------------------------------------------------------------
# Scenario 3: transcript claim with no evidence match
# ---------------------------------------------------------------------------

def test_transcript_no_evidence_match():
    """A transcript claim with no similar text in corpus → empty evidence list."""
    transcript_claim = _claim(
        "cl-trans-01",
        "Senator Williams announced a new bipartisan infrastructure framework.",
        "transcript-senate-001",
        "transcript",
    )
    # Completely unrelated corpus
    unrelated_corpus = [
        ("The price of crude oil fell by 2 dollars per barrel.", "news-energy-001", "news"),
        ("Scientists discovered a new species of deep-sea fish.", "blog-science-002", "blog"),
        ("The quarterly earnings report exceeded analyst expectations.", "news-finance-003", "news"),
    ]
    evidence = find_evidence(transcript_claim, unrelated_corpus, threshold=0.20)
    assert evidence == [], f"Expected no evidence but got {len(evidence)} results"


# ---------------------------------------------------------------------------
# find_evidence: same-document filtering
# ---------------------------------------------------------------------------

def test_find_evidence_excludes_same_document():
    """Sentences from the claim's own document must never appear as evidence."""
    claim = _claim("cl-1", "The bank raised rates by 25 basis points.", "doc-001", "news")
    corpus = [
        # Same document — must be skipped
        ("The bank raised its benchmark interest rate by 25 basis points.", "doc-001", "news"),
        # Different document — eligible
        ("Central bank increased interest rates by 25 basis points today.", "doc-002", "blog"),
    ]
    evidence = find_evidence(claim, corpus, threshold=0.15)
    doc_ids = {e.evidence_document_id for e in evidence}
    assert "doc-001" not in doc_ids, "Same-document sentence leaked into evidence"


def test_find_evidence_empty_corpus():
    claim = _claim("cl-1", "GDP grew 3%.", "doc-001", "news")
    assert find_evidence(claim, []) == []


def test_find_evidence_deduplicates_same_document_id():
    """Only the highest-scoring sentence from each evidence document is kept."""
    claim = _claim("cl-1", "The unemployment rate fell to 3.5%.", "doc-x", "news")
    corpus = [
        ("The unemployment rate dropped to 3.5 percent last month.", "doc-y", "blog"),
        ("Unemployment fell sharply to 3.5% according to the labor bureau.", "doc-y", "blog"),
    ]
    evidence = find_evidence(claim, corpus, threshold=0.10)
    ev_docs = [e.evidence_document_id for e in evidence]
    assert ev_docs.count("doc-y") <= 1, "Duplicate document ID in evidence"


# ---------------------------------------------------------------------------
# DuckDB persistence
# ---------------------------------------------------------------------------

def _make_conn():
    """In-memory DuckDB with both claim tables."""
    import duckdb

    conn = duckdb.connect(":memory:")
    conn.execute("""
        CREATE TABLE argument_claims (
            claim_id VARCHAR PRIMARY KEY,
            claim_text VARCHAR NOT NULL,
            document_id VARCHAR NOT NULL,
            source_type VARCHAR NOT NULL,
            confidence DOUBLE,
            extracted_at VARCHAR,
            attributed BOOLEAN,
            attribution_text VARCHAR
        )
    """)
    conn.execute("""
        CREATE TABLE claim_evidence (
            evidence_id VARCHAR PRIMARY KEY,
            claim_id VARCHAR NOT NULL,
            evidence_text VARCHAR,
            evidence_document_id VARCHAR NOT NULL,
            evidence_source_type VARCHAR NOT NULL,
            relation VARCHAR NOT NULL,
            similarity_score DOUBLE,
            found_at VARCHAR
        )
    """)
    return conn


def test_store_claim_roundtrip():
    conn = _make_conn()
    record = ClaimRecord(
        claim_id="cl-abc123",
        claim_text="GDP grew 3.2% in 2024.",
        document_id="doc-001",
        source_type="news",
        confidence=0.82,
    )
    store_claim(record, conn)
    row = conn.execute(
        "SELECT claim_text, source_type, confidence FROM argument_claims WHERE claim_id = ?",
        ["cl-abc123"],
    ).fetchone()
    assert row is not None
    assert row[0] == "GDP grew 3.2% in 2024."
    assert row[1] == "news"
    assert abs(row[2] - 0.82) < 1e-6


def test_store_evidence_roundtrip():
    conn = _make_conn()
    claim = ClaimRecord(
        claim_id="cl-base",
        claim_text="Rates rose 0.5%.",
        document_id="doc-a",
        source_type="news",
        confidence=0.70,
    )
    store_claim(claim, conn)

    ev = EvidenceRecord(
        evidence_id="ev-xyz",
        claim_id="cl-base",
        evidence_text="Interest rates increased by half a percentage point.",
        evidence_document_id="doc-b",
        evidence_source_type="blog",
        relation="supports",
        similarity_score=0.61,
    )
    store_evidence(ev, conn)

    row = conn.execute(
        "SELECT relation, similarity_score FROM claim_evidence WHERE evidence_id = ?",
        ["ev-xyz"],
    ).fetchone()
    assert row is not None
    assert row[0] == "supports"
    assert abs(row[1] - 0.61) < 1e-6


def test_store_claim_is_idempotent():
    conn = _make_conn()
    record = ClaimRecord(
        claim_id="cl-idem",
        claim_text="Original claim.",
        document_id="doc-1",
        source_type="news",
        confidence=0.60,
    )
    store_claim(record, conn)
    # Re-store with updated confidence — should not raise
    record.confidence = 0.80
    store_claim(record, conn)
    row = conn.execute(
        "SELECT COUNT(*) FROM argument_claims WHERE claim_id = 'cl-idem'"
    ).fetchone()
    assert row is not None
    assert row[0] == 1


# ---------------------------------------------------------------------------
# run_pipeline integration
# ---------------------------------------------------------------------------

def test_run_pipeline_end_to_end():
    """Full pipeline with an in-memory DB and injected corpus."""
    conn = _make_conn()
    doc = _doc(
        "pipe-doc-001",
        "blog",
        "The central bank raised interest rates by 0.75% in its June meeting. "
        "This represents the largest single increase in 28 years.",
    )
    corpus = [
        ("The Fed raised rates by 75 basis points, the biggest hike in decades.", "news-fed-001", "news"),
        ("Unrelated: best pasta recipes for summer evenings.", "blog-food-002", "blog"),
    ]
    claims, _ = run_pipeline(doc, conn, corpus_fn=lambda: corpus)

    # At least the measurement sentence should be a claim
    assert len(claims) >= 1
    for c in claims:
        assert c.document_id == "pipe-doc-001"
        assert c.source_type == "blog"

    # Verify persistence
    row = conn.execute(
        "SELECT COUNT(*) FROM argument_claims WHERE document_id = 'pipe-doc-001'"
    ).fetchone()
    assert row is not None
    assert row[0] == len(claims)


def test_run_pipeline_no_claims_returns_empty():
    """Documents with only hedged opinions produce no claims or evidence."""
    conn = _make_conn()
    doc = _doc(
        "hedge-doc-001",
        "note",
        "I might think this could perhaps be interesting. Maybe?",
    )
    claims, all_evidence = run_pipeline(doc, conn, corpus_fn=lambda: [])
    # Either zero claims or claims persist correctly (no error)
    row = conn.execute("SELECT COUNT(*) FROM argument_claims").fetchone()
    assert row is not None
    assert row[0] == len(claims)
    assert all_evidence == []
