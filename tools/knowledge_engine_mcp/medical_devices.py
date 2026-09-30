"""Clinical Evidence medical-devices entry points (provider ``clinical.devices``, #2654): regulatory history as of a
date, adverse-event report counts with caveats, record revisions, reviewable identity, links and monitors.

Acquisition runs through the shared source-pack tools (pack ``clinical-evidence``
0.1.4: ``devices-fda-510k``, ``devices-fda-pma``, ``devices-fda-classification``,
``devices-fda-recalls``, ``devices-fda-enforcement``, ``devices-fda-maude-reports``,
``devices-fda-maude-counts``, ``devices-gudid-identifiers``,
``devices-eudamed-actors``, ``devices-eudamed-devices``,
``devices-eudamed-certificates``). openFDA, GUDID and EUDAMED coverage are the
separate optional features ``medical-devices-fda``, ``medical-devices-gudid`` and
``medical-devices-eudamed``; Product safety, Medicines, trial, Products and
ownership links degrade to a ``provider_unavailable`` / ``missing_targets``
report when those providers are absent. Every answer cites each item with its
source, record revision and as-of time.

Exclusions: no safety-signal detection, no causality from adverse-event reports,
no clinical advice and no patient data beyond what regulators publish. Report
counts are counts of reports with MAUDE's caveats, never rates. The MD01
minimisation decision applies to every output: contact persons, addresses and
patient blocks are never stored, and MAUDE narratives are returned only with
``knowledge:clinical:devices:narratives:read``.
"""

from src.kb.clinical_bundle import require_enabled

MEDICAL_DEVICES_WRITES = {
    "propose_medical_device_identity_matches",
    "review_medical_device_identity_match",
    "revert_medical_device_identity_match",
    "link_medical_device_records",
    "create_medical_devices_monitor",
    "run_medical_devices_monitor",
}
MEDICAL_DEVICES_READS = {
    "medical_devices_source_contracts",
    "medical_devices_readiness",
    "medical_device_regulatory_history",
    "medical_device_adverse_event_counts",
    "medical_device_record_history",
    "list_medical_device_identity_candidates",
    "list_medical_devices_unmatched",
    "list_medical_device_links",
    "export_medical_devices_evidence_bundle",
    "poll_medical_devices_monitor",
}
MEDICAL_DEVICES_TOOLS = MEDICAL_DEVICES_WRITES | MEDICAL_DEVICES_READS
READ = "knowledge:clinical:read"
WRITE = "knowledge:clinical:write"
OWNERSHIP_READ = "knowledge:ownership:read"
OWNERSHIP_WRITE = "knowledge:ownership:write"
OWNERSHIP_REVIEW = "knowledge:ownership:review"
SUBSCRIPTIONS_READ = "knowledge:subscriptions:read"
SUBSCRIPTIONS_WRITE = "knowledge:subscriptions:write"
NARRATIVES = "knowledge:clinical:devices:narratives:read"
# Every scope each tool always reads or writes: device records, the ownership identity state machine (answers reach
# records through accepted matches) and subscriptions. Conditional scopes: the narrative scope returns MAUDE text
# (otherwise withheld and counted); knowledge:products:read is needed only when a products namespace is named.
MEDICAL_DEVICES_SCOPES = {
    "medical_devices_source_contracts": [],
    "medical_devices_readiness": [READ],
    "medical_device_regulatory_history": [READ, OWNERSHIP_READ],
    "medical_device_adverse_event_counts": [READ, OWNERSHIP_READ],
    "medical_device_record_history": [READ],
    "list_medical_device_identity_candidates": [READ, OWNERSHIP_READ],
    "list_medical_devices_unmatched": [READ, OWNERSHIP_READ],
    "list_medical_device_links": [READ],
    "export_medical_devices_evidence_bundle": [READ, OWNERSHIP_READ],
    "poll_medical_devices_monitor": [READ, SUBSCRIPTIONS_READ],
    "propose_medical_device_identity_matches": [READ, OWNERSHIP_READ, OWNERSHIP_WRITE],
    "review_medical_device_identity_match": [OWNERSHIP_READ, OWNERSHIP_REVIEW],
    "revert_medical_device_identity_match": [OWNERSHIP_READ, OWNERSHIP_REVIEW],
    "link_medical_device_records": [READ, WRITE, OWNERSHIP_READ],
    "create_medical_devices_monitor": [READ, SUBSCRIPTIONS_WRITE],
    "run_medical_devices_monitor": [READ, OWNERSHIP_READ, SUBSCRIPTIONS_READ, SUBSCRIPTIONS_WRITE],
}


