"""
NeuroNews Lineage explorer — MCP server.

Wraps the Marquez / OpenLineage metadata service so "if I change this stage,
what's downstream?" is a typed tool instead of clicking the Marquez UI or
curling its API. Complements the pipeline runner (run a stage) and the contract
server (is the boundary valid) with the third question: what depends on it.

Tools:
  list_namespaces()                       -> OpenLineage namespaces
  list_nodes(namespace, kind?)            -> datasets and/or jobs in a namespace
  lineage(node, depth=2, namespace?)      -> compact lineage graph (nodes + edges)
  impact(node, direction, namespace?)     -> downstream/upstream reachable set
                                             ("what breaks if I change this")
  run_history(job, namespace?, limit=5)   -> recent runs + states for a job

Talks to Marquez at $MARQUEZ_URL (default http://localhost:5000). Default
namespace is $MARQUEZ_NAMESPACE (default "neuronews"). Degrades gracefully with
a status string when Marquez is unreachable — never hangs (short timeouts).

A `node` is "dataset:<name>", "job:<name>", a full Marquez nodeId
("dataset:<ns>:<name>"), or a bare name (tried as dataset then job).
"""

# ruff: noqa: BLE001, UP045

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from typing import Any, Optional

from fastmcp import FastMCP

mcp = FastMCP("noesis-lineage")

MARQUEZ_URL = os.getenv("MARQUEZ_URL", "http://localhost:5000").rstrip("/")
DEFAULT_NS = os.getenv("MARQUEZ_NAMESPACE", "neuronews")
TIMEOUT = 4
MAX_NODES = 50


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def _get(path: str, params: Optional[dict] = None) -> Any:
    url = f"{MARQUEZ_URL}/api/v1{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read())


def _unreachable(exc: Exception) -> dict:
    return {
        "error": f"Marquez unreachable at {MARQUEZ_URL}: {exc}",
        "hint": "start it: docker compose -f docker-compose.lineage.yml up -d marquez marquez-db",
    }


def _resolve_node_id(node: str, namespace: str) -> str:
    """Turn a user 'node' into a Marquez nodeId 'kind:ns:name'."""
    parts = node.split(":")
    if len(parts) >= 3 and parts[0] in ("dataset", "job"):
        return node  # already a full nodeId
    if len(parts) == 2 and parts[0] in ("dataset", "job"):
        return f"{parts[0]}:{namespace}:{parts[1]}"
    # bare name: prefer dataset, fall back to job (caller may override)
    return f"dataset:{namespace}:{node}"


def _node_exists(node_id: str) -> bool:
    kind, ns, name = node_id.split(":", 2)
    try:
        if kind == "dataset":
            _get(f"/namespaces/{ns}/datasets/{urllib.parse.quote(name, safe='')}")
        else:
            _get(f"/namespaces/{ns}/jobs/{urllib.parse.quote(name, safe='')}")
        return True
    except Exception:
        return False


def _resolve_existing(node: str, namespace: str) -> Optional[str]:
    """Resolve to an existing nodeId, trying dataset then job for bare names."""
    nid = _resolve_node_id(node, namespace)
    if _node_exists(nid):
        return nid
    # bare name resolved to dataset but maybe it's a job
    parts = node.split(":")
    if len(parts) == 1:
        alt = f"job:{namespace}:{node}"
        if _node_exists(alt):
            return alt
    return None


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #

@mcp.tool
def list_namespaces() -> dict:
    """List the OpenLineage namespaces known to Marquez."""
    try:
        data = _get("/namespaces")
    except Exception as exc:
        return _unreachable(exc)
    return {
        "marquez": MARQUEZ_URL,
        "default_namespace": DEFAULT_NS,
        "namespaces": [n["name"] for n in data.get("namespaces", [])],
    }


@mcp.tool
def list_nodes(namespace: Optional[str] = None, kind: str = "all") -> dict:
    """List datasets and/or jobs in a namespace, so you can pick a node for
    lineage()/impact() without guessing names.

    Args:
        namespace: defaults to $MARQUEZ_NAMESPACE ("neuronews").
        kind: "datasets", "jobs", or "all".
    """
    ns = namespace or DEFAULT_NS
    out: dict[str, Any] = {"namespace": ns}
    try:
        if kind in ("datasets", "all"):
            d = _get(f"/namespaces/{ns}/datasets", {"limit": 100})
            out["datasets"] = [x["name"] for x in d.get("datasets", [])][:MAX_NODES]
        if kind in ("jobs", "all"):
            j = _get(f"/namespaces/{ns}/jobs", {"limit": 100})
            out["jobs"] = [x["name"] for x in j.get("jobs", [])][:MAX_NODES]
    except Exception as exc:
        return _unreachable(exc)
    return out


def _fetch_graph(node_id: str, depth: int) -> list[dict]:
    data = _get("/lineage", {"nodeId": node_id, "depth": depth})
    return data.get("graph", [])


