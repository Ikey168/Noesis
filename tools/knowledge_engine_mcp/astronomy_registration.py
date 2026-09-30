"""Space-object registration entry points: registrations, operators and re-entries as of a date (#2224, SO12).

Acquisition runs through the shared source-pack tools (pack
``astronomy-and-space``, connector ``astronomy-registration``). Every answer
states its knowledge cutoff and quotes the publishers: Noesis computes no
re-entry or collision prediction and attributes no operator, owner or State
beyond a published registration or operator assertion. ESA DISCOS is an
optional, account-gated feature: without a configured token its sources do not
run, and its values are never exported.
"""

import os
from typing import Any

READ = "knowledge:astronomy:read"
WRITE = "knowledge:astronomy:write"
REVIEW = "knowledge:astronomy:review"
DISCOS_SECRET = "NOESIS_ESA_DISCOS_TOKEN"
REGISTRATION_WRITES = {
    "match_space_registration_objects",
    "match_space_registration_parties",
    "review_space_registration_party",
    "revert_space_registration_party",
    "link_space_registration_citations",
    "revert_space_registration_citation",
    "project_reentry_locations",
    "create_space_registration_monitor",
    "run_space_registration_monitor",
}
REGISTRATION_READS = {
    "object_registration_as_of",
    "reentry_record",
    "export_space_registration_evidence",
    "space_object_registration_status",
    "discos_access_status",
    "list_space_registration_candidates",
    "space_registration_citations",
    "poll_space_registration_monitor",
}
REGISTRATION_TOOLS = REGISTRATION_WRITES | REGISTRATION_READS
# Every scope a tool always uses; scopes only an optional argument needs are checked when it is used.
REGISTRATION_SCOPES = {
    "match_space_registration_objects": [READ, WRITE],
    "match_space_registration_parties": [READ, WRITE, "knowledge:ownership:write"],
    "review_space_registration_party": [READ, "knowledge:ownership:review"],
    "revert_space_registration_party": [READ, "knowledge:ownership:review"],
    "link_space_registration_citations": [READ, WRITE],
    "revert_space_registration_citation": [REVIEW],
    "project_reentry_locations": [READ, "knowledge:geospatial:write"],
    "create_space_registration_monitor": [READ, "knowledge:subscriptions:write"],
    "run_space_registration_monitor": [READ, "knowledge:subscriptions:read", "knowledge:subscriptions:write"],
    "poll_space_registration_monitor": [READ, "knowledge:subscriptions:read"],
}
OPTIONAL_SCOPES = {
    "ownership_namespace": ["knowledge:ownership:read"],
    "legal_namespace": ["knowledge:legal:read"],
}
QUERY_EXAMPLES = {
    "object_registration_as_of": {
        "arguments": {"namespace": "astronomy", "identifier": "2099-001A", "as_of": "2099-05-20"},
        "semantics": "registering and supervising State and status from the dated registration chain, operators as "
        "published, catalogue status and re-entry predictions/confirmed report distinctly; 'none on record' with "
        "the sources consulted",
    },
    "reentry_record": {
        "arguments": {"namespace": "astronomy", "identifier": "99901"},
        "semantics": "every published prediction and the confirmed report per publisher, plus UN re-entry notices; "
        "no Noesis prediction",
    },
}


def required_scopes(tool_name, mutability):
    return REGISTRATION_SCOPES.get(tool_name, [WRITE if mutability == "write" else READ])


def discos_configured() -> bool:
    """Whether a DISCOS account token is configured (the value is never read here)."""
    return bool(os.environ.get(DISCOS_SECRET))


