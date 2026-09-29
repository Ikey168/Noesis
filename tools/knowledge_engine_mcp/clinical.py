"""Clinical Evidence entry points: trials, publications, methodology, terms, evidence maps, monitoring.

Every tool except the status, contracts and enablement tools checks that the
bundle is selected; Science and shared providers are never gated by it.
Acquisition runs through the shared source-pack runtime
(``noesis-knowledge-engine.run_source_pack_execution`` with the
``clinical-evidence`` pack). No tool gives medical advice, recommends a dose
or treatment, or asserts a grade without its inputs.
"""

from src.kb.clinical_bundle import BUNDLE, readiness, require_enabled, set_enabled

CLINICAL_WRITES = {
    "set_clinical_bundle_enabled", "import_prospero_registration", "link_clinical_publications",
    "review_clinical_publication_link", "align_clinical_terms", "record_clinical_trial_design",
    "build_clinical_evidence_map", "create_clinical_monitor", "run_clinical_monitor",
}
# The optional ``surveillance`` feature's entry points (#1917): public-health surveillance series, case definitions,
# vintages, boundaries, citation links and monitors (record owner src/kb/surveillance.py, provider
# clinical.surveillance). Every answer carries the feature's never-sentence.
SURVEILLANCE_WRITES = {
    "import_surveillance_export", "align_surveillance_terms", "resolve_surveillance_geographies",
    "review_surveillance_resolution", "link_surveillance_series", "pin_surveillance_vintages",
    "create_surveillance_monitor", "run_surveillance_monitor",
}
SURVEILLANCE_TOOLS = SURVEILLANCE_WRITES | {
    "surveillance_readiness", "list_surveillance_series", "surveillance_series_values",
    "surveillance_definition_history", "expand_surveillance_condition", "surveillance_boundary_series",
    "replay_surveillance_query", "list_surveillance_resolutions", "compare_surveillance_vintages",
    "surveillance_reporting_delay", "surveillance_pin_status", "surveillance_series_links",
    "surveillance_series_claims", "poll_surveillance_monitor",
}
# The optional ``medicines`` feature's entry points (#2214): authorisation status and label text as of a date, label
# diffs, safety communications, regulatory timelines, RxNorm identity review, citation links and monitors (records
# in src/kb/clinical_medicines.py, provider clinical.medicines). Every answer carries the feature's boundary.
MEDICINES_WRITES = {
    "propose_medicine_identities", "review_medicine_identity", "revert_medicine_identity", "link_medicine_evidence",
    "create_medicines_monitor", "run_medicines_monitor",
    # Label diffs are stored as derived label-section-change records and reused (MR08), so these two write.
    "compare_medicine_labels", "medicine_regulatory_timeline",
}
MEDICINES_TOOLS = MEDICINES_WRITES | {
    "medicines_readiness", "medicine_status_as_of", "medicine_label_as_of", "medicine_safety_communications",
    "list_medicine_identity_matches",
    "poll_medicines_monitor",
}
CLINICAL_WRITES = CLINICAL_WRITES | SURVEILLANCE_WRITES | MEDICINES_WRITES
CLINICAL_TOOLS = CLINICAL_WRITES | {
    "clinical_bundle_status", "clinical_provider_contracts", "lookup_clinical_trial", "clinical_trial_history",
    "clinical_coverage_gaps", "clinical_outcome_switching", "expand_clinical_question",
    "inspect_clinical_evidence_map", "clinical_strength_view", "export_clinical_evidence_bundle",
    "poll_clinical_monitor",
} | SURVEILLANCE_TOOLS | MEDICINES_TOOLS
READ = "knowledge:clinical:read"
WRITE = "knowledge:clinical:write"
REVIEW = "knowledge:clinical:review"
GEO_READ = "knowledge:geospatial:read"
SCHEMA_REGISTER = "knowledge:schema:register"
SUBSCRIPTIONS_READ = "knowledge:subscriptions:read"
SUBSCRIPTIONS_WRITE = "knowledge:subscriptions:write"
# Every scope each surveillance tool always reads or writes. The Geospatial read scope needed only when a boundary is
# named rather than given by feature id is checked at call time (conditional scope).
SURVEILLANCE_SCOPES = {
    "surveillance_readiness": [READ],
    "list_surveillance_series": [READ],
    "surveillance_series_values": [READ],
    "surveillance_definition_history": [READ],
    "expand_surveillance_condition": [READ],
    "surveillance_boundary_series": [READ],
    "replay_surveillance_query": [READ],
    "list_surveillance_resolutions": [READ],
    "compare_surveillance_vintages": [READ],
    "surveillance_reporting_delay": [READ],
    "surveillance_pin_status": [READ],
    "surveillance_series_links": [READ],
    "surveillance_series_claims": [READ],
    "poll_surveillance_monitor": [READ, SUBSCRIPTIONS_READ],
    "import_surveillance_export": [WRITE],
    "align_surveillance_terms": [READ, WRITE, SCHEMA_REGISTER],
    "resolve_surveillance_geographies": [WRITE, GEO_READ],
    "review_surveillance_resolution": [REVIEW],
    "link_surveillance_series": [READ, WRITE],
    "pin_surveillance_vintages": [WRITE],
    "create_surveillance_monitor": [READ, SUBSCRIPTIONS_WRITE],
    "run_surveillance_monitor": [READ, SUBSCRIPTIONS_READ, SUBSCRIPTIONS_WRITE],
}
MEDICINES_SCOPES = {
    "medicines_readiness": [READ],
    "medicine_status_as_of": [READ],
    "medicine_label_as_of": [READ],
    "compare_medicine_labels": [READ],
    "medicine_safety_communications": [READ],
    "medicine_regulatory_timeline": [READ],
    "list_medicine_identity_matches": [READ],
    "poll_medicines_monitor": [READ, SUBSCRIPTIONS_READ],
    "propose_medicine_identities": [READ, WRITE],
    "review_medicine_identity": [REVIEW],
    "revert_medicine_identity": [REVIEW],
    "link_medicine_evidence": [READ, WRITE],
    "create_medicines_monitor": [READ, SUBSCRIPTIONS_WRITE],
    "run_medicines_monitor": [READ, SUBSCRIPTIONS_READ, SUBSCRIPTIONS_WRITE],
}
CLINICAL_SCOPES = {
    "set_clinical_bundle_enabled": ["operator"],
    "import_prospero_registration": ["knowledge:clinical:write"],
    # Linking reads the trials it links (clinical read); paper-family members also need the object-level
    # document:<id>:read grant of each document, which is checked per document and reported as family_errors.
    # Re-linking reads the paper families it created before (paper-family read) to extend them idempotently.
    "link_clinical_publications": ["knowledge:clinical:read", "knowledge:clinical:write",
                                   "knowledge:paper-family:read", "knowledge:paper-family:write"],
    "review_clinical_publication_link": ["knowledge:clinical:review"],
    # Alignment reads the namespace's records and the published MeSH module before registering modules.
    "align_clinical_terms": ["knowledge:clinical:read", "knowledge:clinical:write", "knowledge:schema:read",
                             "knowledge:schema:register"],
    "record_clinical_trial_design": ["knowledge:clinical:write", "knowledge:methodology:write"],
    "clinical_outcome_switching": ["knowledge:clinical:read", "knowledge:methodology:read"],
    "build_clinical_evidence_map": ["knowledge:clinical:write"],
    "create_clinical_monitor": ["knowledge:clinical:write", "knowledge:subscriptions:write"],
    "run_clinical_monitor": ["knowledge:clinical:write", "knowledge:subscriptions:write"],
    "poll_clinical_monitor": ["knowledge:clinical:read", "knowledge:subscriptions:read"],
}


