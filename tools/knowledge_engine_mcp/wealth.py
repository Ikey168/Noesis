"""Society bundle ``society.wealth`` entry points (#2882): wealth distribution series.

Acquisition runs through the shared source-pack tools (pack ``society-wealth-distribution``: sources
``wid-wealth-distribution``, ``ecb-distributional-wealth-accounts`` and ``fed-distributional-financial-accounts``; each
an optional Society feature, default off). These tools answer wealth figures for a place as of a release, a series'
history with comparability notes, recorded comparability notes and a cited evidence-bundle export. Every namespaced
tool checks the Society bundle's enablement first.

Exclusions (declared by every answer): no nowcasting, no filled or interpolated periods, no blending of WID, DWA and
DFA figures, no re-harmonising of wealth concepts, population units or groups, no shares, ratios or averages of our
own, no correction of a publisher's model-based estimates and no forecasts. Every answer is checked against the WD01
minimisation decision (no person- or household-level field, no derived key) before it leaves.
"""

READ = "knowledge:wealth:read"
WRITE = "knowledge:wealth:write"
REVIEW = "knowledge:wealth:review"

WEALTH_WRITES = {
    "register_wealth_schemas",
    "record_wealth_comparability",
    "review_wealth_comparability",
}
WEALTH_READS = {
    "wealth_source_contracts",
    "wealth_readiness",
    "list_wealth_series",
    "wealth_indicator_for_place",
    "wealth_series_history",
    "wealth_comparability_notes",
    "wealth_profile",
    "export_wealth_profile",
}
WEALTH_TOOLS = WEALTH_WRITES | WEALTH_READS
WEALTH_SCOPES = {
    "wealth_source_contracts": [],
    "wealth_readiness": [READ],
    "list_wealth_series": [READ],
    "wealth_indicator_for_place": [READ],
    "wealth_series_history": [READ],
    "wealth_comparability_notes": [READ],
    "wealth_profile": [READ],
    "export_wealth_profile": [READ],
    "register_wealth_schemas": [WRITE, "knowledge:schema:register"],
    "record_wealth_comparability": [WRITE],
    "review_wealth_comparability": [REVIEW],
}


def required_scopes(tool_name, mutability):
    return WEALTH_SCOPES.get(tool_name, [WRITE if mutability == "write" else READ])


def _require(scopes, *required):
    from src.kb.wealth_distribution_records import WealthError

    missing = [s for s in required if s not in scopes and "operator" not in scopes]
    if missing:
        raise WealthError("unauthorized", f"{', '.join(missing)} required")


def _declared(answer):
    """Every answer is checked against the minimisation decision and leaves with the exclusions declared (an evidence
    bundle carries them in its root receipt, so its sealed contract is left untouched)."""
    from src.kb.wealth_distribution_records import (
        EXCLUSIONS,
        MINIMISATION,
        WealthError,
        forbidden_paths,
        personal_data_paths,
    )

    if not isinstance(answer, dict):
        return answer
    if personal_data_paths(answer) or forbidden_paths(answer):
        raise WealthError("minimisation", "an answer would carry a person-level, household-level or derived field")
    if answer.get("contract") == "noesis-evidence-bundle-v1":
        return answer
    return {**answer, "exclusions": list(EXCLUSIONS), "minimisation": answer.get("minimisation")
            or MINIMISATION["decision"]}


