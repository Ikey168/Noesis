"""
Position Follow-Through Tracker (issue #111).

For each stored policy position, scans recent news articles for references
to the same (actor, topic) pair and classifies each reference as:
  - reaffirmed  : actor confirmed or doubled down on the original position
  - reversed    : actor changed or abandoned the original position
  - updated     : actor modified or adjusted the original position
  - no_signal   : actor+topic mentioned but no clear follow-through signal

Results are stored in ``position_updates`` and attached to each position
returned by ``GET /api/v1/arguments/positions``.

Run standalone:
    python -m src.argument_mining.position_tracker

Or via the nightly scheduler:
    from src.argument_mining.followthrough_scheduler import schedule_followthrough_job
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Tuple

from src.database.news_articles_compat import corpus_table

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Signal patterns
# ---------------------------------------------------------------------------

_REVERSAL_SIGNALS = re.compile(
    r"\b("
    r"reversed?|abandoned|dropped|scrapped|u-turn|walked\s+back|flip-flop(?:ped)?|"
    r"backtracked?|no\s+longer|retracted|rescinded|cancelled|annulled|"
    r"overturned|retreats?\s+from|changed\s+course|changed\s+(?:their\s+)?position|"
    r"reneged|backed\s+away\s+from|broke\s+(?:his|her|their)\s+promise|"
    r"ditched|shelved|withdrew|stepping\s+back|pulling\s+back"
    r")\b",
    re.IGNORECASE,
)

_UPDATE_SIGNALS = re.compile(
    r"\b("
    r"updated?|modified?|adjusted?|expanded?|extended?|narrowed?|strengthened?|"
    r"weakened?|amended?|revised?|refined?|shifted?|evolved?|softened?|"
    r"hardened?|scaled\s+(?:back|up)|paused?|delayed?|postponed?"
    r")\b",
    re.IGNORECASE,
)

_REAFFIRM_SIGNALS = re.compile(
    r"\b("
    r"reaffirmed?|reiterates?|doubled\s+down|confirmed?|remained?\s+committed|"
    r"maintained?|stood\s+by|upheld|renewed?|insists?|continues?\s+to|"
    r"still\s+plans?|standing\s+firm|held\s+(?:firm|course)|"
    r"reiterated?|restated?|repeated?|re-confirmed?"
    r")\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class FollowthroughRecord:
    update_id: str
    position_id: str
    article_id: str
    update_type: str        # reaffirmed | reversed | updated | no_signal
    evidence_text: str
    confidence: float
    detected_at: str        # ISO UTC timestamp


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _update_id(position_id: str, article_id: str) -> str:
    """Deterministic ID keyed on (position_id, article_id)."""
    h = hashlib.sha1(f"{position_id}|{article_id}".encode()).hexdigest()[:32]
    return f"upd-{h}"


def _split_sentences(text: str) -> List[str]:
    """Split raw text into sentences (mirrors sentences_from_document)."""
    parts = re.split(r"(?<=[.!?])\s+|\n{2,}", text.strip())
    return [p.strip() for p in parts if len(p.strip()) >= 20]


def _actor_mentioned(text: str, actor: str) -> bool:
    """Return True if any ≥4-char word from the actor name appears in the text."""
    words = [w for w in actor.split() if len(w) >= 4]
    if not words:
        return actor.lower() in text.lower()
    low = text.lower()
    return any(w.lower() in low for w in words)


def _topic_mentioned(text: str, keywords: frozenset) -> bool:
    """Return True if any topic keyword appears in the text."""
    low = text.lower()
    return any(kw in low for kw in keywords)


def _classify_sentence(sentence: str) -> Tuple[str, float]:
    """Map a sentence to (update_type, confidence).

    Priority: reversal beats reaffirmation; mixed signals → updated.
    """
    has_reversal = bool(_REVERSAL_SIGNALS.search(sentence))
    has_reaffirm = bool(_REAFFIRM_SIGNALS.search(sentence))
    has_update   = bool(_UPDATE_SIGNALS.search(sentence))

    if has_reversal and not has_reaffirm:
        return "reversed",   0.80
    if has_reaffirm and not has_reversal:
        return "reaffirmed", 0.75
    if has_reversal and has_reaffirm:
        return "updated",    0.50
    if has_update:
        return "updated",    0.65
    return "no_signal", 0.30


def _topic_keywords(topic: str) -> frozenset:
    """Return the keyword frozenset for a topic label from the taxonomy."""
    from src.argument_mining.positions import _TOPIC_TAXONOMY
    for label, kw in _TOPIC_TAXONOMY:
        if label == topic:
            return kw
    return frozenset({topic.lower()})


# ---------------------------------------------------------------------------
# Core follow-through check
# ---------------------------------------------------------------------------


def check_position_followthrough(
    position_id: str,
    actor: str,
    topic: str,
    articles: List[Tuple],  # (article_id, content, publish_date)
) -> List[FollowthroughRecord]:
    """Check one position against a list of (article_id, content, publish_date) tuples.

    Returns at most one FollowthroughRecord per article — the sentence with the
    highest-confidence signal wins.  ``no_signal`` records are produced only when
    the actor+topic are both referenced so we have a traceable mention.
    """
    topic_kw = _topic_keywords(topic)
    records: List[FollowthroughRecord] = []
    now_iso = datetime.now(timezone.utc).isoformat()

    for article_id, content, _pub_date in articles:
        if not content:
            continue
        if not _actor_mentioned(content, actor):
            continue
        if not _topic_mentioned(content, topic_kw):
            continue

        best_type = "no_signal"
        best_conf = 0.30
        best_sent = ""

        for sent in _split_sentences(content):
            if not _actor_mentioned(sent, actor):
                continue
            utype, conf = _classify_sentence(sent)
            if conf > best_conf:
                best_type = utype
                best_conf = conf
                best_sent = sent

        records.append(FollowthroughRecord(
            update_id=_update_id(position_id, article_id),
            position_id=position_id,
            article_id=article_id,
            update_type=best_type,
            evidence_text=(best_sent or content[:200])[:500],
            confidence=round(best_conf, 4),
            detected_at=now_iso,
        ))

    return records


def store_followthrough(records: List[FollowthroughRecord], conn) -> None:
    """Persist FollowthroughRecord list to position_updates. Idempotent (INSERT OR REPLACE)."""
    if not records:
        return
    conn.executemany(
        """
        INSERT OR REPLACE INTO position_updates
            (update_id, position_id, article_id, update_type,
             evidence_text, confidence, detected_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (r.update_id, r.position_id, r.article_id, r.update_type,
             r.evidence_text, r.confidence, r.detected_at)
            for r in records
        ],
    )


