"""
Provisioning lifecycle orchestration (R8 #607): the object the MCP tools call.

One :class:`Provisioner` wraps an injected read-write DuckDB connection and a
serialising lock (the API process owns the single warehouse writer). It ties
together the namespace DDL/routing (:mod:`~src.provisioning.namespaces`), the
registry and lineage log (:mod:`~src.provisioning.store`) and the guardrails
(:mod:`~src.provisioning.guardrails`) into the five verbs:

    deploy(name, description, ontology?, approve)   -> namespaced KG + lineage
    attach_sources(name, sources? | criteria?)      -> bind feeds (quality-driven
                                                       criteria resolved via
                                                       outlet_scores)
    ingest(name, backfill_days?)                     -> route matching documents
    status(name) / list_kgs()                        -> counts, source health, lag
    teardown(name, confirm)                          -> archive + detach

Every write registers a lineage event, so each step is visible in lineage. All
writes hold the lock for their duration; reads (status/list/preview) are free.

Stdlib-only; the caller injects the connection, the lock, and the clock.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence

from src.provisioning import namespaces, store
from src.provisioning.guardrails import (
    GuardrailError,
    Quotas,
    require_approval,
    require_confirm,
)


class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Provisioner:
    def __init__(
        self,
        conn,
        lock: Any = None,
        quotas: Optional[Quotas] = None,
        clock: Callable[[], datetime] = _utcnow,
        ensure: bool = True,
    ):
        self._conn = conn
        self._lock = lock if lock is not None else _NullLock()
        self._quotas = quotas if quotas is not None else Quotas.from_env()
        self._clock = clock
        # Read-only callers pass ensure=False: the schema is created lazily on
        # the first write path, so a read tool never needs DDL rights.
        if ensure:
            with self._lock:
                store.ensure_schema(conn)

    # ------------------------------------------------------------------ deploy

    def deploy(
        self,
        name: str,
        description: str = "",
        ontology: Any = None,
        approve: bool = False,
    ) -> Dict[str, Any]:
        """Deploy (or converge onto) a namespaced KG. Approval-gated: without
        ``approve`` it returns a free dry-run preview and writes nothing."""
        try:
            namespaces.require_valid_name(name)
        except ValueError as exc:
            return {"error": str(exc), "code": "invalid_name"}

        existing = store.get_kg(self._conn, name)
        is_new = existing is None or existing.get("status") != store.STATUS_DEPLOYED

        if not approve:
            try:
                self._quotas.check_deploy_quota(store.count_deployed(self._conn), is_new)
            except GuardrailError as exc:
                return {"error": exc.message, "code": exc.code}
            return {
                "preview": True,
                "kg": name,
                "would": "deploy" if is_new else "update",
                "namespace_tables": namespaces.namespace_tables(name),
                "note": "approval-gated; re-run with approve=true to execute",
            }

        try:
            with self._lock:
                self._quotas.check_deploy_quota(store.count_deployed(self._conn), is_new)
                now = self._clock()
                namespaces.create_namespace(self._conn, name)
                kg = store.upsert_kg(self._conn, name, description, ontology, now)
                store.record_event(
                    self._conn,
                    name,
                    "deploy",
                    {"new": is_new, "namespace": namespaces.namespace_prefix(name)},
                    now,
                )
        except GuardrailError as exc:
            return {"error": exc.message, "code": exc.code}
        return {"deployed": True, "created": is_new, "kg": kg}

    # ----------------------------------------------------------------- attach

    def attach_sources(
        self,
        name: str,
        sources: Optional[Sequence[str]] = None,
        criteria: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Bind sources to a KG, explicitly or by a quality criterion resolved
        against the outlet transparency scores. Idempotent by ``(kg, source)``.
        """
        kg = store.get_kg(self._conn, name)
        if kg is None or kg.get("status") != store.STATUS_DEPLOYED:
            return {"error": f"KG {name!r} is not deployed", "code": "not_deployed"}

        resolved: List[Dict[str, Any]]
        if criteria:
            resolved = self._resolve_criteria(criteria)
            if not resolved:
                return {
                    "attached": 0,
                    "kg": name,
                    "note": "no sources matched the criteria",
                    "criteria": criteria,
                }
        elif sources:
            resolved = [
                {"source": s, "source_type": None, "reason": "explicitly listed"}
                for s in sources
            ]
        else:
            return {"error": "provide sources or criteria", "code": "no_input"}

        try:
            with self._lock:
                current = store.count_sources(self._conn, name)
                # Count only sources not already bound toward the quota.
                already = {r["source"] for r in store.list_sources(self._conn, name)}
                adding = sum(1 for r in resolved if r["source"] not in already)
                self._quotas.check_sources_quota(current, adding)
                now = self._clock()
                newly = 0
                for r in resolved:
                    if store.upsert_source(
                        self._conn,
                        name,
                        r["source"],
                        r.get("source_type"),
                        r.get("reason", ""),
                        now,
                    ):
                        newly += 1
                store.record_event(
                    self._conn,
                    name,
                    "attach",
                    {
                        "requested": len(resolved),
                        "newly_bound": newly,
                        "criteria": criteria,
                        "sources": [r["source"] for r in resolved],
                    },
                    now,
                )
        except GuardrailError as exc:
            return {"error": exc.message, "code": exc.code}
        return {
            "attached": newly,
            "already_bound": len(resolved) - newly,
            "kg": name,
            "sources": store.list_sources(self._conn, name),
        }

    def _resolve_criteria(self, criteria: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Resolve a quality criterion into a source list from ``outlet_scores``
        (the transparency ranking). Supported keys: ``min_transparency`` (on
        composite_score), ``min_attribution`` (attribution_rate), ``type``
        (source_type)."""
        if not namespaces._table_exists(self._conn, "outlet_scores"):
            return []
        where = ["1 = 1"]
        params: List[Any] = []
        if criteria.get("type"):
            where.append("source_type = ?")
            params.append(criteria["type"])
        min_transparency = criteria.get("min_transparency")
        if min_transparency is not None:
            where.append("composite_score >= ?")
            params.append(float(min_transparency))
        min_attribution = criteria.get("min_attribution")
        if min_attribution is not None:
            where.append("attribution_rate >= ?")
            params.append(float(min_attribution))
        # Latest score per (source, source_type).
        rows = self._conn.execute(
            "SELECT source, source_type, composite_score, attribution_rate "
            "FROM outlet_scores o WHERE "
            + " AND ".join(where)
            + " AND score_date = (SELECT MAX(score_date) FROM outlet_scores i "
            "WHERE i.source = o.source AND i.source_type = o.source_type) "
            "ORDER BY composite_score DESC NULLS LAST",
            params,
        ).fetchall()
        out = []
        for r in rows:
            bits = []
            if min_transparency is not None:
                bits.append(f"transparency {float(r[2] or 0):.2f} >= {float(min_transparency):.2f}")
            if min_attribution is not None:
                bits.append(f"attribution {float(r[3] or 0):.2f} >= {float(min_attribution):.2f}")
            if criteria.get("type"):
                bits.append(f"type={criteria['type']}")
            out.append(
                {
                    "source": r[0],
                    "source_type": r[1],
                    "reason": "selected because " + ", ".join(bits)
                    if bits
                    else "matched criteria",
                }
            )
        return out

    # ----------------------------------------------------------------- ingest

    def ingest(self, name: str, backfill_days: Optional[int] = None) -> Dict[str, Any]:
        """Route the bound sources' documents (and claims / derived entities)
        into the KG namespace. Rate-capped, idempotent (re-ingest converges)."""
        kg = store.get_kg(self._conn, name)
        if kg is None or kg.get("status") != store.STATUS_DEPLOYED:
            return {"error": f"KG {name!r} is not deployed", "code": "not_deployed"}
        bound = [r["source"] for r in store.list_sources(self._conn, name)]
        if not bound:
            return {"error": "no sources bound; attach first", "code": "no_sources"}

        try:
            with self._lock:
                now = self._clock()
                self._quotas.check_ingest_rate(
                    self._seconds_since_last_ingest(kg, now)
                )
                routed = namespaces.route_documents(
                    self._conn, name, bound, now, backfill_days
                )
                store.mark_ingested(self._conn, name, now)
                store.record_event(
                    self._conn,
                    name,
                    "ingest",
                    {"routed": routed, "sources": bound, "backfill_days": backfill_days},
                    now,
                )
        except GuardrailError as exc:
            return {"error": exc.message, "code": exc.code}
        return {"ingested": True, "kg": name, "routed": routed,
                "totals": namespaces.namespace_counts(self._conn, name)}

    @staticmethod
    def _seconds_since_last_ingest(kg: Dict[str, Any], now: datetime) -> Optional[float]:
        last = kg.get("last_ingest_at")
        if not last:
            return None
        try:
            parsed = datetime.fromisoformat(str(last))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return (now - parsed).total_seconds()
        except Exception:
            return None

    # -------------------------------------------------------- status / list

    def status(self, name: str) -> Dict[str, Any]:
        """Entity/document/claim counts, bound-source health and ingest lag for
        a KG. Read-only and free (no approval needed)."""
        kg = store.get_kg(self._conn, name)
        if kg is None:
            return {"error": f"KG {name!r} not found", "code": "not_found"}
        sources = store.list_sources(self._conn, name)
        counts = namespaces.namespace_counts(self._conn, name)
        health = self._source_health([s["source"] for s in sources])
        return {
            "kg": kg,
            "sources": sources,
            "source_count": len(sources),
            "counts": counts,
            "source_health": health,
            "lineage": store.list_events(self._conn, name, limit=20),
        }

    def _source_health(self, sources: Sequence[str]) -> List[Dict[str, Any]]:
        if not sources or not namespaces._table_exists(self._conn, "news_articles"):
            return [{"source": s, "documents": 0, "last_seen": None} for s in sources]
        ph = ", ".join("?" for _ in sources)
        has_date = namespaces._has_column(self._conn, "news_articles", "publish_date")
        date_expr = "MAX(publish_date)" if has_date else "NULL"
        rows = self._conn.execute(
            f"SELECT source, COUNT(*), {date_expr} FROM news_articles "
            f"WHERE source IN ({ph}) GROUP BY source",
            list(sources),
        ).fetchall()
        seen = {r[0]: (int(r[1]), str(r[2]) if r[2] is not None else None) for r in rows}
        return [
            {
                "source": s,
                "documents": seen.get(s, (0, None))[0],
                "last_seen": seen.get(s, (0, None))[1],
            }
            for s in sources
        ]

    def list_kgs(self, include_archived: bool = False) -> Dict[str, Any]:
        """List KGs with their namespace counts."""
        kgs = store.list_kgs(self._conn, include_archived=include_archived)
        out = []
        for kg in kgs:
            out.append(
                {
                    **kg,
                    "source_count": store.count_sources(self._conn, kg["name"]),
                    "counts": namespaces.namespace_counts(self._conn, kg["name"]),
                }
            )
        return {"kgs": out, "count": len(out)}

    def lineage(self, name: Optional[str] = None, limit: int = 50) -> Dict[str, Any]:
        """The provisioning lineage event log (all KGs, or one)."""
        return {"events": store.list_events(self._conn, name, limit=limit)}

    # --------------------------------------------------------------- teardown

    def teardown(self, name: str, confirm: bool = False) -> Dict[str, Any]:
        """Archive a KG: rename its namespace tables aside (never delete),
        detach its sources, mark it archived. Confirm-gated; never touches the
        shared corpus."""
        kg = store.get_kg(self._conn, name)
        if kg is None:
            return {"error": f"KG {name!r} not found", "code": "not_found"}
        if kg.get("status") == store.STATUS_ARCHIVED:
            return {"error": f"KG {name!r} already archived", "code": "already_archived"}
        try:
            require_confirm(confirm, "teardown")
        except GuardrailError as exc:
            return {
                "error": exc.message,
                "code": exc.code,
                "preview": True,
                "would_archive": namespaces.namespace_tables(name),
            }
        with self._lock:
            now = self._clock()
            counts = namespaces.namespace_counts(self._conn, name)
            archived_tables = namespaces.archive_namespace(self._conn, name)
            detached = store.detach_all_sources(self._conn, name)
            store.set_status(self._conn, name, store.STATUS_ARCHIVED, now)
            store.record_event(
                self._conn,
                name,
                "teardown",
                {
                    "archived_counts": counts,
                    "archived_tables": archived_tables,
                    "sources_detached": detached,
                },
                now,
            )
        return {
            "archived": True,
            "kg": name,
            "archived_tables": archived_tables,
            "archived_counts": counts,
            "sources_detached": detached,
        }
