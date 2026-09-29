"""Astronomy and Space entry points: as-of object histories, orbit vintages, dispositions, launches, alerts.

Acquisition runs through the shared source-pack tools (pack
``astronomy-and-space``). Every answer states its knowledge cutoff and quotes
the publishers: Noesis determines no orbit, computes no ephemeris or
conjunction, issues no impact-risk verdict, dispositions no exoplanet and
gives no operational space-weather advice.
"""

from typing import Any

READ = "knowledge:astronomy:read"
WRITE = "knowledge:astronomy:write"
REVIEW = "knowledge:astronomy:review"
ASTRONOMY_WRITES = {
    "propose_astronomy_identity_matches",
    "review_astronomy_identity_match",
    "revert_astronomy_identity_match",
    "review_astronomy_launch_site",
    "link_astronomy_citations",
    "revert_astronomy_citation",
    "create_astronomy_monitor",
    "run_astronomy_monitor",
}
ASTRONOMY_READS = {
    "small_body_history",
    "orbit_solution_as_of",
    "impact_risk_listing_as_of",
    "exoplanet_status_as_of",
    "lookup_launches",
    "orbital_object_history",
    "space_weather_alerts",
    "list_astronomy_identity_candidates",
    "astronomy_citations",
    "poll_astronomy_monitor",
}
ASTRONOMY_TOOLS = ASTRONOMY_WRITES | ASTRONOMY_READS
# Every scope a tool always reads or writes. Scopes only an optional argument needs (a Geospatial namespace to
# resolve launch sites against) are checked when that argument is used.
ASTRONOMY_SCOPES = {
    # Proposing returns the namespace's candidates, so it always reads them too.
    "propose_astronomy_identity_matches": [READ, WRITE],
    "review_astronomy_identity_match": [REVIEW],
    "revert_astronomy_identity_match": [REVIEW],
    "review_astronomy_launch_site": [READ, "knowledge:geospatial:review"],
    "link_astronomy_citations": [READ, WRITE],
    "revert_astronomy_citation": [REVIEW],
    "create_astronomy_monitor": [READ, "knowledge:subscriptions:write"],
    # Running reads the monitor (subscriptions:read) and records its evaluation (subscriptions:write).
    "run_astronomy_monitor": [
        READ,
        "knowledge:subscriptions:read",
        "knowledge:subscriptions:write",
    ],
    "poll_astronomy_monitor": [READ, "knowledge:subscriptions:read"],
}
OPTIONAL_SCOPES = {
    "geo_namespace": ["knowledge:geospatial:read", "knowledge:geospatial:write"]
}
QUERY_EXAMPLES = {
    "small_body_history": {
        "arguments": {
            "namespace": "astronomy",
            "designation": "2099 AB12",
            "as_of": "2099-04-30",
        },
        "semantics": "designations and MPC identifications published by the end of the day, each cited; "
        "not_yet_published before the first publication",
    },
    "orbit_solution_as_of": {
        "arguments": {
            "namespace": "astronomy",
            "designation": "2099 AB12",
            "as_of": "2099-05-10",
            "publisher": "JPL",
        },
        "semantics": "the latest solution the publisher had published, with epoch, arc, observation count and "
        "solution ID; other publishers listed separately, never averaged",
    },
    "exoplanet_status_as_of": {
        "arguments": {
            "namespace": "astronomy",
            "planet": "TOI-99902.01",
            "as_of": "2099-03-01",
        },
        "semantics": "the archive's disposition per table with its reference; later changes shown as later",
    },
    "space_weather_alerts": {
        "arguments": {
            "namespace": "astronomy",
            "window_from": "2099-09-01",
            "window_to": "2099-09-02",
        },
        "semantics": "SWPC products issued in the window, verbatim, with cancellations and extensions threaded",
    },
}


