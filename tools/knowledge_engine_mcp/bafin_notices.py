"""Market bafin-notices feature entry points: BaFin notices as of a date, dossiers, identity, projection, monitors.

Acquisition runs through the shared source-pack tools (pack
``bafin-capital-market-notices``). Every answer applies the Market publication
cutoff (a notice is never seen before it was published), states its semantics
and carries no investment advice, signal, recommendation or sentiment. Holdings
are never summed across notifiers, and a position that stopped being published
is never read as 0 %.
"""

from typing import Any

BAFIN_WRITES = {
    "propose_bafin_identity_matches",
    "propose_bafin_identity_link",
    "review_bafin_identity_match",
    "revert_bafin_identity_match",
    "project_bafin_voting_rights",
    "create_bafin_notice_monitor",
    "run_bafin_notice_monitor",
}
BAFIN_READS = {
    "bafin_holders_as_of",
    "bafin_managers_transactions",
    "bafin_net_short_positions",
    "bafin_warnings_for_entity",
    "bafin_authorisation_status",
    "bafin_notice_dossier",
    "inspect_bafin_notice",
    "list_bafin_identity_candidates",
    "poll_bafin_notice_monitor",
}
BAFIN_TOOLS = BAFIN_WRITES | BAFIN_READS
READ = "market:bafin:read"
# Every scope a tool always reads or writes. Scopes only an optional argument needs (another namespace for
# instruments or LEIs, an ownership namespace to propose against) are checked when that argument is used.
BAFIN_SCOPES = {
    "list_bafin_identity_candidates": [READ, "knowledge:ownership:read"],
    "propose_bafin_identity_matches": [
        READ,
        "knowledge:ownership:read",
        "knowledge:ownership:write",
    ],
    "propose_bafin_identity_link": [READ, "knowledge:ownership:write"],
    "review_bafin_identity_match": [READ, "knowledge:ownership:review"],
    "revert_bafin_identity_match": [READ, "knowledge:ownership:review"],
    "project_bafin_voting_rights": [
        READ,
        "knowledge:ownership:read",
        "knowledge:ownership:write",
    ],
    "create_bafin_notice_monitor": [READ, "knowledge:subscriptions:write"],
    # Running reads the monitor (subscriptions:read) and records its evaluation (subscriptions:write).
    "run_bafin_notice_monitor": [
        READ,
        "knowledge:subscriptions:read",
        "knowledge:subscriptions:write",
    ],
    # Polling re-checks current access to the retained notice evidence (market:bafin:read) the monitor was made with.
    "poll_bafin_notice_monitor": [READ, "knowledge:subscriptions:read"],
}
OPTIONAL_SCOPES = {
    "market_namespace": "market:instruments:read",
    "lei_namespace": "knowledge:companies:read",
}
QUERY_EXAMPLES = {
    "bafin_holders_as_of": {
        "arguments": {
            "namespace": "market-bafin",
            "isin": "DE000MSTR014",
            "as_of": "2026-04-14",
        },
        "semantics": "the latest voting-rights notification per notifier published by the end of 2026-04-14 (UTC), "
        "corrections collapsed, with the thresholds each stated percentage reaches; never summed",
    },
    "bafin_net_short_positions": {
        "arguments": {
            "namespace": "market-bafin",
            "isin": "DE000MSTR014",
            "as_of": "2026-05-01",
        },
        "semantics": "the latest published position per holder; a position no longer published is 'below "
        "publication threshold or closed', never 0 %",
    },
    "bafin_notice_dossier": {
        "arguments": {
            "namespace": "market-bafin",
            "isin": "DE000MSTR014",
            "as_of": "2026-07-02",
        },
        "semantics": "holders, managers' transactions in the preceding 365 days, short positions, warnings and "
        "measures naming related entities and their authorisation, each cited with its notice revision",
    },
}


