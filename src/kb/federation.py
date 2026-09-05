"""Bounded, provenance-preserving federation over external knowledge stores.

Adapters expose one deliberately small contract.  They never return credentials,
arbitrary backend handles, or executable queries; every result carries the source
identity, effective limits, freshness, and backend-specific scoring semantics.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

CONTRACT = "noesis-knowledge-source-v1"
RESULT_CONTRACT = "noesis-federated-result-v1"
READ_SCOPE = "knowledge:federation:read"
ADMIN_SCOPE = "knowledge:federation:admin"
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SECRET_KEYS = frozenset({"password", "token", "secret", "api_key", "authorization"})


class FederationError(RuntimeError):
    """Stable, credential-safe adapter or planning failure."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code, self.message, self.details = code, message, details

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            value["details"] = _redact(self.details)
        return value


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): ("[REDACTED]" if str(key).lower() in _SECRET_KEYS else _redact(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


def _require_scope(required: str, scopes: set[str] | None) -> None:
    if required not in (scopes or set()) and "operator" not in (scopes or set()):
        raise FederationError("unauthorized", f"missing required scope {required}")


def source_definition(
    source_id: str,
    kind: str,
    *,
    capabilities: Sequence[str],
    schemas: Mapping[str, Any],
    limits: Mapping[str, int],
    freshness: Mapping[str, Any] | None = None,
    consistency: str = "snapshot",
    score_semantics: str = "none",
    temporal_support: str = "current",
    failure_behavior: str = "partial",
    authorization_scopes: Sequence[str] = (READ_SCOPE,),
    version: str = "1.0.0",
) -> dict[str, Any]:
    """Build and validate a source capability declaration."""

    if not source_id or kind not in {"sql", "vector", "graph", "mcp", "fake"}:
        raise FederationError("invalid_source", "source id and supported kind are required")
    required_limits = {"max_results", "timeout_ms", "max_bytes"}
    if required_limits - set(limits):
        raise FederationError("invalid_source", "result, timeout, and byte limits are required")
    definition = {
        "contract": CONTRACT,
        "source_id": source_id,
        "kind": kind,
        "version": version,
        "capabilities": sorted(set(capabilities)),
        "schemas": _redact(dict(schemas)),
        "limits": {key: int(value) for key, value in sorted(limits.items())},
        "pagination": {"kind": "opaque-cursor", "stable": True},
        "freshness": dict(freshness or {"kind": "unknown"}),
        "authorization_scopes": sorted(set(authorization_scopes)),
        "consistency": consistency,
        "score_semantics": score_semantics,
        "temporal_support": temporal_support,
        "failure_behavior": failure_behavior,
    }
    definition["capability_hash"] = _digest(definition)
    return definition


@runtime_checkable
class KnowledgeSourceAdapter(Protocol):
    """Read-only contract implemented by every federated source."""

    def describe(self) -> dict[str, Any]: ...

    def query(self, request: Mapping[str, Any], *, scopes: set[str]) -> dict[str, Any]: ...


def _envelope(
    definition: Mapping[str, Any],
    request: Mapping[str, Any],
    items: Sequence[Mapping[str, Any]],
    *,
    started: float,
    cursor: str | None = None,
    warnings: Sequence[str] = (),
) -> dict[str, Any]:
    clean_items = [_redact(dict(item)) for item in items]
    provenance = {
        "source_id": definition["source_id"],
        "source_kind": definition["kind"],
        "source_version": definition["version"],
        "capability_hash": definition["capability_hash"],
        "query_hash": _digest(_redact(dict(request))),
        "observed_at_ms": int(time.time() * 1000),
        "freshness": definition["freshness"],
        "consistency": definition["consistency"],
    }
    return {
        "contract": "noesis-source-result-v1",
        "source": definition["source_id"],
        "items": clean_items,
        "cursor": cursor,
        "coverage": {"complete": cursor is None, "returned": len(clean_items)},
        "score_semantics": definition["score_semantics"],
        "temporal_support": definition["temporal_support"],
        "warnings": list(warnings),
        "provenance": provenance,
        "latency_ms": max(0, round((time.monotonic() - started) * 1000, 3)),
    }


class FakeKnowledgeAdapter:
    """Deterministic offline conformance adapter, including injected failures."""

    def __init__(self, source_id: str, items: Sequence[Mapping[str, Any]], **overrides: Any):
        self.items = [dict(item) for item in items]
        self.definition = source_definition(
            source_id,
            "fake",
            capabilities=overrides.pop("capabilities", ["search", "temporal"]),
            schemas=overrides.pop("schemas", {"knowledge": {"type": "object"}}),
            limits=overrides.pop(
                "limits", {"max_results": 100, "timeout_ms": 1000, "max_bytes": 1_000_000}
            ),
            score_semantics=overrides.pop("score_semantics", "higher-is-better"),
            **overrides,
        )
        self.calls: list[dict[str, Any]] = []

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def query(self, request: Mapping[str, Any], *, scopes: set[str]) -> dict[str, Any]:
        started = time.monotonic()
        _authorize(self.definition, scopes)
        self.calls.append(dict(request))
        if request.get("fail"):
            raise FederationError("source_unavailable", "deterministic fake failure")
        limit = min(int(request.get("limit", 20)), self.definition["limits"]["max_results"])
        offset = int(request.get("cursor", "0"))
        selected = self.items[offset : offset + limit]
        cursor = str(offset + limit) if offset + limit < len(self.items) else None
        return _envelope(self.definition, request, selected, started=started, cursor=cursor)


def _authorize(definition: Mapping[str, Any], scopes: set[str]) -> None:
    required = set(definition.get("authorization_scopes", ()))
    if "operator" not in scopes and not required.intersection(scopes):
        raise FederationError("unauthorized", "caller cannot query this source")


class SQLKnowledgeAdapter:
    """Typed select/aggregate adapter; callers can never provide SQL text."""

    def __init__(
        self,
        source_id: str,
        connection_factory: Callable[[], Any],
        allowed_tables: Mapping[str, Sequence[str] | None],
        *,
        dialect: str = "duckdb",
        limits: Mapping[str, int] | None = None,
    ) -> None:
        if dialect not in {"duckdb", "postgresql"}:
            raise FederationError("invalid_source", "SQL dialect must be duckdb or postgresql")
        for table, columns in allowed_tables.items():
            self._identifier(table)
            for column in columns or ():
                self._identifier(column)
        self.connection_factory = connection_factory
        self.allowed_tables = {k: None if v is None else tuple(v) for k, v in allowed_tables.items()}
        self.dialect = dialect
        self.definition = source_definition(
            source_id,
            "sql",
            capabilities=["schema", "select", "aggregate"],
            schemas={"tables": sorted(allowed_tables)},
            limits=limits
            or {"max_results": 500, "timeout_ms": 5000, "max_bytes": 2_000_000, "max_cost": 10_000},
            consistency="backend-snapshot",
            temporal_support="declared-columns",
        )

    @staticmethod
    def _identifier(value: str) -> str:
        if not _IDENTIFIER.fullmatch(str(value)):
            raise FederationError("invalid_identifier", "SQL identifiers must be simple names")
        return str(value)

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def discover_schema(self, *, scopes: set[str]) -> dict[str, Any]:
        _authorize(self.definition, scopes)
        conn = self.connection_factory()
        try:
            result: dict[str, list[dict[str, str]]] = {}
            for table, allow_columns in sorted(self.allowed_tables.items()):
                placeholder = "%s" if self.dialect == "postgresql" else "?"
                rows = conn.execute(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    f"WHERE table_name = {placeholder} ORDER BY ordinal_position",
                    [table],
                ).fetchall()
                result[table] = [
                    {"name": str(name), "type": str(data_type)}
                    for name, data_type in rows
                    if allow_columns is None or name in allow_columns
                ]
            return {"source": self.definition["source_id"], "tables": result}
        finally:
            conn.close()

    def query(self, request: Mapping[str, Any], *, scopes: set[str]) -> dict[str, Any]:
        started = time.monotonic()
        _authorize(self.definition, scopes)
        if "sql" in request or "query" in request:
            raise FederationError("arbitrary_sql_forbidden", "use typed select or aggregate operations")
        table = self._identifier(str(request.get("table", "")))
        if table not in self.allowed_tables:
            raise FederationError("table_forbidden", "table is not allowlisted")
        operation = str(request.get("operation", "select"))
        allowed = self.allowed_tables[table]
        columns = [self._identifier(str(item)) for item in request.get("columns", [])]
        if not columns:
            columns = list(allowed or ())
        if not columns:
            raise FederationError("columns_required", "explicit columns are required for partial schemas")
        if allowed is not None and set(columns) - set(allowed):
            raise FederationError("column_forbidden", "a selected column is not allowlisted")
        placeholder = "%s" if self.dialect == "postgresql" else "?"
        params: list[Any] = []
        clauses: list[str] = []
        for condition in request.get("filters", []):
            column = self._identifier(str(condition.get("column", "")))
            operator = str(condition.get("operator", "eq"))
            if allowed is not None and column not in allowed:
                raise FederationError("column_forbidden", "a filter column is not allowlisted")
            sql_operator = {"eq": "=", "ne": "<>", "lt": "<", "lte": "<=", "gt": ">", "gte": ">="}.get(operator)
            if sql_operator is None:
                raise FederationError("operator_forbidden", "unsupported filter operator")
            clauses.append(f'"{column}" {sql_operator} {placeholder}')
            params.append(condition.get("value"))
        if operation == "select":
            expression = ", ".join(f'"{column}"' for column in columns)
        elif operation == "aggregate":
            aggregate = str(request.get("aggregate", "count"))
            if aggregate not in {"count", "min", "max", "sum", "avg"}:
                raise FederationError("aggregate_forbidden", "unsupported aggregate")
            target = "*" if aggregate == "count" else f'"{columns[0]}"'
            expression = f"{aggregate.upper()}({target}) AS value"
        else:
            raise FederationError("operation_forbidden", "operation must be select or aggregate")
        limit = min(max(int(request.get("limit", 50)), 1), self.definition["limits"]["max_results"])
        offset = max(0, int(request.get("cursor", "0") or 0))
        estimated_cost = (limit + 1) * max(1, len(columns)) * max(1, len(clauses) + 1)
        if estimated_cost > self.definition["limits"].get("max_cost", estimated_cost):
            raise FederationError("cost_exceeded", "SQL request exceeds its declared cost budget")
        sql = f'SELECT {expression} FROM "{table}"'  # identifiers validated above
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += f" LIMIT {placeholder} OFFSET {placeholder}"
        params.extend([limit + 1, offset])
        conn = self.connection_factory()
        try:
            before = time.monotonic()
            cursor = conn.execute(sql, params)
            names = [str(item[0]) for item in cursor.description]
            rows = cursor.fetchall()
            elapsed_ms = (time.monotonic() - before) * 1000
            if elapsed_ms > self.definition["limits"]["timeout_ms"]:
                raise FederationError("source_timeout", "SQL source exceeded its time budget")
            more = len(rows) > limit
            items = []
            for index, row in enumerate(rows[:limit]):
                value = dict(zip(names, row, strict=True))
                items.append(
                    {
                        "id": f"{self.definition['source_id']}:{table}:{_digest(value)[:20]}",
                        "type": "row",
                        "value": value,
                        "backend": {"table": table, "row_offset": offset + index},
                    }
                )
            if len(_canonical(items).encode()) > self.definition["limits"]["max_bytes"]:
                raise FederationError("result_too_large", "SQL result exceeded its byte budget")
            return _envelope(
                self.definition,
                request,
                items,
                started=started,
                cursor=(str(offset + limit) if more else None),
            )
        finally:
            conn.close()


class RemoteMCPAdapter:
    """Allowlisted remote MCP resources/tools behind an injected safe client."""

    def __init__(self, source_id: str, client: Any, *, resources: Sequence[str] = (), tools: Sequence[str] = (), cache_ttl_ms: int = 60_000, limits: Mapping[str, int] | None = None):
        self.client, self.allowed_resources, self.allowed_tools = client, set(resources), set(tools)
        self.cache_ttl_ms, self._cache = cache_ttl_ms, None
        self.definition = source_definition(
            source_id, "mcp", capabilities=["resources", "tools"],
            schemas={"resources": sorted(resources), "tools": sorted(tools)},
            limits=limits or {"max_results": 100, "timeout_ms": 5000, "max_bytes": 1_000_000},
            consistency="remote-declared", temporal_support="remote-declared",
        )

    def describe(self) -> dict[str, Any]: return dict(self.definition)

    def refresh(self, *, scopes: set[str]) -> dict[str, Any]:
        _authorize(self.definition, scopes)
        advertised = {"resources": self.client.list_resources(), "tools": self.client.list_tools()}
        version = getattr(self.client, "version", "unknown")
        previous = self._cache
        self._cache = {
            "advertised": _redact(advertised),
            "refreshed_at_ms": int(time.time() * 1000),
            "version": version,
            "schema_drift": bool(
                previous
                and (
                    previous["version"] != version
                    or previous["advertised"] != _redact(advertised)
                )
            ),
        }
        return dict(self._cache)

    def capabilities(self, *, scopes: set[str]) -> dict[str, Any]:
        now = int(time.time() * 1000)
        if self._cache is None or now - self._cache["refreshed_at_ms"] > self.cache_ttl_ms:
            return self.refresh(scopes=scopes)
        return dict(self._cache)

    def query(self, request: Mapping[str, Any], *, scopes: set[str]) -> dict[str, Any]:
        started = time.monotonic(); _authorize(self.definition, scopes); self.capabilities(scopes=scopes)
        kind, name = str(request.get("kind", "resource")), str(request.get("name", ""))
        arguments = _redact(dict(request.get("arguments") or {}))
        if kind == "resource" and name in self.allowed_resources:
            raw = self.client.read_resource(name)
        elif kind == "tool" and name in self.allowed_tools:
            raw = self.client.call_tool(name, arguments)
        else:
            raise FederationError("remote_operation_forbidden", "remote operation is not allowlisted")
        if isinstance(raw, Mapping) and any(key in raw for key in ("prompts", "roots")):
            raise FederationError("untrusted_control_content", "remote control-plane content is not knowledge")
        clean = _redact(raw)
        if (time.monotonic() - started) * 1000 > self.definition["limits"]["timeout_ms"]:
            raise FederationError("source_timeout", "remote MCP source exceeded its time budget")
        if len(_canonical(clean).encode()) > self.definition["limits"]["max_bytes"]:
            raise FederationError("result_too_large", "remote MCP output exceeded its byte budget")
        item = {"id": f"mcp:{_digest([name, arguments, clean])[:20]}", "type": kind, "value": clean, "backend": {"operation": name, "arguments_hash": _digest(arguments), "server_version": (self._cache or {}).get("version", "unknown")}}
        return _envelope(self.definition, request, [item], started=started)


class VectorStoreAdapter:
    def __init__(self, source_id: str, backend: Any, *, namespaces: Sequence[str], limits: Mapping[str, int] | None = None):
        self.backend, self.namespaces = backend, set(namespaces)
        self.definition = source_definition(source_id, "vector", capabilities=["semantic-search"], schemas={"result": "vector-hit"}, limits=limits or {"max_results": 100, "timeout_ms": 3000, "max_bytes": 1_000_000}, score_semantics="backend-native; higher-is-better", temporal_support="filter")

    def describe(self) -> dict[str, Any]: return dict(self.definition)

    def query(self, request: Mapping[str, Any], *, scopes: set[str]) -> dict[str, Any]:
        started=time.monotonic(); _authorize(self.definition, scopes)
        namespace=str(request.get("namespace", ""))
        if namespace not in self.namespaces or f"namespace:{namespace}:read" not in scopes and "operator" not in scopes:
            raise FederationError("namespace_forbidden", "vector namespace is not authorized")
        limit=min(int(request.get("limit", 20)), self.definition["limits"]["max_results"])
        hits=self.backend.search(request.get("vector") or request.get("text"), namespace=namespace, limit=limit, filters=dict(request.get("filters") or {}))
        items=[{"id": str(hit["id"]), "type": "vector-hit", "value": _redact(hit.get("value", {})), "score": hit.get("score"), "backend": {"score": hit.get("score"), "metric": hit.get("metric", "backend-native"), "namespace": namespace}} for hit in hits[:limit]]
        if (time.monotonic()-started)*1000>self.definition["limits"]["timeout_ms"]:raise FederationError("source_timeout","vector source exceeded its time budget")
        if len(_canonical(items).encode())>self.definition["limits"]["max_bytes"]:raise FederationError("result_too_large","vector result exceeded its byte budget")
        return _envelope(self.definition, request, items, started=started)


class GraphStoreAdapter:
    def __init__(self, source_id: str, backend: Any, *, namespaces: Sequence[str], max_depth: int = 3, limits: Mapping[str, int] | None = None):
        self.backend, self.namespaces, self.max_depth = backend, set(namespaces), max_depth
        self.definition = source_definition(source_id, "graph", capabilities=["neighbors", "paths"], schemas={"result": "graph-traversal"}, limits=limits or {"max_results": 200, "timeout_ms": 3000, "max_bytes": 2_000_000}, score_semantics="path-specific", temporal_support="edge-validity")

    def describe(self) -> dict[str, Any]: return dict(self.definition)

    def query(self, request: Mapping[str, Any], *, scopes: set[str]) -> dict[str, Any]:
        started=time.monotonic(); _authorize(self.definition, scopes)
        namespace=str(request.get("namespace", "")); depth=int(request.get("depth", 1))
        if namespace not in self.namespaces or f"namespace:{namespace}:read" not in scopes and "operator" not in scopes:
            raise FederationError("namespace_forbidden", "graph namespace is not authorized")
        if not 1 <= depth <= self.max_depth: raise FederationError("depth_exceeded", "graph depth exceeds the source limit")
        limit=min(int(request.get("limit", 50)), self.definition["limits"]["max_results"])
        hits=self.backend.traverse(str(request.get("start_id", "")), namespace=namespace, depth=depth, predicates=list(request.get("predicates") or []), limit=limit)
        items=[{"id": str(hit.get("id") or _digest(hit)[:20]), "type": "graph-hit", "value": _redact(hit), "backend": {"depth": hit.get("depth"), "path": hit.get("path"), "namespace": namespace}} for hit in hits[:limit]]
        if (time.monotonic()-started)*1000>self.definition["limits"]["timeout_ms"]:raise FederationError("source_timeout","graph source exceeded its time budget")
        if len(_canonical(items).encode())>self.definition["limits"]["max_bytes"]:raise FederationError("result_too_large","graph result exceeded its byte budget")
        return _envelope(self.definition, request, items, started=started)


@dataclass
class FederationRegistry:
    adapters: dict[str, KnowledgeSourceAdapter]

    def __init__(self, adapters: Sequence[KnowledgeSourceAdapter] = ()) -> None:
        self.adapters = {}
        for adapter in adapters: self.add(adapter)

    def add(self, adapter: KnowledgeSourceAdapter) -> None:
        definition=adapter.describe(); source_id=str(definition["source_id"])
        if source_id in self.adapters: raise FederationError("duplicate_source", "source identity is already registered")
        self.adapters[source_id]=adapter

    def list(self, *, scopes: set[str]) -> list[dict[str, Any]]:
        return [adapter.describe() for _, adapter in sorted(self.adapters.items()) if set(adapter.describe()["authorization_scopes"]).intersection(scopes) or "operator" in scopes]


class FederatedQueryEngine:
    """Capability-aware bounded execution and honest deterministic merging."""

    def __init__(self, registry: FederationRegistry, *, max_workers: int = 4): self.registry, self.max_workers = registry, max_workers

    def plan(self, request: Mapping[str, Any], *, scopes: set[str]) -> dict[str, Any]:
        capability=str(request.get("capability", "search")); requested=set(request.get("sources") or self.registry.adapters)
        selected=[]; omitted=[]
        for source_id, adapter in sorted(self.registry.adapters.items()):
            definition=adapter.describe()
            if source_id not in requested: continue
            if capability not in definition["capabilities"]:
                omitted.append({"source": source_id, "reason": "capability"}); continue
            if "operator" not in scopes and not set(definition["authorization_scopes"]).intersection(scopes):
                omitted.append({"source": source_id, "reason": "authorization"}); continue
            selected.append(source_id)
        plan={"selected": selected, "omitted": omitted, "capability": capability, "request_hash": _digest(_redact(dict(request)))}
        plan["plan_hash"]=_digest(plan); return plan

    def execute(
        self,
        request: Mapping[str, Any],
        *,
        scopes: set[str],
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        started=time.monotonic(); plan=self.plan(request, scopes=scopes); results=[]; failures=[]
        per_source=dict(request.get("per_source") or {}); budgets=dict(request.get("source_budgets") or {}); timeout_ms=max(1, int(request.get("timeout_ms", 5000)));max_retries=min(3,max(0,int(request.get("max_retries",1))))
        def run(source_id: str) -> tuple[str, dict[str, Any]]:
            child=dict(request.get("query") or {}); child.update(per_source.get(source_id) or {})
            definition=self.registry.adapters[source_id].describe();budget=dict(budgets.get(source_id) or {})
            if "max_results" in budget:child["limit"]=min(int(child.get("limit",budget["max_results"])),int(budget["max_results"]),int(definition["limits"]["max_results"]))
            last_error=None
            for attempt in range(max_retries+1):
                if cancelled and cancelled():raise FederationError("cancelled","federated query was cancelled")
                try:
                    result=self.registry.adapters[source_id].query(child,scopes=scopes);result["provenance"]["attempts"]=attempt+1;return source_id,result
                except FederationError as exc:
                    last_error=exc
                    if exc.code not in {"source_unavailable","source_timeout"} or attempt>=max_retries:raise
            raise last_error or FederationError("source_failed","source failed without an error")
        if cancelled and cancelled():
            return {"contract":RESULT_CONTRACT,"plan":plan,"results":[],"failures":[],"coverage":{"requested":len(plan["selected"]),"completed":0,"partial":True,"omitted":plan["omitted"]},"contradictions":[],"cancelled":True,"latency_ms":0,"replay_hash":_digest([plan,"cancelled"])}
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(self.max_workers, max(1, len(plan["selected"])))) as executor:
            futures={executor.submit(run, source): source for source in plan["selected"]}
            try:
                for future in concurrent.futures.as_completed(futures, timeout=timeout_ms / 1000):
                    source=futures[future]
                    try: results.append(future.result())
                    except Exception as exc:  # noqa: BLE001 - isolate independent sources
                        error=exc.as_dict() if isinstance(exc, FederationError) else {"code": "source_failed", "message": str(exc)[:300]}
                        failures.append({"source": source, "error": _redact(error)})
            except TimeoutError:
                pass
            for future, source in futures.items():
                if not future.done(): future.cancel(); failures.append({"source": source, "error": {"code": "source_timeout", "message": "federation time budget exhausted"}})
        dedup: dict[str, dict[str, Any]]={}; contradictions=[]
        for source_id, result in sorted(results):
            for item in result["items"]:
                identity=str(item.get("canonical_id") or item.get("id") or _digest(item))
                existing=dedup.get(identity)
                evidence={"source": source_id, "item": item, "provenance": result["provenance"], "score_semantics": result["score_semantics"]}
                if existing is None: dedup[identity]={"identity": identity, "evidence": [evidence]}
                else:
                    if existing["evidence"][0]["item"].get("value") != item.get("value"): contradictions.append(identity)
                    existing["evidence"].append(evidence)
        merged=[dedup[key] for key in sorted(dedup)]
        output={"contract": RESULT_CONTRACT, "plan": plan, "results": merged, "failures": sorted(failures, key=lambda x:x["source"]), "coverage": {"requested": len(plan["selected"])+len(plan["omitted"]), "completed": len(results), "partial": bool(failures or plan["omitted"]), "omitted": plan["omitted"]}, "contradictions": sorted(set(contradictions)), "latency_ms": round((time.monotonic()-started)*1000,3)}
        replay_results = [
            {
                "identity": item["identity"],
                "evidence": [
                    {
                        "source": evidence["source"],
                        "item": evidence["item"],
                        "query_hash": evidence["provenance"]["query_hash"],
                        "capability_hash": evidence["provenance"]["capability_hash"],
                        "score_semantics": evidence["score_semantics"],
                    }
                    for evidence in item["evidence"]
                ],
            }
            for item in merged
        ]
        output["replay_hash"] = _digest(
            {
                "plan": plan,
                "results": replay_results,
                "failures": output["failures"],
                "coverage": output["coverage"],
                "contradictions": output["contradictions"],
            }
        )
        return output

    @staticmethod
    def evaluate(result: Mapping[str, Any], *, expected_ids: Sequence[str] = ()) -> dict[str, Any]:
        found={str(item["identity"]) for item in result.get("results", [])}; expected=set(expected_ids)
        evidence=[ev for item in result.get("results", []) for ev in item.get("evidence", [])]
        return {"recall": (len(found & expected)/len(expected) if expected else None), "provenance_completeness": (sum(bool(ev.get("provenance")) for ev in evidence)/len(evidence) if evidence else 1.0), "partial_failure": bool(result.get("failures")), "latency_ms": result.get("latency_ms"), "replay_hash": result.get("replay_hash")}
