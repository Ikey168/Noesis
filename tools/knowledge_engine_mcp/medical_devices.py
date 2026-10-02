"""Clinical Evidence medical devices entry points (#2654): the ``clinical.devices`` provider's tools.

Registered through :mod:`tools.knowledge_engine_mcp.clinical` (the Clinical
Evidence tools, gated by the bundle's selection); acquisition runs through
``noesis-knowledge-engine.run_source_pack_execution`` with the
``clinical-evidence`` 0.1.4 medical-devices sources (``clinical-devices-*``).
openFDA, AccessGUDID and EUDAMED coverage are the optional
``medical-devices-fda``, ``medical-devices-gudid`` and ``medical-devices-eudamed``
features (default off).

Exclusions: no safety-signal detection, no causality from adverse-event
reports, no clinical advice and no patient data beyond what regulators
publish. Every answer carries the review boundary, is checked for assessment
keys and personal fields before it is returned (MD01), and returns MAUDE
narratives only to principals holding the narrative scope.
"""

DEVICE_WRITES = {
    "propose_medical_device_identities",
    "review_medical_device_identity",
    "revert_medical_device_identity",
    "link_medical_device_records",
    "create_medical_device_monitor",
    "run_medical_device_monitor",
}
DEVICE_READS = {
    "medical_device_source_contracts",
    "medical_devices_readiness",
    "medical_device_record_history",
    "medical_device_regulatory_history",
    "medical_device_adverse_event_counts",
    "list_medical_device_identity_candidates",
    "list_medical_device_links",
    "export_medical_device_evidence_bundle",
    "poll_medical_device_monitor",
}
DEVICE_TOOLS = DEVICE_WRITES | DEVICE_READS
READ = "knowledge:clinical:read"
WRITE = "knowledge:clinical:write"
REVIEW = "knowledge:clinical:review"
SUBSCRIPTIONS_READ = "knowledge:subscriptions:read"
SUBSCRIPTIONS_WRITE = "knowledge:subscriptions:write"
# Every scope each tool always reads or writes. The ownership and Products read scopes needed only when an ownership
# or Products namespace is named, and the narrative scope, are checked at call time (conditional scopes).
DEVICE_SCOPES = {
    "medical_device_source_contracts": [],
    "medical_devices_readiness": [READ],
    "medical_device_record_history": [READ],
    "medical_device_regulatory_history": [READ],
    "medical_device_adverse_event_counts": [READ],
    "list_medical_device_identity_candidates": [READ],
    "list_medical_device_links": [READ],
    "export_medical_device_evidence_bundle": [READ],
    "poll_medical_device_monitor": [READ, SUBSCRIPTIONS_READ],
    "propose_medical_device_identities": [READ, WRITE],
    "review_medical_device_identity": [REVIEW],
    "revert_medical_device_identity": [REVIEW],
    "link_medical_device_records": [READ, WRITE],
    "create_medical_device_monitor": [READ, SUBSCRIPTIONS_WRITE],
    "run_medical_device_monitor": [READ, SUBSCRIPTIONS_READ, SUBSCRIPTIONS_WRITE],
}
EXCLUSIONS = ("no safety-signal detection, no causality from adverse-event reports, no clinical advice, no patient "
              "data beyond what regulators publish")


def checked(result):
    """Attach the boundary and refuse an answer that carries an assessment key or a personal field (MD01)."""
    from src.ingestion.medical_devices_sources import REVIEW_BOUNDARY
    from src.kb.medical_devices_records import (
        MedicalDeviceError,
        forbidden_keys,
        personal_fields,
    )

    if not isinstance(result, dict):
        return result
    problems = forbidden_keys(result) + personal_fields(result)
    if problems:
        raise MedicalDeviceError("outside_boundary", "answer withheld: it would carry an assessment or a personal "
                                                     "field", paths=problems)
    return {**result, "boundary": REVIEW_BOUNDARY, "exclusions": EXCLUSIONS}


