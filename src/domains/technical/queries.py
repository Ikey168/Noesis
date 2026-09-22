"""Cycle-safe, cited technical graph queries."""

from __future__ import annotations

import json
import time
from collections import deque
from typing import Any

from src.domains.technical.model import (
    ensure_technical_schema,
    resolve_package,
    version_in_events,
)
from src.kb.temporal import parse_source_time

TECHNICAL_RESEARCH_CONTRACT = "noesis-technical-research-v1"
QUERY_TYPES = frozenset(
    {
        "dependency_paths",
        "depends_on",
        "affected_by",
        "fixed_in",
        "supersedes",
        "implements",
        "breaking_changes",
    }
)


class TechnicalQueryError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _object(conn: Any, domain: str, identifier: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT object_id, object_type, coordinate, canonical_name, version, status, "
        "source_url, source_document_id, metadata_json FROM technical_objects "
        "WHERE domain=? AND (object_id=? OR coordinate=? OR immutable_id=?) "
        "ORDER BY CASE WHEN object_id=? THEN 0 ELSE 1 END LIMIT 1",
        [domain, identifier, identifier, identifier, identifier],
    ).fetchone()
    if not row:
        package = resolve_package(conn, identifier, domain=domain)
        if not package:
            return None
        return _object(conn, domain, package["object_id"])
    return {
        "object_id": row[0], "object_type": row[1], "coordinate": row[2],
        "canonical_name": row[3], "version": row[4], "status": row[5],
        "source_url": row[6], "source_document_id": row[7],
        "metadata": json.loads(row[8] or "{}"),
    }


def _relations(
    conn: Any,
    domain: str,
    *,
    subject_id: str | None = None,
    object_id: str | None = None,
    relation: str | None = None,
    observed: int,
) -> list[dict[str, Any]]:
    clauses, params = ["domain=?", "observed_at_ms<=?"], [domain, observed]
    if subject_id:
        clauses.append("subject_id=?")
        params.append(subject_id)
    if object_id:
        clauses.append("object_id=?")
        params.append(object_id)
    if relation:
        clauses.append("relation=?")
        params.append(relation)
    rows = conn.execute(
        "SELECT relation_id, subject_id, relation, object_id, constraint_text, optional, "
        "observed_at_ms, source_url, source_document_id, metadata_json "
        "FROM technical_relations WHERE " + " AND ".join(clauses)
        + " ORDER BY relation_id",
        params,
    ).fetchall()
    keys = (
        "relation_id", "subject_id", "relation", "object_id", "constraint",
        "optional", "observed_at_ms", "source_url", "source_document_id", "metadata",
    )
    result = []
    for row in rows:
        item = dict(zip(keys, row))
        item["metadata"] = json.loads(item["metadata"] or "{}")
        item["optional"] = bool(item["optional"])
        result.append(item)
    return result


def _citation(edge: dict[str, Any]) -> dict[str, Any]:
    return {
        "relation_id": edge["relation_id"],
        "source_url": edge["source_url"],
        "source_document_id": edge["source_document_id"],
        "observed_at_ms": edge["observed_at_ms"],
        "locator_available": bool(edge["source_url"] or edge["source_document_id"]),
    }


def _root_id(
    conn: Any, domain: str, coordinate: str | None, version: str | None
) -> tuple[str, dict[str, Any]]:
    if not coordinate:
        raise TechnicalQueryError("bad_request", "coordinate is required")
    package = resolve_package(conn, coordinate, domain=domain)
    if package is None:
        technical_object = _object(conn, domain, coordinate)
        if technical_object is None:
            raise TechnicalQueryError(
                "not_found",
                "exact coordinate, object ID, or recorded alias was not found",
            )
        if version:
            raise TechnicalQueryError(
                "bad_request", "version can only qualify a package coordinate"
            )
        return str(technical_object["object_id"]), technical_object
    if version:
        row = conn.execute(
            "SELECT object_id FROM technical_objects WHERE domain=? AND object_type='version' "
            "AND coordinate=? AND version=? ORDER BY object_id LIMIT 1",
            [domain, package["coordinate"], version],
        ).fetchone()
        if row is None:
            raise TechnicalQueryError("not_found", "exact package version was not found")
        return str(row[0]), package
    return str(package["object_id"]), package


