"""
``infrastructure_pivot(identifier)`` - organization-keyed infrastructure pivot
(OX08, #2048).

Answers "which other sources share this domain's registrant organization or
certificate SAN set" over the ``source_relationship`` graph that the RDAP
(OX05) and certificate-transparency (OX06) projections write. Each hop is a
current ``ownership`` or ``shared-infrastructure`` relationship revision,
carried with its evidence (the RDAP / CT observation that states it) and its
status: ``as-registered`` for registrant ownership, ``probable`` for shared
infrastructure. There is no "same operator" verdict field.

Accepted identifiers: a domain name (resolved through the namespace's source
identities), or a source-identity id. Person-keyed identifiers are refused in
code with ``status: "person_identifier_refused"``, the way ``geolocate_claims``
refuses person entities: e-mail addresses, ``@handles``, IPv4/IPv6 literals and
``person:`` ids. This composes ``src.kb.source_identity`` relationships only;
``src.osint.paths`` stays the co-mention path over documents.

Stdlib-only; the connection is injected read-only.
"""

from __future__ import annotations

import ipaddress
import json
import re
from collections import deque
from typing import Any, Dict, List, Optional

from src.osint import common

PIVOT_EDGES = ("ownership", "shared-infrastructure")
MAX_DEPTH = 3
MAX_RESULTS = 50
CAVEAT = (
    "shared certificates or hosting are commonly explained by shared hosting providers, CDNs and managed "
    "certificate services; registrant organizations are as stated in RDAP. Paths are cited relations, never a "
    "same-operator or attribution verdict."
)
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+$")
_HANDLE = re.compile(r"^@\w[\w.\-]*$")


def classify_identifier(identifier: str) -> Optional[str]:
    """The person-keyed form an identifier matches, or None if it is allowed."""
    value = str(identifier or "").strip()
    lowered = value.lower()
    if lowered.startswith("person:"):
        return "person_id"
    if _HANDLE.match(value):
        return "handle"
    if _EMAIL.match(value) or "@" in value:
        return "email"
    try:
        ipaddress.ip_address(value.strip("[]"))
        return "ip_address"
    except ValueError:
        pass
    if lowered.startswith(("username:", "user:", "handle:", "account:")):
        return "username"
    return None


def _refused(identifier: str, form: str) -> Dict[str, Any]:
    return {
        "status": "person_identifier_refused",
        "identifier_form": form,
        "note": (
            "infrastructure pivoting is organization-keyed only; e-mail, username/handle, IP address and "
            "person identifiers are refused"
        ),
        "paths": [],
    }


def _resolve(conn, namespace: str, identifier: str) -> Dict[str, Any]:
    value = identifier.strip()
    if value.startswith("source-identity:"):
        row = conn.execute(
            "SELECT source_id FROM source_identities WHERE namespace = ? AND source_id = ?",
            [namespace, value],
        ).fetchone()
        return {"kind": "source_id", "source_ids": [row[0]] if row else []}
    from src.ingestion.osint_observations import (
        ObservationError,
        normalize_domain,
        source_domains,
    )

    try:
        domain = normalize_domain(value)
    except ObservationError as exc:
        return {
            "kind": "invalid",
            "error": str(exc),
            "code": exc.code,
            "source_ids": [],
        }
    return {
        "kind": "domain",
        "domain": domain,
        "source_ids": source_domains(conn, namespace).get(domain, []),
    }