def register(mcp, gated, who):
    def md(namespace, operation, *, write=False, scope=READ):
        return gated(namespace, lambda conn: checked(operation(conn)), write=write, scope=scope)

    @mcp.tool()
    def medical_device_source_contracts() -> dict:
        """Access decisions, licences, rate limits, revision models, EUDAMED module availability, the MD01
        minimisation decision, bounded coverage and LIVE_VERIFICATION for openFDA, AccessGUDID and EUDAMED."""
        from src.ingestion.medical_devices_sources import (
            BOUNDED_COVERAGE,
            DECLINED,
            EUDAMED_MODULES,
            FEATURES,
            IDENTIFIERS,
            LIVE_VERIFICATION,
            MINIMISATION,
            PROVIDER_CONTRACTS,
        )

        return checked({"contracts": PROVIDER_CONTRACTS, "eudamed_modules": EUDAMED_MODULES, "declined": DECLINED,
                        "identifiers": IDENTIFIERS, "minimisation": MINIMISATION,
                        "bounded_coverage": BOUNDED_COVERAGE, "features": FEATURES,
                        "live_verification": LIVE_VERIFICATION, "source_pack": "clinical-evidence"})

    @mcp.tool()
    def medical_devices_readiness(namespace: str = "clinical") -> dict:
        """Which device features are selected, records per provider, live-verification state and EUDAMED gaps."""
        from src.kb.medical_devices_records import authorize, readiness

        def run(conn):
            authorize(namespace, who()[1], READ)
            return readiness(conn, namespace)
        return md(namespace, run)

    @mcp.tool()
    def medical_device_record_history(namespace: str, record_key: str) -> dict:
        """Every revision of one device, clearance, approval, supplement, recall, report or certificate record with
        source, revision and as-of time; removals and corrections are revisions. Narratives need the narrative
        scope."""
        from src.kb.medical_devices_records import MedicalDeviceStore

        return md(namespace, lambda conn: {"record_key": record_key, "revisions": MedicalDeviceStore(
            conn, initialize=False).history(namespace, record_key, scopes=who()[1])})

    @mcp.tool()
    def medical_device_regulatory_history(namespace: str, subject: str, as_of: str) -> dict:
        """A device (K/P number, DI, Basic UDI-DI) or FDA product code to its clearances, approvals, supplements,
        recalls and EU certificates as of a date, each citing its record revision; US and EU side by side;
        unavailable EUDAMED modules and missing providers stated. No safety verdict or clinical advice."""
        from src.kb.medical_devices_queries import regulatory_history

        return md(namespace, lambda conn: regulatory_history(conn, namespace, subject, as_of, scopes=who()[1]))

    @mcp.tool()
    def medical_device_adverse_event_counts(namespace: str, subject: str, received_from: str,
                                            received_to: str) -> dict:
        """MAUDE adverse-event report counts per event type and period as published, beside the reports on record,
        with FDA's caveats, the query window and source revisions. Counts are reports - never rates, incidence or
        causal events; no signal detection."""
        from src.kb.medical_devices_queries import adverse_event_counts

        return md(namespace, lambda conn: adverse_event_counts(conn, namespace, subject, received_from, received_to,
                                                               scopes=who()[1]))

    @mcp.tool()
    def export_medical_device_evidence_bundle(namespace: str, subject: str, as_of: str,
                                              received_from: str | None = None,
                                              received_to: str | None = None) -> dict:
        """An evidence bundle of the regulatory history (and report counts with a window): every item cites its
        source, record revision and as-of time; caveats, gaps and exclusions included; narratives never exported."""
        from src.kb.medical_devices_queries import MedicalDeviceQueries

        return md(namespace, lambda conn: MedicalDeviceQueries(conn).evidence_bundle(
            namespace, subject, as_of, scopes=who()[1], received_from=received_from, received_to=received_to))

    @mcp.tool()
    def list_medical_device_identity_candidates(namespace: str, state: str | None = None,
                                                subject_key: str | None = None) -> dict:
        """Reviewable device and manufacturer identity candidates (method, evidence, confidence, state) and the
        subjects that stay unmatched."""
        from src.kb.medical_devices_identity import MedicalDeviceIdentity

        def run(conn):
            identity = MedicalDeviceIdentity(conn, initialize=False)
            return {"candidates": identity.candidates(namespace, scopes=who()[1], state=state,
                                                      subject_key=subject_key),
                    "unmatched": identity.unmatched(namespace, scopes=who()[1])}
        return md(namespace, run)

    @mcp.tool()
    def propose_medical_device_identities(namespace: str, ownership_namespace: str | None = None,
                                          products_namespace: str | None = None) -> dict:
        """Propose device matches across FDA, GUDID and EUDAMED by UDI-DI and premarket numbers (product codes as
        low evidence), manufacturers against ownership entities (identifiers first, names as low evidence) and
        GUDID DIs against Products GTINs; nothing is accepted or merged automatically."""
        from src.kb.medical_devices_identity import MedicalDeviceIdentity

        return md(namespace, lambda conn: MedicalDeviceIdentity(conn).propose(
            namespace, principal_id=who()[0], scopes=who()[1], ownership_namespace=ownership_namespace,
            products_namespace=products_namespace), write=True, scope=WRITE)

    @mcp.tool()
    def review_medical_device_identity(namespace: str, candidate_id: str, decision: str, reason: str) -> dict:
        """Accept or reject an identity candidate with a reason (an entity identity decision with reviewer and
        time; records are never merged)."""
        from src.kb.medical_devices_identity import MedicalDeviceIdentity

        return md(namespace, lambda conn: MedicalDeviceIdentity(conn).review(
            namespace, candidate_id, decision, reason, principal_id=who()[0], scopes=who()[1]), write=True,
            scope=REVIEW)

    @mcp.tool()
    def revert_medical_device_identity(namespace: str, candidate_id: str, reason: str) -> dict:
        """Revert a reviewed device or manufacturer identity decision; decisions are kept."""
        from src.kb.medical_devices_identity import MedicalDeviceIdentity

        return md(namespace, lambda conn: MedicalDeviceIdentity(conn).revert(
            namespace, candidate_id, reason, principal_id=who()[0], scopes=who()[1]), write=True, scope=REVIEW)

    @mcp.tool()
    def link_medical_device_records(namespace: str, ownership_namespace: str | None = None,
                                    safety_namespace: str = "global") -> dict:
        """Link recalls to Product safety notices by recall number, combination products to Medicines records by
        application number, devices to trial registrations naming their identifiers and manufacturers to ownership
        entities by accepted match; every link names its basis and record revisions; missing packs are reported."""
        from src.kb.medical_devices_links import MedicalDeviceLinks

        return md(namespace, lambda conn: MedicalDeviceLinks(conn).link(
            namespace, scopes=who()[1], ownership_namespace=ownership_namespace, safety_namespace=safety_namespace),
            write=True, scope=WRITE)

    @mcp.tool()
    def list_medical_device_links(namespace: str, record_key: str | None = None, status: str | None = None) -> dict:
        """Cross-pack links with basis, record revisions and status (linked, target-missing, provider-missing)."""
        from src.kb.medical_devices_links import MedicalDeviceLinks

        return md(namespace, lambda conn: {"links": MedicalDeviceLinks(conn, initialize=False).links(
            namespace, scopes=who()[1], record_key=record_key, status=status)})

    @mcp.tool()
    def create_medical_device_monitor(namespace: str, request_key: str, watch: str, key: str,
                                      delivery: dict | None = None) -> dict:
        """Subscribe to a device, a manufacturer or a product code for new clearances, approvals, supplements,
        recalls, recall status changes and certificate status changes (platform subscriptions; no new scheduler)."""
        from src.kb.medical_devices_monitoring import MedicalDeviceMonitor

        return md(namespace, lambda conn: MedicalDeviceMonitor(conn).create(
            namespace, request_key, watch=watch, key=key, principal_id=who()[0], scopes=who()[1], delivery=delivery),
            write=True, scope=READ)

    @mcp.tool()
    def run_medical_device_monitor(namespace: str, subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate a medical-device monitor at a committed watermark; each notice cites the new and previous record
        revision and states what changed; unchanged records deliver nothing."""
        from src.kb.medical_devices_monitoring import MedicalDeviceMonitor

        return md(namespace, lambda conn: MedicalDeviceMonitor(conn).run(
            subscription_id, watermark, principal_id=who()[0], scopes=who()[1]), write=True, scope=READ)

    @mcp.tool()
    def poll_medical_device_monitor(namespace: str, subscription_id: str, cursor: str = "") -> dict:
        """Poll a medical-device monitor's delivered events after a cursor."""
        from src.kb.medical_devices_monitoring import MedicalDeviceMonitor

        return md(namespace, lambda conn: MedicalDeviceMonitor(conn, initialize=False).poll(
            subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor))
