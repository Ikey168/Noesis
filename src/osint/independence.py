"""Deterministic evidence-independence graph over ingested documents.

The graph keeps publications as evidence while separating their likely
reporting origins.  It deliberately represents uncertainty: strong explicit
or exact-copy signals can form probable origins, similarity can only form a
``likely_dependent`` relation, and incomplete provenance remains ``unknown``.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
import uuid
from collections.abc import Iterable, Mapping, Sequence
from itertools import combinations
from typing import Any

METHOD = "deterministic evidence-independence graph"
METHOD_VERSION = "origin-inference-v1"
SIGNAL_VERSION = "origin-signals-v1"
DEFAULT_NEAR_DUPLICATE_THRESHOLD = 0.78

RELATION_STATES = ("known_independent", "likely_dependent", "unknown")

_WIRE_ALIASES = {
    "ap": "associated-press",
    "associated press": "associated-press",
    "afp": "agence-france-presse",
    "agence france presse": "agence-france-presse",
    "reuters": "reuters",
    "bloomberg": "bloomberg",
}
_WIRE_RE = re.compile(
    r"(?:^|\baccording\s+to\s+|\bsource\s*:\s*|\bvia\s+)"
    r"(reuters|associated\s+press|ap|afp|agence\s+france\s+presse|bloomberg)\b",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://[^\s\]\[()<>\"']+", re.IGNORECASE)
_QUOTE_RE = re.compile(r"[\"“]([^\"”]{24,240})[\"”]")
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)

_DDL = """
CREATE TABLE IF NOT EXISTS noesis_schema_migrations (
    component TEXT NOT NULL,
    version INTEGER NOT NULL,
    applied_at_ms BIGINT NOT NULL,
    PRIMARY KEY (component, version)
);
CREATE TABLE IF NOT EXISTS document_origin_signals (
    document_id TEXT NOT NULL,
    method_version TEXT NOT NULL,
    signals_json TEXT NOT NULL,
    signal_hash TEXT NOT NULL,
    extracted_at_ms BIGINT NOT NULL,
    PRIMARY KEY (document_id, method_version)
);
CREATE TABLE IF NOT EXISTS reporting_origins (
    origin_id TEXT PRIMARY KEY,
    method TEXT NOT NULL,
    method_version TEXT NOT NULL,
    origin_state TEXT NOT NULL CHECK (origin_state IN ('known', 'probable')),
    representative_document_id TEXT NOT NULL,
    member_count INTEGER NOT NULL,
    active BOOLEAN NOT NULL,
    created_at_ms BIGINT NOT NULL,
    updated_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS document_origin_links (
    document_id TEXT NOT NULL,
    method TEXT NOT NULL,
    method_version TEXT NOT NULL,
    origin_id TEXT,
    relation_state TEXT NOT NULL
        CHECK (relation_state IN ('known_independent', 'likely_dependent', 'unknown')),
    confidence DOUBLE NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    confidence_lo DOUBLE NOT NULL CHECK (confidence_lo >= 0 AND confidence_lo <= 1),
    confidence_hi DOUBLE NOT NULL CHECK (confidence_hi >= 0 AND confidence_hi <= 1),
    reason_codes_json TEXT NOT NULL,
    decisive_signals_json TEXT NOT NULL,
    as_of_ms BIGINT NOT NULL,
    run_id TEXT NOT NULL,
    PRIMARY KEY (document_id, method_version)
);
CREATE TABLE IF NOT EXISTS document_origin_link_history (
    history_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL,
    method TEXT NOT NULL,
    method_version TEXT NOT NULL,
    origin_id TEXT,
    relation_state TEXT NOT NULL,
    confidence DOUBLE NOT NULL,
    reason_codes_json TEXT NOT NULL,
    decisive_signals_json TEXT NOT NULL,
    as_of_ms BIGINT NOT NULL,
    run_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS origin_inference_runs (
    run_id TEXT PRIMARY KEY,
    method_version TEXT NOT NULL,
    threshold DOUBLE NOT NULL,
    documents INTEGER NOT NULL,
    probable_origins INTEGER NOT NULL,
    unresolved INTEGER NOT NULL,
    started_at_ms BIGINT NOT NULL,
    completed_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS origin_backfill_progress (
    method_version TEXT PRIMARY KEY,
    cursor_document_id TEXT,
    processed_documents INTEGER NOT NULL,
    total_documents INTEGER NOT NULL,
    status TEXT NOT NULL,
    updated_at_ms BIGINT NOT NULL,
    last_run_id TEXT
);
"""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return default
    return value


def _table_exists(conn: Any, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = ?", [name]
        ).fetchone()
        is not None
    )


def ensure_independence_schema(conn: Any) -> None:
    """Apply the additive independence-graph schema idempotently."""
    conn.execute(_DDL)
    conn.execute(
        "INSERT OR IGNORE INTO noesis_schema_migrations VALUES (?, 1, ?)",
        ["evidence-independence", _now_ms()],
    )


def _normalized_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(text.split())


def _normalized_name(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "-", _normalized_text(value)).strip("-")


def _canonical_url(value: Any) -> str | None:
    if not value:
        return None
    from src.ingestion.canonical import canonicalize_url

    return canonicalize_url(str(value))


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    values = value if isinstance(value, (list, tuple, set)) else [value]
    return sorted({_normalized_text(item) for item in values if _normalized_text(item)})


def _metadata_list(metadata: Mapping[str, Any], *keys: str) -> list[str]:
    found: list[str] = []
    for key in keys:
        found.extend(_string_list(metadata.get(key)))
    return sorted(set(found))


def _hashed_shingles(text: str, size: int = 4) -> list[str]:
    tokens = [token.casefold() for token in _TOKEN_RE.findall(text) if len(token) > 1]
    if not tokens:
        return []
    spans = (
        [" ".join(tokens)]
        if len(tokens) < size
        else [" ".join(tokens[index : index + size]) for index in range(len(tokens) - size + 1)]
    )
    return sorted({hashlib.sha256(span.encode()).hexdigest()[:20] for span in spans})


def _hashed_words(text: str) -> list[str]:
    return sorted(
        {
            hashlib.sha256(token.casefold().encode()).hexdigest()[:20]
            for token in _TOKEN_RE.findall(text)
            if len(token) > 1
        }
    )


def _claim_fingerprints(values: Iterable[Any]) -> list[str]:
    if isinstance(values, (str, bytes)):
        values = [values]
    return sorted(
        {
            hashlib.sha256(_normalized_text(value).encode()).hexdigest()[:20]
            for value in values
            if _normalized_text(value)
        }
    )


def _explicit_upstreams(content: str, metadata: Mapping[str, Any]) -> list[str]:
    explicit = _metadata_list(
        metadata,
        "wire_service",
        "upstream_source",
        "attribution",
        "attributed_to",
    )
    for match in _WIRE_RE.finditer(content[:2_000]):
        explicit.append(match.group(1))
    normalized = []
    for item in explicit:
        name = _normalized_text(item)
        normalized.append(_WIRE_ALIASES.get(name, _normalized_name(name)))
    return sorted({item for item in normalized if item})


def extract_document_signals(document: Mapping[str, Any]) -> dict[str, Any]:
    """Extract traceable signals without making an origin decision."""
    metadata = _json(document.get("metadata"), {})
    if not isinstance(metadata, Mapping):
        metadata = {}
    content = str(document.get("content") or "")
    title = str(document.get("title") or "")
    normalized_content = _normalized_text(content)
    authors = _string_list(_json(document.get("authors"), []))
    bylines = sorted(set(authors + _metadata_list(metadata, "byline", "bylines", "author")))
    outbound = _metadata_list(
        metadata, "source_links", "outbound_links", "citations", "references"
    )
    outbound.extend(_URL_RE.findall(content))
    outbound = sorted({_canonical_url(url) or url for url in outbound if url})
    media_hashes = _metadata_list(
        metadata, "media_hash", "media_hashes", "image_hash", "image_hashes"
    )
    quote_markers = sorted(
        {hashlib.sha256(_normalized_text(value).encode()).hexdigest()[:20] for value in _QUOTE_RE.findall(content)}
    )
    kind = _normalized_name(metadata.get("document_kind") or document.get("source_type"))
    press_release = bool(
        kind in {"press-release", "press_release"}
        or "for immediate release" in normalized_content[:500]
        or metadata.get("is_press_release") is True
    )
    original_reporting = metadata.get("original_reporting") is True
    published = document.get("created_at") or metadata.get("published_at_ms")
    try:
        published_at_ms = int(published) if published is not None else None
    except (TypeError, ValueError):
        published_at_ms = None
    return {
        "signal_version": SIGNAL_VERSION,
        "document_id": str(document.get("document_id") or ""),
        "source_id": _normalized_name(document.get("source_id")),
        "source_type": _normalized_name(document.get("source_type")),
        "canonical_url": _canonical_url(document.get("canonical_url") or document.get("url")),
        "content_hash": hashlib.sha256(normalized_content.encode()).hexdigest() if normalized_content else None,
        "title_hash": hashlib.sha256(_normalized_text(title).encode()).hexdigest() if title else None,
        "word_fingerprints": _hashed_words(f"{title} {content}"),
        "text_shingles": _hashed_shingles(f"{title} {content}"),
        "bylines": bylines,
        "dateline": _normalized_text(metadata.get("dateline")) or None,
        "explicit_upstreams": _explicit_upstreams(content, metadata),
        "outbound_links": outbound,
        "publisher_owner": _normalized_name(metadata.get("publisher_owner")) or None,
        "media_hashes": media_hashes,
        "quote_markers": quote_markers,
        "claim_fingerprints": _claim_fingerprints(
            metadata.get("claim_texts") or metadata.get("claims") or []
        ),
        "published_at_ms": published_at_ms,
        "press_release": press_release,
        "original_reporting": original_reporting,
    }


def _document_row(conn: Any, document_id: str) -> dict[str, Any] | None:
    if not _table_exists(conn, "documents"):
        return None
    row = conn.execute(
        "SELECT document_id, source_type, source_id, url, canonical_url, content_hash,"
        " title, content, authors, metadata, created_at, ingested_at"
        " FROM documents WHERE document_id = ?",
        [document_id],
    ).fetchone()
    if row is None:
        return None
    keys = (
        "document_id",
        "source_type",
        "source_id",
        "url",
        "canonical_url",
        "stored_content_hash",
        "title",
        "content",
        "authors",
        "metadata",
        "created_at",
        "ingested_at",
    )
    return dict(zip(keys, row))


def record_document_signals(
    conn: Any, document_id: str, *, extracted_at_ms: int | None = None
) -> dict[str, Any]:
    """Extract and upsert one document's signals deterministically."""
    ensure_independence_schema(conn)
    document = _document_row(conn, str(document_id))
    if document is None:
        raise ValueError(f"document {document_id!r} not found")
    signals = extract_document_signals(document)
    if _table_exists(conn, "argument_claims"):
        claim_rows = conn.execute(
            "SELECT claim_text FROM argument_claims WHERE document_id = ? ORDER BY claim_id",
            [str(document_id)],
        ).fetchall()
        signals["claim_fingerprints"] = sorted(
            set(signals["claim_fingerprints"])
            | set(_claim_fingerprints(row[0] for row in claim_rows))
        )
    encoded = _canonical(signals)
    conn.execute(
        "INSERT OR REPLACE INTO document_origin_signals VALUES (?, ?, ?, ?, ?)",
        [str(document_id), SIGNAL_VERSION, encoded, _digest(signals), extracted_at_ms or _now_ms()],
    )
    return signals


def document_signals(conn: Any, document_id: str) -> dict[str, Any] | None:
    """Read extracted signals independently of any clustering decision."""
    if not _table_exists(conn, "document_origin_signals"):
        return None
    row = conn.execute(
        "SELECT signals_json FROM document_origin_signals"
        " WHERE document_id = ? AND method_version = ?",
        [str(document_id), SIGNAL_VERSION],
    ).fetchone()
    return _json(row[0], {}) if row else None


def _jaccard(left: Sequence[str], right: Sequence[str]) -> float:
    a, b = set(left), set(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def compare_signals(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    *,
    near_duplicate_threshold: float = DEFAULT_NEAR_DUPLICATE_THRESHOLD,
) -> dict[str, Any]:
    """Return a deterministic, explainable pair decision."""
    word_similarity = _jaccard(
        left.get("word_fingerprints", []), right.get("word_fingerprints", [])
    )
    shingle_similarity = _jaccard(
        left.get("text_shingles", []), right.get("text_shingles", [])
    )
    similarity = round(max(word_similarity, shingle_similarity), 6)
    exact = bool(left.get("content_hash") and left.get("content_hash") == right.get("content_hash"))
    canonical = bool(left.get("canonical_url") and left.get("canonical_url") == right.get("canonical_url"))
    upstreams = sorted(set(left.get("explicit_upstreams", [])) & set(right.get("explicit_upstreams", [])))
    conflicting_upstreams = bool(
        left.get("explicit_upstreams")
        and right.get("explicit_upstreams")
        and not upstreams
    )
    shared_byline = sorted(set(left.get("bylines", [])) & set(right.get("bylines", [])))
    shared_media = sorted(set(left.get("media_hashes", [])) & set(right.get("media_hashes", [])))
    shared_links = sorted(set(left.get("outbound_links", [])) & set(right.get("outbound_links", [])))
    shared_quotes = sorted(set(left.get("quote_markers", [])) & set(right.get("quote_markers", [])))
    shared_claims = sorted(
        set(left.get("claim_fingerprints", []))
        & set(right.get("claim_fingerprints", []))
    )
    same_owner = bool(
        left.get("publisher_owner")
        and left.get("publisher_owner") == right.get("publisher_owner")
    )
    reasons: list[str] = []
    confidence = 0.0
    dependent = False
    if canonical:
        reasons.append("canonical_url_match")
        confidence, dependent = 0.995, True
    if exact:
        reasons.append("exact_content_match")
        confidence, dependent = max(confidence, 0.99), True
    if not dependent and upstreams and similarity >= 0.30:
        reasons.append("explicit_upstream_match")
        if left.get("published_at_ms") is not None and right.get("published_at_ms") is not None:
            reasons.append("timestamp_ordered_syndication")
        confidence, dependent = 0.97, True
    provenance_signal_count = sum(
        bool(value) for value in (shared_byline, shared_media, shared_links, shared_quotes)
    )
    corroborating = provenance_signal_count > 0
    effective_threshold = near_duplicate_threshold * (
        0.75 if provenance_signal_count >= 2 else 1.0
    )
    if (
        not dependent
        and not conflicting_upstreams
        and similarity >= effective_threshold
        and corroborating
    ):
        reasons.append("near_duplicate_with_provenance")
        confidence, dependent = 0.9, True
    if (
        not dependent
        and not conflicting_upstreams
        and similarity >= min(0.99, near_duplicate_threshold + 0.12)
    ):
        reasons.append("high_similarity_copy")
        confidence, dependent = 0.82, True
    if shared_byline:
        reasons.append("shared_byline")
    if shared_media:
        reasons.append("shared_media")
    if shared_links:
        reasons.append("shared_source_link")
    if shared_quotes:
        reasons.append("shared_quoted_passage")
    if shared_claims:
        reasons.append("shared_claim_overlap_nondecisive")
    if same_owner:
        reasons.append("shared_ownership_nondecisive")
    if conflicting_upstreams:
        reasons.append("conflicting_explicit_upstreams")
    return {
        "dependent": dependent,
        "relation_state": "likely_dependent" if dependent else "unknown",
        "confidence": confidence if dependent else 0.5,
        "similarity": similarity,
        "reason_codes": sorted(set(reasons)),
        "signals": {
            "explicit_upstreams": upstreams,
            "shared_bylines": shared_byline,
            "shared_media_hashes": shared_media,
            "shared_source_links": shared_links,
            "shared_quote_markers": shared_quotes,
            "shared_claim_fingerprints": shared_claims,
            "same_owner": same_owner,
            "publication_times_ms": [
                left.get("published_at_ms"),
                right.get("published_at_ms"),
            ],
        },
    }


class _UnionFind:
    def __init__(self, items: Iterable[str]) -> None:
        self.parent = {item: item for item in items}

    def find(self, item: str) -> str:
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != root:
            self.parent[item], item = root, self.parent[item]
        return root

    def union(self, left: str, right: str) -> None:
        a, b = self.find(left), self.find(right)
        if a == b:
            return
        if b < a:
            a, b = b, a
        self.parent[b] = a


def _load_signals(conn: Any) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        "SELECT document_id, signals_json FROM document_origin_signals"
        " WHERE method_version = ? ORDER BY document_id",
        [SIGNAL_VERSION],
    ).fetchall()
    return {str(row[0]): _json(row[1], {}) for row in rows}


def _confidence_band(confidence: float, state: str) -> tuple[float, float]:
    if state == "known_independent":
        return 0.97, 1.0
    if state == "likely_dependent":
        return max(0.0, confidence - 0.1), min(1.0, confidence + 0.05)
    return 0.0, 1.0


def run_origin_inference(
    conn: Any,
    *,
    near_duplicate_threshold: float = DEFAULT_NEAR_DUPLICATE_THRESHOLD,
    as_of_ms: int | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Recompute probable-origin components and preserve prior link history."""
    ensure_independence_schema(conn)
    threshold = float(near_duplicate_threshold)
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("near_duplicate_threshold must be between 0 and 1")
    signals = _load_signals(conn)
    document_ids = sorted(signals)
    uf = _UnionFind(document_ids)
    decisions: dict[tuple[str, str], dict[str, Any]] = {}
    for left, right in combinations(document_ids, 2):
        decision = compare_signals(
            signals[left], signals[right], near_duplicate_threshold=threshold
        )
        decisions[(left, right)] = decision
        if decision["dependent"]:
            uf.union(left, right)
    components: dict[str, list[str]] = {}
    for document_id in document_ids:
        components.setdefault(uf.find(document_id), []).append(document_id)
    observed = as_of_ms or _now_ms()
    run = run_id or f"origin-{uuid.uuid4().hex[:16]}"
    links: list[dict[str, Any]] = []
    origins: list[dict[str, Any]] = []
    for members in sorted(components.values(), key=lambda rows: rows[0]):
        member_set = set(members)
        component_pairs = [
            (pair, decision)
            for pair, decision in decisions.items()
            if decision["dependent"] and set(pair) <= member_set
        ]
        component_decisions = [decision for _pair, decision in component_pairs]
        component_reasons = sorted(
            {reason for decision in component_decisions for reason in decision["reason_codes"]}
        )
        representative = min(
            members,
            key=lambda item: (
                signals[item].get("published_at_ms") is None,
                signals[item].get("published_at_ms") or 0,
                item,
            ),
        )
        clustered = len(members) > 1
        origin_id = (
            "origin:" + hashlib.sha256(f"{METHOD_VERSION}|{members[0]}".encode()).hexdigest()[:24]
            if clustered or signals[members[0]].get("original_reporting")
            else None
        )
        if origin_id:
            known = any(signals[item].get("original_reporting") for item in members)
            origins.append(
                {
                    "origin_id": origin_id,
                    "state": "known" if known else "probable",
                    "representative_document_id": representative,
                    "member_count": len(members),
                }
            )
        for document_id in members:
            signal = signals[document_id]
            if signal.get("original_reporting"):
                state, confidence = "known_independent", 0.99
                reasons = ["explicit_original_reporting"]
            elif clustered:
                state = "likely_dependent"
                confidence = max(
                    (decision["confidence"] for decision in component_decisions),
                    default=0.75,
                )
                reasons = component_reasons or ["component_dependency"]
            else:
                state, confidence = "unknown", 0.5
                reasons = ["insufficient_provenance"]
            lo, hi = _confidence_band(confidence, state)
            pair_evidence = [
                {
                    "other_document_id": pair[1] if pair[0] == document_id else pair[0],
                    "similarity": decision["similarity"],
                    "reason_codes": decision["reason_codes"],
                    "signals": decision["signals"],
                }
                for pair, decision in component_pairs
                if document_id in pair
            ]
            links.append(
                {
                    "document_id": document_id,
                    "origin_id": origin_id,
                    "relation_state": state,
                    "confidence": round(confidence, 4),
                    "confidence_interval": {
                        "value": round(confidence, 4),
                        "lo": round(lo, 4),
                        "hi": round(hi, 4),
                        "level": 0.9,
                    },
                    "reason_codes": reasons,
                    "decisive_signals": {
                        "explicit_upstreams": signal.get("explicit_upstreams", []),
                        "component_reasons": component_reasons,
                        "pair_evidence": pair_evidence,
                    },
                }
            )

    conn.execute("BEGIN TRANSACTION")
    try:
        conn.execute(
            "UPDATE reporting_origins SET active = FALSE, updated_at_ms = ?"
            " WHERE method_version = ?",
            [observed, METHOD_VERSION],
        )
        for origin in origins:
            conn.execute(
                "INSERT INTO reporting_origins VALUES (?, ?, ?, ?, ?, ?, TRUE, ?, ?)"
                " ON CONFLICT (origin_id) DO UPDATE SET"
                " origin_state=excluded.origin_state,"
                " representative_document_id=excluded.representative_document_id,"
                " member_count=excluded.member_count, active=TRUE,"
                " updated_at_ms=excluded.updated_at_ms",
                [
                    origin["origin_id"],
                    METHOD,
                    METHOD_VERSION,
                    origin["state"],
                    origin["representative_document_id"],
                    origin["member_count"],
                    observed,
                    observed,
                ],
            )
        conn.execute(
            "DELETE FROM document_origin_links WHERE method_version = ?",
            [METHOD_VERSION],
        )
        for link in links:
            interval = link["confidence_interval"]
            values = [
                link["document_id"],
                METHOD,
                METHOD_VERSION,
                link["origin_id"],
                link["relation_state"],
                link["confidence"],
                interval["lo"],
                interval["hi"],
                _canonical(link["reason_codes"]),
                _canonical(link["decisive_signals"]),
                observed,
                run,
            ]
            conn.execute(
                "INSERT INTO document_origin_links VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )
            history_id = "history:" + _digest([run, link["document_id"], METHOD_VERSION])[:24]
            conn.execute(
                "INSERT OR IGNORE INTO document_origin_link_history VALUES"
                " (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [history_id, *values[:6], values[8], values[9], observed, run],
            )
        unresolved = sum(link["relation_state"] == "unknown" for link in links)
        conn.execute(
            "INSERT OR REPLACE INTO origin_inference_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [run, METHOD_VERSION, threshold, len(links), len(origins), unresolved, observed, observed],
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {
        "run_id": run,
        "method": METHOD,
        "method_version": METHOD_VERSION,
        "near_duplicate_threshold": threshold,
        "documents": len(links),
        "probable_origins": len(origins),
        "unresolved": sum(link["relation_state"] == "unknown" for link in links),
        "links": links,
        "n": len(links),
        "assumptions": [
            "explicit attribution and exact/canonical identity outrank text similarity",
            "similarity never produces a known-independent relation",
            "shared ownership alone is not evidence of shared reporting",
        ],
    }


def origin_summary(
    conn: Any,
    document_ids: Iterable[str | None],
    *,
    sources: Iterable[str | None] = (),
) -> dict[str, Any]:
    """Summarize publications and origins, falling back honestly when absent."""
    documents = sorted({str(item) for item in document_ids if item})
    source_values = [str(item).strip().casefold() for item in sources if item]
    publication_count = len(documents) if documents else len(source_values)
    if not _table_exists(conn, "document_origin_links"):
        distinct_sources = {item for item in source_values if item != "unknown"}
        unresolved = sum(item == "unknown" for item in source_values)
        return {
            "n": publication_count,
            "publication_count": publication_count,
            "probable_origin_count": len(distinct_sources),
            "independent_source_count": len(distinct_sources),
            "known_independent_count": 0,
            "likely_dependent_count": 0,
            "unresolved_count": unresolved,
            "dependency_evidence": [],
            "method": "distinct-source-fallback-v1",
            "method_version": None,
            "lineage_available": False,
            "assumptions": [
                "lineage tables are absent; normalized source identity is the compatibility fallback",
                "publication and reporting-origin counts may differ when syndication is later inferred",
            ],
        }
    rows = []
    if documents:
        placeholders = ",".join("?" for _ in documents)
        rows = conn.execute(
            "SELECT document_id, origin_id, relation_state, confidence, confidence_lo,"
            " confidence_hi, reason_codes_json, decisive_signals_json, as_of_ms"
            f" FROM document_origin_links WHERE method_version = ? AND document_id IN ({placeholders})"
            " ORDER BY document_id",
            [METHOD_VERSION, *documents],
        ).fetchall()
    evidence = [
        {
            "document_id": row[0],
            "origin_id": row[1],
            "relation_state": row[2],
            "confidence": {
                "value": float(row[3]),
                "lo": float(row[4]),
                "hi": float(row[5]),
                "level": 0.9,
            },
            "reason_codes": _json(row[6], []),
            "decisive_signals": _json(row[7], {}),
            "as_of_ms": int(row[8]),
            "n": 1,
            "method": METHOD,
            "method_version": METHOD_VERSION,
            "assumptions": ["this relation is an origin inference, not proven authorship"],
        }
        for row in rows
    ]
    linked = {row["document_id"] for row in evidence}
    origin_ids = {
        row["origin_id"]
        for row in evidence
        if row["origin_id"] and row["relation_state"] != "unknown"
    }
    unresolved = len(set(documents) - linked) + sum(
        row["relation_state"] == "unknown" for row in evidence
    )
    known_origins = {
        row["origin_id"]
        for row in evidence
        if row["origin_id"] and row["relation_state"] == "known_independent"
    }
    return {
        "n": publication_count,
        "publication_count": publication_count,
        "probable_origin_count": len(origin_ids),
        "independent_source_count": len(origin_ids),
        "known_independent_count": len(known_origins),
        "likely_dependent_count": sum(
            row["relation_state"] == "likely_dependent" for row in evidence
        ),
        "unresolved_count": unresolved,
        "dependency_evidence": evidence,
        "method": METHOD,
        "method_version": METHOD_VERSION,
        "lineage_available": True,
        "assumptions": [
            "probable origins are evidence clusters, not adjudicated authorship",
            "unknown links are excluded from the probable-origin count",
            "each publication remains separately cited",
        ],
    }


def run_origin_backfill(
    conn: Any,
    *,
    batch_size: int = 100,
    near_duplicate_threshold: float = DEFAULT_NEAR_DUPLICATE_THRESHOLD,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Process one idempotent, resumable batch and recompute current origins."""
    ensure_independence_schema(conn)
    size = int(batch_size)
    if not 1 <= size <= 10_000:
        raise ValueError("batch_size must be between 1 and 10000")
    if not _table_exists(conn, "documents"):
        total, pending = 0, []
    else:
        total = int(conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
        pending = [
            row[0]
            for row in conn.execute(
                "SELECT d.document_id FROM documents d"
                " LEFT JOIN document_origin_signals s"
                " ON s.document_id=d.document_id AND s.method_version=?"
                " WHERE s.document_id IS NULL ORDER BY d.document_id LIMIT ?",
                [SIGNAL_VERSION, size],
            ).fetchall()
        ]
    observed = now_ms or _now_ms()
    for document_id in pending:
        record_document_signals(conn, document_id, extracted_at_ms=observed)
    inference = run_origin_inference(
        conn,
        near_duplicate_threshold=near_duplicate_threshold,
        as_of_ms=observed,
    )
    processed = int(
        conn.execute(
            "SELECT COUNT(*) FROM document_origin_signals WHERE method_version = ?",
            [SIGNAL_VERSION],
        ).fetchone()[0]
    )
    status = "complete" if processed >= total else "running"
    cursor = pending[-1] if pending else None
    conn.execute(
        "INSERT OR REPLACE INTO origin_backfill_progress VALUES (?, ?, ?, ?, ?, ?, ?)",
        [METHOD_VERSION, cursor, processed, total, status, observed, inference["run_id"]],
    )
    return {
        "method_version": METHOD_VERSION,
        "status": status,
        "cursor_document_id": cursor,
        "processed_documents": processed,
        "processed_this_batch": len(pending),
        "total_documents": total,
        "remaining_documents": max(0, total - processed),
        "last_run_id": inference["run_id"],
        "probable_origins": inference["probable_origins"],
        "unresolved": inference["unresolved"],
    }


def backfill_status(conn: Any) -> dict[str, Any]:
    ensure_independence_schema(conn)
    row = conn.execute(
        "SELECT cursor_document_id, processed_documents, total_documents, status,"
        " updated_at_ms, last_run_id FROM origin_backfill_progress WHERE method_version = ?",
        [METHOD_VERSION],
    ).fetchone()
    if row is None:
        return {
            "method_version": METHOD_VERSION,
            "status": "not_started",
            "cursor_document_id": None,
            "processed_documents": 0,
            "total_documents": 0,
            "updated_at_ms": None,
            "last_run_id": None,
        }
    return {
        "method_version": METHOD_VERSION,
        "cursor_document_id": row[0],
        "processed_documents": int(row[1]),
        "total_documents": int(row[2]),
        "status": row[3],
        "updated_at_ms": int(row[4]),
        "last_run_id": row[5],
    }


def origin_graph(conn: Any, document_ids: Iterable[str] | None = None) -> dict[str, Any]:
    """Return current origin nodes and typed document relations."""
    if not _table_exists(conn, "document_origin_links"):
        return {
            "method": "distinct-source-fallback-v1",
            "method_version": None,
            "origins": [],
            "links": [],
            "publication_count": 0,
            "probable_origin_count": 0,
            "unresolved_count": 0,
            "n": 0,
            "assumptions": ["origin lineage has not been materialized"],
        }
    wanted = sorted(set(document_ids or []))
    where = ""
    params: list[Any] = [METHOD_VERSION]
    if wanted:
        where = f" AND document_id IN ({','.join('?' for _ in wanted)})"
        params.extend(wanted)
    rows = conn.execute(
        "SELECT document_id, origin_id, relation_state, confidence, confidence_lo,"
        " confidence_hi, reason_codes_json, decisive_signals_json, as_of_ms, run_id"
        " FROM document_origin_links WHERE method_version = ?"
        f"{where} ORDER BY document_id",
        params,
    ).fetchall()
    links = [
        {
            "document_id": row[0],
            "origin_id": row[1],
            "relation_state": row[2],
            "confidence": {"value": row[3], "lo": row[4], "hi": row[5], "level": 0.9},
            "reason_codes": _json(row[6], []),
            "decisive_signals": _json(row[7], {}),
            "as_of_ms": int(row[8]),
            "run_id": row[9],
            "n": 1,
            "method": METHOD,
            "method_version": METHOD_VERSION,
            "assumptions": ["this relation is an origin inference, not proven authorship"],
        }
        for row in rows
    ]
    origin_ids = sorted({row["origin_id"] for row in links if row["origin_id"]})
    origins = []
    if origin_ids:
        placeholders = ",".join("?" for _ in origin_ids)
        origin_rows = conn.execute(
            "SELECT origin_id, origin_state, representative_document_id, member_count"
            f" FROM reporting_origins WHERE active AND origin_id IN ({placeholders})"
            " ORDER BY origin_id",
            origin_ids,
        ).fetchall()
        origins = [
            {
                "origin_id": row[0],
                "origin_state": row[1],
                "representative_document_id": row[2],
                "member_count": int(row[3]),
                "n": int(row[3]),
                "method": METHOD,
                "method_version": METHOD_VERSION,
                "assumptions": ["the node is a probable reporting origin, not proven authorship"],
            }
            for row in origin_rows
        ]
    return {
        "method": METHOD,
        "method_version": METHOD_VERSION,
        "origins": origins,
        "links": links,
        "publication_count": len(links),
        "probable_origin_count": len(origins),
        "unresolved_count": sum(row["relation_state"] == "unknown" for row in links),
        "n": len(links),
        "assumptions": [
            "probable origins preserve every publication and expose unresolved provenance"
        ],
    }


__all__ = [
    "DEFAULT_NEAR_DUPLICATE_THRESHOLD",
    "METHOD",
    "METHOD_VERSION",
    "RELATION_STATES",
    "SIGNAL_VERSION",
    "backfill_status",
    "compare_signals",
    "document_signals",
    "ensure_independence_schema",
    "extract_document_signals",
    "origin_graph",
    "origin_summary",
    "record_document_signals",
    "run_origin_backfill",
    "run_origin_inference",
]
