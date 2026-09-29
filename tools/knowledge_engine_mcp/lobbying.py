"""Political lobbying feature entry points: declared interests per dossier, registrant history, official meetings.

Acquisition runs through the shared source-pack tools (pack
``official-political-records``: ``eu-transparency-register``,
``de-lobbyregister``, ``ep-mep-meetings``, ``ec-meetings``,
``uk-consultant-lobbyists``). Every answer reports what a register filed,
cited to the register revision and export behind each row; spend stays the
declared range. Identity matches and dossier links are reviewable and
reversible. No tool states influence, corruption or undeclared lobbying, or
collapses a spend range into a point estimate.
"""

LOBBYING_WRITES = {
    "propose_lobbying_identity_matches",
    "propose_lobbying_identity_link",
    "review_lobbying_identity_match",
    "revert_lobbying_identity_match",
    "link_lobbying_dossier",
    "propose_lobbying_dossier_link",
    "review_lobbying_dossier_link",
    "revert_lobbying_dossier_link",
    "export_dossier_interests_report",
    "create_lobbying_monitor",
    "run_lobbying_monitor",
}
LOBBYING_READS = {
    "lobbying_source_contracts",
    "lobbying_readiness",
    "list_dossier_declared_interests",
    "list_registrant_declarations",
    "list_official_meetings",
    "list_lobbying_identity_candidates",
    "list_lobbying_dossier_links",
    "lobbying_source_identity_candidates",
    "lobbying_grant_candidates",
    "poll_lobbying_monitor",
}
LOBBYING_TOOLS = LOBBYING_WRITES | LOBBYING_READS
READ = "knowledge:political:lobbying:read"
WRITE = "knowledge:political:lobbying:write"
REVIEW = "knowledge:political:lobbying:review"
DOSSIER_READ = "knowledge:political:dossier:read"
# Every scope each tool reads or writes: register records, dossiers, the ownership identity state machine
# (identity views are part of every answer), LEI records, reports, subscriptions, source identity and funding.
LOBBYING_SCOPES = {
    "lobbying_source_contracts": [],
    "lobbying_readiness": [READ],
    "list_dossier_declared_interests": [READ, DOSSIER_READ, "knowledge:ownership:read"],
    "list_registrant_declarations": [READ, "knowledge:ownership:read"],
    "list_official_meetings": [READ, "knowledge:ownership:read"],
    "list_lobbying_identity_candidates": [READ, "knowledge:ownership:read"],
    "list_lobbying_dossier_links": [READ],
    "lobbying_source_identity_candidates": [READ, "knowledge:source-identity:read"],
    "lobbying_grant_candidates": [READ, "knowledge:funding:read"],
    "poll_lobbying_monitor": [READ, "knowledge:subscriptions:read"],
    "propose_lobbying_identity_matches": [
        READ,
        "knowledge:ownership:read",
        "knowledge:ownership:write",
        "knowledge:companies:read",
    ],
    "propose_lobbying_identity_link": [
        READ,
        "knowledge:ownership:read",
        "knowledge:ownership:write",
    ],
    "review_lobbying_identity_match": ["knowledge:ownership:review"],
    "revert_lobbying_identity_match": ["knowledge:ownership:review"],
    "link_lobbying_dossier": [READ, WRITE, DOSSIER_READ],
    "propose_lobbying_dossier_link": [READ, WRITE, DOSSIER_READ],
    "review_lobbying_dossier_link": [READ, REVIEW],
    "revert_lobbying_dossier_link": [READ, REVIEW],
    "export_dossier_interests_report": [
        READ,
        DOSSIER_READ,
        "knowledge:ownership:read",
        "knowledge:reports:write",
    ],
    "create_lobbying_monitor": [READ, "knowledge:subscriptions:write"],
    "run_lobbying_monitor": [
        READ,
        "knowledge:ownership:read",
        "knowledge:subscriptions:write",
    ],
}