def required_scopes(tool_name, mutability):
    return ASTRONOMY_SCOPES.get(tool_name, [WRITE if mutability == "write" else READ])


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def checked(
        tool_name,
        operation,
        *,
        optional: dict[str, Any] | None = None,
        ready: bool = True,
    ):
        """Run with every scope the tool always uses present, plus those its optional arguments need."""

        def run(conn):
            from src.kb.astronomy_records import AstronomyError
            from src.kb.astronomy_store import AstronomyStore

            scopes = who()[1]
            needed = list(ASTRONOMY_SCOPES.get(tool_name, [READ]))
            for argument, value in (optional or {}).items():
                if value:
                    needed += OPTIONAL_SCOPES[argument]
            missing = [s for s in needed if s not in scopes]
            if missing and "operator" not in scopes:
                raise AstronomyError("unauthorized", f"{', '.join(missing)} required")
            if ready:
                AstronomyStore(conn, initialize=False).require_ready()
            return operation(conn)

        return run

    def queries(conn):
        from src.kb.astronomy_queries import AstronomyQueries

        return AstronomyQueries(conn)

    def read(tool_name, operation):
        return safe(
            checked(tool_name, operation),
            required_scope=ASTRONOMY_SCOPES.get(tool_name, [READ])[0],
        )

    def write(tool_name, operation, **kwargs):
        return safe(
            checked(tool_name, operation, **kwargs),
            write=True,
            required_scope=ASTRONOMY_SCOPES[tool_name][-1],
        )

    @mcp.tool()
    def small_body_history(
        namespace: str, designation: str, as_of: str, acquired_by_ms: int | None = None
    ) -> dict:
        """A small body's designations and MPC identifications known at a date (YYYY-MM-DD, end of day UTC), cited.

        Packed and unpacked designations, numbers and names are accepted; linked designations come only from what
        the MPC and JPL state. A date before the first publication answers not_yet_published, not absence. No orbit
        determination and no impact-risk verdict.
        """
        return read(
            "small_body_history",
            lambda conn: queries(conn).small_body_history(
                namespace,
                designation,
                as_of,
                scopes=who()[1],
                acquired_by_ms=acquired_by_ms,
            ),
        )

    @mcp.tool()
    def orbit_solution_as_of(
        namespace: str,
        designation: str,
        as_of: str,
        publisher: str | None = None,
        acquired_by_ms: int | None = None,
    ) -> dict:
        """The latest orbit solution each publisher (MPC, JPL) had published by a date, quoted with epoch, arc,
        observation count, uncertainty and solution ID; other publishers and earlier vintages listed separately.
        Values from different solutions are never averaged; Noesis determines and propagates no orbit."""
        return read(
            "orbit_solution_as_of",
            lambda conn: queries(conn).orbit_solution_as_of(
                namespace,
                designation,
                as_of,
                scopes=who()[1],
                publisher=publisher,
                acquired_by_ms=acquired_by_ms,
            ),
        )

    @mcp.tool()
    def impact_risk_listing_as_of(
        namespace: str, designation: str, as_of: str, acquired_by_ms: int | None = None
    ) -> dict:
        """The published Sentry listing for a small body at a date: listed or removed, with the listing date and
        the published figures quoted as text. Noesis issues no impact-risk, hazard or collision verdict."""
        return read(
            "impact_risk_listing_as_of",
            lambda conn: queries(conn).impact_risk_listing_as_of(
                namespace,
                designation,
                as_of,
                scopes=who()[1],
                acquired_by_ms=acquired_by_ms,
            ),
        )

    @mcp.tool()
    def exoplanet_status_as_of(
        namespace: str, planet: str, as_of: str, acquired_by_ms: int | None = None
    ) -> dict:
        """An exoplanet's (or TOI/KOI candidate's) disposition per NASA Exoplanet Archive table at a date, with the
        archive's reference, linked papers, listing state and any later change shown as later. The archive's
        disposition is quoted; Noesis validates and dispositions nothing."""
        return read(
            "exoplanet_status_as_of",
            lambda conn: queries(conn).exoplanet_status_as_of(
                namespace, planet, as_of, scopes=who()[1], acquired_by_ms=acquired_by_ms
            ),
        )

    @mcp.tool()
    def lookup_launches(
        namespace: str,
        provider: str | None = None,
        site: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        as_of: str | None = None,
        acquired_by_ms: int | None = None,
    ) -> dict:
        """Launches by GCAT provider code, site code or date window, with the outcome as coded by the source
        (native code kept), payloads, reviewed provider identity and the site's reviewed place."""
        return read(
            "lookup_launches",
            lambda conn: queries(conn).launches(
                namespace,
                scopes=who()[1],
                provider=provider,
                site=site,
                date_from=date_from,
                date_to=date_to,
                as_of=as_of,
                acquired_by_ms=acquired_by_ms,
            ),
        )

    @mcp.tool()
    def orbital_object_history(
        namespace: str,
        identifier: str,
        as_of: str | None = None,
        acquired_by_ms: int | None = None,
    ) -> dict:
        """An orbital object's catalogue history (launch, status, decay) by COSPAR, NORAD or GCAT JCAT, per
        catalogue with disagreements side by side. No positions, TLE propagation or conjunction screening."""
        return read(
            "orbital_object_history",
            lambda conn: queries(conn).orbital_object_history(
                namespace,
                identifier,
                scopes=who()[1],
                as_of=as_of,
                acquired_by_ms=acquired_by_ms,
            ),
        )

    @mcp.tool()
    def space_weather_alerts(
        namespace: str,
        window_from: str,
        window_to: str,
        product_type: str | None = None,
        scale: str | None = None,
        as_of: str | None = None,
        acquired_by_ms: int | None = None,
    ) -> dict:
        """NOAA SWPC alerts, watches, warnings and summaries issued in a window (by type and NOAA scale
        threshold), verbatim, with cancellations and extensions threaded. No operational advice."""
        return read(
            "space_weather_alerts",
            lambda conn: queries(conn).space_weather_alerts(
                namespace,
                window_from,
                window_to,
                scopes=who()[1],
                product_type=product_type,
                scale=scale,
                as_of=as_of,
                acquired_by_ms=acquired_by_ms,
            ),
        )

    @mcp.tool()
    def list_astronomy_identity_candidates(
        namespace: str, state: str | None = None
    ) -> dict:
        """Reviewable identity candidates (shared host identifiers, catalogue conflicts, organisation names)."""

        def run(conn):
            from src.kb.astronomy_identity import AstronomyIdentity

            candidates = AstronomyIdentity(conn, initialize=False).candidates(
                namespace, scopes=who()[1], state=state
            )
            return {"candidates": candidates, "n": len(candidates)}

        return read("list_astronomy_identity_candidates", run)

    @mcp.tool()
    def astronomy_citations(namespace: str, record_id: str | None = None) -> dict:
        """Links from astronomy records to Science papers (by stated bibcode or DOI) and unresolved references."""

        def run(conn):
            from src.kb.astronomy_citations import AstronomyCitations

            links = AstronomyCitations(conn, initialize=False).links(
                namespace, scopes=who()[1], record_id=record_id
            )
            return {"links": links, "n": len(links)}

        return read("astronomy_citations", run)

    @mcp.tool()
    def propose_astronomy_identity_matches(
        namespace: str, geo_namespace: str | None = None
    ) -> dict:
        """Propose reviewable identity candidates (never accepted here) and, with geo_namespace, save launch-site
        place resolutions for Geospatial review. Source-stated identifiers link without review."""

        def run(conn):
            from src.kb.astronomy_identity import AstronomyIdentity

            principal, scopes = who()
            return AstronomyIdentity(conn).propose(
                namespace,
                principal_id=principal,
                scopes=scopes,
                geo_namespace=geo_namespace,
            )

        return write(
            "propose_astronomy_identity_matches",
            run,
            optional={"geo_namespace": geo_namespace},
        )

    @mcp.tool()
    def review_astronomy_identity_match(
        namespace: str, candidate_id: str, decision: str, reason: str
    ) -> dict:
        """Accept or reject an astronomy identity candidate with a reason (an entity identity decision)."""

        def run(conn):
            from src.kb.astronomy_identity import AstronomyIdentity

            principal, scopes = who()
            return AstronomyIdentity(conn).review(
                namespace,
                candidate_id,
                decision,
                reason,
                principal_id=principal,
                scopes=scopes,
            )

        return write("review_astronomy_identity_match", run)

    @mcp.tool()
    def revert_astronomy_identity_match(
        namespace: str, candidate_id: str, reason: str
    ) -> dict:
        """Revert an accepted or rejected astronomy identity decision; records stay intact."""

        def run(conn):
            from src.kb.astronomy_identity import AstronomyIdentity

            principal, scopes = who()
            return AstronomyIdentity(conn).revert(
                namespace, candidate_id, reason, principal_id=principal, scopes=scopes
            )

        return write("revert_astronomy_identity_match", run)

    @mcp.tool()
    def review_astronomy_launch_site(
        namespace: str,
        site_code: str,
        decision: str,
        reason: str,
        selected_place_id: str | None = None,
    ) -> dict:
        """Review a launch site's saved place resolution through the Geospatial owner (accept, reject, defer)."""

        def run(conn):
            from src.kb.astronomy_identity import AstronomyIdentity

            principal, scopes = who()
            return AstronomyIdentity(conn).review_site(
                namespace,
                site_code,
                decision,
                selected_place_id=selected_place_id,
                reason=reason,
                principal_id=principal,
                scopes=scopes,
            )

        return write("review_astronomy_launch_site", run)

    @mcp.tool()
    def link_astronomy_citations(namespace: str) -> dict:
        """Resolve the bibcodes, DOIs and circulars the records state to Science paper records by exact
        identifier; unresolved references stay visible. Idempotent; never reactivates a reverted link."""

        def run(conn):
            from src.kb.astronomy_citations import AstronomyCitations

            principal, scopes = who()
            return AstronomyCitations(conn).link(
                namespace, principal_id=principal, scopes=scopes
            )

        return write("link_astronomy_citations", run)

    @mcp.tool()
    def revert_astronomy_citation(namespace: str, link_id: str, reason: str) -> dict:
        """Revert one record-to-paper link (final for that paper revision)."""

        def run(conn):
            from src.kb.astronomy_citations import AstronomyCitations

            principal, scopes = who()
            return AstronomyCitations(conn).revert(
                namespace, link_id, reason, principal_id=principal, scopes=scopes
            )

        return write("revert_astronomy_citation", run)

    @mcp.tool()
    def create_astronomy_monitor(
        namespace: str,
        request_key: str,
        watch: str,
        target: str | None = None,
        scale: str | None = None,
    ) -> dict:
        """Watch a small body, exoplanet, host, orbital object, launch provider or site, or SWPC products (by type
        and NOAA scale threshold) as a knowledge subscription; no separate scheduler."""

        def run(conn):
            from src.kb.astronomy_monitoring import AstronomyMonitor

            principal, scopes = who()
            return AstronomyMonitor(conn).create(
                namespace,
                request_key,
                watch=watch,
                target=target,
                scale=scale,
                principal_id=principal,
                scopes=scopes,
            )

        return write("create_astronomy_monitor", run)

    @mcp.tool()
    def run_astronomy_monitor(
        subscription_id: str, watermark: int | None = None
    ) -> dict:
        """Evaluate a monitor at a committed watermark: notifications cite the old and new revision and source."""

        def run(conn):
            from src.kb.astronomy_monitoring import AstronomyMonitor

            principal, scopes = who()
            return AstronomyMonitor(conn).run(
                subscription_id, watermark, principal_id=principal, scopes=scopes
            )

        return write("run_astronomy_monitor", run)

    @mcp.tool()
    def poll_astronomy_monitor(subscription_id: str, cursor: str = "") -> dict:
        """Poll a monitor's recorded events."""

        def run(conn):
            from src.kb.astronomy_monitoring import AstronomyMonitor

            principal, scopes = who()
            return AstronomyMonitor(conn, initialize=False).poll(
                subscription_id, principal_id=principal, scopes=scopes, cursor=cursor
            )

        return safe(
            checked("poll_astronomy_monitor", run, ready=False), required_scope=READ
        )
