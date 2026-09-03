#!/usr/bin/env python3
"""
One-command local demo runner — Issue #298.

Runs the full NeuroNews pipeline offline, in a single Python process,
with no Docker, Kafka, Spark, or internet access required.

Usage:
    python3 scripts/demo.py
    python3 scripts/demo.py --open-api        # start FastAPI after pipeline
    python3 scripts/demo.py --reset           # clear demo data before running
    python3 scripts/demo.py --no-kg           # skip knowledge-graph step
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

DEMO_ARTICLES_FILE = REPO_ROOT / "data" / "demo" / "articles.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _banner(text: str) -> None:
    width = 60
    print("\n" + "=" * width)
    print(f"  {text}")
    print("=" * width)


def _step(text: str) -> None:
    print(f"\n  >> {text}")


def _ok(text: str) -> None:
    print(f"     [OK] {text}")


def _warn(text: str) -> None:
    print(f"     [!!] {text}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Step 1 – ingest demo articles into DuckDB
# ---------------------------------------------------------------------------

def ingest_articles(conn, articles: List[Dict[str, Any]], reset: bool) -> int:
    """Insert demo articles into the documents sink, return count inserted.

    ``news_articles`` is a view over ``documents`` (#909), so writes go through
    the compat writer and the schema is created here for a fresh warehouse.
    """
    from src.database.news_articles_compat import (
        ensure_news_articles_view,
        write_news_articles,
    )

    ensure_news_articles_view(conn)

    if reset:
        conn.execute("DELETE FROM document_enrichments WHERE document_id LIKE 'demo-%'")
        conn.execute("DELETE FROM documents WHERE document_id LIKE 'demo-%'")

    existing = {
        r[0] for r in conn.execute(
            "SELECT id FROM news_articles WHERE id LIKE 'demo-%'"
        ).fetchall()
    }

    rows = [
        {
            "id": art["id"],
            "title": art["title"],
            "url": art["url"],
            "content": art["content"],
            "source": art["source"],
            "category": art["category"],
            "publish_date": art.get(
                "published_date", datetime.now(timezone.utc).isoformat()
            ),
            "sentiment_score": art.get("sentiment_score", 0.0),
            "sentiment_label": art.get("sentiment_label", "neutral"),
        }
        for art in articles
        if art["id"] not in existing
    ]
    return write_news_articles(conn, rows)


# ---------------------------------------------------------------------------
# Step 2 – sentiment analysis
# ---------------------------------------------------------------------------

def run_sentiment(articles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Run sentiment analysis; fall back to bundled scores if model unavailable."""
    results = []
    try:
        from src.nlp.sentiment_analysis import SentimentAnalyzer
        sa = SentimentAnalyzer()
        for art in articles:
            try:
                r = sa.analyze(art["content"][:512])
                score_val = r.get("score")
                score = round(float(score_val) if isinstance(score_val, (int, float)) else abs(art["sentiment_score"]), 3)
                results.append({
                    "id": art["id"],
                    "title": art["title"],
                    "label": str(r.get("label", art["sentiment_label"])),
                    "score": score,
                    "source": art["source"],
                    "category": art["category"],
                })
            except Exception:
                results.append({
                    "id": art["id"],
                    "title": art["title"],
                    "label": art["sentiment_label"],
                    "score": round(abs(art["sentiment_score"]), 3),
                    "source": art["source"],
                    "category": art["category"],
                })
    except Exception as exc:
        _warn(f"SentimentAnalyzer unavailable ({exc}); using bundled scores")
        for art in articles:
            results.append({
                "id": art["id"],
                "title": art["title"],
                "label": art["sentiment_label"],
                "score": round(abs(art["sentiment_score"]), 3),
                "source": art["source"],
                "category": art["category"],
            })
    return results


# ---------------------------------------------------------------------------
# Step 3 – claim / argument mining
# ---------------------------------------------------------------------------

def run_claim_extraction(articles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Extract claims from each article using the trained ClaimDetector."""
    all_claims: List[Dict[str, Any]] = []
    try:
        from src.argument_mining.models import ClaimDetector
        cd = ClaimDetector()
        for art in articles:
            sentences = [s.strip() for s in art["content"].split(". ") if len(s.strip()) > 20]
            for sent in sentences:
                try:
                    pred = cd.predict_text(sent)
                    if pred.is_claim:
                        all_claims.append({
                            "article_id": art["id"],
                            "source": art["source"],
                            "sentence": sent[:120],
                            "confidence": round(pred.confidence, 3),
                        })
                except Exception:
                    pass
    except Exception as exc:
        _warn(f"ClaimDetector unavailable ({exc}); skipping claim extraction")
    return all_claims


# ---------------------------------------------------------------------------
# Step 4 – outlet scoring
# ---------------------------------------------------------------------------

def run_outlet_scoring(conn) -> List[Dict[str, Any]]:
    """Score outlets using the existing DuckDB-backed scorer."""
    results: List[Dict[str, Any]] = []
    try:
        from src.argument_mining.outlet_scorer import compute_outlet_scores
        scores = compute_outlet_scores(conn, date_range="90d")
        for row in scores[:5]:
            results.append({
                "source": row.source,
                "framing_entropy": round(row.frame_diversity, 3),
                "stance_balance": round(row.stance_neutrality, 3),
                "composite_score": round(row.composite_score, 3),
            })
    except Exception as exc:
        _warn(f"Outlet scorer unavailable ({exc})")
        scores = []

    if not results:
        # Fallback: simple article count per source
        db_rows = conn.execute(
            "SELECT source, COUNT(*) FROM news_articles GROUP BY source ORDER BY 2 DESC LIMIT 5"
        ).fetchall()
        for source, n in db_rows:
            results.append({"source": source, "article_count": n})
    return results


# ---------------------------------------------------------------------------
# Step 5 – knowledge graph
# ---------------------------------------------------------------------------

def run_kg_update(articles: List[Dict[str, Any]]) -> Dict[str, int]:
    """Populate an in-process KnowledgeGraphStore from articles + claims."""
    try:
        from src.knowledge_graph.claim_graph import (
            EntityType, KnowledgeGraphStore, Node, build_claim_graph, ExtractedClaim
        )
        from src.argument_mining.models import ClaimDetector

        kg = KnowledgeGraphStore()
        cd = ClaimDetector()

        for art in articles:
            # build_claim_graph requires the source document node to exist.
            kg.add_node(Node(
                type=EntityType.DOCUMENT,
                name=art["title"],
                node_id=art["id"],
                properties={"source": art["source"], "url": art["url"]},
            ))
            sentences = [s.strip() for s in art["content"].split(". ") if len(s.strip()) > 20]
            extracted: List[ExtractedClaim] = []
            for sent in sentences[:5]:
                try:
                    pred = cd.predict_text(sent)
                    if pred.is_claim:
                        extracted.append(ExtractedClaim(
                            text=sent[:200],
                            subject=art["source"],
                            predicate="claims",
                            object=sent[:80],
                            confidence=pred.confidence,
                            source_doc=art["id"],
                        ))
                except Exception:
                    pass
            if extracted:
                try:
                    build_claim_graph(kg, art["id"], extracted)
                except Exception:
                    pass

        return {"nodes": kg.node_count, "triples": kg.triple_count}
    except Exception as exc:
        _warn(f"KG builder unavailable ({exc}); reporting zeros")
        return {"nodes": 0, "triples": 0}


# ---------------------------------------------------------------------------
# Print summary
# ---------------------------------------------------------------------------

def print_summary(
    articles: List[Dict[str, Any]],
    sentiment_results: List[Dict[str, Any]],
    claims: List[Dict[str, Any]],
    outlet_scores: List[Dict[str, Any]],
    kg_stats: Dict[str, int],
    elapsed: float,
) -> None:
    _banner("DEMO PIPELINE SUMMARY")

    sources = Counter(a["source"] for a in articles)
    cats = Counter(a["category"] for a in articles)
    print(f"\n  Articles ingested : {len(articles)}")
    print(f"  Sources           : {len(sources)}")
    print(f"  Categories        : {', '.join(cats.keys())}")

    labels = Counter(r["label"].upper() for r in sentiment_results)
    print(f"\n  Sentiment breakdown:")
    for label, count in sorted(labels.items()):
        bar = "█" * count
        print(f"    {label:<10} {bar} ({count})")

    top_claims = sorted(claims, key=lambda c: c["confidence"], reverse=True)[:5]
    print(f"\n  Claims extracted  : {len(claims)}")
    if top_claims:
        print("  Top claims:")
        for c in top_claims:
            print(f"    [{c['confidence']:.2f}] {c['sentence'][:90]}…")
            print(f"           ↳ {c['source']}")

    print(f"\n  Outlet scores (top {len(outlet_scores)}):")
    for row in outlet_scores:
        if "composite_score" in row:
            print(f"    {row['source']:<24} composite={row['composite_score']:.3f}  "
                  f"entropy={row['framing_entropy']:.3f}")
        else:
            print(f"    {row['source']:<24} articles={row.get('article_count', '?')}")

    print(f"\n  Knowledge graph   : {kg_stats['nodes']} nodes  {kg_stats['triples']} triples")
    print(f"\n  Pipeline time     : {elapsed:.1f}s")
    print("=" * 60)


# ---------------------------------------------------------------------------
# --open-api: start FastAPI server
# ---------------------------------------------------------------------------

def start_api_server() -> None:
    import subprocess
    print("\n  Starting FastAPI on http://localhost:8000 …")
    print("  Press Ctrl-C to stop.\n")
    try:
        subprocess.run(
            [sys.executable, "-m", "uvicorn", "src.api.app:app",
             "--host", "0.0.0.0", "--port", "8000"],
            cwd=str(REPO_ROOT),
        )
    except KeyboardInterrupt:
        print("\n  API server stopped.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="NeuroNews one-command demo runner (offline, no external services)"
    )
    parser.add_argument("--open-api", action="store_true",
                        help="Start FastAPI on port 8000 after pipeline completes")
    parser.add_argument("--reset", action="store_true",
                        help="Delete existing demo rows before re-inserting")
    parser.add_argument("--no-kg", action="store_true",
                        help="Skip knowledge-graph construction (faster)")
    args = parser.parse_args()

    t0 = time.time()

    _banner("NeuroNews Demo Pipeline (offline)")
    print("  No Kafka, Spark, Docker, or internet connection required.\n")

    _step("Loading demo articles …")
    try:
        articles: List[Dict[str, Any]] = json.loads(DEMO_ARTICLES_FILE.read_text())
    except FileNotFoundError:
        print(f"  ERROR: {DEMO_ARTICLES_FILE} not found.", file=sys.stderr)
        return 1
    _ok(f"{len(articles)} articles from {len({a['source'] for a in articles})} sources")

    _step("Connecting to local DuckDB warehouse …")
    from src.database.local_analytics_connector import get_shared_connection
    conn = get_shared_connection()
    _ok("warehouse ready")

    _step("Ingesting articles into DuckDB …")
    n_inserted = ingest_articles(conn, articles, reset=args.reset)
    _ok(f"{n_inserted} new rows inserted (pre-existing rows skipped)")

    _step("Running sentiment analysis …")
    sentiment_results = run_sentiment(articles)
    pos = sum(1 for r in sentiment_results if "POS" in r["label"].upper())
    neg = sum(1 for r in sentiment_results if "NEG" in r["label"].upper())
    _ok(f"{len(sentiment_results)} articles analysed — {pos} positive, {neg} negative")

    _step("Running argument mining (claim extraction) …")
    claims = run_claim_extraction(articles)
    _ok(f"{len(claims)} claims detected")

    _step("Scoring outlets …")
    outlet_scores = run_outlet_scoring(conn)
    _ok(f"{len(outlet_scores)} outlets scored")

    if not args.no_kg:
        _step("Updating knowledge graph …")
        kg_stats = run_kg_update(articles)
        _ok(f"{kg_stats['nodes']} nodes, {kg_stats['triples']} triples")
    else:
        kg_stats = {"nodes": 0, "triples": 0}
        _step("Knowledge graph skipped (--no-kg)")

    elapsed = time.time() - t0
    print_summary(articles, sentiment_results, claims, outlet_scores, kg_stats, elapsed)

    if args.open_api:
        start_api_server()

    return 0


if __name__ == "__main__":
    sys.exit(main())
