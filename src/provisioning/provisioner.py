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
        pipeline_runner: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
        contract_validator: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None,
        tenant: str = store.DEFAULT_TENANT,
    ):
        self._conn = conn
        # M4.1: the owning tenant. Every read/list/act is scoped to it, so one
        # tenant can never see or touch another tenant's namespaces.
        self._tenant = tenant or store.DEFAULT_TENANT
        self._lock = lock if lock is not None else _NullLock()
        # M4.2: quotas resolve per tenant (env overrides per tenant, counted
        # per tenant) unless an explicit Quotas is injected.
        self._quotas = quotas if quotas is not None else Quotas.for_tenant(self._tenant)
        self._clock = clock
        # P2: how a bound pipeline actually runs (injected so the server wires
        # it to the MCP pipeline server and tests inject a fake); and how a
        # pipeline config is contract-validated at attach. Both optional: with
        # no runner, ingest degrades to routing already-ingested documents, and
        # with no validator a local sanity check is used.
        self._pipeline_runner = pipeline_runner
        self._contract_validator = contract_validator
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
        backend: str = namespaces.BACKEND_TABLE_PREFIX,
    ) -> Dict[str, Any]:
        """Deploy (or converge onto) a namespaced KG. Approval-gated: without
        ``approve`` it returns a free dry-run preview and writes nothing.

        ``backend`` selects the isolation: ``table-prefix`` (default; tables in
        the shared warehouse) or ``attached`` (the KG gets its own DuckDB file)."""
        try:
            namespaces.require_valid_name(name)
        except ValueError as exc:
            return {"error": str(exc), "code": "invalid_name"}
        if backend not in (namespaces.BACKEND_TABLE_PREFIX, namespaces.BACKEND_ATTACHED):
            return {"error": f"unknown backend {backend!r}", "code": "bad_backend"}

        # Namespace names are globally unique (shared tables); a name owned by
        # another tenant is refused rather than silently co-opted.
        global_existing = store.get_kg(self._conn, name)
        if (
            global_existing is not None
            and global_existing.get("tenant", store.DEFAULT_TENANT) != self._tenant
        ):
            return {"error": f"KG name {name!r} is owned by another tenant",
                    "code": "name_taken"}
        existing = store.get_kg(self._conn, name, tenant=self._tenant)
        is_new = existing is None or existing.get("status") != store.STATUS_DEPLOYED
        # A converging re-deploy keeps the original backend.
        if existing is not None and existing.get("backend"):
            backend = existing["backend"]
        is_attached = backend == namespaces.BACKEND_ATTACHED
        db_path = (
            (existing or {}).get("db_path")
            or (namespaces.attached_db_path(name) if is_attached else None)
        )

        if not approve:
            try:
                self._quotas.check_deploy_quota(store.count_deployed(self._conn, tenant=self._tenant), is_new)
                if is_attached:
                    self._quotas.check_database_quota(self._count_databases(), is_new)
            except GuardrailError as exc:
                return {"error": exc.message, "code": exc.code}
            return {
                "preview": True,
                "kg": name,
                "would": "deploy" if is_new else "update",
                "backend": backend,
                "db_path": db_path,
                "namespace_tables": namespaces.namespace_tables(name, backend),
                "note": "approval-gated; re-run with approve=true to execute",
            }

        try:
            with self._lock:
                self._quotas.check_deploy_quota(store.count_deployed(self._conn, tenant=self._tenant), is_new)
                if is_attached:
                    self._quotas.check_database_quota(self._count_databases(), is_new)
                now = self._clock()
                namespaces.create_namespace(self._conn, name, backend, db_path)
                kg = store.upsert_kg(
                    self._conn, name, description, ontology, now,
                    backend=backend, db_path=db_path, tenant=self._tenant,
                )
                store.record_event(
                    self._conn,
                    name,
                    "deploy",
                    {"new": is_new, "backend": backend, "db_path": db_path},
                    now,
                )
        except GuardrailError as exc:
            return {"error": exc.message, "code": exc.code}
        return {"deployed": True, "created": is_new, "backend": backend, "kg": kg}

    def _count_databases(self) -> int:
        """How many deployed KGs use the attached-database backend."""
        return sum(
            1
            for kg in store.list_kgs(self._conn, include_archived=False, tenant=self._tenant)
            if kg.get("backend") == namespaces.BACKEND_ATTACHED
        )

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
        kg = store.get_kg(self._conn, name, tenant=self._tenant)
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

    # --------------------------------------------------------------- pipelines

    def attach_pipeline(
        self,
        name: str,
        connector: str,
        connector_type: str,
        config: Optional[Dict[str, Any]] = None,
        contract: Optional[str] = None,
        approve: bool = False,
    ) -> Dict[str, Any]:
        """Bind a pipeline (a connector or feed) to a KG, contract-validated at
        attach. Approval-gated (it binds a connector that will run on ingest);
        idempotent by ``(kg, connector)``."""
        kg = store.get_kg(self._conn, name, tenant=self._tenant)
        if kg is None or kg.get("status") != store.STATUS_DEPLOYED:
            return {"error": f"KG {name!r} is not deployed", "code": "not_deployed"}
        if not connector or not connector_type:
            return {"error": "connector and connector_type are required", "code": "no_input"}
        config = config or {}

        # Contract-validate the config before it can ever run.
        validation = self._validate_pipeline(connector_type, config, contract)
        if not validation.get("valid", False):
            return {
                "error": f"pipeline failed contract validation: {validation.get('reason')}",
                "code": "contract_invalid",
                "validation": validation,
            }

        already = any(
            p["connector"] == connector for p in store.list_pipelines(self._conn, name)
        )
        if not approve:
            return {
                "preview": True,
                "kg": name,
                "would": "update" if already else "attach",
                "connector": connector,
                "connector_type": connector_type,
                "validation": validation,
                "note": "approval-gated; re-run with approve=true to bind",
            }

        try:
            with self._lock:
                current = store.count_pipelines(self._conn, name)
                self._quotas.check_pipelines_quota(current, 0 if already else 1)
                now = self._clock()
                newly = store.upsert_pipeline(
                    self._conn, name, connector, connector_type, config,
                    validation.get("contract"), now,
                )
                store.record_event(
                    self._conn,
                    name,
                    "attach_pipeline",
                    {"connector": connector, "connector_type": connector_type,
                     "newly_bound": newly, "contract": validation.get("contract")},
                    now,
                )
        except GuardrailError as exc:
            return {"error": exc.message, "code": exc.code}
        return {
            "attached": newly,
            "kg": name,
            "connector": connector,
            "pipelines": store.list_pipelines(self._conn, name),
        }

    def _validate_pipeline(
        self, connector_type: str, config: Dict[str, Any], contract: Optional[str]
    ) -> Dict[str, Any]:
        """Validate a pipeline config against its ingest contract. Uses the
        injected validator (the contracts server) when present, else a local
        sanity check that the connector type and required config are present."""
        if self._contract_validator is not None:
            try:
                result = self._contract_validator(connector_type, config)
                if isinstance(result, dict):
                    result.setdefault("valid", True)
                    result.setdefault("contract", contract or result.get("contract"))
                    return result
            except Exception as exc:
                return {"valid": False, "reason": f"validator error: {exc}"}
        # Local fallback: a connector must name its type and, for a feed, a url.
        if not isinstance(config, dict):
            return {"valid": False, "reason": "config must be an object"}
        if connector_type in ("rss", "feed", "blog") and not config.get("url"):
            return {"valid": False, "reason": "a feed connector requires a 'url'"}
        default_contract = (
            "document-ingest-v1"
            if connector_type in ("document", "paper", "book", "transcript")
            else "article-ingest-v1"
        )
        return {"valid": True, "contract": contract or default_contract,
                "checked_by": "local"}

    # ----------------------------------------------------------------- ingest

    def ingest(self, name: str, backfill_days: Optional[int] = None) -> Dict[str, Any]:
        """Run the KG's bound pipelines, then route the bound sources' documents
        into the KG namespace. Rate-capped, idempotent (re-ingest converges).

        With bound pipelines and a runner (the MCP pipeline server), each
        connector runs first (connector to contract to enrich), then routing
        copies the matching documents into the namespace. With no pipeline or
        runner, it degrades to routing already-ingested documents (R8 behaviour).
        """
        kg = store.get_kg(self._conn, name, tenant=self._tenant)
        if kg is None or kg.get("status") != store.STATUS_DEPLOYED:
            return {"error": f"KG {name!r} is not deployed", "code": "not_deployed"}
        bound = [r["source"] for r in store.list_sources(self._conn, name)]
        pipelines = store.list_pipelines(self._conn, name)
        if not bound and not pipelines:
            return {"error": "no sources or pipelines bound; attach first",
                    "code": "no_sources"}

        backend = kg.get("backend") or namespaces.BACKEND_TABLE_PREFIX
        db_path = kg.get("db_path")

        try:
            with self._lock:
                now = self._clock()
                self._quotas.check_ingest_rate(
                    self._seconds_since_last_ingest(kg, now)
                )
                # Stage 1: run the bound pipelines (connector -> contract ->
                # enrich), collecting per-pipeline progress.
                pipeline_runs = self._run_pipelines(pipelines)
                # M3.2: record each connector run as its own lineage entry so the
                # audit trail names the real run (connector, source, run id, and
                # the fetched/written document counts), not just a nested blob.
                for run in pipeline_runs:
                    result = run.get("result", {}) if isinstance(run, dict) else {}
                    connector = run.get("connector")
                    store.record_event(
                        self._conn,
                        name,
                        "pipeline_run",
                        {
                            "run_id": f"{name}:{connector}:{now.isoformat()}",
                            "connector": connector,
                            "source": result.get("source"),
                            "fetched": result.get("fetched"),
                            "written": result.get("written"),
                            "ok": run.get("ok"),
                            "error": run.get("error"),
                        },
                        now,
                    )
                # Stage 2: route the matching documents into the namespace.
                routed = namespaces.route_documents(
                    self._conn, name, bound, now, backfill_days, backend, db_path
                )
                store.mark_ingested(self._conn, name, now)
                store.record_event(
                    self._conn,
                    name,
                    "ingest",
                    {"routed": routed, "sources": bound,
                     "pipeline_runs": pipeline_runs, "backfill_days": backfill_days},
                    now,
                )
        except GuardrailError as exc:
            return {"error": exc.message, "code": exc.code}
        return {
            "ingested": True,
            "kg": name,
            "backend": backend,
            "routed": routed,
            "pipeline_runs": pipeline_runs,
            "totals": namespaces.namespace_counts(self._conn, name, backend, db_path),
        }

    def _run_pipelines(self, pipelines: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Run each bound pipeline through the injected runner, returning
        per-pipeline progress. No runner (or none bound) yields an empty list,
        so ingest degrades to pure routing."""
        if not pipelines or self._pipeline_runner is None:
            return []
        runs = []
        for p in pipelines:
            try:
                result = self._pipeline_runner(
                    {"connector": p["connector"], "connector_type": p["connector_type"],
                     "config": p.get("config", {})}
                )
                runs.append({"connector": p["connector"], "ok": True,
                             "result": result if isinstance(result, dict) else {}})
            except Exception as exc:
                runs.append({"connector": p["connector"], "ok": False, "error": str(exc)})
        return runs

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
        kg = store.get_kg(self._conn, name, tenant=self._tenant)
        if kg is None:
            return {"error": f"KG {name!r} not found", "code": "not_found"}
        sources = store.list_sources(self._conn, name)
        pipelines = store.list_pipelines(self._conn, name)
        backend = kg.get("backend") or namespaces.BACKEND_TABLE_PREFIX
        counts = namespaces.namespace_counts(self._conn, name, backend, kg.get("db_path"))
        health = self._source_health([s["source"] for s in sources])
        return {
            "kg": kg,
            "backend": backend,
            "sources": sources,
            "source_count": len(sources),
            "pipelines": pipelines,
            "pipeline_count": len(pipelines),
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
        kgs = store.list_kgs(self._conn, include_archived=include_archived, tenant=self._tenant)
        out = []
        for kg in kgs:
            backend = kg.get("backend") or namespaces.BACKEND_TABLE_PREFIX
            out.append(
                {
                    **kg,
                    "source_count": store.count_sources(self._conn, kg["name"]),
                    "pipeline_count": store.count_pipelines(self._conn, kg["name"]),
                    "counts": namespaces.namespace_counts(
                        self._conn, kg["name"], backend, kg.get("db_path")
                    ),
                }
            )
        return {"kgs": out, "count": len(out)}

    def view(self, name: Optional[str] = None) -> Dict[str, Any]:
        """The scoped panel family for the provisioned KGs: each KG with its
        counts, bound sources (and why), and a sample of its routed documents,
        top entities and scoped claims. With ``name``, scopes to one KG. This
        is what the discovered ``provisioned_kg`` panel renders."""
        kgs = store.list_kgs(self._conn, include_archived=False, tenant=self._tenant)
        if name is not None:
            kgs = [k for k in kgs if k["name"] == name]
        out = []
        for kg in kgs:
            backend = kg.get("backend") or namespaces.BACKEND_TABLE_PREFIX
            sample = namespaces.namespace_sample(
                self._conn, kg["name"], backend=backend, db_path=kg.get("db_path")
            )
            out.append(
                {
                    **kg,
                    "source_count": store.count_sources(self._conn, kg["name"]),
                    "counts": namespaces.namespace_counts(
                        self._conn, kg["name"], backend, kg.get("db_path")
                    ),
                    "sources": store.list_sources(self._conn, kg["name"]),
                    "pipelines": store.list_pipelines(self._conn, kg["name"]),
                    "sample": sample,
                }
            )
        return {"kgs": out, "count": len(out)}

    def lineage(self, name: Optional[str] = None, limit: int = 50) -> Dict[str, Any]:
        """The provisioning lineage event log (all KGs, or one)."""
        return {"events": store.list_events(self._conn, name, limit=limit)}

    # --------------------------------------------------------------- teardown

    def teardown(self, name: str, confirm: bool = False) -> Dict[str, Any]:
        """Archive a KG: for ``table-prefix`` rename its tables aside, for
        ``attached`` detach its database (the file is left on disk); detach its
        sources and pipelines; mark it archived. Confirm-gated; never touches
        the shared corpus."""
        kg = store.get_kg(self._conn, name, tenant=self._tenant)
        if kg is None:
            return {"error": f"KG {name!r} not found", "code": "not_found"}
        if kg.get("status") == store.STATUS_ARCHIVED:
            return {"error": f"KG {name!r} already archived", "code": "already_archived"}
        backend = kg.get("backend") or namespaces.BACKEND_TABLE_PREFIX
        db_path = kg.get("db_path")
        try:
            require_confirm(confirm, "teardown")
        except GuardrailError as exc:
            return {
                "error": exc.message,
                "code": exc.code,
                "preview": True,
                "backend": backend,
                "would_archive": namespaces.namespace_tables(name, backend),
            }
        with self._lock:
            now = self._clock()
            counts = namespaces.namespace_counts(self._conn, name, backend, db_path)
            archived = namespaces.archive_namespace(self._conn, name, backend, db_path)
            detached = store.detach_all_sources(self._conn, name)
            pipelines_detached = store.detach_all_pipelines(self._conn, name)
            store.set_status(self._conn, name, store.STATUS_ARCHIVED, now)
            store.record_event(
                self._conn,
                name,
                "teardown",
                {
                    "backend": backend,
                    "archived_counts": counts,
                    "archived": archived,
                    "sources_detached": detached,
                    "pipelines_detached": pipelines_detached,
                },
                now,
            )
        return {
            "archived": True,
            "kg": name,
            "backend": backend,
            "archived_location": archived,
            "archived_counts": counts,
            "sources_detached": detached,
            "pipelines_detached": pipelines_detached,
        }
