"""OSINT platform-transparency feature entry points: political ads by advertiser or election, moderation statements
by platform, ground and period, takedown notices, reviewable identity, links and monitors (#2580).

Acquisition runs through the shared source-pack tools (pack
``osint-platform-transparency`` 1.0.0: ``dsa-sor-dumps``,
``meta-ad-library-political``, ``google-political-ads``, ``lumen-notices``).
DSA, Meta, Google and Lumen coverage are the separate optional features
``platform-transparency-dsa``, ``-meta``, ``-google`` and ``-lumen`` of the
Osint bundle; elections, campaign-finance, lobbying and ownership links degrade
to an ``unavailable`` / ``missing_targets`` report when those providers are
absent. Every answer cites each item with its source, record revision and
as-of time.

Exclusions: no user-level profiling, no collection of private content, no
inference of coordinated behaviour and no conversion of spend or impression
ranges into point estimates. The SP01 minimisation decision applies to every
output: free text, content ids, notifier identity, creative text and audience
distributions are never stored or returned, and Lumen notices are returned only
with ``knowledge:osint:platform-transparency:notices:read`` (otherwise counted).
"""

PLATFORM_TRANSPARENCY_WRITES = {
    "propose_platform_transparency_identity_matches",
    "review_platform_transparency_identity_match",
    "revert_platform_transparency_identity_match",
    "link_platform_transparency_records",
    "create_platform_transparency_monitor",
    "run_platform_transparency_monitor",
}
PLATFORM_TRANSPARENCY_READS = {
    "platform_transparency_source_contracts",
    "platform_transparency_readiness",
    "political_ads_for_advertiser",
    "political_ads_for_election",
    "moderation_statement_counts",
    "platform_transparency_record_history",
    "platform_takedown_notices",
    "export_platform_transparency_evidence_bundle",
    "list_platform_transparency_identity_candidates",
    "list_platform_transparency_links",
    "poll_platform_transparency_monitor",
}
PLATFORM_TRANSPARENCY_TOOLS = PLATFORM_TRANSPARENCY_WRITES | PLATFORM_TRANSPARENCY_READS
READ = "knowledge:osint:platform-transparency:read"
WRITE = "knowledge:osint:platform-transparency:write"
NOTICES = "knowledge:osint:platform-transparency:notices:read"
OWNERSHIP_READ = "knowledge:ownership:read"
# Every scope each tool always reads or writes: platform-transparency records, the ownership identity state machine
# (advertiser answers, identity views and links rest on accepted decisions) and subscriptions. The notices scope is
# conditional: without it Lumen notices are counted, never returned.
PLATFORM_TRANSPARENCY_SCOPES = {
    "platform_transparency_source_contracts": [],
    "platform_transparency_readiness": [READ],
    "political_ads_for_advertiser": [READ, OWNERSHIP_READ],
    "political_ads_for_election": [READ],
    "moderation_statement_counts": [READ],
    "platform_transparency_record_history": [READ],
    "platform_takedown_notices": [READ],
    "export_platform_transparency_evidence_bundle": [READ, OWNERSHIP_READ],
    "list_platform_transparency_identity_candidates": [READ, OWNERSHIP_READ],
    "list_platform_transparency_links": [READ],
    "poll_platform_transparency_monitor": [READ, "knowledge:subscriptions:read"],
    "propose_platform_transparency_identity_matches": [READ, OWNERSHIP_READ, "knowledge:ownership:write"],
    "review_platform_transparency_identity_match": [OWNERSHIP_READ, "knowledge:ownership:review"],
    "revert_platform_transparency_identity_match": [OWNERSHIP_READ, "knowledge:ownership:review"],
    "link_platform_transparency_records": [READ, WRITE, OWNERSHIP_READ],
    "create_platform_transparency_monitor": [READ, OWNERSHIP_READ, "knowledge:subscriptions:write"],
    "run_platform_transparency_monitor": [READ, OWNERSHIP_READ, "knowledge:subscriptions:write"],
}
EXCLUSIONS_NOTE = ("No user-level profiling, no private content, no inference of coordinated behaviour and no "
                   "conversion of spend or impression ranges into point estimates (SP01 minimisation applies).")


