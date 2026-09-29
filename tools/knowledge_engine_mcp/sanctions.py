"""Legal sanctions feature entry points: per-list statements as of a date, control-list editions, identity, monitors.

Acquisition runs through the shared source-pack tools (pack ``legal-research``:
``eu-sanctions-consolidated``, ``un-sc-consolidated``, ``ofac-sls``,
``uk-sanctions-list``, ``cellar-dual-use-2021-821``). Every answer reports what
a list or an annex edition stated, cited to snapshots, listing revisions and
passage locators. No tool returns a screening result, risk score or compliance
status, gives legal advice, or treats a similar name as the same party.
"""

SANCTIONS_WRITES = {
    "propose_sanctions_identity_matches",
    "propose_sanctions_identity_link",
    "review_sanctions_identity_match",
    "revert_sanctions_identity_match",
    "create_sanctions_monitor",
    "run_sanctions_monitor",
    "record_sanctions_trade_correlations",
    "resolve_sanctions_legal_bases",
    "sync_control_list_entries",
}
SANCTIONS_READS = {
    "sanctions_source_contracts",
    "sanctions_readiness",
    "lookup_sanctions_designation",
    "designation_history_as_of",
    "control_list_entry_as_of",
    "compare_control_list_editions",
    "sanctions_trade_context",
    "list_sanctions_identity_candidates",
    "poll_sanctions_monitor",
}
SANCTIONS_TOOLS = SANCTIONS_WRITES | SANCTIONS_READS
SANCTIONS_SCOPES = {
    "sanctions_source_contracts": [],
    # Proposing reads the lists, reads Corporate Ownership records (and the candidates it returns) and writes
    # candidates into the ownership identity state machine.
    "propose_sanctions_identity_matches": [
        "knowledge:legal:read",
        "knowledge:ownership:read",
        "knowledge:ownership:write",
    ],
    "propose_sanctions_identity_link": [
        "knowledge:legal:read",
        "knowledge:ownership:write",
    ],
    "review_sanctions_identity_match": ["knowledge:ownership:review"],
    "revert_sanctions_identity_match": ["knowledge:ownership:review"],
    "list_sanctions_identity_candidates": [
        "knowledge:legal:read",
        "knowledge:ownership:read",
    ],
    "create_sanctions_monitor": [
        "knowledge:legal:read",
        "knowledge:subscriptions:write",
    ],
    "run_sanctions_monitor": ["knowledge:legal:read", "knowledge:subscriptions:write"],
    "poll_sanctions_monitor": ["knowledge:legal:read", "knowledge:subscriptions:read"],
}


