"""Technology internet-infrastructure features' entry points (``technology.internet-infrastructure``, #2743): a
declared ASN's, prefix's or domain's routing observations, PeeringDB self-declaration, RDAP registrations and
certificates as of a date, history, reviewable identity across sources, OSINT and Vulnerabilities links, evidence
bundles and subscription monitors.

Acquisition runs through the shared source-pack tools (pack ``technology-internet-infrastructure``: sources
``ripestat-routing``, ``peeringdb-network``, ``rdap-registrations``, ``crtsh-certificates`` and ``ct-log-list``).
Every statement is cited with its source, record revision and as-of time; sources are shown separately, never merged.

OSINT review gate (``docs/security/osint-review-gate.md``): none of these tools is gated. They answer registry and
routing facts for *declared* resources only, refuse IP-keyed, person-keyed and wildcard queries in code and never
list other networks of an organisation; any undeclared or reverse lookup is out of scope and would go through the
gate and the abuse analysis first.

Exclusions (declared by every answering tool): no exposed-service, port or banner data, no subdomain enumeration, no
IP-keyed or person-keyed lookups, no reputation, risk, hijack or misconfiguration verdicts, no ranking of networks and
no merging of RIPEstat, PeeringDB and RDAP statements into one record.
"""

READ = "knowledge:technical:internet-infrastructure:read"
WRITE = "knowledge:technical:internet-infrastructure:write"
REVIEW = "knowledge:technical:internet-infrastructure:review"
SOURCE_IDENTITY_READ = "knowledge:source-identity:read"
VULNERABILITIES_READ = "knowledge:vulnerabilities:read"
SUBSCRIPTIONS_READ = "knowledge:subscriptions:read"
SUBSCRIPTIONS_WRITE = "knowledge:subscriptions:write"
OSINT_GATE = "not gated (docs/security/osint-review-gate.md): declared resources only; undeclared or reverse " \
             "lookups are out of scope"

INTERNET_INFRASTRUCTURE_WRITES = {
    "propose_internet_infrastructure_identity",
    "review_internet_infrastructure_identity",
    "revert_internet_infrastructure_identity",
    "link_internet_infrastructure_osint",
    "link_internet_infrastructure_vulnerabilities",
    "create_internet_infrastructure_monitor",
    "run_internet_infrastructure_monitor",
}
INTERNET_INFRASTRUCTURE_READS = {
    "internet_infrastructure_source_contracts",
    "internet_infrastructure_readiness",
    "list_internet_infrastructure_resources",
    "internet_infrastructure_records_as_of",
    "internet_infrastructure_history",
    "list_internet_infrastructure_identity",
    "list_internet_infrastructure_links",
    "export_internet_infrastructure_bundle",
    "poll_internet_infrastructure_monitor",
}
INTERNET_INFRASTRUCTURE_TOOLS = INTERNET_INFRASTRUCTURE_WRITES | INTERNET_INFRASTRUCTURE_READS
INTERNET_INFRASTRUCTURE_SCOPES = {
    "internet_infrastructure_source_contracts": [],
    "internet_infrastructure_readiness": [READ],
    "list_internet_infrastructure_resources": [READ],
    "internet_infrastructure_records_as_of": [READ],
    "internet_infrastructure_history": [READ],
    "list_internet_infrastructure_identity": [READ],
    "list_internet_infrastructure_links": [READ],
    "export_internet_infrastructure_bundle": [READ],
    "poll_internet_infrastructure_monitor": [READ, SUBSCRIPTIONS_READ],
    "propose_internet_infrastructure_identity": [WRITE],
    "review_internet_infrastructure_identity": [REVIEW],
    "revert_internet_infrastructure_identity": [REVIEW],
    "link_internet_infrastructure_osint": [WRITE, SOURCE_IDENTITY_READ],
    "link_internet_infrastructure_vulnerabilities": [WRITE, VULNERABILITIES_READ],
    "create_internet_infrastructure_monitor": [READ, SUBSCRIPTIONS_WRITE],
    "run_internet_infrastructure_monitor": [READ, SUBSCRIPTIONS_WRITE],
}


