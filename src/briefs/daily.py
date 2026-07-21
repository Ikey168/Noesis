"""
The daily brief: one composed digest over everything the warehouse knows.

For each topic in an interest profile this gathers, from the source-agnostic
corpus view:

* recent **coverage** (news + blog documents in the window, with sentiment and
  per-document summaries where the summary sink has them),
* **key claims** mined from that coverage, fact-check verdicts first,
* **contradictions** where the public record disagrees with itself,
* a day-bucketed **timeline** of cited claims (via the OSINT reconstructor),
* **deeper reading** — papers, books, transcripts and notes on the same topic,
  found by keyword and (when an embedding sink exists) by vector similarity to
  the day's top story.

Sections that come back empty carry a ``note`` naming the pipeline that feeds
them, so an under-populated warehouse tells the reader what to ingest next
instead of silently showing nothing. Read-only; the connection is injected.

Usage (standalone):
    python -m src.briefs.daily --format markdown
    python -m src.briefs.daily --hours 48 --format json --out brief.json
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

from src.briefs.profile import InterestProfile, InterestTopic, load_profile
from src.database.news_articles_compat import corpus_table

# Source types that count as "the news of the day" versus "deeper reading".
CURRENT_TYPES = ("news", "blog")
DEEP_TYPES = ("paper", "book", "transcript", "note", "filing")


def _table_exists(conn, name: str) -> bool:
    try:
        return bool(conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = ?", [name]
        ).fetchone())
    except Exception:
        return False


def _has_source_type(conn, table: str) -> bool:
    try:
        return bool(conn.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = ? AND column_name = 'source_type'", [table]
        ).fetchone())
    except Exception:
        return False


def _keyword_clause(fields: Sequence[str], keywords: Sequence[str]):
    """OR of ``field ILIKE %kw%`` over every (field, keyword) pair."""
    clauses, params = [], []
    for kw in keywords:
        per_kw = " OR ".join(f"{f} ILIKE ?" for f in fields)
        clauses.append(f"({per_kw})")
        params.extend([f"%{kw}%"] * len(fields))
    return "(" + " OR ".join(clauses) + ")", params


def _summaries(conn, doc_ids: Sequence[str]) -> Dict[str, str]:
    if not doc_ids or not _table_exists(conn, "document_summaries"):
        return {}
    ph = ", ".join("?" for _ in doc_ids)
    rows = conn.execute(
        f"SELECT document_id, summary FROM document_summaries WHERE document_id IN ({ph})",
        list(doc_ids),
    ).fetchall()
    return {r[0]: r[1] for r in rows if r[1]}


# --------------------------------------------------------------------------- #
# Per-topic sections
# --------------------------------------------------------------------------- #

def _coverage(conn, tbl: str, typed: bool, topic: InterestTopic,
              cutoff: datetime, limit: int) -> Dict[str, Any]:
    kw_sql, kw_params = _keyword_clause(("title", "category", "content"), topic.keywords)
    type_sql = f"AND source_type IN ({', '.join('?' for _ in CURRENT_TYPES)})" if typed else ""
    rows = conn.execute(
        f"""
        SELECT id, title, url, source, category,
               {'source_type' if typed else "'news'"} AS source_type,
               STRFTIME(publish_date, '%Y-%m-%d %H:%M') AS publish_date,
               COALESCE(sentiment_label, 'neutral') AS sentiment_label,
               COALESCE(sentiment_score, 0.0) AS sentiment_score
        FROM {tbl}
        WHERE {kw_sql} AND publish_date >= ? {type_sql}
        ORDER BY publish_date DESC
        LIMIT ?
        """,
        kw_params + [cutoff] + (list(CURRENT_TYPES) if typed else []) + [int(limit)],
    ).fetchall()

    articles = [
        {"document_id": r[0], "title": r[1], "url": r[2],
         "source": r[3] or "unknown", "category": r[4],
         "source_type": r[5], "publish_date": r[6],
         "sentiment_label": r[7], "sentiment_score": round(float(r[8]), 3)}
        for r in rows
    ]
    summaries = _summaries(conn, [a["document_id"] for a in articles])
    for a in articles:
        if a["document_id"] in summaries:
            a["summary"] = summaries[a["document_id"]]

    total = len(articles)
    pos = sum(1 for a in articles if a["sentiment_label"] == "positive")
    neg = sum(1 for a in articles if a["sentiment_label"] == "negative")
    by_source: Dict[str, int] = {}
    for a in articles:
        by_source[a["source"]] = by_source.get(a["source"], 0) + 1

    section: Dict[str, Any] = {
        "articles": articles,
        "count": total,
        "sentiment": {"positive": pos, "negative": neg, "neutral": total - pos - neg},
        "top_sources": [s for s, _ in sorted(by_source.items(), key=lambda x: -x[1])[:5]],
    }
    if not total:
        section["note"] = ("no news/blog documents matched in the window — run a "
                           "scraper (python -m src.scraper.run --spider bbc) or "
                           "harvest a blog watchlist (blog_mcp)")
    return section


def _claims(conn, topic: InterestTopic, article_ids: Sequence[str],
            limit: int) -> Dict[str, Any]:
    if not _table_exists(conn, "argument_claims"):
        return {"claims": [], "count": 0,
                "note": "no claim layer — run the argument-mining batch "
                        "(airflow news_pipeline, or pipeline_mcp)"}
    kw_sql, params = _keyword_clause(("claim_text",), topic.keywords)
    where = kw_sql
    if article_ids:
        where = f"({kw_sql} OR document_id IN ({', '.join('?' for _ in article_ids)}))"
        params = params + list(article_ids)
    rows = conn.execute(
        f"""
        SELECT claim_id, claim_text, document_id, source_type, confidence,
               factcheck_verdict, factcheck_publisher, factcheck_url
        FROM argument_claims
        WHERE {where}
        ORDER BY (factcheck_verdict IS NOT NULL) DESC, confidence DESC NULLS LAST
        LIMIT ?
        """,
        params + [int(limit)],
    ).fetchall()
    claims = [
        {"claim_id": r[0], "text": r[1], "document_id": r[2], "source_type": r[3],
         "confidence": None if r[4] is None else round(float(r[4]), 3),
         "verdict": r[5], "verdict_publisher": r[6], "verdict_url": r[7]}
        for r in rows
    ]
    section: Dict[str, Any] = {"claims": claims, "count": len(claims)}
    if not claims:
        section["note"] = ("no mined claims matched — run the argument-mining "
                           "batch over the ingested corpus")
    return section


def _contradictions(conn, topic: InterestTopic, limit: int) -> Dict[str, Any]:
    if not (_table_exists(conn, "claim_conflicts")
            and _table_exists(conn, "argument_claims")):
        return {"conflicts": [], "count": 0}
    kw_sql, params = _keyword_clause(
        ("k.topic", "a.claim_text", "b.claim_text"), topic.keywords)
    rows = conn.execute(
        f"""
        SELECT a.claim_text, b.claim_text, k.conflict_type, k.topic,
               k.source_type_a, k.source_type_b
        FROM claim_conflicts k
        JOIN argument_claims a ON a.claim_id = k.claim_id_a
        JOIN argument_claims b ON b.claim_id = k.claim_id_b
        WHERE {kw_sql}
        LIMIT ?
        """,
        params + [int(limit)],
    ).fetchall()
    conflicts = [
        {"claim_a": r[0], "claim_b": r[1], "conflict_type": r[2], "topic": r[3],
         "source_type_a": r[4], "source_type_b": r[5]}
        for r in rows
    ]
    return {"conflicts": conflicts, "count": len(conflicts)}


def _timeline(conn, topic: InterestTopic, max_events: int) -> Dict[str, Any]:
    try:
        from src.osint.timeline import timeline_reconstruct
        result = timeline_reconstruct(conn, topic=topic.keywords[0], limit=200)
    except Exception:
        return {"events": [], "count": 0}
    events = result.get("events") or []
    # Most recent days matter most in a daily brief.
    events = [e for e in events if e.get("date") != "undated"][-max_events:]
    return {"events": events, "count": len(events),
            "keyword": topic.keywords[0]}


def _deep_reading(conn, tbl: str, typed: bool, topic: InterestTopic,
                  top_article_id: Optional[str], limit: int) -> Dict[str, Any]:
    if not typed:
        return {"documents": [], "count": 0,
                "note": "corpus view has no source_type — deeper reading needs "
                        "the unified documents sink (see news_articles_compat)"}
    kw_sql, kw_params = _keyword_clause(("title", "category", "content"), topic.keywords)
    rows = conn.execute(
        f"""
        SELECT id, title, url, source, source_type,
               STRFTIME(publish_date, '%Y-%m-%d') AS publish_date
        FROM {tbl}
        WHERE {kw_sql} AND source_type IN ({', '.join('?' for _ in DEEP_TYPES)})
        ORDER BY publish_date DESC NULLS LAST
        LIMIT ?
        """,
        kw_params + list(DEEP_TYPES) + [int(limit)],
    ).fetchall()
    docs = [
        {"document_id": r[0], "title": r[1], "url": r[2],
         "source": r[3] or "unknown", "source_type": r[4],
         "publish_date": r[5], "matched_by": "keyword"}
        for r in rows
    ]

    # Vector-related deep documents: similar_documents needs no query model, so
    # it works offline whenever the embedding sink is populated.
    if top_article_id and len(docs) < limit and _table_exists(conn, "document_embeddings"):
        try:
            from src.analytics.semantic_search import similar_documents
            hits = (similar_documents(conn, top_article_id, top_k=20) or {}).get("results") or []
            have = {d["document_id"] for d in docs}
            hit_ids = [h["document_id"] for h in hits if h["document_id"] not in have]
            if hit_ids:
                ph = ", ".join("?" for _ in hit_ids)
                type_rows = conn.execute(
                    f"SELECT id, source_type FROM {tbl} WHERE id IN ({ph})", hit_ids
                ).fetchall()
                deep_ids = {r[0] for r in type_rows if r[1] in DEEP_TYPES}
                for h in hits:
                    if h["document_id"] in deep_ids and len(docs) < limit:
                        docs.append({
                            "document_id": h["document_id"], "title": h.get("title"),
                            "url": h.get("url"), "source": h.get("source") or "unknown",
                            "source_type": "deep", "publish_date": None,
                            "matched_by": f"embedding (score {h.get('score')})",
                        })
        except Exception:
            pass

    summaries = _summaries(conn, [d["document_id"] for d in docs])
    for d in docs:
        if d["document_id"] in summaries:
            d["summary"] = summaries[d["document_id"]]

    section: Dict[str, Any] = {"documents": docs, "count": len(docs)}
    if not docs:
        section["note"] = ("no papers/books/transcripts matched — ingest deeper "
                           "sources with the document connectors "
                           "(src/ingestion/connectors) to ground the news")
    return section


# --------------------------------------------------------------------------- #
# The brief
# --------------------------------------------------------------------------- #

def generate_daily_brief(
    conn,
    profile: Optional[InterestProfile] = None,
    window_hours: Optional[int] = None,
    top_articles: int = 8,
    top_claims: int = 6,
    top_deep: int = 5,
    max_timeline_events: int = 5,
) -> Dict[str, Any]:
    """Compose the daily brief as a JSON-serialisable dict. Read-only."""
    profile = profile or load_profile()
    hours = int(window_hours or profile.window_hours or 24)
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours)

    tbl = corpus_table(conn)
    typed = _has_source_type(conn, tbl)

    topics = []
    for topic in profile.topics:
        coverage = _coverage(conn, tbl, typed, topic, cutoff, top_articles)
        article_ids = [a["document_id"] for a in coverage["articles"]]
        top_id = article_ids[0] if article_ids else None
        topics.append({
            "name": topic.name,
            "keywords": topic.keywords,
            "coverage": coverage,
            "claims": _claims(conn, topic, article_ids, top_claims),
            "contradictions": _contradictions(conn, topic, top_claims),
            "timeline": _timeline(conn, topic, max_timeline_events),
            "deep_reading": _deep_reading(conn, tbl, typed, topic, top_id, top_deep),
        })

    # Overview: what arrived in the window, by source type.
    if typed:
        counts = conn.execute(
            f"SELECT source_type, COUNT(*) FROM {tbl} "
            "WHERE publish_date >= ? GROUP BY source_type ORDER BY 2 DESC",
            [cutoff],
        ).fetchall()
    else:
        n = conn.execute(
            f"SELECT COUNT(*) FROM {tbl} WHERE publish_date >= ?", [cutoff]
        ).fetchone()[0]
        counts = [("news", n)]
    ingested = {r[0]: int(r[1]) for r in counts}

    overview: Dict[str, Any] = {
        "window_hours": hours,
        "ingested_by_source_type": ingested,
        "ingested_total": sum(ingested.values()),
    }
    if not overview["ingested_total"]:
        overview["note"] = ("nothing ingested in the window — the brief only "
                            "gets useful once connectors feed the corpus; start "
                            "with a scraper run or a blog watchlist harvest")

    return {
        "profile": profile.name,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "overview": overview,
        "topics": topics,
    }


# --------------------------------------------------------------------------- #
# Markdown renderer
# --------------------------------------------------------------------------- #

def _md_article(a: Dict[str, Any]) -> str:
    title = a.get("title") or a["document_id"]
    head = f"[{title}]({a['url']})" if a.get("url") else title
    line = f"- {head} — {a['source']}, {a.get('publish_date') or 'undated'} " \
           f"({a['sentiment_label']})"
    if a.get("summary"):
        line += f"\n  - {a['summary']}"
    return line


def render_markdown(brief: Dict[str, Any]) -> str:
    out: List[str] = []
    ov = brief["overview"]
    out.append(f"# Daily Brief — {brief['generated_at']}")
    out.append(f"_Profile: {brief['profile']} · window: last {ov['window_hours']}h_")
    out.append("")
    out.append("## Overview")
    if ov.get("ingested_total"):
        parts = ", ".join(f"{n} {t}" for t, n in ov["ingested_by_source_type"].items())
        out.append(f"Ingested in window: **{ov['ingested_total']}** documents ({parts}).")
    if ov.get("note"):
        out.append(f"> {ov['note']}")
    out.append("")

    for topic in brief["topics"]:
        out.append(f"## {topic['name']}")
        cov = topic["coverage"]
        if cov["count"]:
            s = cov["sentiment"]
            out.append(f"**Coverage** ({cov['count']} pieces · "
                       f"+{s['positive']}/−{s['negative']}/±{s['neutral']} · "
                       f"top sources: {', '.join(cov['top_sources'])})")
            out.extend(_md_article(a) for a in cov["articles"])
        elif cov.get("note"):
            out.append(f"> {cov['note']}")
        out.append("")

        claims = topic["claims"]
        if claims["count"]:
            out.append("**Key claims**")
            for c in claims["claims"]:
                verdict = f" — verdict: **{c['verdict']}**" if c.get("verdict") else ""
                conf = f" (confidence {c['confidence']})" if c.get("confidence") is not None else ""
                out.append(f"- {c['text']}{conf}{verdict}")
            out.append("")

        conflicts = topic["contradictions"]
        if conflicts["count"]:
            out.append("**Contradictions on record**")
            for k in conflicts["conflicts"]:
                out.append(f"- \"{k['claim_a']}\" **vs** \"{k['claim_b']}\" "
                           f"({k['conflict_type']})")
            out.append("")

        tl = topic["timeline"]
        if tl["count"]:
            out.append(f"**Timeline** (keyword: {tl['keyword']})")
            for e in tl["events"]:
                lead = e["entries"][0]["text"] if e.get("entries") else ""
                out.append(f"- {e['date']}: {lead} "
                           f"({e['claim_count']} claims, "
                           f"{e['corroboration_density']} independent sources)")
            out.append("")

        deep = topic["deep_reading"]
        if deep["count"]:
            out.append("**Deeper reading**")
            for d in deep["documents"]:
                title = d.get("title") or d["document_id"]
                head = f"[{title}]({d['url']})" if d.get("url") else title
                out.append(f"- {head} — {d['source_type']}, {d['source']} "
                           f"(matched by {d['matched_by']})")
                if d.get("summary"):
                    out.append(f"  - {d['summary']}")
        elif deep.get("note"):
            out.append(f"> {deep['note']}")
        out.append("")

    return "\n".join(out)


# --------------------------------------------------------------------------- #
# CLI entry point
# --------------------------------------------------------------------------- #

def _cli():
    import argparse
    import pathlib
    import sys

    parser = argparse.ArgumentParser(description="Generate the Noesis daily brief")
    parser.add_argument("--profile", help="Path to an interest-profile JSON file")
    parser.add_argument("--hours", type=int, help="Window in hours (default: profile setting)")
    parser.add_argument("--format", choices=["markdown", "json"], default="markdown")
    parser.add_argument("--out", default="-", help="Output path, or - for stdout")
    args = parser.parse_args()

    from src.database.local_analytics_connector import get_shared_connection, _LOCK

    conn = get_shared_connection()
    with _LOCK:
        brief = generate_daily_brief(
            conn, profile=load_profile(args.profile), window_hours=args.hours
        )

    text = (json.dumps(brief, indent=2) if args.format == "json"
            else render_markdown(brief))
    if args.out == "-":
        sys.stdout.write(text + "\n")
    else:
        pathlib.Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"{args.format} → {args.out}", file=sys.stderr)


if __name__ == "__main__":
    _cli()
