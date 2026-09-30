"""Cultural primary sources in Science/Research and Geospatial (DDB, Europeana).

Acquisition runs through the scientific source pack (``primary-scientific-evidence``)
with the ``ddb`` and ``europeana`` sources. Objects stay their own records;
links to scholarly works need a citation, provider relation or review, and
places come only from provider coordinates or gazetteer-resolved names.
"""

# Books, music and authority metadata beside Cultural Collections (#2225, MM11).
MEDIA_WRITES = {
    "propose_media_identity_matches", "review_media_identity_match", "revert_media_identity_match",
    "link_media_cultural_objects", "link_media_news_entities", "review_media_link", "assert_media_link",
    "create_media_metadata_monitor", "run_media_metadata_monitor",
}
MEDIA_READS = {
    "media_metadata_source_contracts", "media_metadata_status", "resolve_media_identifier", "search_media_titles",
    "media_authority_history", "export_media_evidence_bundle", "list_media_identity_matches",
    "poll_media_metadata_monitor",
}
MEDIA_TOOLS = MEDIA_WRITES | MEDIA_READS
MEDIA_SCOPES = {
    "media_metadata_source_contracts": [],
    **{name: ["knowledge:cultural:read"] for name in (
        "media_metadata_status", "resolve_media_identifier", "search_media_titles", "media_authority_history",
        "export_media_evidence_bundle", "list_media_identity_matches")},
    **{name: ["knowledge:cultural:write"] for name in (
        "propose_media_identity_matches", "link_media_cultural_objects", "link_media_news_entities")},
    **{name: ["knowledge:cultural:review"] for name in (
        "review_media_identity_match", "revert_media_identity_match", "review_media_link", "assert_media_link")},
    "create_media_metadata_monitor": ["knowledge:cultural:read", "knowledge:subscriptions:write"],
    # Running reads media records, inspects the subscription and writes its events.
    "run_media_metadata_monitor": ["knowledge:cultural:read", "knowledge:subscriptions:read",
                                   "knowledge:subscriptions:write"],
    "poll_media_metadata_monitor": ["knowledge:subscriptions:read", "knowledge:cultural:read"],
}
CULTURAL_WRITES = {
    "propose_cultural_matches", "review_cultural_match", "link_cultural_research",
    "suggest_cultural_research_candidates", "review_cultural_research_candidate", "acquire_cultural_assets",
} | MEDIA_WRITES
CULTURAL_TOOLS = CULTURAL_WRITES | MEDIA_READS | {
    "cultural_readiness", "cultural_source_contracts", "cultural_rights_policy", "search_cultural_objects",
    "inspect_cultural_object", "primary_sources_for_work",
}
CULTURAL_SCOPES = {
    "cultural_source_contracts": [], "cultural_rights_policy": [],
    "review_cultural_match": ["knowledge:cultural:review"],
    "review_cultural_research_candidate": ["knowledge:cultural:review"],
    **MEDIA_SCOPES,
}


