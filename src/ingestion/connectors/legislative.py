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

import csv
import hashlib
import io
import json
import os
import re
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from services.ingest.common.document_model import Document
from src.analytics.honesty import analytic_envelope
from src.ingestion.connectors.base import Connector, PermanentFetchError, RawDocument, SourceRef
from src.ingestion.connectors.registry import register_connector

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


# ---------------------------------------------------------------------------
# Registered ingestion connector
# ---------------------------------------------------------------------------

LEGISLATIVE_SOURCES_ENV = "NOESIS_LEGISLATIVE_SOURCES"


def _source_locators() -> List[str]:
    return [value.strip() for value in os.getenv(LEGISLATIVE_SOURCES_ENV, "").split(",")
            if value.strip()]


def _payload_rows(content: str, content_type: str = "") -> List[Dict[str, Any]]:
    stripped = content.lstrip()
    if "csv" in content_type or (not stripped.startswith(("{", "[")) and "\t" not in stripped):
        try:
            return [dict(row) for row in csv.DictReader(io.StringIO(content))]
        except csv.Error:
            pass
    if stripped.startswith("["):
        data = json.loads(content)
        return [dict(row) for row in data]
    if stripped.startswith("{"):
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return [json.loads(line) for line in content.splitlines() if line.strip()]
        if isinstance(data, dict):
            for key in ("records", "votes", "results", "data"):
                if isinstance(data.get(key), list):
                    return [dict(row) for row in data[key]]
            return [data]
    return [json.loads(line) for line in content.splitlines() if line.strip()]


def _first(row: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def _epoch_ms(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        number = int(value)
        return number if number > 10_000_000_000 else number * 1000
    from datetime import datetime
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


@register_connector
class LegislativeConnector(Connector):
    """Ingest JSON/JSONL/CSV roll-call exports into canonical documents.

    Sources come from an explicit query, constructor injection, or the
    comma-separated ``NOESIS_LEGISLATIVE_SOURCES`` setting. HTTPS and local
    files share the same deterministic parser and are fully fixture-testable.
    """

    name = "legislative"
    source_type = "note"

    def __init__(self, sources: Optional[List[str]] = None, opener=None):
        self._sources = list(sources) if sources is not None else None
        self._opener = opener or urllib.request.urlopen

    def discover(self, query: Optional[Any] = None):
        if query is None:
            sources = self._sources if self._sources is not None else _source_locators()
        elif isinstance(query, (str, Path)):
            sources = [str(query)]
        else:
            sources = [str(value) for value in query]
        for locator in sources:
            yield SourceRef(locator=locator, metadata={"source_id": f"legislative:{locator}"})

    def fetch(self, ref: SourceRef) -> RawDocument:
        if ref.locator.startswith(("https://", "http://")):
            request = urllib.request.Request(
                ref.locator, headers={"User-Agent": "Noesis/1.0 legislative connector"}
            )
            try:
                with self._opener(request, timeout=30) as response:
                    payload = response.read()
                    content_type = response.headers.get_content_type()
            except Exception as exc:
                raise PermanentFetchError(f"could not fetch legislative export: {exc}") from exc
            return RawDocument(ref=ref, content=payload, content_type=content_type)
        path = Path(ref.locator).expanduser()
        if not path.is_file():
            raise PermanentFetchError(f"legislative export not found: {path}")
        content_type = "text/csv" if path.suffix.lower() == ".csv" else "application/json"
        return RawDocument(ref=ref, content=path.read_bytes(), content_type=content_type)

    def parse(self, raw: RawDocument) -> List[Document]:
        text = raw.content.decode("utf-8-sig") if isinstance(raw.content, bytes) else raw.content
        documents: List[Document] = []
        for index, row in enumerate(_payload_rows(text, raw.content_type or "")):
            actor = str(_first(row, "actor", "member", "legislator", "name") or "").strip()
            raw_position = str(_first(row, "position", "vote", "choice", "result") or "").strip()
            position = normalize_position(raw_position)
            bill = str(_first(row, "bill", "bill_id", "measure", "motion") or "").strip()
            topic = str(_first(row, "topic", "subject", "title", "description") or bill).strip()
            if not actor or not topic or position is None:
                raise ValueError(
                    f"record {index} needs actor, topic/bill, and a recognized position"
                )
            date = _epoch_ms(_first(row, "date", "voted_at", "timestamp"))
            source = str(_first(row, "source", "source_url", "roll_call_url")
                         or raw.ref.locator)
            external_id = str(_first(row, "id", "record_id", "vote_id") or index)
            digest = hashlib.sha256(
                f"{raw.ref.locator}|{external_id}|{actor}|{topic}|{date}".encode()
            ).hexdigest()[:20]
            doc_id = f"legislative:{digest}"
            verb = "supported" if position == FOR else "opposed" if position == AGAINST else "abstained on"
            title = f"{actor}: {bill or topic}"
            content = f"{actor} {verb} {bill or topic}."
            documents.append(Document(
                document_id=doc_id, source_type="note", language="en",
                ingested_at=raw.fetched_at or int(time.time() * 1000),
                created_at=date, source_id="legislative", url=source,
                title=title, content=content,
                metadata={
                    "record_type": "legislative_vote", "actor": actor,
                    "topic": topic, "bill": bill or None, "position": position,
                    "date": date, "source": source, "external_id": external_id,
                },
            ))
        return documents
