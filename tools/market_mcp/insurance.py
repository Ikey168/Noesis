"""Market insurance feature entry points (#2230, IN11): insurer, market and event lookups as of a date.

Acquisition runs through the shared source-pack tools (pack
``insurance-supervisory-and-catastrophe-losses``). Every answer applies the
Market publication cutoff, lists each publisher separately with its revisions and
citations, and reports the IN01 licence decisions (excluded and metadata-only
sources included). There is no solvency or rating assessment, no loss modelling,
and no ratio, reconciliation or aggregate across publishers.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

READ = "market:insurance:read"
INSURANCE_READS = {
    "insurance_insurer_as_of",
    "insurance_market_as_of",
    "insurance_event_estimates",
    "insurance_revision_history",
    "insurance_coverage",
    "list_insurance_identity_candidates",
    "poll_insurance_monitor",
}
INSURANCE_WRITES = {
    "propose_insurance_identity_matches",
    "review_insurance_identity_match",
    "link_insurance_loss_estimates",
    "review_insurance_event_link",
    "create_insurance_monitor",
    "run_insurance_monitor",
}
INSURANCE_TOOLS = INSURANCE_READS | INSURANCE_WRITES
# Every scope a tool always uses; scopes for optional namespaces are checked when they are given.
INSURANCE_SCOPES = {
    "insurance_insurer_as_of": [READ],
    "insurance_market_as_of": [READ],
    "insurance_event_estimates": [READ],
    "insurance_revision_history": [READ],
    "insurance_coverage": [READ],
    "list_insurance_identity_candidates": [READ, "knowledge:ownership:read"],
    "propose_insurance_identity_matches": [READ, "knowledge:ownership:read", "knowledge:ownership:write"],
    "review_insurance_identity_match": [READ, "knowledge:ownership:review"],
    "link_insurance_loss_estimates": [READ, "market:insurance:write"],
    "review_insurance_event_link": [READ, "market:insurance:review"],
    "create_insurance_monitor": [READ, "knowledge:subscriptions:write"],
    "run_insurance_monitor": [READ, "knowledge:subscriptions:read", "knowledge:subscriptions:write"],
    "poll_insurance_monitor": [READ, "knowledge:subscriptions:read"],
}
OPTIONAL_SCOPES = {
    "lei_namespace": "knowledge:companies:read",
    "hazard_namespace": "knowledge:hazards:read",
}


def required_scopes(tool_name: str, mutability: str) -> list[str]:
    return list(INSURANCE_SCOPES.get(tool_name, ["market:insurance:write" if mutability == "write" else READ]))


def register(mcp: Any, context: Callable[[], tuple[str, set[str]]], connect: Callable[[bool], Any]) -> None:
    """Register the insurance tools on the Market MCP server (``connect(write)`` opens the warehouse)."""

    def call(tool: str, operation: Callable[[Any, str, set[str]], Any], *, write: bool = False,
             optional: dict[str, Any] | None = None) -> dict:
        principal, scopes = context()
        needed = list(INSURANCE_SCOPES.get(tool, [READ]))
        needed += [OPTIONAL_SCOPES[arg] for arg, value in (optional or {}).items() if value]
        missing = [s for s in needed if s not in scopes]
        if missing and "operator" not in scopes:
            return {"ok": False, "error": {"code": "unauthorized", "message": f"{', '.join(missing)} required"}}
        conn = None
        try:
            conn = connect(write)
            return {"ok": True, "result": operation(conn, principal, scopes)}
        except Exception as exc:  # noqa: BLE001 - one stable MCP envelope
            code = getattr(exc, "code", None)
            return {"ok": False, "error": {"code": code if isinstance(code, str) else "insurance_unavailable",
                                           "message": "the insurance request could not be answered"}}
        finally:
            if conn is not None:
                conn.close()

    def queries(conn):
        from src.domains.market.insurance import InsuranceQueries

        return InsuranceQueries(conn)

    @mcp.tool()
    def insurance_insurer_as_of(namespace: str, insurer: str, as_of: str, acquired_by_ms: int | None = None,
                                lei_namespace: str | None = None) -> dict:
        """An insurer's SFCR figures (quoted QRT cells) and insurer-level statistics as of a date (YYYY-MM-DD).

        ``insurer`` is an LEI, a NAIC company code, an insurance party key or an accepted match. Group and solo
        reports stay distinct; with ``lei_namespace`` a group report appears beside an undertaking only through
        a GLEIF parent relationship, labelled as the group's. No solvency or rating assessment.
        """
        return call("insurance_insurer_as_of", lambda c, p, s: queries(c).insurer(
            namespace, insurer, as_of=as_of, scopes=s, acquired_by_ms=acquired_by_ms, lei_namespace=lei_namespace),
            optional={"lei_namespace": lei_namespace})

    @mcp.tool()
    def insurance_market_as_of(namespace: str, country: str, as_of: str, indicator: str | None = None,
                               line_of_business: str | None = None, acquired_by_ms: int | None = None) -> dict:
        """Supervisory statistics for a market (country) as of a date, per publisher with definitions, currency,
        period, release vintage and history; confidential cells keep their markers; nothing is reconciled."""
        return call("insurance_market_as_of", lambda c, p, s: queries(c).market(
            namespace, country, as_of=as_of, scopes=s, indicator=indicator, line_of_business=line_of_business,
            acquired_by_ms=acquired_by_ms))

    @mcp.tool()
    def insurance_event_estimates(namespace: str, event: str, as_of: str, acquired_by_ms: int | None = None) -> dict:
        """Every publisher's catastrophe-loss estimate series for an event as of a date, listed separately (never
        merged) with the revision in force, all revisions known then, links and citations. ``event`` is the
        event name or reference as published, a published identifier, or a linked Natural Hazards record id."""
        return call("insurance_event_estimates", lambda c, p, s: queries(c).event(
            namespace, event, as_of=as_of, scopes=s, acquired_by_ms=acquired_by_ms))

    @mcp.tool()
    def insurance_revision_history(namespace: str, record_id: str, as_of: str | None = None) -> dict:
        """The full revision history of one insurance record (an estimate, figure or report), in publication
        order, each revision cited; optionally only what was published by ``as_of``."""
        return call("insurance_revision_history", lambda c, p, s: queries(c).revision_history(
            namespace, record_id, scopes=s, as_of=as_of))

    @mcp.tool()
    def insurance_coverage() -> dict:
        """The recorded access and licence decision per source and publisher (in scope, metadata-only, excluded),
        with reasons and live-verification status."""

        def run(conn, principal, scopes):
            from src.domains.market.insurance import coverage
            from src.ingestion.insurance_sources import LIVE_VERIFICATION

            return {"decisions": coverage(), "live_verification": LIVE_VERIFICATION}

        principal, scopes = context()
        if READ not in scopes and "operator" not in scopes:
            return {"ok": False, "error": {"code": "unauthorized", "message": f"{READ} required"}}
        return {"ok": True, "result": run(None, principal, scopes)}

    @mcp.tool()
    def list_insurance_identity_candidates(namespace: str, party: str | None = None) -> dict:
        """Reviewable insurer identity candidates (name + jurisdiction or name-only) and their states."""

        def run(conn, principal, scopes):
            from src.domains.market.insurance_identity import InsuranceIdentity

            return InsuranceIdentity(conn, initialize=False).candidates(namespace, scopes=scopes, party=party)

        return call("list_insurance_identity_candidates", run)

    @mcp.tool()
    def propose_insurance_identity_matches(namespace: str, ownership_namespace: str) -> dict:
        """Offer name + jurisdiction candidates between insurers and ownership entities (exact LEI/NAIC matches
        need no review and are never offered as candidates). Idempotent."""

        def run(conn, principal, scopes):
            from src.domains.market.insurance_identity import InsuranceIdentity

            return InsuranceIdentity(conn).propose(namespace, principal_id=principal, scopes=scopes,
                                                   ownership_namespace=ownership_namespace)

        return call("propose_insurance_identity_matches", run, write=True)

    @mcp.tool()
    def review_insurance_identity_match(namespace: str, candidate_id: str, decision: str, reason: str,
                                        revert: bool = False) -> dict:
        """Accept or reject an insurer identity candidate with a reason, or revert a decision (revert=true)."""

        def run(conn, principal, scopes):
            from src.domains.market.insurance_identity import InsuranceIdentity

            identity = InsuranceIdentity(conn, initialize=False)
            if revert:
                return identity.revert(namespace, candidate_id, reason, principal_id=principal, scopes=scopes)
            return identity.review(namespace, candidate_id, decision, reason, principal_id=principal, scopes=scopes)

        return call("review_insurance_identity_match", run, write=True)

    @mcp.tool()
    def link_insurance_loss_estimates(namespace: str, hazard_namespace: str | None = None) -> dict:
        """Link current loss estimates to Natural Hazards events and insurer reports on published identifiers or
        explicit citations; name and date matches become candidates for review. Idempotent."""

        def run(conn, principal, scopes):
            from src.domains.market.insurance import InsuranceLinks

            return InsuranceLinks(conn).propose(namespace, principal_id=principal, scopes=scopes,
                                                hazard_namespace=hazard_namespace)

        return call("link_insurance_loss_estimates", run, write=True, optional={"hazard_namespace": hazard_namespace})

    @mcp.tool()
    def review_insurance_event_link(namespace: str, link_id: str, decision: str, reason: str) -> dict:
        """Accept or reject a name/date candidate link between an estimate and a hazard event."""

        def run(conn, principal, scopes):
            from src.domains.market.insurance import InsuranceLinks

            return InsuranceLinks(conn).review(namespace, link_id, decision, reason, principal_id=principal,
                                               scopes=scopes)

        return call("review_insurance_event_link", run, write=True)

    @mcp.tool()
    def create_insurance_monitor(namespace: str, request_key: str, watch: str, target: str) -> dict:
        """Watch an insurer, a market (country) or an event for new releases, new or corrected SFCRs, new
        loss-estimate revisions and identity-match changes (a knowledge subscription; no separate scheduler)."""

        def run(conn, principal, scopes):
            from src.domains.market.insurance_monitoring import InsuranceMonitor

            return InsuranceMonitor(conn).create(namespace, request_key, watch=watch, target=target,
                                                 principal_id=principal, scopes=scopes)

        return call("create_insurance_monitor", run, write=True)

    @mcp.tool()
    def run_insurance_monitor(subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate an insurance monitor at a committed watermark; notifications cite the publication and carry no
        solvency, rating or loss-trend verdict."""

        def run(conn, principal, scopes):
            from src.domains.market.insurance_monitoring import InsuranceMonitor

            return InsuranceMonitor(conn, initialize=False).run(subscription_id, watermark, principal_id=principal,
                                                                scopes=scopes)

        return call("run_insurance_monitor", run, write=True)

    @mcp.tool()
    def poll_insurance_monitor(subscription_id: str, cursor: str = "") -> dict:
        """Poll an insurance monitor's recorded events from a cursor."""

        def run(conn, principal, scopes):
            from src.domains.market.insurance_monitoring import InsuranceMonitor

            return InsuranceMonitor(conn, initialize=False).poll(subscription_id, principal_id=principal,
                                                                 scopes=scopes, cursor=cursor)

        return call("poll_insurance_monitor", run)
