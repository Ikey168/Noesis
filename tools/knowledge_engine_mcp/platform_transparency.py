"""OSINT platform-transparency feature entry points: DSA statements of reasons, Meta and Google political ads,
reviewable advertiser identity, links, monitors and evidence bundles (#2580).

Acquisition runs through the shared source-pack tools (pack
``bounded-public-osint`` 1.2.0: ``platform-transparency-dsa-sor``,
``platform-transparency-meta-ads``, ``platform-transparency-google-political-ads``).
DSA, Meta, Google and Lumen coverage are the separate optional features
``platform-transparency-dsa``, ``platform-transparency-meta``,
``platform-transparency-google`` and ``platform-transparency-lumen`` (Lumen is
recorded as not implemented); elections, campaign-finance and lobbying links
degrade to an ``unavailable`` / ``missing_targets`` report when those providers
are absent. Every answer cites each item with its source, record revision and
as-of time.

Exclusions: no user-level profiling, no collection of private content, no
inference of coordinated behaviour and no conversion of spend or impression
ranges into point estimates. The SP01 minimisation decision applies to every
output: a response carrying a withheld field or a point-estimate key is refused
(``minimisation_violation``) instead of being returned.
"""

PLATFORM_TRANSPARENCY_WRITES = {
    "propose_platform_transparency_identity_matches",
    "review_platform_transparency_identity_match",
    "revert_platform_transparency_identity_match",
    "register_platform_transparency_platforms",
    "link_platform_transparency_elections",
    "link_platform_transparency_campaign_finance",
    "link_platform_transparency_lobbying",
    "create_platform_transparency_monitor",
    "run_platform_transparency_monitor",
}
PLATFORM_TRANSPARENCY_READS = {
    "platform_transparency_source_contracts",
    "platform_transparency_readiness",
    "platform_transparency_ads_by_advertiser",
    "platform_transparency_ads_for_election",
    "platform_transparency_moderation_statements",
    "platform_transparency_record_history",
    "export_platform_transparency_evidence_bundle",
    "list_platform_transparency_identity_candidates",
    "list_platform_transparency_links",
    "poll_platform_transparency_monitor",
}
PLATFORM_TRANSPARENCY_TOOLS = PLATFORM_TRANSPARENCY_WRITES | PLATFORM_TRANSPARENCY_READS
READ = "knowledge:osint:platform-transparency:read"
WRITE = "knowledge:osint:platform-transparency:write"
OWNERSHIP_READ = "knowledge:ownership:read"
# Every scope each tool always reads or writes: platform-transparency records, the ownership identity state machine
# (answers show identity state; links rest on accepted decisions), other packs' records and subscriptions.
PLATFORM_TRANSPARENCY_SCOPES = {
    "platform_transparency_source_contracts": [],
    "platform_transparency_readiness": [READ],
    "platform_transparency_ads_by_advertiser": [READ, OWNERSHIP_READ],
    "platform_transparency_ads_for_election": [READ],
    "platform_transparency_moderation_statements": [READ],
    "platform_transparency_record_history": [READ],
    "export_platform_transparency_evidence_bundle": [READ, OWNERSHIP_READ],
    "list_platform_transparency_identity_candidates": [READ, OWNERSHIP_READ],
    "list_platform_transparency_links": [READ],
    "poll_platform_transparency_monitor": [READ, "knowledge:subscriptions:read"],
    "propose_platform_transparency_identity_matches": [READ, OWNERSHIP_READ, "knowledge:ownership:write"],
    "review_platform_transparency_identity_match": [OWNERSHIP_READ, "knowledge:ownership:review"],
    "revert_platform_transparency_identity_match": [OWNERSHIP_READ, "knowledge:ownership:review"],
    "register_platform_transparency_platforms": [READ, "knowledge:source-identity:write"],
    "link_platform_transparency_elections": [READ, WRITE, OWNERSHIP_READ],
    "link_platform_transparency_campaign_finance": [READ, WRITE, OWNERSHIP_READ,
                                                    "knowledge:political:campaign-finance:read"],
    "link_platform_transparency_lobbying": [READ, WRITE, OWNERSHIP_READ, "knowledge:political:lobbying:read"],
    "create_platform_transparency_monitor": [READ, "knowledge:subscriptions:write"],
    "run_platform_transparency_monitor": [READ, OWNERSHIP_READ, "knowledge:subscriptions:write"],
}
EXCLUSIONS_NOTE = ("No user-level profiling, no private content, no coordination inference and no point estimate "
                   "from spend or impression ranges; the SP01 minimisation decision applies to every output.")


