"""Political legislation feature entry points: US and UK bill dossiers as of a date, votes, sponsors, review, links.

Acquisition runs through the shared source-pack tools (pack
``official-political-records`` 1.3.0: ``us-congress-gov-bills``,
``us-congress-gov-house-votes``, ``us-senate-roll-calls``, ``us-govinfo-bills``,
``us-govinfo-billstatus``, ``uk-parliament-bills``, ``uk-commons-divisions``,
``uk-lords-divisions``, ``uk-hansard-debates``). Bills are dossiers in the
existing legislative dossier store. Every answer cites the committed document
revision, provider and observation time behind each part. US coverage is the
``legislation-us`` feature and UK coverage ``legislation-uk``; lobbying and
Legal links degrade to an ``unavailable`` note when those providers are absent.

Exclusions: no passage prediction, no member scoring or ideology rating, and
no summary presented as a bill's legal effect.
"""

LEGISLATION_WRITES = {
    "build_bill_dossier",
    "review_bill_record_link",
    "revert_bill_record_link",
    "propose_legislation_identity_matches",
    "review_legislation_identity_match",
    "revert_legislation_identity_match",
    "link_bill_lobbying",
    "link_bill_enactment",
    "create_legislation_monitor",
    "run_legislation_monitor",
}
LEGISLATION_READS = {
    "legislation_source_contracts",
    "legislation_readiness",
    "bill_dossier_as_of",
    "list_bill_votes",
    "lookup_sponsor_bills",
    "list_bill_records",
    "list_bill_link_candidates",
    "export_bill_evidence_bundle",
    "list_legislation_identity_candidates",
    "poll_legislation_monitor",
}
LEGISLATION_TOOLS = LEGISLATION_WRITES | LEGISLATION_READS
READ = "knowledge:political:legislation:read"
WRITE = "knowledge:political:legislation:write"
REVIEW = "knowledge:political:legislation:review"
DOSSIER_READ = "knowledge:political:dossier:read"
DOSSIER_WRITE = "knowledge:political:dossier:write"
OWNERSHIP_READ = "knowledge:ownership:read"
# Every scope each tool always reads or writes: legislation records, dossiers, the ownership identity state
# machine (identity views are part of vote and sponsor answers), lobbying links, Legal works and subscriptions.
LEGISLATION_SCOPES = {
    "legislation_source_contracts": [],
    "legislation_readiness": [READ],
    "bill_dossier_as_of": [READ, DOSSIER_READ, OWNERSHIP_READ],
    "list_bill_votes": [READ, DOSSIER_READ, OWNERSHIP_READ],
    "lookup_sponsor_bills": [READ, OWNERSHIP_READ],
    "list_bill_records": [READ],
    "list_bill_link_candidates": [READ],
    "export_bill_evidence_bundle": [READ, DOSSIER_READ, OWNERSHIP_READ],
    "list_legislation_identity_candidates": [READ, OWNERSHIP_READ],
    "poll_legislation_monitor": [READ, "knowledge:subscriptions:read"],
    "build_bill_dossier": [READ, DOSSIER_READ, DOSSIER_WRITE],
    "review_bill_record_link": [READ, REVIEW],
    "revert_bill_record_link": [REVIEW],
    "propose_legislation_identity_matches": [READ, OWNERSHIP_READ, "knowledge:ownership:write"],
    "review_legislation_identity_match": [OWNERSHIP_READ, "knowledge:ownership:review"],
    "revert_legislation_identity_match": [OWNERSHIP_READ, "knowledge:ownership:review"],
    "link_bill_lobbying": [READ, DOSSIER_READ, "knowledge:political:lobbying:read",
                           "knowledge:political:lobbying:write"],
    "link_bill_enactment": [READ, WRITE, DOSSIER_READ, "knowledge:legal:read", "knowledge:legal:write"],
    "create_legislation_monitor": [READ, "knowledge:subscriptions:write"],
    "run_legislation_monitor": [READ, "knowledge:subscriptions:write"],
}