def bundle_status(conn) -> dict[str, Any]:
    """Per-source LIVE_VERIFICATION, DISCOS enablement and whether the features are selected."""
    from src.ingestion.astronomy_registration_sources import LIVE_VERIFICATION, PROVIDER_CONTRACTS
    from src.kb.astronomy_registration import FEATURES, feature_enabled

    features = {f: feature_enabled(conn, f) for f in FEATURES}
    sources = []
    for provider, contract in sorted(PROVIDER_CONTRACTS.items()):
        entry = {"provider": provider, "publisher": contract["publisher"],
                 "access_decision": contract["access_decision"], "live_verification": LIVE_VERIFICATION[provider]}
        if provider == "esa-discos":
            enabled = features["astronomy-discos"] and discos_configured()
            entry["state"] = ("enabled" if enabled else "disabled: no account configured" if not discos_configured()
                              else "disabled: the astronomy-discos feature is off")
        sources.append(entry)
    return {"features": features, "sources": sources,
            "policy": "no re-entry or collision prediction; attribution only as published; DISCOS never exported"}


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def checked(tool_name, operation, *, optional: dict[str, Any] | None = None, ready: bool = True):
        def run(conn):
            from src.kb.astronomy_records import AstronomyError
            from src.kb.astronomy_registration import RegistrationStore

            scopes = who()[1]
            needed = list(REGISTRATION_SCOPES.get(tool_name, [READ]))
            for argument, value in (optional or {}).items():
                if value:
                    needed += OPTIONAL_SCOPES[argument]
            missing = [s for s in needed if s not in scopes]
            if missing and "operator" not in scopes:
                raise AstronomyError("unauthorized", f"{', '.join(missing)} required")
            if ready:
                RegistrationStore(conn, initialize=False).require_ready()
            return operation(conn)

        return run

    def read(tool_name, operation, **kwargs):
        return safe(checked(tool_name, operation, **kwargs),
                    required_scope=REGISTRATION_SCOPES.get(tool_name, [READ])[0])

    def write(tool_name, operation, **kwargs):
        return safe(checked(tool_name, operation, **kwargs), write=True,
                    required_scope=REGISTRATION_SCOPES[tool_name][-1])

    def queries(conn):
        from src.kb.astronomy_registration_queries import RegistrationQueries

        return RegistrationQueries(conn)

    @mcp.tool()
    def object_registration_as_of(namespace: str, identifier: str, as_of: str,
                                  acquired_by_ms: int | None = None) -> dict:
        """A space object's registration (registering and supervising State, status, UN documents with locators),
        operator assertions, catalogue status and re-entry record at a date (YYYY-MM-DD, end of day UTC), cited.

        The object is a COSPAR designator, NORAD number, discos:<id> or name. Missing registration answers 'none on
        record' with the sources consulted (never 'unregistered'); re-entry predictions and the confirmed report stay
        distinct and Noesis adds no prediction."""
        return read("object_registration_as_of", lambda conn: queries(conn).object_registration_as_of(
            namespace, identifier, as_of, scopes=who()[1], acquired_by_ms=acquired_by_ms))

    @mcp.tool()
    def reentry_record(namespace: str, identifier: str, as_of: str | None = None,
                       acquired_by_ms: int | None = None) -> dict:
        """An object's published re-entry predictions and confirmed report per publisher, and UN re-entry notices,
        as published. No re-entry or collision prediction is computed."""
        return read("reentry_record", lambda conn: queries(conn).reentry_record(
            namespace, identifier, scopes=who()[1], as_of=as_of, acquired_by_ms=acquired_by_ms))

    @mcp.tool()
    def export_space_registration_evidence(namespace: str, identifier: str, as_of: str,
                                           acquired_by_ms: int | None = None) -> dict:
        """The object_registration_as_of answer as an evidence bundle with every revision and identity match used;
        account-restricted DISCOS values are cited only."""

        def run(conn):
            answer = queries(conn).object_registration_as_of(namespace, identifier, as_of, scopes=who()[1],
                                                             acquired_by_ms=acquired_by_ms)
            return queries(conn).export_bundle(namespace, answer, scopes=who()[1])

        return read("export_space_registration_evidence", run)

    @mcp.tool()
    def space_object_registration_status() -> dict:
        """Registration sources with their access decision and LIVE_VERIFICATION, feature selection and whether
        ESA DISCOS is enabled (it is off without a configured account)."""
        return read("space_object_registration_status", bundle_status, ready=False)

    @mcp.tool()
    def discos_access_status() -> dict:
        """Whether ESA DISCOS is configured and selected, and its documented access decision (permitted subset or
        citation only; never redistributed). The token is never shown."""

        def run(conn):
            status = bundle_status(conn)
            discos = next(s for s in status["sources"] if s["provider"] == "esa-discos")
            return {"configured": discos_configured(), "feature": status["features"]["astronomy-discos"], **discos}

        return read("discos_access_status", run, ready=False)

    @mcp.tool()
    def list_space_registration_candidates(namespace: str, state: str | None = None) -> dict:
        """Reviewable identity candidates of registrations (identifier conflicts, name-only objects) and of parties
        (registering States, intergovernmental registrants, operators), with the exact object links."""

        def run(conn):
            from src.kb.astronomy_registration_identity import RegistrationIdentity

            identity = RegistrationIdentity(conn, initialize=False)
            objects = [c for c in identity.astronomy.candidates(namespace, scopes=who()[1], state=state)
                       if c["basis"].startswith("registration-")]
            parties = [c for c in identity.party_candidates(namespace, scopes=who()[1])
                       if state is None or c["state"] == state]
            return {"objects": objects, "parties": parties, "object_links": identity.object_links(namespace),
                    "n": len(objects) + len(parties)}

        return read("list_space_registration_candidates", run)

    @mcp.tool()
    def space_registration_citations(namespace: str, record_id: str | None = None) -> dict:
        """Links from registrations to Legal works (by the instrument identifier the document names) and Science
        papers (by DOI or bibcode), and unresolved citations."""

        def run(conn):
            from src.kb.astronomy_registration import RegistrationCitations

            links = RegistrationCitations(conn, initialize=False).links(namespace, scopes=who()[1],
                                                                         record_id=record_id)
            return {"links": links, "n": len(links)}

        return read("space_registration_citations", run)

    @mcp.tool()
    def match_space_registration_objects(namespace: str) -> dict:
        """Link registrations, DISCOS and re-entry records to SATCAT/GCAT objects on exact COSPAR/NORAD agreement
        with evidence; conflicting identifiers and name-only registrations become reviewable candidates (reviewed
        with review_astronomy_identity_match). Records are never merged."""

        def run(conn):
            from src.kb.astronomy_registration_identity import RegistrationIdentity

            principal, scopes = who()
            return RegistrationIdentity(conn).match_objects(namespace, principal_id=principal, scopes=scopes)

        return write("match_space_registration_objects", run)

    @mcp.tool()
    def match_space_registration_parties(namespace: str, ownership_namespace: str | None = None) -> dict:
        """Offer registering States, intergovernmental registrants and operators for review against canonical
        entities (and, with ownership_namespace, ownership legal entities by published LEI). Nothing is accepted
        here and no attribution beyond the published records is created."""

        def run(conn):
            from src.kb.astronomy_registration_identity import RegistrationIdentity

            principal, scopes = who()
            return RegistrationIdentity(conn).match_parties(namespace, principal_id=principal, scopes=scopes,
                                                            ownership_namespace=ownership_namespace)

        return write("match_space_registration_parties", run, optional={"ownership_namespace": ownership_namespace})

    @mcp.tool()
    def review_space_registration_party(namespace: str, candidate_id: str, decision: str, reason: str) -> dict:
        """Accept or reject a space-registration party candidate with a reason (an entity identity decision)."""

        def run(conn):
            from src.kb.astronomy_registration_identity import RegistrationIdentity

            principal, scopes = who()
            return RegistrationIdentity(conn).review_party(namespace, candidate_id, decision, reason,
                                                           principal_id=principal, scopes=scopes)

        return write("review_space_registration_party", run)

    @mcp.tool()
    def revert_space_registration_party(namespace: str, candidate_id: str, reason: str) -> dict:
        """Revert an accepted or rejected space-registration party decision; records stay intact."""

        def run(conn):
            from src.kb.astronomy_registration_identity import RegistrationIdentity

            principal, scopes = who()
            return RegistrationIdentity(conn).revert_party(namespace, candidate_id, reason, principal_id=principal,
                                                           scopes=scopes)

        return write("revert_space_registration_party", run)

    @mcp.tool()
    def link_space_registration_citations(namespace: str, legal_namespace: str | None = None) -> dict:
        """Resolve the instrument identifiers, DOIs and bibcodes registrations state to Legal works and Science
        papers by exact identifier; absent packs are skipped; idempotent; never reactivates a revert."""

        def run(conn):
            from src.kb.astronomy_registration import RegistrationCitations

            principal, scopes = who()
            return RegistrationCitations(conn).link(namespace, principal_id=principal, scopes=scopes,
                                                    legal_namespace=legal_namespace)

        return write("link_space_registration_citations", run, optional={"legal_namespace": legal_namespace})

    @mcp.tool()
    def revert_space_registration_citation(namespace: str, link_id: str, reason: str) -> dict:
        """Revert one registration citation link (final for that target revision)."""

        def run(conn):
            from src.kb.astronomy_registration import RegistrationCitations

            principal, scopes = who()
            return RegistrationCitations(conn).revert(namespace, link_id, reason, principal_id=principal,
                                                      scopes=scopes)

        return write("revert_space_registration_citation", run)

    @mcp.tool()
    def project_reentry_locations(namespace: str, geo_namespace: str) -> dict:
        """Project published re-entry locations into Geospatial places, only where the publisher states
        coordinates; text-only locations stay text and nothing is computed."""

        def run(conn):
            from src.kb.astronomy_registration import project_reentry_locations as project

            principal, scopes = who()
            return project(conn, namespace, geo_namespace, principal_id=principal, scopes=scopes)

        return write("project_reentry_locations", run)

    @mcp.tool()
    def create_space_registration_monitor(namespace: str, request_key: str, watch: str, target: str) -> dict:
        """Watch an object, operator or registering State for new registrations, status changes, transfers,
        operator changes and re-entry reports, as a knowledge subscription; no separate scheduler."""

        def run(conn):
            from src.kb.astronomy_registration_monitoring import RegistrationMonitor

            principal, scopes = who()
            return RegistrationMonitor(conn).create(namespace, request_key, watch=watch, target=target,
                                                    principal_id=principal, scopes=scopes)

        return write("create_space_registration_monitor", run)

    @mcp.tool()
    def run_space_registration_monitor(subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate a registration monitor at a committed watermark: dated events cite the new revision."""

        def run(conn):
            from src.kb.astronomy_registration_monitoring import RegistrationMonitor

            principal, scopes = who()
            return RegistrationMonitor(conn).run(subscription_id, watermark, principal_id=principal, scopes=scopes)

        return write("run_space_registration_monitor", run)

    @mcp.tool()
    def poll_space_registration_monitor(subscription_id: str, cursor: str = "") -> dict:
        """Poll a registration monitor's recorded events."""

        def run(conn):
            from src.kb.astronomy_registration_monitoring import RegistrationMonitor

            principal, scopes = who()
            return RegistrationMonitor(conn, initialize=False).poll(subscription_id, principal_id=principal,
                                                                    scopes=scopes, cursor=cursor)

        return read("poll_space_registration_monitor", run, ready=False)