def required_scopes(tool_name, mutability):
    return PLATFORM_TRANSPARENCY_SCOPES.get(tool_name, [WRITE if mutability == "write" else READ])


def guarded(result):
    """Refuse an output that would carry a withheld field or a point-estimate key (SP01), whatever produced it."""
    from src.ingestion.platform_transparency_sources import WITHHELD_KEYS
    from src.kb.platform_transparency_records import (
        PlatformTransparencyError,
        forbidden_keys,
    )

    found = forbidden_keys(result)

    def walk(value, path):
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).casefold() in WITHHELD_KEYS and item not in (None, "", [], {}):
                    found.append(f"{path}.{key}")
                walk(item, f"{path}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")

    walk(result, "$")
    if found:
        raise PlatformTransparencyError("minimisation_violation", "the answer would carry withheld or "
                                        "point-estimate fields (SP01)", paths=sorted(set(found)))
    return result


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def queries(conn):
        from src.kb.platform_transparency_queries import PlatformTransparencyQueries

        return PlatformTransparencyQueries(conn)

    @mcp.tool()
    def platform_transparency_source_contracts() -> dict:
        """Per-provider endpoints, token handling, licences, rate limits, revision and removal models, bounded
        coverage, LIVE_VERIFICATION status and the SP01 data-minimisation decision for the DSA Transparency
        Database, the Meta Ad Library API, Google political ads and Lumen (recorded as not implemented)."""
        from src.ingestion.platform_transparency_sources import source_contracts

        return {**source_contracts(), "exclusions_note": EXCLUSIONS_NOTE}

    @mcp.tool()
    def platform_transparency_readiness() -> dict:
        """Which platform-transparency features (DSA, Meta, Google, Lumen) are selected, and records per provider."""
        from src.kb.platform_transparency_records import readiness

        return safe(lambda conn: readiness(conn), required_scope=READ)

    @mcp.tool()
    def platform_transparency_ads_by_advertiser(namespace: str, advertiser: str, as_of: str | None = None) -> dict:
        """Political ads of an advertiser or funding entity (Meta page id, Google advertiser id, declared name, or an
        FEC committee / campaign-finance / elections / lobbying / ownership record reached through accepted identity
        decisions) with delivery dates and spend and impression ranges exactly as published, each ad revision cited;
        removed ads carry their removal revision. No midpoint, sum or point estimate; no user-level profiling."""
        return safe(lambda conn: guarded(queries(conn).ads_by_advertiser(namespace, advertiser, scopes=who()[1],
                                                                         as_of=as_of)), required_scope=READ)

    @mcp.tool()
    def platform_transparency_ads_for_election(namespace: str, election_id: str, as_of: str | None = None) -> dict:
        """Political ads linked to an election through accepted identity decisions, grouped by advertiser, with each
        link's basis and the delivery dates relative to election day as published; ranges as published. No
        coordination or influence reading."""
        return safe(lambda conn: guarded(queries(conn).ads_for_election(namespace, election_id, scopes=who()[1],
                                                                        as_of=as_of)), required_scope=READ)

    @mcp.tool()
    def platform_transparency_moderation_statements(namespace: str, platform: str, start: str, end: str,
                                                    ground: str | None = None, as_of: str | None = None,
                                                    include_statements: bool = False) -> dict:
        """Counts of stored DSA statements of reasons for a platform and period by decision type, decision ground
        and category, with automated-detection and automated-decision values as published; states the stored
        window, the days without a stored dump and cites the dump versions used. No user identifiers."""
        return safe(lambda conn: guarded(queries(conn).moderation_statements(
            namespace, platform, scopes=who()[1], start=start, end=end, ground=ground, as_of=as_of,
            include_statements=include_statements)), required_scope=READ)

    @mcp.tool()
    def platform_transparency_record_history(namespace: str, record_key: str, as_of: str | None = None) -> dict:
        """Every revision of one platform-transparency record (removals and corrections are revisions), and the
        revision current as of a time."""
        from src.kb.platform_transparency_records import PlatformTransparencyStore, cite

        def run(conn):
            store = PlatformTransparencyStore(conn, initialize=False)
            history = store.history(namespace, record_key, scopes=who()[1])
            current = store.as_of(namespace, record_key, as_of, scopes=who()[1]) if history else None
            return guarded({"status": "answered" if history else "none_on_record", "record_key": record_key,
                            "revisions": [{"revision_id": h["revision_id"], "revision_no": h["revision_no"],
                                           "change": h["change"], "record": h["record"], "citation": cite(h)}
                                          for h in history],
                            "as_of": as_of, "current_as_of": current["revision_id"] if current else None})

        return safe(run, required_scope=READ)

    @mcp.tool()
    def export_platform_transparency_evidence_bundle(namespace: str, query: str, key: str,
                                                     as_of: str | None = None, start: str | None = None,
                                                     end: str | None = None) -> dict:
        """An evidence bundle for an advertiser, election or moderation answer (query: advertiser | election |
        moderation; key: advertiser, election id or platform; start/end for moderation): assertions each citing the
        record revision, source and as-of time behind them."""
        def run(conn):
            ask = queries(conn)
            if query == "advertiser":
                answer = ask.ads_by_advertiser(namespace, key, scopes=who()[1], as_of=as_of)
            elif query == "election":
                answer = ask.ads_for_election(namespace, key, scopes=who()[1], as_of=as_of)
            elif query == "moderation" and start and end:
                answer = ask.moderation_statements(namespace, key, scopes=who()[1], start=start, end=end, as_of=as_of,
                                                   include_statements=True)
            else:
                return {"ok": False, "error": {"code": "invalid_request",
                                               "message": "query is advertiser, election or moderation (with start "
                                                          "and end)"}}
            return guarded({"status": answer["status"], "query": query, "key": key, "as_of": as_of,
                            "evidence_bundle": ask.evidence_bundle(answer), "exclusions": answer["exclusions"]})

        return safe(run, required_scope=READ)

    @mcp.tool()
    def list_platform_transparency_identity_candidates(namespace: str, record_key: str | None = None) -> dict:
        """Identity candidates and decisions for advertisers and funding entities, with method, evidence and
        confidence, and the subjects still unmatched. Natural persons are never candidates."""
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
        """Election, campaign-finance and lobbying links with the ad and target revisions they point at and their
        basis. A link is not evidence of coordination or influence."""
        from src.kb.platform_transparency_links import PlatformTransparencyLinks

        return safe(lambda conn: {"links": PlatformTransparencyLinks(conn, initialize=False).links(
            namespace, scopes=who()[1], kind=kind, record_key=record_key, target_key=target_key)},
            required_scope=READ)

    @mcp.tool()
    def propose_platform_transparency_identity_matches(namespace: str, ownership_namespace: str | None = None,
                                                       campaign_finance_namespace: str | None = None,
                                                       lobbying_namespace: str | None = None,
                                                       elections_namespace: str | None = None) -> dict:
        """Propose reviewable matches of advertisers and funding entities (published FEC ids first, then names in
        one country) to campaign-finance committees, party lists, lobbying registrants and Corporate Ownership
        entities; nothing is merged or accepted automatically, persons are never targets and absent providers are
        reported."""
        from src.kb.platform_transparency_identity import PlatformTransparencyIdentity

        return safe(lambda conn: PlatformTransparencyIdentity(conn).propose(
            namespace, principal_id=who()[0], scopes=who()[1], ownership_namespace=ownership_namespace,
            campaign_finance_namespace=campaign_finance_namespace, lobbying_namespace=lobbying_namespace,
            elections_namespace=elections_namespace), write=True, required_scope="knowledge:ownership:write")

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
    def register_platform_transparency_platforms(namespace: str) -> dict:
        """Register each platform on record as a source identity (idempotent); platforms are never identity
        candidates."""
        from src.kb.platform_transparency_identity import PlatformTransparencyIdentity

        return safe(lambda conn: {"platforms": PlatformTransparencyIdentity(conn).register_platforms(
            namespace, principal_id=who()[0], scopes=who()[1])}, write=True,
            required_scope="knowledge:source-identity:write")

    @mcp.tool()
    def link_platform_transparency_elections(namespace: str, elections_namespace: str | None = None,
                                             campaign_finance_namespace: str | None = None) -> dict:
        """Link ads to elections through accepted party-list matches or accepted committee matches with a cited
        campaign-finance contest link; degrades when the elections feature is absent and reports missing targets."""
        from src.kb.platform_transparency_links import PlatformTransparencyLinks

        return safe(lambda conn: PlatformTransparencyLinks(conn).link_elections(
            namespace, principal_id=who()[0], scopes=who()[1], elections_namespace=elections_namespace,
            campaign_finance_namespace=campaign_finance_namespace), write=True, required_scope=WRITE)

    @mcp.tool()
    def link_platform_transparency_campaign_finance(namespace: str,
                                                    campaign_finance_namespace: str | None = None) -> dict:
        """Link ads to the filing versions of committees their advertiser was accepted as (published FEC id or a
        reviewed name match); degrades when the campaign-finance feature is absent."""
        from src.kb.platform_transparency_links import PlatformTransparencyLinks

        return safe(lambda conn: PlatformTransparencyLinks(conn).link_campaign_finance(
            namespace, principal_id=who()[0], scopes=who()[1], campaign_finance_namespace=campaign_finance_namespace),
            write=True, required_scope=WRITE)

    @mcp.tool()
    def link_platform_transparency_lobbying(namespace: str, lobbying_namespace: str | None = None) -> dict:
        """Link ads to the lobbying register revision in force through accepted matches; degrades when the lobbying
        feature is absent. No influence or coordination inference."""
        from src.kb.platform_transparency_links import PlatformTransparencyLinks

        return safe(lambda conn: PlatformTransparencyLinks(conn).link_lobbying(
            namespace, principal_id=who()[0], scopes=who()[1], lobbying_namespace=lobbying_namespace),
            write=True, required_scope=WRITE)

    @mcp.tool()
    def create_platform_transparency_monitor(namespace: str, request_key: str, watch: str, key: str) -> dict:
        """Watch an advertiser, an election or a platform for new ads, removed ads, revised ranges and new DSA dump
        releases."""
        from src.kb.platform_transparency_monitoring import PlatformTransparencyMonitor

        return safe(lambda conn: PlatformTransparencyMonitor(conn).create(
            namespace, request_key, watch=watch, key=key, principal_id=who()[0], scopes=who()[1]),
            write=True, required_scope="knowledge:subscriptions:write")

    @mcp.tool()
    def run_platform_transparency_monitor(subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate a platform-transparency monitor at a committed watermark; notices cite the new and previous
        revision and state what changed."""
        from src.kb.platform_transparency_monitoring import PlatformTransparencyMonitor

        return safe(lambda conn: guarded(PlatformTransparencyMonitor(conn).run(
            subscription_id, watermark, principal_id=who()[0], scopes=who()[1])),
            write=True, required_scope="knowledge:subscriptions:write")

    @mcp.tool()
    def poll_platform_transparency_monitor(subscription_id: str, cursor: str = "") -> dict:
        """Delivered platform-transparency monitor events after a cursor."""
        from src.kb.platform_transparency_monitoring import PlatformTransparencyMonitor

        return safe(lambda conn: PlatformTransparencyMonitor(conn, initialize=False).poll(
            subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor),
            required_scope="knowledge:subscriptions:read")
