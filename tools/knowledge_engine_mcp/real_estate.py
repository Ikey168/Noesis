"""Geospatial real-estate feature entry points: transactions, parcels, indices, identity, links and monitors (#2228).

Acquisition runs through the shared source-pack tools (pack
``geospatial-real-estate``: ``hmlr-price-paid-data``, ``hmlr-uk-hpi``,
``dvf-geolocalisees`` and ``eurostat-house-price-index`` through the
real-estate connector; ``inspire-cp-france`` and
``inspire-cp-nordrhein-westfalen`` through the WFS path). Every answer cites
its source release. No tool gives a valuation, price estimate or investment
advice, profiles an owner or party, or re-identifies a person; prices and
indices are shown as published, side by side, never averaged, converted or
interpolated.
"""

READ = "knowledge:housing:read"
WRITE = "knowledge:housing:write"
REVIEW = "knowledge:housing:review"
GEO_READ = "knowledge:geospatial:read"
GEO_CALCULATE = "knowledge:geospatial:calculate"
LEGAL_READ = "knowledge:legal:read"
SUBSCRIPTIONS_READ = "knowledge:subscriptions:read"
SUBSCRIPTIONS_WRITE = "knowledge:subscriptions:write"
ENTITY_REVIEW = "knowledge:entity-history:review"

REAL_ESTATE_WRITES = {
    "real_estate_place_as_of",
    "real_estate_parcel_as_of",
    "propose_real_estate_matches",
    "review_real_estate_match",
    "revert_real_estate_match",
    "rematch_real_estate_parcel",
    "link_real_estate_records",
    "cite_real_estate_legal_work",
    "create_real_estate_monitor",
    "run_real_estate_monitor",
}
REAL_ESTATE_READS = {
    "real_estate_source_contracts",
    "real_estate_readiness",
    "list_real_estate_records",
    "inspect_real_estate_record",
    "list_real_estate_vintages",
    "real_estate_parcel_revisions",
    "list_real_estate_matches",
    "list_real_estate_links",
    "poll_real_estate_monitor",
}
REAL_ESTATE_TOOLS = REAL_ESTATE_WRITES | REAL_ESTATE_READS
# Every scope each tool always reads or writes.
REAL_ESTATE_SCOPES = {
    "real_estate_source_contracts": [],
    "real_estate_readiness": [READ],
    "list_real_estate_records": [READ],
    "inspect_real_estate_record": [READ],
    "list_real_estate_vintages": [READ],
    "real_estate_parcel_revisions": [READ],
    "list_real_estate_matches": [READ],
    "list_real_estate_links": [READ],
    "poll_real_estate_monitor": [READ, SUBSCRIPTIONS_READ],
    "real_estate_place_as_of": [READ, GEO_READ, GEO_CALCULATE],
    "real_estate_parcel_as_of": [READ, GEO_READ, GEO_CALCULATE],
    "propose_real_estate_matches": [WRITE, GEO_READ, GEO_CALCULATE],
    "review_real_estate_match": [REVIEW, ENTITY_REVIEW],
    "revert_real_estate_match": [REVIEW, ENTITY_REVIEW],
    "rematch_real_estate_parcel": [WRITE],
    "link_real_estate_records": [WRITE, GEO_READ, GEO_CALCULATE],
    "cite_real_estate_legal_work": [WRITE, LEGAL_READ],
    "create_real_estate_monitor": [READ, SUBSCRIPTIONS_WRITE],
    "run_real_estate_monitor": [READ, SUBSCRIPTIONS_READ, SUBSCRIPTIONS_WRITE],
}


