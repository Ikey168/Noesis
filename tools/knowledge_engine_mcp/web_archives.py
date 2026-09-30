"""Web-archive provenance entry points (#2226): Memento resolution, as-of answers, pins and Save Page Now.

Registered on the knowledge-engine server for the ``platform.web-archives``
provider (``packs/platform/providers/platform.web-archives.json``), which any
pack can compose. The Osint bundle composes it through the optional
``web-archives`` feature. Save Page Now is the ``platform.web-archive-capture``
capability behind the optional ``save-page-now`` feature (off by default) and
the dedicated ``knowledge:citation:archive-request`` write scope.

Exclusions: no bypass of robots, exclusion, paywalls, reading-room or
anti-automation controls, no bulk mirroring or TimeMap harvesting, and no retry
of a refused URL through another archive or proxy.
"""

READ = "knowledge:citation:read"
WRITE = "knowledge:citation:write"
CAPTURE = "knowledge:citation:capture"
ARCHIVE_REQUEST = "knowledge:citation:archive-request"
SUBS_READ = "knowledge:subscriptions:read"
SUBS_WRITE = "knowledge:subscriptions:write"

WEB_ARCHIVE_WRITES = {
    "resolve_web_archive_captures",
    "propose_web_archive_matches",
    "review_web_archive_match",
    "pin_citation_capture",
    "create_web_archive_monitor",
    "check_web_archive_monitor",
    "run_web_archive_monitor",
    "request_save_page_now",
    "check_save_page_now",
}
WEB_ARCHIVE_READS = {
    "web_archive_contracts",
    "web_archive_readiness",
    "list_web_archive_captures",
    "web_page_as_of",
    "list_citation_pins",
    "list_save_page_now_requests",
    "poll_web_archive_monitor",
}
WEB_ARCHIVE_TOOLS = WEB_ARCHIVE_WRITES | WEB_ARCHIVE_READS
# Every scope each tool always uses.
WEB_ARCHIVE_SCOPES = {
    "web_archive_contracts": [],
    "web_archive_readiness": [READ],
    "list_web_archive_captures": [READ],
    "web_page_as_of": [READ],
    "list_citation_pins": [READ],
    "list_save_page_now_requests": [READ],
    "poll_web_archive_monitor": [READ, SUBS_READ],
    "resolve_web_archive_captures": [READ, CAPTURE],
    "propose_web_archive_matches": [READ, WRITE],
    "review_web_archive_match": [READ, WRITE],
    "pin_citation_capture": [READ, WRITE],
    "create_web_archive_monitor": [READ, SUBS_WRITE],
    "check_web_archive_monitor": [READ, CAPTURE, WRITE, SUBS_READ],
    "run_web_archive_monitor": [READ, SUBS_WRITE],
    "request_save_page_now": [ARCHIVE_REQUEST],
    "check_save_page_now": [ARCHIVE_REQUEST],
}