@mcp.tool
def lineage(node: str, depth: int = 2, namespace: Optional[str] = None) -> dict:
    """Return a compact lineage graph around a node: the nodes (datasets/jobs)
    and the directed edges between them, up to `depth` hops — not the full
    Marquez facet payload.

    Args:
        node: "dataset:<name>", "job:<name>", a full nodeId, or a bare name.
        depth: hops to traverse (1-5).
        namespace: defaults to $MARQUEZ_NAMESPACE.
    """
    ns = namespace or DEFAULT_NS
    depth = max(1, min(int(depth), 5))
    try:
        node_id = _resolve_existing(node, ns)
        if not node_id:
            return {"error": f"node {node!r} not found in namespace {ns!r}",
                    "hint": "call list_nodes() to see valid names"}
        graph = _fetch_graph(node_id, depth)
    except Exception as exc:
        return _unreachable(exc)

    edges = []
    nodes = []
    for n in graph:
        nodes.append({"id": n["id"], "type": n["type"]})
        for e in n.get("outEdges", []):
            edges.append({"from": e["origin"], "to": e["destination"]})
    return {
        "root": node_id,
        "depth": depth,
        "node_count": len(nodes),
        "nodes": nodes[:MAX_NODES],
        "edges": edges[: MAX_NODES * 2],
    }


@mcp.tool
def impact(node: str, direction: str = "downstream", namespace: Optional[str] = None) -> dict:
    """The reachable set from a node — "what breaks if I change this"
    (downstream) or "what feeds this" (upstream). Walks the lineage edges and
    returns the jobs and datasets reached.

    Args:
        node: "dataset:<name>", "job:<name>", a full nodeId, or a bare name.
        direction: "downstream" (consumers) or "upstream" (producers).
        namespace: defaults to $MARQUEZ_NAMESPACE.
    """
    ns = namespace or DEFAULT_NS
    direction = direction.lower()
    if direction not in ("downstream", "upstream"):
        return {"error": "direction must be 'downstream' or 'upstream'"}
    try:
        node_id = _resolve_existing(node, ns)
        if not node_id:
            return {"error": f"node {node!r} not found in namespace {ns!r}",
                    "hint": "call list_nodes() to see valid names"}
        graph = _fetch_graph(node_id, 5)
    except Exception as exc:
        return _unreachable(exc)

    # Build adjacency from the graph's edges.
    adj: dict[str, list[str]] = {}
    kinds: dict[str, str] = {}
    for n in graph:
        kinds[n["id"]] = n["type"]
        for e in n.get("outEdges", []):
            adj.setdefault(e["origin"], []).append(e["destination"])
        for e in n.get("inEdges", []):
            # reverse adjacency for upstream walks
            adj.setdefault("<rev>" + e["destination"], []).append(e["origin"])

    start = node_id
    seen = {start}
    order = []
    q = deque([start])
    while q and len(order) < MAX_NODES:
        cur = q.popleft()
        key = cur if direction == "downstream" else "<rev>" + cur
        for nxt in adj.get(key, []):
            if nxt not in seen:
                seen.add(nxt)
                order.append(nxt)
                q.append(nxt)

    jobs = [n for n in order if n.startswith("job:")]
    datasets = [n for n in order if n.startswith("dataset:")]
    return {
        "root": node_id,
        "direction": direction,
        "reached_count": len(order),
        "jobs": jobs[:MAX_NODES],
        "datasets": datasets[:MAX_NODES],
    }


@mcp.tool
def run_history(job: str, namespace: Optional[str] = None, limit: int = 5) -> dict:
    """Recent runs of a job with their states and timings — did the stage run,
    and did it fail?

    Args:
        job: the job name (e.g. "nlp.sentiment"), or "job:<name>".
        namespace: defaults to $MARQUEZ_NAMESPACE.
        limit: how many recent runs (1-20).
    """
    ns = namespace or DEFAULT_NS
    name = job.split(":")[-1]
    limit = max(1, min(int(limit), 20))
    try:
        data = _get(f"/namespaces/{ns}/jobs/{urllib.parse.quote(name, safe='')}/runs",
                    {"limit": limit})
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"error": f"job {name!r} not found in namespace {ns!r}",
                    "hint": "call list_nodes(kind='jobs')"}
        return _unreachable(exc)
    except Exception as exc:
        return _unreachable(exc)

    runs = [
        {
            "state": r.get("state"),
            "startedAt": r.get("startedAt"),
            "endedAt": r.get("endedAt"),
            "durationMs": r.get("durationMs"),
        }
        for r in data.get("runs", [])
    ]
    states = {}
    for r in runs:
        states[r["state"]] = states.get(r["state"], 0) + 1
    return {"job": name, "namespace": ns, "run_count": len(runs),
            "state_summary": states, "runs": runs}


@mcp.tool
def artifact_upstream(artifact_id: str) -> dict:
    """Traverse exact local derived-artifact inputs, including historical gaps."""
    try:
        import duckdb

        from src.config.env import warehouse_path
        from src.kb.artifacts import ArtifactGraph

        conn = duckdb.connect(warehouse_path(), read_only=True)
        try:
            return ArtifactGraph(conn, initialize=False).upstream(artifact_id)
        finally:
            conn.close()
    except Exception as exc:
        return {"artifact_id": artifact_id, "edges": [], "error": str(exc)[:300]}


@mcp.tool
def artifact_downstream(dependency_id: str, namespace: Optional[str] = None) -> dict:
    """Report all local artifacts affected by a source, model, schema, or artifact."""
    try:
        import duckdb

        from src.config.env import warehouse_path
        from src.kb.artifacts import ArtifactGraph

        conn = duckdb.connect(warehouse_path(), read_only=True)
        try:
            return ArtifactGraph(conn, initialize=False).downstream(
                dependency_id, namespace=namespace
            )
        finally:
            conn.close()
    except Exception as exc:
        return {"dependency_id": dependency_id, "edges": [], "affected": [], "error": str(exc)[:300]}


if __name__ == "__main__":
    from src.mcp_host.transport import run_server

    run_server(mcp)  # stdio by default; HTTP via NOESIS_MCP_TRANSPORT=http
