"""Offline harness for the Clinical Evidence medical devices provider (#2654): authored responses, real adapter.

Every file under ``tests/fixtures/medical_devices`` is authored in the publisher's documented or observed shape for
fictional devices, organisations and reports (the Exampla Medical and Northwind Medtech devices); nothing here is
live coverage. Responses go through :class:`MedicalDevicesAdapter` (the connector the runtime compiles) and
:class:`MedicalDeviceProjector`. :func:`build_pack_fixture` composes the pinned source-pack fixtures
(``tests/fixtures/source_packs/clinical-devices-*.json``) from the same files, so they can be rebuilt and checked.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import duckdb

from src.ingestion.medical_devices_sources import (
    MedicalDevicesAdapter,
    fixture_request_key,
    fixture_transport,
    requests_for,
)
from src.ingestion.source_packs import validate_source_pack
from src.kb.medical_devices_records import NARRATIVE_SCOPE, MedicalDeviceProjector

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/medical_devices"
PACK = ROOT / "config/source_packs/clinical-evidence.json"
NS = "clinical"
OWN_NS = "ownership"
PRODUCTS_NS = "global"
SCOPES = {"knowledge:clinical:read", "knowledge:clinical:write", f"namespace:{NS}:read", f"namespace:{NS}:write",
          "knowledge:subscriptions:read", "knowledge:subscriptions:write", "knowledge:ownership:read",
          f"namespace:{OWN_NS}:read", "knowledge:products:read", f"namespace:{PRODUCTS_NS}:read"}
REVIEW_SCOPES = SCOPES | {"knowledge:clinical:review"}
NARRATIVE_SCOPES = SCOPES | {NARRATIVE_SCOPE}
READ_ONLY = {"knowledge:clinical:read", f"namespace:{NS}:read"}

# source id -> (provider, format, selection, {unit index: {request name: fixture file}}, license, scope text)
SOURCES: dict[str, dict] = {
    "clinical-devices-openfda-510k": {
        "provider": "openfda-device", "format": "openfda-510k-json",
        "selection": {"k_numbers": [{"k_number": "K999001"}, {"k_number": "K999002"}, {"k_number": "K999404"}]},
        "files": [{"clearance": "openfda_510k_K999001.json"}, {"clearance": "openfda_510k_K999002.json"},
                  {"clearance": "openfda_510k_K999404.json"}],
        "scope": "Declared 510(k) numbers: clearance, decision code and date, product code as published"},
    "clinical-devices-openfda-pma": {
        "provider": "openfda-device", "format": "openfda-pma-json",
        "selection": {"pma_numbers": [{"pma_number": "P999001"}]},
        "files": [{"approvals": "openfda_pma_P999001.json"}],
        "scope": "Declared PMA numbers: the original approval and every published supplement"},
    "clinical-devices-openfda-classification": {
        "provider": "openfda-device", "format": "openfda-classification-json",
        "selection": {"product_codes": [{"product_code": "ZXA"}, {"product_code": "ZXB"}]},
        "files": [{"classification": "openfda_classification_ZXA.json"},
                  {"classification": "openfda_classification_ZXB.json"}],
        "scope": "Declared FDA product codes: device class, regulation number and review panel"},
    "clinical-devices-openfda-recalls": {
        "provider": "openfda-device", "format": "openfda-recall-json",
        "selection": {"recall_numbers": [{"recall_number": "Z-9901-2025"}]},
        "files": [{"recall": "openfda_recall_Z-9901-2025.json", "enforcement": "openfda_enforcement_Z-9901-2025.json"}],
        "scope": "Declared device recall numbers: recall class, status and reason as published"},
    "clinical-devices-openfda-maude": {
        "provider": "openfda-device", "format": "openfda-event-json",
        "selection": {"event_windows": [
            {"product_code": "ZXA", "received_from": "20250101", "received_to": "20250630"},
            {"product_code": "ZXB", "received_from": "20250101", "received_to": "20250630"}]},
        "files": [{"reports": "openfda_event_ZXA_2025H1.json", "counts": "openfda_event_count_ZXA_2025H1.json"},
                  {"reports": "openfda_event_ZXB_2025H1.json", "counts": "openfda_event_count_ZXB_2025H1.json"}],
        "scope": "MAUDE reports and published event-type counts per declared product code and received-date "
                 "window (at most one year); reports, never incidence"},
    "clinical-devices-accessgudid": {
        "provider": "accessgudid", "format": "gudid-device-json",
        "selection": {"device_identifiers": [{"di": "00899999000011"}, {"di": "00899999000028"}]},
        "files": [{"device": "gudid_00899999000011.json"}, {"device": "gudid_00899999000028.json"}],
        "scope": "Declared primary DIs: device identifier records with package DIs, product codes and premarket "
                 "submission numbers"},
    "clinical-devices-eudamed-actors": {
        "provider": "eudamed", "format": "eudamed-actor-json",
        "selection": {"actors": [{"srn": "US-MF-000099902"}, {"srn": "DE-MF-000099901"}]},
        "files": [{"actor": "eudamed_actor_US-MF-000099902.json"}, {"actor": "eudamed_actor_DE-MF-000099901.json"}],
        "scope": "Declared EUDAMED actor SRNs: organisation, role, country and status (no contact persons)"},
    "clinical-devices-eudamed-devices": {
        "provider": "eudamed", "format": "eudamed-device-json",
        "selection": {"basic_udi_dis": [{"basic_udi_di": "0899999EXFLOWSENSEZ7"}]},
        "files": [{"device": "eudamed_device_0899999EXFLOWSENSEZ7.json"}],
        "scope": "Declared Basic UDI-DIs: device, risk class, UDI-DIs and certificates as published"},
    "clinical-devices-eudamed-certificates": {
        "provider": "eudamed", "format": "eudamed-certificate-json",
        "selection": {"certificates": [{"certificate_number": "EX-9999-0001", "notified_body": "9999"}]},
        "files": [{"certificate": "eudamed_certificate_9999_EX-9999-0001.json"}],
        "scope": "Declared notified-body certificates: type, status, validity and covered Basic UDI-DIs"},
}
ENDPOINTS = {"openfda-device": "https://api.fda.gov", "accessgudid": "https://accessgudid.nlm.nih.gov",
             "eudamed": "https://ec.europa.eu/tools/eudamed"}
PUBLISHER = {"openfda-device": "U.S. Food and Drug Administration (openFDA)",
             "accessgudid": "U.S. National Library of Medicine (AccessGUDID)",
             "eudamed": "European Commission (EUDAMED)"}
LICENSE = {
    "openfda-device": {"id": "openfda-terms", "terms_url": "https://open.fda.gov/terms/",
                       "redistribution": "public domain US government data; keep openFDA's disclaimer; no FDA "
                                         "endorsement"},
    "accessgudid": {"id": "nlm-accessgudid-terms", "terms_url": "https://accessgudid.nlm.nih.gov/about-gudid",
                    "redistribution": "public US government data; cite AccessGUDID; no NLM or FDA endorsement"},
    "eudamed": {"id": "ec-reuse-2011-833", "terms_url": "https://ec.europa.eu/tools/eudamed/#/screen/legal-notice",
                "redistribution": "reuse with acknowledgement (Commission Decision 2011/833/EU); personal data of "
                                  "contact persons excluded"},
}
# Later publisher revisions: supplement S003, the recall terminated, a new GUDID version with a package DI, the
# certificate suspended and K999002 no longer answered (a removal is a revision).
V2 = {
    "clinical-devices-openfda-pma": {0: {"approvals": "v2/openfda_pma_P999001.json"}},
    "clinical-devices-openfda-recalls": {0: {"recall": "v2/openfda_recall_Z-9901-2025.json",
                                             "enforcement": "v2/openfda_enforcement_Z-9901-2025.json"}},
    "clinical-devices-accessgudid": {0: {"device": "v2/gudid_00899999000011.json"}},
    "clinical-devices-eudamed-certificates": {0: {"certificate": "v2/eudamed_certificate_9999_EX-9999-0001.json"}},
    "clinical-devices-openfda-510k": {1: {"clearance": "openfda_510k_K999404.json"}},
}
PUMP_DI = "00899999000011"
LEAD_DI = "00899999000028"
PUMP_CLEARANCE = "medical-devices:fda:510k:K999001"
RECALL = "medical-devices:fda:recall:Z-9901-2025"
PMA = "medical-devices:fda:pma:P999001"
CERTIFICATE = "medical-devices:eudamed:certificate:9999:EX-9999-0001"
EU_DEVICE = "medical-devices:eudamed:basic-udi-di:0899999EXFLOWSENSEZ7"
GUDID_PUMP = f"medical-devices:gudid:di:{PUMP_DI}"


def pack_source(source_id: str) -> dict:
    """The clinical-evidence source entry (without its fixture pin) for one medical-devices source."""
    spec = SOURCES[source_id]
    return {
        "mapping": {"target_schema": "noesis-medical-device-record-v2", "version": "2.0.0"},
        "extractor_versions": ["medical-devices-sources:1.0.0"],
        "health": {"required": False, "max_staleness_s": 2592000},
        "source_id": source_id, "connector": "medical-devices", "publisher": PUBLISHER[spec["provider"]],
        "endpoint": ENDPOINTS[spec["provider"]], "scope": spec["scope"],
        "update_cadence": "weekly at most; each publisher revision is a new record revision",
        "temporal_semantics": "the publisher's revision date (openFDA meta.last_updated, GUDID public version date, "
                              "EUDAMED last update) is the as-of date; event dates as published; observation time "
                              "recorded separately",
        "operations": ["records"], "schedule": {"kind": "interval", "interval_s": 604800},
        "budgets": {"timeout_ms": 30000, "max_results": 100, "max_bytes": 5000000, "max_pages": 25},
        "medical_devices": {"namespace": NS, "provider": spec["provider"], "format": spec["format"],
                            "live_verification": "unverified-live", "minimisation": "medical-devices-minimisation-v1",
                            "selection": spec["selection"]},
        "license": LICENSE[spec["provider"]], "auth": {"kind": "none"},
    }


def _pages(source_id: str, files_per_unit: list[dict[str, str]]) -> list[dict]:
    spec = SOURCES[source_id]
    item = {**pack_source(source_id), "source_hash": "x"}
    unit_key = next(iter(spec["selection"]))
    pages = []
    for unit, files in zip(spec["selection"][unit_key], files_per_unit):
        for name, (path, params) in requests_for(spec["format"], unit).items():
            body = (FIXTURES / files[name]).read_text()
            status = 404 if '"NOT_FOUND"' in body else 200
            pages.append({"request": fixture_request_key(item, path, params), "status": status,
                          "headers": {"Content-Type": "application/json"}, "body": body})
    return pages


def build_pack_fixture(source_id: str) -> dict:
    return {"captured": None, "native_pages": _pages(source_id, SOURCES[source_id]["files"]),
            "scenarios": ["authored fictional responses (tests/fixtures/medical_devices)",
                          "personal fields present in the responses and dropped by the parser (MD01)"]}


def manifest() -> dict:
    return validate_source_pack(json.loads(PACK.read_text()))


def source(source_id: str) -> dict:
    return copy.deepcopy(next(s for s in manifest()["sources"] if s["source_id"] == source_id))


def native_pages(source_id: str, *, v2: bool = False) -> list[dict]:
    files = [dict(f) for f in SOURCES[source_id]["files"]]
    if v2:
        for index, override in V2.get(source_id, {}).items():
            files[index].update(override)
    return _pages(source_id, files)


def fetch(source_id: str, *, v2: bool = False, pages: list[dict] | None = None, item: dict | None = None) -> list:
    item = item or source(source_id)
    adapter = MedicalDevicesAdapter(item, transport=fixture_transport(pages or native_pages(source_id, v2=v2)))
    out, cursor = [], None
    while True:
        page = adapter.fetch_page({"operation": "records", "parameters": {}, "limit": 100}, cursor=cursor)
        out.append(page)
        cursor = page.next_cursor
        if cursor is None:
            return out


def apply(conn, source_id: str, *, run_id: str | None = None, v2: bool = False, now=None) -> list[dict]:
    item = source(source_id)
    projector = MedicalDeviceProjector(conn)
    if now is not None:
        projector.store.now = now
    results = []
    for page in fetch(source_id, v2=v2, item=item):
        results += projector.project_page(run_id=run_id or f"run:{source_id}:{'v2' if v2 else 'v1'}", manifest=None,
                                          source=item, records=page.records, documents=[],
                                          page_receipt=dict(page.receipt), principal_id="operator")
    return results


def load_all(conn, *, v2: bool = False, now=None) -> None:
    for source_id in SOURCES:
        apply(conn, source_id, v2=v2, now=now)


def connection():
    return duckdb.connect(":memory:")


def seed_ownership(conn) -> dict[str, str]:
    """Two legal entities in the ownership namespace, as a register would project them (test data only).

    Exampla Medical Devices Inc. carries the DUNS number GUDID publishes for the labeler; a same-name decoy in
    another country carries none.
    """
    from src.kb.ownership_records import record
    from src.kb.ownership_store import OwnershipStore

    def entity(key, name, jurisdiction, identifiers):
        return record("legal_entity", key, {"provider": "gleif", "provider_record_id": key.rsplit(":", 1)[-1],
                                             "url": "https://example.invalid/" + key.rsplit(":", 1)[-1],
                                             "publisher": "fixture", "license": "fixture"},
                      name=name, jurisdiction=jurisdiction, identifiers=identifiers)

    OwnershipStore(conn).apply(OWN_NS, [
        entity("gleif:lei:5493EXAMPLAMEDDEV001", "Exampla Medical Devices Inc.", "US-MA",
               [{"scheme": "lei", "value": "5493EXAMPLAMEDDEV001"}, {"scheme": "duns", "value": "999000111"}]),
        entity("gleif:lei:5493EXAMPLAMEDDECOY02", "Exampla Medical Devices Inc.", "CA",
               [{"scheme": "lei", "value": "5493EXAMPLAMEDDECOY02"}]),
    ], run_id="run:ownership-fixture", observed_at_ms=1, principal_id="operator")
    return {"exampla": "gleif:lei:5493EXAMPLAMEDDEV001", "decoy": "gleif:lei:5493EXAMPLAMEDDECOY02"}


def seed_clinical(conn) -> dict[str, str]:
    """A Drugs@FDA medicinal product for the lead's drug component and a trial registration naming K999001.

    Test data only: the rows stand in for records the Medicines and trial providers would acquire, so identifier
    links can be exercised offline.
    """
    from src.kb.clinical_medicines import record as medicines_record
    from src.kb.clinical_records import ClinicalRecordStore
    from src.kb.clinical_records import record as clinical_record

    store = ClinicalRecordStore(conn)
    scopes = {"operator"}
    product = medicines_record(
        "medicinal-product", provider="openfda", native_id="NDA999001", jurisdiction="US", authority="FDA",
        source_url="https://api.fda.gov/drug/drugsfda.json?search=application_number:NDA999001",
        native_version={"version": None, "date": "2025-06-30", "basis": "observation"},
        disclaimer={"text": "Do not rely on openFDA to make decisions regarding medical care (fixture)."},
        name="Exampla eluting steroid (fixture)")
    store.ingest(NS, "openfda", [product], observation_id="obs:medicines-fixture", observed_at_ms=1, scopes=scopes)
    trial = clinical_record(
        "registered-trial", registry="ctgov", identifier="NCT09999001",
        title="Occlusion alarm performance of the Exampla FlowSense pump (510(k) K999001) - fixture",
        source_url="https://clinicaltrials.gov/study/NCT09999001",
        native_version={"version": "1", "date": "2024-01-01", "basis": "registry-history"},
        status={"normalized": "completed"}, design={"allocation": "not-applicable"})
    store.ingest(NS, "ctgov", [trial], observation_id="obs:trial-fixture", observed_at_ms=1, scopes=scopes)
    rows = dict(conn.execute("SELECT native_id, record_id FROM clinical_records WHERE namespace=?", [NS]).fetchall())
    return {"product": rows["NDA999001"], "trial": rows["NCT09999001"]}


def seed_safety(conn, namespace: str = PRODUCTS_NS) -> str:
    """A Product safety notice carrying the recall number (test data only; stands in for an acquired notice)."""
    from src.kb.product_safety import ProductSafetyStore

    ProductSafetyStore(conn)
    notice_id = "product-safety-notice:fixture-z99012025"
    conn.execute("INSERT INTO product_safety_notices VALUES (?,?,?,?,?,?)",
                 [namespace, notice_id, "cpsc", "Z-9901-2025", "US", 1])
    conn.execute("INSERT INTO product_safety_current VALUES (?,?,?,?)",
                 [namespace, notice_id, "product-safety-revision:fixture-1", 1])
    return notice_id


class Clock:
    def __init__(self, start: int = 1_760_000_000_000) -> None:
        self.value = start

    def __call__(self) -> int:
        self.value += 1000
        return self.value
