"""Legal treaties entry points (#2581): treaties, participants and treaty actions as depositaries published them.

Registered through :mod:`tools.knowledge_engine_mcp.legal` (the Legal pack's
tools); acquisition runs through the shared source-pack tools (pack
``legal-research`` 1.5.0: ``untc-treaty-status``, ``eu-cellar-agreements``,
``coe-treaty-office``). UN Treaty Collection, EU agreement and Council of
Europe coverage are the optional ``treaties-untc``, ``treaties-eu`` and
``treaties-coe`` features of the ``legal.treaties`` provider.

Exclusions: no legal advice, no inference of obligations or compliance, no
interpretation of the legal effect of reservations, and no treaty-text
redistribution beyond what each source licenses (texts are linked). Outputs
follow the TR01 minimisation decision: no natural person is a field, a key or
a match, and contact details in declarations stay withheld.
"""

TREATIES_WRITES = {
    "propose_treaty_matches",
    "review_treaty_match",
    "revert_treaty_match",
    "link_treaty_records",
    "create_treaties_monitor",
    "run_treaties_monitor",
}
TREATIES_READS = {
    "treaties_source_contracts",
    "treaties_readiness",
    "lookup_treaty",
    "treaty_record_history",
    "treaty_status_as_of",
    "participant_treaty_actions",
    "treaty_reservations_and_objections",
    "export_treaty_evidence_bundle",
    "list_treaty_identity_candidates",
    "list_treaty_links",
    "poll_treaties_monitor",
}
TREATIES_TOOLS = TREATIES_WRITES | TREATIES_READS
READ = "knowledge:legal:read"
WRITE = "knowledge:legal:write"
OWNERSHIP_READ = "knowledge:ownership:read"
# Every scope each tool always reads or writes.
TREATIES_SCOPES = {
    "treaties_source_contracts": [],
    "treaties_readiness": [READ],
    "lookup_treaty": [READ],
    "treaty_record_history": [READ],
    "treaty_status_as_of": [READ, OWNERSHIP_READ],
    "participant_treaty_actions": [READ, OWNERSHIP_READ],
    "treaty_reservations_and_objections": [READ],
    "export_treaty_evidence_bundle": [READ, OWNERSHIP_READ],
    "list_treaty_identity_candidates": [READ, OWNERSHIP_READ],
    "list_treaty_links": [READ],
    "poll_treaties_monitor": [READ, "knowledge:subscriptions:read"],
    "propose_treaty_matches": [READ, OWNERSHIP_READ, "knowledge:ownership:write", "knowledge:geospatial:read"],
    "review_treaty_match": [OWNERSHIP_READ, "knowledge:ownership:review"],
    "revert_treaty_match": [OWNERSHIP_READ, "knowledge:ownership:review"],
    "link_treaty_records": [READ, WRITE, OWNERSHIP_READ],
    "create_treaties_monitor": [READ, "knowledge:subscriptions:write"],
    "run_treaties_monitor": [READ, "knowledge:subscriptions:write"],
}
EXCLUSIONS_NOTE = ("No legal advice, no inference of obligations or compliance, no interpretation of the legal "
                   "effect of reservations; treaty texts are linked, never mirrored (TR01 minimisation applies).")


def required_scopes(tool_name, mutability):
    return TREATIES_SCOPES.get(tool_name, [WRITE if mutability == "write" else READ])