def required_scopes(tool_name, mutability):
    return MEDICAL_DEVICES_SCOPES.get(tool_name, [WRITE if mutability == "write" else READ])


def register(mcp, safe, context):
    """Register the tools; each is gated on the Clinical Evidence bundle and carries the review boundary."""
    from src.ingestion.medical_devices_sources import REVIEW_BOUNDARY

    def who():
        return context()[0], context()[1]

    def run(namespace, operation, *, write=False, scope=READ):
        def guarded(conn):
            require_enabled(conn, namespace)
            result = operation(conn)
            return {**result, "boundary": REVIEW_BOUNDARY} if isinstance(result, dict) else result
        return safe(guarded, write=write, required_scope=scope)

    def queries(conn):
        from src.kb.medical_devices_queries import MedicalDevicesQueries

        return MedicalDevicesQueries(conn)

    @mcp.tool()
    def medical_devices_source_contracts() -> dict:
        """Per-provider endpoints, openFDA key handling, licences and disclaimers, rate limits, revision models,
        EUDAMED module availability (unavailable modules are explicit gaps), bounded coverage, LIVE_VERIFICATION
        status and the MD01 data-minimisation decision for openFDA device endpoints, AccessGUDID and EUDAMED."""
        from src.ingestion.medical_devices_sources import (
            BOUNDED_COVERAGE,
            EUDAMED_MODULES,
            LIVE_VERIFICATION,
            MAUDE_CAVEATS,
            MINIMISATION,
            PROVIDER_CONTRACTS,
        )

        return {"contracts": PROVIDER_CONTRACTS, "live_verification": LIVE_VERIFICATION,
                "bounded_coverage": BOUNDED_COVERAGE, "minimisation": MINIMISATION,
                "eudamed_modules": EUDAMED_MODULES, "maude_caveats": list(MAUDE_CAVEATS),
                "review_boundary": REVIEW_BOUNDARY}

    @mcp.tool()
    def medical_devices_readiness(namespace: str) -> dict:
        """Whether the medical-devices-fda / -gudid / -eudamed features are selected, records and evidence origin
        (fixture or live) per provider, EUDAMED module availability and the minimisation policy."""
        from src.kb.medical_devices_records import authorize, readiness

        def op(conn):
            authorize(namespace, who()[1], READ)
            return readiness(conn)
        return run(namespace, op)

    @mcp.tool()
    def medical_device_regulatory_history(namespace: str, subject: str, as_of: str | None = None) -> dict:
        """A device's regulatory history as of a date: subject = GUDID DI, EUDAMED Basic UDI-DI, K or P number,
        product code, recall number or SRN. Classification, clearances, approvals with supplements, recalls (status
        and class as published) and device identifiers (US) and device registrations, certificates and actors (EU),
        jurisdictions shown separately; each event cites its record revision; later decisions are listed; modules
        not acquired are stated; no record is none_on_record. No safety-signal detection or clinical advice."""
        return run(namespace, lambda conn: queries(conn).regulatory_history(namespace, subject, scopes=who()[1],
                                                                            as_of=as_of))

    @mcp.tool()
    def medical_device_adverse_event_counts(namespace: str, subject: str, window_from: str | None = None,
                                            window_to: str | None = None) -> dict:
        """MAUDE adverse-event report counts per event type and period for a device's product codes, as openFDA
        published them, beside the reports on record counted per month. Counts are reports, never rates, incidence
        or causal events; MAUDE's caveats, the query window and the source revisions come with every answer. No
        safety-signal detection, no causality and no clinical advice."""
        return run(namespace, lambda conn: queries(conn).adverse_event_counts(
            namespace, subject, scopes=who()[1], window_from=window_from, window_to=window_to))

    @mcp.tool()
    def medical_device_record_history(namespace: str, record_key: str, as_of: str | None = None,
                                      basis: str = "published") -> dict:
        """Every revision of one record (recall status changes, supplements listed on an approval, GUDID versions,
        certificate status changes) and, with as_of, the revision in force at that date by the source's date
        (basis=published) or by observation (basis=observed). MAUDE narratives only with the narrative scope."""
        from src.kb.medical_devices_records import MedicalDevicesStore

        def op(conn):
            store = MedicalDevicesStore(conn, initialize=False)
            history = store.history(namespace, record_key, scopes=who()[1])
            answer = {"record_key": record_key, "revisions": history,
                      "status": "answered" if history else "none_on_record"}
            if as_of:
                answer["as_of"] = store.as_of(namespace, record_key, as_of, scopes=who()[1], basis=basis)
            return answer
        return run(namespace, op)

    @mcp.tool()
    def propose_medical_device_identity_matches(namespace: str, ownership_namespace: str | None = None,
                                                products_namespace: str | None = None) -> dict:
        """Propose reviewable matches: devices across GUDID and EUDAMED by UDI-DI, devices and Products identities
        by GTIN, manufacturers through accepted device matches and shared K/P numbers, and manufacturers and
        Corporate Ownership entities by name and country (low confidence). Identifiers before names; devices are
        never matched by name; nothing is merged or accepted automatically."""
        from src.kb.medical_devices_identity import MedicalDevicesIdentity

        return run(namespace, lambda conn: MedicalDevicesIdentity(conn).propose(
            namespace, principal_id=who()[0], scopes=who()[1], ownership_namespace=ownership_namespace,
            products_namespace=products_namespace), write=True, scope=OWNERSHIP_WRITE)

    @mcp.tool()
    def review_medical_device_identity_match(namespace: str, candidate_id: str, decision: str, reason: str) -> dict:
        """Accept or reject a proposed medical-device identity candidate with a reason (an entity identity
        decision; records are never merged)."""
        from src.kb.medical_devices_identity import MedicalDevicesIdentity

        return run(namespace, lambda conn: MedicalDevicesIdentity(conn).review(
            namespace, candidate_id, decision, reason, principal_id=who()[0], scopes=who()[1]), write=True,
            scope=OWNERSHIP_REVIEW)

    @mcp.tool()
    def revert_medical_device_identity_match(namespace: str, candidate_id: str, reason: str) -> dict:
        """Revert an accepted or rejected medical-device identity decision; both records stay intact."""
        from src.kb.medical_devices_identity import MedicalDevicesIdentity

        return run(namespace, lambda conn: MedicalDevicesIdentity(conn).revert(
            namespace, candidate_id, reason, principal_id=who()[0], scopes=who()[1]), write=True,
            scope=OWNERSHIP_REVIEW)

    @mcp.tool()
    def list_medical_device_identity_candidates(namespace: str, record_key: str | None = None) -> dict:
        """Medical-device identity candidates with method, evidence, confidence and review state."""
        from src.kb.medical_devices_identity import MedicalDevicesIdentity

        return run(namespace, lambda conn: {"candidates": MedicalDevicesIdentity(conn, initialize=False).candidates(
            namespace, scopes=who()[1], record_key=record_key)}, scope=OWNERSHIP_READ)

    @mcp.tool()
    def list_medical_devices_unmatched(namespace: str) -> dict:
        """Device and manufacturer subjects without an accepted identity decision (kept visible as unmatched)."""
        from src.kb.medical_devices_identity import MedicalDevicesIdentity

        return run(namespace, lambda conn: MedicalDevicesIdentity(conn, initialize=False).unmatched(
            namespace, scopes=who()[1]), scope=OWNERSHIP_READ)

    @mcp.tool()
    def link_medical_device_records(namespace: str, kinds: list[str] | None = None,
                                    products_namespace: str | None = None, clinical_namespace: str | None = None,
                                    ownership_namespace: str | None = None) -> dict:
        """Link device records to Product safety notices by recall number, to Medicines records by a cited
        application number, to trials that name a K/P number or DI, and to ownership and Products records through
        accepted matches. Each link cites both revisions and its basis; absent providers and missing targets are
        reported. A link is never a safety conclusion."""
        from src.kb.medical_devices_links import MedicalDevicesLinks

        return run(namespace, lambda conn: MedicalDevicesLinks(conn).link(
            namespace, principal_id=who()[0], scopes=who()[1], kinds=kinds, products_namespace=products_namespace,
            clinical_namespace=clinical_namespace, ownership_namespace=ownership_namespace), write=True, scope=WRITE)

    @mcp.tool()
    def list_medical_device_links(namespace: str, kind: str | None = None, record_key: str | None = None) -> dict:
        """Stored links with their basis and the record revisions on both sides."""
        from src.kb.medical_devices_links import MedicalDevicesLinks

        return run(namespace, lambda conn: {"links": MedicalDevicesLinks(conn, initialize=False).links(
            namespace, scopes=who()[1], kind=kind, record_key=record_key)})

    @mcp.tool()
    def export_medical_devices_evidence_bundle(namespace: str, query: str, subject: str, as_of: str | None = None,
                                               window_from: str | None = None, window_to: str | None = None) -> dict:
        """An evidence bundle of a regulatory-history (query=history) or report-count (query=counts) answer: every
        assertion cites its source, record revision and as-of time; report counts keep MAUDE's caveats."""
        def op(conn):
            ask = queries(conn)
            if query == "counts":
                answer = ask.adverse_event_counts(namespace, subject, scopes=who()[1], window_from=window_from,
                                                  window_to=window_to)
            else:
                answer = ask.regulatory_history(namespace, subject, scopes=who()[1], as_of=as_of)
            return {"answer": answer, "evidence_bundle": ask.evidence_bundle(answer)}
        return run(namespace, op)

    @mcp.tool()
    def create_medical_devices_monitor(namespace: str, request_key: str, watch: str, key: str,
                                       delivery: dict | None = None) -> dict:
        """Watch a device (DI, Basic UDI-DI, K/P number), a manufacturer (SRN or subject key) or a product code for
        new clearances, supplements, recalls, recall status and class changes, certificate status changes and new
        GUDID versions. Adverse-event reports are never notified; no new scheduler."""
        from src.kb.medical_devices_monitoring import MedicalDevicesMonitor

        return run(namespace, lambda conn: MedicalDevicesMonitor(conn).create(
            namespace, request_key, watch=watch, key=key, principal_id=who()[0], scopes=who()[1],
            delivery=delivery), write=True, scope=SUBSCRIPTIONS_WRITE)

    @mcp.tool()
    def run_medical_devices_monitor(namespace: str, subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate a medical-devices monitor at a committed watermark; notices cite the new and previous record
        revisions and state what changed. Record changes only, never assessments."""
        from src.kb.medical_devices_monitoring import MedicalDevicesMonitor

        return run(namespace, lambda conn: MedicalDevicesMonitor(conn).run(
            subscription_id, watermark, principal_id=who()[0], scopes=who()[1]), write=True,
            scope=SUBSCRIPTIONS_WRITE)

    @mcp.tool()
    def poll_medical_devices_monitor(namespace: str, subscription_id: str, cursor: str = "") -> dict:
        """Delivered notices of a medical-devices monitor."""
        from src.kb.medical_devices_monitoring import MedicalDevicesMonitor

        return run(namespace, lambda conn: MedicalDevicesMonitor(conn, initialize=False).poll(
            subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor), scope=SUBSCRIPTIONS_READ)