def required_scopes(tool_name, mutability):
    if tool_name == "clinical_provider_contracts":
        return []
    if tool_name in SURVEILLANCE_SCOPES:
        return SURVEILLANCE_SCOPES[tool_name]
    if tool_name in MEDICINES_SCOPES:
        return MEDICINES_SCOPES[tool_name]
    return CLINICAL_SCOPES.get(tool_name, ["knowledge:clinical:write" if mutability == "write"
                                           else "knowledge:clinical:read"])


def register(mcp, safe, context):
    def gated(namespace, operation, *, write=False, scope="knowledge:clinical:read"):
        def run(conn):
            require_enabled(conn, namespace)
            return operation(conn)
        return safe(run, write=write, required_scope=scope)

    def who():
        return context()[0], context()[1]

    @mcp.tool()
    def clinical_bundle_status(namespace: str) -> dict:
        """Declared contributions and actual ready/fixture-only/unavailable/not-implemented state per provider."""
        return safe(lambda conn: {**readiness(conn, namespace, scopes=who()[1]), "declaration": BUNDLE},
                    required_scope="knowledge:clinical:read")

    @mcp.tool()
    def set_clinical_bundle_enabled(namespace: str, enabled: bool) -> dict:
        """Select or deselect Clinical Evidence through the lifecycle coordinator (activation receipt)."""
        return safe(lambda conn: set_enabled(conn, namespace, enabled, principal_id=who()[0], scopes=who()[1]),
                    write=True, required_scope="operator")

    @mcp.tool()
    def clinical_provider_contracts() -> dict:
        """Per-source access contracts, identifiers, not-implemented reasons and live-verification state."""
        from src.ingestion.clinical_providers import LIVE_VERIFICATION, PROVIDER_CONTRACTS
        return {"contracts": PROVIDER_CONTRACTS, "live_verification": LIVE_VERIFICATION,
                "source_pack": "clinical-evidence", "boundary": BUNDLE["boundary"]}

    @mcp.tool()
    def lookup_clinical_trial(namespace: str, registry: str, identifier: str) -> dict:
        """One registered trial with arms, outcomes, results postings, links and cross-registry disagreements."""
        from src.kb.clinical_records import ClinicalRecordError, ClinicalRecordStore

        def run(conn):
            store = ClinicalRecordStore(conn, initialize=False)
            record_id = store.trial_id(namespace, registry, identifier)
            if record_id is None:
                raise ClinicalRecordError("record_not_found", "trial is not recorded in this namespace")
            return {**store.trial(namespace, record_id, scopes=who()[1]), "boundary": BUNDLE["boundary"]}
        return gated(namespace, run)

    @mcp.tool()
    def clinical_trial_history(namespace: str, record_id: str) -> dict:
        """Every revision of a clinical record (registry versions) with classified changes and version conflicts."""
        from src.kb.clinical_records import ClinicalRecordStore
        return gated(namespace, lambda conn: ClinicalRecordStore(conn, initialize=False).history(
            namespace, record_id, scopes=who()[1]))

    @mcp.tool()
    def import_prospero_registration(namespace: str, export: dict, protocol_id: str | None = None) -> dict:
        """Record a user-supplied PROSPERO record export; optionally link it to a systematic-review protocol."""
        from src.kb.clinical_reviews import import_prospero_registration as run_import
        return gated(namespace, lambda conn: run_import(conn, namespace, export, principal_id=who()[0],
                                                        scopes=who()[1], protocol_id=protocol_id),
                     write=True, scope="knowledge:clinical:write")

    @mcp.tool()
    def link_clinical_publications(namespace: str, observation: str, document_ids: list[str] | None = None) -> dict:
        """Link trials to harvested publications from declared identifiers and paper families (never inferred)."""
        from src.kb.clinical_publications import PublicationLinker
        return gated(namespace, lambda conn: PublicationLinker(conn).link(
            namespace, principal_id=who()[0], scopes=who()[1], observation_id=observation,
            document_ids=document_ids), write=True, scope="knowledge:clinical:write")

    @mcp.tool()
    def review_clinical_publication_link(namespace: str, link_id: str, decision: str, rationale: str,
                                         observation: str) -> dict:
        """Accept or reject a text-mention candidate link (independent review)."""
        from src.kb.clinical_publications import PublicationLinker
        return gated(namespace, lambda conn: PublicationLinker(conn).review_candidate(
            namespace, link_id, decision, rationale, principal_id=who()[0], scopes=who()[1],
            observation_id=observation), write=True, scope="knowledge:clinical:review")

    @mcp.tool()
    def clinical_coverage_gaps(namespace: str) -> dict:
        """Unlinked trials, unregistered trial publications, unharvested references and pending candidates."""
        from src.kb.clinical_publications import PublicationLinker
        return gated(namespace, lambda conn: PublicationLinker(conn, initialize=False).coverage_gaps(
            namespace, scopes=who()[1]))

    @mcp.tool()
    def record_clinical_trial_design(namespace: str, record_id: str) -> dict:
        """Register every registry version of a trial in methodology provenance, with publication outcomes."""
        from src.kb.clinical_evidence import EvidenceMapService
        from src.kb.clinical_methodology import TrialMethodology

        def run(conn):
            methods = TrialMethodology(conn)
            synced = methods.sync(namespace, record_id, principal_id=who()[0], scopes=who()[1])
            documents = EvidenceMapService(conn).linked_documents(namespace, scopes=who()[1])
            extracted = methods.record_publication_outcomes(namespace, synced["study_id"], documents,
                                                            principal_id=who()[0], scopes=who()[1]) \
                if "knowledge:methodology:extract" in who()[1] or "operator" in who()[1] else []
            return {**synced, "publication_extractions": [e["extraction_id"] for e in extracted]}
        return gated(namespace, run, write=True, scope="knowledge:clinical:write")

    @mcp.tool()
    def clinical_outcome_switching(namespace: str, study_id: str) -> dict:
        """Outcome-switching findings (added, removed, re-timed, demoted, publication differs), both sides cited."""
        from src.kb.methodology_provenance import MethodologyStore
        return gated(namespace, lambda conn: MethodologyStore(conn, initialize=False).outcome_switching(
            namespace, study_id, scopes=who()[1]))

    @mcp.tool()
    def align_clinical_terms(namespace: str, mesh_version: str, curations: list[dict] | None = None,
                             mesh_descriptors: list[dict] | None = None) -> dict:
        """Publish registry-native terms and their MeSH crosswalk (kinds preserved); unmapped terms are listed."""
        from src.kb.clinical_terms import ClinicalTerms

        def run(conn):
            terms = ClinicalTerms(conn)
            if mesh_descriptors:
                terms.publish_mesh(mesh_descriptors, mesh_version, principal_id=who()[0], scopes=who()[1])
            return terms.align(namespace, principal_id=who()[0], scopes=who()[1], mesh_version=mesh_version,
                               curations=curations or [])
        return gated(namespace, run, write=True, scope="knowledge:clinical:write")

    @mcp.tool()
    def expand_clinical_question(namespace: str, question: dict) -> dict:
        """Explained term expansion for a clinical question through the MeSH crosswalk."""
        from src.kb.clinical_evidence import EvidenceMapService, validate_question

        def run(conn):
            valid = validate_question(question)
            answer = {"question": valid, "expansion": EvidenceMapService(conn, initialize=False)._expand(
                namespace, valid, who()[1])}
            if conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name='surveillance_series'"
                            ).fetchone():
                # Surveillance series of the expanded condition (the optional surveillance feature's records).
                from src.kb.clinical_terms import ClinicalTerms

                answer["surveillance"] = ClinicalTerms(conn, initialize=False).expand_surveillance(
                    namespace, valid["condition"], scopes=who()[1])
            return answer
        return gated(namespace, run)

    @mcp.tool()
    def build_clinical_evidence_map(namespace: str, question: dict, request_key: str) -> dict:
        """Question to cited evidence map with an explained, rule-based strength view (not medical advice)."""
        from src.kb.clinical_evidence import EvidenceMapService
        return gated(namespace, lambda conn: EvidenceMapService(conn).build(
            namespace, question, principal_id=who()[0], scopes=who()[1], request_key=request_key),
            write=True, scope="knowledge:clinical:write")

    @mcp.tool()
    def inspect_clinical_evidence_map(namespace: str, view_id: str, revision: int | None = None) -> dict:
        """A stored evidence map revision and whether record changes have made it stale."""
        from src.kb.clinical_evidence import EvidenceMapService
        return gated(namespace, lambda conn: EvidenceMapService(conn, initialize=False).inspect(
            namespace, view_id, scopes=who()[1], revision=revision))

    @mcp.tool()
    def clinical_strength_view(namespace: str, view_id: str, revision: int | None = None) -> dict:
        """The strength-of-evidence view of a map: rules, traced inputs, unknowns, GRADE inputs missing."""
        from src.kb.clinical_evidence import EvidenceMapService

        def run(conn):
            content = EvidenceMapService(conn, initialize=False).inspect(namespace, view_id, scopes=who()[1],
                                                                         revision=revision)
            return {**content["strength"], "view_id": view_id, "revision": content["revision"],
                    "freshness": content["freshness"], "question": content["question"]}
        return gated(namespace, run)

    @mcp.tool()
    def export_clinical_evidence_bundle(namespace: str, view_id: str, revision: int | None = None) -> dict:
        """Export a map revision as a noesis-evidence-bundle-v1 with coverage gaps as omissions."""
        from src.kb.clinical_evidence import EvidenceMapService
        return gated(namespace, lambda conn: EvidenceMapService(conn, initialize=False).export_bundle(
            namespace, view_id, scopes=who()[1], revision=revision))

    @mcp.tool()
    def create_clinical_monitor(namespace: str, view_id: str, request_key: str, delivery: dict | None = None) -> dict:
        """Watch a map's trials and regulatory records through a knowledge subscription (no new scheduler)."""
        from src.kb.clinical_monitoring import ClinicalMonitor
        return gated(namespace, lambda conn: ClinicalMonitor(conn).create(
            namespace, view_id, request_key, principal_id=who()[0], scopes=who()[1], delivery=delivery),
            write=True, scope="knowledge:clinical:write")

    @mcp.tool()
    def run_clinical_monitor(namespace: str, subscription_id: str, watermark: int) -> dict:
        """Evaluate a monitor at a committed watermark; replaying a watermark creates no new events."""
        from src.kb.clinical_monitoring import ClinicalMonitor
        return gated(namespace, lambda conn: ClinicalMonitor(conn).run(
            subscription_id, watermark, principal_id=who()[0], scopes=who()[1]),
            write=True, scope="knowledge:clinical:write")

    @mcp.tool()
    def poll_clinical_monitor(namespace: str, subscription_id: str, cursor: str = "") -> dict:
        """Poll monitor events after a cursor."""
        from src.kb.clinical_monitoring import ClinicalMonitor
        return gated(namespace, lambda conn: ClinicalMonitor(conn, initialize=False).poll(
            subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor))

    register_surveillance(mcp, gated, who)
    register_medicines(mcp, gated, who)


