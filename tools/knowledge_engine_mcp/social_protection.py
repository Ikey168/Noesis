"""Society bundle ``society.social-protection`` entry points (#2741, SS11): social protection series.

Acquisition runs through the shared source-pack tools (pack ``society-social-protection``: sources
``eurostat-esspros``, ``oecd-socx`` and ``ilo-social-protection-coverage``; each an optional Society feature, default
off). These tools answer an indicator for a place as of a release, a series' history with comparability notes,
reviewable place and function identity, citation links to Demographics and Public finance, monitors and a cited
evidence-bundle export. Every namespaced tool checks the Society bundle's enablement first; Geospatial, Demographics,
Public finance and platform providers are never gated by it.

Exclusions (declared by every answer): no nowcasting, no filled years, no blending of ESSPROS, SOCX and ILO figures,
no re-classification of one publisher's functions into another's, no combination with COFOG, no per-capita,
per-beneficiary or share-of-GDP figure of our own, no derived indicators and no forecasts. Every answer is checked
against the SS01 minimisation decision (no person- or household-level field, no derived key) before it leaves.
"""

READ = "knowledge:social-protection:read"
WRITE = "knowledge:social-protection:write"
REVIEW = "knowledge:social-protection:review"
GEO_READ = "knowledge:geospatial:read"
DEMOGRAPHICS_READ = "knowledge:demographics:read"
PUBLIC_FINANCE_READ = "knowledge:public-finance:read"
SUBSCRIPTIONS_READ = "knowledge:subscriptions:read"
SUBSCRIPTIONS_WRITE = "knowledge:subscriptions:write"
EXCLUSIONS_NOTE = (
    "Exclusions: no nowcasting, no filled years, no blending of ESSPROS, SOCX and ILO figures, no re-classification "
    "of functions, no combination with COFOG, no per-capita or share figures of our own, no derived figures and no "
    "forecasts."
)

SOCIAL_PROTECTION_WRITES = {
    "register_social_protection_schemas",
    "propose_social_protection_place_matches",
    "propose_social_protection_function_relations",
    "record_social_protection_function_relation",
    "review_social_protection_identity",
    "revert_social_protection_identity",
    "link_social_protection_series",
    "record_social_protection_comparability",
    "review_social_protection_comparability",
    "create_social_protection_monitor",
    "run_social_protection_monitor",
}
SOCIAL_PROTECTION_READS = {
    "social_protection_source_contracts",
    "social_protection_readiness",
    "list_social_protection_series",
    "social_protection_indicator_for_place",
    "social_protection_series_history",
    "social_protection_comparability_notes",
    "list_social_protection_identity",
    "list_social_protection_links",
    "social_protection_profile",
    "export_social_protection_profile",
    "poll_social_protection_monitor",
}
SOCIAL_PROTECTION_TOOLS = SOCIAL_PROTECTION_WRITES | SOCIAL_PROTECTION_READS
SOCIAL_PROTECTION_SCOPES = {
    "social_protection_source_contracts": [],
    "social_protection_readiness": [READ],
    "list_social_protection_series": [READ],
    "social_protection_indicator_for_place": [READ],
    "social_protection_series_history": [READ],
    "social_protection_comparability_notes": [READ],
    "list_social_protection_identity": [READ],
    "list_social_protection_links": [READ],
    "social_protection_profile": [READ],
    "export_social_protection_profile": [READ],
    "poll_social_protection_monitor": [READ, SUBSCRIPTIONS_READ],
    "register_social_protection_schemas": [WRITE, "knowledge:schema:register"],
    "propose_social_protection_place_matches": [WRITE, GEO_READ],
    "propose_social_protection_function_relations": [WRITE],
    "record_social_protection_function_relation": [WRITE],
    "review_social_protection_identity": [REVIEW],
    "revert_social_protection_identity": [REVIEW],
    "link_social_protection_series": [WRITE, DEMOGRAPHICS_READ, PUBLIC_FINANCE_READ],
    "record_social_protection_comparability": [WRITE],
    "review_social_protection_comparability": [REVIEW],
    "create_social_protection_monitor": [READ, SUBSCRIPTIONS_WRITE],
    "run_social_protection_monitor": [READ, SUBSCRIPTIONS_WRITE],
}


