"""News fact-checks entry points: published fact-checks with ratings as published, publisher status, reviewable
identity, cross-pack links, evidence bundles and monitors (#2659).

Acquisition runs through the shared source-pack tools (pack
``bounded-public-osint`` 1.2.0: ``news-fact-checks-google``,
``news-fact-checks-datacommons``, ``news-fact-checks-ifcn``). Google Fact Check
Tools coverage is the News bundle's ``fact-checks-google`` feature, Data Commons
ClaimReview ``fact-checks-datacommons`` and IFCN signatory status
``fact-checks-ifcn``; news, OSINT and claim links degrade to an ``unavailable`` /
``missing_targets`` report when those providers are absent. Every answer cites
each fact-check revision with its source, record revision and as-of time.

Exclusions: no truth verdicts by Noesis, no rating normalisation presented as the
publisher's, no automatic claim matching without review, no scraping beyond each
publisher's terms. The FC01 minimisation decision applies to every output: tools
read minimised records only, so no claimant image, job title, contact detail or
social profile, no review author and no social-platform appearance URL can be
returned.
"""

FACT_CHECK_WRITES = {
    "propose_fact_check_identity_matches",
    "review_fact_check_identity_match",
    "revert_fact_check_identity_match",
    "link_fact_checks",
    "create_fact_check_monitor",
    "run_fact_check_monitor",
}
FACT_CHECK_READS = {
    "fact_check_source_contracts",
    "fact_check_readiness",
    "fact_checks_of_claim_or_claimant",
    "fact_checks_citing_article",
    "fact_check_revision_history",
    "export_fact_check_evidence_bundle",
    "list_fact_check_identity_candidates",
    "list_fact_check_links",
    "poll_fact_check_monitor",
}
FACT_CHECK_TOOLS = FACT_CHECK_WRITES | FACT_CHECK_READS
READ = "knowledge:news:fact-checks:read"
WRITE = "knowledge:news:fact-checks:write"
REVIEW = "knowledge:news:fact-checks:review"
# Every scope each tool always reads or writes. Archived captures in fact_checks_citing_article are read only with
# knowledge:citation:read (otherwise reported unavailable); source-identity links read the source identity store.
FACT_CHECK_SCOPES = {
    "fact_check_source_contracts": [],
    "fact_check_readiness": [READ],
    "fact_checks_of_claim_or_claimant": [READ],
    "fact_checks_citing_article": [READ],
    "fact_check_revision_history": [READ],
    "export_fact_check_evidence_bundle": [READ],
    "list_fact_check_identity_candidates": [READ],
    "list_fact_check_links": [READ],
    "poll_fact_check_monitor": [READ, "knowledge:subscriptions:read"],
    "propose_fact_check_identity_matches": [READ, WRITE],
    "review_fact_check_identity_match": [READ, REVIEW],
    "revert_fact_check_identity_match": [READ, REVIEW],
    "link_fact_checks": [READ, WRITE],
    "create_fact_check_monitor": [READ, "knowledge:subscriptions:write"],
    "run_fact_check_monitor": [READ, "knowledge:subscriptions:write"],
}
EXCLUSIONS_NOTE = ("No truth verdict by Noesis, no rating normalisation presented as the publisher's, no automatic "
                   "claim matching without review; claimants minimised (FC01).")


