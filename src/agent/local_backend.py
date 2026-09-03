"""
In-process backend for the agent runtime (M10.2+).

:func:`build_local_caller` returns a tool caller for :class:`AgentRuntime` that
dispatches the three planes to their real Python backends against an injected
DuckDB connection, instead of over live MCP transports:

* **provisioning** -> :class:`src.provisioning.provisioner.Provisioner`,
* **osint** -> the ``src.osint`` composition functions,

The tool names and argument shapes match the MCP tool surface, so an agent
written against the runtime is identical whether it runs over live MCP
(``runtime.live_caller``) or in-process here. This is what lets the agents run
end to end deterministically in tests and offline acceptance harnesses.

Stdlib + duckdb (via the injected connection).
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional


def build_local_caller(conn, clock: Optional[Callable[[], Any]] = None):
    """A ``(server, tool, arguments) -> result`` caller backed by the real
    provisioning / OSINT code against ``conn``."""
    from src.provisioning.provisioner import Provisioner

    prov = Provisioner(conn, clock=clock) if clock else Provisioner(conn)

    def _provisioning(tool: str, a: Dict[str, Any]) -> Any:
        if tool == "kg_deploy":
            return prov.deploy(
                a["name"], a.get("description", ""), ontology=a.get("ontology"),
                approve=a.get("approve", True), backend=a.get("backend", "table-prefix"),
            )
        if tool == "kg_attach_sources":
            return prov.attach_sources(a["name"], sources=a.get("sources"), criteria=a.get("criteria"))
        if tool == "kg_attach_pipeline":
            return prov.attach_pipeline(
                a["name"], connector=a["connector"], connector_type=a.get("connector_type", "rss"),
                config=a.get("config"), approve=a.get("approve", True),
            )
        if tool == "kg_ingest":
            return prov.ingest(a["name"], backfill_days=a.get("backfill_days"))
        if tool == "kg_status":
            return prov.status(a["name"])
        if tool == "kg_list":
            return prov.list_kgs(include_archived=a.get("include_archived", False))
        if tool == "kg_view":
            return prov.view(a.get("name"))
        if tool == "kg_lineage":
            return prov.lineage(a.get("name"), limit=a.get("limit", 50))
        if tool == "kg_teardown":
            return prov.teardown(a["name"], confirm=a.get("confirm", False))
        raise RuntimeError(f"no local backend for provisioning tool {tool!r}")

    def _osint(tool: str, a: Dict[str, Any]) -> Any:
        from src.osint import (
            contradiction_scan, corroborate, entity_dossier, investigation_audit,
            relationship_path, source_reliability, timeline_reconstruct, trace_artifact,
        )

        if tool == "corroborate":
            return corroborate(conn, a["claim_id"])
        if tool == "origin_signals":
            from src.osint.independence import document_signals

            return document_signals(conn, a["document_id"])
        if tool == "evidence_origin_graph":
            from src.osint.independence import origin_graph

            return origin_graph(conn, a.get("document_ids"))
        if tool == "source_reliability":
            return source_reliability(conn, a["source"])
        if tool == "contradiction_scan":
            return contradiction_scan(conn, topic=a.get("topic"), entity=a.get("entity"))
        if tool == "entity_dossier":
            return entity_dossier(conn, a["entity"], entity_type=a.get("entity_type"))
        if tool == "relationship_path":
            return relationship_path(conn, a["a"], a["b"])
        if tool == "timeline_reconstruct":
            return timeline_reconstruct(conn, topic=a.get("topic"), entity=a.get("entity"))
        if tool == "trace_artifact":
            return trace_artifact(conn, claim_id=a.get("claim_id"), document_id=a.get("document_id"))
        if tool == "investigation_audit":
            return investigation_audit(conn, a["name"])
        if tool in ("geolocate_claims", "narrative_coordination"):
            from src.osint import gated
            fn = getattr(gated, tool)
            return fn(conn, **{k: v for k, v in a.items()})
        raise RuntimeError(f"no local backend for osint tool {tool!r}")

    def caller(server: str, tool: str, arguments: Dict[str, Any]) -> Any:
        args = arguments or {}
        if server == "neuronews-provisioning":
            return _provisioning(tool, args)
        if server == "neuronews-osint":
            return _osint(tool, args)
        raise RuntimeError(f"no local backend for server {server!r}")

    return caller


__all__ = ["build_local_caller"]