def register(mcp, safe, context):
    def who():
        return context()[0], set(context()[1])

    def run_tool(tool, operation, *, namespace=None, write=False):
        """Checks every declared scope and the bundle's enablement, runs the operation and declares exclusions."""
        scopes = WEALTH_SCOPES[tool]

        def run(conn):
            _require(who()[1], *scopes)
            if namespace is not None:
                from src.kb.society_bundle import require_enabled

                require_enabled(conn, namespace)
            return _declared(operation(conn))

        return safe(run, write=write, required_scope=scopes[0] if scopes else None)

    @mcp.tool()
    def wealth_source_contracts() -> dict:
        """Per-source access decisions (WID, ECB DWA, Fed DFA unverified-live; ECB HFCS tables and Bundesbank PHF
        not-implemented), licences, limits, revision models, methods, caps, bounded coverage and the minimisation
        decision.
        Exclusions: no nowcasting, no filled periods, no blending of WID, DWA and DFA figures, no re-harmonising, no
        shares or ratios of our own, no correction of model-based estimates and no forecasts."""
        from src.ingestion.wealth_distribution_sources import (
            AUDIT,
            BOUNDED_COVERAGE,
            CAPS,
            LIVE_VERIFICATION,
            NEVER_SENTENCE,
            PACING,
            PROVIDER_CONTRACTS,
        )
        from src.kb.wealth_distribution_records import MINIMISATION

        return _declared({"contracts": PROVIDER_CONTRACTS, "live_verification": LIVE_VERIFICATION, "caps": CAPS,
                          "pacing": PACING, "bounded_coverage": BOUNDED_COVERAGE, "minimisation": MINIMISATION,
                          "never": NEVER_SENTENCE, "audit": AUDIT})

    @mcp.tool()
    def wealth_readiness(namespace: str = "global") -> dict:
        """ready / fixture-only / stale / unavailable / feature-disabled / not-implemented per source; live
        verification reported separately."""
        from src.kb.wealth_distribution_records import readiness

        return run_tool("wealth_readiness", lambda conn: readiness(conn, namespace))

    @mcp.tool()
    def list_wealth_series(namespace: str, provider: str | None = None, indicator: str | None = None,
                           area: dict | None = None, groups: list[str] | None = None) -> dict:
        """Series with source, dataset, indicator, wealth concept, group, unit, population, method (model-based
        estimates say so), place and frequency."""
        from src.kb.wealth_distribution_records import authorize
        from src.kb.wealth_distribution_store import WealthStore

        def op(conn):
            authorize(namespace, who()[1], READ)
            codes = [(area["scheme"], str(area["code"]).upper())] if area else None
            return {"series": WealthStore(conn, initialize=False).find_series(
                namespace, provider=provider, indicator=indicator, area_codes=codes, groups=groups)}

        return run_tool("list_wealth_series", op, namespace=namespace)

    @mcp.tool()
    def wealth_indicator_for_place(namespace: str, area: dict, indicator: str | None = None,
                                   groups: list[str] | None = None, as_of: str | None = None,
                                   reference_period: str | None = None) -> dict:
        """WID, ECB DWA and Fed DFA values for a place ({scheme: iso3166-1-alpha2, code}) released by a date, side by
        side with definitions, groups, units, populations, methods, flags and cited vintages; pairs from different
        sources state their notes or comparability_unknown.
        Exclusions: no nowcasting, no filled periods, no blending of WID, DWA and DFA figures, no re-harmonising, no
        shares or ratios of our own, no correction of model-based estimates and no forecasts."""
        from src.kb.wealth_distribution_queries import WealthQueries
        from src.kb.wealth_distribution_records import enabled_providers

        return run_tool("wealth_indicator_for_place", lambda conn: WealthQueries(conn).indicator_for_place(
            namespace, scopes=who()[1], area=area, indicator=indicator, groups=groups, as_of=as_of,
            reference_period=reference_period, enabled_providers=enabled_providers(conn)), namespace=namespace)

    @mcp.tool()
    def wealth_series_history(namespace: str, series_id: str) -> dict:
        """Every vintage of a series with new, revised and dropped periods, model estimates the source replaced,
        method and definition changes, removals and the notes per release pair (comparability_unknown when none
        applies).
        Exclusions: no nowcasting, no filled periods, no blending of WID, DWA and DFA figures, no re-harmonising, no
        shares or ratios of our own, no correction of model-based estimates and no forecasts."""
        from src.kb.wealth_distribution_queries import WealthQueries

        return run_tool("wealth_series_history", lambda conn: WealthQueries(conn).series_history(
            namespace, series_id, scopes=who()[1]), namespace=namespace)

    @mcp.tool()
    def wealth_comparability_notes(namespace: str, series_id: str, include_inactive: bool = False) -> dict:
        """Source-stated scope notes and reviewer-recorded notes for a series or its pairs."""
        from src.kb.wealth_distribution_records import authorize
        from src.kb.wealth_distribution_store import WealthStore

        def op(conn):
            authorize(namespace, who()[1], READ)
            return {"notes": WealthStore(conn, initialize=False).notes(namespace, series_id,
                                                                      active_only=not include_inactive)}

        return run_tool("wealth_comparability_notes", op, namespace=namespace)

    @mcp.tool()
    def wealth_profile(namespace: str, area: dict, as_of: str | None = None) -> dict:
        """A place's wealth figures from each selected source side by side as of a date.
        Exclusions: no nowcasting, no filled periods, no blending of WID, DWA and DFA figures, no re-harmonising, no
        shares or ratios of our own, no correction of model-based estimates and no forecasts."""
        from src.kb.wealth_distribution_queries import place_profile

        return run_tool("wealth_profile", lambda conn: place_profile(conn, namespace, scopes=who()[1], area=area,
                                                                     as_of=as_of), namespace=namespace)

    @mcp.tool()
    def export_wealth_profile(namespace: str, area: dict, as_of: str | None = None) -> dict:
        """The profile as a noesis-evidence-bundle-v1: every item cited with source, record revision and as-of
        time; exclusions and the minimisation decision in its root receipt."""
        from src.kb.wealth_distribution_queries import export_profile, place_profile

        return run_tool("export_wealth_profile", lambda conn: export_profile(place_profile(
            conn, namespace, scopes=who()[1], area=area, as_of=as_of)), namespace=namespace)

    @mcp.tool()
    def register_wealth_schemas(namespace: str) -> dict:
        """Register noesis-wealth-distribution-record-v2 in the schema registry."""
        from src.kb.wealth_distribution_records import register_schemas

        return run_tool("register_wealth_schemas", lambda conn: {"modules": register_schemas(
            conn, principal_id=who()[0], scopes=who()[1])}, namespace=namespace, write=True)

    @mcp.tool()
    def record_wealth_comparability(namespace: str, series_id: str, relation: str, statement: str,
                                    cited: list[dict], other_series_id: str | None = None,
                                    periods: list[str] | None = None) -> dict:
        """Propose a cited comparability note for a series or a pair; nothing is harmonised or merged."""
        from src.kb.wealth_distribution_store import WealthStore

        return run_tool("record_wealth_comparability", lambda conn: WealthStore(conn).record_note(
            namespace, series_id, relation, statement, other_series_id=other_series_id, periods=periods or [],
            cited=cited, principal_id=who()[0], scopes=who()[1]), namespace=namespace, write=True)

    @mcp.tool()
    def review_wealth_comparability(namespace: str, note_id: str, decision: str, reason: str) -> dict:
        """Accept, reject or revert a recorded comparability note (another principal reviews)."""
        from src.kb.wealth_distribution_store import WealthStore

        return run_tool("review_wealth_comparability", lambda conn: WealthStore(conn).review_note(
            namespace, note_id, decision, reason, principal_id=who()[0], scopes=who()[1]), namespace=namespace,
            write=True)
