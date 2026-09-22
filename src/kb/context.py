"""Deterministic, citation-preserving context assembly for knowledge clients."""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

CONTEXT_CONTRACT = "noesis-context-v1"
RRF_K = 60
SURFACES = frozenset(
    {"lexical", "semantic", "document", "passage", "claim", "entity", "graph", "quantitative"}
)
OBJECT_TYPES = frozenset(
    {"document", "passage", "claim", "entity", "relation", "observation"}
)
TRUNCATION_MARKER = "[… truncated]"


class ContextAssemblyError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ContextAssemblyError("bad_request", f"{field_name} must be a list")
    values = tuple(str(item).strip() for item in value)
    if any(not item for item in values) or len(values) != len(set(values)):
        raise ContextAssemblyError(
            "bad_request", f"{field_name} must contain unique non-empty values"
        )
    return values


@dataclass(frozen=True)
class EvidencePolicy:
    mandatory_citations: bool = True
    include_contradictions: bool = True
    allow_unresolved_lineage: bool = True

    @classmethod
    def from_value(cls, value: Any) -> EvidencePolicy:
        raw = dict(value or {})
        return cls(
            mandatory_citations=bool(raw.get("mandatory_citations", True)),
            include_contradictions=bool(raw.get("include_contradictions", True)),
            allow_unresolved_lineage=bool(raw.get("allow_unresolved_lineage", True)),
        )


@dataclass(frozen=True)
class DiversityPolicy:
    max_per_source: int = 3
    max_per_domain: int = 12
    max_per_object_type: int = 8
    max_per_origin: int = 1

    @classmethod
    def from_value(cls, value: Any) -> DiversityPolicy:
        raw = dict(value or {})
        fields = {
            "max_per_source": raw.get("max_per_source", 3),
            "max_per_domain": raw.get("max_per_domain", 12),
            "max_per_object_type": raw.get("max_per_object_type", 8),
            "max_per_origin": raw.get("max_per_origin", 1),
        }
        try:
            parsed = {key: int(item) for key, item in fields.items()}
        except (TypeError, ValueError) as exc:
            raise ContextAssemblyError(
                "bad_request", "diversity limits must be integers"
            ) from exc
        if any(item < 1 or item > 1000 for item in parsed.values()):
            raise ContextAssemblyError(
                "bad_request", "diversity limits must be between 1 and 1000"
            )
        return cls(**parsed)


@dataclass(frozen=True)
class ContextRequest:
    task: str
    query: str
    domains: tuple[str, ...]
    namespace_scope: tuple[str, ...]
    all_authorized: bool
    token_budget: int
    evidence_policy: EvidencePolicy = field(default_factory=EvidencePolicy)
    diversity: DiversityPolicy = field(default_factory=DiversityPolicy)
    recency_after_ms: int | None = None
    required_object_types: tuple[str, ...] = ()
    allowed_surfaces: tuple[str, ...] = tuple(sorted(SURFACES))
    max_candidates: int = 200

    @classmethod
    def from_value(cls, value: Mapping[str, Any]) -> ContextRequest:
        raw = dict(value)
        task = str(raw.get("task") or "").strip()
        query = str(raw.get("query") or task).strip()
        if not task or not query:
            raise ContextAssemblyError("bad_request", "task and query must be non-empty")
        if len(task) > 5000 or len(query) > 5000:
            raise ContextAssemblyError(
                "bad_request", "task and query must be at most 5000 characters"
            )
        try:
            budget = int(raw.get("token_budget"))
            maximum = int(raw.get("max_candidates", 200))
        except (TypeError, ValueError) as exc:
            raise ContextAssemblyError(
                "bad_request", "token_budget and max_candidates must be integers"
            ) from exc
        if not 1 <= budget <= 1_000_000:
            raise ContextAssemblyError(
                "bad_request", "token_budget must be between 1 and 1000000"
            )
        if not 1 <= maximum <= 5000:
            raise ContextAssemblyError(
                "bad_request", "max_candidates must be between 1 and 5000"
            )
        domains = _tuple(raw.get("domains"), "domains")
        namespaces = _tuple(raw.get("namespace_scope"), "namespace_scope")
        all_authorized = bool(raw.get("all_authorized", False))
        if not domains and not namespaces and not all_authorized:
            raise ContextAssemblyError(
                "bad_request",
                "domains, namespace_scope, or all_authorized=true is required",
            )
        if (domains or namespaces) and all_authorized:
            raise ContextAssemblyError(
                "bad_request", "explicit scope and all_authorized are mutually exclusive"
            )
        required = _tuple(raw.get("required_object_types"), "required_object_types")
        unknown_types = set(required) - OBJECT_TYPES
        if unknown_types:
            raise ContextAssemblyError(
                "bad_request", f"unsupported required object types: {sorted(unknown_types)}"
            )
        allowed = _tuple(
            raw.get("allowed_surfaces") or sorted(SURFACES), "allowed_surfaces"
        )
        unknown_surfaces = set(allowed) - SURFACES
        if unknown_surfaces:
            raise ContextAssemblyError(
                "bad_request", f"unsupported retrieval surfaces: {sorted(unknown_surfaces)}"
            )
        recency = raw.get("recency_after_ms")
        if recency is not None:
            try:
                recency = int(recency)
            except (TypeError, ValueError) as exc:
                raise ContextAssemblyError(
                    "bad_request", "recency_after_ms must be integer milliseconds"
                ) from exc
            if recency < 0:
                raise ContextAssemblyError(
                    "bad_request", "recency_after_ms must be non-negative"
                )
        return cls(
            task=task,
            query=query,
            domains=domains,
            namespace_scope=namespaces,
            all_authorized=all_authorized,
            token_budget=budget,
            evidence_policy=EvidencePolicy.from_value(raw.get("evidence_policy")),
            diversity=DiversityPolicy.from_value(raw.get("diversity")),
            recency_after_ms=recency,
            required_object_types=required,
            allowed_surfaces=allowed,
            max_candidates=maximum,
        )

    def to_dict(self) -> dict[str, Any]:
        return json.loads(json.dumps(asdict(self)))


