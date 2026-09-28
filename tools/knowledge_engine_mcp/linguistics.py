"""Linguistics pack entry points: lexemes, senses, etymologies, languoid profiles, identity and monitors (#2189).

Acquisition runs through the shared source-pack tools (pack
``linguistics-lexical-typological``). Every answer cites each record's source,
revision and licence, and flags CC BY-SA content. Nothing machine-translated is
returned as a sourced definition. Identity candidates are proposals until
reviewed. Every read answers ``not_ready`` before any linguistics source ran.
"""

LINGUISTICS_WRITES = {
    "propose_languoid_matches",
    "propose_lexeme_matches",
    "review_linguistic_identity_match",
    "revert_linguistic_identity_match",
    "record_iso_code_events",
    "project_languoid_locations",
    "review_languoid_location",
    "create_linguistics_monitor",
    "run_linguistics_monitor",
    "index_linguistic_texts",
    "propose_lexeme_alias",
}
LINGUISTICS_READS = {
    "lookup_lexeme",
    "sense_history",
    "definition_changes",
    "lexeme_etymology",
    "languoid_profile",
    "typological_profile",
    "lexeme_cross_language_links",
    "list_linguistic_identity_candidates",
    "export_lexeme_dossier",
    "linguistics_readiness",
    "poll_linguistics_monitor",
}
LINGUISTICS_TOOLS = LINGUISTICS_WRITES | LINGUISTICS_READS
READ = "knowledge:linguistics:read"
WRITE = "knowledge:linguistics:write"
REVIEW = "knowledge:linguistics:review"
LINGUISTICS_SCOPES = {
    # Proposing writes candidates and returns the namespace's candidate list, which is a read.
    "propose_languoid_matches": [WRITE, READ],
    "propose_lexeme_matches": [WRITE, READ],
    "review_linguistic_identity_match": [REVIEW],
    "revert_linguistic_identity_match": [REVIEW],
    # Projection stores the cited point and a place resolution through the Geospatial owner.
    "project_languoid_locations": [
        WRITE,
        "knowledge:geospatial:read",
        "knowledge:geospatial:write",
    ],
    "review_languoid_location": [READ, "knowledge:geospatial:review"],
    "create_linguistics_monitor": [READ, "knowledge:subscriptions:write"],
    # Running reads the monitor (subscriptions:read) and records its evaluation (subscriptions:write).
    "run_linguistics_monitor": [
        READ,
        "knowledge:subscriptions:read",
        "knowledge:subscriptions:write",
    ],
    "poll_linguistics_monitor": [READ, "knowledge:subscriptions:read"],
    # Definitions and lemmas become noesis-language-text-v1 texts; aliases are cross-language candidates.
    "index_linguistic_texts": [WRITE, "knowledge:cross-language:write"],
    "propose_lexeme_alias": [WRITE, "knowledge:cross-language:write"],
}
QUERY_EXAMPLES = {
    "lookup_lexeme": {
        "intent": "What does this word mean according to each source",
        "arguments": {
            "namespace": "linguistics",
            "lemma": "tamo",
            "languoid": "nort3456",
        },
        "semantics": "every source's lexeme side by side with forms, paradigms, senses and definitions as "
        "published; licences flag CC BY-SA content",
    },
    "sense_history": {
        "intent": "What did this sense mean on a date",
        "arguments": {
            "namespace": "linguistics",
            "sense": "<sense record key>",
            "as_of": "2026-04-01",
        },
        "semantics": "the definition revision in force per source at the date, by the source's own revision date",
    },
    "lexeme_etymology": {
        "intent": "Where does this word come from",
        "arguments": {"namespace": "linguistics", "lexeme": "<lexeme record key>"},
        "semantics": "a chain of cited assertions; disputed etymologies side by side; unresolved endpoints as text",
    },
    "languoid_profile": {
        "intent": "Which language is this and what is its typology",
        "arguments": {"namespace": "linguistics", "languoid": "qnv"},
        "semantics": "Glottocode, ISO 639-3, classification path and history, location and WALS values with "
        "references; a feature without a value is 'no value on record'",
    },
}