def _names(conn, namespace: str, source_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    if not source_ids:
        return {}
    ph = ", ".join("?" for _ in source_ids)
    rows = conn.execute(
        "SELECT r.source_id, r.display_name, i.kind FROM source_identity_current c "
        "JOIN source_identity_revisions r USING(revision_id) JOIN source_identities i ON i.source_id = r.source_id "
        f"WHERE r.namespace = ? AND r.source_id IN ({ph})",
        [namespace, *source_ids],
    ).fetchall()
    return {r[0]: {"source_id": r[0], "display_name": r[1], "kind": r[2]} for r in rows}


def _edges(conn, namespace: str) -> List[Dict[str, Any]]:
    ph = ", ".join("?" for _ in PIVOT_EDGES)
    rows = conn.execute(
        "SELECT r.relationship_id, r.relationship_revision_id, r.from_source_id, r.to_source_id, "
        "r.relationship_type, r.confidence, r.evidence_json, r.policy_json, r.observed_at_ms "
        "FROM source_relationship_current c JOIN source_relationship_revisions r USING(relationship_revision_id) "
        f"WHERE r.namespace = ? AND r.lifecycle = 'active' AND r.relationship_type IN ({ph}) "
        "ORDER BY r.relationship_id",
        [namespace, *PIVOT_EDGES],
    ).fetchall()
    out = []
    for rid, rev_id, left, right, kind, confidence, evidence, policy, observed in rows:
        policy_obj = json.loads(policy or "{}")
        evidence_list = json.loads(evidence or "[]")
        citations = [
            {
                k: e.get(k)
                for k in (
                    "observation_id",
                    "source_id",
                    "locator",
                    "archive_at",
                    "certificate_ids",
                    "fact",
                )
                if e.get(k) is not None
            }
            for e in evidence_list
        ]
        out.append(
            {
                "relationship_id": rid,
                "relationship_revision_id": rev_id,
                "from_source_id": left,
                "to_source_id": right,
                "relationship_type": kind,
                "status": policy_obj.get("status")
                or ("probable" if kind == "shared-infrastructure" else "sourced"),
                "confidence": float(confidence),
                "observed_at_ms": int(observed),
                "citations": citations,
                "cited": any(
                    c.get("observation_id") or c.get("locator") for c in citations
                ),
            }
        )
    return out


def infrastructure_pivot(
    conn, identifier: str, *, namespace: str = "osint", max_depth: int = 2
) -> Dict[str, Any]:
    """Sources related to a domain or organization through cited registrant
    ownership and probable shared-infrastructure relations."""
    form = classify_identifier(identifier)
    if form is not None:
        return _refused(identifier, form)
    if not (
        common.table_exists(conn, "source_relationship_current")
        and common.table_exists(conn, "source_identities")
    ):
        return {
            "status": "not_available",
            "identifier": identifier,
            "paths": [],
            "note": "no source-identity relationships in this warehouse",
        }
    depth = min(max(int(max_depth), 1), MAX_DEPTH)
    resolved = _resolve(conn, namespace, identifier)
    if resolved["kind"] == "invalid":
        return {
            "status": resolved["code"],
            "identifier": identifier,
            "error": resolved["error"],
            "paths": [],
        }
    starts = resolved["source_ids"]
    if not starts:
        return {
            "status": "not_found",
            "identifier": identifier,
            "resolution": resolved,
            "paths": [],
            "note": "no source identity in this namespace carries this identifier",
        }
    if len(starts) > 1:
        return {
            "status": "ambiguous",
            "identifier": identifier,
            "candidates": _names(conn, namespace, starts),
            "paths": [],
            "note": "the domain is linked to more than one source identity; pick one",
        }
    start = starts[0]
    edges = _edges(conn, namespace)
    graph: Dict[str, List[Dict[str, Any]]] = {}
    for edge in edges:
        graph.setdefault(edge["from_source_id"], []).append(
            {**edge, "_next": edge["to_source_id"]}
        )
        graph.setdefault(edge["to_source_id"], []).append(
            {**edge, "_next": edge["from_source_id"]}
        )
    paths: List[Dict[str, Any]] = []
    queue = deque([(start, [start], [])])
    seen = {start}
    truncated = False
    while queue:
        node, nodes, hops = queue.popleft()
        if len(hops) >= depth:
            continue
        for edge in sorted(
            graph.get(node, []), key=lambda e: (e["_next"], e["relationship_id"])
        ):
            nxt = edge["_next"]
            if nxt in seen:
                continue
            seen.add(nxt)
            hop = {k: v for k, v in edge.items() if k != "_next"}
            path = {"nodes": nodes + [nxt], "hops": hops + [hop], "reached": nxt}
            if len(paths) >= MAX_RESULTS:
                truncated = True
                break
            paths.append(path)
            queue.append((nxt, path["nodes"], path["hops"]))
    names = _names(
        conn, namespace, sorted({n for p in paths for n in p["nodes"]} | {start})
    )
    for path in paths:
        path["reached_identity"] = names.get(path["reached"])
        path["statuses"] = sorted({h["status"] for h in path["hops"]})
        path["cited"] = all(h["cited"] for h in path["hops"])
    return {
        "status": "ok",
        "identifier": identifier,
        "namespace": namespace,
        "resolution": {**resolved, "source_id": start, "identity": names.get(start)},
        "edge_types": list(PIVOT_EDGES),
        "max_depth": depth,
        "paths": paths,
        "count": len(paths),
        "truncated": truncated,
        "caveat": CAVEAT,
    }