def required_scopes(tool_name, mutability):
    return PLATFORM_TRANSPARENCY_SCOPES.get(tool_name, [WRITE if mutability == "write" else READ])


def guard(answer):
    """Refuse an output that would carry a point estimate, a profile, a coordination reading or a minimised field."""
    from src.ingestion.platform_transparency_sources import forbidden_paths
    from src.kb.platform_transparency_records import forbidden_keys

    found = forbidden_keys(answer) + [p for p in forbidden_paths(answer) if not p.endswith(".url")]
    if found:
        return {"ok": False, "error": {"code": "minimisation_violation",
                                       "message": "the answer carries a field the exclusions or SP01 forbid",
                                       "paths": found[:20]}}
    return {**answer, "exclusions_note": EXCLUSIONS_NOTE} if isinstance(answer, dict) else answer


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def queries(conn):
        from src.kb.platform_transparency_queries import PlatformTransparencyQueries

        return PlatformTransparencyQueries(conn)

    @mcp.tool()
    def platform_transparency_source_contracts() -> dict:
        """Per-provider endpoints, token handling, licences, rate limits, revision models, bounded coverage,
        LIVE_VERIFICATION status (Lumen: gated, researcher access not granted) and the SP01 data-minimisation
        decision for the DSA Transparency Database, Meta Ad Library, Google political ads and Lumen."""
        from src.ingestion.platform_transparency_sources import source_contracts

        return source_contracts()

    @mcp.tool()
    def platform_transparency_readiness() -> dict:
        """Which platform-transparency features are selected, records per provider, and degraded providers."""
        from src.kb.platform_transparency_records import readiness

        return safe(lambda conn: readiness(conn), required_scope=READ)

    @mcp.tool()
    def political_ads_for_advertiser(namespace: str, advertiser: str, as_of: str | None = None) -> dict:
        """Political ads of an advertiser (Google AR id, Meta page id, declared funding entity, or a campaign-finance
        committee / lobbying / ownership record reached through accepted identity) with delivery dates and spend and
        impression ranges exactly as published; removed ads shown with their removal revision; each ad cited by
        revision. Ranges are never converted to midpoints or sums; no user-level profiling or coordination
        inference; none_on_record is not evidence that none ran."""
        return safe(lambda conn: guard(queries(conn).ads_for_advertiser(namespace, advertiser, scopes=who()[1],
                                                                         as_of=as_of)), required_scope=READ)

    @mcp.tool()
    def political_ads_for_election(namespace: str, election_id: str, elections_namespace: str | None = None) -> dict:
        """Political ads linked to an elections record (declared selection or published election label), grouped by
        advertiser, ranges exactly as published and each ad cited by revision. No point estimates."""
        return safe(lambda conn: guard(queries(conn).ads_for_election(
            namespace, election_id, scopes=who()[1], elections_namespace=elections_namespace)), required_scope=READ)

    @mcp.tool()
    def moderation_statement_counts(namespace: str, platform: str, start: str, end: str,
                                    ground: str | None = None) -> dict:
        """Counts of stored DSA statements of reasons for a platform and period by decision type, ground and
        category, with automated-detection and automated-decision flags as published; states the stored window,
        days without a dump and the dump versions used. Counts are over stored records, not platform totals."""
        return safe(lambda conn: guard(queries(conn).moderation_statements(
            namespace, platform, scopes=who()[1], start=start, end=end, ground=ground)), required_scope=READ)

    @mcp.tool()
    def platform_transparency_record_history(namespace: str, record_key: str) -> dict:
        """Every revision of one statement, ad, advertiser, dump release or notice (removals and corrections are
        revisions), with source, revision and as-of time. Lumen notices need the notices scope."""
        def run(conn):
            if record_key.startswith("platform-transparency:lumen:") and NOTICES not in set(who()[1]) \
                    and "operator" not in set(who()[1]):
                return {"ok": False, "error": {"code": "unauthorized", "message": f"{NOTICES} is required"}}
            return guard(queries(conn).statement_history(namespace, record_key, scopes=who()[1]))

        return safe(run, required_scope=READ)

    @mcp.tool()
    def platform_takedown_notices(namespace: str, recipient: str) -> dict:
        """Lumen takedown notices to a platform under researcher access, with Lumen's redactions as published;
        returned only with the notices scope, otherwise counted. Bodies, works and URLs are never stored."""
        return safe(lambda conn: guard(queries(conn).takedown_notices(namespace, recipient, scopes=who()[1])),
                    required_scope=READ)

    @mcp.tool()
    def export_platform_transparency_evidence_bundle(namespace: str, query: str, key: str, as_of: str | None = None,
                                                     start: str | None = None, end: str | None = None) -> dict:
        """An evidence bundle for an advertiser, election or moderation answer (query: advertiser | election |
        moderation): assertions each citing the record revision, source and as-of time behind them."""
        def run(conn):
            ask = queries(conn)
            if query == "advertiser":
                answer = ask.ads_for_advertiser(namespace, key, scopes=who()[1], as_of=as_of)
            elif query == "election":
                answer = ask.ads_for_election(namespace, key, scopes=who()[1])
            elif query == "moderation":
                if not start or not end:
                    return {"ok": False, "error": {"code": "invalid_request", "message": "moderation needs start and "
                                                                                        "end"}}
                answer = ask.moderation_statements(namespace, key, scopes=who()[1], start=start, end=end)
            else:
                return {"ok": False, "error": {"code": "invalid_request",
                                               "message": "query is advertiser, election or moderation"}}
            return guard({"status": answer["status"], "query": query, "key": key, "as_of": as_of,
                          "evidence_bundle": ask.evidence_bundle(answer), "exclusions": answer["exclusions"]})

        return safe(run, required_scope=READ)

    @mcp.tool()
    def list_platform_transparency_identity_candidates(namespace: str, record_key: str | None = None) -> dict:
        """Identity candidates and decisions for advertisers and funding entities, with method, evidence and
        confidence, and the subjects that stay unmatched. Natural-person records are never targets."""
        from src.kb.platform_transparency_identity import PlatformTransparencyIdentity
        from src.kb.platform_transparency_records import authorize

        def run(conn):
            authorize(namespace, who()[1], READ)
            identity = PlatformTransparencyIdentity(conn, initialize=False)
            return {"candidates": identity.candidates(namespace, scopes=who()[1], record_key=record_key),
                    "unmatched": identity.unmatched(namespace, scopes=who()[1])}

        return safe(run, required_scope=READ)

    @mcp.tool()
    def list_platform_transparency_links(namespace: str, kind: str | None = None, record_key: str | None = None,
                                         target_key: str | None = None) -> dict:
        """Election, campaign-finance, lobbying and ownership links with the ad revision they point at and their
        basis. A link is not evidence of coordination or influence."""
        from src.kb.platform_transparency_links import PlatformTransparencyLinks

        return safe(lambda conn: {"links": PlatformTransparencyLinks(conn, initialize=False).links(
            namespace, scopes=who()[1], kind=kind, record_key=record_key, target_key=target_key)},
            required_scope=READ)

    @mcp.tool()
    def propose_platform_transparency_identity_matches(namespace: str, ownership_namespace: str | None = None,
                                                       lobbying_namespace: str | None = None,
                                                       campaign_finance_namespace: str | None = None) -> dict:
        """Propose reviewable matches (published FEC ids first, then name+country) from advertisers and funding
        entities to campaign-finance committees, lobbying registrants and clients and ownership entities; nothing is
        merged or accepted automatically, natural persons are never targets and absent providers are reported."""
        from src.kb.platform_transparency_identity import PlatformTransparencyIdentity

        return safe(lambda conn: PlatformTransparencyIdentity(conn).propose(
            namespace, principal_id=who()[0], scopes=who()[1], ownership_namespace=ownership_namespace,
            lobbying_namespace=lobbying_namespace, campaign_finance_namespace=campaign_finance_namespace),
            write=True, required_scope="knowledge:ownership:write")

    @mcp.tool()
    def review_platform_transparency_identity_match(namespace: str, candidate_id: str, decision: str,
                                                    reason: str) -> dict:
        """Accept or reject a platform-transparency identity candidate as an entity identity decision."""
        from src.kb.platform_transparency_identity import PlatformTransparencyIdentity

        return safe(lambda conn: PlatformTransparencyIdentity(conn).review(
            namespace, candidate_id, decision, reason, principal_id=who()[0], scopes=who()[1]),
            write=True, required_scope="knowledge:ownership:review")

    @mcp.tool()
    def revert_platform_transparency_identity_match(namespace: str, candidate_id: str, reason: str) -> dict:
        """Revert an accepted or rejected platform-transparency identity decision; records stay intact."""
        from src.kb.platform_transparency_identity import PlatformTransparencyIdentity

        return safe(lambda conn: PlatformTransparencyIdentity(conn).revert(
            namespace, candidate_id, reason, principal_id=who()[0], scopes=who()[1]),
            write=True, required_scope="knowledge:ownership:review")

    @mcp.tool()
    def link_platform_transparency_records(namespace: str, elections_namespace: str | None = None,
                                           campaign_finance_namespace: str | None = None,
                                           lobbying_namespace: str | None = None,
                                           ownership_namespace: str | None = None) -> dict:
        """Link ads to elections (declared selection or published label), campaign-finance registrations, lobbying
        register revisions and ownership entities through accepted matches; each absent provider degrades only its
        own link kind and missing targets are reported. No coordination or influence inference."""
        from src.kb.platform_transparency_links import PlatformTransparencyLinks

        return safe(lambda conn: PlatformTransparencyLinks(conn).link_all(
            namespace, principal_id=who()[0], scopes=who()[1], elections_namespace=elections_namespace,
            campaign_finance_namespace=campaign_finance_namespace, lobbying_namespace=lobbying_namespace,
            ownership_namespace=ownership_namespace), write=True, required_scope=WRITE)

    @mcp.tool()
    def create_platform_transparency_monitor(namespace: str, request_key: str, watch: str, key: str) -> dict:
        """Watch an advertiser, an election or a platform for new ads, removed ads and new DSA dump releases."""
        from src.kb.platform_transparency_monitoring import PlatformTransparencyMonitor

        return safe(lambda conn: PlatformTransparencyMonitor(conn).create(
            namespace, request_key, watch=watch, key=key, principal_id=who()[0], scopes=who()[1]),
            write=True, required_scope="knowledge:subscriptions:write")

    @mcp.tool()
    def run_platform_transparency_monitor(subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate a platform-transparency monitor at a committed watermark; notices cite the new and previous
        revision and state what changed; they are record changes, not assessments."""
        from src.kb.platform_transparency_monitoring import PlatformTransparencyMonitor

        return safe(lambda conn: PlatformTransparencyMonitor(conn).run(
            subscription_id, watermark, principal_id=who()[0], scopes=who()[1]),
            write=True, required_scope="knowledge:subscriptions:write")

    @mcp.tool()
    def poll_platform_transparency_monitor(subscription_id: str, cursor: str = "") -> dict:
        """Delivered platform-transparency monitor events after a cursor."""
        from src.kb.platform_transparency_monitoring import PlatformTransparencyMonitor

        return safe(lambda conn: PlatformTransparencyMonitor(conn, initialize=False).poll(
            subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor),
            required_scope="knowledge:subscriptions:read")
