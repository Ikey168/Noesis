"""Offline harness for the Clinical Evidence ``clinical.devices`` provider (#2654): authored responses replayed
through the real adapter and projector.

Every file under ``tests/fixtures/medical_devices`` is written by
:mod:`tests.unit.medical_devices_fixture_builder` in the documented openFDA
device and AccessGUDID shapes and an authored mapping of the EUDAMED public
fields; every company, device and identifier is fictional. Nothing here is live
coverage. Responses go through :class:`MedicalDevicesAdapter` (the connector the
runtime compiles) and :class:`MedicalDevicesProjector`.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from urllib.parse import urlencode

import duckdb

from src.ingestion.medical_devices_sources import (
    FIXTURE_SECRET,
    MedicalDevicesAdapter,
    fixture_transport,
    requests_for,
)
from src.ingestion.source_packs import validate_source_pack
from src.kb.medical_devices_records import MedicalDevicesProjector
from tests.unit import medical_devices_fixture_builder as fb

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/medical_devices"
PACK = ROOT / "config/source_packs/clinical-evidence.json"
PACK_ID = "clinical-evidence"
NS = "clinical"
OWN_NS = "ownership"
PRODUCTS_NS = "products"
READ = "knowledge:clinical:read"
WRITE = "knowledge:clinical:write"
REVIEW = "knowledge:clinical:review"
NARRATIVES = "knowledge:clinical:devices:narratives:read"
SCOPES = {
    READ, WRITE, f"namespace:{NS}:read", f"namespace:{NS}:write", f"namespace:{OWN_NS}:read",
    f"namespace:{OWN_NS}:write", f"namespace:{PRODUCTS_NS}:read", "knowledge:ownership:read",
    "knowledge:ownership:write", "knowledge:subscriptions:read", "knowledge:subscriptions:write",
    "knowledge:products:read", "knowledge:ingestion:execute",
}
REVIEW_SCOPES = SCOPES | {REVIEW, "knowledge:ownership:review"}
READ_ONLY = {READ, f"namespace:{NS}:read"}
EXAMPLE_DI, FIXTURE_DI = fb.GUDID_A, fb.GUDID_B
# source id -> ordered (unit, [fixture file per request]) pairs, as declared in config/source_packs/clinical-evidence.json
UNITS: dict[str, list[tuple[dict, list[str]]]] = {
    "devices-fda-510k": [({"product_code": c}, [f"fda_510k_{c}.json"]) for c in ("ZZA", "ZZC")],
    "devices-fda-pma": [({"product_code": "ZZB"}, ["fda_pma_ZZB.json"])],
    "devices-fda-classification": [({"product_code": c}, [f"fda_classification_{c}.json"]) for c in ("ZZA", "ZZB")],
    "devices-fda-recalls": [({"product_code": "ZZA"}, ["fda_recall_ZZA.json"])],
    "devices-fda-enforcement": [({"recall_number": fb.RECALL}, [f"fda_enforcement_{fb.RECALL}.json"])],
    "devices-fda-maude-reports": [({"product_code": "ZZA", "from": "2099-01-01", "to": "2099-06-30"},
                                   ["fda_event_ZZA_2099H1.json"])],
    "devices-fda-maude-counts": [({"product_code": "ZZA", "from": "2099-01-01", "to": "2099-03-31"},
                                  ["fda_event_count_ZZA_2099Q1.json"]),
                                 ({"product_code": "ZZA", "from": "2099-04-01", "to": "2099-06-30"},
                                  ["fda_event_count_ZZA_2099Q2.json"])],
    "devices-gudid-identifiers": [({"di": di}, [f"gudid_lookup_{di}.json", f"gudid_history_{di}.json"])
                                  for di in (EXAMPLE_DI, FIXTURE_DI)],
    "devices-eudamed-actors": [({"key": fb.SRN, "path": f"/tools/eudamed/placeholder/actors/{fb.SRN}.json"},
                                [f"eudamed_actor_{fb.SRN}.json"])],
    "devices-eudamed-devices": [({"key": fb.BASIC_UDI_DI,
                                  "path": f"/tools/eudamed/placeholder/devices/{fb.BASIC_UDI_DI}.json"},
                                 [f"eudamed_device_{fb.BASIC_UDI_DI}.json"])],
    "devices-eudamed-certificates": [({"key": fb.CERTIFICATE,
                                       "path": f"/tools/eudamed/placeholder/certificates/{fb.CERTIFICATE}.json"},
                                      [f"eudamed_certificate_{fb.CERTIFICATE}.json"])],
}
SOURCES = list(UNITS)
FDA_SOURCES = [s for s in SOURCES if s.startswith("devices-fda-")]
FORMATS = {
    "devices-fda-510k": "openfda-device-510k-json", "devices-fda-pma": "openfda-device-pma-json",
    "devices-fda-classification": "openfda-device-classification-json",
    "devices-fda-recalls": "openfda-device-recall-json", "devices-fda-enforcement": "openfda-device-enforcement-json",
    "devices-fda-maude-reports": "openfda-device-event-json",
    "devices-fda-maude-counts": "openfda-device-event-count-json",
    "devices-gudid-identifiers": "accessgudid-device-json", "devices-eudamed-actors": "eudamed-actor-json",
    "devices-eudamed-devices": "eudamed-device-json", "devices-eudamed-certificates": "eudamed-certificate-json",
}
SELECTIONS = {
    "devices-fda-510k": {"product_codes": ["ZZA", "ZZC"]},
    "devices-fda-pma": {"product_codes": ["ZZB"]},
    "devices-fda-classification": {"product_codes": ["ZZA", "ZZB"]},
    "devices-fda-recalls": {"product_codes": ["ZZA"]},
    "devices-fda-enforcement": {"recall_numbers": [fb.RECALL]},
    "devices-fda-maude-reports": {"event_windows": [u for u, _ in UNITS["devices-fda-maude-reports"]]},
    "devices-fda-maude-counts": {"event_windows": [u for u, _ in UNITS["devices-fda-maude-counts"]]},
    "devices-gudid-identifiers": {"device_identifiers": [EXAMPLE_DI, FIXTURE_DI]},
    "devices-eudamed-actors": {"documents": [u for u, _ in UNITS["devices-eudamed-actors"]]},
    "devices-eudamed-devices": {"documents": [u for u, _ in UNITS["devices-eudamed-devices"]]},
    "devices-eudamed-certificates": {"documents": [u for u, _ in UNITS["devices-eudamed-certificates"]]},
}


def connection():
    return duckdb.connect(":memory:")


def manifest() -> dict:
    return validate_source_pack(json.loads(PACK.read_text()))


def source(source_id: str) -> dict:
    return copy.deepcopy(next(s for s in manifest()["sources"] if s["source_id"] == source_id))


def fixture_body(filename: str, version: str = "v1") -> str:
    path = FIXTURES / "v2" / filename if version == "v2" and (FIXTURES / "v2" / filename).exists() \
        else FIXTURES / filename
    return path.read_text()


def native_pages(source_id: str, version: str = "v1") -> list[dict]:
    """The native responses for every declared unit, keyed by the request the adapter makes."""
    fmt = FORMATS[source_id]
    pages = []
    for unit, filenames in UNITS[source_id]:
        for (path, params, _paged), filename in zip(requests_for(fmt, unit), filenames):
            query = urlencode(sorted(params.items()))
            body = fixture_body(filename, version)
            status = 404 if '"NOT_FOUND"' in body else 200
            pages.append({"request": path + ("?" + query if query else ""), "status": status,
                          "headers": {"Content-Type": "application/json"}, "body": body})
    return pages


def source_pack_fixture(source_id: str) -> dict:
    return {
        "captured": None,
        "native_pages": native_pages(source_id),
        "note": "Authored responses in the documented openFDA device and AccessGUDID shapes and an authored mapping "
                "of the EUDAMED public fields (tests/unit/medical_devices_fixture_builder.py); every company, device "
                "and identifier is fictional and personal placeholders are dropped by the parser (MD01 "
                "minimisation).",
        "provider": "authored",
        "scenarios": ["authored-fixture", "fictional-devices", "minimised-personal-fields"],
    }


def adapter(source_id: str, version: str = "v1", item: dict | None = None) -> MedicalDevicesAdapter:
    return MedicalDevicesAdapter(item or source(source_id),
                                 transport=fixture_transport(native_pages(source_id, version)),
                                 secret=FIXTURE_SECRET)


def fetch_all(source_id: str, version: str = "v1") -> tuple[list[dict], list[dict]]:
    fetcher = adapter(source_id, version)
    records, receipts, cursor = [], [], None
    for _ in fetcher.units:
        page = fetcher.fetch_page({"operation": "records", "parameters": {}, "limit": 500}, cursor=cursor)
        records += [r["medical_device_record"] for r in page.records]
        receipts.append(page.receipt)
        cursor = page.next_cursor
    return records, receipts


def apply(conn, source_id: str, *, version: str = "v1", run_id: str | None = None,
          observed_at_ms: int | None = None) -> list[dict]:
    """Every unit of one source through the real adapter and projector; returns the per-page outcomes."""
    item = source(source_id)
    fetcher = adapter(source_id, version, item)
    projector = MedicalDevicesProjector(conn)
    if observed_at_ms is not None:
        projector.store.now = lambda: observed_at_ms
    outcomes, cursor = [], None
    for _ in fetcher.units:
        page = fetcher.fetch_page({"operation": "records", "parameters": {}, "limit": 500}, cursor=cursor)
        outcomes += projector.project_page(run_id=run_id or f"run:{source_id}:{version}", manifest=None, source=item,
                                           records=page.records, documents=[], page_receipt=page.receipt,
                                           principal_id="operator")
        cursor = page.next_cursor
        if cursor is None:
            break
    return outcomes


def load_all(conn, *, version: str = "v1", run_id: str | None = None, observed_at_ms: int | None = None) -> None:
    for source_id in SOURCES:
        apply(conn, source_id, version=version, run_id=run_id, observed_at_ms=observed_at_ms)


def load_ownership(conn, namespace: str = OWN_NS):
    """Synthetic Corporate Ownership legal entities for the two fictional manufacturers."""
    from src.kb.ownership_records import record
    from src.kb.ownership_store import OwnershipStore

    def entity(key, name, jurisdiction, identifiers):
        return record("legal_entity", key, {"provider": "gleif", "provider_record_id": key.split(":", 1)[1]},
                      name=name, jurisdiction=jurisdiction, identifiers=identifiers)

    records = [
        entity("lei:5299EXAMPLEMEDDEV001", "Example Medical Devices Inc", "US",
               [{"scheme": "lei", "value": "5299EXAMPLEMEDDEV001"}]),
        entity("lei:5299FIXTURETECHGMB01", "Fixturetech GmbH", "DE",
               [{"scheme": "lei", "value": "5299FIXTURETECHGMB01"}]),
    ]
    return OwnershipStore(conn).apply(namespace, records, run_id="ownership-fixture", observed_at_ms=0,
                                      principal_id="ownership-loader")


def load_product_safety(conn, namespace: str = PRODUCTS_NS) -> str:
    """A fictional CPSC-shaped notice quoting the FDA recall number, stored by the Products pack's own store."""
    from src.ingestion.product_sources import parse_cpsc_recall
    from src.kb.product_safety import ProductSafetyStore

    notice = {"RecallID": 99901, "RecallNumber": "99901", "RecallDate": "2099-03-05T00:00:00",
              "LastPublishDate": "2099-03-05T00:00:00",
              "Title": f"Example Medical Devices recalls EXAMPLEPUMP chargers (see FDA recall {fb.RECALL})",
              "URL": "https://www.cpsc.gov/Recalls/2099/example-fixture",
              "Products": [{"Name": "EXAMPLEPUMP charger", "Model": "EP-100-CH",
                            "Description": f"Charger for the EXAMPLEPUMP infusion pump recalled under FDA recall "
                                           f"{fb.RECALL}"}],
              "Hazards": [{"Name": "The charger can overheat (fictional)."}],
              "Remedies": [{"Name": "Stop using the charger and contact the firm (fictional)."}],
              "Manufacturers": [{"Name": "Example Medical Devices Inc"}]}
    store = ProductSafetyStore(conn)
    return store.apply(namespace, parse_cpsc_recall(notice), run_id="product-safety-fixture")["notice_id"]


