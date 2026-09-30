"""News fact-checks entry points: fact-checks of a claim, claimant or cited article as of a date, reviewable matches,
links and monitors (#2659).

Acquisition runs through the shared source-pack tools (pack
``bounded-public-osint`` 1.2.0: ``fact-checks-google-claim-search``,
``fact-checks-datacommons-feed``, ``fact-checks-ifcn-signatories``). Google,
Data Commons and IFCN coverage are the separate ``fact-checks-google``,
``fact-checks-datacommons`` and ``fact-checks-ifcn`` features of the News
bundle; news, OSINT, claim-timeline and web-archive links degrade to an
``unavailable`` / ``missing_targets`` report when those providers are absent.
Every answer cites each fact-check revision with its source, record revision
and observation time.

Exclusions: no truth verdict by Noesis, no rating normalisation presented as
the publisher's, no automatic claim matching without review and no scraping
beyond each publisher's terms. The FC01 minimisation decision applies to every
output: review authors who are natural persons, images and job titles are never
stored; claimant queries, claimant matches and claimant monitors need
``knowledge:news:fact-checks:claimant:read``.
"""

FACT_CHECK_WRITES = {
    "propose_fact_check_matches",
    "review_fact_check_match",
    "revert_fact_check_match",
    "link_fact_checks",
    "create_fact_checks_monitor",
    "run_fact_checks_monitor",
}
FACT_CHECK_READS = {
    "fact_checks_source_contracts",
    "fact_checks_readiness",
    "fact_checks_for_claim",
    "fact_checks_for_claimant",
    "fact_checks_citing_url",
    "fact_check_history",
    "fact_checks_publisher_status",
    "export_fact_checks_evidence_bundle",
    "list_fact_check_matches",
    "list_fact_check_links",
    "poll_fact_checks_monitor",
}
FACT_CHECK_TOOLS = FACT_CHECK_WRITES | FACT_CHECK_READS
READ = "knowledge:news:fact-checks:read"
WRITE = "knowledge:news:fact-checks:write"
REVIEW = "knowledge:news:fact-checks:review"
CLAIMANT = "knowledge:news:fact-checks:claimant:read"
# Every scope each tool always reads or writes. Claimant matches in listings and identity reviews are conditional on
# the claimant scope (without it they are withheld), and archived captures on knowledge:citation:read.
FACT_CHECK_SCOPES = {
    "fact_checks_source_contracts": [],
    "fact_checks_readiness": [READ],
    "fact_checks_for_claim": [READ],
    "fact_checks_for_claimant": [READ, CLAIMANT],
    "fact_checks_citing_url": [READ],
    "fact_check_history": [READ],
    "fact_checks_publisher_status": [READ],
    "export_fact_checks_evidence_bundle": [READ],
    "list_fact_check_matches": [READ],
    "list_fact_check_links": [READ],
    "poll_fact_checks_monitor": [READ, "knowledge:subscriptions:read"],
    "propose_fact_check_matches": [READ, WRITE],
    "review_fact_check_match": [READ, REVIEW],
    "revert_fact_check_match": [READ, REVIEW],
    "link_fact_checks": [READ, WRITE],
    "create_fact_checks_monitor": [READ, "knowledge:subscriptions:write"],
    "run_fact_checks_monitor": [READ, "knowledge:subscriptions:write"],
}
EXCLUSIONS_NOTE = ("No truth verdict by Noesis, no rating normalisation presented as the publisher's, no automatic "
                   "claim matching without review, no scraping beyond each publisher's terms (FC01 minimisation).")