def required_scopes(tool_name, mutability):
    return SOCIAL_PROTECTION_SCOPES.get(tool_name, [WRITE if mutability == "write" else READ])


def _require(scopes, *required):
    from src.kb.social_protection_records import SocialProtectionError

    missing = [s for s in required if s not in scopes and "operator" not in scopes]
    if missing:
        raise SocialProtectionError("unauthorized", f"{', '.join(missing)} required")


def _declared(answer):
    """Every answer is checked against the minimisation decision and leaves with the exclusions declared (an evidence
    bundle carries them in its root receipt, so its sealed contract is left untouched)."""
    from src.kb.social_protection_records import (
        EXCLUSIONS,
        MINIMISATION,
        SocialProtectionError,
        forbidden_paths,
        personal_data_paths,
    )

    if not isinstance(answer, dict):
        return answer
    if personal_data_paths(answer) or forbidden_paths(answer):
        raise SocialProtectionError("minimisation", "an answer would carry a person-level, household-level or "
                                                    "derived field")
    if answer.get("contract") == "noesis-evidence-bundle-v1":
        return answer
    return {**answer, "exclusions": list(EXCLUSIONS), "minimisation": answer.get("minimisation")
            or MINIMISATION["decision"]}


def register(mcp, safe, context):
    def who():
        return context()[0], set(context()[1])

    def run_tool(tool, operation, *, namespace=None, write=False):
        """Checks every declared scope and the bundle's enablement, runs the operation and declares exclusions."""
        scopes = SOCIAL_PROTECTION_SCOPES[tool]

        def run(conn):
            _require(who()[1], *scopes)
            if namespace is not None:
                from src.kb.society_bundle import require_enabled

                require_enabled(conn, namespace)
            return _declared(operation(conn))

        return safe(run, write=write, required_scope=scopes[0] if scopes else None)

    @mcp.tool()
    def social_protection_source_contracts() -> dict:
        """Per-source access decisions (Eurostat ESSPROS, OECD SOCX, ILOSTAT SDG 1.3.1; the ILO dashboards
        not-implemented), licences, limits and pacing, revision models, caps, bounded coverage and the minimisation
        decision; every source is unverified-live until a dated live run.
        Exclusions: no nowcasting, no filled years, no blending of ESSPROS, SOCX and ILO figures, no re-classification
        of functions, no combination with COFOG, no per-capita or share figures of our own, no derived figures and no
        forecasts."""
        from src.ingestion.social_protection_sources import (
            AUDIT,
            BOUNDED_COVERAGE,
            CAPS,
            LIVE_VERIFICATION,
            NEVER_SENTENCE,
            PACING,
            PROVIDER_CONTRACTS,
        )
        from src.kb.social_protection_records import MINIMISATION

        return _declared({"contracts": PROVIDER_CONTRACTS, "live_verification": LIVE_VERIFICATION, "caps": CAPS,
                          "pacing": PACING, "bounded_coverage": BOUNDED_COVERAGE, "minimisation": MINIMISATION,
                          "never": NEVER_SENTENCE, "audit": AUDIT})

    @mcp.tool()
    def social_protection_readiness(namespace: str = "global") -> dict:
        """ready / fixture-only / stale / unavailable / feature-disabled per source, Demographics and Public finance
        links (provider_absent when not composed); live verification reported separately."""
        from src.kb.social_protection_records import readiness

        return run_tool("social_protection_readiness", lambda conn: readiness(conn, namespace))

    @mcp.tool()
    def list_social_protection_series(namespace: str, provider: str | None = None, measure: str | None = None,
                                      area: dict | None = None, function: dict | None = None) -> dict:
        """Series with source, dataset, measure, the publisher's own function, scheme type, financing, cash or in
        kind, basis, sex, unit as published, place and population denominator."""
        from src.kb.social_protection_records import authorize
        from src.kb.social_protection_store import SocialProtectionStore

        def op(conn):
            authorize(namespace, who()[1], READ)
            codes = [(area["scheme"], str(area["code"]))] if area else None
            functions = [(function["scheme"], str(function["code"]))] if function else None
            return {"series": SocialProtectionStore(conn, initialize=False).find_series(
                namespace, provider=provider, measure=measure, area_codes=codes, functions=functions)}

        return run_tool("list_social_protection_series", op, namespace=namespace)

    @mcp.tool()
    def social_protection_indicator_for_place(namespace: str, place_id: str | None = None, area: dict | None = None,
                                              measure: str | None = None, function: dict | None = None,
                                              as_of: str | None = None, reference_year: str | None = None) -> dict:
        """ESSPROS, SOCX and ILO values for a place (place id or {scheme, code}) released by a date, side by side by
        measure with definitions, functions, units, flags, publication status and cited vintages; expenditure and
        coverage are never equated and COFOG is shown only as cited links.
        Exclusions: no nowcasting, no filled years, no blending of ESSPROS, SOCX and ILO figures, no re-classification
        of functions, no combination with COFOG, no per-capita or share figures of our own, no derived figures and no
        forecasts."""
        from src.kb.social_protection_queries import SocialProtectionQueries
        from src.kb.social_protection_records import enabled_providers

        return run_tool("social_protection_indicator_for_place",
                        lambda conn: SocialProtectionQueries(conn).indicator_for_place(
                            namespace, scopes=who()[1], place_id=place_id, area=area, measure=measure,
                            function=function, as_of=as_of, reference_year=reference_year,
                            enabled_providers=enabled_providers(conn)), namespace=namespace)

    @mcp.tool()
    def social_protection_series_history(namespace: str, series_id: str) -> dict:
        """Every vintage of a series with changed periods, estimates replaced, report-edition restatements, manual
        edition and definition changes, removals and the comparability notes per release pair
        (comparability_unknown when none applies).
        Exclusions: no nowcasting, no filled years, no blending of ESSPROS, SOCX and ILO figures, no re-classification
        of functions, no combination with COFOG, no per-capita or share figures of our own, no derived figures and no
        forecasts."""
        from src.kb.social_protection_queries import SocialProtectionQueries

        return run_tool("social_protection_series_history", lambda conn: SocialProtectionQueries(
            conn).series_history(namespace, series_id, scopes=who()[1]), namespace=namespace)

    @mcp.tool()
    def social_protection_comparability_notes(namespace: str, series_id: str, include_inactive: bool = False) -> dict:
        """Source-stated breaks, definition differences and ESSPROS-SOCX scope notes, and reviewer notes."""
        from src.kb.social_protection_records import authorize
        from src.kb.social_protection_store import SocialProtectionStore

        def op(conn):
            authorize(namespace, who()[1], READ)
            return {"notes": SocialProtectionStore(conn, initialize=False).notes(
                namespace, series_id, active_only=not include_inactive)}

        return run_tool("social_protection_comparability_notes", op, namespace=namespace)

    @mcp.tool()
    def social_protection_profile(namespace: str, place_id: str | None = None, area: dict | None = None,
                                  as_of: str | None = None) -> dict:
        """A place's expenditure, beneficiary and coverage figures from each source side by side as of a date, with
        Demographics and Public finance links.
        Exclusions: no nowcasting, no filled years, no blending of ESSPROS, SOCX and ILO figures, no re-classification
        of functions, no combination with COFOG, no per-capita or share figures of our own, no derived figures and no
        forecasts."""
        from src.kb.social_protection_queries import place_profile

        return run_tool("social_protection_profile", lambda conn: place_profile(
            conn, namespace, scopes=who()[1], as_of=as_of, place_id=place_id, area=area), namespace=namespace)

    @mcp.tool()
    def export_social_protection_profile(namespace: str, place_id: str | None = None, area: dict | None = None,
                                         as_of: str | None = None) -> dict:
        """The profile as a noesis-evidence-bundle-v1: every item cited with source, record revision and as-of
        time; exclusions and the minimisation decision in its root receipt."""
        from src.kb.social_protection_queries import export_profile, place_profile

        return run_tool("export_social_protection_profile", lambda conn: export_profile(place_profile(
            conn, namespace, scopes=who()[1], as_of=as_of, place_id=place_id, area=area)), namespace=namespace)

    @mcp.tool()
    def list_social_protection_identity(namespace: str, kind: str | None = None, state: str | None = None) -> dict:
        """Place and function assertions with their review state, unmatched areas and functions with no accepted
        relation (queryable natively)."""
        from src.kb.social_protection_identity import SocialProtectionIdentity

        def op(conn):
            identity = SocialProtectionIdentity(conn, initialize=False)
            return {"assertions": identity.assertions(namespace, scopes=who()[1], kind=kind, state=state),
                    "unmatched": identity.unmatched(namespace, scopes=who()[1]),
                    "unrelated_functions": identity.unrelated_functions(namespace)}

        return run_tool("list_social_protection_identity", op, namespace=namespace)

    @mcp.tool()
    def list_social_protection_links(namespace: str, series_id: str | None = None, kind: str | None = None) -> dict:
        """Links with their basis and pinned revisions; provider_absent and target_not_held reported, never
        dropped; COFOG links carry their distinct-concept note."""
        from src.kb.social_protection_links import SocialProtectionLinks

        return run_tool("list_social_protection_links", lambda conn: {"links": SocialProtectionLinks(
            conn, initialize=False).links(namespace, scopes=who()[1], series_id=series_id, kind=kind)},
            namespace=namespace)

    @mcp.tool()
    def poll_social_protection_monitor(namespace: str, subscription_id: str, cursor: str = "") -> dict:
        """Poll monitor events after a cursor."""
        from src.kb.social_protection_monitoring import SocialProtectionMonitor

        return run_tool("poll_social_protection_monitor", lambda conn: SocialProtectionMonitor(
            conn, initialize=False).poll(subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor),
            namespace=namespace)

    @mcp.tool()
    def register_social_protection_schemas(namespace: str) -> dict:
        """Register noesis-social-protection-record-v2 in the schema registry."""
        from src.kb.social_protection_records import register_schemas

        return run_tool("register_social_protection_schemas", lambda conn: {"modules": register_schemas(
            conn, principal_id=who()[0], scopes=who()[1])}, namespace=namespace, write=True)

    @mcp.tool()
    def propose_social_protection_place_matches(namespace: str, geo_namespace: str = "geo") -> dict:
        """Propose place matches by published ISO code (names are context only); none accepted."""
        from src.kb.social_protection_identity import SocialProtectionIdentity

        return run_tool("propose_social_protection_place_matches", lambda conn: SocialProtectionIdentity(
            conn).propose_places(namespace, geo_namespace=geo_namespace, principal_id=who()[0], scopes=who()[1]),
            namespace=namespace, write=True)

    @mcp.tool()
    def propose_social_protection_function_relations(namespace: str) -> dict:
        """Propose functions of different publishers that state the same published label as related (confidence
        low); never merged or re-classified; COFOG stays distinct."""
        from src.kb.social_protection_identity import SocialProtectionIdentity

        return run_tool("propose_social_protection_function_relations", lambda conn: SocialProtectionIdentity(
            conn).propose_function_relations(namespace, principal_id=who()[0], scopes=who()[1]),
            namespace=namespace, write=True)

    @mcp.tool()
    def record_social_protection_function_relation(namespace: str, left: dict, right: dict, statement: str,
                                                   cited: list[dict]) -> dict:
        """State, with citations, that one publisher's function ({scheme, code}) is related to another publisher's;
        proposed for review by another principal; a COFOG function is refused (distinct concept)."""
        from src.kb.social_protection_identity import SocialProtectionIdentity

        return run_tool("record_social_protection_function_relation", lambda conn: SocialProtectionIdentity(
            conn).propose_function_relation(namespace, left, right, cited=cited, statement=statement,
                                            principal_id=who()[0], scopes=who()[1]), namespace=namespace, write=True)

    @mcp.tool()
    def review_social_protection_identity(namespace: str, assertion_id: str, decision: str, reason: str,
                                          place_id: str | None = None) -> dict:
        """Accept or reject another principal's assertion with a reason (an entity identity decision)."""
        from src.kb.social_protection_identity import SocialProtectionIdentity

        return run_tool("review_social_protection_identity", lambda conn: SocialProtectionIdentity(conn).review(
            namespace, assertion_id, decision, reason, principal_id=who()[0], scopes=who()[1], place_id=place_id),
            namespace=namespace, write=True)

    @mcp.tool()
    def revert_social_protection_identity(namespace: str, assertion_id: str, reason: str) -> dict:
        """Revert an accepted or rejected assertion; the series stay as published."""
        from src.kb.social_protection_identity import SocialProtectionIdentity

        return run_tool("revert_social_protection_identity", lambda conn: SocialProtectionIdentity(conn).revert(
            namespace, assertion_id, reason, principal_id=who()[0], scopes=who()[1]), namespace=namespace, write=True)

    @mcp.tool()
    def link_social_protection_series(namespace: str, demographic_namespace: str = "global",
                                      public_finance_namespace: str = "global") -> dict:
        """Link series to Demographics population denominators and to COFOG social-protection expenditure in
        Public finance by shared code or accepted match; absences reported, nothing computed from linked series.
        Exclusions: no nowcasting, no filled years, no blending of ESSPROS, SOCX and ILO figures, no re-classification
        of functions, no combination with COFOG, no per-capita or share figures of our own, no derived figures and no
        forecasts."""
        from src.kb.social_protection_links import SocialProtectionLinks

        def op(conn):
            links = SocialProtectionLinks(conn)
            return {"denominators": links.link_demographics(namespace, principal_id=who()[0], scopes=who()[1],
                                                            demographic_namespace=demographic_namespace),
                    "cofog": links.link_public_finance(namespace, principal_id=who()[0], scopes=who()[1],
                                                       public_finance_namespace=public_finance_namespace)}

        return run_tool("link_social_protection_series", op, namespace=namespace, write=True)

    @mcp.tool()
    def record_social_protection_comparability(namespace: str, series_id: str, relation: str, statement: str,
                                               cited: list[dict], other_series_id: str | None = None,
                                               periods: list[str] | None = None) -> dict:
        """Propose a cited comparability note for a series or a pair; nothing is harmonised or merged."""
        from src.kb.social_protection_store import SocialProtectionStore

        return run_tool("record_social_protection_comparability", lambda conn: SocialProtectionStore(
            conn).record_note(namespace, series_id, relation, statement, other_series_id=other_series_id,
                              periods=periods or [], cited=cited, principal_id=who()[0], scopes=who()[1]),
            namespace=namespace, write=True)

    @mcp.tool()
    def review_social_protection_comparability(namespace: str, note_id: str, decision: str, reason: str) -> dict:
        """Accept, reject or revert a recorded comparability note (another principal reviews)."""
        from src.kb.social_protection_store import SocialProtectionStore

        return run_tool("review_social_protection_comparability", lambda conn: SocialProtectionStore(
            conn).review_note(namespace, note_id, decision, reason, principal_id=who()[0], scopes=who()[1]),
            namespace=namespace, write=True)

    @mcp.tool()
    def create_social_protection_monitor(namespace: str, request_key: str, target: dict,
                                         delivery: dict | None = None) -> dict:
        """Subscribe to a series, a provider, a measure, a function or a place: new releases, revisions,
        restatements, definition changes and removals (record changes, not assessments)."""
        from src.kb.social_protection_monitoring import SocialProtectionMonitor

        return run_tool("create_social_protection_monitor", lambda conn: SocialProtectionMonitor(conn).create(
            namespace, request_key, target=target, delivery=delivery, principal_id=who()[0], scopes=who()[1]),
            namespace=namespace, write=True)

    @mcp.tool()
    def run_social_protection_monitor(namespace: str, subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate a monitor at a committed watermark (newest by default); a replay creates no new notices."""
        from src.kb.social_protection_monitoring import SocialProtectionMonitor

        return run_tool("run_social_protection_monitor", lambda conn: SocialProtectionMonitor(conn).run(
            subscription_id, watermark, principal_id=who()[0], scopes=who()[1]), namespace=namespace, write=True)