def required_scopes(tool_name, mutability):
    return FACT_CHECK_SCOPES.get(tool_name, [WRITE if mutability == "write" else READ])


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def queries(conn):
        from src.kb.fact_checks_queries import FactCheckQueries

        return FactCheckQueries(conn)

    @mcp.tool()
    def fact_check_source_contracts() -> dict:
        """Per-provider endpoints, API-key handling, licences and terms (not re-verified live), rate limits, revision
        and removal models, bounded coverage, LIVE_VERIFICATION status and the data-minimisation decision for the
        Google Fact Check Tools API, the Data Commons ClaimReview feed and the IFCN signatory list."""
        from src.ingestion.fact_checks_sources import (
            BOUNDED_COVERAGE,
            EXCLUSIONS,
            LIVE_VERIFICATION,
            MINIMISATION,
            PROVIDER_CONTRACTS,
            REVIEW_BOUNDARY,
        )

        return {"contracts": PROVIDER_CONTRACTS, "live_verification": LIVE_VERIFICATION,
                "bounded_coverage": BOUNDED_COVERAGE, "minimisation": MINIMISATION, "review_boundary": REVIEW_BOUNDARY,
                "exclusions": list(EXCLUSIONS)}

    @mcp.tool()
    def fact_check_readiness() -> dict:
        """Whether the fact-checks-google / -datacommons / -ifcn features are selected, and records per provider."""
        from src.kb.fact_checks_records import readiness

        return safe(lambda conn: readiness(conn), required_scope=READ)

    @mcp.tool()
    def fact_checks_of_claim_or_claimant(namespace: str, claim_id: str | None = None, claimant: str | None = None,
                                         as_of: str | None = None, known_at: str | None = None) -> dict:
        """Fact-checks published by a date of an argument claim (claim_id, through accepted claim matches only) or a
        claimant (a canonical entity id through accepted matches, or the name exactly as published), with each
        publisher's rating verbatim and its scale, differing ratings side by side, and the publisher's IFCN status at
        the review date; known_at limits the answer to what was acquired by then. Each fact-check revision is cited.
        No truth verdict, no rating normalisation."""
        return safe(lambda conn: queries(conn).for_claim_or_claimant(
            namespace, scopes=who()[1], claim_id=claim_id, claimant=claimant, as_of=as_of, known_at=known_at),
            required_scope=READ)

    @mcp.tool()
    def fact_checks_citing_article(namespace: str, url: str | None = None, document_id: str | None = None,
                                   as_of: str | None = None, known_at: str | None = None) -> dict:
        """Fact-checks that cite a news article (document_id) or URL as an appearance, matched under the stated
        wa-canon-v1 URL rules (social-platform appearances only by digest), with archived captures near each review
        date when the web-archive store is present. Ratings verbatim; each fact-check revision is cited."""
        return safe(lambda conn: queries(conn).citing(namespace, scopes=who()[1], url=url, document_id=document_id,
                                                      as_of=as_of, known_at=known_at), required_scope=READ)

    @mcp.tool()
    def fact_check_revision_history(namespace: str, record_key: str) -> dict:
        """Every revision of one fact-check or publisher record per source: publisher updates, rating changes, IFCN
        status changes and absences from a later release or listing (removals are revisions, never deletions)."""
        from src.kb.fact_checks_records import FactCheckStore

        def run(conn):
            history = FactCheckStore(conn, initialize=False).history(namespace, record_key, scopes=who()[1])
            return {"status": "answered" if history else "none_on_record", "record_key": record_key,
                    "revisions": [{"source_id": v["source_id"], "revision_no": v["revision_no"],
                                   "change": v["change"], "status": v["status"], "effective_on": v["effective_on"],
                                   "record": v["record"], "citation": v["citation"]} for v in history]}

        return safe(run, required_scope=READ)

    @mcp.tool()
    def export_fact_check_evidence_bundle(namespace: str, query: str, key: str, as_of: str | None = None) -> dict:
        """An evidence bundle for a claim, claimant or citing-article answer (query: claim | claimant | citing): each
        assertion states a publisher's rating verbatim and cites the fact-check revision (source, record revision and
        as-of time) and the IFCN status revision behind it."""
        def run(conn):
            ask = queries(conn)
            if query == "claim":
                answer = ask.for_claim_or_claimant(namespace, scopes=who()[1], claim_id=key, as_of=as_of)
            elif query == "claimant":
                answer = ask.for_claim_or_claimant(namespace, scopes=who()[1], claimant=key, as_of=as_of)
            elif query == "citing":
                answer = ask.citing(namespace, scopes=who()[1], url=key, as_of=as_of)
            else:
                return {"ok": False, "error": {"code": "invalid_request",
                                               "message": "query is claim, claimant or citing"}}
            return {"status": answer["status"], "query": query, "key": key, "as_of": as_of,
                    "evidence_bundle": ask.evidence_bundle(answer), "exclusions": answer["exclusions"]}

        return safe(run, required_scope=READ)

    @mcp.tool()
    def list_fact_check_identity_candidates(namespace: str, match_kind: str | None = None, key: str | None = None
                                            ) -> dict:
        """Claimant, claim and publisher identity candidates and decisions with method, evidence and confidence, and
        the subjects that stay unmatched. Claimant accounts are never candidates."""
        from src.kb.fact_checks_identity import FactCheckIdentity

        def run(conn):
            identity = FactCheckIdentity(conn, initialize=False)
            return {"candidates": identity.candidates(namespace, scopes=who()[1], match_kind=match_kind, key=key),
                    "unmatched": identity.unmatched(namespace, scopes=who()[1])}

        return safe(run, required_scope=READ)

    @mcp.tool()
    def list_fact_check_links(namespace: str, kind: str | None = None, record_key: str | None = None,
                              target_key: str | None = None) -> dict:
        """News-article, claim-timeline, OSINT-corroboration and source-identity links with the fact-check and target
        revisions they point at and their basis. A link is not a verdict on the linked claim."""
        from src.kb.fact_checks_links import FactCheckLinks

        return safe(lambda conn: {"links": FactCheckLinks(conn, initialize=False).links(
            namespace, scopes=who()[1], kind=kind, record_key=record_key, target_key=target_key)},
            required_scope=READ)

    @mcp.tool()
    def propose_fact_check_identity_matches(namespace: str, source_identity_namespace: str | None = None) -> dict:
        """Propose reviewable claimant (published identifier, then name as published), claim (shared appearance URL,
        quoted-text overlap) and publisher (published domain) matches; nothing is merged or accepted automatically and
        absent owners are reported."""
        from src.kb.fact_checks_identity import FactCheckIdentity

        return safe(lambda conn: FactCheckIdentity(conn).propose(
            namespace, principal_id=who()[0], scopes=who()[1], source_identity_namespace=source_identity_namespace),
            write=True, required_scope=WRITE)

    @mcp.tool()
    def review_fact_check_identity_match(namespace: str, candidate_id: str, decision: str, reason: str) -> dict:
        """Accept or reject a fact-check identity candidate as an entity identity decision."""
        from src.kb.fact_checks_identity import FactCheckIdentity

        return safe(lambda conn: FactCheckIdentity(conn).review(
            namespace, candidate_id, decision, reason, principal_id=who()[0], scopes=who()[1]),
            write=True, required_scope=REVIEW)

    @mcp.tool()
    def revert_fact_check_identity_match(namespace: str, candidate_id: str, reason: str) -> dict:
        """Revert an accepted or rejected fact-check identity decision; records stay intact."""
        from src.kb.fact_checks_identity import FactCheckIdentity

        return safe(lambda conn: FactCheckIdentity(conn).revert(
            namespace, candidate_id, reason, principal_id=who()[0], scopes=who()[1]),
            write=True, required_scope=REVIEW)

    @mcp.tool()
    def link_fact_checks(namespace: str, timeline_namespace: str | None = None,
                         source_identity_namespace: str | None = None) -> dict:
        """Link current fact-check revisions to the news articles they cite and, through accepted matches only, to
        claim timelines, OSINT corroboration and source identities; missing targets and absent providers are
        reported. No inferred verdicts."""
        from src.kb.fact_checks_links import FactCheckLinks

        return safe(lambda conn: FactCheckLinks(conn).link(
            namespace, principal_id=who()[0], scopes=who()[1], timeline_namespace=timeline_namespace,
            source_identity_namespace=source_identity_namespace), write=True, required_scope=WRITE)

    @mcp.tool()
    def create_fact_check_monitor(namespace: str, request_key: str, watch: str, key: str) -> dict:
        """Watch a claimant (canonical entity id or name as published), a topic query (words in the claim as quoted)
        or a publisher (website domain) for new or updated fact-checks and IFCN status changes."""
        from src.kb.fact_checks_monitoring import FactCheckMonitor

        return safe(lambda conn: FactCheckMonitor(conn).create(
            namespace, request_key, watch=watch, key=key, principal_id=who()[0], scopes=who()[1]),
            write=True, required_scope="knowledge:subscriptions:write")

    @mcp.tool()
    def run_fact_check_monitor(subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate a fact-checks monitor at a committed watermark; notices cite the new and previous revision and
        state what changed (ratings verbatim); they are record changes, not assessments."""
        from src.kb.fact_checks_monitoring import FactCheckMonitor

        return safe(lambda conn: FactCheckMonitor(conn).run(
            subscription_id, watermark, principal_id=who()[0], scopes=who()[1]),
            write=True, required_scope="knowledge:subscriptions:write")

    @mcp.tool()
    def poll_fact_check_monitor(subscription_id: str, cursor: str = "") -> dict:
        """Delivered fact-checks monitor events after a cursor."""
        from src.kb.fact_checks_monitoring import FactCheckMonitor

        return safe(lambda conn: FactCheckMonitor(conn, initialize=False).poll(
            subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor),
            required_scope="knowledge:subscriptions:read")