def required_scopes(tool_name, mutability):
    return LOBBYING_SCOPES.get(tool_name, [WRITE if mutability == "write" else READ])


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def queries(conn):
        from src.kb.lobbying_queries import LobbyingQueries

        return LobbyingQueries(conn)

    @mcp.tool()
    def lobbying_source_contracts() -> dict:
        """Per-register access decisions, formats, identifiers, revision and deregistration semantics and terms."""
        from src.ingestion.lobbying_sources import PROVIDER_CONTRACTS, REVIEW_BOUNDARY

        return {"contracts": PROVIDER_CONTRACTS, "review_boundary": REVIEW_BOUNDARY}

    @mcp.tool()
    def lobbying_readiness() -> dict:
        """Whether the Political lobbying feature is selected, and per-register acquired exports and access decisions."""
        from src.kb.lobbying import readiness

        return safe(lambda conn: readiness(conn), required_scope=READ)

    @mcp.tool()
    def list_dossier_declared_interests(
        namespace: str,
        dossier_namespace: str,
        dossier_id: str,
        as_of: str | None = None,
        include_candidates: bool = True,
    ) -> dict:
        """Who declared an interest in a dossier and which officials met about it, citing every register revision.

        Rows are marked explicit-field, reviewed-assertion or unreviewed-candidate; registrants are matched (a
        reviewed identity decision) or unmatched register strings. Spend is the declared range with currency and
        period; registers are shown side by side and never reconciled. No influence claim is made.
        """
        return safe(
            lambda conn: queries(conn).dossier_declared_interests(
                namespace,
                dossier_namespace,
                dossier_id,
                principal_id=who()[0],
                scopes=who()[1],
                as_of=as_of,
                include_candidates=include_candidates,
            ),
            required_scope=READ,
        )

    @mcp.tool()
    def list_registrant_declarations(
        namespace: str,
        entry_id: str | None = None,
        register: str | None = None,
        native_id: str | None = None,
        entity_id: str | None = None,
        as_of: str | None = None,
    ) -> dict:
        """What a registrant declared over time (every register revision, cited); an entity id gathers its
        reviewed register records side by side. Deregistrations are lifecycle revisions."""
        return safe(
            lambda conn: queries(conn).registrant_declarations(
                namespace,
                scopes=who()[1],
                entry_id=entry_id,
                register=register,
                native_id=native_id,
                entity_id=entity_id,
                as_of=as_of,
            ),
            required_scope=READ,
        )

    @mcp.tool()
    def list_official_meetings(
        namespace: str,
        official_id: str | None = None,
        official_name: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        as_of: str | None = None,
    ) -> dict:
        """Meetings an office holder declared: date, place, subject and organisations as declared, with dossier links."""
        return safe(
            lambda conn: queries(conn).official_meetings(
                namespace,
                scopes=who()[1],
                official_id=official_id,
                official_name=official_name,
                date_from=date_from,
                date_to=date_to,
                as_of=as_of,
            ),
            required_scope=READ,
        )

    @mcp.tool()
    def list_lobbying_identity_candidates(
        namespace: str, record_key: str | None = None
    ) -> dict:
        """Identity candidates and decisions involving register records, with reviewer, evidence and revisions."""
        from src.kb.lobbying import authorize
        from src.kb.lobbying_identity import LobbyingIdentity

        def run(conn):
            authorize(namespace, who()[1], READ)
            return {
                "candidates": LobbyingIdentity(conn, initialize=False).candidates(
                    namespace, scopes=who()[1], record_key=record_key
                )
            }

        return safe(run, required_scope=READ)

    @mcp.tool()
    def propose_lobbying_identity_matches(
        namespace: str,
        ownership_namespace: str | None = None,
        lei_namespace: str | None = None,
    ) -> dict:
        """Propose candidates from identifiers registers state: across registers, to Corporate Ownership records,
        LEI records and canonical entities (a name alone is never acceptable). Nothing is merged."""
        from src.kb.lobbying_identity import LobbyingIdentity

        return safe(
            lambda conn: LobbyingIdentity(conn).propose(
                namespace,
                principal_id=who()[0],
                scopes=who()[1],
                ownership_namespace=ownership_namespace,
                lei_namespace=lei_namespace,
            ),
            write=True,
            required_scope="knowledge:ownership:write",
        )

    @mcp.tool()
    def propose_lobbying_identity_link(
        namespace: str,
        record_key: str,
        target_key: str,
        target_entity: str,
        evidence: dict,
    ) -> dict:
        """A reviewer's candidate resting on an identifier, or a name plus country/address, the register states."""
        from src.kb.lobbying_identity import LobbyingIdentity

        return safe(
            lambda conn: LobbyingIdentity(conn).propose_link(
                namespace,
                record_key,
                target_key=target_key,
                target_entity=target_entity,
                evidence=evidence,
                principal_id=who()[0],
                scopes=who()[1],
            ),
            write=True,
            required_scope="knowledge:ownership:write",
        )

    @mcp.tool()
    def review_lobbying_identity_match(
        namespace: str, candidate_id: str, decision: str, reason: str
    ) -> dict:
        """Accept or reject a candidate as an identity decision; register records are linked, never rewritten."""
        from src.kb.lobbying_identity import LobbyingIdentity

        def run(conn):
            identity = LobbyingIdentity(conn)
            return identity.view(
                identity.service.review(
                    namespace,
                    candidate_id,
                    decision,
                    reason,
                    principal_id=who()[0],
                    scopes=who()[1],
                )
            )

        return safe(run, write=True, required_scope="knowledge:ownership:review")

    @mcp.tool()
    def revert_lobbying_identity_match(
        namespace: str, candidate_id: str, reason: str
    ) -> dict:
        """Undo an accepted or rejected decision; the registrant returns to unmatched and no record changes."""
        from src.kb.lobbying_identity import LobbyingIdentity

        def run(conn):
            identity = LobbyingIdentity(conn)
            return identity.view(
                identity.service.revert(
                    namespace,
                    candidate_id,
                    reason,
                    principal_id=who()[0],
                    scopes=who()[1],
                )
            )

        return safe(run, write=True, required_scope="knowledge:ownership:review")

    @mcp.tool()
    def lobbying_source_identity_candidates(
        namespace: str, entry_id: str, source_namespace: str
    ) -> dict:
        """OSINT source identities a registrant's declared website points to, for review with decide_source_alias."""
        from src.kb.lobbying_identity import LobbyingIdentity

        return safe(
            lambda conn: LobbyingIdentity(
                conn, initialize=False
            ).source_identity_candidates(
                namespace, entry_id, source_namespace=source_namespace, scopes=who()[1]
            ),
            required_scope=READ,
        )

    @mcp.tool()
    def lobbying_grant_candidates(
        namespace: str, revision_id: str, funding_namespace: str
    ) -> dict:
        """Funding programme records a declared EU grant may refer to: candidates only, never confirmed funding."""
        from src.kb.lobbying import LobbyingStore, authorize

        def run(conn):
            authorize(namespace, who()[1], READ)
            return {
                "candidates": LobbyingStore(conn, initialize=False).grant_candidates(
                    namespace,
                    revision_id,
                    funding_namespace=funding_namespace,
                    scopes=who()[1],
                )
            }

        return safe(run, required_scope=READ)

    @mcp.tool()
    def link_lobbying_dossier(
        namespace: str, dossier_namespace: str, dossier_id: str
    ) -> dict:
        """Link declarations to a dossier's current revision by explicit register fields; shared words only
        produce candidates. Idempotent; links to earlier dossier revisions are kept."""
        from src.kb.lobbying_links import LobbyingDossierLinks

        return safe(
            lambda conn: LobbyingDossierLinks(conn).link_dossier(
                namespace,
                dossier_namespace,
                dossier_id,
                principal_id=who()[0],
                scopes=who()[1],
            ),
            write=True,
            required_scope=WRITE,
        )

    @mcp.tool()
    def propose_lobbying_dossier_link(
        namespace: str,
        revision_id: str,
        interest_key: str,
        dossier_namespace: str,
        dossier_id: str,
        evidence: dict,
    ) -> dict:
        """A reviewer's candidate assertion that a declaration concerns a dossier; it links only once reviewed."""
        from src.kb.lobbying_links import LobbyingDossierLinks

        return safe(
            lambda conn: LobbyingDossierLinks(conn).propose(
                namespace,
                revision_id,
                interest_key,
                dossier_namespace,
                dossier_id,
                evidence=evidence,
                principal_id=who()[0],
                scopes=who()[1],
            ),
            write=True,
            required_scope=WRITE,
        )

    @mcp.tool()
    def review_lobbying_dossier_link(
        namespace: str,
        link_id: str,
        decision: str,
        reason: str,
        evidence: dict | None = None,
    ) -> dict:
        """Accept (a reviewed assertion) or reject a dossier-link candidate with reviewer, evidence and time."""
        from src.kb.lobbying_links import LobbyingDossierLinks

        return safe(
            lambda conn: LobbyingDossierLinks(conn).review(
                namespace,
                link_id,
                decision,
                reason,
                principal_id=who()[0],
                scopes=who()[1],
                evidence=evidence,
            ),
            write=True,
            required_scope=REVIEW,
        )

    @mcp.tool()
    def revert_lobbying_dossier_link(namespace: str, link_id: str, reason: str) -> dict:
        """Revert a reviewed dossier-link decision; explicit register fields cannot be reverted."""
        from src.kb.lobbying_links import LobbyingDossierLinks

        return safe(
            lambda conn: LobbyingDossierLinks(conn).revert(
                namespace, link_id, reason, principal_id=who()[0], scopes=who()[1]
            ),
            write=True,
            required_scope=REVIEW,
        )

    @mcp.tool()
    def list_lobbying_dossier_links(
        namespace: str, dossier_namespace: str, dossier_id: str
    ) -> dict:
        """Links and candidates for a dossier with their basis, cited dossier and register revisions and review state."""
        from src.kb.lobbying_links import LobbyingDossierLinks

        return safe(
            lambda conn: {
                "links": LobbyingDossierLinks(conn, initialize=False).links(
                    namespace, dossier_namespace, dossier_id, scopes=who()[1]
                )
            },
            required_scope=READ,
        )

    @mcp.tool()
    def export_dossier_interests_report(
        namespace: str,
        dossier_namespace: str,
        dossier_id: str,
        request_key: str,
        as_of: str | None = None,
        report_namespace: str | None = None,
    ) -> dict:
        """Export a dossier's declared interests and meetings as a cited authored report (evidence bundle)."""
        return safe(
            lambda conn: queries(conn).export_report(
                namespace,
                dossier_namespace,
                dossier_id,
                request_key,
                principal_id=who()[0],
                scopes=who()[1],
                as_of=as_of,
                report_namespace=report_namespace,
            ),
            write=True,
            required_scope="knowledge:reports:write",
        )

    @mcp.tool()
    def create_lobbying_monitor(
        namespace: str,
        request_key: str,
        watch: str,
        key: str,
        dossier_namespace: str | None = None,
        delivery: dict | None = None,
    ) -> dict:
        """A subscription on a dossier, a registrant entry, a client or an office holder; no new scheduler."""
        from src.kb.lobbying_monitoring import LobbyingMonitor

        return safe(
            lambda conn: LobbyingMonitor(conn).create(
                namespace,
                request_key,
                watch=watch,
                key=key,
                principal_id=who()[0],
                scopes=who()[1],
                dossier_namespace=dossier_namespace,
                delivery=delivery,
            ),
            write=True,
            required_scope="knowledge:subscriptions:write",
        )

    @mcp.tool()
    def run_lobbying_monitor(
        subscription_id: str, watermark: int | None = None
    ) -> dict:
        """Evaluate a monitor at a committed watermark: registrations, deregistrations, spend or client revisions
        (old and new as filed) and meetings, each citing the new and previous register revision."""
        from src.kb.lobbying_monitoring import LobbyingMonitor

        return safe(
            lambda conn: LobbyingMonitor(conn).run(
                subscription_id, watermark, principal_id=who()[0], scopes=who()[1]
            ),
            write=True,
            required_scope="knowledge:subscriptions:write",
        )

    @mcp.tool()
    def poll_lobbying_monitor(subscription_id: str, cursor: str = "") -> dict:
        """Poll a lobbying monitor's events through the subscription delivery path."""
        from src.kb.lobbying_monitoring import LobbyingMonitor

        return safe(
            lambda conn: LobbyingMonitor(conn, initialize=False).poll(
                subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor
            ),
            required_scope="knowledge:subscriptions:read",
        )