def load_clinical(conn, namespace: str = NS) -> dict:
    """A Drugs@FDA medicinal product (the drug component cited by PMA P999901/S002) and a registered trial naming
    the EXAMPLEPUMP by its 510(k) number, in the clinical record store."""
    from src.kb.clinical_medicines import record as medicine
    from src.kb.clinical_records import ClinicalRecordStore
    from src.kb.clinical_records import record as clinical

    product = medicine("medicinal-product", provider="openfda", native_id="NDA099001", jurisdiction="US",
                       authority="FDA", source_url="https://api.fda.gov/drug/drugsfda.json?search="
                       "application_number:NDA099001",
                       native_version={"version": None, "date": "2098-01-01", "basis": "observation"},
                       disclaimer={"text": fb.DISCLAIMER}, name="EXAMPLODRUG (fictional)", active_substances=[],
                       brand_names=[])
    trial = clinical("registered-trial", registry="ctgov", identifier="NCT09999901",
                     title="EXAMPLEPUMP home infusion study (fictional)",
                     source_url="https://clinicaltrials.gov/study/NCT09999901",
                     native_version={"version": "1", "date": "2099-02-01", "basis": "registry-history"},
                     status={"normalized": "recruiting"},
                     interventions=[{"type": "DEVICE", "name": "EXAMPLEPUMP infusion pump (510(k) K999901)",
                                     "other_names": [], "locator": {"json_pointer": "/protocolSection/"
                                                                                    "armsInterventionsModule/"
                                                                                    "interventions/0"}}])
    store = ClinicalRecordStore(conn)
    scopes = {"operator"}  # a fixture loader, not a principal of the feature
    store.ingest(namespace, "openfda", [product], observation_id="devices-medicine", observed_at_ms=1,
                 scopes=scopes)
    store.ingest(namespace, "ctgov", [trial], observation_id="devices-trial", observed_at_ms=1, scopes=scopes)
    return {"medicine": "NDA099001", "trial": "NCT09999901"}


def load_product_identity(conn, namespace: str = PRODUCTS_NS) -> str:
    """A fictional Products identity carrying the EXAMPLEPUMP GTIN (the GUDID primary DI)."""
    from src.kb.products import ProductStore

    ProductStore(conn)
    identity_id = "product-identity:fixture-examplepump"
    conn.execute("INSERT INTO product_identities VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 [identity_id, namespace, "variant", None, "fixture", "EXAMPLE MEDICAL", "EXAMPLEPUMP EP-100", None,
                  "fixture-1", "{}", json.dumps({"gtin": [{"value": EXAMPLE_DI, "state": "valid"}]}), None,
                  "product-fixture", 0])
    return identity_id