def required_scopes(tool_name, mutability):
    return REAL_ESTATE_SCOPES.get(tool_name, [WRITE if mutability == "write" else READ])


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    @mcp.tool()
    def real_estate_source_contracts() -> dict:
        """Per-source access, identifiers, licence, rate limits, reuse conditions, bounded coverage and live status."""
        from src.ingestion.real_estate_sources import (
            BOUNDED_COVERAGE,
            EXCLUSIONS,
            LIVE_VERIFICATION,
            PROVIDER_CONTRACTS,
            REUSE_CONDITIONS,
        )

        return {"contracts": PROVIDER_CONTRACTS, "reuse_conditions": REUSE_CONDITIONS,
                "bounded_coverage": BOUNDED_COVERAGE, "live_verification": LIVE_VERIFICATION,
                "exclusions": EXCLUSIONS}

    @mcp.tool()
    def real_estate_readiness() -> dict:
        """Whether the Geospatial real-estate feature is selected, record counts and per-source decisions."""
        from src.kb.real_estate import readiness

        return safe(lambda conn: readiness(conn), required_scope=READ)

    @mcp.tool()
    def list_real_estate_records(namespace: str, record_type: str | None = None, provider: str | None = None) -> dict:
        """Transactions, parcels or price-index observations with their provider and native key."""
        from src.kb.real_estate import read_store

        return safe(lambda conn: {"records": read_store(conn, namespace, who()[1]).records(
            namespace, record_type=record_type, provider=provider)}, required_scope=READ)

    @mcp.tool()
    def inspect_real_estate_record(namespace: str, record_id: str) -> dict:
        """One record with every revision (added, changed, withdrawn, removed) and its release vintage."""
        from src.kb.real_estate import read_store

        return safe(lambda conn: read_store(conn, namespace, who()[1]).record(namespace, record_id),
                    required_scope=READ)

    @mcp.tool()
    def list_real_estate_vintages(namespace: str, source_id: str | None = None) -> dict:
        """Every release read per source (a vintage even when nothing in it changed)."""
        from src.kb.real_estate import read_store

        return safe(lambda conn: {"vintages": read_store(conn, namespace, who()[1]).vintages(
            namespace, source_id=source_id)}, required_scope=READ)

    @mcp.tool()
    def real_estate_parcel_revisions(namespace: str, record_id: str, as_of: str | None = None) -> dict:
        """A parcel's revisions known by a date: identifiers, geometry reference and source CRS as published."""
        from src.kb.real_estate import read_store

        def run(conn):
            store = read_store(conn, namespace, who()[1])
            record = store.record(namespace, record_id)
            if record["record_type"] != "parcel":
                return {"ok": False, "error": {"code": "not_a_parcel", "message": "record is not a parcel"}}
            return {"record": {k: v for k, v in record.items() if k != "revisions"},
                    "revisions": store.revisions(namespace, record_id, as_of=as_of)}

        return safe(run, required_scope=READ)

    @mcp.tool()
    def real_estate_place_as_of(namespace: str, as_of: str | None = None, place_id: str | None = None,
                                codes: list[dict] | None = None) -> dict:
        """Transactions, price indices and parcels published for a place (or place codes) by a date, with citations.
        Writes only replayable spatial receipts."""
        from src.kb.real_estate_queries import RealEstateQueries

        return safe(lambda conn: RealEstateQueries(conn).place(
            namespace, scopes=who()[1], principal_id=who()[0], as_of=as_of, place_id=place_id, codes=codes),
            write=True, required_scope=READ)

    @mcp.tool()
    def real_estate_parcel_as_of(namespace: str, as_of: str | None = None, record_id: str | None = None,
                                 reference: str | None = None) -> dict:
        """A parcel's revision in force, history, matched transactions, links and containment context."""
        from src.kb.real_estate_queries import RealEstateQueries

        return safe(lambda conn: RealEstateQueries(conn).parcel(
            namespace, scopes=who()[1], principal_id=who()[0], as_of=as_of, record_id=record_id,
            reference=reference), write=True, required_scope=READ)

    @mcp.tool()
    def propose_real_estate_matches(namespace: str) -> dict:
        """Record exact identifier matches and offer address- or geometry-derived candidates for review."""
        from src.kb.real_estate_identity import RealEstateIdentity

        return safe(lambda conn: RealEstateIdentity(conn).propose(namespace, principal_id=who()[0],
                                                                  scopes=who()[1]), write=True, required_scope=WRITE)

    @mcp.tool()
    def list_real_estate_matches(namespace: str, state: str | None = None, transaction_id: str | None = None) -> dict:
        """Transaction-parcel and transaction-place matches with basis, pinned revisions and drift."""
        from src.kb.real_estate_identity import RealEstateIdentity

        def run(conn):
            identity = RealEstateIdentity(conn, initialize=False)
            return {"matches": identity.matches(namespace, scopes=who()[1], state=state,
                                                transaction_id=transaction_id),
                    "unmatched": identity.unmatched(namespace, scopes=who()[1])}

        return safe(run, required_scope=READ)

    @mcp.tool()
    def review_real_estate_match(namespace: str, match_id: str, decision: str, reason: str) -> dict:
        """Accept or reject a candidate match; recorded as an entity identity decision."""
        from src.kb.real_estate_identity import RealEstateIdentity

        return safe(lambda conn: RealEstateIdentity(conn).review(namespace, match_id, decision, reason,
                                                                 principal_id=who()[0], scopes=who()[1]),
                    write=True, required_scope=REVIEW)

    @mcp.tool()
    def revert_real_estate_match(namespace: str, match_id: str, reason: str) -> dict:
        """Revert an accepted or rejected candidate decision."""
        from src.kb.real_estate_identity import RealEstateIdentity

        return safe(lambda conn: RealEstateIdentity(conn).revert(namespace, match_id, reason,
                                                                 principal_id=who()[0], scopes=who()[1]),
                    write=True, required_scope=REVIEW)

    @mcp.tool()
    def rematch_real_estate_parcel(namespace: str, match_id: str, reason: str) -> dict:
        """Re-match a parcel match whose parcel was revised; the old match is kept as superseded."""
        from src.kb.real_estate_identity import RealEstateIdentity

        return safe(lambda conn: RealEstateIdentity(conn).rematch(namespace, match_id, reason,
                                                                  principal_id=who()[0], scopes=who()[1]),
                    write=True, required_scope=WRITE)

    @mcp.tool()
    def link_real_estate_records(namespace: str) -> dict:
        """Links from shared published identifiers (parcel reference, geography code, dataset code) only."""
        from src.kb.real_estate_links import RealEstateLinks

        return safe(lambda conn: RealEstateLinks(conn).link_identifiers(namespace, principal_id=who()[0],
                                                                        scopes=who()[1]),
                    write=True, required_scope=WRITE)

    @mcp.tool()
    def cite_real_estate_legal_work(namespace: str, record_id: str, identifier: str, relation: str,
                                    stated_in: str, legal_namespace: str, jurisdiction: str | None = None) -> dict:
        """An explicit citation stated by a publication, resolved to exactly one legal work or kept unresolved."""
        from src.kb.real_estate_links import RealEstateLinks

        return safe(lambda conn: RealEstateLinks(conn).cite(
            namespace, record_id, identifier, relation=relation, stated_in=stated_in,
            legal_namespace=legal_namespace, jurisdiction=jurisdiction, principal_id=who()[0], scopes=who()[1]),
            write=True, required_scope=WRITE)

    @mcp.tool()
    def list_real_estate_links(namespace: str, record_id: str | None = None, as_of: str | None = None) -> dict:
        """Links with their basis, both records and the revision in force (as of a date)."""
        from src.kb.real_estate_links import RealEstateLinks

        return safe(lambda conn: {"links": RealEstateLinks(conn, initialize=False).links(
            namespace, scopes=who()[1], record_id=record_id, as_of=as_of)}, required_scope=READ)

    @mcp.tool()
    def create_real_estate_monitor(namespace: str, request_key: str, places: list[str] | None = None,
                                   codes: list[dict] | None = None, parcels: list[str] | None = None) -> dict:
        """Follow places, place codes or parcels for new releases, revisions, index vintages and parcel revisions."""
        from src.kb.real_estate_monitoring import RealEstateMonitor

        return safe(lambda conn: RealEstateMonitor(conn).create(
            namespace, request_key, principal_id=who()[0], scopes=who()[1], places=places, codes=codes,
            parcels=parcels), write=True, required_scope=READ)

    @mcp.tool()
    def run_real_estate_monitor(subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate a monitor at a committed geospatial-real-estate watermark; unchanged releases emit nothing."""
        from src.kb.real_estate_monitoring import RealEstateMonitor

        return safe(lambda conn: RealEstateMonitor(conn).run(subscription_id, watermark, principal_id=who()[0],
                                                             scopes=who()[1]), write=True, required_scope=READ)

    @mcp.tool()
    def poll_real_estate_monitor(subscription_id: str, cursor: str = "") -> dict:
        """Delivered real-estate notifications after a cursor."""
        from src.kb.real_estate_monitoring import RealEstateMonitor

        return safe(lambda conn: RealEstateMonitor(conn, initialize=False).poll(
            subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor), required_scope=READ)