def required_scopes(tool_name, mutability):
    return LINGUISTICS_SCOPES.get(tool_name, [WRITE if mutability == "write" else READ])


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def checked(tool_name, operation, mutability="read"):
        """Run with every scope the tool always uses present (not only the first)."""

        def run(conn):
            from src.kb.linguistics_records import LinguisticsError

            missing = [
                s for s in required_scopes(tool_name, mutability) if s not in who()[1]
            ]
            if missing and "operator" not in who()[1]:
                raise LinguisticsError("unauthorized", f"{', '.join(missing)} required")
            return operation(conn)

        return run

    def read(tool_name, operation):
        return safe(
            checked(tool_name, operation),
            required_scope=required_scopes(tool_name, "read")[0],
        )

    def write(tool_name, operation):
        return safe(
            checked(tool_name, operation, "write"),
            write=True,
            required_scope=required_scopes(tool_name, "write")[0],
        )

    def queries(conn):
        from src.kb.linguistics_queries import LinguisticsQueries

        return LinguisticsQueries(conn)

    def identity(conn, initialize=True):
        from src.kb.linguistics_identity import LinguisticsIdentity

        return LinguisticsIdentity(conn, initialize=initialize)

    def monitor(conn, initialize=True):
        from src.kb.linguistics_monitoring import LinguisticsMonitor

        return LinguisticsMonitor(conn, initialize=initialize)

    @mcp.tool()
    def lookup_lexeme(
        namespace: str,
        lemma: str,
        languoid: str | None = None,
        as_of: str | None = None,
    ) -> dict:
        """Every source's lexeme for a word (NFC, case-folded lemma), optionally in one languoid (Glottocode or
        ISO 639-3): forms and paradigm tables grouped by grammatical features, senses, definitions as published,
        usage examples and Leipzig-validated glosses, identity candidates and citations. Sources stay side by side;
        CC BY-SA content is flagged. Noesis translations are labelled and never shown as sourced definitions."""
        return read(
            "lookup_lexeme",
            lambda conn: queries(conn).lookup_lexeme(
                namespace, lemma, languoid, scopes=who()[1], as_of=as_of
            ),
        )

    @mcp.tool()
    def sense_history(namespace: str, sense: str, as_of: str) -> dict:
        """The definition revision in force per source (and definition language) for a sense on a date, by the
        source's own revision date; 'no revision in force' is listed as unknown."""
        return read(
            "sense_history",
            lambda conn: queries(conn).sense_history(
                namespace, sense, as_of, scopes=who()[1]
            ),
        )

    @mcp.tool()
    def definition_changes(namespace: str, sense: str) -> dict:
        """Every definition revision of a sense per source, in source order, with the revisions that restated it."""
        return read(
            "definition_changes",
            lambda conn: queries(conn).definition_changes(
                namespace, sense, scopes=who()[1]
            ),
        )

    @mcp.tool()
    def lexeme_etymology(namespace: str, lexeme: str, as_of: str | None = None) -> dict:
        """The cited etymology chain of a lexeme (and its reviewed equivalents): each link names its relation, the
        source's native value, its references (DOIs resolved to literature records) and citation. Disputed
        etymologies are shown side by side and never resolved; proto-forms stay cited text."""
        return read(
            "lexeme_etymology",
            lambda conn: queries(conn).lexeme_etymology(
                namespace, lexeme, scopes=who()[1], as_of=as_of
            ),
        )

    @mcp.tool()
    def languoid_profile(
        namespace: str, languoid: str, as_of: str | None = None
    ) -> dict:
        """A languoid (Glottocode, ISO 639-3 or Wiktionary code) with identifiers, classification path and its
        history across releases, location, ISO macrolanguage and retirement context and WALS values with their
        references. A feature without a value is 'no value on record'; ambiguous codes are never guessed."""
        return read(
            "languoid_profile",
            lambda conn: queries(conn).languoid_profile(
                namespace, languoid, scopes=who()[1], as_of=as_of
            ),
        )

    @mcp.tool()
    def typological_profile(
        namespace: str, languoid: str, as_of: str | None = None
    ) -> dict:
        """WALS feature values of a languoid through the Glottocode WALS publishes, each with its source
        references and release; a declared feature without a value is 'no value on record'."""

        def run(conn):
            profile = queries(conn).languoid_profile(
                namespace, languoid, scopes=who()[1], as_of=as_of
            )
            keep = (
                "contract",
                "languoid",
                "status",
                "as_of",
                "name",
                "typology",
                "unknowns",
                "licences",
            )
            return {**{k: profile[k] for k in keep if k in profile}, "n": profile["n"]}

        return read("typological_profile", run)

    @mcp.tool()
    def lexeme_cross_language_links(namespace: str, key: str) -> dict:
        """Source-stated cross-language links of a lexeme or sense: Wiktionary translation-table rows and senses
        that state the same Wikidata item, each with its citations. Links, not identity claims."""

        def run(conn):
            from src.kb.linguistics_crosslang import CrossLanguageLinks

            return CrossLanguageLinks(conn).links(namespace, key, scopes=who()[1])

        return read("lexeme_cross_language_links", run)

    @mcp.tool()
    def list_linguistic_identity_candidates(
        namespace: str,
        kind: str | None = None,
        state: str | None = None,
        record_key: str | None = None,
    ) -> dict:
        """Languoid and lexeme identity candidates with basis, evidence and state (proposed, accepted, rejected,
        reverted). Candidates are proposals; records are never merged."""

        def run(conn):
            ident = identity(conn, initialize=False)
            ident.store.require_ready()
            items = ident.candidates(
                namespace,
                scopes=who()[1],
                kind=kind,
                state=state,
                record_key=record_key,
            )
            return {"candidates": items, "n": len(items)}

        return read("list_linguistic_identity_candidates", run)

    @mcp.tool()
    def export_lexeme_dossier(
        namespace: str,
        lemma: str,
        target_licence: str,
        languoid: str | None = None,
        keep_attribution: bool = True,
        exclude_share_alike: bool = False,
    ) -> dict:
        """A lexeme dossier for export under ``target_licence``. Refused when it contains CC BY-SA Wiktionary
        content and the licence is not share-alike compatible (unless ``exclude_share_alike`` lists and drops it),
        and whenever attribution would be removed."""

        def run(conn):
            from src.kb.linguistics_queries import export_lexeme_dossier as export

            return export(
                queries(conn),
                namespace,
                lemma,
                languoid,
                target_licence=target_licence,
                scopes=who()[1],
                keep_attribution=keep_attribution,
                exclude_share_alike=exclude_share_alike,
            )

        return read("export_lexeme_dossier", run)

    @mcp.tool()
    def linguistics_readiness(namespace: str) -> dict:
        """Per-provider acquisition state, the selected optional features (linguistics-wiktionary,
        linguistics-typology), live-verification status (unverified-live until a dated live run) and exclusions."""

        def run(conn):
            from src.kb.linguistics_bundle import readiness

            return readiness(conn, namespace, scopes=who()[1])

        return read("linguistics_readiness", run)

    @mcp.tool()
    def propose_languoid_matches(namespace: str) -> dict:
        """Propose candidates for language references that do not resolve deterministically (macrolanguages,
        retired or split ISO codes, shared ISO codes); stated Glottocodes and ISO codes resolve without a
        candidate. Candidates stay proposed until reviewed."""
        return write(
            "propose_languoid_matches",
            lambda conn: identity(conn).propose_languoid_matches(
                namespace, principal_id=who()[0], scopes=who()[1]
            ),
        )

    @mcp.tool()
    def propose_lexeme_matches(namespace: str) -> dict:
        """Propose cross-source lexeme candidates (same languoid, lemma and lexical category; a shared
        source-stated sense item upgrades them). Homographs are never paired and nothing is merged."""
        return write(
            "propose_lexeme_matches",
            lambda conn: identity(conn).propose_lexeme_matches(
                namespace, principal_id=who()[0], scopes=who()[1]
            ),
        )

    @mcp.tool()
    def review_linguistic_identity_match(
        namespace: str, candidate_id: str, decision: str, reason: str
    ) -> dict:
        """Accept or reject a proposed languoid or lexeme candidate; recorded as an entity-history decision."""
        return write(
            "review_linguistic_identity_match",
            lambda conn: identity(conn).review(
                namespace,
                candidate_id,
                decision,
                reason,
                principal_id=who()[0],
                scopes=who()[1],
            ),
        )

    @mcp.tool()
    def revert_linguistic_identity_match(
        namespace: str, candidate_id: str, reason: str
    ) -> dict:
        """Revert an accepted or rejected candidate; the earlier decision is never reactivated."""
        return write(
            "revert_linguistic_identity_match",
            lambda conn: identity(conn).revert(
                namespace, candidate_id, reason, principal_id=who()[0], scopes=who()[1]
            ),
        )

    @mcp.tool()
    def record_iso_code_events(namespace: str) -> dict:
        """Record acquired ISO 639-3 retirements, changes and splits as identity-history events (idempotent)."""

        def run(conn):
            events = identity(conn).record_iso_events(
                namespace, principal_id=who()[0], scopes=who()[1]
            )
            return {"events": events, "n": len(events)}

        return write("record_iso_code_events", run)

    @mcp.tool()
    def project_languoid_locations(
        namespace: str, geo_namespace: str = "global"
    ) -> dict:
        """Store each Glottolog point as a geometry and place and save a resolution awaiting Geospatial review."""
        return write(
            "project_languoid_locations",
            lambda conn: identity(conn).project_locations(
                namespace,
                geo_namespace=geo_namespace,
                principal_id=who()[0],
                scopes=who()[1],
            ),
        )

    @mcp.tool()
    def review_languoid_location(
        namespace: str, resolution_id: str, decision: str, reason: str
    ) -> dict:
        """Accept, reject or defer a languoid place resolution through the Geospatial owner."""
        return write(
            "review_languoid_location",
            lambda conn: identity(conn).review_location(
                namespace,
                resolution_id,
                decision,
                reason=reason,
                principal_id=who()[0],
                scopes=who()[1],
            ),
        )

    @mcp.tool()
    def create_linguistics_monitor(
        namespace: str,
        request_key: str,
        watch: str,
        target: str,
        delivery: dict | None = None,
    ) -> dict:
        """A subscription on a lexeme, sense, languoid (Glottocode) or WALS parameter; events definition_revised,
        sense_added, sense_removed, etymology_changed, classification_changed, iso_code_changed and
        feature_value_changed cite the old and new revision or release. No new scheduler."""
        return write(
            "create_linguistics_monitor",
            lambda conn: monitor(conn).create(
                namespace,
                request_key,
                watch=watch,
                target=target,
                principal_id=who()[0],
                scopes=who()[1],
                delivery=delivery,
            ),
        )

    @mcp.tool()
    def run_linguistics_monitor(
        subscription_id: str, watermark: int | None = None
    ) -> dict:
        """Evaluate a linguistics monitor at a committed watermark (the first run is an in_view baseline)."""
        return write(
            "run_linguistics_monitor",
            lambda conn: monitor(conn).run(
                subscription_id, watermark, principal_id=who()[0], scopes=who()[1]
            ),
        )

    @mcp.tool()
    def poll_linguistics_monitor(subscription_id: str, cursor: str = "") -> dict:
        """Poll a linguistics monitor's events through the subscription delivery path."""
        return read(
            "poll_linguistics_monitor",
            lambda conn: monitor(conn, initialize=False).poll(
                subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor
            ),
        )

    @mcp.tool()
    def index_linguistic_texts(namespace: str) -> dict:
        """Write current definitions and lemmas as noesis-language-text-v1 texts so multilingual_search surfaces
        lexeme senses; idempotent, one text per revision, licence kept in the metadata."""

        def run(conn):
            from src.kb.linguistics_crosslang import CrossLanguageLinks

            return CrossLanguageLinks(conn, initialize=True).index(
                namespace, principal_id=who()[0], scopes=who()[1]
            )

        return write("index_linguistic_texts", run)

    @mcp.tool()
    def propose_lexeme_alias(
        namespace: str, lexeme: str, entity_id: str, item: str
    ) -> dict:
        """Record a lexeme's lemma as a candidate multilingual alias of a canonical entity whose Wikidata item a
        sense of the lexeme states; only review_multilingual_alias can accept it."""

        def run(conn):
            from src.kb.linguistics_crosslang import CrossLanguageLinks

            return CrossLanguageLinks(conn, initialize=True).propose_alias(
                namespace,
                lexeme,
                entity_id,
                item,
                principal_id=who()[0],
                scopes=who()[1],
            )

        return write("propose_lexeme_alias", run)