def required_scopes(tool_name, mutability):
    return WEB_ARCHIVE_SCOPES.get(tool_name, [WRITE if mutability == "write" else READ])


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def store(conn, write=False):
        from src.kb.citation_preservation import CitationPreservationStore

        return CitationPreservationStore(conn, initialize=write)

    @mcp.tool()
    def web_archive_contracts() -> dict:
        """Per-archive Memento contracts, access decisions (archive.today excluded, LoC and Vefsafn deferred, BnF
        excluded), budgets, the Save Page Now write terms and LIVE_VERIFICATION, from the WA01 audit."""
        from src.ingestion.memento import (
            ARCHIVES,
            BOUNDED_COVERAGE,
            EXCLUSIONS,
            LIVE_VERIFICATION,
            SAVE_PAGE_NOW,
        )

        return {"archives": ARCHIVES, "bounded_coverage": BOUNDED_COVERAGE, "save_page_now": SAVE_PAGE_NOW,
                "live_verification": LIVE_VERIFICATION, "exclusions": list(EXCLUSIONS)}

    @mcp.tool()
    def web_archive_readiness() -> dict:
        """Whether the citation preservation tables hold captures, TimeMaps and pins, and whether the optional
        save-page-now feature is selected (default off)."""
        def run(conn):
            from src.ingestion.wayback import save_page_now_enabled

            counts = {}
            for table in ("citation_snapshots", "web_archive_captures", "web_archive_timemaps", "citation_pins",
                          "web_archive_matches"):
                exists = conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name=?",
                                      [table]).fetchone()
                counts[table] = int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]) if exists else None  # noqa: S608
            return {"tables": counts, "save_page_now_feature": save_page_now_enabled(conn),
                    "live": "unverified-live; fixture evidence only until WA15 (#2348)"}
        return safe(run, required_scope=READ)

    @mcp.tool()
    def resolve_web_archive_captures(namespace: str, url: str, request_id: str, at: str | None = None,
                                     via_aggregator: bool = True, archives: list[str] | None = None,
                                     crawls: list[str] | None = None) -> dict:
        """Resolve a URL's captures across archives (Time Travel aggregator TimeMap, direct Internet Archive,
        UK Web Archive and Arquivo.pt TimeMaps with CDX digests, Common Crawl index) within the WA01 budgets.
        Every archive gets an explicit outcome; excluded and deferred archives are listed, never omitted. The
        request id is an idempotent receipt."""
        def run(conn):
            from src.ingestion.memento import MementoClient

            principal, scopes = who()
            return MementoClient(conn, namespace, principal_id=principal, scopes=scopes).resolve(
                url, request_id=request_id, at=at, via_aggregator=via_aggregator, archives=archives,
                crawls=crawls or [])
        return safe(run, write=True, required_scope=CAPTURE)

    @mcp.tool()
    def list_web_archive_captures(namespace: str, url: str) -> dict:
        """Capture records for a URL (canonically matched) and the latest TimeMap snapshot per archive and
        resolver, with digests labelled published or computed."""
        def run(conn):
            item = store(conn)
            return {"captures": item.captures_for_url(namespace, url, scopes=who()[1]),
                    "timemaps": item.timemaps_for_url(namespace, url, scopes=who()[1])}
        return safe(run, required_scope=READ)

    @mcp.tool()
    def web_page_as_of(namespace: str, url: str, at: str) -> dict:
        """What a page said on a date and according to which archive: per-archive nearest captures before and
        after the date with distance and digest, same-content groups by digest equality, and 'no capture on
        record', 'archive unavailable' and 'excluded by access decision' kept distinct. No content diffing and no
        inference about unarchived dates."""
        def run(conn):
            from src.kb.web_archive_queries import page_as_of

            return page_as_of(conn, namespace, url, at, scopes=who()[1])
        return safe(run, required_scope=READ)

    @mcp.tool()
    def propose_web_archive_matches(namespace: str, citation_id: str, cited_url: str) -> dict:
        """Match captures to a cited URL under the versioned canonicalisation: exact matches are accepted,
        canonicalised and redirect-derived matches await review (redirects recorded as published, never
        followed)."""
        def run(conn):
            from src.kb.web_archive_identity import CaptureMatcher

            principal, scopes = who()
            return CaptureMatcher(conn).propose(namespace, citation_id, cited_url, principal_id=principal,
                                                scopes=scopes)
        return safe(run, write=True, required_scope=WRITE)

    @mcp.tool()
    def review_web_archive_match(namespace: str, match_id: str, decision: str, reason: str) -> dict:
        """Accept or reject a non-exact capture match with a reason (by someone other than its proposer);
        rejected matches are never used for pinning."""
        def run(conn):
            from src.kb.web_archive_identity import CaptureMatcher

            principal, scopes = who()
            return CaptureMatcher(conn).review(namespace, match_id, decision, reason, principal_id=principal,
                                               scopes=scopes)
        return safe(run, write=True, required_scope=WRITE)

    @mcp.tool()
    def pin_citation_capture(namespace: str, citation_id: str, capture_id: str, cited_url: str,
                             match_id: str | None = None, reason: str | None = None) -> dict:
        """Pin a citation from any pack to one archived capture; the cited URL is never rewritten and re-pinning
        adds a revision. Citation exports then carry the pin (archive, URI-M, datetime, digest)."""
        def run(conn):
            principal, scopes = who()
            return store(conn, True).pin_citation(namespace, citation_id, capture_id, cited_url,
                                                  principal_id=principal, scopes=scopes, match_id=match_id,
                                                  reason=reason)
        return safe(run, write=True, required_scope=WRITE)

    @mcp.tool()
    def list_citation_pins(namespace: str, citation_id: str) -> dict:
        """Every pin revision of a citation (the last is current) and its capture matches."""
        def run(conn):
            from src.kb.web_archive_identity import CaptureMatcher

            scopes = who()[1]
            return {"pins": store(conn).pins(namespace, citation_id, scopes=scopes),
                    "matches": CaptureMatcher(conn, initialize=False).matches(namespace, citation_id,
                                                                              scopes=scopes)}
        return safe(run, required_scope=READ)

    @mcp.tool()
    def create_web_archive_monitor(namespace: str, request_key: str, url: str, citation_id: str | None = None) -> dict:
        """Subscribe to a cited URL: new captures, live-URL failure while a pin exists, and pinned-capture
        unavailability."""
        def run(conn):
            from src.kb.web_archive_monitoring import WebArchiveMonitor

            principal, scopes = who()
            return WebArchiveMonitor(conn).create(namespace, request_key, url, citation_id=citation_id,
                                                  principal_id=principal, scopes=scopes)
        return safe(run, write=True, required_scope=SUBS_WRITE)

    @mcp.tool()
    def check_web_archive_monitor(subscription_id: str, request_id: str, via_aggregator: bool = True,
                                  archives: list[str] | None = None) -> dict:
        """One bounded monitor round: a resolution, the live URL and each pinned capture; HTTP outcomes are
        recorded as observed."""
        def run(conn):
            from src.kb.web_archive_monitoring import WebArchiveMonitor

            principal, scopes = who()
            return WebArchiveMonitor(conn).check(subscription_id, request_id=request_id, principal_id=principal,
                                                 scopes=scopes, via_aggregator=via_aggregator, archives=archives)
        return safe(run, write=True, required_scope=CAPTURE)

    @mcp.tool()
    def run_web_archive_monitor(subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate a web-archive monitor at a committed watermark: new_capture, live_url_failure and
        pinned_capture_unavailable notifications citing their records; never a conclusion about the publisher."""
        def run(conn):
            from src.kb.web_archive_monitoring import WebArchiveMonitor

            principal, scopes = who()
            return WebArchiveMonitor(conn).run(subscription_id, watermark, principal_id=principal, scopes=scopes)
        return safe(run, write=True, required_scope=SUBS_WRITE)

    @mcp.tool()
    def poll_web_archive_monitor(subscription_id: str, cursor: str = "") -> dict:
        """Poll a web-archive monitor's delivered events."""
        def run(conn):
            from src.kb.web_archive_monitoring import WebArchiveMonitor

            principal, scopes = who()
            return WebArchiveMonitor(conn, initialize=False).poll(subscription_id, principal_id=principal,
                                                                  scopes=scopes, cursor=cursor)
        return safe(run, required_scope=SUBS_READ)

    @mcp.tool()
    def request_save_page_now(namespace: str, url: str, request_id: str, citation_id: str | None = None) -> dict:
        """Ask the Internet Archive for a new capture of a cited URL (Save Page Now, a write operation). Needs
        the knowledge:citation:archive-request scope and the optional save-page-now feature (off by default);
        per-namespace daily budget; refused URLs are recorded and never retried elsewhere."""
        def run(conn):
            from src.ingestion.wayback import request_save_page_now as request, save_page_now_enabled

            principal, scopes = who()
            return request(conn, url, namespace=namespace, request_id=request_id, principal_id=principal,
                           scopes=scopes, feature_enabled=save_page_now_enabled(conn), citation_id=citation_id)
        return safe(run, write=True, required_scope=ARCHIVE_REQUEST)

    @mcp.tool()
    def check_save_page_now(namespace: str, request_id: str) -> dict:
        """Poll a pending Save Page Now job once; a success records the new capture for pinning."""
        def run(conn):
            from src.ingestion.wayback import check_save_page_now as check, save_page_now_enabled
            from src.ingestion.wayback import SavePageNowError

            if not save_page_now_enabled(conn):
                raise SavePageNowError("feature_disabled", "the optional save-page-now feature is off")
            return check(conn, request_id, namespace=namespace, scopes=who()[1])
        return safe(run, write=True, required_scope=ARCHIVE_REQUEST)

    @mcp.tool()
    def list_save_page_now_requests(namespace: str) -> dict:
        """Save Page Now request receipts (requester, URL, time, job id, status, resulting URI-M)."""
        def run(conn):
            from src.ingestion.wayback import save_page_now_requests

            return {"requests": save_page_now_requests(conn, namespace, scopes=who()[1])}
        return safe(run, required_scope=READ)
