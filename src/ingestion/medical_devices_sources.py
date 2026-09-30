"""openFDA device, AccessGUDID and EUDAMED acquisition for the Clinical Evidence pack (#2654, MD01, MD03-MD06).

One native connector, ``medical-devices``, reads a bounded, declared selection
from one documented provider per source and emits
``noesis-medical-device-record-v1`` records exactly as the regulator published
them:

* ``openfda-device-510k-json`` - openFDA ``/device/510k.json``: one
  ``clearance`` per K number with its decision code, decision date and product
  code as published;
* ``openfda-device-pma-json`` - openFDA ``/device/pma.json``: one ``approval``
  per P number (the original row) and one ``supplement`` per P number and
  supplement number; the approval lists the supplements published for it, so a
  new supplement is a new revision of the approval record;
* ``openfda-device-classification-json`` - openFDA
  ``/device/classification.json``: one ``classification`` per product code
  (device class, regulation number, panel);
* ``openfda-device-recall-json`` - openFDA ``/device/recall.json``: one
  ``recall`` per recall number (``product_res_number``) with its status and
  dates; a status change is a new revision;
* ``openfda-device-enforcement-json`` - openFDA ``/device/enforcement.json``:
  the enforcement report of a declared recall number, with the recall class
  (``classification``) and status as published;
* ``openfda-device-event-json`` - openFDA ``/device/event.json`` (MAUDE): one
  ``adverse-event-report`` per report number within a declared product code
  and date window; narrative text only as published;
* ``openfda-device-event-count-json`` - the same endpoint with
  ``count=event_type.exact``: an ``adverse-event-count`` of *reports* per event
  type for a product code and window, as openFDA returns it;
* ``accessgudid-device-json`` - AccessGUDID device lookup and device history:
  one ``device-identifier`` per primary DI with package DIs, the published
  version and the premarket submission numbers as published;
* ``eudamed-actor-json`` / ``eudamed-device-json`` /
  ``eudamed-certificate-json`` - EUDAMED public records (actor by SRN, device
  by Basic UDI-DI, certificate by certificate number) from operator-declared
  documents on the public site host; the paths and the record shape are
  unverified until MD14 (#2723).

**Data minimisation (MD01).** Contact persons (the 510(k) ``contact``, the
recall ``additional_info_contact``, MAUDE manufacturer and reporter contact
fields, GUDID customer contacts, EUDAMED contact details and PRRC names),
street addresses and every MAUDE ``patient`` block are dropped *here*, before
any record, document or receipt exists, and listed under
``minimisation.withheld``. MAUDE narrative text is kept only as published and
is returned only to principals holding the narrative scope. The store refuses a
record that still carries a withheld key.

A unit is all-or-nothing: a result set longer than the declared page bound is
``budget_exhausted``, never truncated; a redirect to another host is a
network-policy failure. Receipts name every request path, status and response
digest; the optional openFDA key travels as the ``api_key`` parameter and never
appears in a receipt or a record. Report counts are counts of *reports* with
MAUDE's caveats attached: never incidence, rates or causal events. Nothing here
detects safety signals or gives clinical advice.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime
from typing import Any
from urllib.parse import urlencode, urlsplit

from src.ingestion.source_packs import SourcePackError

ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
RECORD_CONTRACT = "noesis-medical-device-record-v1"
MINIMISATION_POLICY = "medical-devices-minimisation-v1"
CONNECTOR = "medical-devices"
PER_PAGE = 100
MAX_PAGES_PER_UNIT = 5
MAX_UNITS = 50
MAX_WINDOW_DAYS = 366
OPENFDA_ATTRIBUTION = "Source: openFDA device endpoints (U.S. Food and Drug Administration)."
GUDID_ATTRIBUTION = "Source: AccessGUDID (U.S. National Library of Medicine and U.S. Food and Drug Administration)."
EUDAMED_ATTRIBUTION = "Source: EUDAMED public site (European Commission)."
REVIEW_BOUNDARY = ("Records are what the regulators published. Adverse-event report counts are counts of reports with "
                   "the source's caveats, never incidence, rates or causal events. No safety-signal detection, no "
                   "causality from adverse-event reports, no clinical advice and no patient data beyond what "
                   "regulators publish.")
# MAUDE caveats attached to every adverse-event report and count (openFDA device adverse-event overview and FDA MDR
# data-files page, search excerpts read 2026-09-30; verify the exact wording against the live pages in MD14).
MAUDE_CAVEATS = (
    ("MAUDE holds reports submitted by mandatory reporters (manufacturers, importers, device user facilities) and "
    "voluntary reporters (health care professionals, patients, consumers)."),
    ("Reports can be incomplete, inaccurate, untimely, unverified or biased; the submission of a report does not "
    "establish that the device caused or contributed to the reported event."),
    ("Adverse events are under-reported; report counts cannot be used to establish rates of events or to compare "
    "devices."),
    "A report count is a number of reports received in the query window, not a number of patients or events.",
)
# EUDAMED modules: availability as audited (MD01); unavailable modules stay explicit gaps (MD06).
EUDAMED_MODULES: dict[str, dict[str, Any]] = {
    "actor-registration": {"availability": "public (voluntary since December 2020; mandatory from 2026-05-28)",
                           "acquired": True, "record_kind": "actor", "format": "eudamed-actor-json"},
    "udi-device-registration": {"availability": "public (voluntary since October 2021; mandatory from 2026-05-28)",
                                "acquired": True, "record_kind": "eudamed-device", "format": "eudamed-device-json"},
    "notified-bodies-certificates": {"availability": "public (voluntary since October 2021; mandatory from "
                                                     "2026-05-28)",
                                     "acquired": True, "record_kind": "certificate",
                                     "format": "eudamed-certificate-json"},
    "market-surveillance": {"availability": "mandatory for competent authorities from 2026-05-28; public content not "
                                            "verified", "acquired": False, "record_kind": None, "format": None,
                            "gap": "not acquired: no verified public view"},
    "vigilance-post-market-surveillance": {"availability": "not in mandatory use; public content not verified",
                                           "acquired": False, "record_kind": None, "format": None,
                                           "gap": "not acquired: module not publicly available"},
    "clinical-investigations-performance-studies": {"availability": "not in mandatory use; public content not "
                                                                    "verified",
                                                    "acquired": False, "record_kind": None, "format": None,
                                                    "gap": "not acquired: module not publicly available"},
}

# format -> provider, jurisdiction, the selection list it reads and the path it requests
FORMATS: dict[str, dict[str, Any]] = {
    "openfda-device-510k-json": {"provider": "openfda-device", "jurisdiction": "US", "unit": "product_codes",
                                 "path": "/device/510k.json"},
    "openfda-device-pma-json": {"provider": "openfda-device", "jurisdiction": "US", "unit": "product_codes",
                                "path": "/device/pma.json"},
    "openfda-device-classification-json": {"provider": "openfda-device", "jurisdiction": "US",
                                           "unit": "product_codes", "path": "/device/classification.json"},
    "openfda-device-recall-json": {"provider": "openfda-device", "jurisdiction": "US", "unit": "product_codes",
                                   "path": "/device/recall.json"},
    "openfda-device-enforcement-json": {"provider": "openfda-device", "jurisdiction": "US", "unit": "recall_numbers",
                                        "path": "/device/enforcement.json"},
    "openfda-device-event-json": {"provider": "openfda-device", "jurisdiction": "US", "unit": "event_windows",
                                  "path": "/device/event.json"},
    "openfda-device-event-count-json": {"provider": "openfda-device", "jurisdiction": "US",
                                        "unit": "event_windows", "path": "/device/event.json"},
    "accessgudid-device-json": {"provider": "accessgudid", "jurisdiction": "US", "unit": "device_identifiers",
                                "path": "/api/v2/devices/lookup.json"},
    "eudamed-actor-json": {"provider": "eudamed", "jurisdiction": "EU", "unit": "documents", "path": None},
    "eudamed-device-json": {"provider": "eudamed", "jurisdiction": "EU", "unit": "documents", "path": None},
    "eudamed-certificate-json": {"provider": "eudamed", "jurisdiction": "EU", "unit": "documents", "path": None},
}
RECORD_KINDS = ("classification", "clearance", "approval", "supplement", "recall", "adverse-event-report",
                "adverse-event-count", "device-identifier", "actor", "eudamed-device", "certificate")
FEATURE_FOR_PROVIDER = {"openfda-device": "medical-devices-fda", "accessgudid": "medical-devices-gudid",
                        "eudamed": "medical-devices-eudamed"}

# MD01 access decisions. Official pages could not be fetched from this runtime (egress blocked, 2026-09-30); endpoints,
# fields and terms are recorded from search-engine excerpts of the official pages and the providers' documentation as
# known. Every item marked ``verify`` is checked before the dated live run (MD14, #2723).
PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "openfda-device": {
        "publisher": "US Food and Drug Administration (openFDA device endpoints)",
        "documentation": "https://open.fda.gov/apis/device/",
        "endpoints": ["/device/510k.json", "/device/pma.json", "/device/classification.json", "/device/recall.json",
                      "/device/enforcement.json", "/device/event.json"],
        "formats": [f for f, spec in FORMATS.items() if spec["provider"] == "openfda-device"],
        "authentication": "optional openFDA key (optional-secret NOESIS_OPENFDA_API_KEY, the key the Medicines feature "
        "uses) sent as the api_key query parameter; never stored in a record, receipt or durable URL",
        "rate_limits": "240 requests per minute; 1,000 requests per day per IP without a key and 120,000 per day per "
        "key (openFDA authentication page, search excerpt 2026-09-30; verify)",
        "pagination": "limit <= 100 and skip; at most 5 pages per unit; meta.results.total above the bound is "
        "budget_exhausted, never truncated; count queries return terms and counts only",
        "identifiers": ["K number (510(k))", "P number with supplement number (PMA)", "three-letter product code",
                        "recall number (product_res_number) and recall event id", ("MAUDE report_number and "
                        "mdr_report_key")],
        "revisions": "openFDA publishes no per-record revision stamp; a changed row is a new revision of the same "
        "record key (a recall status change, a later supplement listed on an approval); meta.last_updated is kept "
        "in the receipt; rows that disappear from a later response are not deleted",
        "licence": "openFDA terms of service (https://open.fda.gov/terms/); FDA data are US government works; every "
        "response carries meta.disclaimer, which is stored with every record ('Do not rely on openFDA to make "
        "decisions regarding medical care'; results are unvalidated) (verify the licence wording)",
        "attribution": OPENFDA_ATTRIBUTION,
        "caveats": list(MAUDE_CAVEATS),
        "access_decision": "unverified-live",
        "reason": "fixture-verified parsers in the documented openFDA JSON shape; field names marked verify in the "
        "audit (recall classification on the enforcement endpoint, event_date_terminated, mdr_text) must be "
        "confirmed against real responses",
    },
    "accessgudid": {
        "publisher": "US National Library of Medicine, AccessGUDID (FDA Global Unique Device Identification "
        "Database)",
        "documentation": "https://accessgudid.nlm.nih.gov/resources/developers",
        "endpoints": ["/api/v2/devices/lookup.json?di=", "/api/v2/devices/history.json?di="],
        "formats": ["accessgudid-device-json"],
        "authentication": "none",
        "rate_limits": "not stated in the excerpts read (verify); one lookup and one history request per declared DI",
        "pagination": "none (one device record per request)",
        "identifiers": ["primary DI", "package DIs", "public device record key (stable across DI changes)",
                        "premarket submission numbers as published"],
        "revisions": "publicVersionNumber and publicVersionDate per device record version; the device history "
        "endpoint lists earlier versions; a new version is a new revision",
        "licence": "FDA GUDID data published by NLM; public information (verify the AccessGUDID terms of use; "
        "the excerpts read did not state them)",
        "attribution": GUDID_ATTRIBUTION,
        "access_decision": "unverified-live",
        "reason": "fixture-verified parser for the device lookup JSON as documented; the v2/v3 path, the history "
        "response shape and the terms of use must be verified",
    },
    "eudamed": {
        "publisher": "European Commission, EUDAMED public site",
        "documentation": "https://ec.europa.eu/tools/eudamed/",
        "endpoints": ["operator-declared public-site documents on ec.europa.eu (placeholders until MD14)"],
        "formats": ["eudamed-actor-json", "eudamed-device-json", "eudamed-certificate-json"],
        "authentication": "none for the public site; machine-to-machine exchange is for registered actors only and "
        "is not used",
        "rate_limits": "not documented (verify); one request per declared document",
        "pagination": "none (one document per unit)",
        "identifiers": ["SRN (actor)", "Basic UDI-DI and UDI-DI (device)", ("certificate number and notified body "
                        "number (certificate)")],
        "revisions": "version number and last-update date per public record as published; a certificate status "
        "change (issued, suspended, withdrawn, expired) is a new revision",
        "licence": "European Commission legal notice (reuse of Commission documents under Decision 2011/833/EU "
        "unless stated otherwise; verify that it covers EUDAMED public data)",
        "attribution": EUDAMED_ATTRIBUTION,
        "modules": EUDAMED_MODULES,
        "access_decision": "unverified-live",
        "reason": "no documented public API or bulk download could be confirmed; the adapter reads operator-declared "
        "documents on the public host in an authored mapping of the public fields; paths, shape and reuse terms "
        "must be verified before any live run",
    },
    "openfda-device-registrationlisting": {
        "publisher": "US Food and Drug Administration (openFDA /device/registrationlisting.json)",
        "documentation": "https://open.fda.gov/apis/device/registrationlisting/",
        "endpoints": ["/device/registrationlisting.json"],
        "formats": [],
        "authentication": "as openfda-device",
        "rate_limits": "as openfda-device",
        "pagination": "as openfda-device",
        "identifiers": ["FEI number", "registration number", "listing number"],
        "revisions": "none published per record",
        "licence": "as openfda-device",
        "attribution": OPENFDA_ATTRIBUTION,
        "access_decision": "documented-not-acquired",
        "reason": "establishment records name official correspondents and contact persons; MD03 does not need them "
        "and the minimisation decision keeps contact persons out",
    },
    "openfda-device-udi": {
        "publisher": "US Food and Drug Administration (openFDA /device/udi.json)",
        "documentation": "https://open.fda.gov/apis/device/udi/",
        "endpoints": ["/device/udi.json"],
        "formats": [],
        "authentication": "as openfda-device",
        "rate_limits": "as openfda-device",
        "pagination": "as openfda-device",
        "identifiers": ["primary DI"],
        "revisions": "current version only",
        "licence": "as openfda-device",
        "attribution": OPENFDA_ATTRIBUTION,
        "access_decision": "documented-not-acquired",
        "reason": "a copy of GUDID without its version history; AccessGUDID is acquired instead",
    },
}
LIVE_VERIFICATION = {
    provider: {"status": contract["access_decision"], "note": "no dated live run from this runtime; offline "
                                                              "fixtures only (MD14, #2723)"}
    for provider, contract in PROVIDER_CONTRACTS.items()
}
PROVIDER_HOSTS = {"openfda-device": {"api.fda.gov"}, "accessgudid": {"accessgudid.nlm.nih.gov"},
                  "eudamed": {"ec.europa.eu"}}
# Bounded first coverage (MD01): nothing implies complete coverage of a device class, a manufacturer or a market.
BOUNDED_COVERAGE = {
    "fda": "the product codes and recall numbers named in each source's selection (at most 50 units per source, 5 "
    "pages of 100 rows per unit); MAUDE reports and report counts only for declared (product code, window) pairs "
    f"of at most {MAX_WINDOW_DAYS} days",
    "gudid": "the primary DIs named in the selection (at most 50), one lookup and one history request each",
    "eudamed": "the actor SRNs, Basic UDI-DIs and certificate numbers named in the selection (at most 50 each) from "
    "the actor, UDI/device and certificate modules; the market-surveillance, vigilance and clinical-investigation "
    "modules are not acquired and stay explicit gaps",
    "periods": "decision, recall and report dates as published; no back-fill beyond the declared selection",
}
# MD01 data-minimisation decision (docs/development/medical-devices-evidence/source-audit.md).
MINIMISATION: dict[str, Any] = {
    "policy": MINIMISATION_POLICY,
    "stored": [("regulatory identifiers (K and P numbers, supplement numbers, product codes, recall numbers, report "
               "numbers, DIs, SRNs, certificate numbers)"), ("decision, recall, report and certificate dates as "
               "published"), "company names as published and their city, state and country",
               "device brand, model, catalogue number and description as published",
               "MAUDE event type, report source, product problems and device fields as published",
               "MAUDE narrative text as published (returned only with the narrative scope)"],
    "never_stored": [("contact persons (510(k) contact, recall additional_info_contact, MAUDE manufacturer contact "
                     "and reporter fields, GUDID customer contacts, EUDAMED contact details and PRRC names)"),
                     "street addresses, postal codes, telephone numbers and e-mail addresses",
                     "every MAUDE patient block (age, sex, weight, ethnicity, race, patient problems and outcomes)"],
    "narratives": "MAUDE mdr_text is kept verbatim as published and returned only to principals holding "
    "knowledge:clinical:devices:narratives:read; otherwise it is counted and withheld",
    "matching": "companies only; no person is a subject, matched or linked",
    "retention": "retained with the record revision; no personal identifier is stored, so nothing personal "
    "remains to purge; no automatic expiry in the first coverage",
    "query_scope": "knowledge:clinical:read with namespace access for records; the narrative scope for MAUDE text",
}
# Keys never stored on any record (checked recursively on ``fields``).
PERSONAL_KEYS = frozenset({
    "contact", "contacts", "additional_info_contact", "customercontact", "customercontacts", "contactdetails",
    "contact_details", "email", "e_mail", "phone", "telephone", "fax", "prrc", "prrcs", "person",
    "responsible_person", "address_1", "address_2", "street", "street_1", "street_2", "zip_code", "postal_code",
    "postcode", "patient", "patients", "patient_age", "patient_sex", "patient_weight", "patient_ethnicity",
    "patient_race", "reporter_name", "reporter_occupation_code", "initial_report_to_fda",
})
_PERSONAL_PREFIXES = ("patient", "manufacturer_contact", "reporter_", "contact_", "distributor_address",
                      "distributor_zip", "manufacturer_g1_address", "manufacturer_g1_zip", "manufacturer_address",
                      "manufacturer_zip", "manufacturer_postal", "manufacturer_d_address", "manufacturer_d_zip",
                      "manufacturer_d_postal")


class MedicalDevicesFormatError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
                          ).hexdigest()


def _clean(value: Any) -> str | None:
    if isinstance(value, list):
        value = value[0] if value else None
    text = " ".join(str(value if value is not None else "").split())
    return text or None


def _list(value: Any) -> list[str]:
    items = value if isinstance(value, list) else [value] if value not in (None, "") else []
    return sorted({t for t in (_clean(v) for v in items) if t})


def slug(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").casefold()).strip("-") or "none"


def _day(value: Any) -> str | None:
    """An ISO day from ``YYYYMMDD``, ``YYYY-MM-DD`` or ``MM/DD/YYYY``."""
    text = str(value or "").strip()
    match = re.match(r"^(\d{4})-?(\d{2})-?(\d{2})", text)
    if match:
        try:
            return date(int(match[1]), int(match[2]), int(match[3])).isoformat()
        except ValueError:
            return None
    match = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if match:
        try:
            return date(int(match[3]), int(match[1]), int(match[2])).isoformat()
        except ValueError:
            return None
    return None


def _flag(value: Any) -> bool | None:
    text = str(value if value is not None else "").strip().casefold()
    return {"y": True, "yes": True, "true": True, "n": False, "no": False, "false": False}.get(text)


def _order(*parts: Any) -> str:
    out = []
    for part in parts:
        if isinstance(part, int) or (isinstance(part, str) and part.isdigit()):
            out.append(f"{int(part):012d}")
        else:
            out.append(str(part or ""))
    return "|".join(out)


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MedicalDevicesFormatError("schema_drift", "response is not valid UTF-8 JSON") from exc


# ------------------------------------------------------------------ keys


PRODUCT_CODE = re.compile(r"^[A-Z]{3}$")
K_NUMBER = re.compile(r"^(K|DEN|BK)\d{6}$")
P_NUMBER = re.compile(r"^(P|H|N|D|BP)\d{5,6}$")
RECALL_NUMBER = re.compile(r"^Z-\d{4}-\d{4}$")
SRN = re.compile(r"^[A-Z]{2}-(MF|AR|IM|PR|SP)-\d{9}$")


def classification_key(product_code: str) -> str:
    return f"medical-devices:fda:product-code:{product_code}"


def clearance_key(k_number: str) -> str:
    return f"medical-devices:fda:510k:{k_number}"


def approval_key(pma_number: str) -> str:
    return f"medical-devices:fda:pma:{pma_number}"


def supplement_key(pma_number: str, supplement: str) -> str:
    return f"medical-devices:fda:pma:{pma_number}:{supplement}"


def recall_key(recall_number: str) -> str:
    return f"medical-devices:fda:recall:{recall_number}"


def report_key(report_number: str) -> str:
    return f"medical-devices:fda:maude:{report_number}"


def count_key(product_code: str, start: str, end: str) -> str:
    return f"medical-devices:fda:maude-count:{product_code}:{start}:{end}"


def gudid_key(di: str) -> str:
    return f"medical-devices:gudid:di:{di}"


def actor_key(srn: str) -> str:
    return f"medical-devices:eudamed:actor:{srn}"


def eudamed_device_key(basic_udi_di: str) -> str:
    return f"medical-devices:eudamed:basic-udi-di:{basic_udi_di}"


def certificate_key(number: str) -> str:
    return f"medical-devices:eudamed:certificate:{number}"


def premarket_key(number: str) -> str:
    """The FDA record key a published K, DEN or P number names (a supplement number is not part of it)."""
    number = str(number or "").strip().upper()
    return clearance_key(number) if K_NUMBER.fullmatch(number) else approval_key(number)


def _record(fmt: str, kind: str, record_key: str, *, title: Any, locator: str, fields: Mapping[str, Any],
            native_revision: Any = None, revision_order: str = "", effective_on: Any = None,
            product_codes: Sequence[str] = (), premarket_numbers: Sequence[str] = (), udi_dis: Sequence[str] = (),
            manufacturer: Any = None, manufacturer_srn: Any = None, parent_key: str | None = None,
            withheld: Sequence[str] = (), disclaimer: Mapping[str, Any] | None = None,
            caveats: Sequence[str] | None = None) -> dict[str, Any]:
    spec = FORMATS[fmt]
    if kind not in RECORD_KINDS:
        raise MedicalDevicesFormatError("schema_drift", f"unknown record kind {kind!r}")
    if not str(locator or "").startswith("https://"):
        raise MedicalDevicesFormatError("schema_drift", f"{record_key} has no HTTPS locator")
    return {
        "contract": RECORD_CONTRACT,
        "format": fmt,
        "provider": spec["provider"],
        "jurisdiction": spec["jurisdiction"],
        "record_kind": kind,
        "record_key": record_key,
        "parent_key": parent_key,
        "product_codes": sorted({p for p in product_codes if p}),
        "premarket_numbers": sorted({p for p in premarket_numbers if p}),
        "udi_dis": sorted({d for d in udi_dis if d}),
        "manufacturer": _clean(manufacturer),
        "manufacturer_srn": _clean(manufacturer_srn),
        "native_revision": _clean(native_revision),
        "revision_order": revision_order,
        "effective_on": _day(effective_on) if effective_on else None,
        "title": _clean(title) or record_key,
        "locator": locator,
        "disclaimer": dict(disclaimer) if disclaimer else None,
        "caveats": list(caveats) if caveats is not None else None,
        "minimisation": {"policy": MINIMISATION_POLICY, "withheld": sorted(set(withheld))},
        "fields": dict(fields),
    }


# ------------------------------------------------------------------ minimisation


def _personal(key: Any) -> bool:
    bare = str(key).casefold()
    return bare in PERSONAL_KEYS or bare.startswith(_PERSONAL_PREFIXES)


def withheld_keys(row: Mapping[str, Any]) -> list[str]:
    """Top-level keys of a native row that the minimisation decision drops (present and non-empty)."""
    return sorted(k for k, v in row.items() if _personal(k) and v not in (None, "", [], {}))


def minimisation_violations(record: Mapping[str, Any]) -> list[str]:
    """Paths of withheld personal keys a record still carries (empty when the record honours MD01)."""
    found = []

    def walk(value: Any, path: str) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if _personal(key) and item not in (None, "", [], {}):
                    found.append(f"{path}.{key}")
                walk(item, f"{path}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")

    walk(record.get("fields") or {}, "$.fields")
    if record.get("record_kind") != "adverse-event-report" and (record.get("fields") or {}).get("narratives"):
        found.append("$.fields.narratives")
    return sorted(set(found))


# ------------------------------------------------------------------ openFDA parsers


def _openfda(payload: Any, what: str) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """(results, disclaimer, meta) of an openFDA response; the disclaimer is required on every stored record."""
    from src.ingestion.clinical_providers import _openfda_meta

    if not isinstance(payload, Mapping):
        raise MedicalDevicesFormatError("schema_drift", f"{what} response is not an object")
    try:
        meta = _openfda_meta(payload)
    except SourcePackError as exc:
        raise MedicalDevicesFormatError("schema_drift", str(exc)) from exc
    results = payload.get("results")
    if not isinstance(results, list) or not all(isinstance(r, Mapping) for r in results):
        raise MedicalDevicesFormatError("schema_drift", f"{what} response has no results list")
    disclaimer = {"text": meta["text"], "terms": meta.get("terms"), "license": meta.get("license")}
    return [dict(r) for r in results], disclaimer, {"last_updated": meta.get("last_updated"),
                                                    "total": ((payload.get("meta") or {}).get("results") or {})
                                                    .get("total")}


def _fda_url(path: str, search: str) -> str:
    return "https://api.fda.gov" + path + "?" + urlencode({"search": search})


def _product_code(row: Mapping[str, Any], unit_code: str | None = None) -> str | None:
    code = _clean(row.get("product_code")) or _clean((row.get("openfda") or {}).get("product_code"))
    code = code.upper() if code else None
    if unit_code and code and code != unit_code:
        raise MedicalDevicesFormatError("schema_drift", f"row names product code {code}, not {unit_code}")
    return code


def parse_510k(pages: Sequence[Any], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    fmt = "openfda-device-510k-json"
    code = str(unit["product_code"]).upper()
    out = []
    for page in pages:
        rows, disclaimer, _ = _openfda(page, "510(k)")
        for row in rows:
            number = str(row.get("k_number") or "").strip().upper()
            if not K_NUMBER.fullmatch(number):
                raise MedicalDevicesFormatError("schema_drift", "510(k) row has no K number")
            fields = {
                "k_number": number, "decision_code": _clean(row.get("decision_code")),
                "decision_description": _clean(row.get("decision_description")),
                "decision_date": _day(row.get("decision_date")), "date_received": _day(row.get("date_received")),
                "clearance_type": _clean(row.get("clearance_type")), "device_name": _clean(row.get("device_name")),
                "product_code": _product_code(row, code), "applicant": _clean(row.get("applicant")),
                "city": _clean(row.get("city")), "state": _clean(row.get("state")),
                "country_code": _clean(row.get("country_code")),
                "advisory_committee": _clean(row.get("advisory_committee")),
                "statement_or_summary": _clean(row.get("statement_or_summary")),
                "third_party_flag": _flag(row.get("third_party_flag")),
                "expedited_review_flag": _flag(row.get("expedited_review_flag")),
            }
            out.append(_record(fmt, "clearance", clearance_key(number), title=f"510(k) {number}: "
                               f"{fields['device_name'] or ''}", locator=_fda_url("/device/510k.json",
                                                                                   f'k_number:"{number}"'),
                               fields=fields, effective_on=fields["decision_date"], product_codes=[code],
                               premarket_numbers=[number], manufacturer=fields["applicant"],
                               withheld=withheld_keys(row), disclaimer=disclaimer))
    return out


def parse_pma(pages: Sequence[Any], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Originals become ``approval`` records listing their published supplements; each supplement is its own
    record whose parent is the approval."""
    fmt = "openfda-device-pma-json"
    code = str(unit["product_code"]).upper()
    rows_by_number: dict[str, list[tuple[dict[str, Any], dict[str, Any], list[str]]]] = {}
    for page in pages:
        rows, disclaimer, _ = _openfda(page, "PMA")
        for row in rows:
            number = str(row.get("pma_number") or "").strip().upper()
            if not P_NUMBER.fullmatch(number):
                raise MedicalDevicesFormatError("schema_drift", "PMA row has no P number")
            rows_by_number.setdefault(number, []).append((row, disclaimer, withheld_keys(row)))
    out = []
    for number, rows in sorted(rows_by_number.items()):
        supplements = sorted({str(r.get("supplement_number") or "").strip().upper() for r, _, _ in rows} - {""})
        for row, disclaimer, withheld in rows:
            supplement = str(row.get("supplement_number") or "").strip().upper()
            fields = {
                "pma_number": number, "supplement_number": supplement or None,
                "supplement_type": _clean(row.get("supplement_type")),
                "supplement_reason": _clean(row.get("supplement_reason")),
                "decision_code": _clean(row.get("decision_code")), "decision_date": _day(row.get("decision_date")),
                "date_received": _day(row.get("date_received")), "trade_name": _clean(row.get("trade_name")),
                "generic_name": _clean(row.get("generic_name")), "product_code": _product_code(row, code),
                "applicant": _clean(row.get("applicant")), "city": _clean(row.get("city")),
                "state": _clean(row.get("state")), "advisory_committee": _clean(row.get("advisory_committee")),
                "ao_statement": _clean(row.get("ao_statement")), "docket_number": _clean(row.get("docket_number")),
                "expedited_review_flag": _flag(row.get("expedited_review_flag")),
            }
            common = {"fields": fields, "effective_on": fields["decision_date"], "product_codes": [code],
                      "premarket_numbers": [number], "manufacturer": fields["applicant"], "withheld": withheld,
                      "disclaimer": disclaimer}
            if supplement:
                out.append(_record(fmt, "supplement", supplement_key(number, supplement),
                                   title=f"PMA {number} supplement {supplement}: {fields['supplement_type'] or ''}",
                                   locator=_fda_url("/device/pma.json", f'pma_number:"{number}" AND '
                                                    f'supplement_number:"{supplement}"'),
                                   parent_key=approval_key(number), **common))
            else:
                fields["supplements_as_published"] = supplements
                out.append(_record(fmt, "approval", approval_key(number), title=f"PMA {number}: "
                                   f"{fields['trade_name'] or ''}", locator=_fda_url("/device/pma.json",
                                                                                    f'pma_number:"{number}"'),
                                   **common))
    return out


