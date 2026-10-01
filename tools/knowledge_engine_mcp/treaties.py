"""Legal treaties entry points (#2581): treaties and treaty actions as the depositary published them.

Registered through :mod:`tools.knowledge_engine_mcp.legal` (the Legal pack's
tools); acquisition runs through the shared source-pack tools (pack
``legal-research`` 1.5.0: ``cellar-eu-international-agreements``,
``coe-treaty-office-charts`` and the declined ``untc-multilateral-status``).
UNTC, CELLAR and Council of Europe coverage are the optional features
``treaties-untc``, ``treaties-eu`` and ``treaties-coe``; Legislation (Legal
works), Sanctions and Trade flows links degrade to ``provider_unavailable``.

Exclusions: no legal advice, no inference of obligations or compliance, no
interpretation of the legal effect of reservations, declarations or objections,
no treaty-text redistribution beyond what each source licenses (texts are
linked) and no natural-person data. Every answer is checked against the
TR01 minimisation decision and the exclusion keys before it is returned.
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
    "lookup_treaties",
    "treaty_status_as_of",
    "participant_treaty_actions",
    "treaty_reservations_and_objections",
    "treaty_revision_history",
    "list_treaty_identity_candidates",
    "list_treaty_links",
    "export_treaty_evidence_bundle",
    "poll_treaties_monitor",
}
TREATIES_TOOLS = TREATIES_WRITES | TREATIES_READS
READ = "knowledge:legal:read"
WRITE = "knowledge:legal:write"
REVIEW = "knowledge:legal:review"
# Every scope each tool always reads or writes.
TREATIES_SCOPES = {
    "treaties_source_contracts": [],
    "treaties_readiness": [READ],
    "lookup_treaties": [READ],
    "treaty_status_as_of": [READ],
    "participant_treaty_actions": [READ],
    "treaty_reservations_and_objections": [READ],
    "treaty_revision_history": [READ],
    "list_treaty_identity_candidates": [READ],
    "list_treaty_links": [READ],
    "export_treaty_evidence_bundle": [READ],
    "poll_treaties_monitor": [READ, "knowledge:subscriptions:read"],
    "propose_treaty_matches": [READ, WRITE, "knowledge:geospatial:read"],
    "review_treaty_match": [READ, REVIEW],
    "revert_treaty_match": [READ, REVIEW],
    "link_treaty_records": [READ, WRITE],
    "create_treaties_monitor": [READ, "knowledge:subscriptions:write"],
    "run_treaties_monitor": [READ, "knowledge:subscriptions:write"],
}


def required_scopes(tool_name, mutability):
    return TREATIES_SCOPES.get(tool_name, [WRITE if mutability == "write" else READ])


def guarded(value):
    """Refuse an answer that would carry a natural-person field or an obligation/compliance/effect key."""
    from src.kb.treaties_records import (
        TreatiesError,
        forbidden_keys,
        minimisation_violations,
    )

    found = minimisation_violations(value) + forbidden_keys(value)
    if found:
        raise TreatiesError("exclusion_violation", f"answer carries excluded fields: {', '.join(found)}")
    return value


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def queries(conn):
        from src.kb.treaties_queries import TreatyQueries

        return TreatyQueries(conn)

    def identity(conn, write=False):
        from src.kb.treaties_identity import TreatiesIdentity

        return TreatiesIdentity(conn, initialize=write)

    def links(conn, write=False):
        from src.kb.treaties_links import TreatiesLinks

        return TreatiesLinks(conn, initialize=write)

    def monitor(conn, write=False):
        from src.kb.treaties_monitoring import TreatiesMonitor

        return TreatiesMonitor(conn, initialize=write)

    @mcp.tool()
    def treaties_source_contracts() -> dict:
        """Per-source access, licence and redistribution terms, rate limits, revision models, the licence
        decisions (the UN Treaty Collection is declined pending written permission), the minimisation decision,
        bounded coverage and LIVE_VERIFICATION for UNTC, CELLAR and the Council of Europe Treaty Office."""
        from src.ingestion.treaties_sources import (
            BOUNDED_COVERAGE,
            LICENCE_DECISIONS,
            LIVE_VERIFICATION,
            MINIMISATION,
            PROVIDER_CONTRACTS,
            REVIEW_BOUNDARY,
        )
        from src.kb.treaties_records import EXCLUSIONS

        return {"contracts": PROVIDER_CONTRACTS, "licence_decisions": LICENCE_DECISIONS,
                "live_verification": LIVE_VERIFICATION, "minimisation": MINIMISATION,
                "bounded_coverage": BOUNDED_COVERAGE, "review_boundary": REVIEW_BOUNDARY, "exclusions": EXCLUSIONS}

    @mcp.tool()
    def treaties_readiness() -> dict:
        """Whether the treaties-untc, treaties-eu and treaties-coe features are selected and what each provider
        has acquired; declined and unverified-live access is stated."""
        from src.kb.treaties_store import readiness

        return safe(lambda conn: readiness(conn), required_scope=READ)

    @mcp.tool()
    def lookup_treaties(namespace: str, identifier: str | None = None, source: str | None = None) -> dict:
        """Treaties by published identifier (untc:XXIX-99, celex:..., cets:990, UNTS registration) with the title,
        depositary, adoption and entry into force as published and the revision cited; accepted cross-source
        matches add the other sources' records. Texts are linked, not reproduced. No legal advice."""
        return safe(lambda conn: guarded(queries(conn).lookup(namespace, scopes=who()[1], identifier=identifier,
                                                              source=source)), required_scope=READ)

    @mcp.tool()
    def treaty_status_as_of(namespace: str, treaty: str, participant: str, as_of: str,
                            known_as_of: str | None = None) -> dict:
        """The action chain a depositary records for a treaty and a participant (key, ISO 3166-1 code via an
        accepted match, or name as published) on a date: signature, consent to be bound, reservations,
        withdrawals and entry into force selected by published dates, with pending and unclear items returned as
        such and every item citing its depositary revision. Describes the record only: no legal advice, no
        inferred obligation, compliance or effect of a reservation."""
        return safe(lambda conn: guarded(queries(conn).status_as_of(namespace, treaty, participant, as_of,
                                                                    scopes=who()[1], known_as_of=known_as_of)),
                    required_scope=READ)

    @mcp.tool()
    def participant_treaty_actions(namespace: str, participant: str, date_from: str | None = None,
                                   date_to: str | None = None, action_types: list[str] | None = None,
                                   source: str | None = None) -> dict:
        """A participant's treaty actions over a period, filtered by action type and source, each citing its
        record revision; undated items are listed separately. No legal advice or inferred obligations."""
        return safe(lambda conn: guarded(queries(conn).participant_actions(
            namespace, participant, scopes=who()[1], date_from=date_from, date_to=date_to,
            action_types=action_types, source=source)), required_scope=READ)

    @mcp.tool()
    def treaty_reservations_and_objections(namespace: str, treaty: str, kinds: list[str] | None = None,
                                           participant: str | None = None, source: str | None = None,
                                           date_from: str | None = None, date_to: str | None = None) -> dict:
        """A treaty's reservations, declarations, objections and withdrawals quoted verbatim with their anchors,
        an objection linked to the objected reservation only where the source links it, each citing its record
        revision. No interpretation of the legal effect of any reservation; no legal advice."""
        return safe(lambda conn: guarded(queries(conn).treaty_statements(
            namespace, treaty, scopes=who()[1], kinds=kinds, participant=participant, source=source,
            date_from=date_from, date_to=date_to)), required_scope=READ)

    @mcp.tool()
    def treaty_revision_history(namespace: str, treaty: str) -> dict:
        """Every revision of a treaty and its actions (new, revised by a depositary correction, removed by the
        source, relisted) with the fields each revision changed."""
        return safe(lambda conn: guarded(queries(conn).history(namespace, treaty, scopes=who()[1])),
                    required_scope=READ)

    @mcp.tool()
    def export_treaty_evidence_bundle(namespace: str, treaty: str, participant: str, as_of: str) -> dict:
        """A noesis-evidence-bundle-v1 for a treaty status answer citing every item with source, record revision
        and as-of time; pending, unclear and not-acquired items are omissions."""
        def run(conn):
            store = queries(conn)
            answer = guarded(store.status_as_of(namespace, treaty, participant, as_of, scopes=who()[1]))
            return {"answer_status": answer["status"], "bundle": store.export_bundle(answer)}
        return safe(run, required_scope=READ)

    @mcp.tool()
    def list_treaty_identity_candidates(namespace: str, kind: str | None = None, subject: str | None = None) -> dict:
        """Reviewable identity candidates (participant to place, participant to canonical entity, treaty to
        treaty) with method, evidence and confidence, and the unmatched participants and treaties."""
        def run(conn):
            item = identity(conn)
            return {"candidates": item.candidates(namespace, scopes=who()[1], kind=kind, subject=subject),
                    "unmatched": item.unmatched(namespace, scopes=who()[1])}
        return safe(run, required_scope=READ)

    @mcp.tool()
    def propose_treaty_matches(namespace: str, geo_namespace: str = "global") -> dict:
        """Propose participant-to-place matches (published ISO 3166-1 codes first, then the exact name of a
        coded place; names alone are never acceptable) and treaty-to-treaty matches through published
        cross-references only. Nothing is merged or accepted automatically."""
        return safe(lambda conn: identity(conn, True).propose(namespace, principal_id=who()[0], scopes=who()[1],
                                                              geo_namespace=geo_namespace),
                    write=True, required_scope=WRITE)

    @mcp.tool()
    def review_treaty_match(namespace: str, candidate_id: str, decision: str, reason: str) -> dict:
        """Accept or reject a treaty identity candidate with a reason (an entity identity decision; records are
        never merged)."""
        return safe(lambda conn: identity(conn, True).review(namespace, candidate_id, decision, reason,
                                                             principal_id=who()[0], scopes=who()[1]),
                    write=True, required_scope=REVIEW)

    @mcp.tool()
    def revert_treaty_match(namespace: str, candidate_id: str, reason: str) -> dict:
        """Revert a reviewed treaty identity decision."""
        return safe(lambda conn: identity(conn, True).revert(namespace, candidate_id, reason, principal_id=who()[0],
                                                             scopes=who()[1]),
                    write=True, required_scope=REVIEW)

    @mcp.tool()
    def link_treaty_records(namespace: str, legal_namespace: str | None = None,
                            sanctions_namespace: str | None = None, trade_namespace: str | None = None) -> dict:
        """Link treaties to Legal works of the EU acts CELLAR cites, to sanctions legal bases citing them and to
        Trade flows reporters through accepted matches; missing targets and absent providers are reported. No
        implementation or trade relationship is inferred."""
        return safe(lambda conn: links(conn, True).link_all(
            namespace, principal_id=who()[0], scopes=who()[1], legal_namespace=legal_namespace,
            sanctions_namespace=sanctions_namespace, trade_namespace=trade_namespace),
            write=True, required_scope=WRITE)

    @mcp.tool()
    def list_treaty_links(namespace: str, treaty_key: str | None = None, link_kind: str | None = None,
                          status: str | None = None) -> dict:
        """Treaty links with their basis, the treaty revision and the target revision they point at."""
        return safe(lambda conn: {"links": links(conn).links(namespace, scopes=who()[1], treaty_key=treaty_key,
                                                              link_kind=link_kind, status=status)},
                    required_scope=READ)

    @mcp.tool()
    def create_treaties_monitor(namespace: str, request_key: str, watch: str, key: str) -> dict:
        """Subscribe to a treaty (record key or identifier) or a participant: new actions, depositary
        corrections, removals by the source and entry into force."""
        return safe(lambda conn: monitor(conn, True).create(namespace, request_key, watch=watch, key=key,
                                                            principal_id=who()[0], scopes=who()[1]),
                    write=True, required_scope="knowledge:subscriptions:write")

    @mcp.tool()
    def run_treaties_monitor(subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate a treaties monitor at a committed watermark; each notice cites the new and previous record
        revision and states what changed (a record change, not an assessment)."""
        return safe(lambda conn: monitor(conn, True).run(subscription_id, watermark, principal_id=who()[0],
                                                         scopes=who()[1]),
                    write=True, required_scope="knowledge:subscriptions:write")

    @mcp.tool()
    def poll_treaties_monitor(subscription_id: str, cursor: str = "") -> dict:
        """Poll a treaties monitor's delivered events."""
        return safe(lambda conn: monitor(conn).poll(subscription_id, principal_id=who()[0], scopes=who()[1],
                                                    cursor=cursor), required_scope="knowledge:subscriptions:read")