def guard(answer):
    """Refuse an answer that would carry an advice/obligation/compliance key or a personal field (TR01)."""
    from src.ingestion.treaties_sources import PERSONAL_FIELD_KEYS
    from src.kb.treaties_records import TreatiesError, forbidden_keys

    found = forbidden_keys(answer)

    def personal(value, path="$"):
        out = []
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).casefold() in PERSONAL_FIELD_KEYS:
                    out.append(f"{path}.{key}")
                out += personal(item, f"{path}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                out += personal(item, f"{path}[{index}]")
        return out

    found += personal(answer)
    if found:
        raise TreatiesError("exclusion_violation", "an answer may not carry advice, obligation, compliance or "
                                                   "personal fields", paths=found)
    return answer


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def queries(conn):
        from src.kb.treaties_queries import TreatiesQueries

        return TreatiesQueries(conn)

    def store(conn, write=False):
        from src.kb.treaties_records import TreatiesStore

        return TreatiesStore(conn, initialize=write)

    def identity(conn, write=False):
        from src.kb.treaties_identity import TreatiesIdentity

        return TreatiesIdentity(conn, initialize=write)

    def monitor(conn, write=False):
        from src.kb.treaties_monitoring import TreatiesMonitor

        return TreatiesMonitor(conn, initialize=write)

    @mcp.tool()
    def treaties_source_contracts() -> dict:
        """Per-source endpoints, authentication, licences and redistribution terms, rate limits, revision models,
        the TR01 minimisation decision, bounded coverage and LIVE_VERIFICATION for the UN Treaty Collection, CELLAR
        (EU international agreements) and the Council of Europe Treaty Office."""
        from src.ingestion.treaties_sources import (
            BOUNDED_COVERAGE,
            LIVE_VERIFICATION,
            MINIMISATION,
            PROVIDER_CONTRACTS,
            REVIEW_BOUNDARY,
        )

        return {"contracts": PROVIDER_CONTRACTS, "live_verification": LIVE_VERIFICATION,
                "minimisation": MINIMISATION, "bounded_coverage": BOUNDED_COVERAGE, "review_boundary": REVIEW_BOUNDARY}

    @mcp.tool()
    def treaties_readiness() -> dict:
        """Whether the treaties-untc / treaties-eu / treaties-coe features are selected and records per source."""
        from src.kb.treaties_records import readiness

        return safe(lambda conn: readiness(conn), required_scope=READ)

    @mcp.tool()
    def lookup_treaty(namespace: str, treaty: str) -> dict:
        """A treaty by key, UNTC mtdsg_no (e.g. XXVII-7), CELEX or CETS number: identifiers, title as published,
        adoption, entry-into-force conditions and date, language expressions (CELLAR) and text links, each record
        citing its source, record revision, depositary revision and as-of time. 'treaty_not_on_record' is
        explicit. No legal advice or legal-effect reading."""
        def run(conn):
            ask = queries(conn)
            keys = ask.resolve_treaty(namespace, treaty, scopes=who()[1])
            if not keys:
                return {"status": "treaty_not_on_record", "treaty": treaty}
            rows = [r for key in keys for r in store(conn).records(namespace, scopes=who()[1], treaty_key=key,
                                                                    kinds=["treaty", "treaty-expression"])]
            return guard({"status": "answered", "treaty": treaty, "treaty_keys": keys,
                          "records": [{"record_key": r["record_key"], "kind": r["record_kind"],
                                       "fields": r["record"]["fields"], "citation": r["citation"]} for r in rows],
                          "exclusions": EXCLUSIONS_NOTE})

        return safe(run, required_scope=READ)

    @mcp.tool()
    def treaty_record_history(namespace: str, record_key: str) -> dict:
        """Every revision of one treaty, action or statement record in arrival order: depositary corrections,
        older observations and removals by the source (never deletions), each citing its revision."""
        return safe(lambda conn: guard({"record_key": record_key, "revisions": [
            {"revision_id": r["revision_id"], "revision_no": r["revision_no"], "change": r["change"],
             "publication_status": r["publication_status"], "depositary_revision": r["native_revision"],
             "previous_revision_id": r["previous_revision_id"], "fields": r["record"]["fields"],
             "citation": r["citation"]} for r in store(conn).history(namespace, record_key, scopes=who()[1])]}),
            required_scope=READ)

    @mcp.tool()
    def treaty_status_as_of(namespace: str, treaty: str, participant: str, as_of: str,
                            depositary_as_of: str | None = None) -> dict:
        """A treaty's action chain for one participant (key, exact published name, or geospatial place through
        accepted identity matches) at a date: signature, consent to be bound, entry into force, denunciation or
        withdrawal with linked reservations, declarations, objections and notes verbatim, selected by the dates
        as published. Pending and unclear statuses are returned as such with the source text; answers cite the
        depositary revision (optionally an earlier one). No legal advice or obligation/compliance inference."""
        return safe(lambda conn: guard(queries(conn).status_as_of(namespace, treaty, participant, as_of,
                                                                  scopes=who()[1],
                                                                  depositary_as_of=depositary_as_of)),
                    required_scope=READ)

    @mcp.tool()
    def participant_treaty_actions(namespace: str, participant: str, period_start: str | None = None,
                                   period_end: str | None = None, action_types: list[str] | None = None,
                                   providers: list[str] | None = None, include_statements: bool = True) -> dict:
        """A participant's treaty actions over a period across treaties, filtered by action type and source
        (untc, eu-cellar, coe-treaty-office), with its statements verbatim; undated actions listed separately.
        Each item cites its record revision. No compliance or obligation reading."""
        return safe(lambda conn: guard(queries(conn).participant_actions(
            namespace, participant, scopes=who()[1], period_start=period_start, period_end=period_end,
            action_types=action_types, providers=providers, include_statements=include_statements)),
            required_scope=READ)

    @mcp.tool()
    def treaty_reservations_and_objections(namespace: str, treaty: str, kinds: list[str] | None = None,
                                           participant: str | None = None, providers: list[str] | None = None,
                                           period_start: str | None = None, period_end: str | None = None) -> dict:
        """A treaty's reservations, declarations and objections verbatim, each objection linked to the reservation
        it objects to where the source links it, each citing its record revision. The legal effect of a
        reservation or objection is never interpreted."""
        return safe(lambda conn: guard(queries(conn).treaty_statements(
            namespace, treaty, scopes=who()[1], kinds=kinds, participant=participant, providers=providers,
            period_start=period_start, period_end=period_end)), required_scope=READ)

    @mcp.tool()
    def export_treaty_evidence_bundle(namespace: str, query: str, treaty: str | None = None,
                                      participant: str | None = None, as_of: str | None = None) -> dict:
        """An evidence bundle for a status, actions or statements answer (query: status | actions | statements):
        assertions each citing source, record revision, depositary revision and as-of time."""
        def run(conn):
            ask = queries(conn)
            if query == "status" and treaty and participant and as_of:
                answer = ask.status_as_of(namespace, treaty, participant, as_of, scopes=who()[1])
            elif query == "actions" and participant:
                answer = ask.participant_actions(namespace, participant, scopes=who()[1])
            elif query == "statements" and treaty:
                answer = ask.treaty_statements(namespace, treaty, scopes=who()[1], participant=participant)
            else:
                return {"ok": False, "error": {"code": "invalid_request", "message": "query is status (treaty, "
                                               "participant, as_of), actions (participant) or statements (treaty)"}}
            return guard({"status": answer["status"], "query": query, "as_of": as_of,
                          "evidence_bundle": ask.evidence_bundle(answer), "exclusions": answer["exclusions"]})

        return safe(run, required_scope=READ)

    @mcp.tool()
    def list_treaty_identity_candidates(namespace: str, record_key: str | None = None) -> dict:
        """Identity candidates and decisions for participants (against places and each other) and treaties across
        sources, with method, evidence and confidence; unmatched records are listed as unmatched."""
        def run(conn):
            from src.kb.treaties_records import authorize

            authorize(namespace, who()[1], READ)
            ident = identity(conn)
            return {"candidates": ident.candidates(namespace, scopes=who()[1], record_key=record_key),
                    "unmatched": ident.unmatched(namespace, scopes=who()[1])}

        return safe(run, required_scope=READ)

    @mcp.tool()
    def propose_treaty_matches(namespace: str, geo_namespace: str | None = None) -> dict:
        """Propose participant matches to geospatial places (published ISO 3166 codes first; names only where a
        source publishes no code) and treaty matches across sources through published cross-references. Nothing
        is merged or accepted automatically."""
        return safe(lambda conn: identity(conn, True).propose(namespace, principal_id=who()[0], scopes=who()[1],
                                                              geo_namespace=geo_namespace),
                    write=True, required_scope="knowledge:ownership:write")

    @mcp.tool()
    def review_treaty_match(namespace: str, candidate_id: str, decision: str, reason: str) -> dict:
        """Accept or reject a treaties identity candidate with a reason (an entity identity decision; records are
        never merged)."""
        return safe(lambda conn: identity(conn, True).review(namespace, candidate_id, decision, reason,
                                                             principal_id=who()[0], scopes=who()[1]),
                    write=True, required_scope="knowledge:ownership:review")

    @mcp.tool()
    def revert_treaty_match(namespace: str, candidate_id: str, reason: str) -> dict:
        """Revert a reviewed treaties identity decision."""
        return safe(lambda conn: identity(conn, True).revert(namespace, candidate_id, reason, principal_id=who()[0],
                                                             scopes=who()[1]),
                    write=True, required_scope="knowledge:ownership:review")

    @mcp.tool()
    def link_treaty_records(namespace: str, legal_namespace: str | None = None,
                            sanctions_namespace: str | None = None, trade_namespace: str | None = None) -> dict:
        """Link treaties to Legal works (EU acts by CELEX), sanctions legal bases that cite them and Trade flows
        reporters (published ISO code or accepted match), each link with its basis and the record revisions it
        points at; missing providers and targets are reported, not dropped. No implementation or compliance
        relationship is inferred."""
        from src.kb.treaties_links import TreatiesLinks

        return safe(lambda conn: TreatiesLinks(conn).link_all(
            namespace, principal_id=who()[0], scopes=who()[1], legal_namespace=legal_namespace,
            sanctions_namespace=sanctions_namespace, trade_namespace=trade_namespace),
            write=True, required_scope=WRITE)

    @mcp.tool()
    def list_treaty_links(namespace: str, treaty_key: str | None = None, target_kind: str | None = None) -> dict:
        """Treaty links (legal-work, sanctions-legal-basis, trade-reporter) with basis, status and evidence."""
        from src.kb.treaties_links import TreatiesLinks

        return safe(lambda conn: {"links": TreatiesLinks(conn, initialize=False).links(
            namespace, scopes=who()[1], treaty_key=treaty_key, target_kind=target_kind)}, required_scope=READ)

    @mcp.tool()
    def create_treaties_monitor(namespace: str, request_key: str, watch: str, key: str) -> dict:
        """Subscribe to a treaty (key or published identifier) or a participant (key, exact published name or
        place) for new actions, depositary corrections, entry into force and removals by the source."""
        return safe(lambda conn: monitor(conn, True).create(namespace, request_key, watch=watch, key=key,
                                                            principal_id=who()[0], scopes=who()[1]),
                    write=True, required_scope="knowledge:subscriptions:write")

    @mcp.tool()
    def run_treaties_monitor(subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate a treaties monitor at a committed watermark; each notice cites the new and previous record
        revision and states what changed."""
        return safe(lambda conn: monitor(conn, True).run(subscription_id, watermark, principal_id=who()[0],
                                                         scopes=who()[1]),
                    write=True, required_scope="knowledge:subscriptions:write")

    @mcp.tool()
    def poll_treaties_monitor(subscription_id: str, cursor: str = "") -> dict:
        """Poll a treaties monitor's delivered events."""
        return safe(lambda conn: monitor(conn).poll(subscription_id, principal_id=who()[0], scopes=who()[1],
                                                    cursor=cursor), required_scope="knowledge:subscriptions:read")