def parse_classification(pages: Sequence[Any], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    fmt = "openfda-device-classification-json"
    code = str(unit["product_code"]).upper()
    out = []
    for page in pages:
        rows, disclaimer, _ = _openfda(page, "classification")
        for row in rows:
            fields = {
                "product_code": _product_code(row, code), "device_name": _clean(row.get("device_name")),
                "device_class": _clean(row.get("device_class")),
                "regulation_number": _clean(row.get("regulation_number")),
                "medical_specialty": _clean(row.get("medical_specialty")),
                "medical_specialty_description": _clean(row.get("medical_specialty_description")),
                "review_panel": _clean(row.get("review_panel")),
                "submission_type_id": _clean(row.get("submission_type_id")),
                "definition": _clean(row.get("definition")), "implant_flag": _flag(row.get("implant_flag")),
                "life_sustain_support_flag": _flag(row.get("life_sustain_support_flag")),
                "gmp_exempt_flag": _flag(row.get("gmp_exempt_flag")),
                "third_party_flag": _flag(row.get("third_party_flag")),
            }
            if fields["product_code"] is None:
                raise MedicalDevicesFormatError("schema_drift", "classification row has no product code")
            out.append(_record(fmt, "classification", classification_key(code), title=f"Product code {code}: "
                               f"{fields['device_name'] or ''}", locator=_fda_url("/device/classification.json",
                                                                                   f'product_code:"{code}"'),
                               fields=fields, product_codes=[code], withheld=withheld_keys(row),
                               disclaimer=disclaimer))
    return out


def parse_recall(pages: Sequence[Any], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    fmt = "openfda-device-recall-json"
    code = str(unit["product_code"]).upper()
    out = []
    for page in pages:
        rows, disclaimer, _ = _openfda(page, "recall")
        for row in rows:
            number = str(row.get("product_res_number") or "").strip().upper()
            if not RECALL_NUMBER.fullmatch(number):
                raise MedicalDevicesFormatError("schema_drift", "recall row has no recall number")
            numbers = _list(row.get("k_numbers")) + _list(row.get("pma_numbers"))
            fields = {
                "recall_number": number, "res_event_number": _clean(row.get("res_event_number")),
                "cfres_id": _clean(row.get("cfres_id")), "recall_status": _clean(row.get("recall_status")),
                "event_date_initiated": _day(row.get("event_date_initiated")),
                "event_date_posted": _day(row.get("event_date_posted")),
                "event_date_terminated": _day(row.get("event_date_terminated")),
                "product_code": _product_code(row, code), "k_numbers": _list(row.get("k_numbers")),
                "pma_numbers": _list(row.get("pma_numbers")),
                "product_description": _clean(row.get("product_description")),
                "code_info": _clean(row.get("code_info")), "reason_for_recall": _clean(row.get("reason_for_recall")),
                "root_cause_description": _clean(row.get("root_cause_description")),
                "action": _clean(row.get("action")), "recalling_firm": _clean(row.get("recalling_firm")),
                "city": _clean(row.get("city")), "state": _clean(row.get("state")),
                "country": _clean(row.get("country")), "product_quantity": _clean(row.get("product_quantity")),
                "distribution_pattern": _clean(row.get("distribution_pattern")),
                "firm_fei_number": _clean(row.get("firm_fei_number")),
                "recall_class": None,
                "recall_class_note": "the recall endpoint states no class; the enforcement report of the same "
                                     "recall number does (verify)",
            }
            effective = fields["event_date_terminated"] or fields["event_date_posted"] or fields["event_date_initiated"]
            out.append(_record(fmt, "recall", recall_key(number), title=f"Recall {number}: "
                               f"{fields['recall_status'] or 'status not published'}",
                               locator=_fda_url("/device/recall.json", f'product_res_number:"{number}"'),
                               fields=fields, effective_on=effective, product_codes=[code],
                               premarket_numbers=numbers, manufacturer=fields["recalling_firm"],
                               withheld=withheld_keys(row), disclaimer=disclaimer))
    return out


def parse_enforcement(pages: Sequence[Any], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    fmt = "openfda-device-enforcement-json"
    wanted = str(unit["recall_number"]).upper()
    out = []
    for page in pages:
        rows, disclaimer, _ = _openfda(page, "enforcement")
        for row in rows:
            number = str(row.get("recall_number") or "").strip().upper()
            if number != wanted:
                raise MedicalDevicesFormatError("schema_drift", f"enforcement row names {number}, not {wanted}")
            code = _product_code(row)
            fields = {
                "recall_number": number, "event_id": _clean(row.get("event_id")),
                "recall_class": _clean(row.get("classification")), "status": _clean(row.get("status")),
                "recall_initiation_date": _day(row.get("recall_initiation_date")),
                "center_classification_date": _day(row.get("center_classification_date")),
                "termination_date": _day(row.get("termination_date")), "report_date": _day(row.get("report_date")),
                "voluntary_mandated": _clean(row.get("voluntary_mandated")),
                "initial_firm_notification": _clean(row.get("initial_firm_notification")),
                "product_description": _clean(row.get("product_description")),
                "code_info": _clean(row.get("code_info")), "reason_for_recall": _clean(row.get("reason_for_recall")),
                "recalling_firm": _clean(row.get("recalling_firm")), "city": _clean(row.get("city")),
                "state": _clean(row.get("state")), "country": _clean(row.get("country")),
                "product_quantity": _clean(row.get("product_quantity")),
                "distribution_pattern": _clean(row.get("distribution_pattern")), "product_code": code,
            }
            effective = fields["termination_date"] or fields["center_classification_date"] or fields["report_date"]
            label = fields["recall_class"] or "class not published"
            out.append(_record(fmt, "recall", recall_key(number), title=f"Recall {number} ({label}): "
                               f"{fields['status'] or ''}",
                               locator=_fda_url("/device/enforcement.json", f'recall_number:"{number}"'),
                               fields=fields, effective_on=effective, product_codes=[code] if code else [],
                               manufacturer=fields["recalling_firm"], withheld=withheld_keys(row),
                               disclaimer=disclaimer))
    return out


_MAUDE_DEVICE_FIELDS = ("brand_name", "generic_name", "manufacturer_d_name", "device_report_product_code",
                        "model_number", "catalog_number", "udi_di", "device_sequence_number", "device_availability",
                        "device_operator", "implant_flag")


def parse_event(pages: Sequence[Any], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    fmt = "openfda-device-event-json"
    code = str(unit["product_code"]).upper()
    out = []
    for page in pages:
        rows, disclaimer, _ = _openfda(page, "device event")
        for row in rows:
            number = _clean(row.get("report_number"))
            if not number:
                raise MedicalDevicesFormatError("schema_drift", "device event row has no report_number")
            devices, withheld = [], set(withheld_keys(row))
            for index, device in enumerate(row.get("device") or []):
                if not isinstance(device, Mapping):
                    continue
                withheld |= {f"device[{index}].{k}" for k in withheld_keys(device)}
                devices.append({k: _clean(device.get(k)) for k in _MAUDE_DEVICE_FIELDS})
            codes = sorted({str(d["device_report_product_code"]).upper() for d in devices
                            if d.get("device_report_product_code")})
            if code not in codes:
                raise MedicalDevicesFormatError("schema_drift", f"report {number} names no device of {code}")
            narratives = [{"text_type_code": _clean(t.get("text_type_code")), "text": _clean(t.get("text")),
                           "as_published": True}
                          for t in row.get("mdr_text") or [] if isinstance(t, Mapping) and _clean(t.get("text"))]
            fields = {
                "report_number": number, "mdr_report_key": _clean(row.get("mdr_report_key")),
                "event_type": _clean(row.get("event_type")), "date_received": _day(row.get("date_received")),
                "date_of_event": _day(row.get("date_of_event")), "date_report": _day(row.get("date_report")),
                "report_source_code": _clean(row.get("report_source_code")),
                "type_of_report": _list(row.get("type_of_report")),
                "adverse_event_flag": _flag(row.get("adverse_event_flag")),
                "product_problem_flag": _flag(row.get("product_problem_flag")),
                "product_problems": _list(row.get("product_problems")), "devices": devices,
                "narratives": narratives,
            }
            udis = [d["udi_di"] for d in devices if d.get("udi_di")]
            manufacturer = next((d["manufacturer_d_name"] for d in devices if d.get("manufacturer_d_name")), None)
            out.append(_record(fmt, "adverse-event-report", report_key(number),
                               title=f"MAUDE report {number} ({fields['event_type'] or 'event type not published'})",
                               locator=_fda_url("/device/event.json", f'report_number:"{number}"'), fields=fields,
                               effective_on=fields["date_received"], product_codes=codes, udi_dis=udis,
                               manufacturer=manufacturer, withheld=sorted(withheld), disclaimer=disclaimer,
                               caveats=MAUDE_CAVEATS))
    return out


def parse_event_counts(pages: Sequence[Any], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    fmt = "openfda-device-event-count-json"
    code = str(unit["product_code"]).upper()
    start, end = _day(unit["from"]), _day(unit["to"])
    counts: list[dict[str, Any]] = []
    disclaimer = None
    for page in pages:
        rows, disclaimer, _ = _openfda(page, "device event count")
        for row in rows:
            term, count = _clean(row.get("term")), row.get("count")
            if term is None or not isinstance(count, int) or count < 0:
                raise MedicalDevicesFormatError("schema_drift", "count rows carry a term and a nonnegative count")
            counts.append({"event_type_as_published": term, "reports": count})
    fields = {"product_code": code, "window": {"from": start, "to": end}, "count_field": "event_type.exact",
              "search": event_search(code, start, end), "counts_as_published": counts,
              "unit": "reports", "semantics": "number of MAUDE reports received in the window per event type as "
                                              "openFDA returned them; not events, patients, incidence or a rate"}
    return [_record(fmt, "adverse-event-count", count_key(code, start, end),
                    title=f"MAUDE reports for {code} received {start} to {end}, by event type",
                    locator=_fda_url("/device/event.json", event_search(code, start, end)) + "&count=event_type.exact",
                    fields=fields, effective_on=end, product_codes=[code], disclaimer=disclaimer,
                    caveats=MAUDE_CAVEATS)] if disclaimer is not None else []


def event_search(product_code: str, start: str, end: str) -> str:
    return (f'device.device_report_product_code:"{product_code}" AND date_received:'
            f'[{start.replace("-", "")} TO {end.replace("-", "")}]')


# ------------------------------------------------------------------ AccessGUDID


def _gudid_list(container: Any, key: str) -> list[dict[str, Any]]:
    items = (container or {}).get(key) if isinstance(container, Mapping) else None
    items = items if isinstance(items, list) else [items] if isinstance(items, Mapping) else []
    return [dict(i) for i in items if isinstance(i, Mapping)]


def parse_gudid(pages: Sequence[Any], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    """``pages`` = [lookup, history]; the lookup is the current version, the history the published versions."""
    fmt = "accessgudid-device-json"
    di = str(unit["di"])
    lookup = pages[0]
    device = ((lookup or {}).get("gudid") or {}).get("device") if isinstance(lookup, Mapping) else None
    if not isinstance(device, Mapping):
        raise MedicalDevicesFormatError("schema_drift", "device lookup has no gudid.device object")
    identifiers = []
    for item in _gudid_list(device.get("identifiers"), "identifier"):
        identifiers.append({"device_id": _clean(item.get("deviceId")), "type": _clean(item.get("deviceIdType")),
                            "issuing_agency": _clean(item.get("deviceIdIssuingAgency")),
                            "contains_di": _clean(item.get("containsDINumber")),
                            "package_quantity": _clean(item.get("pkgQuantity")),
                            "package_status": _clean(item.get("pkgStatus")),
                            "package_discontinue_date": _day(item.get("pkgDiscontinueDate"))})
    primary = [i for i in identifiers if str(i["type"] or "").casefold() == "primary"]
    if not primary or primary[0]["device_id"] != di:
        raise MedicalDevicesFormatError("schema_drift", f"device lookup does not name {di} as its primary DI")
    codes = [str(c.get("productCode") or "").upper() for c in _gudid_list(device.get("productCodes"),
                                                                           "fdaProductCode") if c.get("productCode")]
    submissions = [{"submission_number": _clean(s.get("submissionNumber")),
                    "supplement_number": _clean(s.get("supplementNumber"))}
                   for s in _gudid_list(device.get("premarketSubmissions"), "premarketSubmission")
                   if _clean(s.get("submissionNumber"))]
    history_payload = pages[1] if len(pages) > 1 else None
    history = []
    if history_payload is not None:
        rows = history_payload.get("history") if isinstance(history_payload, Mapping) else None
        if not isinstance(rows, list):
            raise MedicalDevicesFormatError("schema_drift", "device history has no history list")
        history = sorted(({"public_version_number": _clean(h.get("publicVersionNumber")),
                           "public_version_date": _day(h.get("publicVersionDate")),
                           "public_version_status": _clean(h.get("publicVersionStatus"))}
                          for h in rows if isinstance(h, Mapping)),
                         key=lambda h: int(h["public_version_number"] or 0))
    withheld = withheld_keys(device) + (["contacts"] if device.get("contacts") else [])
    version = _clean(device.get("publicVersionNumber"))
    fields = {
        "primary_di": di, "public_device_record_key": _clean(device.get("publicDeviceRecordKey")),
        "issuing_agency": primary[0]["issuing_agency"], "identifiers": identifiers,
        "package_dis": sorted(i["device_id"] for i in identifiers if i is not primary[0] and i["device_id"]),
        "brand_name": _clean(device.get("brandName")), "version_model_number": _clean(device.get("versionModelNumber")),
        "catalog_number": _clean(device.get("catalogNumber")), "company_name": _clean(device.get("companyName")),
        "duns_number": _clean(device.get("dunsNumber")), "device_description": _clean(device.get("deviceDescription")),
        "commercial_distribution_status": _clean(device.get("deviceCommDistributionStatus")),
        "record_status": _clean(device.get("deviceRecordStatus")),
        "public_version_number": version, "public_version_date": _day(device.get("publicVersionDate")),
        "public_version_status": _clean(device.get("publicVersionStatus")),
        "device_publish_date": _day(device.get("devicePublishDate")),
        "product_codes": sorted(set(codes)), "premarket_submissions": submissions,
        "gmdn_terms": sorted({_clean(g.get("gmdnPTName")) for g in _gudid_list(device.get("gmdnTerms"), "gmdn")
                              if _clean(g.get("gmdnPTName"))}),
        "combination_product": _flag(device.get("deviceCombinationProduct")),
        "kit": _flag(device.get("deviceKit")), "version_history_as_published": history,
    }
    dis = [i["device_id"] for i in identifiers if i["device_id"]]
    return [_record(fmt, "device-identifier", gudid_key(di), title=f"{fields['brand_name'] or di} "
                    f"({fields['version_model_number'] or 'model not published'})",
                    locator="https://accessgudid.nlm.nih.gov/devices/" + di, fields=fields,
                    native_revision=f"version:{version}" if version else None,
                    revision_order=_order(int(version) if version and version.isdigit() else 0),
                    effective_on=fields["public_version_date"], product_codes=codes,
                    premarket_numbers=[s["submission_number"] for s in submissions], udi_dis=dis,
                    manufacturer=fields["company_name"], withheld=withheld)]


# ------------------------------------------------------------------ EUDAMED (authored mapping of the public fields)


def _eudamed_url(path: str) -> str:
    return "https://ec.europa.eu" + path


def parse_eudamed_actor(pages: Sequence[Any], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    fmt = "eudamed-actor-json"
    row = pages[0]
    srn = str((row or {}).get("srn") or "").strip().upper() if isinstance(row, Mapping) else ""
    if srn != str(unit["key"]).upper() or not SRN.fullmatch(srn):
        raise MedicalDevicesFormatError("schema_drift", "actor document does not carry the declared SRN")
    address = row.get("address") if isinstance(row.get("address"), Mapping) else {}
    fields = {"srn": srn, "actor_type": _clean(row.get("actorType")), "name": _clean(row.get("name")),
              "abbreviated_name": _clean(row.get("abbreviatedName")),
              "country": _clean(row.get("countryIso2Code")), "city": _clean(address.get("city")),
              "status": _clean(row.get("status")), "version_number": _clean(row.get("versionNumber")),
              "last_update_date": _day(row.get("lastUpdateDate"))}
    withheld = withheld_keys(row) + [f"address.{k}" for k in withheld_keys(address)]
    return [_record(fmt, "actor", actor_key(srn), title=f"{fields['name']} ({srn})",
                    locator=_eudamed_url(str(unit["path"])), fields=fields,
                    native_revision=f"version:{fields['version_number']}" if fields["version_number"] else None,
                    revision_order=_order(int(fields["version_number"] or 0)), effective_on=fields["last_update_date"],
                    manufacturer=fields["name"], manufacturer_srn=srn, withheld=withheld)]


def parse_eudamed_device(pages: Sequence[Any], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    fmt = "eudamed-device-json"
    row = pages[0]
    basic = str((row or {}).get("basicUdiDi") or "").strip() if isinstance(row, Mapping) else ""
    if not basic or basic != str(unit["key"]):
        raise MedicalDevicesFormatError("schema_drift", "device document does not carry the declared Basic UDI-DI")
    udis = [{"udi_di": _clean(u.get("udiDi")), "issuing_entity": _clean(u.get("issuingEntity")),
             "trade_name": _clean(u.get("tradeName")), "status": _clean(u.get("status"))}
            for u in row.get("udiDis") or [] if isinstance(u, Mapping) and _clean(u.get("udiDi"))]
    fields = {"basic_udi_di": basic, "manufacturer_srn": _clean(row.get("manufacturerSrn")),
              "authorised_representative_srn": _clean(row.get("authorisedRepresentativeSrn")),
              "device_name": _clean(row.get("deviceName")), "risk_class": _clean(row.get("riskClass")),
              "legislation": _clean(row.get("legislation")), "status": _clean(row.get("status")),
              "udi_dis": udis, "version_number": _clean(row.get("versionNumber")),
              "last_update_date": _day(row.get("lastUpdateDate"))}
    return [_record(fmt, "eudamed-device", eudamed_device_key(basic), title=f"{fields['device_name'] or basic} "
                    f"(Basic UDI-DI {basic})", locator=_eudamed_url(str(unit["path"])), fields=fields,
                    native_revision=f"version:{fields['version_number']}" if fields["version_number"] else None,
                    revision_order=_order(int(fields["version_number"] or 0)), effective_on=fields["last_update_date"],
                    udi_dis=[u["udi_di"] for u in udis], manufacturer_srn=fields["manufacturer_srn"],
                    withheld=withheld_keys(row))]


def parse_eudamed_certificate(pages: Sequence[Any], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    fmt = "eudamed-certificate-json"
    row = pages[0]
    number = str((row or {}).get("certificateNumber") or "").strip() if isinstance(row, Mapping) else ""
    if not number or number != str(unit["key"]):
        raise MedicalDevicesFormatError("schema_drift", "certificate document does not carry the declared number")
    body = row.get("notifiedBody") if isinstance(row.get("notifiedBody"), Mapping) else {}
    fields = {"certificate_number": number, "notified_body_number": _clean(body.get("number")),
              "notified_body_name": _clean(body.get("name")), "manufacturer_srn": _clean(row.get("manufacturerSrn")),
              "certificate_type": _clean(row.get("certificateType")), "status": _clean(row.get("status")),
              "status_date": _day(row.get("statusDate")), "issue_date": _day(row.get("issueDate")),
              "starting_validity_date": _day(row.get("startingValidityDate")),
              "expiry_date": _day(row.get("expiryDate")), "revision": _clean(row.get("revision")),
              "basic_udi_dis": _list(row.get("basicUdiDis"))}
    return [_record(fmt, "certificate", certificate_key(number), title=f"Certificate {number} "
                    f"({fields['status'] or 'status not published'})", locator=_eudamed_url(str(unit["path"])),
                    fields=fields, native_revision=f"revision:{fields['revision']}" if fields["revision"] else None,
                    revision_order=_order(int(fields["revision"] or 0), fields["status_date"]),
                    effective_on=fields["status_date"] or fields["issue_date"],
                    manufacturer_srn=fields["manufacturer_srn"], withheld=withheld_keys(row))]


# ------------------------------------------------------------------ units and requests


_DI = re.compile(r"^[0-9A-Za-z+./-]{6,40}$")


def _units(fmt: str, selection: Mapping[str, Any]) -> list[dict[str, Any]]:
    key = FORMATS[fmt]["unit"]
    raw = selection.get(key) or []
    units = [dict(u) if isinstance(u, Mapping) else {"id": u} for u in raw]
    if not 1 <= len(units) <= MAX_UNITS:
        raise SourcePackError("invalid_manifest", f"a medical-devices selection names 1-{MAX_UNITS} {key}")
    out = []
    for unit in units:
        if key == "product_codes":
            code = str(unit.get("id") or "").upper()
            if not PRODUCT_CODE.fullmatch(code):
                raise SourcePackError("invalid_manifest", f"not a product code: {unit.get('id')!r}")
            out.append({"product_code": code})
        elif key == "recall_numbers":
            number = str(unit.get("id") or "").upper()
            if not RECALL_NUMBER.fullmatch(number):
                raise SourcePackError("invalid_manifest", f"not a recall number: {unit.get('id')!r}")
            out.append({"recall_number": number})
        elif key == "event_windows":
            code = str(unit.get("product_code") or "").upper()
            start, end = _day(unit.get("from")), _day(unit.get("to"))
            if not PRODUCT_CODE.fullmatch(code) or not start or not end or end < start:
                raise SourcePackError("invalid_manifest", "an event window names a product code and a date window")
            if (datetime.fromisoformat(end) - datetime.fromisoformat(start)).days > MAX_WINDOW_DAYS:
                raise SourcePackError("invalid_manifest", f"an event window is at most {MAX_WINDOW_DAYS} days")
            out.append({"product_code": code, "from": start, "to": end})
        elif key == "device_identifiers":
            di = str(unit.get("id") or "")
            if not _DI.fullmatch(di):
                raise SourcePackError("invalid_manifest", f"not a device identifier: {di!r}")
            out.append({"di": di})
        else:
            path, ident = str(unit.get("path") or ""), str(unit.get("key") or "")
            if not path.startswith("/tools/eudamed/") or ".." in path or "?" in path or not ident:
                raise SourcePackError("invalid_manifest", "a EUDAMED document names its key and a path under "
                                                          "/tools/eudamed/")
            out.append({"key": ident, "path": path})
    return out


def requests_for(fmt: str, unit: Mapping[str, Any]) -> list[tuple[str, dict[str, Any], bool]]:
    """(path relative to the endpoint, parameters, paged) for every request of one unit."""
    path = FORMATS[fmt]["path"]
    if fmt in {"openfda-device-510k-json", "openfda-device-pma-json", "openfda-device-classification-json",
               "openfda-device-recall-json"}:
        return [(path, {"search": f'product_code:"{unit["product_code"]}"', "limit": PER_PAGE}, True)]
    if fmt == "openfda-device-enforcement-json":
        return [(path, {"search": f'recall_number:"{unit["recall_number"]}"', "limit": PER_PAGE}, True)]
    if fmt == "openfda-device-event-json":
        return [(path, {"search": event_search(unit["product_code"], unit["from"], unit["to"]), "limit": PER_PAGE},
                 True)]
    if fmt == "openfda-device-event-count-json":
        return [(path, {"search": event_search(unit["product_code"], unit["from"], unit["to"]),
                        "count": "event_type.exact"}, False)]
    if fmt == "accessgudid-device-json":
        return [("/api/v2/devices/lookup.json", {"di": unit["di"]}, False),
                ("/api/v2/devices/history.json", {"di": unit["di"]}, False)]
    if fmt in {"eudamed-actor-json", "eudamed-device-json", "eudamed-certificate-json"}:
        return [(unit["path"], {}, False)]
    raise SourcePackError("invalid_manifest", f"unknown medical-devices format {fmt!r}")


_PARSERS: dict[str, Callable[[Sequence[Any], Mapping[str, Any]], list[dict[str, Any]]]] = {
    "openfda-device-510k-json": parse_510k,
    "openfda-device-pma-json": parse_pma,
    "openfda-device-classification-json": parse_classification,
    "openfda-device-recall-json": parse_recall,
    "openfda-device-enforcement-json": parse_enforcement,
    "openfda-device-event-json": parse_event,
    "openfda-device-event-count-json": parse_event_counts,
    "accessgudid-device-json": parse_gudid,
    "eudamed-actor-json": parse_eudamed_actor,
    "eudamed-device-json": parse_eudamed_device,
    "eudamed-certificate-json": parse_eudamed_certificate,
}


def parse_unit(fmt: str, responses: Sequence[bytes], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Parse the responses of one unit; every emitted record honours the minimisation decision."""
    if fmt not in _PARSERS:
        raise MedicalDevicesFormatError("schema_drift", f"unknown medical-devices format {fmt!r}")
    records = _PARSERS[fmt]([_json(raw) for raw in responses], unit)
    for record in records:
        if minimisation_violations(record):
            raise MedicalDevicesFormatError("minimisation_violation", f"{record['record_key']} carries withheld "
                                                                      "personal fields")
    return records


def medical_devices_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    declared = dict(source.get("medical_devices") or {})
    fmt = declared.get("format")
    if fmt not in FORMATS or FORMATS[fmt]["provider"] != declared.get("provider"):
        raise SourcePackError("invalid_manifest", "medical-devices sources declare a matching provider and format")
    if declared.get("live_verification") not in {"unverified-live", "verified-live"}:
        raise SourcePackError("invalid_manifest", "medical-devices sources state their LIVE_VERIFICATION status")
    if declared.get("minimisation") != MINIMISATION_POLICY:
        raise SourcePackError("invalid_manifest", "medical-devices sources declare the MD01 minimisation policy")
    host = (urlsplit(str(source.get("endpoint") or "")).hostname or "").casefold()
    if host not in PROVIDER_HOSTS[FORMATS[fmt]["provider"]]:
        raise SourcePackError("invalid_manifest", "the endpoint host is not the provider's documented host")
    _units(fmt, dict(declared.get("selection") or {}))
    auth = dict(source.get("auth") or {}).get("kind")
    if (auth != "none") != (FORMATS[fmt]["provider"] == "openfda-device"):
        raise SourcePackError("invalid_manifest", "openFDA sources declare the optional openFDA key; GUDID and "
                                                  "EUDAMED sources declare none")
    return declared


class MedicalDevicesAdapter:
    """Fetch one declared selection unit per page from the source's endpoint host and emit its records."""

    accepts_transport = True
    connector = CONNECTOR

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None) -> None:
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        self.source = json.loads(json.dumps(source))
        self.declared = medical_devices_declaration(self.source)
        self.format = self.declared["format"]
        self.provider = self.declared["provider"]
        self.units = _units(self.format, dict(self.declared.get("selection") or {}))
        self.secret = secret if self.provider == "openfda-device" else None
        if transport is None:
            from functools import partial

            transport = partial(HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"]))
        self.transport = transport
        self.definition = {
            "contract": ADAPTER_CONTRACT, "source_id": source["source_id"], "connector": source["connector"],
            "endpoint": source["endpoint"], "operations": list(source["operations"]),
            "source_hash": source["source_hash"], "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"], "limits": source["budgets"],
            "medical_devices": {"provider": self.provider, "format": self.format, "units": len(self.units),
                                "credential": "configured" if self.secret else "none",
                                "minimisation": MINIMISATION_POLICY},
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _check(self, request: Mapping[str, Any]) -> None:
        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError("operation_forbidden", "operation is not declared by the source")
        if set(request) - {"operation", "parameters", "limit", "from_ms", "to_ms"}:
            raise SourcePackError("parameter_forbidden", "runtime adapter received undeclared controls")
        if dict(request.get("parameters") or {}):
            raise SourcePackError("parameter_forbidden", "medical-devices runs fetch the declared selection only")

    def _get(self, path: str, params: Mapping[str, Any]) -> tuple[bytes | None, dict[str, Any]]:
        from src.ingestion.source_pack_runtime import _retry_after_ms

        endpoint = self.source["endpoint"].rstrip("/")
        url = "https://" + (urlsplit(endpoint).hostname or "") + path
        host = (urlsplit(endpoint).hostname or "").casefold()
        sent = dict(sorted(params.items()))
        if self.secret:
            sent["api_key"] = self.secret  # a request parameter only; never in the receipt or a record
        response = self.transport(url=url, params=sent, headers={"Accept": "application/json"},
                                  timeout=int(self.definition["limits"]["timeout_ms"]) / 1000)
        final_host = (urlsplit(str(response.get("final_url") or url)).hostname or "").casefold()
        if final_host != host:
            raise SourcePackError("network_policy", "medical-devices response was served from another host")
        status = int(response.get("status", 200))
        headers_in = {str(k).casefold(): v for k, v in dict(response.get("headers") or {}).items()}
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError("response_too_large", "response exceeds its byte limit")
        if self.secret and len(self.secret) >= 8 and self.secret.encode() in raw:
            raise SourcePackError("authentication_failed", "provider echoed the credential; response discarded")
        if status == 429:
            raise SourcePackError("rate_limited", "provider quota is temporarily exhausted",
                                  retry_after_ms=_retry_after_ms(headers_in.get("retry-after")))
        if status in {401, 403}:
            raise SourcePackError("authentication_failed", f"request refused (HTTP {status})")
        if status >= 500:
            raise SourcePackError("source_unavailable", f"provider returned HTTP {status}")
        query = urlencode(sorted(params.items()))
        receipt = {"path": path + ("?" + query if query else ""), "status": status,
                   "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
                   "origin": "fixture" if response.get("origin") == "fixture" else "live"}
        if status == 404:
            return None, receipt  # openFDA: no matches; GUDID/EUDAMED: record not published
        if status >= 400:
            raise SourcePackError("schema_drift", f"request returned HTTP {status}")
        return raw, receipt

    def _collect(self, unit: Mapping[str, Any]) -> tuple[list[bytes], list[dict[str, Any]], bool]:
        """All responses of one unit (all-or-nothing within MAX_PAGES_PER_UNIT); False when nothing is published."""
        responses, receipts = [], []
        for path, params, paged in requests_for(self.format, unit):
            skip, seen = 0, 0
            while True:
                request = {**params, **({"skip": skip} if paged and skip else {})}
                raw, receipt = self._get(path, request)
                receipts.append(receipt)
                if raw is None:
                    if not responses:
                        return [], receipts, False  # the unit's record is not published (openFDA: no matches)
                    break  # a later request (the GUDID history) is absent: kept as an empty history
                responses.append(raw)
                if not paged:
                    break
                try:
                    payload = _json(raw)
                except MedicalDevicesFormatError as exc:
                    raise SourcePackError("schema_drift", f"{exc.code}: {exc}") from exc
                rows = payload.get("results") if isinstance(payload, Mapping) else None
                seen += len(rows) if isinstance(rows, list) else 0
                total = ((payload.get("meta") or {}).get("results") or {}).get("total") \
                    if isinstance(payload, Mapping) else None
                if total is None or seen >= int(total) or not rows:
                    break
                if len(receipts) >= MAX_PAGES_PER_UNIT:
                    raise SourcePackError("budget_exhausted", "unit is longer than its page bound; never truncated")
                skip = seen
        return responses, receipts, True

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage

        self._check(request)
        index = 0 if cursor is None else int(cursor)
        if not 0 <= index < len(self.units):
            raise SourcePackError("cursor_drift", "cursor is outside the declared selection")
        unit = self.units[index]
        responses, requests, published = self._collect(unit)
        records: list[dict[str, Any]] = []
        if published:
            try:
                records = parse_unit(self.format, responses, unit)
            except MedicalDevicesFormatError as exc:
                raise SourcePackError("schema_drift", f"{exc.code}: {exc}") from exc
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        if len(records) > limit:
            raise SourcePackError("budget_exhausted", "unit has more records than the run's result budget")
        origin = "fixture" if all(r["origin"] == "fixture" for r in requests) else "live"
        last_updated = None
        if published and self.provider == "openfda-device":
            meta = (_json(responses[0]).get("meta") or {}) if responses else {}
            last_updated = _day(meta.get("last_updated"))
        receipt = {
            "contract": "noesis-medical-devices-acquisition-receipt-v1", "source_id": self.source["source_id"],
            "provider": self.provider, "format": self.format, "unit_index": index, "unit": unit,
            "requests": requests, "records": len(records), "outcome": "returned" if published else "not_published",
            "evidence_origin": origin, "live_verification": self.declared["live_verification"],
            "minimisation": MINIMISATION_POLICY, "source_last_updated": last_updated,
            "withheld_personal_fields": sum(len(r["minimisation"]["withheld"]) for r in records),
            "final_page": index + 1 >= len(self.units),
        }
        out = []
        for record in records:
            record = {**record, "evidence_origin": origin}
            out.append({
                "id": record["record_key"], "title": record["title"], "url": record["locator"], "language": "en",
                "published_at": record["effective_on"], "updated_at": record["native_revision"],
                "content": json.dumps({k: v for k, v in record.items() if k != "fields" or record["record_kind"]
                                       != "adverse-event-report"}, sort_keys=True, ensure_ascii=False),
                "medical_device_record": record, "medical_devices_receipt": receipt,
            })
        next_cursor = None if receipt["final_page"] else str(index + 1)
        return RuntimePage(tuple(out), next_cursor, sum(r["bytes"] for r in requests), receipt=receipt)


FIXTURE_SECRET = "fixture-credential-not-a-real-key"
ADAPTERS = {CONNECTOR: MedicalDevicesAdapter}


def fixture_transport(pages: Sequence[Mapping[str, Any]]) -> Callable[..., Mapping[str, Any]]:
    """Replay authored responses keyed by URL path (+ sorted query without the key); marked as fixture evidence."""
    by_key = {page["request"]: page for page in pages}

    def transport(*, url, params, headers, timeout):
        del headers, timeout
        parts = urlsplit(url)
        query = urlencode(sorted((k, v) for k, v in dict(params or {}).items() if k != "api_key"))
        key = parts.path + ("?" + query if query else "")
        page = by_key.get(key)
        if page is None:
            raise SourcePackError("fixture_missing", f"no native page for {key}")
        body = page.get("body")
        content = body.encode() if isinstance(body, str) else b"" if body is None else json.dumps(body).encode()
        return {"status": int(page.get("status", 200)), "headers": dict(page.get("headers") or {}),
                "content": content, "origin": "fixture",
                **({"final_url": page["final_url"]} if page.get("final_url") else {})}

    return transport


def replay_native_fixture(source: Mapping[str, Any], fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    adapter = MedicalDevicesAdapter(source, transport=fixture_transport(list(fixture["native_pages"])),
                                    secret=FIXTURE_SECRET)
    records, cursor = [], None
    for _ in range(len(adapter.units)):
        page = adapter.fetch_page({"operation": min(source["operations"]), "parameters": {},
                                   "limit": int(source["budgets"]["max_results"])}, cursor=cursor)
        records += [dict(item) for item in page.records]
        cursor = page.next_cursor
        if cursor is None:
            break
    return records


__all__ = [
    "ADAPTERS", "BOUNDED_COVERAGE", "CONNECTOR", "EUDAMED_MODULES", "FEATURE_FOR_PROVIDER", "FIXTURE_SECRET",
    "FORMATS", "LIVE_VERIFICATION", "MAUDE_CAVEATS", "MINIMISATION", "PROVIDER_CONTRACTS", "PROVIDER_HOSTS",
    "RECORD_CONTRACT", "RECORD_KINDS", "REVIEW_BOUNDARY", "MedicalDevicesAdapter", "MedicalDevicesFormatError",
    "fixture_transport", "medical_devices_declaration", "minimisation_violations", "parse_unit",
    "replay_native_fixture", "requests_for",
]