def required_scopes(tool_name, mutability):
    return SANCTIONS_SCOPES.get(
        tool_name,
        ["knowledge:legal:write" if mutability == "write" else "knowledge:legal:read"],
    )


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def queries(conn):
        from src.kb.sanctions_queries import SanctionsQueries

        return SanctionsQueries(conn)

    @mcp.tool()
    def sanctions_source_contracts() -> dict:
        """Per-list access decisions, formats, identifiers, revision/delisting semantics and legal-act references."""
        from src.ingestion.sanctions_sources import PROVIDER_CONTRACTS
        from src.kb.sanctions_trade import COMEXT_SELECTION

        return {"contracts": PROVIDER_CONTRACTS, "trade_statistics": COMEXT_SELECTION}

    @mcp.tool()
    def sanctions_readiness() -> dict:
        """Whether the Legal sanctions feature is selected, and per-list acquired snapshots and access decisions."""
        from src.kb.sanctions import readiness

        return safe(lambda conn: readiness(conn), required_scope="knowledge:legal:read")

    @mcp.tool()
    def lookup_sanctions_designation(
        namespace: str,
        list_id: str | None = None,
        list_entry_id: str | None = None,
        identifier: str | None = None,
        name: str | None = None,
    ) -> dict:
        """Current per-list statements for an entry, a stated identifier or an exactly stated name; never merged.

        Returns list statements only: no screening result, risk score or compliance status. An exact stated
        name is a list statement, not an identity.
        """
        return safe(
            lambda conn: queries(conn).lookup(
                namespace,
                scopes=who()[1],
                list_id=list_id,
                list_entry_id=list_entry_id,
                identifier=identifier,
                name=name,
            ),
            required_scope="knowledge:legal:read",
        )

    @mcp.tool()
    def designation_history_as_of(
        namespace: str,
        as_of: str,
        designation_id: str | None = None,
        list_id: str | None = None,
        list_entry_id: str | None = None,
        identifier: str | None = None,
        name: str | None = None,
    ) -> dict:
        """What each list stated on a date: listing revision, aliases, programme, legal basis passages, delisting.

        Grouped by list; accepted identity decisions appear as links and unreviewed ones as candidates. Dates
        before the first snapshot, after a delisting or between differing snapshots are `unknown` or
        `not_listed_in_snapshot`. No screening result, risk score or compliance status is returned.
        """
        return safe(
            lambda conn: queries(conn).history_as_of(
                namespace,
                as_of,
                scopes=who()[1],
                designation_id=designation_id,
                list_id=list_id,
                list_entry_id=list_entry_id,
                identifier=identifier,
                name=name,
            ),
            required_scope="knowledge:legal:read",
        )

    @mcp.tool()
    def control_list_entry_as_of(
        namespace: str, control_code: str, as_of: str, control_list: str = "eu-dual-use"
    ) -> dict:
        """The annex edition applying on a date and the control code's passage in it, with every edition listed.

        Source text by edition only: no classification of goods, licensing determination or legal advice.
        """
        return safe(
            lambda conn: queries(conn).control_entry_as_of(
                namespace,
                control_code,
                as_of,
                scopes=who()[1],
                control_list=control_list,
            ),
            required_scope="knowledge:legal:read",
        )

    @mcp.tool()
    def compare_control_list_editions(
        namespace: str, control_code: str, left_version_id: str, right_version_id: str
    ) -> dict:
        """Passage-level change of one control code between two editions (source change, not legal effect)."""
        return safe(
            lambda conn: queries(conn).compare_control_entry(
                namespace,
                control_code,
                left_version_id,
                right_version_id,
                scopes=who()[1],
            ),
            required_scope="knowledge:legal:read",
        )

    @mcp.tool()
    def sanctions_trade_context(namespace: str, control_code: str) -> dict:
        """Correlated product codes (a sourced lookup aid) and acquired Comext flows with their vintages cited."""
        from src.kb.sanctions_trade import SanctionsTrade

        return safe(
            lambda conn: SanctionsTrade(conn, initialize=False).context(
                namespace, control_code, scopes=who()[1]
            ),
            required_scope="knowledge:legal:read",
        )

    @mcp.tool()
    def list_sanctions_identity_candidates(
        namespace: str, designation_id: str | None = None
    ) -> dict:
        """Identity candidates and decisions involving designations, with decision id, reviewer, reason, evidence."""
        from src.kb.sanctions_identity import SanctionsIdentity

        return safe(
            lambda conn: {
                "candidates": SanctionsIdentity(conn, initialize=False).candidates(
                    namespace, scopes=who()[1], designation_id=designation_id
                )
            },
            required_scope="knowledge:legal:read",
        )

    @mcp.tool()
    def propose_sanctions_identity_matches(
        namespace: str, ownership_namespace: str | None = None
    ) -> dict:
        """Propose candidates from identifiers the lists state (cross-list and to Corporate Ownership records)."""
        from src.kb.sanctions_identity import SanctionsIdentity

        return safe(
            lambda conn: SanctionsIdentity(conn).propose(
                namespace,
                principal_id=who()[0],
                scopes=who()[1],
                ownership_namespace=ownership_namespace,
            ),
            write=True,
            required_scope="knowledge:ownership:write",
        )

    @mcp.tool()
    def propose_sanctions_identity_link(
        namespace: str,
        designation_id: str,
        target_key: str,
        target_entity: str,
        evidence: dict,
    ) -> dict:
        """A reviewer's candidate resting on an identifier or a name plus attributes the list states."""
        from src.kb.sanctions_identity import SanctionsIdentity

        return safe(
            lambda conn: SanctionsIdentity(conn).propose_link(
                namespace,
                designation_id,
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
    def review_sanctions_identity_match(
        namespace: str, candidate_id: str, decision: str, reason: str
    ) -> dict:
        """Accept or reject a candidate as an identity decision; list records are linked, never rewritten."""
        from src.kb.sanctions_identity import SanctionsIdentity

        def run(conn):
            identity = SanctionsIdentity(conn)
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
    def revert_sanctions_identity_match(
        namespace: str, candidate_id: str, reason: str
    ) -> dict:
        """Undo an accepted or rejected decision; the prior state is restored and no record changes."""
        from src.kb.sanctions_identity import SanctionsIdentity

        def run(conn):
            identity = SanctionsIdentity(conn)
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
    def resolve_sanctions_legal_bases(
        namespace: str, legal_namespace: str | None = None
    ) -> dict:
        """Re-link legal-basis citations to acquired CELLAR works by exact CELEX/ELI; unresolved stays unresolved."""

        def run(conn):
            from src.kb.sanctions import WRITE_SCOPE, SanctionsStore, authorize

            authorize(namespace, who()[1], WRITE_SCOPE, write=True)
            return {
                "changed": SanctionsStore(conn).resolve_legal_bases(
                    namespace, legal_namespace=legal_namespace
                )
            }

        return safe(run, write=True, required_scope="knowledge:legal:write")

    @mcp.tool()
    def sync_control_list_entries(
        namespace: str, control_list: str = "eu-dual-use"
    ) -> dict:
        """Persist control-list-entry records for every captured annex edition (idempotent)."""

        def run(conn):
            from src.kb.sanctions import WRITE_SCOPE, SanctionsStore, authorize

            authorize(namespace, who()[1], WRITE_SCOPE, write=True)
            return SanctionsStore(conn).sync_control_entries(namespace, control_list)

        return safe(run, write=True, required_scope="knowledge:legal:write")

    @mcp.tool()
    def record_sanctions_trade_correlations(namespace: str, table: dict) -> dict:
        """Store a sourced control-code-to-CN8/HS6 correlation table (a lookup aid; changes are new revisions)."""
        from src.kb.sanctions_trade import SanctionsTrade

        return safe(
            lambda conn: SanctionsTrade(conn).record_correlations(
                namespace, table, principal_id=who()[0], scopes=who()[1]
            ),
            write=True,
            required_scope="knowledge:legal:write",
        )

    @mcp.tool()
    def create_sanctions_monitor(
        namespace: str,
        request_key: str,
        watch: str,
        key: str,
        list_id: str | None = None,
        delivery: dict | None = None,
    ) -> dict:
        """A subscription on a designation id, a programme (within a list) or a control code; no new scheduler."""
        from src.kb.sanctions_monitoring import SanctionsMonitor

        return safe(
            lambda conn: SanctionsMonitor(conn).create(
                namespace,
                request_key,
                watch=watch,
                key=key,
                list_id=list_id,
                principal_id=who()[0],
                scopes=who()[1],
                delivery=delivery,
            ),
            write=True,
            required_scope="knowledge:subscriptions:write",
        )

    @mcp.tool()
    def run_sanctions_monitor(
        subscription_id: str, watermark: int | None = None
    ) -> dict:
        """Evaluate a monitor at a committed watermark: listed, amended, delisted or new-edition source changes."""
        from src.kb.sanctions_monitoring import SanctionsMonitor

        return safe(
            lambda conn: SanctionsMonitor(conn).run(
                subscription_id, watermark, principal_id=who()[0], scopes=who()[1]
            ),
            write=True,
            required_scope="knowledge:subscriptions:write",
        )

    @mcp.tool()
    def poll_sanctions_monitor(subscription_id: str, cursor: str = "") -> dict:
        """Poll a sanctions monitor's events through the subscription delivery path."""
        from src.kb.sanctions_monitoring import SanctionsMonitor

        return safe(
            lambda conn: SanctionsMonitor(conn, initialize=False).poll(
                subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor
            ),
            required_scope="knowledge:subscriptions:read",
        )
