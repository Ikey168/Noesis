"""Engineering Safety pack entry points (#2059, ES15): directives as of a date, investigations, recommendations,
subject dossiers, reviewable subject matches, citation links and monitors.

Acquisition runs through the shared source-pack tools (pack ``engineering-safety``). Every answer is quoted as
the authority published it and states its semantics: as published, never a verdict, risk score, ranking or
compliance determination. A subject with no records is "none on record", never "safe".
"""

from typing import Any

READ = "knowledge:engineering-safety:read"
WRITE = "knowledge:engineering-safety:write"
REVIEW = "knowledge:engineering-safety:review"
SUBSCRIPTIONS_READ = "knowledge:subscriptions:read"
SUBSCRIPTIONS_WRITE = "knowledge:subscriptions:write"
ENGINEERING_SAFETY_WRITES = {
    "propose_safety_subject_matches",
    "review_safety_subject_match",
    "revert_safety_subject_match",
    "link_safety_citations",
    "create_engineering_safety_monitor",
    "run_engineering_safety_monitor",
}
ENGINEERING_SAFETY_READS = {
    "search_directives",
    "search_defect_investigations",
    "directives_as_of",
    "inspect_investigation",
    "inspect_safety_record",
    "search_safety_recommendations",
    "engineering_safety_dossier",
    "list_safety_subject_matches",
    "poll_engineering_safety_monitor",
}
ENGINEERING_SAFETY_TOOLS = ENGINEERING_SAFETY_WRITES | ENGINEERING_SAFETY_READS
# Every scope a tool always reads or writes. Scopes only an optional argument needs (a Products, standards,
# legal or ownership namespace) are checked when that argument is used.
ENGINEERING_SAFETY_SCOPES = {
    "search_directives": [READ],
    "search_defect_investigations": [READ],
    "directives_as_of": [READ],
    "inspect_investigation": [READ],
    "inspect_safety_record": [READ],
    "search_safety_recommendations": [READ],
    "engineering_safety_dossier": [READ],
    "list_safety_subject_matches": [READ],
    "propose_safety_subject_matches": [READ, WRITE],
    "review_safety_subject_match": [READ, REVIEW],
    "revert_safety_subject_match": [READ, REVIEW],
    "link_safety_citations": [READ, WRITE],
    "create_engineering_safety_monitor": [READ, SUBSCRIPTIONS_WRITE],
    # Running reads the monitor (subscriptions:read) and records its evaluation (subscriptions:write).
    "run_engineering_safety_monitor": [READ, SUBSCRIPTIONS_READ, SUBSCRIPTIONS_WRITE],
    "poll_engineering_safety_monitor": [READ, SUBSCRIPTIONS_READ],
}
OPTIONAL_SCOPES = {
    "products_namespace": ["knowledge:products:read"],
    "standards_namespace": ["knowledge:standards:read"],
    "legal_namespace": ["knowledge:legal:read"],
    "ownership_namespace": ["knowledge:ownership:read", "knowledge:ownership:write"],
}
QUERY_EXAMPLES = {
    "directives_as_of": {
        "arguments": {
            "namespace": "global",
            "subject": {"kind": "aircraft_model", "model": "EX-100"},
            "as_of": "2026-06-01",
        },
        "semantics": "directives whose published applicability names the model and that were effective and not "
        "superseded on the date, each with the revision used and its supersession chain; unparsed applicability "
        "is 'possibly applicable - see text'; as published, not a compliance determination",
    },
    "engineering_safety_dossier": {
        "arguments": {
            "namespace": "global",
            "subject": {"kind": "vehicle", "make": "VELOMARK", "model": "CITYRUNNER"},
        },
        "semantics": "directives, investigations with verbatim findings, recommendations with dated status, "
        "defect investigations with linked recalls and complaints, each citing its record revision and the "
        "reviewable match that connected it; a subject with no records is 'none on record', never 'safe'",
    },
    "search_safety_recommendations": {
        "arguments": {
            "namespace": "global",
            "addressee": "Examplar Aircraft Company",
            "as_of": "2026-09-01",
        },
        "semantics": "recommendations addressed to the named body with the status published as of the date and "
        "the full dated response history",
    },
}