def required_scopes(tool_name, mutability):
    return LEGISLATION_SCOPES.get(tool_name, [WRITE if mutability == "write" else READ])


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def queries(conn):
        from src.domains.political.legislation_queries import LegislationQueries

        return LegislationQueries(conn)

    @mcp.tool()
    def legislation_source_contracts() -> dict:
        """Per-provider endpoints, API-key handling, licences and attribution, rate limits, revision models, bounded
        coverage and LIVE_VERIFICATION status for congress.gov, senate.gov, GovInfo and the UK Parliament APIs."""
        from src.ingestion.legislation_sources import (
            BOUNDED_COVERAGE,
            LIVE_VERIFICATION,
            PROVIDER_CONTRACTS,
            REVIEW_BOUNDARY,
        )

        return {"contracts": PROVIDER_CONTRACTS, "live_verification": LIVE_VERIFICATION,
                "bounded_coverage": BOUNDED_COVERAGE, "review_boundary": REVIEW_BOUNDARY}

    @mcp.tool()
    def legislation_readiness() -> dict:
        """Whether the legislation-us / legislation-uk features are selected, and acquired records per provider."""
        from src.kb.legislation import readiness

        return safe(lambda conn: readiness(conn), required_scope=READ)

    @mcp.tool()
    def bill_dossier_as_of(namespace: str, bill_key: str, dossier_namespace: str, as_of: str | None = None,
                           revision: int | None = None, lobbying_namespace: str | None = None) -> dict:
        """A US or UK bill's stage and text version in force on a date, its sponsors, how members voted in each
        recorded vote, source disagreements, linked lobbying disclosures and enactment links, every part cited to
        the record revision used. A bill with no record is none_on_record. No passage prediction, member score or
        legal-effect summary. Conditional scope: lobbying_namespace also needs knowledge:political:lobbying:read."""
        return safe(lambda conn: queries(conn).bill_as_of(
            namespace, bill_key, dossier_namespace, principal_id=who()[0], scopes=who()[1], as_of=as_of,
            revision=revision, lobbying_namespace=lobbying_namespace), required_scope=READ)

    @mcp.tool()
    def list_bill_votes(namespace: str, bill_key: str, dossier_namespace: str, as_of: str | None = None) -> dict:
        """Roll calls and divisions on a bill held on or before a date: member positions as published, accepted
        identity matches only, and divisions still awaiting review listed as unlinked candidates. No member scoring."""
        def run(conn):
            answer = queries(conn).bill_as_of(namespace, bill_key, dossier_namespace, principal_id=who()[0],
                                              scopes=who()[1], as_of=as_of)
            return {"status": answer["status"], "bill_key": bill_key, "as_of": answer["as_of"],
                    "votes": answer.get("votes"), "exclusions": answer["exclusions"]}

        return safe(run, required_scope=READ)

    @mcp.tool()
    def lookup_sponsor_bills(namespace: str, member_key: str) -> dict:
        """Bills a member (bioguide, Senate LIS or UK Parliament member key) sponsored or cosponsored, per record,
        with party and place as each record stated them and the member's reviewed identity state."""
        return safe(lambda conn: queries(conn).sponsor_bills(namespace, member_key, scopes=who()[1]),
                    required_scope=READ)

    @mcp.tool()
    def list_bill_records(namespace: str, bill_key: str) -> dict:
        """Acquired records for a bill (linked by the source) and candidate records (declared for it), with the
        committed revision, provider revision stamp and evidence origin of each."""
        from src.kb.legislation import LegislationStore

        def run(conn):
            found = LegislationStore(conn, initialize=False).records_for_bill(namespace, bill_key, scopes=who()[1])
            return {"bill_key": bill_key, **{k: [{key: r[key] for key in r if key != "record"} for r in v]
                                            for k, v in found.items()}}

        return safe(run, required_scope=READ)

    @mcp.tool()
    def list_bill_link_candidates(namespace: str, bill_key: str) -> dict:
        """Divisions and Hansard references declared for a bill whose source names no bill, with review state."""
        from src.kb.legislation import LegislationStore

        return safe(lambda conn: {"candidates": LegislationStore(conn, initialize=False).link_candidates(
            namespace, bill_key, scopes=who()[1])}, required_scope=READ)

    @mcp.tool()
    def export_bill_evidence_bundle(namespace: str, bill_key: str, dossier_namespace: str,
                                    as_of: str | None = None) -> dict:
        """An evidence bundle for a bill as of a date: assertions each depending on a committed document revision,
        with a bibliography naming provider, revision stamp, observation time and evidence origin."""
        def run(conn):
            answer = queries(conn).bill_as_of(namespace, bill_key, dossier_namespace, principal_id=who()[0],
                                              scopes=who()[1], as_of=as_of)
            return {"status": answer["status"], "bill_key": bill_key, "as_of": answer["as_of"],
                    "evidence_bundle": answer.get("evidence_bundle"), "exclusions": answer["exclusions"]}

        return safe(run, required_scope=READ)

    @mcp.tool()
    def build_bill_dossier(namespace: str, bill_key: str, dossier_namespace: str) -> dict:
        """Save the bill's dossier revision in the existing legislative dossier store from the current record
        revisions (idempotent); records whose source names no bill stay review candidates unless accepted."""
        from src.kb.legislation import LegislationDossiers

        return safe(lambda conn: LegislationDossiers(conn).build(
            namespace, bill_key, dossier_namespace, principal_id=who()[0], scopes=who()[1]),
            write=True, required_scope=DOSSIER_WRITE)

    @mcp.tool()
    def review_bill_record_link(namespace: str, record_key: str, source_id: str, bill_key: str, decision: str,
                                reason: str, evidence: dict | None = None) -> dict:
        """Accept or reject a division or debate reference as linked to a bill, recording reviewer and reason."""
        from src.kb.legislation import LegislationStore

        return safe(lambda conn: LegislationStore(conn).review_link(
            namespace, record_key, source_id, bill_key, decision, reason, principal_id=who()[0], scopes=who()[1],
            evidence=evidence), write=True, required_scope=REVIEW)

    @mcp.tool()
    def revert_bill_record_link(namespace: str, review_id: str, reason: str) -> dict:
        """Revert an accepted or rejected record-to-bill link review; the next dossier revision reflects it."""
        from src.kb.legislation import LegislationStore

        return safe(lambda conn: LegislationStore(conn).revert_link(
            namespace, review_id, reason, principal_id=who()[0], scopes=who()[1]), write=True, required_scope=REVIEW)

    @mcp.tool()
    def list_legislation_identity_candidates(namespace: str, member_key: str | None = None) -> dict:
        """Identity candidates and decisions for sponsors and voting members, with method, evidence and confidence."""
        from src.kb.legislation import authorize
        from src.kb.legislation_identity import LegislationIdentity

        def run(conn):
            authorize(namespace, who()[1], READ)
            return {"candidates": LegislationIdentity(conn, initialize=False).candidates(
                namespace, scopes=who()[1], member_key=member_key)}

        return safe(run, required_scope=READ)

    @mcp.tool()
    def propose_legislation_identity_matches(namespace: str, political_jurisdictions: dict | None = None) -> dict:
        """Propose reviewable matches from members' official ids to election candidates and scoped Political pack
        persons in the same jurisdiction; a name alone is never acceptable and nothing is merged."""
        from src.kb.legislation_identity import LegislationIdentity

        return safe(lambda conn: LegislationIdentity(conn).propose(
            namespace, principal_id=who()[0], scopes=who()[1], political_jurisdictions=political_jurisdictions),
            write=True, required_scope="knowledge:ownership:write")

    @mcp.tool()
    def review_legislation_identity_match(namespace: str, candidate_id: str, decision: str, reason: str) -> dict:
        """Accept or reject a member identity candidate as an entity identity decision; records stay separate."""
        from src.kb.legislation_identity import LegislationIdentity

        return safe(lambda conn: LegislationIdentity(conn).review(
            namespace, candidate_id, decision, reason, principal_id=who()[0], scopes=who()[1]),
            write=True, required_scope="knowledge:ownership:review")

    @mcp.tool()
    def revert_legislation_identity_match(namespace: str, candidate_id: str, reason: str) -> dict:
        """Revert an accepted or rejected member identity decision."""
        from src.kb.legislation_identity import LegislationIdentity

        return safe(lambda conn: LegislationIdentity(conn).revert(
            namespace, candidate_id, reason, principal_id=who()[0], scopes=who()[1]),
            write=True, required_scope="knowledge:ownership:review")

    @mcp.tool()
    def link_bill_lobbying(namespace: str, bill_key: str, dossier_namespace: str, lobbying_namespace: str) -> dict:
        """Link lobbying disclosures that name the bill (explicit bill numbers; title words stay candidates) and
        report references to bills without a dossier. Degrades when the lobbying feature is absent; no influence
        is inferred."""
        from src.kb.legislation_links import LegislationLinks

        return safe(lambda conn: LegislationLinks(conn).link_lobbying(
            namespace, bill_key, dossier_namespace, lobbying_namespace, principal_id=who()[0], scopes=who()[1]),
            write=True, required_scope="knowledge:political:lobbying:write")

    @mcp.tool()
    def link_bill_enactment(namespace: str, bill_key: str, dossier_namespace: str,
                            legal_namespace: str | None = None) -> dict:
        """Link an enacted bill to its Legal work by a published public-law or Act citation; ambiguous, missing and
        Legal-unavailable targets are reported, never dropped."""
        from src.kb.legislation_links import LegislationLinks

        return safe(lambda conn: LegislationLinks(conn).link_enactment(
            namespace, bill_key, dossier_namespace, principal_id=who()[0], scopes=who()[1],
            legal_namespace=legal_namespace), write=True, required_scope=WRITE)

    @mcp.tool()
    def create_legislation_monitor(namespace: str, request_key: str, watch: str, key: str) -> dict:
        """Watch a bill (bill key) or a sponsor (member key) for new actions, stages, text versions and votes."""
        from src.kb.legislation_monitoring import LegislationMonitor

        return safe(lambda conn: LegislationMonitor(conn).create(
            namespace, request_key, watch=watch, key=key, principal_id=who()[0], scopes=who()[1]),
            write=True, required_scope="knowledge:subscriptions:write")

    @mcp.tool()
    def run_legislation_monitor(subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate a legislation monitor at a committed watermark; notices cite the new and previous revision."""
        from src.kb.legislation_monitoring import LegislationMonitor

        return safe(lambda conn: LegislationMonitor(conn).run(
            subscription_id, watermark, principal_id=who()[0], scopes=who()[1]),
            write=True, required_scope="knowledge:subscriptions:write")

    @mcp.tool()
    def poll_legislation_monitor(subscription_id: str, cursor: str = "") -> dict:
        """Delivered legislation monitor events after a cursor."""
        from src.kb.legislation_monitoring import LegislationMonitor

        return safe(lambda conn: LegislationMonitor(conn, initialize=False).poll(
            subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor),
            required_scope="knowledge:subscriptions:read")