def required_scopes(tool_name, mutability):
    return CULTURAL_SCOPES.get(
        tool_name, ["knowledge:cultural:write" if mutability == "write" else "knowledge:cultural:read"])


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def store(conn):
        from src.kb.cultural import CulturalStore

        return CulturalStore(conn)

    @mcp.tool()
    def cultural_source_contracts() -> dict:
        """DDB and Europeana access, identifiers, pagination, rights semantics and live-verification state."""
        from src.ingestion.cultural_sources import PROVIDER_CONTRACTS

        return {"contracts": PROVIDER_CONTRACTS}

    @mcp.tool()
    def cultural_rights_policy(statement: str, purpose: str = "research") -> dict:
        """How an item rights statement governs metadata, asset storage and export (unclear means link-only)."""
        from src.kb.cultural import rights_policy

        return rights_policy(statement, purpose=purpose)

    @mcp.tool()
    def cultural_readiness() -> dict:
        """Per-provider readiness of the cultural sources inside the scientific source pack."""
        from src.kb.cultural import readiness

        return safe(lambda conn: readiness(conn), required_scope="knowledge:cultural:read")

    @mcp.tool()
    def search_cultural_objects(namespace: str, place_id: str | None = None, geometry_ids: list[str] | None = None,
                                collection: str | None = None, creator: str | None = None,
                                subject: str | None = None, date_from: str | None = None,
                                date_to: str | None = None, provider: str | None = None, limit: int = 25) -> dict:
        """Objects by place, collection, creator, subject or declared date range, with rights and source links."""
        return safe(lambda conn: store(conn).search(
            namespace, scopes=who()[1], place_id=place_id, geometry_ids=geometry_ids, collection=collection,
            creator=creator, subject=subject, date_from=date_from, date_to=date_to, provider=provider, limit=limit),
            required_scope="knowledge:cultural:read")

    @mcp.tool()
    def inspect_cultural_object(namespace: str, object_id: str) -> dict:
        """One object: provider record, revisions, rights per representation, places with role and precision."""
        return safe(lambda conn: store(conn).object(namespace, object_id, scopes=who()[1]),
                    required_scope="knowledge:cultural:read")

    @mcp.tool()
    def primary_sources_for_work(namespace: str, work_kind: str, work_id: str) -> dict:
        """Start from a scholarly work: linked primary objects, open candidates and their geometries/places."""
        return safe(lambda conn: store(conn).primary_sources_for_work(namespace, work_kind, work_id, scopes=who()[1]),
                    required_scope="knowledge:cultural:read")

    @mcp.tool()
    def propose_cultural_matches(namespace: str) -> dict:
        """Explicit identifier matches plus conservative multi-field candidates across providers."""
        return safe(lambda conn: store(conn).propose_matches(namespace, scopes=who()[1]), write=True,
                    required_scope="knowledge:cultural:write")

    @mcp.tool()
    def review_cultural_match(namespace: str, match_id: str, decision: str, reason: str) -> dict:
        """Accept, reject or defer a cross-provider object match; provider records stay separate."""
        return safe(lambda conn: store(conn).review_match(namespace, match_id, decision, reason, scopes=who()[1],
                                                          principal_id=who()[0]),
                    write=True, required_scope="knowledge:cultural:review")

    @mcp.tool()
    def link_cultural_research(namespace: str, object_id: str, work_kind: str, work_id: str, basis: str,
                               evidence: str) -> dict:
        """Link an object to a scholarly work by explicit citation, provider relation or reviewed assertion."""
        return safe(lambda conn: store(conn).link_research(namespace, object_id, work_kind, work_id, basis, evidence,
                                                           scopes=who()[1], principal_id=who()[0]),
                    write=True, required_scope="knowledge:cultural:write")

    @mcp.tool()
    def suggest_cultural_research_candidates(namespace: str, work_kind: str, work_id: str, text: str,
                                             limit: int = 10) -> dict:
        """Keyword-overlap candidates between a work and objects; suggestions only, never links."""
        return safe(lambda conn: store(conn).suggest_research_candidates(
            namespace, work_kind, work_id, text, scopes=who()[1], principal_id=who()[0], limit=limit),
            write=True, required_scope="knowledge:cultural:write")

    @mcp.tool()
    def review_cultural_research_candidate(namespace: str, link_id: str, decision: str, reason: str) -> dict:
        """Accept (becomes a reviewed link) or reject a candidate research link."""
        return safe(lambda conn: store(conn).review_research_candidate(
            namespace, link_id, decision, reason, scopes=who()[1], principal_id=who()[0]),
            write=True, required_scope="knowledge:cultural:review")

    @mcp.tool()
    def acquire_cultural_assets(namespace: str, object_id: str, action: str = "store", purpose: str = "research",
                                allowed_hosts: list[str] | None = None) -> dict:
        """Retain representations only where item rights permit; otherwise record link-only."""
        return safe(lambda conn: store(conn).acquire_assets(
            namespace, object_id, scopes=who()[1], principal_id=who()[0], action=action, purpose=purpose,
            allowed_hosts=allowed_hosts or []), write=True, required_scope="knowledge:cultural:write")

    register_media_metadata(mcp, safe, who)