def required_scopes(tool_name, mutability):
    return BAFIN_SCOPES.get(
        tool_name, ["market:bafin:write" if mutability == "write" else READ]
    )


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def checked(tool_name, operation, *, optional: dict[str, Any] | None = None):
        """Run with every scope the tool always uses present, plus those its optional arguments need."""

        def run(conn):
            from src.domains.market.bafin_notices import BafinError

            scopes = who()[1]
            needed = list(BAFIN_SCOPES.get(tool_name, [READ]))
            needed += [
                OPTIONAL_SCOPES[arg] for arg, value in (optional or {}).items() if value
            ]
            missing = [s for s in needed if s not in scopes]
            if missing and "operator" not in scopes:
                raise BafinError("unauthorized", f"{', '.join(missing)} required")
            return operation(conn)

        return run

    def queries(conn):
        from src.domains.market.bafin_queries import BafinQueries

        return BafinQueries(conn)

    @mcp.tool()
    def bafin_holders_as_of(
        namespace: str,
        isin: str,
        as_of: str,
        acquired_by_ms: int | None = None,
        threshold: str | None = None,
    ) -> dict:
        """Holders above the WpHG thresholds for an issuer as of a date (YYYY-MM-DD, end of day UTC).

        The latest voting-rights notification per notifier published by the cutoff (and acquired by acquired_by_ms
        when given), corrections collapsed to the latest published, with the holder chain as stated, percentages by
        § 33/§ 38/§ 39 and the thresholds they reach. A notice published after the cutoff is never seen even when
        its event date is earlier. Stale holders keep their last notice date. Never summed across notifiers; no
        advice or signal.
        """
        return safe(
            checked(
                "bafin_holders_as_of",
                lambda conn: queries(conn).holders_above_thresholds(
                    namespace,
                    isin,
                    as_of,
                    scopes=who()[1],
                    acquired_by_ms=acquired_by_ms,
                    threshold=threshold,
                ),
            ),
            required_scope=READ,
        )

    @mcp.tool()
    def bafin_managers_transactions(
        namespace: str,
        date_from: str,
        date_to: str,
        isin: str | None = None,
        person: str | None = None,
        as_of: str | None = None,
        acquired_by_ms: int | None = None,
    ) -> dict:
        """Managers' transactions (Art. 19 MAR) for an issuer or a person in a window, as published by the cutoff
        (as_of, default date_to). Each notification keeps its trades and the aggregate as published; amendments
        supersede. A person is matched by name within each notice's issuer; withdrawn person data never matches.
        No sentiment metric."""
        return safe(
            checked(
                "bafin_managers_transactions",
                lambda conn: queries(conn).managers_transactions(
                    namespace,
                    scopes=who()[1],
                    date_from=date_from,
                    date_to=date_to,
                    isin=isin,
                    person=person,
                    as_of=as_of,
                    acquired_by_ms=acquired_by_ms,
                ),
            ),
            required_scope=READ,
        )

    @mcp.tool()
    def bafin_net_short_positions(
        namespace: str, isin: str, as_of: str, acquired_by_ms: int | None = None
    ) -> dict:
        """Published net short positions (Art. 11 SSR) in an issuer as of a date: the latest per holder, with
        positions that stopped being published marked 'below publication threshold or closed' (never 0 %) and
        only a labelled sum of published positions."""
        return safe(
            checked(
                "bafin_net_short_positions",
                lambda conn: queries(conn).net_short_positions(
                    namespace,
                    isin,
                    as_of,
                    scopes=who()[1],
                    acquired_by_ms=acquired_by_ms,
                ),
            ),
            required_scope=READ,
        )

    @mcp.tool()
    def bafin_warnings_for_entity(
        namespace: str,
        name: str | None = None,
        party: str | None = None,
        as_of: str | None = None,
        acquired_by_ms: int | None = None,
    ) -> dict:
        """BaFin warnings and measures naming an entity: strings equal to the name (never attributed without a
        reviewed match) and those a reviewed identity match links to the party record key, with removals kept."""
        return safe(
            checked(
                "bafin_warnings_for_entity",
                lambda conn: queries(conn).warnings_for_entity(
                    namespace,
                    scopes=who()[1],
                    name=name,
                    party=party,
                    as_of=as_of,
                    acquired_by_ms=acquired_by_ms,
                ),
            ),
            required_scope=READ,
        )

    @mcp.tool()
    def bafin_authorisation_status(
        namespace: str,
        as_of: str,
        bafin_id: str | None = None,
        name: str | None = None,
        acquired_by_ms: int | None = None,
    ) -> dict:
        """Whether an entity appears in the acquired BaFin company database as of a date, with each licence's
        start and end as published. Absence means not acquired or not listed, never that no licence exists."""
        return safe(
            checked(
                "bafin_authorisation_status",
                lambda conn: queries(conn).authorisation_status(
                    namespace,
                    scopes=who()[1],
                    as_of=as_of,
                    bafin_id=bafin_id,
                    name=name,
                    acquired_by_ms=acquired_by_ms,
                ),
            ),
            required_scope=READ,
        )

    @mcp.tool()
    def bafin_notice_dossier(
        namespace: str,
        isin: str,
        as_of: str,
        acquired_by_ms: int | None = None,
        window_days: int = 365,
        market_namespace: str | None = None,
        lei_namespace: str | None = None,
        export_bundle: bool = False,
    ) -> dict:
        """A cited notice dossier for an issuer as of a date: holders above thresholds with chains and corrections,
        managers' transactions in the preceding window, short positions, warnings and measures naming related
        entities and their authorisation status, each with publication time and locator. market_namespace resolves
        the issuer by ISIN (needs market:instruments:read), lei_namespace by LEI (needs knowledge:companies:read).
        export_bundle returns a noesis-evidence-bundle-v1. No advice or signals."""

        def run(conn):
            from src.domains.market.bafin_queries import export_dossier_bundle

            dossier = queries(conn).dossier(
                namespace,
                isin,
                as_of,
                scopes=who()[1],
                principal_id=who()[0],
                acquired_by_ms=acquired_by_ms,
                window_days=window_days,
                market_namespace=market_namespace,
                lei_namespace=lei_namespace,
            )
            if export_bundle:
                return {
                    "dossier": dossier,
                    "bundle": export_dossier_bundle(conn, dossier),
                }
            return dossier

        return safe(
            checked(
                "bafin_notice_dossier",
                run,
                optional={
                    "market_namespace": market_namespace,
                    "lei_namespace": lei_namespace,
                },
            ),
            required_scope=READ,
        )

    @mcp.tool()
    def inspect_bafin_notice(namespace: str, notice_id: str) -> dict:
        """Every revision of one BaFin notice (with the change each made and its listing history) and the
        correction chain it belongs to."""

        def run(conn):
            from src.domains.market.bafin_notices import (
                BafinNoticeStore,
                authorize,
                correction_chains,
            )

            authorize(namespace, who()[1], READ)
            store = BafinNoticeStore(conn, initialize=False)
            history = store.history(namespace, notice_id)
            kind = history[-1]["notice"]["kind"]
            chains = correction_chains(
                store.visible(namespace, kinds=(kind,))["notices"]
            )
            return {
                "notice_id": notice_id,
                "revisions": history,
                "chain": chains.get(notice_id),
            }

        return safe(checked("inspect_bafin_notice", run), required_scope=READ)

    @mcp.tool()
    def list_bafin_identity_candidates(
        namespace: str, party: str | None = None
    ) -> dict:
        """Reviewable identity candidates for parties BaFin notices name (proposed, accepted, rejected, reverted),
        with evidence; natural persons only ever within their issuer."""

        def run(conn):
            from src.domains.market.bafin_identity import BafinIdentity
            from src.domains.market.bafin_notices import BafinNoticeStore

            BafinNoticeStore(conn, initialize=False).require_ready()
            identity = BafinIdentity(conn, initialize=False)
            return {
                "candidates": identity.candidates(
                    namespace, scopes=who()[1], party=party
                )
            }

        return safe(checked("list_bafin_identity_candidates", run), required_scope=READ)

    @mcp.tool()
    def propose_bafin_identity_matches(
        namespace: str, ownership_namespace: str | None = None
    ) -> dict:
        """Propose identity candidates for notifiers, chain members, short sellers, authorised entities and
        warning strings (LEI and BaFin ID exact, name + country, name only as never-acceptable similar-name).
        Natural persons are never proposed automatically. Idempotent; nothing is merged."""

        def run(conn):
            from src.domains.market.bafin_identity import BafinIdentity

            return BafinIdentity(conn).propose(
                namespace,
                principal_id=who()[0],
                scopes=who()[1],
                ownership_namespace=ownership_namespace,
            )

        return safe(
            checked("propose_bafin_identity_matches", run),
            write=True,
            required_scope=READ,
        )

    @mcp.tool()
    def propose_bafin_identity_link(
        namespace: str,
        party: str,
        target_key: str,
        target_source: str,
        kind: str,
        value: str,
        target_entity: str | None = None,
    ) -> dict:
        """A reviewer-proposed candidate for one party, resting on what a notice states (kind lei, bafin-id or
        name) and where the target states it. A person link across issuers is flagged and stays proposed until
        reviewed."""

        def run(conn):
            from src.domains.market.bafin_identity import BafinIdentity

            return BafinIdentity(conn).propose_link(
                namespace,
                party,
                target_key=target_key,
                target_entity=target_entity,
                evidence={"kind": kind, "value": value, "target_source": target_source},
                principal_id=who()[0],
                scopes=who()[1],
            )

        return safe(
            checked("propose_bafin_identity_link", run), write=True, required_scope=READ
        )

    def _decide(namespace, candidate_id, action, reason, decision=None):
        def run(conn):
            from src.domains.market.bafin_identity import PREFIX, BafinIdentity
            from src.domains.market.bafin_notices import BafinError, authorize

            authorize(namespace, who()[1], READ)
            identity = BafinIdentity(conn)
            candidate = identity.service._row(namespace, candidate_id)
            if not (
                candidate["left_key"].startswith(PREFIX)
                or candidate["right_key"].startswith(PREFIX)
            ):
                raise BafinError(
                    "invalid_request", "the candidate does not concern a BaFin party"
                )
            if action == "review":
                return identity.service.review(
                    namespace,
                    candidate_id,
                    decision,
                    reason,
                    principal_id=who()[0],
                    scopes=who()[1],
                )
            return identity.service.revert(
                namespace, candidate_id, reason, principal_id=who()[0], scopes=who()[1]
            )

        return run

    @mcp.tool()
    def review_bafin_identity_match(
        namespace: str, candidate_id: str, decision: str, reason: str
    ) -> dict:
        """Accept or reject a BaFin party candidate as an entity identity decision (a similar name alone is never
        accepted). Records stay separate."""
        return safe(
            checked(
                "review_bafin_identity_match",
                _decide(namespace, candidate_id, "review", reason, decision),
            ),
            write=True,
            required_scope=READ,
        )

    @mcp.tool()
    def revert_bafin_identity_match(
        namespace: str, candidate_id: str, reason: str
    ) -> dict:
        """Revert an accepted or rejected BaFin party candidate; earlier decisions are never reactivated."""
        return safe(
            checked(
                "revert_bafin_identity_match",
                _decide(namespace, candidate_id, "revert", reason),
            ),
            write=True,
            required_scope=READ,
        )

    @mcp.tool()
    def project_bafin_voting_rights(
        namespace: str, ownership_namespace: str, issuers: list[str] | None = None
    ) -> dict:
        """Project voting-rights notifications into Corporate Ownership as voting_rights control assertions, as
        stated by the notifier, citing each notice revision: one per chain member and WpHG basis; corrections
        revise the assertion (history kept); unreviewed holders stay source strings. Idempotent."""

        def run(conn):
            from src.domains.market.bafin_ownership import BafinOwnershipProjection

            return BafinOwnershipProjection(conn).project(
                namespace,
                ownership_namespace,
                principal_id=who()[0],
                scopes=who()[1],
                issuers=issuers,
            )

        return safe(
            checked("project_bafin_voting_rights", run), write=True, required_scope=READ
        )

    @mcp.tool()
    def create_bafin_notice_monitor(
        namespace: str,
        request_key: str,
        watch: str,
        isin: str | None = None,
        name: str | None = None,
        person: str | None = None,
        delivery: dict | None = None,
    ) -> dict:
        """A subscription watching an issuer (ISIN), a holder or entity (name), a manager within an issuer (ISIN and
        person) or the warning list (optionally one name); no new scheduler."""

        def run(conn):
            from src.domains.market.bafin_monitoring import BafinNoticeMonitor

            return BafinNoticeMonitor(conn).create(
                namespace,
                request_key,
                watch=watch,
                isin=isin,
                name=name,
                person=person,
                principal_id=who()[0],
                scopes=who()[1],
                delivery=delivery,
            )

        return safe(
            checked("create_bafin_notice_monitor", run),
            write=True,
            required_scope="knowledge:subscriptions:write",
        )

    @mcp.tool()
    def run_bafin_notice_monitor(
        subscription_id: str, watermark: int | None = None
    ) -> dict:
        """Evaluate a BaFin notice monitor at a committed watermark: new or corrected notifications, threshold
        crossings, new managers' transactions, new or ended short positions, new warnings or measures and
        authorisation changes, each with a receipt citing the notice revision. The first evaluation is a baseline."""

        def run(conn):
            from src.domains.market.bafin_monitoring import BafinNoticeMonitor

            return BafinNoticeMonitor(conn).run(
                subscription_id, watermark, principal_id=who()[0], scopes=who()[1]
            )

        return safe(
            checked("run_bafin_notice_monitor", run),
            write=True,
            required_scope="knowledge:subscriptions:write",
        )

    @mcp.tool()
    def poll_bafin_notice_monitor(subscription_id: str, cursor: str = "") -> dict:
        """Poll a BaFin notice monitor's events through the subscription delivery path."""

        def run(conn):
            from src.domains.market.bafin_monitoring import BafinNoticeMonitor

            return BafinNoticeMonitor(conn, initialize=False).poll(
                subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor
            )

        return safe(
            checked("poll_bafin_notice_monitor", run),
            required_scope="knowledge:subscriptions:read",
        )
