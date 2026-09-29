"""Weather pack entry points: as-of observations, forecasts as issued, warnings in force, verification, identity.

Acquisition runs through the shared source-pack tools (pack ``weather-operational``).
Every answer states its knowledge cutoff and sources. Warnings are quoted as
issued. No tool produces a forecast of its own or gives safety, travel,
agricultural or aviation advice. Before any weather source has run in a
namespace, every entry point returns ``not_ready``.
"""

from __future__ import annotations

WEATHER_WRITES = {
    # These two record replayable spatial receipts (Geospatial proximity/containment), a local mutation.
    "weather_observations",
    "warnings_in_force",
    "verify_published_forecasts",
    "propose_weather_station_matches",
    "review_weather_station_match",
    "revert_weather_station_match",
    "attach_weather_evidence",
    "review_weather_evidence_link",
    "create_weather_monitor",
    "run_weather_monitor",
    "register_weather_schemas",
}
WEATHER_READS = {
    "forecast_as_issued",
    "forecast_evolution",
    "replay_weather_verification",
    "list_weather_station_matches",
    "poll_weather_monitor",
    "weather_source_contracts",
}
WEATHER_TOOLS = WEATHER_WRITES | WEATHER_READS
READ = "knowledge:weather:read"
WRITE = "knowledge:weather:write"
REVIEW = "knowledge:weather:review"
# Every scope each tool always uses; scopes needed only by optional arguments are checked when used.
WEATHER_SCOPES = {
    "weather_observations": [READ],
    "warnings_in_force": [READ, "knowledge:geospatial:calculate"],
    "forecast_as_issued": [READ],
    "forecast_evolution": [READ],
    "verify_published_forecasts": [READ, WRITE],
    "replay_weather_verification": [READ],
    "propose_weather_station_matches": [READ, WRITE],
    "review_weather_station_match": [READ, REVIEW],
    "revert_weather_station_match": [READ, REVIEW],
    "list_weather_station_matches": [READ],
    "attach_weather_evidence": [WRITE],
    "review_weather_evidence_link": [REVIEW],
    "create_weather_monitor": [WRITE, "knowledge:subscriptions:write"],
    "run_weather_monitor": [
        READ,
        "knowledge:subscriptions:read",
        "knowledge:subscriptions:write",
    ],
    "poll_weather_monitor": [READ, "knowledge:subscriptions:read"],
    "weather_source_contracts": [READ],
    "register_weather_schemas": [WRITE, "knowledge:schema:register"],
}
PLACE_SCOPE = "knowledge:geospatial:calculate"


def required_scopes(tool_name, mutability):
    return list(
        WEATHER_SCOPES.get(tool_name, [WRITE if mutability == "write" else READ])
    )


