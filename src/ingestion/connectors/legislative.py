"""
Legislative and court record connector (candidate track #788).

Votes, bill texts, and rulings are structured events that anchor "X voted for Y"
claims — the strongest possible corroborators for the policy-position tracking
the argument miner already does. A mined policy-position claim becomes checkable
against the actor's actual recorded votes.

This module stores vote/ruling records and checks position claims against them,
honesty-enveloped and citing the record. Connection-injected, stdlib only.

See ``docs/architecture/BEYOND_TEXT_ROADMAP.md`` §4.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from src.analytics.honesty import analytic_envelope

# Normalized positions.
FOR = "for"
AGAINST = "against"
ABSTAIN = "abstain"
_POSITIONS = (FOR, AGAINST, ABSTAIN)

_FOR_WORDS = {"for", "yes", "yea", "aye", "support", "supports", "supported", "backed", "in favor", "in favour"}
_AGAINST_WORDS = {"against", "no", "nay", "oppose", "opposes", "opposed", "voted down", "rejected"}
_ABSTAIN_WORDS = {"abstain", "abstained", "abstention", "present", "did not vote"}

_VOTES_DDL = """
CREATE TABLE IF NOT EXISTS vote_records (
    record_id     TEXT PRIMARY KEY,
    actor         TEXT NOT NULL,
    topic         TEXT NOT NULL,
    bill          TEXT,
    position      TEXT NOT NULL,
    date          BIGINT,
    source        TEXT,
    document_id   TEXT
)
"""


def normalize_position(raw: Optional[str]) -> Optional[str]:
    """Map a free-text position/vote to for/against/abstain."""
    if not raw:
        return None
    t = raw.strip().lower()
    if t in _POSITIONS:
        return t
    if any(w in t for w in _ABSTAIN_WORDS):
        return ABSTAIN
    if any(w in t for w in _AGAINST_WORDS):
        return AGAINST
    if any(w in t for w in _FOR_WORDS):
        return FOR
    return None


@dataclass
class VoteRecord:
    actor: str
    topic: str
    position: str
    bill: Optional[str] = None
    date: Optional[int] = None
    source: Optional[str] = None
    document_id: Optional[str] = None


def _table_exists(conn, table: str) -> bool:
    try:
        return bool(conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name = ?",
            [table],
        ).fetchall())
    except Exception:  # noqa: BLE001
        return False


def ensure_schema(conn) -> None:
    conn.execute(_VOTES_DDL)


def record_vote(conn, vote: VoteRecord) -> str:
    """Persist a vote/ruling record (idempotent by actor/topic/bill/date)."""
    ensure_schema(conn)
    pos = normalize_position(vote.position)
    if pos is None:
        raise ValueError(f"unrecognized position {vote.position!r}")
    import hashlib

    key = f"{vote.actor}|{vote.topic}|{vote.bill}|{vote.date}"
    record_id = "vote:" + hashlib.md5(key.encode()).hexdigest()[:16]
    conn.execute(
        """
        INSERT INTO vote_records (record_id, actor, topic, bill, position, date, source, document_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (record_id) DO UPDATE SET
            position = excluded.position, source = excluded.source, document_id = excluded.document_id
        """,
        [record_id, vote.actor, vote.topic, vote.bill, pos, vote.date, vote.source, vote.document_id],
    )
    return record_id


def voting_record(conn, actor: str, topic: Optional[str] = None) -> List[Dict[str, Any]]:
    if not _table_exists(conn, "vote_records"):
        return []
    clauses = ["LOWER(actor) = ?"]
    params: List[Any] = [actor.lower()]
    if topic:
        clauses.append("LOWER(topic) LIKE ?")
        params.append(f"%{topic.lower()}%")
    rows = conn.execute(
        f"SELECT record_id, actor, topic, bill, position, date, source, document_id FROM vote_records WHERE {' AND '.join(clauses)} ORDER BY date DESC NULLS LAST",
        params,
    ).fetchall()
    keys = ["record_id", "actor", "topic", "bill", "position", "date", "source", "document_id"]
    return [dict(zip(keys, r)) for r in rows]


# Position-claim parsing: "X supports/opposes the climate bill".
_CLAIM_RE = re.compile(
    r"^(?P<actor>.+?)\s+(?P<verb>supports?|opposed?|opposes|backed|voted for|voted against|rejected|championed)\s+(?:the\s+)?(?P<topic>.+?)\.?$",
    re.IGNORECASE,
)


@dataclass
class PositionClaim:
    actor: str
    topic: str
    claimed_position: str


def parse_position_claim(text: str) -> Optional[PositionClaim]:
    if not text:
        return None
    m = _CLAIM_RE.match(text.strip())
    if not m:
        return None
    pos = normalize_position(m.group("verb"))
    if pos is None:
        return None
    return PositionClaim(actor=m.group("actor").strip(), topic=m.group("topic").strip(), claimed_position=pos)


def check_position(conn, actor: str, topic: str, claimed_position: str) -> Dict[str, Any]:
    """Check a policy-position claim against the actor's recorded votes."""
    claimed = normalize_position(claimed_position)
    if claimed is None:
        return analytic_envelope(n=0, method="position-vs-record check", assumptions=["unrecognized claimed position"], verdict="unverifiable")
    records = voting_record(conn, actor, topic)
    if not records:
        return analytic_envelope(
            n=0, method="position-vs-record check",
            assumptions=[f"no recorded votes for {actor!r} on {topic!r}"],
            verdict="unverifiable", actor=actor, topic=topic,
        )
    # Use the most recent record on the topic.
    latest = records[0]
    actual = latest["position"]
    verdict = "supported" if actual == claimed else ("contradicted" if actual in (FOR, AGAINST) and claimed in (FOR, AGAINST) else "unverifiable")
    return analytic_envelope(
        n=len(records),
        method="position-vs-record check",
        assumptions=[
            "compared against the actor's most recent recorded vote on the topic",
            "position derived from the roll-call record, cited",
        ],
        verdict=verdict,
        actor=actor,
        topic=topic,
        claimed_position=claimed,
        recorded_position=actual,
        citation={"record_id": latest["record_id"], "bill": latest["bill"], "source": latest["source"], "cited": True},
    )


def check_position_claim(conn, claim_text: str) -> Dict[str, Any]:
    """Parse a position claim from text and check it."""
    parsed = parse_position_claim(claim_text)
    if parsed is None:
        return analytic_envelope(n=0, method="position-vs-record check", assumptions=["not a position claim"], verdict="unverifiable")
    return check_position(conn, parsed.actor, parsed.topic, parsed.claimed_position)