def register_surveillance(mcp, gated, who):
    """The optional ``surveillance`` feature's tools; each answer carries the never-sentence."""
    from src.kb.surveillance import NEVER_SENTENCE

    def sv(namespace, operation, *, write=False, scope=READ):
        def run(conn):
            result = operation(conn)
            return {**result, "boundary": NEVER_SENTENCE} if isinstance(result, dict) else result
        return gated(namespace, run, write=write, scope=scope)

    @mcp.tool()
    def surveillance_readiness(namespace: str) -> dict:
        """Whether the surveillance feature is selected, per-provider access decisions, releases and evidence origin."""
        from src.kb.surveillance import authorize, readiness

        def run(conn):
            authorize(namespace, who()[1], READ)
            from src.ingestion.surveillance_sources import PROVIDER_CONTRACTS

            return {**readiness(conn), "contracts": PROVIDER_CONTRACTS}
        return sv(namespace, run)

    @mcp.tool()
    def list_surveillance_series(namespace: str, provider: str | None = None, condition_scheme: str | None = None,
                                 condition_code: str | None = None, geography_system: str | None = None,
                                 geography_code: str | None = None, kind: str | None = None) -> dict:
        """Series with source, condition, geography code and system, indicator, unit, interval, kind and breaks."""
        from src.kb.surveillance import SurveillanceStore, authorize

        def run(conn):
            authorize(namespace, who()[1], READ)
            return {"series": SurveillanceStore(conn, initialize=False).find_series(
                namespace, provider=provider, condition_scheme=condition_scheme,
                condition_codes=[condition_code] if condition_code else None, geography_system=geography_system,
                geography_code=geography_code, kind=kind)}
        return sv(namespace, run)

    @mcp.tool()
    def surveillance_series_values(namespace: str, series_id: str, vintage_id: str | None = None,
                                   reporting_as_of: str | None = None, period_from: str | None = None,
                                   period_to: str | None = None) -> dict:
        """Values of one vintage with reporting date and reference date apart, kind, case definition and source
        revision; as of a reporting date, values without one are listed apart. Nothing interpolated."""
        from src.kb.surveillance import SurveillanceStore

        return sv(namespace, lambda conn: SurveillanceStore(conn, initialize=False).answer(
            namespace, series_id, scopes=who()[1], vintage_id=vintage_id, reporting_as_of=reporting_as_of,
            period_from=period_from, period_to=period_to))

    @mcp.tool()
    def surveillance_definition_history(namespace: str, definition_key: str) -> dict:
        """Every case-definition revision of a condition: editions by valid-from, corrections by declaration."""
        from src.kb.surveillance import SurveillanceStore, authorize

        def run(conn):
            authorize(namespace, who()[1], READ)
            return SurveillanceStore(conn, initialize=False).definition_history(namespace, definition_key)
        return sv(namespace, run)

    @mcp.tool()
    def expand_surveillance_condition(namespace: str, condition: str) -> dict:
        """Series of an expanded condition through the MeSH and ICD crosswalks, steps explained, gaps listed."""
        from src.kb.clinical_terms import ClinicalTerms

        return sv(namespace, lambda conn: ClinicalTerms(conn, initialize=False).expand_surveillance(
            namespace, condition, scopes=who()[1]))

    @mcp.tool()
    def surveillance_boundary_series(namespace: str, feature_id: str | None = None,
                                     boundary_name: str | None = None, boundary_collection: str | None = None,
                                     geo_namespace: str = "global", condition: str | None = None,
                                     reporting_as_of: str | None = None, period_from: str | None = None,
                                     period_to: str | None = None, kind: str | None = None) -> dict:
        """Series for a condition within a boundary as of a reporting date, sources side by side with a receipt.
        Conditional scope: naming the boundary (boundary_name) also needs knowledge:geospatial:read."""
        from src.kb.surveillance_places import SurveillancePlaces

        return sv(namespace, lambda conn: SurveillancePlaces(conn, initialize=False).boundary_series(
            namespace, scopes=who()[1], feature_id=feature_id, boundary_name=boundary_name,
            boundary_collection=boundary_collection, geo_namespace=geo_namespace, condition=condition,
            reporting_as_of=reporting_as_of, period_from=period_from, period_to=period_to, kind=kind))

    @mcp.tool()
    def replay_surveillance_query(namespace: str, receipt: dict) -> dict:
        """Re-run a boundary query receipt with every recorded parameter: reproduced or changed.
        Conditional scope: a receipt that names its boundary also needs knowledge:geospatial:read."""
        from src.kb.surveillance import SurveillanceError
        from src.kb.surveillance_places import SurveillancePlaces

        def run(conn):
            if dict(dict(receipt or {}).get("request") or {}).get("namespace") != namespace:
                raise SurveillanceError("invalid_receipt", "the receipt belongs to another namespace")
            return SurveillancePlaces(conn, initialize=False).replay(receipt, scopes=who()[1])
        return sv(namespace, run)

    @mcp.tool()
    def list_surveillance_resolutions(namespace: str, current_only: bool = True) -> dict:
        """Geography-code resolutions with code system, code-list version, boundary revision and review state."""
        from src.kb.surveillance_places import SurveillancePlaces

        return sv(namespace, lambda conn: {"resolutions": SurveillancePlaces(conn, initialize=False).resolutions(
            namespace, scopes=who()[1], current_only=current_only)})

    @mcp.tool()
    def compare_surveillance_vintages(namespace: str, series_id: str, left: str | None = None,
                                      right: str | None = None) -> dict:
        """Differences between two published vintages, both cited; definition changes shown as such."""
        from src.kb.surveillance_vintages import compare

        return sv(namespace, lambda conn: compare(conn, namespace, series_id, scopes=who()[1], left=left, right=right))

    @mcp.tool()
    def surveillance_reporting_delay(namespace: str, series_id: str) -> dict:
        """Per reference period: first-reported and later values with the source's own delay note; no nowcast."""
        from src.kb.surveillance_vintages import reporting_delay

        return sv(namespace, lambda conn: reporting_delay(conn, namespace, series_id, scopes=who()[1]))

    @mcp.tool()
    def surveillance_pin_status(namespace: str, view_key: str | None = None) -> dict:
        """Pinned vintages of downstream views with their values; stale when a newer vintage exists."""
        from src.kb.surveillance_vintages import pin_status

        return sv(namespace, lambda conn: pin_status(conn, namespace, view_key, scopes=who()[1]))

    @mcp.tool()
    def surveillance_series_links(namespace: str, series_id: str) -> dict:
        """Publications and trials that explicitly cite a series' dataset, and pending mention candidates."""
        from src.kb.clinical_publications import PublicationLinker

        return sv(namespace, lambda conn: PublicationLinker(conn, initialize=False).series_links(
            namespace, series_id, scopes=who()[1]))

    @mcp.tool()
    def surveillance_series_claims(namespace: str, series_id: str) -> dict:
        """Science-layer claims of the documents citing a series, beside it; no conclusion is drawn."""
        from src.kb.clinical_publications import PublicationLinker

        return sv(namespace, lambda conn: PublicationLinker(conn, initialize=False).series_claims(
            namespace, series_id, scopes=who()[1]))

    @mcp.tool()
    def poll_surveillance_monitor(namespace: str, subscription_id: str, cursor: str = "") -> dict:
        """Poll a surveillance monitor's events after a cursor."""
        from src.kb.surveillance_monitoring import SurveillanceMonitor

        return sv(namespace, lambda conn: SurveillanceMonitor(conn, initialize=False).poll(
            subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor))

    @mcp.tool()
    def import_surveillance_export(namespace: str, export: dict) -> dict:
        """Record an operator-supplied ECDC Surveillance Atlas export (the atlas is never fetched or scraped)."""
        from src.kb.surveillance import SurveillanceStore

        return sv(namespace, lambda conn: SurveillanceStore(conn).import_export(
            namespace, export, principal_id=who()[0], scopes=who()[1]), write=True, scope=WRITE)

    @mcp.tool()
    def align_surveillance_terms(namespace: str, mesh_version: str, icd: dict | None = None,
                                 curations: list[dict] | None = None, icd_codes: list[dict] | None = None,
                                 icd_mesh_curations: list[dict] | None = None) -> dict:
        """Publish surveillance condition terms with crosswalks to MeSH (labels) and ICD (codes); optionally
        publish the ICD module ({system, version}) first. Kinds preserved; unmapped terms listed."""
        from src.kb.clinical_terms import ClinicalTerms

        def run(conn):
            terms = ClinicalTerms(conn)
            published = None
            if icd and icd_codes:
                published = terms.publish_icd(icd_codes, icd["version"], system=icd["system"], principal_id=who()[0],
                                              scopes=who()[1], mesh_version=mesh_version,
                                              curations=icd_mesh_curations or [])
            aligned = terms.align_surveillance(namespace, principal_id=who()[0], scopes=who()[1],
                                               mesh_version=mesh_version, icd=icd, curations=curations or [])
            return {**aligned, "icd_published": published}
        return sv(namespace, run, write=True, scope=WRITE)

    @mcp.tool()
    def resolve_surveillance_geographies(namespace: str, geo_namespace: str = "global",
                                         collections: dict | None = None) -> dict:
        """Resolve every series geography code to a boundary feature by the published code; unresolved stay so."""
        from src.kb.surveillance_places import SurveillancePlaces

        return sv(namespace, lambda conn: SurveillancePlaces(conn).resolve_geographies(
            namespace, principal_id=who()[0], scopes=who()[1], geo_namespace=geo_namespace,
            collections=collections), write=True, scope=WRITE)

    @mcp.tool()
    def review_surveillance_resolution(namespace: str, resolution_id: str, decision: str, reason: str) -> dict:
        """Accept or reject a geography resolution; a rejected resolution is not used."""
        from src.kb.surveillance_places import SurveillancePlaces

        return sv(namespace, lambda conn: SurveillancePlaces(conn).review(
            namespace, resolution_id, decision, reason, principal_id=who()[0], scopes=who()[1]),
            write=True, scope=REVIEW)

    @mcp.tool()
    def link_surveillance_series(namespace: str, observation: str, document_ids: list[str] | None = None) -> dict:
        """Link series to publications and trials by explicit dataset citation only; mentions become candidates."""
        from src.kb.clinical_publications import PublicationLinker

        return sv(namespace, lambda conn: PublicationLinker(conn).link_series(
            namespace, principal_id=who()[0], scopes=who()[1], observation_id=observation,
            document_ids=document_ids), write=True, scope=WRITE)

    @mcp.tool()
    def pin_surveillance_vintages(namespace: str, view_key: str, vintage_ids: list[str]) -> dict:
        """Keep the vintages a downstream view used; later vintages mark the pin stale, never replace it."""
        from src.kb.surveillance_vintages import pin

        return sv(namespace, lambda conn: pin(conn, namespace, view_key, vintage_ids, principal_id=who()[0],
                                              scopes=who()[1]), write=True, scope=WRITE)

    @mcp.tool()
    def create_surveillance_monitor(namespace: str, request_key: str, watch: dict, thresholds: list[dict] | None = None,
                                    delivery: dict | None = None) -> dict:
        """Watch a series or a condition within a boundary; thresholds are user-configured and unit-checked."""
        from src.kb.surveillance_monitoring import SurveillanceMonitor

        return sv(namespace, lambda conn: SurveillanceMonitor(conn).create(
            namespace, request_key, watch=watch, principal_id=who()[0], scopes=who()[1],
            thresholds=thresholds or [], delivery=delivery), write=True, scope=READ)

    @mcp.tool()
    def run_surveillance_monitor(namespace: str, subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate a monitor at a committed watermark; replaying a watermark emits no new events."""
        from src.kb.surveillance_monitoring import SurveillanceMonitor

        return sv(namespace, lambda conn: SurveillanceMonitor(conn).run(
            subscription_id, watermark, principal_id=who()[0], scopes=who()[1]), write=True, scope=READ)


def register_medicines(mcp, gated, who):
    """The optional ``medicines`` feature's tools; each answer carries the feature's boundary sentence."""
    from src.kb.clinical_medicines import BOUNDARY

    def md(namespace, operation, *, write=False, scope=READ):
        def run(conn):
            result = operation(conn)
            return {**result, "boundary": BOUNDARY} if isinstance(result, dict) else result
        return gated(namespace, run, write=write, scope=scope)

    @mcp.tool()
    def medicines_readiness(namespace: str) -> dict:
        """Whether the medicines feature is selected, per-source access decisions, freshness and live state."""
        from src.ingestion.medicines_sources import BOUNDED_SET, LIVE_VERIFICATION, PROVIDER_CONTRACTS
        from src.kb.clinical_medicines import feature_enabled
        from src.kb.clinical_records import ClinicalRecordStore, _require_read

        def run(conn):
            _require_read(namespace, who()[1])
            store = ClinicalRecordStore(conn, initialize=False)
            return {"feature": "medicines", "selected": feature_enabled(conn), "contracts": PROVIDER_CONTRACTS,
                    "live_verification": LIVE_VERIFICATION, "bounded_set": BOUNDED_SET,
                    "providers": {p: store.provider_state(namespace, p) for p in PROVIDER_CONTRACTS}}
        return md(namespace, run)

    @mcp.tool()
    def medicine_status_as_of(namespace: str, medicine: str, as_of: str, jurisdiction: str | None = None) -> dict:
        """Authorisation status per jurisdiction and product on a date, cited, with the identity match used."""
        from src.kb.clinical_medicines import MedicinesService

        return md(namespace, lambda conn: MedicinesService(conn, initialize=False).status_as_of(
            namespace, medicine, as_of, scopes=who()[1], jurisdiction=jurisdiction))

    @mcp.tool()
    def medicine_label_as_of(namespace: str, medicine: str, as_of: str, provider: str | None = None) -> dict:
        """The label revision in force on a date per label document: sections verbatim with locators (dosing
        sections are listed as omitted, never quoted)."""
        from src.kb.clinical_medicines import MedicinesService

        return md(namespace, lambda conn: MedicinesService(conn, initialize=False).label_as_of(
            namespace, medicine, as_of, scopes=who()[1], provider=provider))

    @mcp.tool()
    def compare_medicine_labels(namespace: str, medicine: str | None = None, left_record_id: str | None = None,
                                right_record_id: str | None = None, document_id: str | None = None,
                                from_version: str | None = None, to_version: str | None = None) -> dict:
        """Section-by-section changes between two label revisions (two record ids, or a medicine's consecutive or
        chosen versions), quoting both revisions; unaligned sections reported, no significance rating."""
        from src.kb.clinical_medicines import MedicinesService
        from src.kb.clinical_records import ClinicalRecordError

        def run(conn):
            service = MedicinesService(conn, initialize=False)
            if left_record_id and right_record_id:
                return service.diff(namespace, left_record_id, right_record_id, scopes=who()[1])
            if not medicine:
                raise ClinicalRecordError("invalid_request", "name a medicine or two label-revision record ids")
            return service.what_changed(namespace, medicine, scopes=who()[1], document_id=document_id,
                                        from_version=from_version, to_version=to_version)
        return md(namespace, run, write=True, scope=READ)

    @mcp.tool()
    def medicine_safety_communications(namespace: str, substance: str) -> dict:
        """FDA Drug Safety Communications naming a substance, with dated updates, quotes and the match used."""
        from src.kb.clinical_medicines import MedicinesService

        return md(namespace, lambda conn: MedicinesService(conn, initialize=False).communications(
            namespace, substance, scopes=who()[1]))

    @mcp.tool()
    def medicine_regulatory_timeline(namespace: str, medicine: str, view_id: str | None = None) -> dict:
        """Authorisations, label revisions with section diffs and safety communications in date order, sources
        side by side, with explicitly cited trials and FAERS reporting counts; none on record is said so."""
        from src.kb.clinical_medicines import MedicinesService

        return md(namespace, lambda conn: MedicinesService(conn, initialize=False).timeline(
            namespace, medicine, scopes=who()[1], view_id=view_id), write=True, scope=READ)

    @mcp.tool()
    def list_medicine_identity_matches(namespace: str, state: str | None = None) -> dict:
        """RxNorm crosswalk records (kind, evidence, RxNorm release, review state and decisions)."""
        from src.kb.clinical_terms import MedicineIdentity

        return md(namespace, lambda conn: {"matches": MedicineIdentity(conn, initialize=False).matches(
            namespace, scopes=who()[1], state=state)})

    @mcp.tool()
    def propose_medicine_identities(namespace: str) -> dict:
        """Look published medicine and substance names up in RxNav (bounded, receipted) and record crosswalk
        candidates; EU products match by active substance only and wait for review; unmatched names reported."""
        from src.ingestion.medicines_sources import RxNavClient
        from src.kb.clinical_terms import MedicineIdentity

        return md(namespace, lambda conn: MedicineIdentity(conn).propose(
            namespace, principal_id=who()[0], scopes=who()[1], client=RxNavClient()), write=True, scope=WRITE)

    @mcp.tool()
    def review_medicine_identity(namespace: str, match_id: str, decision: str, reason: str) -> dict:
        """Accept or reject a medicine identity match with a reason."""
        from src.kb.clinical_terms import MedicineIdentity

        return md(namespace, lambda conn: MedicineIdentity(conn).review(
            namespace, match_id, decision, reason, principal_id=who()[0], scopes=who()[1]), write=True,
            scope=REVIEW)

    @mcp.tool()
    def revert_medicine_identity(namespace: str, match_id: str, reason: str) -> dict:
        """Undo the latest review decision of a medicine identity match; decisions are kept."""
        from src.kb.clinical_terms import MedicineIdentity

        return md(namespace, lambda conn: MedicineIdentity(conn).revert(
            namespace, match_id, reason, principal_id=who()[0], scopes=who()[1]), write=True, scope=REVIEW)

    @mcp.tool()
    def link_medicine_evidence(namespace: str, observation: str, document_ids: list[str] | None = None) -> dict:
        """Link regulatory records to trials and publications they cite, and to FAERS counts by reviewed identity."""
        from src.kb.clinical_publications import PublicationLinker

        return md(namespace, lambda conn: PublicationLinker(conn).link_medicines(
            namespace, principal_id=who()[0], scopes=who()[1], observation_id=observation,
            document_ids=document_ids), write=True, scope=WRITE)

    @mcp.tool()
    def create_medicines_monitor(namespace: str, medicine: str, request_key: str, delivery: dict | None = None) -> dict:
        """Watch a medicine or substance for authorisation changes, label revisions and safety communications."""
        from src.kb.clinical_monitoring import MedicinesMonitor

        return md(namespace, lambda conn: MedicinesMonitor(conn).create_medicine(
            namespace, medicine, request_key, principal_id=who()[0], scopes=who()[1], delivery=delivery),
            write=True, scope=READ)

    @mcp.tool()
    def run_medicines_monitor(namespace: str, subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate a medicines monitor at a committed watermark; unchanged records deliver nothing."""
        from src.kb.clinical_monitoring import MedicinesMonitor

        return md(namespace, lambda conn: MedicinesMonitor(conn).run_medicine(
            subscription_id, watermark, principal_id=who()[0], scopes=who()[1]), write=True, scope=READ)

    @mcp.tool()
    def poll_medicines_monitor(namespace: str, subscription_id: str, cursor: str = "") -> dict:
        """Poll medicines monitor events after a cursor."""
        from src.kb.clinical_monitoring import MedicinesMonitor

        return md(namespace, lambda conn: MedicinesMonitor(conn, initialize=False).poll(
            subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor))