def register_media_metadata(mcp, safe, who):
    """Media metadata tools (#2225): identity backbone, never content, popularity or rights determinations."""

    def queries(conn):
        from src.kb.media_metadata import MediaMetadataQueries

        return MediaMetadataQueries(conn)

    def identity(conn, *, initialize=True):
        from src.kb.media_metadata import MediaMetadataIdentity

        return MediaMetadataIdentity(conn, initialize=initialize)

    def links(conn):
        from src.kb.media_metadata import MediaMetadataLinks

        return MediaMetadataLinks(conn)

    def monitor(conn, *, initialize=True):
        from src.kb.media_metadata_monitoring import MediaMetadataMonitor

        return MediaMetadataMonitor(conn, initialize=initialize)

    @mcp.tool()
    def media_metadata_source_contracts() -> dict:
        """MM01 access decisions for Open Library (API and named dump slices), MusicBrainz (CC0 core data only),
        Wikidata, the Deutsche Nationalbibliothek and the Library of Congress: licence, rate limit, User-Agent,
        revision semantics, bounded coverage, excluded sources and record classes, LIVE_VERIFICATION per source.
        No full text, media content, popularity ranking or rights clearance determination is in scope."""
        from src.ingestion.media_metadata_sources import (
            BOUNDED_COVERAGE,
            EXCLUDED_RECORD_CLASSES,
            EXCLUDED_SOURCES,
            LIVE_VERIFICATION,
            PROVIDER_CONTRACTS,
        )

        return {"contracts": PROVIDER_CONTRACTS, "live_verification": LIVE_VERIFICATION,
                "bounded_coverage": BOUNDED_COVERAGE, "excluded_sources": EXCLUDED_SOURCES,
                "excluded_record_classes": EXCLUDED_RECORD_CLASSES}

    @mcp.tool()
    def media_metadata_status() -> dict:
        """Bundle status of the media-metadata sources in the scientific source pack: install and enablement,
        LIVE_VERIFICATION per source, the optional media-metadata and media-metadata-news features and records
        held per source. Offline fixture evidence is never reported as live."""
        from src.kb.media_metadata import bundle_status

        return safe(lambda conn: bundle_status(conn), required_scope="knowledge:cultural:read")

    @mcp.tool()
    def resolve_media_identifier(namespace: str, identifier: str, scheme: str | None = None,
                                 as_of: str | None = None) -> dict:
        """Exact lookup of an ISBN, ISRC, ISWC, MBID, Wikidata QID, GND, DNB IDN, LCCN/LCNAF or OLID: the
        work/edition/recording identity with its authority records (source, revision, as-of time, licence),
        the identity matches used, redirects and merges, and revision history as of an ISO date. Unknown,
        invalid and ambiguous identifiers are reported as such; nothing is chosen for you."""
        return safe(lambda conn: queries(conn).answer(namespace, scopes=who()[1], identifier=identifier,
                                                      scheme=scheme, as_of=as_of),
                    required_scope="knowledge:cultural:read")

    @mcp.tool()
    def search_media_titles(namespace: str, title: str | None = None, creator: str | None = None,
                            as_of: str | None = None, limit: int = 10) -> dict:
        """Title and/or creator lookup over acquired records: ranked candidates, one per identity, each with the
        title and name evidence that scored it; ties are flagged ambiguous. Not a single asserted answer and no
        popularity ranking."""
        return safe(lambda conn: queries(conn).answer(namespace, scopes=who()[1], title=title, creator=creator,
                                                      as_of=as_of, limit=limit),
                    required_scope="knowledge:cultural:read")

    @mcp.tool()
    def media_authority_history(namespace: str, record: str, as_of: str | None = None) -> dict:
        """Every revision of one record (source:native_id or record ID) known at an ISO date, oldest first, with
        provider revision markers, status changes (redirected, deprecated, deleted), redirects and any revision
        conflicts."""
        return safe(lambda conn: queries(conn).authority_history(namespace, record, scopes=who()[1], as_of=as_of),
                    required_scope="knowledge:cultural:read")

    @mcp.tool()
    def export_media_evidence_bundle(namespace: str, identifier: str | None = None, scheme: str | None = None,
                                     title: str | None = None, creator: str | None = None,
                                     as_of: str | None = None) -> dict:
        """An identifier, title or creator answer as a noesis-evidence-bundle-v1 citing every authority record
        revision and identity match used; unresolved, ambiguous and conflicting parts are omissions."""
        def run(conn):
            q = queries(conn)
            return q.export_bundle(q.answer(namespace, scopes=who()[1], identifier=identifier, scheme=scheme,
                                            title=title, creator=creator, as_of=as_of))
        return safe(run, required_scope="knowledge:cultural:read")

    @mcp.tool()
    def list_media_identity_matches(namespace: str, record: str | None = None, state: str | None = None) -> dict:
        """Identity matches and candidates (explicit identifier, shared identifier, similarity, canonical-entity)
        with their evidence, review state and history; conflicting identifier claims are listed."""
        def run(conn):
            from src.kb.media_metadata import authorize

            authorize(namespace, who()[1], "knowledge:cultural:read")
            ident = identity(conn, initialize=False)
            if not ident.store.ready():
                return {"matches": [], "conflicts": [], "status": "no media metadata acquired yet"}
            record_id = ident.store.resolve_record(namespace, record) if record else None
            return {"matches": ident.matches(namespace, record_id=record_id, state=state),
                    "conflicts": [{"key": k, "claimed_by": v}
                                  for k, v in sorted(ident.conflicting_keys(namespace).items())]}
        return safe(run, required_scope="knowledge:cultural:read")

    @mcp.tool()
    def propose_media_identity_matches(namespace: str) -> dict:
        """Explicit identifier statements (Wikidata P648/P227/P244/P434, Open Library identifiers, MusicBrainz URL
        relations, MARC 024/010) become matches citing the asserting revision; title/creator/date similarity and
        creator names against News canonical entities only propose scored candidates. Records are never merged
        and canonical entities are only read."""
        return safe(lambda conn: identity(conn).propose(namespace, scopes=who()[1], principal_id=who()[0]),
                    write=True, required_scope="knowledge:cultural:write")

    @mcp.tool()
    def review_media_identity_match(namespace: str, match_id: str, decision: str, reason: str) -> dict:
        """Accept, reject or defer a media identity match with a reason; recorded as an entity-history decision
        and reversible. Conflicts stay visible after review."""
        return safe(lambda conn: identity(conn).review(namespace, match_id, decision, reason, scopes=who()[1],
                                                       principal_id=who()[0]),
                    write=True, required_scope="knowledge:cultural:review")

    @mcp.tool()
    def revert_media_identity_match(namespace: str, match_id: str, reason: str) -> dict:
        """Undo a reviewed media identity decision (entity-history undo); the match becomes reviewable again."""
        return safe(lambda conn: identity(conn).revert(namespace, match_id, reason, scopes=who()[1],
                                                       principal_id=who()[0]),
                    write=True, required_scope="knowledge:cultural:review")

    @mcp.tool()
    def link_media_cultural_objects(namespace: str) -> dict:
        """Link cultural objects to works and creators only through identifiers (creator authority IDs) or
        provider relations (same_as) in the object record, citing both revisions; keyword overlap only creates a
        candidate."""
        return safe(lambda conn: links(conn).link_cultural_objects(namespace, scopes=who()[1],
                                                                   principal_id=who()[0]),
                    write=True, required_scope="knowledge:cultural:write")

    @mcp.tool()
    def link_media_news_entities(namespace: str) -> dict:
        """Offer authority records as reviewable candidates for News canonical entities (the optional
        media-metadata-news feature); only reviewed matches become links and no entity is re-labelled."""
        return safe(lambda conn: links(conn).link_news_entities(namespace, scopes=who()[1], principal_id=who()[0]),
                    write=True, required_scope="knowledge:cultural:write")

    @mcp.tool()
    def review_media_link(namespace: str, link_id: str, decision: str, reason: str) -> dict:
        """Accept (a reviewed assertion) or reject a candidate link between a cultural object or news entity and a
        media record."""
        return safe(lambda conn: links(conn).review_link(namespace, link_id, decision, reason, scopes=who()[1],
                                                         principal_id=who()[0]),
                    write=True, required_scope="knowledge:cultural:review")

    @mcp.tool()
    def assert_media_link(namespace: str, subject_kind: str, subject_id: str, record: str, evidence: str) -> dict:
        """A reviewer's cited assertion that a cultural object or news entity refers to a media record
        (source:native_id or record ID)."""
        return safe(lambda conn: links(conn).assert_link(namespace, subject_kind, subject_id, record, evidence,
                                                         scopes=who()[1], principal_id=who()[0]),
                    write=True, required_scope="knowledge:cultural:review")

    @mcp.tool()
    def create_media_metadata_monitor(namespace: str, request_key: str, records: list[str] | None = None,
                                      identifiers: list[str] | None = None, delivery: dict | None = None) -> dict:
        """Watch records or identifiers for authority revisions, redirects, merges, deprecations and new identifier
        assertions, as a knowledge subscription."""
        watch = {k: v for k, v in (("records", records), ("identifiers", identifiers)) if v}
        return safe(lambda conn: monitor(conn).create(namespace, request_key, watch=watch, principal_id=who()[0],
                                                      scopes=who()[1], delivery=delivery),
                    write=True, required_scope="knowledge:cultural:read")

    @mcp.tool()
    def run_media_metadata_monitor(subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate a media metadata monitor at the committed state; each notification is dated and cites both
        revisions. Replays emit nothing."""
        return safe(lambda conn: monitor(conn).run(subscription_id, watermark, principal_id=who()[0],
                                                   scopes=who()[1]),
                    write=True, required_scope="knowledge:subscriptions:write")

    @mcp.tool()
    def poll_media_metadata_monitor(subscription_id: str, cursor: str = "") -> dict:
        """Poll a media metadata monitor's delivered events."""
        return safe(lambda conn: monitor(conn, initialize=False).poll(subscription_id, principal_id=who()[0],
                                                                      scopes=who()[1], cursor=cursor),
                    required_scope="knowledge:subscriptions:read")
