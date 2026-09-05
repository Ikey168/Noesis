"""One deterministic query plane across local, temporal, memory, and remote knowledge."""

from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import json
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

REQUEST_CONTRACT = "noesis-knowledge-query-request-v1"
PLAN_CONTRACT = "noesis-knowledge-query-plan-v1"
RESULT_CONTRACT = "noesis-knowledge-query-result-v1"
CAPABILITY_CONTRACT = "noesis-knowledge-query-capability-v1"
SURFACES = frozenset(
    {
        "lexical",
        "semantic",
        "document",
        "claim",
        "entity",
        "graph",
        "quantitative",
        "temporal",
        "memory",
        "federated",
        "event",
        "artifact",
    }
)
OBJECT_TYPES = frozenset(
    {
        "document",
        "passage",
        "claim",
        "entity",
        "relation",
        "observation",
        "memory",
        "event",
        "artifact",
    }
)


class UnifiedQueryError(RuntimeError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code, self.message, self.details = code, message, details

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            value["details"] = self.details
        return value


def _canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _time_ms(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise UnifiedQueryError(
            "bad_request", "temporal values must be epoch milliseconds or ISO-8601"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def _list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise UnifiedQueryError("bad_request", f"{field} must be a list")
    result = [str(item).strip() for item in value]
    if any(not item for item in result) or len(result) != len(set(result)):
        raise UnifiedQueryError(
            "bad_request", f"{field} must contain unique non-empty values"
        )
    return sorted(result)


def validate_query_request(value: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize the public query contract and reject ambiguous scope or budgets."""

    raw = dict(value)
    query = str(raw.get("query") or raw.get("task") or "").strip()
    task = str(raw.get("task") or query).strip()
    if not query or not task or len(query) > 5000 or len(task) > 5000:
        raise UnifiedQueryError(
            "bad_request",
            "query and task must be non-empty and at most 5000 characters",
        )
    scope = dict(raw.get("scope") or {})
    domains = _list(scope.get("domains"), "scope.domains")
    namespaces = _list(scope.get("namespaces"), "scope.namespaces")
    all_authorized = bool(scope.get("all_authorized", False))
    if not domains and not namespaces and not all_authorized:
        raise UnifiedQueryError(
            "bad_request", "domains, namespaces, or all_authorized=true is required"
        )
    if all_authorized and (domains or namespaces):
        raise UnifiedQueryError(
            "bad_request", "explicit scope and all_authorized are mutually exclusive"
        )
    surfaces = _list(raw.get("surfaces") or ["lexical", "semantic"], "surfaces")
    unknown = set(surfaces) - SURFACES
    if unknown:
        raise UnifiedQueryError(
            "bad_request", f"unsupported surfaces: {sorted(unknown)}"
        )
    object_types = _list(raw.get("object_types") or [], "object_types")
    unknown_types = set(object_types) - OBJECT_TYPES
    if unknown_types:
        raise UnifiedQueryError(
            "bad_request", f"unsupported object types: {sorted(unknown_types)}"
        )
    source_policy = dict(raw.get("source_policy") or {})
    include = _list(source_policy.get("include"), "source_policy.include")
    exclude = _list(source_policy.get("exclude"), "source_policy.exclude")
    required = _list(source_policy.get("required"), "source_policy.required")
    if set(include) & set(exclude) or set(required) & set(exclude):
        raise UnifiedQueryError(
            "bad_request", "included or required sources cannot also be excluded"
        )
    drift = str(source_policy.get("capability_drift") or "fail")
    if drift not in {"fail", "replan"}:
        raise UnifiedQueryError(
            "bad_request", "capability_drift must be fail or replan"
        )
    memory = dict(raw.get("memory") or {})
    memory_mode = str(memory.get("mode") or "off")
    if memory_mode not in {"off", "query-expansion", "separate"}:
        raise UnifiedQueryError(
            "bad_request", "memory.mode must be off, query-expansion, or separate"
        )
    temporal = dict(raw.get("temporal") or {})
    temporal_normalized = {
        key: _time_ms(temporal.get(key))
        for key in ("as_of", "valid_at", "observed_before")
        if temporal.get(key) is not None
    }
    if temporal.get("generation") is not None:
        try:
            temporal_normalized["generation"] = int(temporal["generation"])
        except (TypeError, ValueError) as exc:
            raise UnifiedQueryError(
                "bad_request", "temporal.generation must be an integer"
            ) from exc
        if temporal_normalized["generation"] < 0:
            raise UnifiedQueryError(
                "bad_request", "temporal.generation must be non-negative"
            )
    if "history" in temporal:
        temporal_normalized["history"] = bool(temporal["history"])
    if "include_retracted" in temporal:
        temporal_normalized["include_retracted"] = bool(temporal["include_retracted"])
    budgets = dict(raw.get("budgets") or {})
    try:
        total_results = int(budgets.get("max_results", 50))
        timeout_ms = int(budgets.get("timeout_ms", 5000))
        max_bytes = int(budgets.get("max_bytes", 2_000_000))
        retries = int(budgets.get("max_retries", 1))
        token_budget = int(budgets.get("token_budget", 4000))
        per_source = int(budgets.get("per_source_results", min(50, total_results)))
        memory_limit = int(memory.get("limit", 10))
        memory_token_budget = int(memory.get("token_budget", 500))
        max_plan_nodes = int(budgets.get("max_plan_nodes", 1000))
    except (TypeError, ValueError) as exc:
        raise UnifiedQueryError(
            "bad_request", "query budgets must be integers"
        ) from exc
    if (
        not 1 <= total_results <= 5000
        or not 1 <= per_source <= 500
        or not 1 <= timeout_ms <= 120_000
        or not 1 <= max_bytes <= 50_000_000
        or not 0 <= retries <= 3
        or not 1 <= token_budget <= 1_000_000
        or not 1 <= memory_limit <= 100
        or not 1 <= memory_token_budget <= 10_000
        or not 3 <= max_plan_nodes <= 5000
    ):
        raise UnifiedQueryError(
            "bad_request", "one or more query budgets are outside supported bounds"
        )
    authorization = dict(raw.get("authorization_context") or {})
    snapshot = dict(raw.get("snapshot") or {})
    if snapshot and (
        snapshot.get("contract") != "noesis-research-snapshot-v1"
        or not snapshot.get("session_id")
        or not snapshot.get("vector_hash")
        or not isinstance(snapshot.get("vector"), Mapping)
    ):
        raise UnifiedQueryError("bad_snapshot", "snapshot binding is incomplete")
    normalized = {
        "contract": REQUEST_CONTRACT,
        "query": query,
        "task": task,
        "scope": {
            "domains": domains,
            "namespaces": namespaces,
            "all_authorized": all_authorized,
            "tenant_id": scope.get("tenant_id"),
            "task_id": scope.get("task_id"),
        },
        "surfaces": surfaces,
        "object_types": object_types,
        "source_policy": {
            "include": include,
            "exclude": exclude,
            "required": required,
            "allow_remote": bool(source_policy.get("allow_remote", False)),
            "capability_drift": drift,
        },
        "temporal": temporal_normalized,
        "memory": {
            "mode": memory_mode,
            "limit": memory_limit,
            "kinds": _list(memory.get("kinds"), "memory.kinds"),
            "token_budget": memory_token_budget,
        },
        "evidence_policy": {
            "mandatory_citations": bool(
                dict(raw.get("evidence_policy") or {}).get("mandatory_citations", True)
            ),
            "include_contradictions": bool(
                dict(raw.get("evidence_policy") or {}).get(
                    "include_contradictions", True
                )
            ),
            "allow_unresolved_lineage": bool(
                dict(raw.get("evidence_policy") or {}).get(
                    "allow_unresolved_lineage", True
                )
            ),
        },
        "diversity": dict(raw.get("diversity") or {}),
        "budgets": {
            "max_results": total_results,
            "per_source_results": per_source,
            "timeout_ms": timeout_ms,
            "max_bytes": max_bytes,
            "max_retries": retries,
            "token_budget": token_budget,
            "max_plan_nodes": max_plan_nodes,
        },
        "cursor": raw.get("cursor"),
        "authorization_context": {
            "principal_id": authorization.get("principal_id"),
            "purpose": authorization.get("purpose"),
            "required_scopes": _list(
                authorization.get("required_scopes"),
                "authorization_context.required_scopes",
            ),
        },
        "snapshot": snapshot,
    }
    normalized["request_hash"] = _digest(
        {
            key: item
            for key, item in normalized.items()
            if key not in {"cursor", "request_hash"}
        }
    )
    return normalized


def capability_definition(
    source_id: str,
    kind: str,
    *,
    domains: Sequence[str] = (),
    namespaces: Sequence[str] = (),
    surfaces: Sequence[str],
    object_types: Sequence[str],
    required_scopes: Sequence[str] = ("knowledge:read",),
    remote: bool = False,
    limits: Mapping[str, int] | None = None,
    temporal: bool = False,
    version: str = "1",
) -> dict[str, Any]:
    value = {
        "contract": CAPABILITY_CONTRACT,
        "source_id": source_id,
        "kind": kind,
        "version": version,
        "domains": sorted(set(domains)),
        "namespaces": sorted(set(namespaces)),
        "surfaces": sorted(set(surfaces)),
        "object_types": sorted(set(object_types)),
        "required_scopes": sorted(set(required_scopes)),
        "remote": bool(remote),
        "temporal_support": "bitemporal" if temporal else "current-only",
        "score_semantics": "native-score-with-rank",
        "pagination": {"kind": "stable-offset", "stable": True},
        "freshness": {"kind": "source-reported-or-unknown"},
        "consistency": "snapshot",
        "filters": ["query", "limit", "surface", "temporal"],
        "limits": dict(
            limits or {"max_results": 100, "timeout_ms": 5000, "max_bytes": 2_000_000}
        ),
    }
    value["capability_hash"] = _digest(value)
    return value


@runtime_checkable
class QueryAdapter(Protocol):
    def describe(self) -> dict[str, Any]: ...
    def query(
        self, request: Mapping[str, Any], *, scopes: set[str]
    ) -> dict[str, Any]: ...


class QueryCatalog:
    def __init__(self, adapters: Sequence[QueryAdapter] = ()) -> None:
        self.adapters: dict[str, QueryAdapter] = {}
        for adapter in adapters:
            self.add(adapter)

    def add(self, adapter: QueryAdapter) -> None:
        description = adapter.describe()
        source = str(description["source_id"])
        if source in self.adapters:
            raise UnifiedQueryError(
                "duplicate_source", f"duplicate query source {source}"
            )
        self.adapters[source] = adapter

    def capabilities(self, *, scopes: set[str]) -> list[dict[str, Any]]:
        return [
            adapter.describe()
            for _, adapter in sorted(self.adapters.items())
            if "operator" in scopes
            or set(adapter.describe().get("required_scopes") or ()).issubset(scopes)
        ]

    def fingerprint(self, *, scopes: set[str]) -> str:
        return _digest(self.capabilities(scopes=scopes))


def _text(row: Mapping[str, Any]) -> str:
    for key in (
        "text",
        "content",
        "excerpt",
        "representative",
        "name",
        "title",
        "value",
    ):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, Mapping):
            return _canonical(value)
    return _canonical(row)


def _citation(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    citations = row.get("citations")
    if isinstance(citations, Sequence) and not isinstance(citations, (str, bytes)):
        return [dict(item) for item in citations if isinstance(item, Mapping)]
    url = row.get("url") or row.get("source_url")
    document = row.get("document_id") or row.get("source_document_id")
    return [{"url": url, "document_id": document}] if url or document else []


class BackingQueryAdapter:
    def __init__(self, domain: str, backing: Any) -> None:
        self.domain, self.backing, self._lock = domain, backing, threading.RLock()
        self.definition = capability_definition(
            f"local:{domain}",
            "local-backing",
            domains=[domain],
            surfaces=[
                "lexical",
                "semantic",
                "document",
                "claim",
                "entity",
                "quantitative",
            ],
            object_types=["document", "passage", "claim", "entity", "observation"],
            temporal=False,
        )

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def query(self, request: Mapping[str, Any], *, scopes: set[str]) -> dict[str, Any]:
        if "operator" not in scopes and "knowledge:read" not in scopes:
            raise UnifiedQueryError("unauthorized", "knowledge:read scope is required")
        limit = min(
            int(request.get("limit", 20)), int(self.definition["limits"]["max_results"])
        )
        surface = str(request.get("surface", "lexical"))
        started = time.monotonic()
        if surface == "quantitative":
            object_type = "observation"
            rows = []
            with self._lock:
                for claim in list(self.backing.claims(limit=limit) or ()):
                    representative = claim.get("representative")
                    claim_id = claim.get("claim_id") or (
                        representative.get("claim_id")
                        if isinstance(representative, Mapping)
                        else None
                    )
                    check = (
                        self.backing.quantitative_check(str(claim_id))
                        if claim_id
                        else None
                    )
                    if check:
                        rows.append(
                            {
                                "observation_id": f"quantitative:{claim_id}",
                                "source_claim_id": claim_id,
                                **check,
                            }
                        )
        else:
            method, args, kwargs, object_type = {
                "lexical": (
                    "search",
                    (str(request["query"]),),
                    {"limit": limit},
                    "passage",
                ),
                "semantic": (
                    "semantic_search",
                    (str(request["query"]),),
                    {"limit": limit},
                    "passage",
                ),
                "document": ("documents", (), {"limit": limit}, "document"),
                "claim": ("claims", (), {"limit": limit}, "claim"),
                "entity": ("entities", (str(request["query"]),), {}, "entity"),
            }[surface]
            with self._lock:
                rows = list(getattr(self.backing, method)(*args, **kwargs) or ())
        items = []
        for rank, original in enumerate(rows[:limit], 1):
            row = dict(original)
            identity = str(
                row.get("canonical_id")
                or row.get(f"{object_type}_id")
                or row.get("document_id")
                or row.get("claim_id")
                or row.get("entity_id")
                or _digest(row)[:24]
            )
            items.append(
                {
                    "id": identity,
                    "canonical_id": row.get("canonical_id"),
                    "origin_id": str(
                        row.get("origin_id") or row.get("document_id") or identity
                    ),
                    "object_type": object_type,
                    "text": _text(row),
                    "title": row.get("title"),
                    "score": row.get("score") or row.get("similarity"),
                    "native_rank": rank,
                    "citations": _citation(row),
                    "timestamp_ms": row.get("timestamp_ms")
                    or row.get("published_at_ms")
                    or row.get("observed_at_ms"),
                    "raw": row,
                }
            )
        return {
            "source": self.definition["source_id"],
            "items": items,
            "score_semantics": "native-preserved-rank-fused",
            "provenance": {
                "source_id": self.definition["source_id"],
                "domain": self.domain,
                "capability_hash": self.definition["capability_hash"],
                "query_hash": _query_hash(request),
            },
            "latency_ms": round((time.monotonic() - started) * 1000, 3),
        }


class TemporalQueryAdapter:
    def __init__(self, domain: str, backing: Any) -> None:
        self.domain, self.backing, self._lock = domain, backing, threading.RLock()
        self.definition = capability_definition(
            f"temporal:{domain}",
            "temporal",
            domains=[domain],
            surfaces=["temporal"],
            object_types=["claim", "entity", "relation", "observation"],
            temporal=True,
        )

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def query(self, request: Mapping[str, Any], *, scopes: set[str]) -> dict[str, Any]:
        from src.kb.temporal import query_temporal

        if "operator" not in scopes and "knowledge:read" not in scopes:
            raise UnifiedQueryError("unauthorized", "knowledge:read scope is required")
        temporal = dict(request.get("temporal") or {})
        with self._lock:
            result = query_temporal(
                self.backing,
                as_of=temporal.get("as_of"),
                valid_at=temporal.get("valid_at"),
                observed_before=temporal.get("observed_before"),
                history=bool(temporal.get("history")),
                include_retracted=bool(temporal.get("include_retracted")),
                limit=int(request.get("limit", 20)),
            )
        items = []
        for rank, row in enumerate(result.get("items") or (), 1):
            item = dict(row)
            identity = str(
                item.get("assertion_id")
                or item.get("temporal_id")
                or _digest(item)[:24]
            )
            items.append(
                {
                    "id": identity,
                    "origin_id": str(item.get("source_document_id") or identity),
                    "object_type": str(item.get("assertion_kind") or "observation"),
                    "text": _text(item),
                    "native_rank": rank,
                    "citations": _citation(item),
                    "temporal": {
                        key: item.get(key)
                        for key in (
                            "valid_from_ms",
                            "valid_to_ms",
                            "observed_at_ms",
                            "retracted_at_ms",
                        )
                    },
                    "raw": item,
                }
            )
        return {
            "source": self.definition["source_id"],
            "items": items,
            "score_semantics": "temporal-order",
            "provenance": {
                "source_id": self.definition["source_id"],
                "domain": self.domain,
                "capability_hash": self.definition["capability_hash"],
                "query_hash": _query_hash(request),
            },
            "basis": result.get("basis"),
            "cursor": dict(result.get("page") or {}).get("next_cursor"),
        }


class MemoryQueryAdapter:
    def __init__(
        self,
        store: Any,
        namespace: str,
        tenant_id: str,
        task_id: str | None,
        principal_id: str,
    ) -> None:
        self.store, self.namespace, self.tenant_id, self.task_id, self.principal_id = (
            store,
            namespace,
            tenant_id,
            task_id,
            principal_id,
        )
        self.definition = capability_definition(
            f"memory:{namespace}",
            "memory",
            namespaces=[namespace],
            surfaces=["memory"],
            object_types=["memory"],
            required_scopes=[
                "knowledge:memory:read",
                f"namespace:{namespace}:read",
                f"tenant:{tenant_id}",
            ],
            temporal=True,
        )

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def query(self, request: Mapping[str, Any], *, scopes: set[str]) -> dict[str, Any]:
        result = self.store.retrieve(
            request["query"],
            {
                "namespace": self.namespace,
                "tenant_id": self.tenant_id,
                "task_id": self.task_id,
            },
            principal_id=self.principal_id,
            scopes=scopes,
            kinds=list(dict(request.get("memory") or {}).get("kinds") or ()),
            limit=int(request.get("limit", 10)),
            at_ms=_time_ms(dict(request.get("temporal") or {}).get("as_of")),
        )
        items = []
        used_tokens = 0
        for rank, hit in enumerate(result["results"], 1):
            memory = hit["memory"]
            text = _text(memory)
            tokens = max(1, (len(text.encode()) + 3) // 4)
            if used_tokens + tokens > int(
                dict(request.get("memory") or {}).get("token_budget", 500)
            ):
                break
            used_tokens += tokens
            items.append(
                {
                    "id": memory["memory_id"],
                    "origin_id": memory["memory_id"],
                    "object_type": "memory",
                    "text": text,
                    "score": hit["score"],
                    "native_rank": rank,
                    "citations": [],
                    "evidence_class": "context-only",
                    "raw": memory,
                }
            )
        return {
            "source": self.definition["source_id"],
            "items": items,
            "score_semantics": "memory-relevance",
            "provenance": {
                "source_id": self.definition["source_id"],
                "capability_hash": self.definition["capability_hash"],
                "query_hash": _query_hash(request),
            },
            "token_accounting": {
                "used": used_tokens,
                "budget": int(
                    dict(request.get("memory") or {}).get("token_budget", 500)
                ),
            },
        }


class FederationQueryAdapter:
    """Expose an existing federation engine as one remote plan node."""

    def __init__(self, engine: Any, source_id: str = "federation") -> None:
        self.engine = engine
        self.definition = capability_definition(
            source_id,
            "federation",
            surfaces=["federated"],
            object_types=list(OBJECT_TYPES - {"memory"}),
            required_scopes=["knowledge:federation:read"],
            remote=True,
        )

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def query(self, request: Mapping[str, Any], *, scopes: set[str]) -> dict[str, Any]:
        output = self.engine.execute(
            {
                "capability": "search",
                "query": {"query": request["query"], "limit": request.get("limit", 20)},
                "timeout_ms": request.get("timeout_ms", 5000),
                "max_retries": request.get("max_retries", 1),
            },
            scopes=scopes,
        )
        items = []
        for rank, merged in enumerate(output.get("results") or (), 1):
            for evidence in merged.get("evidence") or ():
                item = dict(evidence.get("item") or {})
                item.setdefault("id", merged["identity"])
                item.setdefault("origin_id", item["id"])
                item.setdefault("object_type", item.get("type", "document"))
                item.setdefault("text", _text(item))
                item["native_rank"] = rank
                item["citations"] = _citation(item)
                item["upstream_source"] = evidence.get("source")
                items.append(item)
        return {
            "source": self.definition["source_id"],
            "items": items,
            "score_semantics": "federated-preserved",
            "provenance": {
                "source_id": self.definition["source_id"],
                "capability_hash": self.definition["capability_hash"],
                "query_hash": _query_hash(request),
            },
            "failures": output.get("failures", []),
            "coverage": output.get("coverage", {}),
        }


class StoredObjectQueryAdapter:
    """Read canonical events or immutable artifacts through fixed typed queries."""

    def __init__(self, conn: Any, namespace: str, surface: str) -> None:
        if surface not in {"event", "artifact"}:
            raise UnifiedQueryError(
                "invalid_adapter", "stored surface is not supported"
            )
        self.conn, self.namespace, self.surface = conn, namespace, surface
        self.definition = capability_definition(
            f"{surface}:{namespace}",
            f"stored-{surface}",
            namespaces=[namespace],
            surfaces=[surface],
            object_types=[surface],
            required_scopes=["knowledge:read", f"namespace:{namespace}:read"],
            temporal=False,
        )

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def query(self, request: Mapping[str, Any], *, scopes: set[str]) -> dict[str, Any]:
        required = set(self.definition["required_scopes"])
        if "operator" not in scopes and not required.issubset(scopes):
            raise UnifiedQueryError(
                "unauthorized", "stored object scope is not authorized"
            )
        limit = min(int(request.get("limit", 20)), 100)
        needle = f"%{str(request['query']).casefold()}%"
        if self.surface == "event":
            rows = self.conn.execute(
                "SELECT event_id,event_type,participants_json,location_json,start_ms,end_ms,"
                "evidence_json,revision,status,canonical_id,updated_at_ms FROM canonical_events "
                "WHERE namespace=? AND status='active' AND (lower(event_type) LIKE ? OR "
                "lower(participants_json) LIKE ?) ORDER BY updated_at_ms DESC,event_id LIMIT ?",
                [self.namespace, needle, needle, limit],
            ).fetchall()
            values = [
                {
                    "id": row[0],
                    "canonical_id": row[9],
                    "origin_id": row[0],
                    "object_type": "event",
                    "text": f"{row[1]} {row[2]}",
                    "citations": json.loads(row[6]),
                    "temporal": {"valid_from_ms": row[4], "valid_to_ms": row[5]},
                    "version": row[7],
                    "status": row[8],
                    "timestamp_ms": row[10],
                }
                for row in rows
            ]
        else:
            rows = self.conn.execute(
                "SELECT artifact_id,logical_id,kind,generation,content_json,content_hash,"
                "producer_json,lineage_complete,created_at_ms FROM knowledge_artifacts "
                "WHERE namespace=? AND status='active' AND (lower(logical_id) LIKE ? OR "
                "lower(content_json) LIKE ?) ORDER BY created_at_ms DESC,artifact_id LIMIT ?",
                [self.namespace, needle, needle, limit],
            ).fetchall()
            values = [
                {
                    "id": row[0],
                    "origin_id": row[1],
                    "object_type": "artifact",
                    "text": row[4] or row[1],
                    "citations": [],
                    "kind": row[2],
                    "version": row[3],
                    "content_hash": row[5],
                    "producer": json.loads(row[6]),
                    "lineage": {"complete": bool(row[7])},
                    "timestamp_ms": row[8],
                }
                for row in rows
            ]
        for rank, value in enumerate(values, 1):
            value["native_rank"] = rank
        return {
            "source": self.definition["source_id"],
            "items": values,
            "score_semantics": "newest-first",
            "provenance": {
                "source_id": self.definition["source_id"],
                "capability_hash": self.definition["capability_hash"],
                "query_hash": _query_hash(request),
            },
        }


class MaintainedDocumentQueryAdapter:
    """Query normalized documents only after their end-to-end generation commits."""

    def __init__(self, conn: Any, domains: Sequence[str]) -> None:
        self.conn = conn
        self.definition = capability_definition(
            "maintained-documents",
            "committed-document-store",
            domains=domains,
            surfaces=["lexical", "document"],
            object_types=["document"],
            required_scopes=["knowledge:read"],
        )

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def query(self, request: Mapping[str, Any], *, scopes: set[str]) -> dict[str, Any]:
        if "operator" not in scopes and "knowledge:read" not in scopes:
            raise UnifiedQueryError("unauthorized", "knowledge:read scope is required")
        temporal = dict(request.get("temporal") or {})
        generation = temporal.get("generation")
        generation_rows = self.conn.execute(
            "SELECT pack_id,source_watermark,receipt_json FROM knowledge_maintenance_generations "
            "WHERE status IN ('complete','partial') "
            + ("AND source_watermark<=? " if generation is not None else "")
            + "ORDER BY pack_id,source_watermark",
            [] if generation is None else [int(generation)],
        ).fetchall()
        pack_limits: dict[str, int] = {}
        for row in generation_rows:
            pack_limits[str(row[0])] = max(int(row[1]), pack_limits.get(str(row[0]), 0))
        if generation is not None:
            exact_packs = {
                str(row[0]) for row in generation_rows if int(row[1]) == int(generation)
            }
            if not exact_packs:
                raise UnifiedQueryError(
                    "generation_unavailable", "requested generation is not committed"
                )
            if len(pack_limits) > 1:
                raise UnifiedQueryError(
                    "mixed_generation",
                    "an exact document generation query must resolve to one source pack",
                )
        needle = f"%{str(request['query']).casefold()}%"
        conditions = [
            "r.committed_watermark IS NOT NULL",
            (
                "(lower(COALESCE(json_extract_string(r.payload_json,'$.title'),'')) LIKE ? "
                "OR lower(COALESCE(json_extract_string(r.payload_json,'$.content'),'')) LIKE ? "
                "OR lower(r.payload_json) LIKE ?)"
            ),
        ]
        params: list[Any] = [needle, needle, needle]
        if generation is not None:
            conditions.append("r.committed_watermark<=?")
            params.append(int(generation))
        if temporal.get("valid_at") is not None:
            conditions.extend(
                [
                    "(r.valid_from_ms IS NULL OR r.valid_from_ms<=?)",
                    "(r.valid_to_ms IS NULL OR r.valid_to_ms>?)",
                ]
            )
            params.extend([int(temporal["valid_at"]), int(temporal["valid_at"])])
        observed = temporal.get("observed_before", temporal.get("as_of"))
        if observed is not None:
            conditions.append("r.observed_at_ms<=?")
            params.append(int(observed))
        if pack_limits:
            conditions.append(
                "("
                + " OR ".join(
                    "(r.pack_id=? AND r.committed_watermark<=?)" for _ in pack_limits
                )
                + ")"
            )
            for pack_id, limit in sorted(pack_limits.items()):
                params.extend([pack_id, limit])
        else:
            return {
                "source": self.definition["source_id"],
                "items": [],
                "score_semantics": "committed-document-order",
                "watermark": generation,
                "provenance": {
                    "source_id": self.definition["source_id"],
                    "capability_hash": self.definition["capability_hash"],
                    "query_hash": _query_hash(request),
                },
            }
        history_clause = (
            ""
            if temporal.get("history", False)
            else (
                "QUALIFY ROW_NUMBER() OVER (PARTITION BY r.document_id ORDER BY r.revision DESC)=1 "
            )
        )
        rows = self.conn.execute(
            "SELECT r.document_id,r.source_id,r.payload_json,r.valid_from_ms,r.content_hash,"
            "r.revision_id,r.revision,r.committed_watermark,r.lifecycle,r.observed_at_ms "
            "FROM document_revision_records r WHERE "
            + " AND ".join(conditions)
            + " "
            + history_clause
            + "ORDER BY r.observed_at_ms DESC,r.document_id,r.revision DESC LIMIT ?",
            [*params, min(1000, int(request.get("limit", 20)) * 10)],
        ).fetchall()
        values = []
        for row in rows:
            if not temporal.get("include_retracted", False) and row[8] != "active":
                continue
            payload = json.loads(row[2])
            metadata = dict(payload.get("metadata") or {})
            values.append(
                {
                    "id": row[0],
                    "origin_id": row[1] or row[0],
                    "object_type": "document",
                    "text": " ".join(
                        str(value)
                        for value in (
                            payload.get("title"),
                            payload.get("content"),
                            _canonical(metadata),
                        )
                        if value
                    ),
                    "citations": [
                        {
                            "document_id": row[0],
                            "source": row[1],
                            "url": payload.get("url"),
                            "revision_id": row[5],
                        }
                    ],
                    "timestamp_ms": row[9],
                    "content_hash": row[4],
                    "revision_id": row[5],
                    "revision": int(row[6]),
                    "generation": int(row[7]),
                    "lifecycle": row[8],
                    "temporal": {"valid_from_ms": row[3], "observed_at_ms": row[9]},
                }
            )
            if len(values) >= int(request.get("limit", 20)):
                break
        watermark = self.conn.execute(
            "SELECT MAX(generation) FROM knowledge_maintenance_generations"
        ).fetchone()[0]
        return {
            "source": self.definition["source_id"],
            "items": values,
            "score_semantics": "committed-document-order",
            "watermark": None if watermark is None else int(watermark),
            "provenance": {
                "source_id": self.definition["source_id"],
                "capability_hash": self.definition["capability_hash"],
                "query_hash": _query_hash(request),
            },
        }


class StoredGraphQueryAdapter:
    """Expose relation tables only when their concrete schema is installed."""

    def __init__(self, conn: Any, domain: str, table: str) -> None:
        self.conn, self.domain, self.table = conn, domain, table
        self.definition = capability_definition(
            f"graph:{domain}:{table}",
            "stored-graph",
            domains=[domain],
            surfaces=["graph"],
            object_types=["relation"],
        )

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def query(self, request: Mapping[str, Any], *, scopes: set[str]) -> dict[str, Any]:
        if "operator" not in scopes and "knowledge:read" not in scopes:
            raise UnifiedQueryError("unauthorized", "knowledge:read scope is required")
        limit = min(int(request.get("limit", 20)), 100)
        needle = f"%{str(request['query']).casefold()}%"
        if self.table == "technical_relations":
            rows = self.conn.execute(
                "SELECT relation_id,subject_id,relation,object_id,observed_at_ms,source_url,"
                "source_document_id FROM technical_relations WHERE domain=? AND "
                "(lower(subject_id) LIKE ? OR lower(object_id) LIKE ? OR lower(relation) LIKE ?) "
                "ORDER BY observed_at_ms DESC,relation_id LIMIT ?",
                [self.domain, needle, needle, needle, limit],
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT relation_id,subject_id,relation_type,object_id,observed_at_ms,NULL,"
                "source_document_id FROM political_relations WHERE domain=? AND "
                "(lower(subject_id) LIKE ? OR lower(object_id) LIKE ? OR lower(relation_type) LIKE ?) "
                "ORDER BY observed_at_ms DESC,relation_id LIMIT ?",
                [self.domain, needle, needle, needle, limit],
            ).fetchall()
        items = [
            {
                "id": row[0],
                "origin_id": row[6] or row[0],
                "object_type": "relation",
                "text": f"{row[1]} {row[2]} {row[3]}",
                "native_rank": rank,
                "timestamp_ms": row[4],
                "citations": [{"url": row[5], "document_id": row[6]}]
                if row[5] or row[6]
                else [],
            }
            for rank, row in enumerate(rows, 1)
        ]
        return {
            "source": self.definition["source_id"],
            "items": items,
            "score_semantics": "newest-first",
            "provenance": {
                "source_id": self.definition["source_id"],
                "capability_hash": self.definition["capability_hash"],
                "query_hash": _query_hash(request),
            },
        }


class StaticQueryAdapter:
    """Offline adapter used by conformance tests and embedded deployments."""

    def __init__(
        self,
        source_id: str,
        items: Sequence[Mapping[str, Any]],
        *,
        domains: Sequence[str] = (),
        surfaces: Sequence[str] = ("lexical",),
        remote: bool = False,
        required_scopes: Sequence[str] = ("knowledge:read",),
        fail: str | None = None,
        temporal: bool = False,
    ) -> None:
        self.items = [dict(item) for item in items]
        self.fail = fail
        self.calls = []
        self.definition = capability_definition(
            source_id,
            "static",
            domains=domains,
            surfaces=surfaces,
            object_types=list(OBJECT_TYPES - {"memory"}),
            remote=remote,
            required_scopes=required_scopes,
            temporal=temporal,
        )

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def query(self, request: Mapping[str, Any], *, scopes: set[str]) -> dict[str, Any]:
        self.calls.append(dict(request))
        if self.fail:
            raise UnifiedQueryError(self.fail, "injected source failure")
        limit = int(request.get("limit", 20))
        items = []
        for rank, row in enumerate(self.items[:limit], 1):
            item = dict(row)
            item.setdefault("id", _digest(item)[:24])
            item.setdefault("origin_id", item["id"])
            item.setdefault("object_type", "document")
            item.setdefault("text", _text(item))
            item.setdefault("native_rank", rank)
            item.setdefault("citations", _citation(item))
            items.append(item)
        return {
            "source": self.definition["source_id"],
            "items": items,
            "score_semantics": "fixture-rank",
            "provenance": {
                "source_id": self.definition["source_id"],
                "capability_hash": self.definition["capability_hash"],
                "query_hash": _query_hash(request),
            },
        }


class MaintainedSemanticQueryAdapter:
    """Search committed maintenance vectors in their declared semantic space."""

    def __init__(self, conn, namespace, *, embedding_provider=None, embedding_configuration=None):
        from src.kb.derived_revisions import DerivedRevisionStore
        self.namespace = namespace
        self.store = DerivedRevisionStore(conn, initialize=False, embedding_provider=embedding_provider,
                                          embedding_configuration=embedding_configuration)
        self.definition = capability_definition(
            f"maintained-semantic:{namespace}", "maintained-semantic", namespaces=[namespace],
            surfaces=["semantic"], object_types=["claim", "entity", "document"],
            required_scopes=["knowledge:read", f"namespace:{namespace}:read"])

    def describe(self):
        return dict(self.definition)

    def query(self, request, *, scopes):
        if request.get("snapshot"):
            raise UnifiedQueryError("snapshot_unavailable", "maintained semantic search currently serves the latest committed generation")
        items = self.store.semantic_search(self.namespace, request["query"], scopes=scopes, limit=request.get("limit", 20))
        for item in items:
            if item["object_type"] in {"index", "embedding", "summary"}:
                item["object_type"] = "document"
        return {"source": self.definition["source_id"], "items": items, "score_semantics": "cosine-similarity",
                "provenance": {"source_id": self.definition["source_id"], "capability_hash": self.definition["capability_hash"],
                               "query_hash": _query_hash(request)}}


def build_local_catalog(
    conn: Any,
    *,
    domains: Sequence[str] = (),
    namespaces: Sequence[str] = (),
    tenant_id: str | None = None,
    task_id: str | None = None,
    principal_id: str = "local-reader",
    include_memory: bool = True,
    embedding_provider=None,
    embedding_configuration=None,
) -> QueryCatalog:
    from src.kb.registry import KnowledgeDomainRegistry

    registry = KnowledgeDomainRegistry.from_config()
    selected = sorted(domains or registry.names())
    adapters = []
    installed_tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
        ).fetchall()
    }
    for domain in (domain for domain in selected if domain in registry.names()):
        backing = registry.resolve(domain, conn)
        adapters.extend(
            [
                BackingQueryAdapter(domain, backing),
                TemporalQueryAdapter(domain, backing),
            ]
        )
        for table in ("technical_relations", "political_relations"):
            if table in installed_tables:
                adapters.append(StoredGraphQueryAdapter(conn, domain, table))
    for namespace in sorted(namespaces):
        if {"derived_projection_items", "derived_object_generations"} <= installed_tables:
            adapters.append(MaintainedSemanticQueryAdapter(conn, namespace, embedding_provider=embedding_provider,
                                                           embedding_configuration=embedding_configuration))
        if "canonical_events" in installed_tables:
            adapters.append(StoredObjectQueryAdapter(conn, namespace, "event"))
        if "knowledge_artifacts" in installed_tables:
            adapters.append(StoredObjectQueryAdapter(conn, namespace, "artifact"))
    if {
        "documents",
        "document_revision_records",
        "knowledge_maintenance_generations",
    }.issubset(installed_tables):
        adapters.append(MaintainedDocumentQueryAdapter(conn, selected))
    if include_memory and tenant_id:
        from src.kb.memory import MemoryStore

        for namespace in sorted(namespaces):
            adapters.append(
                MemoryQueryAdapter(
                    MemoryStore(conn, initialize=False),
                    namespace,
                    tenant_id,
                    task_id,
                    principal_id,
                )
            )
    return QueryCatalog(adapters)


def _query_hash(request):
    # Runtime deadlines and retry allocations do not change the semantic query.
    return _digest({key: value for key, value in request.items()
                    if key not in {"timeout_ms", "max_retries", "deadline_monotonic"}})


def _encode_cursor(payload: Mapping[str, Any]) -> str:
    return base64.urlsafe_b64encode(_canonical(payload).encode()).decode().rstrip("=")


def _decode_cursor(cursor: str | None) -> dict[str, Any]:
    if not cursor:
        return {}
    try:
        return json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    except Exception as exc:
        raise UnifiedQueryError("invalid_cursor", "cursor is malformed") from exc


@dataclass
class UnifiedQueryEngine:
    catalog: QueryCatalog
    max_workers: int = 8
    _local_query_lock: Any = field(
        default_factory=threading.RLock, init=False, repr=False
    )

    def plan(self, request: Mapping[str, Any], *, scopes: set[str]) -> dict[str, Any]:
        normalized = validate_query_request(request)
        policy = normalized["source_policy"]
        selected = []
        omitted = []
        requested_domains = set(normalized["scope"]["domains"])
        requested_namespaces = set(normalized["scope"]["namespaces"])
        surfaces = set(normalized["surfaces"])
        temporal_required = bool(normalized["temporal"])
        declared_required_scopes = set(
            normalized["authorization_context"]["required_scopes"]
        )
        if "operator" not in scopes and not declared_required_scopes.issubset(scopes):
            raise UnifiedQueryError(
                "unauthorized",
                "authorization context requires scopes the caller does not have",
                scopes=sorted(declared_required_scopes - scopes),
            )
        for source, adapter in sorted(self.catalog.adapters.items()):
            cap = adapter.describe()
            reason = None
            if policy["include"] and source not in policy["include"]:
                reason = "not_included"
            elif source in policy["exclude"]:
                reason = "excluded"
            elif cap["remote"] and not policy["allow_remote"]:
                reason = "remote_disabled"
            elif cap["kind"] == "memory" and normalized["memory"]["mode"] == "off":
                reason = "memory_disabled"
            elif temporal_required and cap["temporal_support"] != "bitemporal":
                reason = "temporal_unsupported"
            elif "operator" not in scopes and not set(cap["required_scopes"]).issubset(
                scopes
            ):
                reason = "authorization"
            elif (
                requested_domains
                and cap["domains"]
                and not requested_domains.intersection(cap["domains"])
            ):
                reason = "domain_scope"
            elif (
                requested_namespaces
                and cap["namespaces"]
                and not requested_namespaces.intersection(cap["namespaces"])
            ):
                reason = "namespace_scope"
            elif not surfaces.intersection(cap["surfaces"]):
                reason = "capability"
            if reason:
                omitted.append({"source": source, "reason": reason})
            else:
                chosen = sorted(surfaces.intersection(cap["surfaces"]))
                for surface in chosen:
                    selected.append(
                        {
                            "node_id": f"query:{source}:{surface}",
                            "source": source,
                            "surface": surface,
                            "kind": "retrieval",
                            "capability_hash": cap["capability_hash"],
                            "subquery": {
                                "query": normalized["query"],
                                "object_types": normalized["object_types"],
                                "temporal": normalized["temporal"],
                            },
                            "selected_reason": "scope, capability, authorization, and policy matched",
                            "estimate": {
                                "maximum_results": min(
                                    normalized["budgets"]["per_source_results"],
                                    int(cap["limits"]["max_results"]),
                                ),
                                "maximum_bytes": min(
                                    normalized["budgets"]["max_bytes"],
                                    int(cap["limits"]["max_bytes"]),
                                ),
                            },
                            "budget": {
                                "max_results": min(
                                    normalized["budgets"]["per_source_results"],
                                    int(cap["limits"]["max_results"]),
                                ),
                                "timeout_ms": min(
                                    normalized["budgets"]["timeout_ms"],
                                    int(cap["limits"]["timeout_ms"]),
                                ),
                                "max_bytes": min(
                                    normalized["budgets"]["max_bytes"],
                                    int(cap["limits"]["max_bytes"]),
                                ),
                                "max_retries": normalized["budgets"]["max_retries"],
                            },
                            "depends_on": [],
                        }
                    )
        present = {node["source"] for node in selected}
        if len(selected) + 2 > normalized["budgets"]["max_plan_nodes"]:
            raise UnifiedQueryError(
                "plan_too_large",
                "selected capabilities exceed the configured plan-size limit",
            )
        if selected:
            allocated_timeout = max(
                1, normalized["budgets"]["timeout_ms"] // len(selected)
            )
            for node in selected:
                node["budget"]["timeout_ms"] = min(
                    node["budget"]["timeout_ms"], allocated_timeout
                )
        missing = sorted(set(policy["required"]) - present)
        if missing:
            raise UnifiedQueryError(
                "required_source_unavailable",
                "required sources could not be planned",
                sources=missing,
                omitted=omitted,
            )
        memory_nodes = [
            node["node_id"] for node in selected if node["surface"] == "memory"
        ]
        if normalized["memory"]["mode"] == "query-expansion":
            for node in selected:
                if node["surface"] != "memory":
                    node["depends_on"] = memory_nodes
        nodes = [
            *selected,
            {
                "node_id": "merge",
                "kind": "merge",
                "depends_on": [node["node_id"] for node in selected],
            },
            {"node_id": "context", "kind": "context", "depends_on": ["merge"]},
        ]
        plan = {
            "contract": PLAN_CONTRACT,
            "request": normalized,
            "catalog_hash": self.catalog.fingerprint(scopes=scopes),
            "nodes": nodes,
            "selected_sources": sorted(present),
            "omitted": omitted,
            "budget": normalized["budgets"],
            "deterministic_order": True,
            "policy_decisions": {
                "remote_enabled": policy["allow_remote"],
                "memory_mode": normalized["memory"]["mode"],
                "temporal_required": temporal_required,
                "required_sources": policy["required"],
            },
        }
        plan["plan_hash"] = _digest(plan)
        return plan

    def execute(
        self,
        request: Mapping[str, Any],
        *,
        scopes: set[str],
        cancelled: Callable[[], bool] | None = None,
        expected_plan_hash: str | None = None,
    ) -> dict[str, Any]:
        started = time.monotonic()
        plan = self.plan(request, scopes=scopes)
        if (
            expected_plan_hash
            and expected_plan_hash != plan["plan_hash"]
            and plan["request"]["source_policy"]["capability_drift"] == "fail"
        ):
            raise UnifiedQueryError(
                "capability_drift", "query plan no longer matches the expected plan"
            )
        normalized = plan["request"]
        deadline = started + normalized["budgets"]["timeout_ms"] / 1000
        cursor = _decode_cursor(normalized.get("cursor"))
        for key, actual in (
            ("request_hash", normalized["request_hash"]),
            ("catalog_hash", plan["catalog_hash"]),
        ):
            if cursor and cursor.get(key) != actual:
                raise UnifiedQueryError(
                    "cursor_drift", f"cursor {key} no longer matches"
                )
        query_text = normalized["query"]
        memory_context = []
        responses = []
        failures = []
        timings = {}
        dropped_candidates: list[dict[str, Any]] = []
        nodes = [node for node in plan["nodes"] if node.get("source")]
        memory_nodes = [node for node in nodes if node["surface"] == "memory"]

        def invoke(
            node: Mapping[str, Any], effective_query: str, adapter
        ) -> tuple[dict[str, Any], float]:
            if cancelled and cancelled():
                raise UnifiedQueryError("cancelled", "query was cancelled")
            child = {
                "query": effective_query,
                "surface": node["surface"],
                "limit": node["budget"]["max_results"],
                "timeout_ms": node["budget"]["timeout_ms"],
                "max_retries": node["budget"]["max_retries"],
                "temporal": normalized["temporal"],
                "memory": normalized["memory"],
            }
            begin = time.monotonic()
            node_deadline = min(deadline, begin + node["budget"]["timeout_ms"] / 1000)
            last = None
            local_connection = any(
                hasattr(adapter, attribute)
                for attribute in ("conn", "backing", "store")
            )
            for attempt in range(node["budget"]["max_retries"] + 1):
                try:
                    remaining_seconds = node_deadline - time.monotonic()
                    if remaining_seconds <= 0:
                        raise UnifiedQueryError("source_timeout", "query deadline exhausted")
                    if cancelled and cancelled():
                        raise UnifiedQueryError("cancelled", "query was cancelled")
                    child["timeout_ms"] = max(1, int(remaining_seconds * 1000))
                    child["max_retries"] = 0  # one retry budget, owned by this coordinator
                    child["deadline_monotonic"] = node_deadline
                    if normalized.get("snapshot"):
                        child["snapshot"] = normalized["snapshot"]
                    if local_connection:
                        if not self._local_query_lock.acquire(timeout=max(0, node_deadline - time.monotonic())):
                            raise UnifiedQueryError("source_timeout", "local query admission deadline exhausted")
                        try:
                            result = adapter.query(child, scopes=scopes)
                        finally:
                            self._local_query_lock.release()
                    else:
                        result = adapter.query(child, scopes=scopes)
                    if time.monotonic() > node_deadline:
                        raise UnifiedQueryError("source_timeout", "source exceeded its query deadline")
                    return result, round((time.monotonic() - begin) * 1000, 3)
                except Exception as exc:  # noqa: BLE001 - adapters are failure boundaries
                    last = exc
                    if getattr(exc, "code", "") not in {
                        "source_unavailable",
                        "source_timeout",
                    }:
                        break
            raise last or UnifiedQueryError("source_failed", "source failed")

        def run_nodes(group, effective_query):
            from src.kb.query_runtime import submit
            waiting = list(group)
            active = {}
            completed = []

            def failure(node, code, message):
                failures.append({"source": node["source"], "error": {"code": code, "message": message}})

            while waiting or active:
                if time.monotonic() >= deadline or cancelled and cancelled():
                    code = "cancelled" if cancelled and cancelled() else "source_timeout"
                    for node in waiting:
                        failure(node, code, "total query deadline or cancellation reached")
                    for future, node in active.items():
                        future.cancel()
                        failure(node, code, "total query deadline or cancellation reached")
                    break
                while waiting and len(active) < max(1, min(self.max_workers, 8)):
                    node = waiting.pop(0)
                    try:
                        future = submit(self.catalog.adapters[node["source"]],
                                        lambda adapter, node=node: invoke(node, effective_query, adapter))
                        if future is None:
                            failure(node, "source_busy", "bounded query worker capacity is exhausted")
                        else:
                            active[future] = node
                    except Exception as exc:
                        failure(node, getattr(exc, "code", "source_failed"), str(exc)[:300])
                if not active:
                    continue
                done, _ = concurrent.futures.wait(active, timeout=max(0, min(.05, deadline - time.monotonic())),
                                                 return_when=concurrent.futures.FIRST_COMPLETED)
                for future in done:
                    node = active.pop(future)
                    try:
                        response, elapsed = future.result()
                        timings[node["node_id"]] = elapsed
                        completed.append((node, response))
                        failures.extend(response.get("failures") or ())
                    except Exception as exc:
                        failure(node, getattr(exc, "code", "source_failed"), str(exc)[:300])
            return completed

        if normalized["memory"]["mode"] in {"query-expansion", "separate"}:
            memory_responses = run_nodes(memory_nodes, query_text)
            responses.extend(memory_responses)
            for node, response in sorted(memory_responses, key=lambda item: item[0]["node_id"]):
                memory_context.extend(response.get("items") or ())
            if normalized["memory"]["mode"] == "query-expansion" and memory_context:
                additions = " ".join(item["text"] for item in memory_context[:3])
                query_text = f"{query_text} {additions}"[:5000]
        responses.extend(run_nodes([node for node in nodes if node not in memory_nodes], query_text))
        required = set(normalized["source_policy"]["required"])
        failed_sources = {item["source"] for item in failures}
        if required & failed_sources:
            raise UnifiedQueryError(
                "required_source_failed",
                "a required source failed",
                sources=sorted(required & failed_sources),
            )
        merged: dict[str, dict[str, Any]] = {}
        contradictions = []
        for node, response in sorted(responses, key=lambda item: item[0]["node_id"]):
            is_memory = node["surface"] == "memory"
            for rank, item in enumerate(response.get("items") or (), 1):
                row = dict(item)
                if (
                    normalized["object_types"]
                    and str(row.get("object_type", "document"))
                    not in normalized["object_types"]
                ):
                    dropped_candidates.append(
                        {
                            "source": node["source"],
                            "item_id": row.get("id"),
                            "reason": "object_type_policy",
                        }
                    )
                    continue
                identity = str(
                    row.get("canonical_id")
                    or row.get("origin_id")
                    or row.get("id")
                    or _digest(row)
                )
                origin = str(row.get("origin_id") or row.get("id") or identity)
                entry = merged.setdefault(
                    identity,
                    {
                        "identity": identity,
                        "origin_id": origin,
                        "object_type": row.get("object_type", "document"),
                        "text": row.get("text") or _text(row),
                        "title": row.get("title"),
                        "citations": [],
                        "evidence": [],
                        "context": [],
                        "source_ranks": {},
                        "score": 0.0,
                        "temporal": row.get("temporal"),
                        "timestamps": [],
                        "versions": [],
                        "lineage": [],
                    },
                )
                evidence = {
                    "source": node["source"],
                    "origin_id": origin,
                    "native_rank": row.get("native_rank", rank),
                    "native_score": row.get("score"),
                    "score_semantics": response.get("score_semantics"),
                    "provenance": response.get("provenance", {}),
                    "item_id": row.get("id"),
                    "native_fields": row.get("raw", row),
                }
                if is_memory:
                    entry["context"].append(
                        {**evidence, "evidence_class": "context-only"}
                    )
                else:
                    if entry["evidence"] and entry["text"] != (
                        row.get("text") or _text(row)
                    ):
                        contradictions.append(identity)
                    entry["evidence"].append(evidence)
                    entry["citations"].extend(row.get("citations") or ())
                    entry["source_ranks"][node["source"]] = rank
                    entry["score"] += 1 / (60 + rank)
                    if row.get("timestamp_ms") is not None:
                        entry["timestamps"].append(row["timestamp_ms"])
                    if row.get("version") is not None:
                        entry["versions"].append(row["version"])
                    if row.get("lineage") is not None:
                        entry["lineage"].append(row["lineage"])
        authoritative = [item for item in merged.values() if item["evidence"]]
        for item in authoritative:
            item["independent_source_count"] = len(
                {e["origin_id"] for e in item["evidence"]}
            )
            item["citations"] = [
                dict(value)
                for value in {_canonical(c): c for c in item["citations"]}.values()
            ]
        authoritative.sort(key=lambda item: (-item["score"], item["identity"]))
        result_hash = _digest(
            [
                {
                    key: item[key]
                    for key in ("identity", "score", "evidence", "citations")
                }
                for item in authoritative
            ]
        )
        if cursor and cursor.get("result_set_hash") != result_hash:
            raise UnifiedQueryError(
                "cursor_drift", "cursor result set no longer matches current results"
            )
        offset = int(cursor.get("offset", 0))
        limit = normalized["budgets"]["max_results"]
        candidates = authoritative[offset : offset + limit]
        page: list[dict[str, Any]] = []
        used_bytes = 2
        for item in candidates:
            item_bytes = len(_canonical(item).encode()) + (1 if page else 0)
            if used_bytes + item_bytes > normalized["budgets"]["max_bytes"]:
                break
            page.append(item)
            used_bytes += item_bytes
        byte_limited = len(page) < len(candidates)
        if byte_limited:
            dropped_candidates.extend(
                {
                    "source": "merge",
                    "item_id": item["identity"],
                    "reason": "result_byte_limit",
                }
                for item in candidates[len(page) :]
            )
            failures.append(
                {
                    "source": "merge",
                    "error": {
                        "code": "result_byte_limit",
                        "message": "result page reached the configured byte budget",
                    },
                }
            )
        next_offset = offset + len(page)
        next_cursor = (
            _encode_cursor(
                {
                    "request_hash": normalized["request_hash"],
                    "catalog_hash": plan["catalog_hash"],
                    "result_set_hash": result_hash,
                    "offset": next_offset,
                }
            )
            if next_offset < len(authoritative) and page
            else None
        )
        context = self._assemble_context(page, normalized)
        contributions: dict[str, int] = {}
        for item in page:
            for evidence in item["evidence"]:
                source = str(evidence["source"])
                contributions[source] = contributions.get(source, 0) + 1
        committed_watermarks = sorted(
            {
                str(response["watermark"])
                for _, response in responses
                if response.get("watermark") is not None
            }
        )
        output = {
            "contract": RESULT_CONTRACT,
            "status": "cancelled"
            if cancelled and cancelled()
            else "partial"
            if failures or plan["omitted"]
            else "complete",
            "query_id": "query:" + normalized["request_hash"][:24],
            "result_hash": result_hash,
            "committed_watermark": committed_watermarks[-1]
            if committed_watermarks
            else None,
            "plan": plan,
            "items": page,
            "memory_context": memory_context,
            "memory_policy": {
                "used_for_expansion": normalized["memory"]["mode"] == "query-expansion",
                "counts_as_evidence": False,
                "counts_as_corroboration": False,
            },
            "execution_receipt": {
                "effective_query": query_text,
                "memory_expansion_ids": [item["id"] for item in memory_context]
                if normalized["memory"]["mode"] == "query-expansion"
                else [],
                "capability_hashes": {
                    node["source"]: node["capability_hash"] for node in nodes
                },
                "snapshot": normalized["snapshot"],
            },
            "context": context,
            "failures": sorted(
                failures, key=lambda item: (str(item.get("source")), _canonical(item))
            ),
            "coverage": {
                "planned": len(nodes),
                "completed": len(responses),
                "omitted": plan["omitted"],
                "partial": bool(failures or plan["omitted"]),
                "byte_limited": byte_limited,
                "bytes": used_bytes,
                "source_contributions": contributions,
            },
            "dropped_candidates": dropped_candidates,
            "contradictions": sorted(set(contradictions)),
            "page": {
                "offset": offset,
                "returned": len(page),
                "next_cursor": next_cursor,
                "result_set_hash": result_hash,
            },
            "timings_ms": timings,
            "latency_ms": round((time.monotonic() - started) * 1000, 3),
            "replay_instructions": {
                "method": "receipt",
                "request_hash": normalized["request_hash"],
                "plan_hash": plan["plan_hash"],
                "requires_catalog_hash": plan["catalog_hash"],
            },
        }
        output["replay_hash"] = _digest(
            {
                key: output[key]
                for key in (
                    "plan",
                    "items",
                    "memory_policy",
                    "failures",
                    "coverage",
                    "contradictions",
                    "page",
                )
            }
        )
        return output

    @staticmethod
    def _assemble_context(
        items: Sequence[Mapping[str, Any]], request: Mapping[str, Any]
    ) -> dict[str, Any]:
        from src.kb.context import assemble_context

        class Definition:
            embedding_model = "unified:rank"
            name = "unified"

        class Backing:
            definition = Definition()

            def search(self, query: str, limit: int = 20):
                return list(items)[:limit]

        return assemble_context(
            [("unified", Backing())],
            {
                "task": request["task"],
                "query": request["query"],
                "domains": ["unified"],
                "token_budget": request["budgets"]["token_budget"],
                "evidence_policy": request["evidence_policy"],
                "diversity": request["diversity"],
                "allowed_surfaces": ["lexical"],
                "max_candidates": request["budgets"]["max_results"],
            },
            scope_receipt=request["scope"],
        )

    def replay(
        self, request: Mapping[str, Any], prior: Mapping[str, Any], *, scopes: set[str]
    ) -> dict[str, Any]:
        current = self.execute(
            request,
            scopes=scopes,
            expected_plan_hash=dict(prior.get("plan") or {}).get("plan_hash"),
        )
        return {
            "contract": "noesis-knowledge-query-replay-v1",
            "matched": current["replay_hash"] == prior.get("replay_hash"),
            "prior_hash": prior.get("replay_hash"),
            "current_hash": current["replay_hash"],
            "result": current,
        }

    @staticmethod
    def evaluate(
        result: Mapping[str, Any], *, expected_ids: Sequence[str] = ()
    ) -> dict[str, Any]:
        found = {str(item.get("identity")) for item in result.get("items") or ()}
        expected = set(expected_ids)
        items = list(result.get("items") or ())
        evidence = [e for item in items for e in item.get("evidence") or ()]
        return {
            "contract": "noesis-knowledge-query-evaluation-v1",
            "passed": all(item.get("evidence") for item in items)
            and not any(e.get("evidence_class") == "context-only" for e in evidence),
            "metrics": {
                "recall": len(found & expected) / len(expected) if expected else None,
                "citation_coverage": sum(bool(item.get("citations")) for item in items)
                / len(items)
                if items
                else 1.0,
                "provenance_completeness": sum(
                    bool(e.get("provenance")) for e in evidence
                )
                / len(evidence)
                if evidence
                else 1.0,
                "partial_failure": bool(result.get("failures")),
                "memory_evidence_violations": sum(
                    1 for e in evidence if e.get("evidence_class") == "context-only"
                ),
            },
            "replay_hash": result.get("replay_hash"),
        }