def _place(point, place_id, radius_m):
    if point is None and place_id is None:
        return None
    place = {"point": point} if point is not None else {"place_id": place_id}
    if radius_m is not None:
        place["radius_m"] = radius_m
    return place


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def checked(tool_name, operation, *, optional=()):
        """Run with every scope the tool always uses present, plus those its given optional arguments need."""

        def run(conn):
            from src.kb.weather_store import WeatherError

            scopes = who()[1]
            missing = [
                s for s in [*WEATHER_SCOPES[tool_name], *optional] if s not in scopes
            ]
            if missing and "operator" not in scopes:
                raise WeatherError("unauthorized", f"{', '.join(missing)} required")
            return operation(conn)

        return run

    def any_ready(conn):
        from src.kb.weather_store import TABLES, WeatherError, _table

        if (
            not all(_table(conn, t) for t in TABLES)
            or not conn.execute(
                "SELECT 1 FROM weather_provider_state WHERE last_success_ms IS NOT NULL LIMIT 1"
            ).fetchone()
        ):
            raise WeatherError("not_ready", "no weather source has run yet")

    @mcp.tool()
    def weather_observations(
        namespace: str,
        window_from: str,
        window_to: str,
        station: str | None = None,
        point: list[float] | None = None,
        place_id: str | None = None,
        radius_m: float | None = None,
        knowledge_cutoff: str | None = None,
    ) -> dict:
        """Observation reports at a station (`dwd:00433`, `aviationweather:EDDB`) or near a place, as recorded by
        the knowledge cutoff: QC flag verbatim with its common state, corrections, raw METAR text and the location
        vintage valid at each report's time. A time without data answers "no report on record"; nothing is inferred.
        """
        from src.kb.weather_queries import WeatherQueries

        place = _place(point, place_id, radius_m)
        return safe(
            checked(
                "weather_observations",
                lambda conn: WeatherQueries(conn).observations(
                    namespace,
                    scopes=who()[1],
                    principal_id=who()[0],
                    window_from=window_from,
                    window_to=window_to,
                    station=station,
                    place=place,
                    knowledge_cutoff=knowledge_cutoff,
                ),
                optional=(PLACE_SCOPE,) if place else (),
            ),
            write=True,
            required_scope=READ,
        )

    @mcp.tool()
    def forecast_as_issued(
        namespace: str,
        valid_time: str,
        issued_before: str,
        station: str | None = None,
        point: list[float] | None = None,
        place_id: str | None = None,
        radius_m: float | None = None,
        parameter: str | None = None,
        knowledge_cutoff: str | None = None,
    ) -> dict:
        """The published forecast for a valid time as issued at or before `issued_before`: per provider, product and
        model the latest issuance, with lead time and the issuer. Published forecasts only; Noesis forecasts nothing.
        """
        from src.kb.weather_queries import WeatherQueries

        return safe(
            checked(
                "forecast_as_issued",
                lambda conn: WeatherQueries(conn).forecast_as_issued(
                    namespace,
                    scopes=who()[1],
                    valid_time=valid_time,
                    issued_before=issued_before,
                    station=station,
                    place=_place(point, place_id, radius_m),
                    parameter=parameter,
                    knowledge_cutoff=knowledge_cutoff,
                ),
            ),
            required_scope=READ,
        )

    @mcp.tool()
    def forecast_evolution(
        namespace: str,
        valid_time: str,
        station: str | None = None,
        point: list[float] | None = None,
        place_id: str | None = None,
        radius_m: float | None = None,
        parameter: str | None = None,
        knowledge_cutoff: str | None = None,
    ) -> dict:
        """Every recorded issuance that forecast one valid time (forecast evolution), oldest first, with lead times."""
        from src.kb.weather_queries import WeatherQueries

        return safe(
            checked(
                "forecast_evolution",
                lambda conn: WeatherQueries(conn).forecast_evolution(
                    namespace,
                    scopes=who()[1],
                    valid_time=valid_time,
                    station=station,
                    place=_place(point, place_id, radius_m),
                    parameter=parameter,
                    knowledge_cutoff=knowledge_cutoff,
                ),
            ),
            required_scope=READ,
        )

    @mcp.tool()
    def warnings_in_force(
        namespace: str,
        as_of: str,
        point: list[float] | None = None,
        place_id: str | None = None,
        knowledge_cutoff: str | None = None,
    ) -> dict:
        """Warnings in force at a place as of a time: CAP Alert/Update/Cancel chains threaded by references and
        evaluated at `as_of` over the areas containing the place (replayable containment receipt). Warnings are
        quoted as issued with their issuer and validity; no advice is added."""
        from src.kb.weather_queries import WeatherQueries

        return safe(
            checked(
                "warnings_in_force",
                lambda conn: WeatherQueries(conn).warnings_in_force(
                    namespace,
                    scopes=who()[1],
                    principal_id=who()[0],
                    place=_place(point, place_id, None) or {},
                    as_of=as_of,
                    knowledge_cutoff=knowledge_cutoff,
                ),
            ),
            write=True,
            required_scope=READ,
        )

    @mcp.tool()
    def verify_published_forecasts(
        namespace: str,
        station: str,
        parameter: str,
        period_from: str,
        period_to: str,
        tolerance_s: int = 600,
        providers: list[str] | None = None,
        thresholds: list[dict] | None = None,
        knowledge_cutoff: str | None = None,
    ) -> dict:
        """Compare published forecasts with the observations later recorded at the same (or an equivalent or
        declared) station: bias, MAE, RMSE, Brier score and user-threshold hit rate / false-alarm ratio with
        definitions, sample sizes and exclusions, by provider, lead bucket and period. No ranking; a replayable
        receipt pins every revision used. Requires the optional weather-verification feature when composed."""
        from src.kb.weather_bundle import require_feature
        from src.kb.weather_verification import ForecastVerification

        def run(conn):
            require_feature(conn, "weather-verification")
            return ForecastVerification(conn).verify(
                namespace,
                station=station,
                parameter=parameter,
                period_from=period_from,
                period_to=period_to,
                tolerance_s=tolerance_s,
                providers=providers,
                thresholds=thresholds,
                knowledge_cutoff=knowledge_cutoff,
                principal_id=who()[0],
                scopes=who()[1],
            )

        return safe(
            checked("verify_published_forecasts", run), write=True, required_scope=WRITE
        )

    @mcp.tool()
    def replay_weather_verification(namespace: str, run_id: str) -> dict:
        """Recompute a recorded verification run from its pinned revisions and check it against its receipt."""
        from src.kb.weather_verification import ForecastVerification

        def run(conn):
            from src.kb.weather_store import require_ready

            require_ready(conn, namespace)
            return ForecastVerification(conn, initialize=False).replay(
                namespace, run_id, scopes=who()[1]
            )

        return safe(checked("replay_weather_verification", run), required_scope=READ)

    @mcp.tool()
    def propose_weather_station_matches(namespace: str) -> dict:
        """Link stations by source-stated identifiers and propose cross-provider proximity candidates for review;
        nothing is merged, same-provider ids are never proposed and a reused id stays split into sites."""
        from src.kb.weather_identity import WeatherStationIdentity

        return safe(
            checked(
                "propose_weather_station_matches",
                lambda conn: WeatherStationIdentity(conn).propose(
                    namespace, principal_id=who()[0], scopes=who()[1]
                ),
            ),
            write=True,
            required_scope=WRITE,
        )

    @mcp.tool()
    def review_weather_station_match(
        namespace: str, candidate_id: str, decision: str, reason: str
    ) -> dict:
        """Accept or reject a proposed station match (another principal than the proposer) as an entity identity
        decision; the review inbox can route the same decision."""
        from src.kb.weather_identity import WeatherStationIdentity

        return safe(
            checked(
                "review_weather_station_match",
                lambda conn: WeatherStationIdentity(conn).review(
                    namespace,
                    candidate_id,
                    decision,
                    reason,
                    principal_id=who()[0],
                    scopes=who()[1],
                ),
            ),
            write=True,
            required_scope=REVIEW,
        )

    @mcp.tool()
    def revert_weather_station_match(
        namespace: str, candidate_id: str, reason: str
    ) -> dict:
        """Revert a reviewed station match; the candidate becomes reverted and never reactivates."""
        from src.kb.weather_identity import WeatherStationIdentity

        return safe(
            checked(
                "revert_weather_station_match",
                lambda conn: WeatherStationIdentity(conn).revert(
                    namespace,
                    candidate_id,
                    reason,
                    principal_id=who()[0],
                    scopes=who()[1],
                ),
            ),
            write=True,
            required_scope=REVIEW,
        )

    @mcp.tool()
    def list_weather_station_matches(
        namespace: str, station: str | None = None
    ) -> dict:
        """Station match candidates with their decision state and the source-stated identifier links."""
        from src.kb.weather_identity import WeatherStationIdentity

        def run(conn):
            from src.kb.weather_store import require_ready

            require_ready(conn, namespace)
            identity = WeatherStationIdentity(conn, initialize=False)
            return {
                "candidates": identity.candidates(
                    namespace, scopes=who()[1], station=station
                ),
                "stated_links": identity.stated_links(namespace),
            }

        return safe(checked("list_weather_station_matches", run), required_scope=READ)

    @mcp.tool()
    def attach_weather_evidence(
        namespace: str,
        revision_id: str,
        target_kind: str,
        target_id: str,
        note: str,
        target_namespace: str | None = None,
    ) -> dict:
        """Propose a warning or observation revision as cited evidence beside an environment place dossier or an
        event record; another principal reviews it and it can be reverted. Nothing is written into the target."""
        from src.kb.weather_links import WeatherClimateLinks

        def run(conn):
            from src.kb.weather_store import require_ready

            require_ready(conn, namespace)
            return WeatherClimateLinks(conn).attach(
                namespace,
                revision_id=revision_id,
                target_kind=target_kind,
                target_id=target_id,
                note=note,
                principal_id=who()[0],
                scopes=who()[1],
                target_namespace=target_namespace,
            )

        return safe(
            checked("attach_weather_evidence", run), write=True, required_scope=WRITE
        )

    @mcp.tool()
    def review_weather_evidence_link(
        namespace: str, link_id: str, decision: str, reason: str
    ) -> dict:
        """Accept, reject or revert a weather evidence link (`accept` | `reject` | `revert`)."""
        from src.kb.weather_links import WeatherClimateLinks

        def run(conn):
            links = WeatherClimateLinks(conn, initialize=False)
            if decision == "revert":
                return links.revert(
                    namespace, link_id, reason, principal_id=who()[0], scopes=who()[1]
                )
            return links.review(
                namespace,
                link_id,
                decision,
                reason,
                principal_id=who()[0],
                scopes=who()[1],
            )

        return safe(
            checked("review_weather_evidence_link", run),
            write=True,
            required_scope=REVIEW,
        )

    @mcp.tool()
    def create_weather_monitor(
        namespace: str, request_key: str, target: dict, delivery: dict | None = None
    ) -> dict:
        """A subscription on a place (warnings by containment), a station, a provider + parameter or a warning event
        and severity; events warning_issued/updated/cancelled, forecast_issued, observation_corrected,
        station_relocated. No new scheduler; notifications quote the issuer and add no advice."""
        from src.kb.weather_monitoring import WeatherMonitor

        return safe(
            checked(
                "create_weather_monitor",
                lambda conn: WeatherMonitor(conn).create(
                    namespace,
                    request_key,
                    target=target,
                    principal_id=who()[0],
                    scopes=who()[1],
                    delivery=delivery,
                ),
            ),
            write=True,
            required_scope=WRITE,
        )

    @mcp.tool()
    def run_weather_monitor(subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate a weather monitor at a committed watermark; replaying a watermark adds no events."""
        from src.kb.weather_monitoring import WeatherMonitor

        def run(conn):
            any_ready(conn)
            return WeatherMonitor(conn).run(
                subscription_id, watermark, principal_id=who()[0], scopes=who()[1]
            )

        return safe(
            checked("run_weather_monitor", run),
            write=True,
            required_scope="knowledge:subscriptions:write",
        )

    @mcp.tool()
    def poll_weather_monitor(subscription_id: str, cursor: str = "") -> dict:
        """Poll a weather monitor's events through the subscription delivery path."""
        from src.kb.weather_monitoring import WeatherMonitor

        def run(conn):
            any_ready(conn)
            return WeatherMonitor(conn, initialize=False).poll(
                subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor
            )

        return safe(
            checked("poll_weather_monitor", run),
            required_scope="knowledge:subscriptions:read",
        )

    @mcp.tool()
    def weather_source_contracts(namespace: str) -> dict:
        """Access contracts, attribution, decisions and live-verification state per weather source, with each
        provider's last run in the namespace (never a claim of live coverage)."""
        from src.ingestion.weather_sources import LIVE_VERIFICATION, PROVIDER_CONTRACTS
        from src.kb.weather_store import WeatherStore, authorize

        def run(conn):
            authorize(namespace, who()[1], READ)
            store = WeatherStore(conn, initialize=False)
            return {
                "contracts": PROVIDER_CONTRACTS,
                "live_verification": LIVE_VERIFICATION,
                "provider_state": {
                    p: store.provider_state(namespace, p)
                    for p in sorted(PROVIDER_CONTRACTS)
                },
                "audit": "docs/development/weather-evidence/source-audit.md",
            }

        return safe(checked("weather_source_contracts", run), required_scope=READ)

    @mcp.tool()
    def register_weather_schemas() -> dict:
        """Register noesis-weather-record-v1 in the shared schema registry (idempotent per version)."""
        from src.kb.weather_records import register_schemas

        return safe(
            checked(
                "register_weather_schemas",
                lambda conn: {
                    "registered": register_schemas(
                        conn, principal_id=who()[0], scopes=who()[1]
                    )
                },
            ),
            write=True,
            required_scope=WRITE,
        )
