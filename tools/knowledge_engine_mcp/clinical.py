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
CLINICAL_TOOLS = CLINICAL_WRITES | {
    "clinical_bundle_status", "clinical_provider_contracts", "lookup_clinical_trial", "clinical_trial_history",
    "clinical_coverage_gaps", "clinical_outcome_switching", "expand_clinical_question",
    "inspect_clinical_evidence_map", "clinical_strength_view", "export_clinical_evidence_bundle",
    "poll_clinical_monitor",
}
CLINICAL_SCOPES = {
    "set_clinical_bundle_enabled": ["operator"],
    "import_prospero_registration": ["knowledge:clinical:write"],
    "link_clinical_publications": ["knowledge:clinical:write", "knowledge:paper-family:write"],
    "review_clinical_publication_link": ["knowledge:clinical:review"],
    "align_clinical_terms": ["knowledge:clinical:write", "knowledge:schema:register"],
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
            return {"question": valid, "expansion": EvidenceMapService(conn, initialize=False)._expand(
                namespace, valid, who()[1])}
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