def required_scopes(tool_name, mutability):
    return INTERNET_INFRASTRUCTURE_SCOPES.get(tool_name, [WRITE if mutability == "write" else READ])


def _require(scopes, *required):
    from src.kb.internet_infrastructure_records import InfrastructureRecordError

    missing = [s for s in required if s not in scopes and "operator" not in scopes]
    if missing:
        raise InfrastructureRecordError("unauthorized", f"{', '.join(missing)} required")


def _declared(answer):
    """Every answer leaves with the exclusions and the OSINT-gate status declared, checked against the minimisation."""
    from src.ingestion.internet_infrastructure_sources import EXCLUSIONS
    from src.kb.internet_infrastructure_records import (
        InfrastructureRecordError,
        forbidden_paths,
        personal_data_paths,
    )

    if not isinstance(answer, dict):
        return answer
    if personal_data_paths(answer) or forbidden_paths(answer):
        raise InfrastructureRecordError("minimisation", "an answer would carry person, contact or verdict data")
    return {**answer, "exclusions": list(EXCLUSIONS), "osint_review_gate": OSINT_GATE}


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def run_tool(tool, operation, *, write=False):
        """Checks every declared scope, runs the operation and declares the exclusions on the answer."""
        scopes = INTERNET_INFRASTRUCTURE_SCOPES[tool]

        def run(conn):
            _require(who()[1], *scopes)
            return _declared(operation(conn))

        return safe(run, write=write, required_scope=scopes[0] if scopes else None)

    @mcp.tool()
    def internet_infrastructure_source_contracts() -> dict:
        """Per-source access decisions (RIPEstat, PeeringDB, RDAP, crt.sh, CT log list; RFC 6962 logs and CAIDA not
        implemented), keys, limits, terms, revision models, caps, bounded coverage and the minimisation decision;
        every source is unverified-live until a dated live run.
        Exclusions: no exposed-service, port or banner data, no subdomain enumeration, no IP-keyed or person-keyed
        lookups, no reputation, risk, hijack or misconfiguration verdicts, no ranking of networks, no merged records."""
        from src.ingestion.internet_infrastructure_sources import (
            BOUNDED_COVERAGE,
            CAPS,
            EXCLUSIONS,
            LIVE_VERIFICATION,
            MINIMISATION,
            NEVER_SENTENCE,
            PROVIDER_CONTRACTS,
        )

        return {"contracts": PROVIDER_CONTRACTS, "live_verification": LIVE_VERIFICATION, "caps": CAPS,
                "bounded_coverage": BOUNDED_COVERAGE, "minimisation": MINIMISATION, "never": NEVER_SENTENCE,
                "exclusions": list(EXCLUSIONS), "osint_review_gate": OSINT_GATE}

    @mcp.tool()
    def internet_infrastructure_readiness() -> dict:
        """Which internet-infrastructure features are selected, the stores, per-source units and staleness."""
        from src.kb.internet_infrastructure_records import readiness

        return run_tool("internet_infrastructure_readiness", readiness)

    @mcp.tool()
    def list_internet_infrastructure_resources(namespace: str) -> dict:
        """The declared ASNs, prefixes and domains this namespace answers; nothing else is looked up."""
        from src.kb.internet_infrastructure_queries import InfrastructureQueries

        return run_tool("list_internet_infrastructure_resources", lambda conn: InfrastructureQueries(
            conn).declared_resources(namespace, scopes=who()[1]))

    @mcp.tool()
    def internet_infrastructure_records_as_of(namespace: str, resource: str | dict,
                                              as_of: str | int | None = None) -> dict:
        """A declared ASN, prefix or domain as of a date: RIPEstat routing observations, the PeeringDB
        self-declaration, RDAP registrations, crt.sh certificates and the CT logs they state, each source separately
        and cited with source, record revision and as-of time; accepted identity matches and links beside them.
        IP-keyed, person-keyed, wildcard and undeclared lookups are refused.
        Exclusions: no exposed-service, port or banner data, no subdomain enumeration, no IP-keyed or person-keyed
        lookups, no reputation, risk, hijack or misconfiguration verdicts, no ranking of networks, no merged records."""
        from src.kb.internet_infrastructure_queries import InfrastructureQueries

        return run_tool("internet_infrastructure_records_as_of", lambda conn: InfrastructureQueries(
            conn).records_as_of(namespace, resource, scopes=who()[1], as_of=as_of))

    @mcp.tool()
    def internet_infrastructure_history(namespace: str, resource: str | dict) -> dict:
        """Every revision and observation each source stated about a declared resource, with citations.
        Exclusions: no exposed-service, port or banner data, no subdomain enumeration, no IP-keyed or person-keyed
        lookups, no reputation, risk, hijack or misconfiguration verdicts, no ranking of networks, no merged records."""
        from src.kb.internet_infrastructure_queries import InfrastructureQueries

        return run_tool("internet_infrastructure_history", lambda conn: InfrastructureQueries(conn).history(
            namespace, resource, scopes=who()[1]))

    @mcp.tool()
    def list_internet_infrastructure_identity(namespace: str, state: str | None = None) -> dict:
        """Identity assertions between records of different sources (proposed, accepted, rejected, reverted) with
        method, evidence and confidence, and the records that stay unmatched."""
        from src.kb.internet_infrastructure_identity import InfrastructureIdentity

        def op(conn):
            identity = InfrastructureIdentity(conn, initialize=False)
            return {"assertions": identity.assertions(namespace, scopes=who()[1], state=state),
                    "unmatched": identity.unmatched(namespace, scopes=who()[1])}

        return run_tool("list_internet_infrastructure_identity", op)

    @mcp.tool()
    def list_internet_infrastructure_links(namespace: str, kind: str | None = None, state: str | None = None) -> dict:
        """Links to OSINT source identities (stated domain) and Vulnerabilities (stated CVE id), each pinning both
        revisions; absent providers and targets are reported."""
        from src.kb.internet_infrastructure_links import InfrastructureLinks

        return run_tool("list_internet_infrastructure_links", lambda conn: {
            "links": InfrastructureLinks(conn, initialize=False).links(namespace, scopes=who()[1], kind=kind,
                                                                      state=state)})

    @mcp.tool()
    def export_internet_infrastructure_bundle(namespace: str, resource: str | dict,
                                              as_of: str | int | None = None) -> dict:
        """An evidence bundle (noesis-evidence-bundle-v1) of a declared resource's records as of a date, citing every
        item with source, record revision and as-of time.
        Exclusions: no exposed-service, port or banner data, no subdomain enumeration, no IP-keyed or person-keyed
        lookups, no reputation, risk, hijack or misconfiguration verdicts, no ranking of networks, no merged records."""
        from src.kb.internet_infrastructure_queries import InfrastructureQueries

        def op(conn):
            queries = InfrastructureQueries(conn)
            answer = queries.records_as_of(namespace, resource, scopes=who()[1], as_of=as_of)
            return queries.export_bundle(namespace, answer, scopes=who()[1])

        return run_tool("export_internet_infrastructure_bundle", op)

    @mcp.tool()
    def poll_internet_infrastructure_monitor(subscription_id: str, cursor: str = "") -> dict:
        """Poll an internet-infrastructure monitor's events (registration changes, PeeringDB updates, new
        certificates, CT log state changes, changed routing observations, removals)."""
        from src.kb.internet_infrastructure_monitoring import InfrastructureMonitor

        return run_tool("poll_internet_infrastructure_monitor", lambda conn: InfrastructureMonitor(
            conn, initialize=False).poll(subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor))

    # ------------------------------------------------------------------ writes

    @mcp.tool()
    def propose_internet_infrastructure_identity(namespace: str) -> dict:
        """Propose matches between records of different sources by stated identifiers (ASN, prefix, domain,
        PeeringDB id); a shared name is never a match and nothing is used until reviewed."""
        from src.kb.internet_infrastructure_identity import InfrastructureIdentity

        return run_tool("propose_internet_infrastructure_identity", lambda conn: InfrastructureIdentity(conn).propose(
            namespace, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def review_internet_infrastructure_identity(namespace: str, assertion_id: str, decision: str,
                                                reason: str) -> dict:
        """Accept or reject a proposed identity assertion with a reason; nothing is merged."""
        from src.kb.internet_infrastructure_identity import InfrastructureIdentity

        return run_tool("review_internet_infrastructure_identity", lambda conn: InfrastructureIdentity(conn).review(
            namespace, assertion_id, decision, reason, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def revert_internet_infrastructure_identity(namespace: str, assertion_id: str, reason: str) -> dict:
        """Revert an accepted identity assertion; answers stop showing it."""
        from src.kb.internet_infrastructure_identity import InfrastructureIdentity

        return run_tool("revert_internet_infrastructure_identity", lambda conn: InfrastructureIdentity(conn).revert(
            namespace, assertion_id, reason, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def link_internet_infrastructure_osint(namespace: str, osint_namespace: str | None = None) -> dict:
        """Link domain records to existing OSINT source identities by their stated domain (read-only; no source
        identity is written); provider_absent when the OSINT store is not held. Nothing is inferred from shared
        addresses.
        Exclusions: no exposed-service, port or banner data, no subdomain enumeration, no IP-keyed or person-keyed
        lookups, no reputation, risk, hijack or misconfiguration verdicts, no ranking of networks, no merged records."""
        from src.kb.internet_infrastructure_links import InfrastructureLinks

        return run_tool("link_internet_infrastructure_osint", lambda conn: InfrastructureLinks(conn).link_osint(
            namespace, principal_id=who()[0], scopes=who()[1], osint_namespace=osint_namespace), write=True)

    @mcp.tool()
    def link_internet_infrastructure_vulnerabilities(namespace: str,
                                                     vulnerabilities_namespace: str | None = None) -> dict:
        """Link records to technology.vulnerabilities only where a source states a CVE id (none does in first
        coverage); provider_absent when the Vulnerabilities store is not held."""
        from src.kb.internet_infrastructure_links import InfrastructureLinks

        return run_tool("link_internet_infrastructure_vulnerabilities", lambda conn: InfrastructureLinks(
            conn).link_vulnerabilities(namespace, principal_id=who()[0], scopes=who()[1],
                                       vulnerabilities_namespace=vulnerabilities_namespace), write=True)

    @mcp.tool()
    def create_internet_infrastructure_monitor(namespace: str, request_key: str, target: dict,
                                               delivery: dict | None = None) -> dict:
        """Subscribe to a declared ASN, prefix or domain (optionally one provider) or a provider: notices of
        registration changes, PeeringDB updates, new certificates, CT log state changes, changed routing
        observations and removals; never a verdict. IP-keyed, person-keyed and wildcard targets are refused."""
        from src.kb.internet_infrastructure_monitoring import InfrastructureMonitor

        return run_tool("create_internet_infrastructure_monitor", lambda conn: InfrastructureMonitor(conn).create(
            namespace, request_key, target=target, principal_id=who()[0], scopes=who()[1], delivery=delivery),
            write=True)

    @mcp.tool()
    def run_internet_infrastructure_monitor(subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate an internet-infrastructure monitor at a committed watermark; notices cite the records."""
        from src.kb.internet_infrastructure_monitoring import InfrastructureMonitor

        return run_tool("run_internet_infrastructure_monitor", lambda conn: InfrastructureMonitor(conn).run(
            subscription_id, watermark, principal_id=who()[0], scopes=who()[1]), write=True)