def required_scopes(tool_name, mutability):
    return ENGINEERING_SAFETY_SCOPES.get(
        tool_name, [WRITE if mutability == "write" else READ]
    )


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def checked(tool_name, operation, *, optional: dict[str, Any] | None = None):
        """Run with every scope the tool always uses present, plus those its optional arguments need."""

        def run(conn):
            from src.kb.engineering_safety_records import EngineeringSafetyError
            from src.kb.engineering_safety_store import EngineeringSafetyStore

            scopes = who()[1]
            needed = list(ENGINEERING_SAFETY_SCOPES[tool_name])
            needed += [
                s
                for arg, value in (optional or {}).items()
                if value
                for s in OPTIONAL_SCOPES[arg]
            ]
            missing = [s for s in needed if s not in scopes]
            if missing and "operator" not in scopes:
                raise EngineeringSafetyError(
                    "unauthorized", f"{', '.join(missing)} required"
                )
            # Readiness before any write can create empty tables: nothing is answered before a source ran.
            EngineeringSafetyStore(conn, initialize=False).require_ready()
            return operation(conn)

        return run

    def queries(conn):
        from src.kb.engineering_safety_queries import EngineeringSafetyQueries

        return EngineeringSafetyQueries(conn)

    @mcp.tool()
    def search_directives(
        namespace: str,
        text: str | None = None,
        authority: str | None = None,
        native_id: str | None = None,
        as_of: str | None = None,
        acquired_by_ms: int | None = None,
        limit: int = 50,
    ) -> dict:
        """Airworthiness directives by AD number, issuing authority (us-faa, eu-easa) or a string their title,
        applicability or subjects state (unmatched subjects included), each with the revision current as of the
        date. As published; no compliance determination."""
        return safe(
            checked(
                "search_directives",
                lambda conn: queries(conn).search(
                    namespace,
                    scopes=who()[1],
                    kinds=["directive"],
                    text=text,
                    authority=authority,
                    native_id=native_id,
                    as_of=as_of,
                    acquired_by_ms=acquired_by_ms,
                    limit=limit,
                ),
            ),
            required_scope=READ,
        )

    @mcp.tool()
    def search_defect_investigations(
        namespace: str,
        text: str | None = None,
        native_id: str | None = None,
        include_complaints: bool = False,
        as_of: str | None = None,
        acquired_by_ms: int | None = None,
        limit: int = 50,
    ) -> dict:
        """NHTSA ODI defect investigations (PE, EA, DP, RQ) by action number or a string they state (make, model,
        component, manufacturer), with the revision current as of the date; include_complaints adds consumer
        complaints, which are unverified reports, never confirmed defects. No defect likelihood or rating."""
        kinds = (
            ["defect_investigation", "complaint"]
            if include_complaints
            else ["defect_investigation"]
        )
        return safe(
            checked(
                "search_defect_investigations",
                lambda conn: queries(conn).search(
                    namespace,
                    scopes=who()[1],
                    kinds=kinds,
                    text=text,
                    native_id=native_id,
                    as_of=as_of,
                    acquired_by_ms=acquired_by_ms,
                    limit=limit,
                ),
            ),
            required_scope=READ,
        )

    @mcp.tool()
    def directives_as_of(
        namespace: str,
        subject: dict,
        as_of: str,
        serial: str | None = None,
        acquired_by_ms: int | None = None,
        export_bundle: bool = False,
    ) -> dict:
        """Directives whose published applicability names a subject (aircraft_model/engine_model model, vehicle,
        component, or a product_model_id/entity_id through accepted matches) and that were in effect on the date,
        each with the revision used, supersession chain and applicability text with locator. Superseded ones
        appear only in chains; unparsed applicability is 'possibly applicable - see text'. As published; not a
        compliance determination. export_bundle returns a noesis-evidence-bundle-v1."""

        def run(conn):
            from src.kb.engineering_safety_queries import export_bundle as bundle

            answer = queries(conn).directives_as_of(
                namespace,
                subject,
                scopes=who()[1],
                as_of=as_of,
                acquired_by_ms=acquired_by_ms,
                serial=serial,
            )
            return (
                {"answer": answer, "bundle": bundle(conn, answer)}
                if export_bundle
                else answer
            )

        return safe(checked("directives_as_of", run), required_scope=READ)

    def _inspect(tool_name, namespace, reference, as_of, acquired_by_ms, kind):
        def run(conn):
            from src.kb.engineering_safety_store import inspect_record

            return inspect_record(
                conn,
                namespace,
                reference,
                scopes=who()[1],
                as_of=as_of,
                acquired_by_ms=acquired_by_ms,
                kind=kind,
            )

        return safe(checked(tool_name, run), required_scope=READ)

    @mcp.tool()
    def inspect_investigation(
        namespace: str,
        investigation: str,
        as_of: str | None = None,
        acquired_by_ms: int | None = None,
    ) -> dict:
        """One investigation (record id or provider:number, e.g. ntsb:ERA26FA101) with the report revision current
        as of the date, its occurrence, verbatim findings and probable cause with locators, and every other report
        revision (preliminary and final side by side). No cause is inferred or aggregated."""
        return _inspect(
            "inspect_investigation",
            namespace,
            investigation,
            as_of,
            acquired_by_ms,
            "investigation",
        )

    @mcp.tool()
    def inspect_safety_record(
        namespace: str,
        record: str,
        as_of: str | None = None,
        acquired_by_ms: int | None = None,
    ) -> dict:
        """Any engineering-safety record (directive, recommendation, occurrence, defect investigation, complaint)
        with its revision as of the date, parts, citations and revision history."""
        return _inspect(
            "inspect_safety_record", namespace, record, as_of, acquired_by_ms, None
        )

    @mcp.tool()
    def search_safety_recommendations(
        namespace: str,
        addressee: str | None = None,
        subject: dict | None = None,
        authority: str | None = None,
        status: str | None = None,
        native_id: str | None = None,
        as_of: str | None = None,
        acquired_by_ms: int | None = None,
    ) -> dict:
        """Safety recommendations by addressee, subject, issuing body or status as of a date, with the status
        published on that date and the full dated response history."""
        return safe(
            checked(
                "search_safety_recommendations",
                lambda conn: queries(conn).recommendations(
                    namespace,
                    scopes=who()[1],
                    addressee=addressee,
                    subject=subject,
                    authority=authority,
                    status=status,
                    native_id=native_id,
                    as_of=as_of,
                    acquired_by_ms=acquired_by_ms,
                ),
            ),
            required_scope=READ,
        )

    @mcp.tool()
    def engineering_safety_dossier(
        namespace: str,
        subject: dict,
        as_of: str | None = None,
        serial: str | None = None,
        acquired_by_ms: int | None = None,
        products_namespace: str | None = None,
        export_bundle: bool = False,
    ) -> dict:
        """A cited engineering-safety dossier for a subject: directives in effect with chains, investigations with
        verbatim findings, recommendations with status history, defect investigations with linked recalls
        (products_namespace, needs knowledge:products:read), occurrences and complaint counts, with the matches
        that connected them, conflicts side by side and unknowns. A subject with no records is 'none on record',
        never 'safe'."""

        def run(conn):
            from src.kb.engineering_safety_queries import export_bundle as bundle

            dossier = queries(conn).dossier(
                namespace,
                subject,
                scopes=who()[1],
                as_of=as_of,
                acquired_by_ms=acquired_by_ms,
                serial=serial,
                products_namespace=products_namespace,
            )
            return (
                {"dossier": dossier, "bundle": bundle(conn, dossier)}
                if export_bundle
                else dossier
            )

        return safe(
            checked(
                "engineering_safety_dossier",
                run,
                optional={"products_namespace": products_namespace},
            ),
            required_scope=READ,
        )

    @mcp.tool()
    def list_safety_subject_matches(
        namespace: str, subject_key: str | None = None, target_id: str | None = None
    ) -> dict:
        """Reviewable subject matches (proposed, accepted, rejected, reverted, detached) with their evidence."""

        def run(conn):
            from src.kb.engineering_safety_identity import SubjectIdentity

            return {
                "candidates": SubjectIdentity(conn, initialize=False).candidates(
                    namespace,
                    scopes=who()[1],
                    subject_key=subject_key,
                    target_id=target_id,
                )
            }

        return safe(checked("list_safety_subject_matches", run), required_scope=READ)

    @mcp.tool()
    def propose_safety_subject_matches(
        namespace: str,
        products_namespace: str | None = None,
        ownership_namespace: str | None = None,
    ) -> dict:
        """Propose candidates from published subjects to Products models (products_namespace, needs
        knowledge:products:read) and canonical entities, and offer operator names to Corporate Ownership
        (ownership_namespace, needs knowledge:ownership:read and :write). Deterministic and idempotent; nothing is
        accepted or merged; no sister-type or similar-model inference."""

        def run(conn):
            from src.kb.engineering_safety_identity import SubjectIdentity

            return SubjectIdentity(conn).propose(
                namespace,
                scopes=who()[1],
                principal_id=who()[0],
                products_namespace=products_namespace,
                ownership_namespace=ownership_namespace,
            )

        return safe(
            checked(
                "propose_safety_subject_matches",
                run,
                optional={
                    "products_namespace": products_namespace,
                    "ownership_namespace": ownership_namespace,
                },
            ),
            write=True,
            required_scope=WRITE,
        )

    @mcp.tool()
    def review_safety_subject_match(
        namespace: str, match_id: str, decision: str, reason: str
    ) -> dict:
        """Accept or reject the current proposal of a subject match (appended; an entity match records an identity
        decision). Records are never merged."""

        def run(conn):
            from src.kb.engineering_safety_identity import SubjectIdentity

            return SubjectIdentity(conn).review(
                namespace,
                match_id,
                decision,
                reason,
                scopes=who()[1],
                principal_id=who()[0],
            )

        return safe(
            checked("review_safety_subject_match", run),
            write=True,
            required_scope=REVIEW,
        )

    @mcp.tool()
    def revert_safety_subject_match(namespace: str, match_id: str, reason: str) -> dict:
        """Revert the decision on a subject match; an earlier acceptance is never reactivated."""

        def run(conn):
            from src.kb.engineering_safety_identity import SubjectIdentity

            return SubjectIdentity(conn).revert(
                namespace, match_id, reason, scopes=who()[1], principal_id=who()[0]
            )

        return safe(
            checked("revert_safety_subject_match", run),
            write=True,
            required_scope=REVIEW,
        )

    @mcp.tool()
    def link_safety_citations(
        namespace: str,
        products_namespace: str | None = None,
        standards_namespace: str | None = None,
        legal_namespace: str | None = None,
    ) -> dict:
        """Link cited AD and recommendation numbers to records here, and (when their namespaces are named) recall
        campaigns to Products safety notices, standards to catalogue editions and CFR/EU acts to Legal works, all
        by exact identifier and revision-aware. Unresolved citations keep their text. Idempotent."""

        def run(conn):
            from src.kb.engineering_safety_citations import link_citations

            return link_citations(
                conn,
                namespace,
                scopes=who()[1],
                principal_id=who()[0],
                products_namespace=products_namespace,
                standards_namespace=standards_namespace,
                legal_namespace=legal_namespace,
            )

        return safe(
            checked(
                "link_safety_citations",
                run,
                optional={
                    "products_namespace": products_namespace,
                    "standards_namespace": standards_namespace,
                    "legal_namespace": legal_namespace,
                },
            ),
            write=True,
            required_scope=WRITE,
        )

    @mcp.tool()
    def create_engineering_safety_monitor(
        namespace: str, request_key: str, watch: dict, delivery: dict | None = None
    ) -> dict:
        """A subscription watching subjects, issuing authorities, directive numbers or recommendation numbers
        (provider:number); refreshed by the engineering-safety source pack's schedule, no new scheduler."""

        def run(conn):
            from src.kb.engineering_safety_monitoring import EngineeringSafetyMonitor

            return EngineeringSafetyMonitor(conn).create(
                namespace,
                request_key,
                watch=watch,
                principal_id=who()[0],
                scopes=who()[1],
                delivery=delivery,
            )

        return safe(
            checked("create_engineering_safety_monitor", run),
            write=True,
            required_scope=SUBSCRIPTIONS_WRITE,
        )

    @mcp.tool()
    def run_engineering_safety_monitor(
        subscription_id: str, watermark: int | None = None
    ) -> dict:
        """Evaluate a monitor at a complete source-pack run: new directives, revisions and supersessions, final
        reports, new findings, recommendation status changes and defect-investigation upgrades, each citing the
        record revision and the previous one. The first evaluation is a baseline."""

        def run(conn):
            from src.kb.engineering_safety_monitoring import EngineeringSafetyMonitor

            return EngineeringSafetyMonitor(conn).run(
                subscription_id, watermark, principal_id=who()[0], scopes=who()[1]
            )

        return safe(
            checked("run_engineering_safety_monitor", run),
            write=True,
            required_scope=SUBSCRIPTIONS_WRITE,
        )

    @mcp.tool()
    def poll_engineering_safety_monitor(subscription_id: str, cursor: str = "") -> dict:
        """Poll an engineering-safety monitor's events through the subscription delivery path."""

        def run(conn):
            from src.kb.engineering_safety_monitoring import EngineeringSafetyMonitor

            return EngineeringSafetyMonitor(conn, initialize=False).poll(
                subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor
            )

        return safe(
            checked("poll_engineering_safety_monitor", run),
            required_scope=SUBSCRIPTIONS_READ,
        )