@dataclass
class ContextCandidate:
    candidate_id: str
    object_type: str
    text: str
    title: str | None
    domain: str
    source: str | None
    timestamp_ms: int | None
    retrieval_methods: list[str]
    score_provenance: list[dict[str, Any]]
    citations: list[dict[str, Any]]
    metadata: dict[str, Any]
    normalized_score: float
    lineage_state: str = "unknown"
    origin_id: str | None = None


def estimate_tokens(value: str) -> int:
    """Stable dependency-free estimator: UTF-8 bytes divided by four."""

    return max(1, math.ceil(len(str(value).encode("utf-8")) / 4))


def _stable_id(prefix: str, value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return f"{prefix}:" + hashlib.sha256(encoded.encode()).hexdigest()[:24]


def _text(raw: Mapping[str, Any]) -> str:
    representative = raw.get("representative")
    if isinstance(representative, Mapping):
        for key in ("text", "statement", "content", "title"):
            if representative.get(key):
                return str(representative[key]).strip()
    for key in ("excerpt", "content", "statement", "text", "title", "name"):
        if raw.get(key):
            return str(raw[key]).strip()
    return ""


def _timestamp(raw: Mapping[str, Any]) -> int | None:
    for key in (
        "created_at", "published_at", "ingested_at", "timestamp_ms",
        "observed_at_ms", "as_of_ms",
        "as_of",
    ):
        value = raw.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _locators(raw: Mapping[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    nested = raw.get("citations")
    if isinstance(nested, Sequence) and not isinstance(nested, (str, bytes)):
        for item in nested:
            if isinstance(item, Mapping):
                found.append(dict(item))
    representative = raw.get("representative")
    if isinstance(representative, Mapping):
        found.extend(_locators(representative))
    direct = {
        "document_id": raw.get("document_id") or raw.get("source_document_id"),
        "claim_id": raw.get("claim_id"),
        "observation_id": raw.get("observation_id"),
        "series_id": raw.get("series_id"),
        "as_of": raw.get("as_of") or raw.get("as_of_ms"),
        "period": raw.get("period"),
        "period_from": raw.get("period_from"),
        "period_to": raw.get("period_to"),
        "url": raw.get("url") or raw.get("source_url"),
        "section": raw.get("section"),
        "path": raw.get("path"),
    }
    if any(value is not None for value in direct.values()):
        found.append({key: value for key, value in direct.items() if value is not None})
    result, seen = [], set()
    for locator in found:
        marker = json.dumps(locator, sort_keys=True, separators=(",", ":"), default=str)
        if marker not in seen:
            seen.add(marker)
            result.append(locator)
    return result


def _candidate(
    raw: Mapping[str, Any],
    *,
    domain: str,
    object_type: str,
    method: str,
    rank: int,
    embedding_model: str | None,
) -> ContextCandidate | None:
    content = _text(raw)
    if not content:
        return None
    raw_identifier = (
        raw.get("candidate_id")
        or raw.get("document_id")
        or raw.get("claim_id")
        or raw.get("cluster_id")
        or raw.get("canonical_id")
        or raw.get("relation_id")
        or raw.get("observation_id")
        or _stable_id(object_type, [domain, content])
    )
    identifier = _stable_id("candidate", [domain, object_type, raw_identifier])
    raw_score = next(
        (
            raw.get(key)
            for key in ("score", "similarity", "relevance", "domain_score")
            if raw.get(key) is not None
        ),
        None,
    )
    source = raw.get("source") or raw.get("source_id") or raw.get("provider")
    return ContextCandidate(
        candidate_id=str(identifier),
        object_type=object_type,
        text=content,
        title=str(raw["title"]) if raw.get("title") else None,
        domain=domain,
        source=str(source) if source is not None else None,
        timestamp_ms=_timestamp(raw),
        retrieval_methods=[method],
        score_provenance=[
            {
                "method": method,
                "domain": domain,
                "rank": rank,
                "raw_score": raw_score,
                "embedding_model": embedding_model if method == "semantic" else None,
                "normalization": f"reciprocal_rank:1/({RRF_K}+rank)",
                "probability": False,
            }
        ],
        citations=_locators(raw),
        metadata={
            "raw_object_id": str(raw_identifier),
            "domains": [domain],
            "sources": [str(source)] if source is not None else [],
            "contradicting": bool(
                raw.get("contradicting") or raw.get("relation") == "contradicts"
            ),
        },
        normalized_score=1.0 / (RRF_K + rank),
    )


def _safe_call(
    backing: Any,
    method: str,
    *args: Any,
    trace: list[dict[str, Any]],
    domain: str,
    surface: str,
    **kwargs: Any,
) -> list[Mapping[str, Any]]:
    fn = getattr(backing, method, None)
    if not callable(fn):
        trace.append(
            {"stage": "retrieve", "domain": domain, "surface": surface, "status": "unavailable"}
        )
        return []
    started = time.perf_counter()
    try:
        value = fn(*args, **kwargs)
        rows = list(value or [])
        trace.append(
            {
                "stage": "retrieve", "domain": domain, "surface": surface,
                "status": "ok", "count": len(rows),
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            }
        )
        return [row for row in rows if isinstance(row, Mapping)]
    except Exception as exc:  # noqa: BLE001 - isolate optional backing failures
        trace.append(
            {
                "stage": "retrieve", "domain": domain, "surface": surface,
                "status": "partial_failure", "error_type": type(exc).__name__,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            }
        )
        return []


def _table_exists(conn: Any, table: str) -> bool:
    try:
        return bool(
            conn.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_name=?", [table]
            ).fetchone()
        )
    except Exception:  # noqa: BLE001 - storage capability probe
        return False


def _graph_rows(backing: Any, domain: str, query: str, limit: int) -> list[dict[str, Any]]:
    conn = getattr(backing, "conn", None)
    if conn is None:
        return []
    needle = f"%{query.casefold()}%"
    if _table_exists(conn, "technical_relations"):
        rows = conn.execute(
            "SELECT relation_id, subject_id, relation, object_id, constraint_text, "
            "observed_at_ms, source_url, source_document_id FROM technical_relations "
            "WHERE domain=? AND (lower(subject_id) LIKE ? OR lower(object_id) LIKE ? "
            "OR lower(relation) LIKE ?) ORDER BY observed_at_ms DESC, relation_id LIMIT ?",
            [domain, needle, needle, needle, limit],
        ).fetchall()
        result = [
            {
                "relation_id": row[0],
                "text": f"{row[1]} {row[2]} {row[3]}"
                + (f" ({row[4]})" if row[4] else ""),
                "relation": row[2],
                "timestamp_ms": row[5],
                "source_url": row[6],
                "source_document_id": row[7],
            }
            for row in rows
        ]
        if result:
            return result
    if _table_exists(conn, "political_relations"):
        rows = conn.execute(
            "SELECT relation_id, subject_id, relation_type, object_id, "
            "observed_at_ms, source_document_id FROM political_relations "
            "WHERE domain=? AND (lower(subject_id) LIKE ? OR lower(object_id) LIKE ? "
            "OR lower(relation_type) LIKE ?) ORDER BY observed_at_ms DESC, relation_id LIMIT ?",
            [domain, needle, needle, needle, limit],
        ).fetchall()
        return [
            {
                "relation_id": row[0], "text": f"{row[1]} {row[2]} {row[3]}",
                "relation": row[2], "timestamp_ms": row[4],
                "source_document_id": row[5],
            }
            for row in rows
        ]
    return []


def _hydrate_document_text(backing: Any, row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    if _text(result) and any(result.get(key) for key in ("content", "excerpt")):
        return result
    document_id = result.get("document_id")
    conn = getattr(backing, "conn", None)
    if not document_id or conn is None or not _table_exists(conn, "documents"):
        return result
    try:
        stored = conn.execute(
            "SELECT content, title, source_id, url FROM documents WHERE document_id=?",
            [str(document_id)],
        ).fetchone()
    except Exception:  # noqa: BLE001 - optional hydration must not abort fusion
        return result
    if stored:
        result["content"] = stored[0]
        result["title"] = result.get("title") or stored[1]
        result["source_id"] = result.get("source_id") or stored[2]
        result["url"] = result.get("url") or stored[3]
    return result


def fuse_candidates(
    resolved: Sequence[tuple[str, Any]], request: ContextRequest
) -> tuple[
    list[ContextCandidate], list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]
]:
    """Retrieve all allowed surfaces and normalize ranks, never raw scores."""

    candidates: list[ContextCandidate] = []
    trace: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    models: set[str] = set()
    per_surface = max(1, min(request.max_candidates, 100))
    for domain, backing in resolved:
        model = str(getattr(backing.definition, "embedding_model", "") or "")
        if model:
            models.add(model)
        surfaces: list[tuple[str, str, tuple[Any, ...], dict[str, Any], str]] = []
        allowed = set(request.allowed_surfaces)
        if "lexical" in allowed or "passage" in allowed:
            surfaces.append(("lexical", "search", (request.query,), {"limit": per_surface}, "passage"))
        if "semantic" in allowed:
            surfaces.append(
                ("semantic", "semantic_search", (request.query,), {"limit": per_surface}, "passage")
            )
        if "document" in allowed:
            surfaces.append(("document", "documents", (), {"limit": per_surface}, "document"))
        if "claim" in allowed:
            surfaces.append(("claim", "claims", (), {"limit": per_surface}, "claim"))
        if "entity" in allowed:
            surfaces.append(("entity", "entities", (request.query,), {}, "entity"))
        if "graph" in allowed:
            if callable(getattr(backing, "relations", None)):
                surfaces.append(
                    ("graph", "relations", (request.query,), {"limit": per_surface}, "relation")
                )
            else:
                rows = _graph_rows(backing, domain, request.query, per_surface)
                trace.append(
                    {
                        "stage": "retrieve", "domain": domain, "surface": "graph",
                        "status": "ok" if rows else "unavailable", "count": len(rows),
                    }
                )
                for rank, row in enumerate(rows, 1):
                    item = _candidate(
                        row, domain=domain, object_type="relation", method="graph",
                        rank=rank, embedding_model=None,
                    )
                    if item:
                        candidates.append(item)
        claims_for_quantitative: list[Mapping[str, Any]] = []
        for surface, method, args, kwargs, object_type in surfaces:
            rows = _safe_call(
                backing, method, *args, trace=trace, domain=domain,
                surface=surface, **kwargs,
            )
            if surface == "claim":
                claims_for_quantitative = rows
            for rank, original in enumerate(rows, 1):
                row = (
                    _hydrate_document_text(backing, original)
                    if object_type in {"document", "passage"} else dict(original)
                )
                actual_method = str(row.get("retrieval_method") or surface)
                item = _candidate(
                    row, domain=domain, object_type=object_type, method=actual_method,
                    rank=rank, embedding_model=model or None,
                )
                if item:
                    candidates.append(item)
        if "quantitative" in allowed:
            if not claims_for_quantitative:
                claims_for_quantitative = _safe_call(
                    backing,
                    "claims",
                    limit=per_surface,
                    trace=trace,
                    domain=domain,
                    surface="quantitative_seed_claims",
                )
            quantitative_rows = _safe_call(
                backing, "quantitative_search", request.query, limit=per_surface,
                trace=trace, domain=domain, surface="quantitative",
            )
            if not quantitative_rows and callable(getattr(backing, "quantitative_check", None)):
                for claim in claims_for_quantitative[:per_surface]:
                    representative = claim.get("representative")
                    claim_id = claim.get("claim_id") or (
                        representative.get("claim_id")
                        if isinstance(representative, Mapping) else None
                    )
                    if not claim_id:
                        continue
                    try:
                        value = backing.quantitative_check(str(claim_id))
                    except Exception as exc:  # noqa: BLE001 - partial surface failure
                        trace.append(
                            {
                                "stage": "retrieve", "domain": domain,
                                "surface": "quantitative", "status": "partial_failure",
                                "error_type": type(exc).__name__,
                            }
                        )
                        continue
                    if isinstance(value, Mapping):
                        observation = dict(value)
                        observation.setdefault(
                            "text",
                            json.dumps(observation, sort_keys=True, default=str),
                        )
                        quantitative_rows.append(
                            {
                                "observation_id": f"quantitative:{claim_id}",
                                **observation,
                            }
                        )
            for rank, row in enumerate(quantitative_rows, 1):
                item = _candidate(
                    row, domain=domain, object_type="observation",
                    method="quantitative", rank=rank, embedding_model=None,
                )
                if item:
                    candidates.append(item)
    recency_excluded = 0
    recency_unknown = 0
    if request.recency_after_ms is not None:
        retained = []
        for item in candidates:
            if item.timestamp_ms is None:
                recency_unknown += 1
                exclusions.append(
                    {
                        "candidate_id": item.candidate_id,
                        "reason": "recency_timestamp_missing",
                        "cutoff_ms": request.recency_after_ms,
                    }
                )
                continue
            if item.timestamp_ms < request.recency_after_ms:
                recency_excluded += 1
                exclusions.append(
                    {
                        "candidate_id": item.candidate_id,
                        "reason": "recency_before_cutoff",
                        "timestamp_ms": item.timestamp_ms,
                        "cutoff_ms": request.recency_after_ms,
                    }
                )
                continue
            retained.append(item)
        candidates = retained
        trace.append(
            {
                "stage": "filter",
                "surface": "recency",
                "status": "ok",
                "older_excluded": recency_excluded,
                "unknown_timestamp_excluded": recency_unknown,
            }
        )
    candidates.sort(
        key=lambda item: (
            -item.normalized_score, item.domain, item.object_type, item.candidate_id
        )
    )
    retrieved_count = len(candidates)
    if len(candidates) > request.max_candidates:
        buckets = {
            domain: [item for item in candidates if item.domain == domain]
            for domain, _backing in resolved
        }
        capped = []
        while len(capped) < request.max_candidates and any(buckets.values()):
            for domain, _backing in resolved:
                if buckets[domain] and len(capped) < request.max_candidates:
                    capped.append(buckets[domain].pop(0))
        retained_ids = {item.candidate_id for item in capped}
        exclusions.extend(
            {
                "candidate_id": item.candidate_id,
                "reason": "candidate_cap",
                "cap": request.max_candidates,
            }
            for item in candidates
            if item.candidate_id not in retained_ids
        )
        candidates = capped
    return candidates, trace, {
        "embedding_models": sorted(models),
        "mixed_embedding_spaces": len(models) > 1,
        "score_interpretation": "reciprocal rank only; raw scores are retained but not compared",
        "retrieved_candidates": retrieved_count,
        "candidate_cap": request.max_candidates,
        "recency_older_excluded": recency_excluded,
        "recency_unknown_excluded": recency_unknown,
    }, exclusions


def _merge_candidates(
    candidates: Sequence[ContextCandidate],
) -> tuple[list[ContextCandidate], list[dict[str, Any]]]:
    merged: list[ContextCandidate] = []
    index: dict[tuple[str, str], ContextCandidate] = {}
    exclusions: list[dict[str, Any]] = []
    for candidate in candidates:
        normalized = " ".join(candidate.text.casefold().split())
        key = (candidate.object_type, hashlib.sha256(normalized.encode()).hexdigest())
        existing = index.get(key)
        if existing is None:
            index[key] = candidate
            merged.append(candidate)
            continue
        for locator in candidate.citations:
            if locator not in existing.citations:
                existing.citations.append(locator)
        existing.retrieval_methods = sorted(
            set(existing.retrieval_methods + candidate.retrieval_methods)
        )
        existing.score_provenance.extend(candidate.score_provenance)
        existing.normalized_score += candidate.normalized_score
        existing.metadata["domains"] = sorted(
            set(existing.metadata["domains"] + candidate.metadata["domains"])
        )
        existing.metadata["sources"] = sorted(
            set(existing.metadata["sources"] + candidate.metadata["sources"])
        )
        existing.metadata["contradicting"] = bool(
            existing.metadata["contradicting"]
            or candidate.metadata["contradicting"]
        )
        exclusions.append(
            {
                "candidate_id": candidate.candidate_id,
                "reason": "equivalent_content_deduplicated",
                "retained_as": existing.candidate_id,
                "citations_retained": len(candidate.citations),
            }
        )
    merged.sort(
        key=lambda item: (
            -item.normalized_score, item.domain, item.object_type, item.candidate_id
        )
    )
    return merged, exclusions


def _annotate_lineage(candidates: Sequence[ContextCandidate], resolved: Sequence[tuple[str, Any]]) -> None:
    from src.osint.independence import METHOD_VERSION

    by_domain = {domain: backing for domain, backing in resolved}
    for candidate in candidates:
        document_ids = [
            str(locator["document_id"])
            for locator in candidate.citations if locator.get("document_id")
        ]
        backing = by_domain.get(candidate.domain)
        conn = getattr(backing, "conn", None) if backing is not None else None
        if not document_ids or conn is None or not _table_exists(conn, "document_origin_links"):
            candidate.lineage_state = "unknown"
            continue
        placeholders = ",".join("?" for _ in document_ids)
        rows = conn.execute(
            "SELECT origin_id, relation_state FROM document_origin_links "
            f"WHERE method_version=? AND document_id IN ({placeholders}) "
            "ORDER BY document_id",
            [METHOD_VERSION, *document_ids],
        ).fetchall()
        origin_ids = sorted({str(row[0]) for row in rows if row[0]})
        states = {str(row[1]) for row in rows}
        candidate.origin_id = origin_ids[0] if len(origin_ids) == 1 else None
        if "likely_dependent" in states:
            candidate.lineage_state = "likely_dependent"
        elif states and states == {"known_independent"}:
            candidate.lineage_state = "known_independent"
        else:
            candidate.lineage_state = "unknown"


def _anchor(locator: Mapping[str, Any]) -> str:
    marker = json.dumps(dict(locator), sort_keys=True, separators=(",", ":"), default=str)
    return "[C:" + hashlib.sha256(marker.encode()).hexdigest()[:10] + "]"


def _compressed(
    candidate: ContextCandidate, available_tokens: int
) -> tuple[dict[str, Any] | None, int]:
    anchors = sorted({_anchor(locator) for locator in candidate.citations})
    suffix = " " + " ".join(anchors) if anchors else ""
    full = candidate.text.strip() + suffix
    full_tokens = estimate_tokens(full)
    if full_tokens <= available_tokens:
        rendered, lossy = full, False
    else:
        reserve = estimate_tokens(TRUNCATION_MARKER + suffix)
        if reserve >= available_tokens:
            return None, 0
        sentences = [
            value.strip()
            for value in re.split(r"(?<=[.!?])\s+|\n+", candidate.text)
            if value.strip()
        ]
        retained: list[str] = []
        for sentence in sentences:
            trial = " ".join([*retained, sentence, TRUNCATION_MARKER]).strip() + suffix
            if estimate_tokens(trial) > available_tokens:
                break
            retained.append(sentence)
        if not retained:
            byte_budget = max(1, (available_tokens - reserve) * 4)
            excerpt = candidate.text.encode("utf-8")[:byte_budget].decode("utf-8", "ignore").strip()
            if not excerpt:
                return None, 0
            retained = [excerpt]
        rendered = " ".join(retained + [TRUNCATION_MARKER]).strip() + suffix
        while estimate_tokens(rendered) > available_tokens and retained[0]:
            retained[0] = retained[0][:-1].rstrip()
            rendered = " ".join(retained + [TRUNCATION_MARKER]).strip() + suffix
        if not retained[0]:
            return None, 0
        lossy = True
    used = estimate_tokens(rendered)
    return {
        "candidate_id": candidate.candidate_id,
        "object_type": candidate.object_type,
        "domain": candidate.domain,
        "source": candidate.source,
        "provenance": {
            "domains": candidate.metadata["domains"],
            "sources": candidate.metadata["sources"],
            "raw_object_id": candidate.metadata["raw_object_id"],
        },
        "title": candidate.title,
        "text": rendered,
        "source_text_sha256": hashlib.sha256(candidate.text.encode()).hexdigest(),
        "citation_anchors": anchors,
        "citations": candidate.citations,
        "timestamp_ms": candidate.timestamp_ms,
        "retrieval_methods": candidate.retrieval_methods,
        "score_provenance": candidate.score_provenance,
        "normalized_rank_score": round(candidate.normalized_score, 12),
        "lineage": {
            "state": candidate.lineage_state,
            "origin_id": candidate.origin_id,
            "unresolved": candidate.lineage_state == "unknown",
        },
        "compression": {
            "method": "extractive-first",
            "lossy": lossy,
            "truncation_marker": TRUNCATION_MARKER if lossy else None,
            "unsupported_facts_added": False,
        },
        "estimated_tokens": used,
    }, used


def _select(
    candidates: Sequence[ContextCandidate],
    request: ContextRequest,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str], int, bool]:
    selected: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    gaps: list[str] = []
    counters: dict[str, Counter[str]] = {
        "source": Counter(), "domain": Counter(), "object_type": Counter(), "origin": Counter()
    }
    used = 0
    selected_ids: set[str] = set()
    policy = request.diversity

    def choose(candidate: ContextCandidate, *, required: bool = False) -> bool:
        nonlocal used
        if candidate.candidate_id in selected_ids:
            return True
        if request.evidence_policy.mandatory_citations and not candidate.citations:
            exclusions.append(
                {"candidate_id": candidate.candidate_id, "reason": "mandatory_citation_missing"}
            )
            return False
        if (
            candidate.lineage_state == "unknown"
            and not request.evidence_policy.allow_unresolved_lineage
        ):
            exclusions.append(
                {"candidate_id": candidate.candidate_id, "reason": "unresolved_lineage_disallowed"}
            )
            return False
        source_key = candidate.source or "unknown"
        origin_key = candidate.origin_id or f"candidate:{candidate.candidate_id}"
        limits = (
            ("source", source_key, policy.max_per_source),
            ("domain", candidate.domain, policy.max_per_domain),
            ("object_type", candidate.object_type, policy.max_per_object_type),
            ("origin", origin_key, policy.max_per_origin),
        )
        for dimension, key, maximum in limits:
            if counters[dimension][key] >= maximum:
                exclusions.append(
                    {
                        "candidate_id": candidate.candidate_id,
                        "reason": f"diversity_limit:{dimension}",
                        "value": key,
                    }
                )
                return False
        item, tokens = _compressed(candidate, request.token_budget - used)
        if item is None:
            exclusions.append(
                {
                    "candidate_id": candidate.candidate_id,
                    "reason": "token_budget",
                    "required": required,
                }
            )
            return False
        selected.append(item)
        selected_ids.add(candidate.candidate_id)
        used += tokens
        for dimension, key, _maximum in limits:
            counters[dimension][key] += 1
        return True

    impossible = False
    for object_type in request.required_object_types:
        options = [item for item in candidates if item.object_type == object_type]
        if not options:
            gaps.append(f"required object type {object_type!r} has no candidates")
            impossible = True
        elif not any(choose(item, required=True) for item in options):
            gaps.append(
                f"required object type {object_type!r} cannot fit the evidence policy and token budget"
            )
            impossible = True
    if impossible:
        return [], exclusions, gaps, 0, True
    ordered = sorted(
        candidates,
        key=lambda item: (
            not (
                request.evidence_policy.include_contradictions
                and item.metadata.get("contradicting")
            ),
            -item.normalized_score,
            item.domain,
            item.object_type,
            item.candidate_id,
        ),
    )
    for candidate in ordered:
        choose(candidate)
    if not selected and any(item["reason"] == "token_budget" for item in exclusions):
        gaps.append("no cited candidate can fit the token budget")
        return [], exclusions, gaps, 0, True
    return selected, exclusions, gaps, used, False


def assemble_context(
    resolved: Sequence[tuple[str, Any]],
    request: ContextRequest | Mapping[str, Any],
    *,
    scope_receipt: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble replayable, diverse context under a hard token budget."""

    started = time.perf_counter()
    parsed = (
        request if isinstance(request, ContextRequest) else ContextRequest.from_value(request)
    )
    candidates, trace, fusion, retrieval_exclusions = fuse_candidates(
        resolved, parsed
    )
    merged, dedup_exclusions = _merge_candidates(candidates)
    _annotate_lineage(merged, resolved)
    selected, selection_exclusions, gaps, used, impossible = _select(merged, parsed)
    partial_failures = [
        item for item in trace if item.get("status") == "partial_failure"
    ]
    if not candidates:
        gaps.append("no retrieval surface returned usable candidates")
    if any(item["lineage"]["unresolved"] for item in selected):
        gaps.append("some candidate lineage is unresolved")
    status = (
        "refused"
        if impossible
        else "partial"
        if gaps or partial_failures or not selected
        else "assembled"
    )
    citations = {}
    for item in selected:
        for locator in item["citations"]:
            citations[_anchor(locator)] = locator
    elapsed = round((time.perf_counter() - started) * 1000, 3)
    trace.append(
        {
            "stage": "assemble",
            "status": status,
            "candidate_count": len(candidates),
            "deduplicated_count": len(merged),
            "selected_count": len(selected),
            "elapsed_ms": elapsed,
        }
    )
    return {
        "context_contract": CONTEXT_CONTRACT,
        "status": status,
        "request": parsed.to_dict(),
        "scope": dict(scope_receipt or {}),
        "items": selected,
        "citations": citations,
        "token_accounting": {
            "budget": parsed.token_budget,
            "used": used,
            "remaining": parsed.token_budget - used,
            "estimator": "ceil(utf8_bytes/4)",
            "compliant": used <= parsed.token_budget,
        },
        "exclusions": [
            *retrieval_exclusions,
            *dedup_exclusions,
            *selection_exclusions,
        ],
        "coverage_gaps": sorted(set(gaps)),
        "assembly_trace": trace,
        "fusion": fusion,
        "refusal": (
            {
                "code": "impossible_budget",
                "message": "mandatory evidence cannot fit the requested token budget",
            }
            if impossible else None
        ),
    }


def evaluate_context(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Measure contract quality without claiming factual truth."""

    items = list(payload.get("items") or ())
    citations = dict(payload.get("citations") or {})
    accounting = dict(payload.get("token_accounting") or {})
    anchors = [
        anchor for item in items for anchor in item.get("citation_anchors") or ()
    ]
    preserved = sum(
        1
        for item in items
        for anchor in item.get("citation_anchors") or ()
        if anchor in citations and anchor in str(item.get("text") or "")
    )
    normalized = [" ".join(str(item.get("text") or "").casefold().split()) for item in items]
    duplicates = len(normalized) - len(set(normalized))
    factual = [item for item in items if str(item.get("text") or "").strip()]
    supported = sum(1 for item in factual if item.get("citations"))
    elapsed = next(
        (
            float(item.get("elapsed_ms") or 0)
            for item in reversed(list(payload.get("assembly_trace") or ()))
            if item.get("stage") == "assemble"
        ),
        0.0,
    )
    refused = payload.get("status") == "refused"
    refusal = payload.get("refusal")
    return {
        "evaluation_contract": "noesis-context-evaluation-v1",
        "passed": bool(
            accounting.get("compliant")
            and preserved == len(anchors)
            and duplicates == 0
            and (not refused or isinstance(refusal, Mapping))
        ),
        "metrics": {
            "budget_compliance": {
                "passed": bool(accounting.get("compliant")),
                "used": accounting.get("used"),
                "budget": accounting.get("budget"),
            },
            "citation_preservation": {
                "preserved": preserved,
                "total": len(anchors),
                "rate": preserved / len(anchors) if anchors else 1.0,
            },
            "answer_support": {
                "supported_items": supported,
                "total_items": len(factual),
                "rate": supported / len(factual) if factual else 0.0,
                "interpretation": "locator presence, not factual truth",
            },
            "redundancy": {
                "duplicate_items": duplicates,
                "rate": duplicates / len(items) if items else 0.0,
            },
            "latency_ms": elapsed,
            "refusal_quality": {
                "applicable": refused,
                "explicit_reason": bool(isinstance(refusal, Mapping) and refusal.get("code")),
                "returned_items": len(items),
            },
        },
    }


def evaluate_cases(
    assemble_fn: Any, cases: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    results = []
    for case in cases:
        payload = assemble_fn(case["request"])
        repeated = assemble_fn(case["request"])
        evaluation = evaluate_context(payload)
        errors = []
        if _deterministic_projection(payload) != _deterministic_projection(repeated):
            errors.append("assembly is not deterministic")
        if payload.get("status") != case.get("expected_status"):
            errors.append(
                f"expected status {case.get('expected_status')!r}, got {payload.get('status')!r}"
            )
        selected = {item["candidate_id"] for item in payload.get("items") or ()}
        selected_raw = {
            item.get("provenance", {}).get("raw_object_id")
            for item in payload.get("items") or ()
        }
        missing = set(case.get("required_candidate_ids") or ()) - selected
        forbidden = set(case.get("forbidden_candidate_ids") or ()) & selected
        missing_raw = set(case.get("required_raw_object_ids") or ()) - selected_raw
        forbidden_raw = set(case.get("forbidden_raw_object_ids") or ()) & selected_raw
        if missing:
            errors.append(f"missing candidates: {sorted(missing)}")
        if forbidden:
            errors.append(f"forbidden candidates selected: {sorted(forbidden)}")
        if missing_raw:
            errors.append(f"missing raw objects: {sorted(missing_raw)}")
        if forbidden_raw:
            errors.append(f"forbidden raw objects selected: {sorted(forbidden_raw)}")
        if not evaluation["passed"]:
            errors.append("context evaluation failed")
        results.append(
            {
                "id": str(case.get("id") or len(results)),
                "passed": not errors,
                "errors": errors,
                "evaluation": evaluation,
            }
        )
    return {
        "evaluation_contract": "noesis-context-regression-v1",
        "cases": results,
        "passed": bool(results) and all(item["passed"] for item in results),
        "n": len(results),
    }


def _deterministic_projection(payload: Mapping[str, Any]) -> dict[str, Any]:
    projected = json.loads(json.dumps(payload, sort_keys=True, default=str))
    for entry in projected.get("assembly_trace") or ():
        entry.pop("elapsed_ms", None)
    return projected


__all__ = [
    "CONTEXT_CONTRACT",
    "OBJECT_TYPES",
    "SURFACES",
    "TRUNCATION_MARKER",
    "ContextAssemblyError",
    "ContextCandidate",
    "ContextRequest",
    "DiversityPolicy",
    "EvidencePolicy",
    "assemble_context",
    "estimate_tokens",
    "evaluate_cases",
    "evaluate_context",
    "fuse_candidates",
]