def _dependency_paths(
    conn: Any,
    domain: str,
    root: str,
    target: str | None,
    include_optional: bool,
    max_depth: int,
    observed: int,
    limit: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    queue = deque([(root, [root], [])])
    results, citations, cycles = [], [], []
    seen_states: set[tuple[str, tuple[str, ...]]] = set()
    while queue and len(results) < limit:
        node, nodes, edges = queue.popleft()
        if len(edges) >= max_depth:
            continue
        outgoing = _relations(conn, domain, subject_id=node, observed=observed)
        outgoing = [
            edge for edge in outgoing
            if edge["relation"] in {"depends_on", "optional_dependency"}
            and (include_optional or not edge["optional"])
        ]
        for edge in outgoing:
            child = edge["object_id"]
            next_nodes, next_edges = [*nodes, child], [*edges, edge]
            if child in nodes:
                cycles.append(" -> ".join(next_nodes))
                continue
            state = (child, tuple(next_nodes))
            if state in seen_states:
                continue
            seen_states.add(state)
            if target is None or child == target:
                results.append(
                    {
                        "nodes": [
                            _object(conn, domain, item) or {"object_id": item, "status": "unresolved"}
                            for item in next_nodes
                        ],
                        "edges": next_edges,
                        "constraints": [item["constraint"] for item in next_edges],
                    }
                )
                citations.extend(_citation(item) for item in next_edges)
                if len(results) >= limit:
                    break
            queue.append((child, next_nodes, next_edges))
    unique_citations = {
        item["relation_id"]: item for item in citations
    }
    return results, list(unique_citations.values()), sorted(set(cycles))


def _affected(
    conn: Any,
    domain: str,
    package_id: str,
    version: str | None,
    observed: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    edges = _relations(
        conn, domain, subject_id=package_id, relation="affected_by", observed=observed
    )
    results, citations = [], []
    for edge in edges:
        ranges = conn.execute(
            "SELECT range_type, events_json, source_document_id, observed_at_ms "
            "FROM technical_advisory_ranges WHERE domain=? AND package_id=? "
            "AND advisory_id=? AND observed_at_ms<=? ORDER BY observed_at_ms DESC",
            [domain, package_id, edge["object_id"], observed],
        ).fetchall()
        evaluations = [
            {
                "range_type": row[0],
                "events": json.loads(row[1]),
                "affected": version_in_events(version, json.loads(row[1])) if version else None,
                "source_document_id": row[2],
                "observed_at_ms": row[3],
            }
            for row in ranges
        ]
        if version and evaluations and not any(item["affected"] for item in evaluations):
            continue
        results.append(
            {
                "advisory": _object(conn, domain, edge["object_id"]),
                "requested_version": version,
                "ranges": evaluations,
                "citation": _citation(edge),
            }
        )
        citations.append(_citation(edge))
    return results, citations


def technical_research(
    backing: Any,
    *,
    query_type: str,
    coordinate: str | None = None,
    version: str | None = None,
    target_id: str | None = None,
    include_optional: bool = False,
    max_depth: int = 8,
    observed_before: Any = None,
    limit: int = 100,
) -> dict[str, Any]:
    if query_type not in QUERY_TYPES:
        raise TechnicalQueryError(
            "bad_request", f"query_type must be one of {sorted(QUERY_TYPES)}"
        )
    try:
        page_size, depth = int(limit), int(max_depth)
    except (TypeError, ValueError) as exc:
        raise TechnicalQueryError("bad_request", "limit and max_depth must be integers") from exc
    if not 1 <= page_size <= 1000 or not 1 <= depth <= 32:
        raise TechnicalQueryError(
            "bad_request", "limit must be 1..1000 and max_depth must be 1..32"
        )
    try:
        observed = (
            parse_source_time(observed_before, field="observed_before")[0]
            if observed_before is not None else int(time.time() * 1000)
        )
    except Exception as exc:
        raise TechnicalQueryError("bad_time", "observed_before is invalid") from exc
    conn, domain = backing.conn, backing.definition.name
    ensure_technical_schema(conn)
    root, resolved_object = _root_id(conn, domain, coordinate, version)
    assumptions = [
        "coordinates and aliases resolve exactly; ambiguous package names are not guessed",
        "version ordering uses recorded ecosystem range events",
        f"relations observed after {observed} are excluded",
    ]
    cycles: list[str] = []
    if query_type in {"dependency_paths", "depends_on"}:
        target = None
        if target_id:
            target_obj = _object(conn, domain, target_id)
            if target_obj is None:
                target_pkg = resolve_package(conn, target_id, domain=domain)
                target = target_pkg["object_id"] if target_pkg else target_id
            else:
                target = target_obj["object_id"]
        results, citations, cycles = _dependency_paths(
            conn, domain, root, target, include_optional, depth, observed, page_size
        )
    elif query_type == "affected_by":
        results, citations = _affected(
            conn, domain, resolved_object["object_id"], version, observed
        )
    elif query_type == "fixed_in":
        advisory = _object(conn, domain, target_id or root)
        if not advisory:
            raise TechnicalQueryError("not_found", "advisory was not found")
        edges = _relations(
            conn, domain, subject_id=advisory["object_id"],
            relation="fixed_in", observed=observed,
        )
        results = [
            {"advisory": advisory, "fixed_version": _object(conn, domain, edge["object_id"]), "edge": edge}
            for edge in edges[:page_size]
        ]
        citations = [_citation(edge) for edge in edges[:page_size]]
    else:
        relation = {
            "supersedes": "supersedes",
            "implements": "implements",
            "breaking_changes": "breaking_change",
        }[query_type]
        edges = _relations(
            conn, domain, subject_id=root, relation=relation,
            observed=observed,
        )[:page_size]
        results = [
            {
                "subject": _object(conn, domain, edge["subject_id"]),
                "relation": relation,
                "object": _object(conn, domain, edge["object_id"])
                or {"object_id": edge["object_id"], "status": "unresolved"},
                "edge": edge,
            }
            for edge in edges
        ]
        citations = [_citation(edge) for edge in edges]
    incomplete = any(
        not citation["locator_available"] for citation in citations
    ) or any(
        node.get("status") == "unresolved"
        for result in results if "nodes" in result for node in result["nodes"]
    )
    return {
        "contract": TECHNICAL_RESEARCH_CONTRACT,
        "query": {
            "query_type": query_type,
            "coordinate": coordinate,
            "resolved_coordinate": resolved_object.get("coordinate"),
            "resolved_object_id": resolved_object["object_id"],
            "version": version,
            "target_id": target_id,
            "include_optional": bool(include_optional),
            "max_depth": depth,
            "observed_before_ms": observed,
        },
        "results": results,
        "citations": citations,
        "assumptions": assumptions,
        "cycles": cycles,
        "coverage": {
            "incomplete": incomplete,
            "conflicting_lockfiles": any(
                bool(edge.get("metadata", {}).get("lockfile_conflict"))
                for result in results for edge in result.get("edges", [])
            ),
            "result_count": len(results),
        },
    }


__all__ = [
    "QUERY_TYPES",
    "TECHNICAL_RESEARCH_CONTRACT",
    "TechnicalQueryError",
    "technical_research",
]