def run_followthrough_batch(conn, lock, limit: int = 50) -> dict:
    """Nightly batch: check all stored positions against new articles.

    For each position (up to *limit*), queries articles published after the
    position's ``extracted_at`` timestamp and classifies any relevant mentions.

    Returns:
        {"checked": int, "matched": int, "stored": int}
    """
    with lock:
        positions = conn.execute(
            """
            SELECT position_id, actor, topic, extracted_at
            FROM policy_positions
            ORDER BY extracted_at ASC
            LIMIT ?
            """,
            [limit],
        ).fetchall()

    counts: dict = {"checked": 0, "matched": 0, "stored": 0}

    for position_id, actor, topic, extracted_at in positions:
        counts["checked"] += 1
        since = (extracted_at or "1970-01-01")[:10]

        with lock:
            articles = conn.execute(
                f"""
                SELECT id, content, publish_date
                FROM {corpus_table(conn)}
                WHERE CAST(publish_date AS VARCHAR) > ?
                LIMIT 200
                """,
                [since],
            ).fetchall()

        if not articles:
            continue

        records = check_position_followthrough(position_id, actor, topic, articles)
        if not records:
            continue

        counts["matched"] += len(records)
        with lock:
            store_followthrough(records, conn)
        counts["stored"] += len(records)

    logger.info("run_followthrough_batch: %s", counts)
    return counts


if __name__ == "__main__":
    import threading
    logging.basicConfig(level=logging.INFO)
    from src.database.local_analytics_connector import get_shared_connection

    _conn = get_shared_connection()
    _lock = getattr(_conn, "_lock", None) or threading.Lock()
    result = run_followthrough_batch(_conn, _lock)
    print(f"Done: {result}")