def required_scopes(tool_name, mutability):
    return FACT_CHECK_SCOPES.get(tool_name, [WRITE if mutability == "write" else READ])


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def queries(conn):
        from src.kb.fact_checks_queries import FactCheckQueries

        return FactCheckQueries(conn)

    @mcp.tool()
    def fact_checks_source_contracts() -> dict:
        """Per-provider endpoints, API-key handling, licences, rate limits, revision models, bounded coverage,
        LIVE_VERIFICATION status, the claimant data-minimisation decision and the exclusions for the Google Fact Check
        Tools API, the Data Commons ClaimReview feed and the IFCN signatories listing."""
        from src.ingestion.fact_checks_sources import (
            BOUNDED_COVERAGE,
            EXCLUSIONS,
            LIVE_VERIFICATION,
            MINIMISATION,
            PROVIDER_CONTRACTS,
            REVIEW_BOUNDARY,
        )

        return {"contracts": PROVIDER_CONTRACTS, "live_verification": LIVE_VERIFICATION,
                "bounded_coverage": BOUNDED_COVERAGE, "minimisation": MINIMISATION,
                "review_boundary": REVIEW_BOUNDARY, "exclusions": list(EXCLUSIONS)}

    @mcp.tool()
    def fact_checks_readiness() -> dict:
        """Whether the fact-checks-google / -datacommons / -ifcn features are selected, and records per provider."""
        from src.kb.fact_checks_records import readiness

        return safe(lambda conn: readiness(conn), required_scope=READ)

    @mcp.tool()
    def fact_checks_for_claim(namespace: str, claim_id: str | None = None, text: str | None = None,
                              as_of: str | None = None) -> dict:
        """Fact-checks of an argument claim (claim_id: accepted claim matches only; unreviewed candidates listed apart)
        or of a quoted-claim text search (text), as published by a date, with each publisher's rating verbatim and
        its scale, conflicting ratings side by side and the publisher's IFCN status at review time. No truth verdict
        by Noesis and no rating normalisation."""
        return safe(lambda conn: queries(conn).for_claim(namespace, scopes=who()[1], claim_id=claim_id, text=text,
                                                         as_of=as_of), required_scope=READ)

    @mcp.tool()
    def fact_checks_for_claimant(namespace: str, claimant: str, as_of: str | None = None) -> dict:
        """Fact-checks of a claimant as the publisher named them, or (claimant='ent-...') of every claimant accepted as
        matching that canonical entity, as published by a date with ratings verbatim. Needs the claimant scope (FC01
        minimisation). No truth verdict by Noesis."""
        return safe(lambda conn: queries(conn).for_claimant(namespace, claimant, scopes=who()[1], as_of=as_of),
                    required_scope=CLAIMANT)

    @mcp.tool()
    def fact_checks_citing_url(namespace: str, url: str | None = None, document_id: str | None = None,
                               as_of: str | None = None, archive_namespace: str | None = None) -> dict:
        """Fact-checks whose revision in effect cites a URL or a news document's URL as an appearance or the review
        itself, matched with the stated wa-canon-v1 URL rules, plus archived captures of the URL when the web-archives
        provider holds them. Each fact-check revision is cited. No verdict on the article."""
        return safe(lambda conn: queries(conn).citing(namespace, scopes=who()[1], url=url, document_id=document_id,
                                                      as_of=as_of, archive_namespace=archive_namespace),
                    required_scope=READ)

    @mcp.tool()
    def fact_check_history(namespace: str, record_key: str) -> dict:
        """Every revision of one fact-check or IFCN signatory record per source, including publisher updates, older
        observations and removals by the source (absent-from-* revisions)."""
        from src.kb.fact_checks_records import FactChecksStore

        def run(conn):
            revisions = FactChecksStore(conn, initialize=False).history(namespace, record_key, scopes=who()[1])
            return {"status": "answered" if revisions else "none_on_record", "record_key": record_key,
                    "revisions": revisions}

        return safe(run, required_scope=READ)

    @mcp.tool()
    def fact_checks_publisher_status(namespace: str, site: str, as_of: str | None = None) -> dict:
        """The IFCN signatory status label and date as published, in effect on a date, for the signatory whose website
        is the publisher site; absence from the listing is never read as a statement about the publisher."""
        from src.ingestion.fact_checks_sources import site_of

        return safe(lambda conn: queries(conn).publisher_status(namespace, site_of(site), as_of, scopes=who()[1]),
                    required_scope=READ)

    @mcp.tool()
    def export_fact_checks_evidence_bundle(namespace: str, query: str, key: str, as_of: str | None = None) -> dict:
        """An evidence bundle for a claim, claim-text, claimant or cited-URL answer (query: claim | text | claimant |
        url): each rating quoted as published, citing the fact-check revision, source and observation time."""
        def run(conn):
            ask = queries(conn)
            scopes = who()[1]
            if query == "claim":
                answer = ask.for_claim(namespace, scopes=scopes, claim_id=key, as_of=as_of)
            elif query == "text":
                answer = ask.for_claim(namespace, scopes=scopes, text=key, as_of=as_of)
            elif query == "claimant":
                answer = ask.for_claimant(namespace, key, scopes=scopes, as_of=as_of)
            elif query == "url":
                answer = ask.citing(namespace, scopes=scopes, url=key, as_of=as_of)
            else:
                return {"ok": False, "error": {"code": "invalid_request",
                                               "message": "query is claim, text, claimant or url"}}
            return {"status": answer["status"], "query": query, "key": key, "as_of": as_of,
                    "evidence_bundle": ask.evidence_bundle(answer), "exclusions": answer["exclusions"]}

        return safe(run, required_scope=READ)

    @mcp.tool()
    def list_fact_check_matches(namespace: str, kind: str | None = None, key: str | None = None) -> dict:
        """Claimant, claim and publisher match assertions with method, evidence, confidence and review state, and the
        subjects that stay unmatched. Claimant matches are listed only with the claimant scope."""
        from src.kb.fact_checks_identity import FactCheckIdentity

        def run(conn):
            identity = FactCheckIdentity(conn, initialize=False)
            return {"matches": identity.matches(namespace, scopes=who()[1], kind=kind, key=key),
                    "unmatched": identity.unmatched(namespace, scopes=who()[1])}

        return safe(run, required_scope=READ)

    @mcp.tool()
    def list_fact_check_links(namespace: str, kind: str | None = None, record_key: str | None = None,
                              target_key: str | None = None) -> dict:
        """News-article, argument-claim, OSINT-corroboration and claim-timeline links with the fact-check and target
        revisions they point at and their basis. A link carries no verdict on the linked record."""
        from src.kb.fact_checks_links import FactCheckLinks

        return safe(lambda conn: {"links": FactCheckLinks(conn, initialize=False).links(
            namespace, scopes=who()[1], kind=kind, record_key=record_key, target_key=target_key)},
            required_scope=READ)

    @mcp.tool()
    def propose_fact_check_matches(namespace: str, source_namespace: str | None = None) -> dict:
        """Propose reviewable matches: claimants to canonical entities (published identifiers before names; generic
        claimants never), fact-checked claims to argument claims (appearance URL, stored review URL, lexical overlap)
        and publishers to source identities by published domain. Nothing is merged or accepted automatically."""
        from src.kb.fact_checks_identity import FactCheckIdentity

        return safe(lambda conn: FactCheckIdentity(conn).propose(
            namespace, principal_id=who()[0], scopes=who()[1], source_namespace=source_namespace),
            write=True, required_scope=WRITE)

    @mcp.tool()
    def review_fact_check_match(namespace: str, match_id: str, decision: str, reason: str) -> dict:
        """Accept or reject a fact-check match assertion; claimant and publisher matches are also entity identity
        decisions. Claimant matches need the claimant scope."""
        from src.kb.fact_checks_identity import FactCheckIdentity

        return safe(lambda conn: FactCheckIdentity(conn).review(
            namespace, match_id, decision, reason, principal_id=who()[0], scopes=who()[1]),
            write=True, required_scope=REVIEW)

    @mcp.tool()
    def revert_fact_check_match(namespace: str, match_id: str, reason: str) -> dict:
        """Revert an accepted or rejected fact-check match; records stay intact."""
        from src.kb.fact_checks_identity import FactCheckIdentity

        return safe(lambda conn: FactCheckIdentity(conn).revert(
            namespace, match_id, reason, principal_id=who()[0], scopes=who()[1]), write=True, required_scope=REVIEW)

    @mcp.tool()
    def link_fact_checks(namespace: str, timeline_namespace: str | None = None) -> dict:
        """Link current fact-check revisions to the news articles they cite and, through accepted claim matches, to
        argument claims, OSINT corroboration results and claim timelines; missing providers and targets are reported.
        No verdict is inferred for a linked claim."""
        from src.kb.fact_checks_links import FactCheckLinks

        return safe(lambda conn: FactCheckLinks(conn).link(namespace, principal_id=who()[0], scopes=who()[1],
                                                           timeline_namespace=timeline_namespace),
                    write=True, required_scope=WRITE)

    @mcp.tool()
    def create_fact_checks_monitor(namespace: str, request_key: str, watch: str, key: str) -> dict:
        """Watch a claimant (claimant scope), a topic query or a publisher site for new or updated fact-checks and
        publisher status changes."""
        from src.kb.fact_checks_monitoring import FactChecksMonitor

        return safe(lambda conn: FactChecksMonitor(conn).create(
            namespace, request_key, watch=watch, key=key, principal_id=who()[0], scopes=who()[1]),
            write=True, required_scope="knowledge:subscriptions:write")

    @mcp.tool()
    def run_fact_checks_monitor(subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate a fact-checks monitor at a committed watermark; notices cite the new and previous revision and state
        what changed as published. Notices are record changes, not assessments."""
        from src.kb.fact_checks_monitoring import FactChecksMonitor

        return safe(lambda conn: FactChecksMonitor(conn).run(
            subscription_id, watermark, principal_id=who()[0], scopes=who()[1]),
            write=True, required_scope="knowledge:subscriptions:write")

    @mcp.tool()
    def poll_fact_checks_monitor(subscription_id: str, cursor: str = "") -> dict:
        """Delivered fact-checks monitor events after a cursor."""
        from src.kb.fact_checks_monitoring import FactChecksMonitor

        return safe(lambda conn: FactChecksMonitor(conn, initialize=False).poll(
            subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor),
            required_scope="knowledge:subscriptions:read")
