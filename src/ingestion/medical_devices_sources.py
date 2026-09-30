"""Medical devices acquisition for the Clinical Evidence bundle (#2654, MD01, MD03-MD06).

One native connector, ``medical-devices``, registered in the ``clinical-evidence``
source pack, reads a bounded, declared selection from one publisher per source
and emits ``noesis-medical-device-record-v1`` records
(:mod:`src.kb.medical_devices_records`) exactly as the regulator published them:

* ``openfda-510k-json`` - openFDA ``/device/510k.json``, one declared K number
  per unit: the clearance with its decision code, decision date and product
  code (MD03);
* ``openfda-pma-json`` - openFDA ``/device/pma.json``, one declared P number per
  unit: the original approval and every published supplement, each keyed by P
  number and supplement number (MD03);
* ``openfda-classification-json`` - openFDA ``/device/classification.json``, one
  declared product code per unit: the device classification (MD03);
* ``openfda-recall-json`` - openFDA ``/device/recall.json`` plus the matching
  ``/device/enforcement.json`` report, one declared recall number per unit:
  the recall with its class, status and reason as published (MD04);
* ``openfda-event-json`` - openFDA ``/device/event.json`` (MAUDE), one product
  code and a received-date window of at most one year per unit: every report
  keyed by report number, and the published ``count=event_type.exact`` tally
  for the same window (MD04). Report counts are reports, never incidence;
* ``gudid-device-json`` - AccessGUDID ``/api/v3/devices/lookup.json``, one
  declared primary DI per unit: the device identifier record with package DIs,
  product codes and premarket submission numbers as published (MD05);
* ``eudamed-actor-json`` / ``eudamed-device-json`` / ``eudamed-certificate-json``
  - EUDAMED public modules, one declared SRN, Basic UDI-DI or certificate per
  unit (MD06). Modules EUDAMED does not publish are explicit gaps
  (``EUDAMED_MODULES``).

The MD01 data-minimisation decision (``MINIMISATION``) is applied twice: the
parsers never copy personal fields (MAUDE patient sections, reporter and
contact persons, 510(k) contact names, GUDID customer contacts, EUDAMED PRRC
and contact persons, street addresses), and the record store refuses any
record that still carries one. Every page is one selection unit and is
all-or-nothing: a result longer than the budget is ``budget_exhausted``, never
truncated; a response from another host is a network-policy failure. Receipts
name every request path, status and response digest. A declared unit the
publisher answers with "not found" becomes a ``not-published`` revision (a
removal is a revision, never a deletion).

Nothing here detects safety signals, infers causality from adverse-event
reports or gives clinical advice.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import urlencode, urlsplit

from src.ingestion.source_packs import SourcePackError

ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
RECORD_CONTRACT = "noesis-medical-device-record-v1"
RECEIPT_CONTRACT = "noesis-medical-device-acquisition-receipt-v1"
CONNECTOR = "medical-devices"
MINIMISATION_ID = "medical-devices-minimisation-v1"
MAX_UNITS = 25
MAX_RESULTS = 100
REVIEW_BOUNDARY = ("Medical device records are kept as each regulator published them. Nothing here detects safety "
                   "signals, infers causality from adverse-event reports or gives clinical advice, and no patient "
                   "data beyond what regulators publish is stored.")
COUNT_SEMANTICS = ("MAUDE figures are counts of reports received by FDA as published, never incidence, rates, "
                   "denominators or causal events.")
# FDA's published limitations of MAUDE data, attached to every adverse-event report and count (MD01).
MAUDE_CAVEATS = (
    ("MAUDE is a passive surveillance system: reports may be incomplete, inaccurate, untimely, unverified or biased, "
     "and the same event can be reported more than once."),
    "A report does not establish that a device caused or contributed to the event; causality is not determined.",
    ("Report counts cannot be used to estimate the incidence or prevalence of an event or to compare devices: the "
     "number of devices in use is not known."),
    ("Reporting is influenced by publicity, litigation, the reporting requirements of each reporter type and the "
     "time since marketing; the database is updated and reports may be revised or supplemented."),
)
FORMATS: dict[str, dict[str, Any]] = {
    "openfda-510k-json": {"provider": "openfda-device", "unit": "k_numbers", "kind": "clearance"},
    "openfda-pma-json": {"provider": "openfda-device", "unit": "pma_numbers", "kind": "approval"},
    "openfda-classification-json": {"provider": "openfda-device", "unit": "product_codes",
                                    "kind": "classification"},
    "openfda-recall-json": {"provider": "openfda-device", "unit": "recall_numbers", "kind": "recall"},
    "openfda-event-json": {"provider": "openfda-device", "unit": "event_windows", "kind": "adverse-event-report"},
    "gudid-device-json": {"provider": "accessgudid", "unit": "device_identifiers", "kind": "device-identifier"},
    "eudamed-actor-json": {"provider": "eudamed", "unit": "actors", "kind": "eudamed-actor"},
    "eudamed-device-json": {"provider": "eudamed", "unit": "basic_udi_dis", "kind": "eudamed-device"},
    "eudamed-certificate-json": {"provider": "eudamed", "unit": "certificates", "kind": "eudamed-certificate"},
}
PROVIDERS = ("openfda-device", "accessgudid", "eudamed")
JURISDICTION = {"openfda-device": ("US", "FDA"), "accessgudid": ("US", "FDA"),
                "eudamed": ("EU", "European Commission (EUDAMED)")}
PUBLISHERS = {"openfda-device": "U.S. Food and Drug Administration (openFDA device endpoints)",
              "accessgudid": "U.S. National Library of Medicine and FDA (AccessGUDID)",
              "eudamed": "European Commission (EUDAMED public site)"}
LICENCES = {"openfda-device": "openfda-terms", "accessgudid": "us-government-work-nlm-terms",
            "eudamed": "ec-reuse-2011-833"}
ATTRIBUTION = {
    "openfda-device": "Source: U.S. Food and Drug Administration, openFDA. openFDA does not endorse this use.",
    "accessgudid": "Source: AccessGUDID, U.S. National Library of Medicine and U.S. Food and Drug Administration.",
    "eudamed": "Source: European Commission, EUDAMED (reuse under Commission Decision 2011/833/EU).",
}
# Features of the Clinical Evidence bundle (MD12): each provider is its own optional feature.
FEATURES = {"openfda-device": "medical-devices-fda", "accessgudid": "medical-devices-gudid",
            "eudamed": "medical-devices-eudamed"}

# MD01 access decisions. Endpoints, fields and terms are recorded from the publishers' documentation as known
# without network access (the terms pages could not be re-fetched from this runtime); every item marked ``verify``
# must be checked against the live pages, terms and a real response before a dated live run is accepted (MD14).
PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "openfda-device": {
        "publisher": PUBLISHERS["openfda-device"],
        "documentation": "https://open.fda.gov/apis/device/",
        "endpoints": ["https://api.fda.gov/device/510k.json", "https://api.fda.gov/device/pma.json",
                      "https://api.fda.gov/device/classification.json", "https://api.fda.gov/device/recall.json",
                      "https://api.fda.gov/device/enforcement.json", "https://api.fda.gov/device/event.json"],
        "formats": ["openfda-510k-json", "openfda-pma-json", "openfda-classification-json", "openfda-recall-json",
                    "openfda-event-json"],
        "access": "Elasticsearch-style search parameter on declared identifiers only (k_number, pma_number, "
                  "product_code, product_res_number / recall_number, device product code with a received-date "
                  "window); never a crawl",
        "authentication": "none; an optional api.data.gov key (NOESIS_OPENFDA_API_KEY) raises quotas but is not "
                          "used by this connector and is never stored",
        "rate_limits": "240 requests per minute and 1,000 requests per day per IP without a key (verify); at most "
                       f"{MAX_UNITS} declared units per source per run; at most {MAX_RESULTS} results per request",
        "revisions": "meta.last_updated is the dataset revision; a changed payload for a key is a new record "
                     "revision; a PMA supplement is its own record keyed by P number and supplement number; a "
                     "declared unit answered NOT_FOUND becomes a not-published revision",
        "licence": "openFDA Terms of Service: data are public domain US government works (CC0 where FDA states it); "
                   "no FDA endorsement may be implied; openFDA's disclaimer is kept on every record",
        "attribution": ATTRIBUTION["openfda-device"],
        "caveats": "MAUDE limitations (MAUDE_CAVEATS) attached to every adverse-event report and count",
        "access_decision": "unverified-live",
    },
    "accessgudid": {
        "publisher": PUBLISHERS["accessgudid"],
        "documentation": "https://accessgudid.nlm.nih.gov/resources/developers",
        "endpoints": ["https://accessgudid.nlm.nih.gov/api/v3/devices/lookup.json?di={primary_di} (verify)"],
        "formats": ["gudid-device-json"],
        "access": "device lookup by declared primary DI; the history and bulk download endpoints are not used",
        "authentication": "none",
        "rate_limits": "none published; NLM asks for reasonable use (verify); at most "
                       f"{MAX_UNITS} declared DIs per run",
        "revisions": "publicVersionNumber and publicVersionDate are the record revision; an older version observed "
                     "later is kept as an older observation and never becomes current",
        "licence": "GUDID data are public US government data published by NLM; cite AccessGUDID; no NLM or FDA "
                   "endorsement (verify the NLM terms page)",
        "attribution": ATTRIBUTION["accessgudid"],
        "caveats": "labeler-submitted data; FDA does not verify every attribute",
        "access_decision": "unverified-live",
    },
    "eudamed": {
        "publisher": PUBLISHERS["eudamed"],
        "documentation": "https://ec.europa.eu/tools/eudamed/",
        "endpoints": ["https://ec.europa.eu/tools/eudamed/api/actors?srn={srn} (verify)",
                      "https://ec.europa.eu/tools/eudamed/api/devices/basicUdiData?basicUdi={basic_udi_di} (verify)",
                      ("https://ec.europa.eu/tools/eudamed/api/certificates?certificateNumber={number}"
                       "&notifiedBody={nb} (verify)")],
        "formats": ["eudamed-actor-json", "eudamed-device-json", "eudamed-certificate-json"],
        "access": "the public site's JSON backend for the actor, UDI/device and notified-body/certificate modules; "
                  "no documented public API or bulk download exists (verify paths and fields before a live run)",
        "authentication": "none",
        "rate_limits": "none published (verify); at most 25 declared units per source per run",
        "revisions": "EUDAMED version numbers and last-update dates are the record revision; a certificate status "
                     "change (issued, suspended, withdrawn, expired, refused) is a new revision",
        "licence": "Commission reuse policy (Decision 2011/833/EU) per the EUDAMED legal notice; personal data of "
                   "contact persons and PRRCs are excluded from reuse here (verify the legal notice)",
        "attribution": ATTRIBUTION["eudamed"],
        "caveats": "only the modules listed as available in EUDAMED_MODULES are public; the others are gaps",
        "access_decision": "unverified-live",
    },
}
LIVE_VERIFICATION = {
    provider: {"status": contract["access_decision"], "intended": "verified-live",
               "note": "no dated live run from this runtime; offline fixtures only (MD14, #2723)"}
    for provider, contract in PROVIDER_CONTRACTS.items()
}
# EUDAMED module availability as audited (MD01). Unavailable modules stay explicit gaps in every answer.
EUDAMED_MODULES = {
    "actor-registration": {"status": "available", "format": "eudamed-actor-json"},
    "udi-device-registration": {"status": "available", "format": "eudamed-device-json"},
    "notified-bodies-certificates": {"status": "available", "format": "eudamed-certificate-json"},
    "vigilance-post-market-surveillance": {"status": "unavailable",
                                           "reason": "not published on the public site; vigilance reports and "
                                                     "field safety notices are not acquired"},
    "clinical-investigations-performance-studies": {"status": "unavailable",
                                                    "reason": "not published on the public site"},
    "market-surveillance": {"status": "unavailable", "reason": "not public (competent-authority module)"},
}
DECLINED = {
    "openfda-registrationlisting": {
        "source": "openFDA /device/registrationlisting.json",
        "reason": "establishment registration names owner/operator contact persons and addresses; manufacturers are "
                  "taken from clearance, approval, GUDID and EUDAMED records instead (documented, not acquired)"},
    "maude-patient": {"source": "MAUDE patient sections",
                      "reason": "patient age, sex, weight, outcomes and treatments are excluded (MINIMISATION)"},
    "gudid-bulk": {"source": "AccessGUDID full and delta releases", "reason": "bulk download outside the bounded "
                                                                              "coverage"},
    "eudamed-vigilance": {"source": "EUDAMED vigilance module", "reason": "not publicly available"},
}
IDENTIFIERS = {
    "openfda-device": ["510(k) K number", "PMA P number + supplement number", "three-letter product code",
                       "recall number (Z-nnnn-yyyy) and recall event id", "MDR report number and mdr_report_key",
                       "device UDI-DI where the report publishes one"],
    "accessgudid": ["primary DI (GS1 GTIN, HIBCC or ICCBBA)", "package DIs", "labeler DUNS",
                    "premarket submission numbers", "FDA product codes"],
    "eudamed": ["actor SRN", "Basic UDI-DI", "UDI-DI", "certificate number + notified body number"],
}
MINIMISATION = {
    "id": MINIMISATION_ID,
    "policy": "store what regulators publish about devices and organisations; exclude personal data of patients, "
              "reporters and contact persons",
    "excluded": ["MAUDE patient sections (age, sex, weight, ethnicity, outcomes, treatments)",
                 "MAUDE reporter and manufacturer/distributor contact names, phones, emails and street addresses",
                 "510(k) contact person and street address", "GUDID customer contact phone and email",
                 "EUDAMED PRRC and contact persons, phones and emails", "street addresses and postcodes of firms"],
    "stored": ["organisation names, cities, states and countries as published", "device identifiers and names",
               "decision and recall fields as published",
               "MAUDE event type, dates, product problems and device fields"],
    "restricted": {"field": "MAUDE narrative text (mdr_text) as published",
                   "scope": "knowledge:clinical:devices:narratives:read",
                   "note": "FDA redacts narratives before release, but they can still describe patients; they are "
                           "stored verbatim and returned only to principals holding the narrative scope"},
    "retention": "revisions are kept for provenance; a namespace deletion removes them; no personal field is ever "
                 "written, so none needs purging",
    "who_may_query": "namespace readers with knowledge:clinical:read; narratives additionally need the narrative "
                     "scope",
}
BOUNDED_COVERAGE = {
    "seed": "declared devices only (fixtures: the fictional Exampla Medical and Northwind Medtech devices); units "
            "are declared identifiers, never a search crawl",
    "window": "clearances, approvals and recalls decided or initiated from 2020-01-01; MAUDE windows of at most one "
              "year per unit",
    "caps": f"at most {MAX_UNITS} units per source per run and {MAX_RESULTS} results per request; a larger result "
            "is budget_exhausted, never truncated",
    "jurisdictions": ["US (FDA)", "EU (EUDAMED public modules)"],
    "justification": "enough to answer a device's regulatory history and report counts for declared devices while "
                     "staying within unauthenticated quotas and away from bulk personal data",
}

_K = re.compile(r"^K\d{6}$")
_P = re.compile(r"^[PN]\d{5,6}$")
_CODE = re.compile(r"^[A-Z]{3}$")
_RECALL = re.compile(r"^Z-\d{4}-\d{4}$")
_DI = re.compile(r"^[0-9A-Z+$/.\-]{6,40}$")
_SRN = re.compile(r"^[A-Z]{2}-(MF|AR|IM|PR)-\d{9}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NB = re.compile(r"^\d{4}$")


class MedicalDeviceFormatError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _clean(value: Any) -> Any:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def iso(value: Any) -> str | None:
    """openFDA ``YYYYMMDD`` or ``YYYY-MM-DD``; anything else is unknown (never guessed)."""
    text = _clean(value)
    if not text:
        return None
    if re.fullmatch(r"\d{8}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    if _DATE.fullmatch(text[:10]):
        return text[:10]
    return None


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MedicalDeviceFormatError("schema_drift", "response is not JSON") from exc


def _meta(payload: Any) -> dict[str, Any]:
    meta = payload.get("meta") if isinstance(payload, Mapping) else None
    if not isinstance(meta, Mapping) or not str(meta.get("disclaimer") or "").strip():
        raise MedicalDeviceFormatError("schema_drift", "openFDA response lacks meta.disclaimer; refusing to store it "
                                                       "without")
    return {"text": str(meta["disclaimer"]), "terms": meta.get("terms"), "license": meta.get("license"),
            "last_updated": iso(meta.get("last_updated"))}


def _not_found(payload: Any) -> bool:
    return isinstance(payload, Mapping) and (payload.get("error") or {}).get("code") == "NOT_FOUND"


def _results(payload: Any) -> list[dict[str, Any]]:
    results = payload.get("results") if isinstance(payload, Mapping) else None
    if not isinstance(results, list):
        raise MedicalDeviceFormatError("schema_drift", "openFDA response lacks results")
    total = ((payload.get("meta") or {}).get("results") or {}).get("total")
    if isinstance(total, int) and total > len(results):
        raise MedicalDeviceFormatError("input_limit", "the publisher has more results than one page holds")
    return [r for r in results if isinstance(r, Mapping)]


def _base(provider: str, kind: str, key: str, native_id: str, locator: str, *, native_revision: Any,
          revision_order: Any, as_of: Any, fields: Mapping[str, Any], identifiers: Sequence[Mapping[str, Any]] = (),
          links: Sequence[Mapping[str, Any]] = (), disclaimer: Mapping[str, Any] | None = None,
          caveats: Sequence[str] = (), state: str = "published") -> dict[str, Any]:
    jurisdiction, authority = JURISDICTION[provider]
    return {"contract": RECORD_CONTRACT, "record_kind": kind, "record_key": key, "provider": provider,
            "jurisdiction": jurisdiction, "authority": authority, "native_id": native_id,
            "native_revision": None if native_revision is None else str(native_revision),
            "revision_order": str(revision_order or native_revision or ""), "as_of": as_of, "locator": locator,
            "publication_state": state, "fields": dict(fields), "identifiers": [dict(i) for i in identifiers],
            "links_as_published": [dict(link) for link in links], "caveats": list(caveats),
            "disclaimer": dict(disclaimer) if disclaimer else None, "attribution": ATTRIBUTION[provider],
            "license": LICENCES[provider], "minimisation": MINIMISATION_ID}


def _ids(*pairs: tuple[str, Any]) -> list[dict[str, str]]:
    return [{"scheme": s, "value": str(v)} for s, v in pairs if _clean(v)]


def _firm(item: Mapping[str, Any], name_field: str) -> dict[str, Any]:
    """An organisation as published: name, city, state, country; never a street address or a contact person."""
    return {"name_as_published": _clean(item.get(name_field)), "city": _clean(item.get("city")),
            "state": _clean(item.get("state")),
            "country": _clean(item.get("country_code") or item.get("country"))}


# ----------------------------------------------------------------- keys


def key_510k(number: str) -> str:
    return f"medical-devices:fda:510k:{number}"


def key_pma(number: str, supplement: str | None = None) -> str:
    return f"medical-devices:fda:pma:{number}" + (f":{supplement}" if supplement else "")


def key_product_code(code: str) -> str:
    return f"medical-devices:fda:product-code:{code}"


def key_recall(number: str) -> str:
    return f"medical-devices:fda:recall:{number}"


def key_report(number: str) -> str:
    return f"medical-devices:fda:mdr:{number}"


def key_count(code: str, start: str, end: str) -> str:
    return f"medical-devices:fda:mdr-count:{code}:{start}:{end}"


def key_di(di: str) -> str:
    return f"medical-devices:gudid:di:{di}"


def key_actor(srn: str) -> str:
    return f"medical-devices:eudamed:actor:{srn}"


def key_basic_udi(basic: str) -> str:
    return f"medical-devices:eudamed:basic-udi-di:{basic}"


def key_certificate(notified_body: str, number: str) -> str:
    return f"medical-devices:eudamed:certificate:{notified_body}:{number}"


# ----------------------------------------------------------------- openFDA parsers


def _openfda_locator(path: str) -> str:
    return "https://api.fda.gov" + path


def parse_510k(responses: Mapping[str, bytes], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    number = unit["k_number"]
    payload = _json(responses["clearance"])
    locator = _openfda_locator(f'/device/510k.json?search=k_number:"{number}"')
    if _not_found(payload):
        return [_base("openfda-device", "clearance", key_510k(number), number, locator, native_revision=None,
                      revision_order="", as_of=None, fields={"k_number": number}, state="not-published")]
    meta = _meta(payload)
    out = []
    for item in _results(payload):
        if item.get("k_number") != number:
            raise MedicalDeviceFormatError("schema_drift", "510(k) result does not carry the declared K number")
        code = _clean(item.get("product_code"))
        fields = {
            "k_number": number, "applicant": _firm(item, "applicant"), "device_name": _clean(item.get("device_name")),
            "product_code": code, "decision_code": _clean(item.get("decision_code")),
            "decision_description": _clean(item.get("decision_description")),
            "decision_date": iso(item.get("decision_date")), "date_received": iso(item.get("date_received")),
            "clearance_type": _clean(item.get("clearance_type")),
            "advisory_committee_description": _clean(item.get("advisory_committee_description")),
            "statement_or_summary": _clean(item.get("statement_or_summary")),
            "third_party_flag": _clean(item.get("third_party_flag")),
            "expedited_review_flag": _clean(item.get("expedited_review_flag")),
        }
        out.append(_base("openfda-device", "clearance", key_510k(number), number, locator,
                         native_revision=meta["last_updated"], revision_order=meta["last_updated"],
                         as_of=meta["last_updated"], fields=fields, disclaimer=meta,
                         identifiers=_ids(("fda-510k", number), ("fda-product-code", code)),
                         links=_ids(("fda-product-code", code))))
    return out


def parse_pma(responses: Mapping[str, bytes], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    number = unit["pma_number"]
    payload = _json(responses["approvals"])
    locator = _openfda_locator(f'/device/pma.json?search=pma_number:"{number}"')
    if _not_found(payload):
        return [_base("openfda-device", "approval", key_pma(number), number, locator, native_revision=None,
                      revision_order="", as_of=None, fields={"pma_number": number, "supplement_number": None},
                      state="not-published")]
    meta = _meta(payload)
    out = []
    for item in _results(payload):
        if item.get("pma_number") != number:
            raise MedicalDeviceFormatError("schema_drift", "PMA result does not carry the declared P number")
        supplement = _clean(item.get("supplement_number"))
        code = _clean(item.get("product_code"))
        fields = {
            "pma_number": number, "supplement_number": supplement, "applicant": _firm(item, "applicant"),
            "trade_name": _clean(item.get("trade_name")), "generic_name": _clean(item.get("generic_name")),
            "product_code": code, "decision_code": _clean(item.get("decision_code")),
            "decision_date": iso(item.get("decision_date")), "date_received": iso(item.get("date_received")),
            "supplement_type": _clean(item.get("supplement_type")),
            "supplement_reason": _clean(item.get("supplement_reason")),
            "advisory_committee_description": _clean(item.get("advisory_committee_description")),
            "ao_statement": _clean(item.get("ao_statement")), "docket_number": _clean(item.get("docket_number")),
        }
        kind = "approval-supplement" if supplement else "approval"
        if supplement:
            fields["approval_key"] = key_pma(number)
        out.append(_base("openfda-device", kind, key_pma(number, supplement), f"{number}{supplement or ''}",
                         locator, native_revision=meta["last_updated"], revision_order=meta["last_updated"],
                         as_of=meta["last_updated"], fields=fields, disclaimer=meta,
                         identifiers=_ids(("fda-pma", number), ("fda-pma-supplement",
                                                                f"{number}/{supplement}" if supplement else None),
                                          ("fda-product-code", code)),
                         links=_ids(("fda-product-code", code))))
    if len({r["record_key"] for r in out}) != len(out):
        raise MedicalDeviceFormatError("schema_drift", "a PMA page repeats a supplement number")
    return out


def parse_classification(responses: Mapping[str, bytes], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    code = unit["product_code"]
    payload = _json(responses["classification"])
    locator = _openfda_locator(f'/device/classification.json?search=product_code:"{code}"')
    if _not_found(payload):
        return [_base("openfda-device", "classification", key_product_code(code), code, locator,
                      native_revision=None, revision_order="", as_of=None, fields={"product_code": code},
                      state="not-published")]
    meta = _meta(payload)
    out = []
    for item in _results(payload):
        if item.get("product_code") != code:
            raise MedicalDeviceFormatError("schema_drift", "classification result does not carry the product code")
        fields = {"product_code": code, "device_name": _clean(item.get("device_name")),
                  "device_class": _clean(item.get("device_class")),
                  "regulation_number": _clean(item.get("regulation_number")),
                  "medical_specialty_description": _clean(item.get("medical_specialty_description")),
                  "review_panel": _clean(item.get("review_panel")),
                  "submission_type_id": _clean(item.get("submission_type_id")),
                  "implant_flag": _clean(item.get("implant_flag")),
                  "life_sustain_support_flag": _clean(item.get("life_sustain_support_flag")),
                  "gmp_exempt_flag": _clean(item.get("gmp_exempt_flag")),
                  "definition": _clean(item.get("definition"))}
        out.append(_base("openfda-device", "classification", key_product_code(code), code, locator,
                         native_revision=meta["last_updated"], revision_order=meta["last_updated"],
                         as_of=meta["last_updated"], fields=fields, disclaimer=meta,
                         identifiers=_ids(("fda-product-code", code))))
    return out


def parse_recall(responses: Mapping[str, bytes], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    number = unit["recall_number"]
    recall, enforcement = _json(responses["recall"]), _json(responses["enforcement"])
    locator = _openfda_locator(f'/device/recall.json?search=product_res_number:"{number}"')
    if _not_found(recall):
        return [_base("openfda-device", "recall", key_recall(number), number, locator, native_revision=None,
                      revision_order="", as_of=None, fields={"recall_number": number}, state="not-published")]
    meta = _meta(recall)
    items = _results(recall)
    if len(items) != 1 or items[0].get("product_res_number") != number:
        raise MedicalDeviceFormatError("schema_drift", "recall result does not carry the declared recall number")
    item = items[0]
    report: Mapping[str, Any] = {}
    enforcement_revision = None
    if not _not_found(enforcement):
        reports = [r for r in _results(enforcement) if r.get("recall_number") == number]
        if len(reports) > 1:
            raise MedicalDeviceFormatError("schema_drift", "several enforcement reports carry one recall number")
        report = reports[0] if reports else {}
        enforcement_revision = _meta(enforcement)["last_updated"]
    code = _clean(item.get("product_code"))
    k_numbers = sorted({str(k) for k in item.get("k_numbers") or [] if _clean(k)})
    pma_numbers = sorted({str(p) for p in item.get("pma_numbers") or [] if _clean(p)})
    fields = {
        "recall_number": number, "event_id": _clean(item.get("res_event_number")),
        "recall_class": _clean(report.get("classification")),
        "status_as_published": _clean(item.get("recall_status")),
        "enforcement_status_as_published": _clean(report.get("status")),
        "recalling_firm": _firm(item, "recalling_firm"),
        "reason_for_recall": _clean(item.get("reason_for_recall") or report.get("reason_for_recall")),
        "root_cause_description": _clean(item.get("root_cause_description")),
        "action": _clean(item.get("action")), "product_code": code,
        "product_description": _clean(item.get("product_description")),
        "code_info": _clean(item.get("code_info")), "k_numbers": k_numbers, "pma_numbers": pma_numbers,
        "event_date_initiated": iso(item.get("event_date_initiated")),
        "event_date_posted": iso(item.get("event_date_posted")),
        "event_date_terminated": iso(item.get("event_date_terminated")),
        "center_classification_date": iso(report.get("center_classification_date")),
        "enforcement_report_revision": enforcement_revision,
    }
    revision = max(filter(None, [meta["last_updated"], enforcement_revision]), default=None)
    links = _ids(("fda-product-code", code)) + [{"scheme": "fda-510k", "value": k} for k in k_numbers] + \
        [{"scheme": "fda-pma", "value": p} for p in pma_numbers]
    return [_base("openfda-device", "recall", key_recall(number), number, locator, native_revision=revision,
                  revision_order=revision, as_of=revision, fields=fields, disclaimer=meta,
                  identifiers=_ids(("fda-recall", number), ("fda-recall-event", fields["event_id"])), links=links)]


_EVENT_DEVICE = ("brand_name", "generic_name", "manufacturer_d_name", "device_report_product_code", "model_number",
                 "catalog_number", "udi_di", "device_sequence_number", "device_operator")


def parse_events(responses: Mapping[str, bytes], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    code, start, end = unit["product_code"], unit["received_from"], unit["received_to"]
    payload, counts = _json(responses["reports"]), _json(responses["counts"])
    search = f'device.device_report_product_code:"{code}" AND date_received:[{start} TO {end}]'
    locator = _openfda_locator("/device/event.json?" + urlencode({"search": search}))
    out = []
    count_meta = None
    if not _not_found(counts):
        count_meta = _meta(counts)
        terms = counts.get("results")
        if not isinstance(terms, list):
            raise MedicalDeviceFormatError("schema_drift", "openFDA count response lacks results")
        tally = [{"term": str(t.get("term")), "count": int(t.get("count"))} for t in terms
                 if isinstance(t, Mapping) and isinstance(t.get("count"), int)]
    else:
        tally = []
    out.append(_base(
        "openfda-device", "report-count", key_count(code, start, end), f"{code}:{start}:{end}",
        locator + "&count=event_type.exact", native_revision=(count_meta or {}).get("last_updated"),
        revision_order=(count_meta or {}).get("last_updated"), as_of=(count_meta or {}).get("last_updated"),
        fields={"product_code": code, "window": {"received_from": start, "received_to": end},
                "count_field": "event_type.exact", "counts_as_published": tally,
                "count_semantics": COUNT_SEMANTICS},
        disclaimer=count_meta, caveats=MAUDE_CAVEATS, identifiers=_ids(("fda-product-code", code)),
        state="published" if count_meta else "not-published"))
    if _not_found(payload):
        return out
    meta = _meta(payload)
    seen = set()
    for item in _results(payload):
        number = _clean(item.get("report_number"))
        if not number or number in seen:
            raise MedicalDeviceFormatError("schema_drift", "MAUDE report lacks a unique report number")
        seen.add(number)
        devices = []
        for device in item.get("device") or []:
            entry = {k: _clean(device.get(k)) for k in _EVENT_DEVICE}
            entry["manufacturer_name"] = entry.pop("manufacturer_d_name")
            entry["product_code"] = entry.pop("device_report_product_code")
            devices.append(entry)
        narratives = [{"text_type_code": _clean(t.get("text_type_code")), "text": str(t.get("text"))}
                      for t in item.get("mdr_text") or [] if isinstance(t, Mapping) and _clean(t.get("text"))]
        fields = {
            "report_number": number, "mdr_report_key": _clean(item.get("mdr_report_key")),
            "event_type": _clean(item.get("event_type")), "date_received": iso(item.get("date_received")),
            "date_of_event": iso(item.get("date_of_event")), "date_report": iso(item.get("date_report")),
            "report_source_code": _clean(item.get("report_source_code")),
            "adverse_event_flag": _clean(item.get("adverse_event_flag")),
            "product_problem_flag": _clean(item.get("product_problem_flag")),
            "product_problems": [str(p) for p in item.get("product_problems") or []],
            "devices": devices, "narratives": narratives,
            "patient_sections_excluded": len(item.get("patient") or []),
        }
        dis = sorted({d["udi_di"] for d in devices if d.get("udi_di")})
        links = _ids(("fda-product-code", code)) + [{"scheme": "udi-di", "value": di} for di in dis]
        out.append(_base("openfda-device", "adverse-event-report", key_report(number), number,
                         _openfda_locator(f'/device/event.json?search=report_number:"{number}"'),
                         native_revision=meta["last_updated"], revision_order=meta["last_updated"],
                         as_of=meta["last_updated"], fields=fields, disclaimer=meta, caveats=MAUDE_CAVEATS,
                         identifiers=_ids(("fda-mdr-report", number), ("fda-mdr-key", fields["mdr_report_key"])),
                         links=links))
    return out


# ----------------------------------------------------------------- AccessGUDID


def _gudid_list(value: Any, key: str) -> list[dict[str, Any]]:
    if isinstance(value, Mapping):
        value = value.get(key)
    if isinstance(value, Mapping):
        value = [value]
    return [v for v in value or [] if isinstance(v, Mapping)]


def parse_gudid(responses: Mapping[str, bytes], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    di = unit["di"]
    payload = _json(responses["device"])
    locator = f"https://accessgudid.nlm.nih.gov/devices/{di}"
    if isinstance(payload, Mapping) and payload.get("error"):
        return [_base("accessgudid", "device-identifier", key_di(di), di, locator, native_revision=None,
                      revision_order="", as_of=None, fields={"primary_di": di}, state="not-published")]
    device = ((payload or {}).get("gudid") or {}).get("device") if isinstance(payload, Mapping) else None
    if not isinstance(device, Mapping):
        raise MedicalDeviceFormatError("schema_drift", "AccessGUDID response lacks gudid.device")
    identifiers = _gudid_list(device.get("identifiers"), "identifier")
    primary = [i for i in identifiers if i.get("deviceIdType") == "Primary"]
    if len(primary) != 1 or primary[0].get("deviceId") != di:
        raise MedicalDeviceFormatError("schema_drift", "AccessGUDID record does not carry the declared primary DI")
    packages = [{"di": _clean(i.get("deviceId")), "quantity": i.get("pkgQuantity"),
                 "contains_di": _clean(i.get("containsDINumber")), "package_type": _clean(i.get("pkgType")),
                 "status": _clean(i.get("pkgStatus")), "discontinued": iso(i.get("pkgDiscontinueDate"))}
                for i in identifiers if i.get("deviceIdType") == "Package"]
    secondary = [{"di": _clean(i.get("deviceId")), "type": _clean(i.get("deviceIdType")),
                  "issuing_agency": _clean(i.get("deviceIdIssuingAgency"))}
                 for i in identifiers if i.get("deviceIdType") not in {"Primary", "Package"}]
    codes = [{"code": _clean(c.get("productCode")), "name": _clean(c.get("productCodeName"))}
             for c in _gudid_list(device.get("productCodes"), "fdaProductCode")]
    submissions = [{"submission_number": _clean(s.get("submissionNumber")),
                    "supplement_number": _clean(s.get("supplementNumber"))}
                   for s in _gudid_list(device.get("premarketSubmissions"), "premarketSubmission")]
    version = _clean(device.get("publicVersionNumber"))
    fields = {
        "primary_di": di, "issuing_agency": _clean(primary[0].get("deviceIdIssuingAgency")),
        "brand_name": _clean(device.get("brandName")), "version_model_number": _clean(device.get("versionModelNumber")),
        "catalog_number": _clean(device.get("catalogNumber")), "company_name": _clean(device.get("companyName")),
        "labeler_duns": _clean(device.get("dunsNumber")), "device_description": _clean(device.get("deviceDescription")),
        "package_dis": packages, "secondary_dis": secondary, "product_codes": codes,
        "premarket_submissions": submissions,
        "gmdn_terms": [_clean(g.get("gmdnPTName")) for g in _gudid_list(device.get("gmdnTerms"), "gmdn")],
        "device_record_status": _clean(device.get("deviceRecordStatus")),
        "public_version_number": version, "public_version_date": iso(device.get("publicVersionDate")),
        "public_version_status": _clean(device.get("publicVersionStatus")),
        "publish_date": iso(device.get("devicePublishDate")),
        "commercial_distribution_status": _clean(device.get("deviceCommDistributionStatus")),
        "customer_contacts_excluded": len(_gudid_list(device.get("contacts"), "customerContact")),
    }
    links = [{"scheme": "fda-product-code", "value": c["code"]} for c in codes if c["code"]]
    for sub in submissions:
        number = sub["submission_number"] or ""
        scheme = ("fda-510k" if number.startswith("K") else "fda-pma" if number.startswith(("P", "N")) and
                  not number.startswith("NDA") else "fda-application" if number.startswith(("NDA", "BLA", "ANDA"))
                  else "fda-submission")
        if number:
            links.append({"scheme": scheme, "value": number})
    order = f"{int(version):08d}" if version and version.isdigit() else (version or "")
    return [_base("accessgudid", "device-identifier", key_di(di), di, locator, native_revision=version,
                  revision_order=order, as_of=fields["public_version_date"], fields=fields,
                  identifiers=_ids(("udi-di", di), ("duns", fields["labeler_duns"]))
                  + [{"scheme": "udi-di-package", "value": p["di"]} for p in packages if p["di"]],
                  links=links)]


# ----------------------------------------------------------------- EUDAMED


def _eudamed_version(item: Mapping[str, Any]) -> tuple[str | None, str]:
    version = _clean(item.get("versionNumber"))
    order = f"{int(version):08d}" if version and version.isdigit() else (version or "")
    return version, order


def parse_eudamed_actor(responses: Mapping[str, bytes], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    srn = unit["srn"]
    payload = _json(responses["actor"])
    locator = f"https://ec.europa.eu/tools/eudamed/#/screen/search-eo/{srn}"
    content = payload.get("content") if isinstance(payload, Mapping) else None
    if not content:
        return [_base("eudamed", "eudamed-actor", key_actor(srn), srn, locator, native_revision=None,
                      revision_order="", as_of=None, fields={"srn": srn}, state="not-published")]
    items = [c for c in content if isinstance(c, Mapping) and c.get("srn") == srn]
    if len(items) != 1:
        raise MedicalDeviceFormatError("schema_drift", "EUDAMED actor search does not return the declared SRN once")
    item = items[0]
    version, order = _eudamed_version(item)
    fields = {"srn": srn, "name": _clean(item.get("name")), "abbreviated_name": _clean(item.get("abbreviatedName")),
              "role": _clean(item.get("actorType") or item.get("role")), "country": _clean(item.get("countryIso2Code")),
              "status": _clean(item.get("actorStatus")), "city": _clean(item.get("city")),
              "version_number": version, "last_update_date": iso(item.get("lastUpdateDate")),
              "contact_persons_excluded": len(item.get("contactPersons") or []) + len(item.get("prrcs") or [])}
    return [_base("eudamed", "eudamed-actor", key_actor(srn), srn, locator, native_revision=version,
                  revision_order=order, as_of=fields["last_update_date"], fields=fields,
                  identifiers=_ids(("eudamed-srn", srn)))]


def parse_eudamed_device(responses: Mapping[str, bytes], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    basic = unit["basic_udi_di"]
    payload = _json(responses["device"])
    locator = f"https://ec.europa.eu/tools/eudamed/#/screen/search-device/{basic}"
    content = payload.get("content") if isinstance(payload, Mapping) else None
    if not content:
        return [_base("eudamed", "eudamed-device", key_basic_udi(basic), basic, locator, native_revision=None,
                      revision_order="", as_of=None, fields={"basic_udi_di": basic}, state="not-published")]
    items = [c for c in content if isinstance(c, Mapping) and c.get("basicUdi") == basic]
    if len(items) != 1:
        raise MedicalDeviceFormatError("schema_drift", "EUDAMED device search does not return the Basic UDI-DI once")
    item = items[0]
    version, order = _eudamed_version(item)
    udi_dis = [{"udi_di": _clean(u.get("primaryDi")), "status": _clean(u.get("deviceStatus")),
                "trade_name": _clean(u.get("tradeName"))} for u in item.get("udiDis") or []
               if isinstance(u, Mapping)]
    fields = {"basic_udi_di": basic, "manufacturer_srn": _clean(item.get("manufacturerSrn")),
              "authorised_representative_srn": _clean(item.get("authorisedRepresentativeSrn")),
              "device_name": _clean(item.get("deviceName")), "model": _clean(item.get("deviceModel")),
              "risk_class": _clean(item.get("riskClass")), "legislation": _clean(item.get("applicableLegislation")),
              "udi_dis": udi_dis, "certificate_numbers": [str(c) for c in item.get("certificateNumbers") or []],
              "version_number": version, "last_update_date": iso(item.get("lastUpdateDate"))}
    links = _ids(("eudamed-srn", fields["manufacturer_srn"])) + \
        [{"scheme": "udi-di", "value": u["udi_di"]} for u in udi_dis if u["udi_di"]]
    return [_base("eudamed", "eudamed-device", key_basic_udi(basic), basic, locator, native_revision=version,
                  revision_order=order, as_of=fields["last_update_date"], fields=fields,
                  identifiers=_ids(("basic-udi-di", basic)) + [{"scheme": "udi-di", "value": u["udi_di"]}
                                                                for u in udi_dis if u["udi_di"]], links=links)]


def parse_eudamed_certificate(responses: Mapping[str, bytes], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    number, nb = unit["certificate_number"], unit["notified_body"]
    payload = _json(responses["certificate"])
    locator = f"https://ec.europa.eu/tools/eudamed/#/screen/search-certificate/{nb}/{number}"
    content = payload.get("content") if isinstance(payload, Mapping) else None
    if not content:
        return [_base("eudamed", "eudamed-certificate", key_certificate(nb, number), f"{nb}:{number}", locator,
                      native_revision=None, revision_order="", as_of=None,
                      fields={"certificate_number": number, "notified_body_number": nb}, state="not-published")]
    items = [c for c in content if isinstance(c, Mapping) and c.get("certificateNumber") == number
             and str(c.get("notifiedBodyNumber")) == nb]
    if len(items) != 1:
        raise MedicalDeviceFormatError("schema_drift", "EUDAMED certificate search does not return it once")
    item = items[0]
    version, order = _eudamed_version(item)
    fields = {"certificate_number": number, "notified_body_number": nb,
              "notified_body_name": _clean(item.get("notifiedBodyName")),
              "certificate_type": _clean(item.get("certificateType")),
              "status_as_published": _clean(item.get("certificateStatus")),
              "issue_date": iso(item.get("issueDate")), "starting_validity_date": iso(item.get("startingValidityDate")),
              "expiry_date": iso(item.get("expiryDate")), "manufacturer_srn": _clean(item.get("manufacturerSrn")),
              "basic_udi_dis": [str(b) for b in item.get("basicUdiDis") or []],
              "status_change_reason": _clean(item.get("statusChangeReason")),
              "version_number": version, "last_update_date": iso(item.get("lastUpdateDate"))}
    links = _ids(("eudamed-srn", fields["manufacturer_srn"])) + \
        [{"scheme": "basic-udi-di", "value": b} for b in fields["basic_udi_dis"]]
    return [_base("eudamed", "eudamed-certificate", key_certificate(nb, number), f"{nb}:{number}", locator,
                  native_revision=version, revision_order=order, as_of=fields["last_update_date"], fields=fields,
                  identifiers=_ids(("eudamed-certificate", f"{nb}:{number}")), links=links)]


# ----------------------------------------------------------------- selection and requests


def _window_ok(unit: Mapping[str, Any]) -> bool:
    """A MAUDE received-date window: two YYYYMMDD dates, in order, at most one year (366 days) apart."""
    from datetime import date

    start, end = str(unit.get("received_from") or ""), str(unit.get("received_to") or "")
    if not (re.fullmatch(r"\d{8}", start) and re.fullmatch(r"\d{8}", end)):
        return False
    try:
        first = date(int(start[:4]), int(start[4:6]), int(start[6:]))
        last = date(int(end[:4]), int(end[4:6]), int(end[6:]))
    except ValueError:
        return False
    return 0 <= (last - first).days <= 366


def _units(fmt: str, selection: Mapping[str, Any]) -> list[dict[str, Any]]:
    key = FORMATS[fmt]["unit"]
    units = [dict(u) for u in selection.get(key) or [] if isinstance(u, Mapping)]
    if not 1 <= len(units) <= MAX_UNITS or len(units) != len(selection.get(key) or []):
        raise SourcePackError("invalid_manifest", f"a medical-devices selection names 1-{MAX_UNITS} {key} objects")
    checks = {
        "openfda-510k-json": lambda u: _K.fullmatch(str(u.get("k_number") or "")),
        "openfda-pma-json": lambda u: _P.fullmatch(str(u.get("pma_number") or "")),
        "openfda-classification-json": lambda u: _CODE.fullmatch(str(u.get("product_code") or "")),
        "openfda-recall-json": lambda u: _RECALL.fullmatch(str(u.get("recall_number") or "")),
        "openfda-event-json": lambda u: _CODE.fullmatch(str(u.get("product_code") or "")) and _window_ok(u),
        "gudid-device-json": lambda u: _DI.fullmatch(str(u.get("di") or "")),
        "eudamed-actor-json": lambda u: _SRN.fullmatch(str(u.get("srn") or "")),
        "eudamed-device-json": lambda u: _DI.fullmatch(str(u.get("basic_udi_di") or "")),
        "eudamed-certificate-json": lambda u: _NB.fullmatch(str(u.get("notified_body") or ""))
        and _clean(u.get("certificate_number")),
    }
    for unit in units:
        if not checks[fmt](unit):
            raise SourcePackError("invalid_manifest", f"invalid {key} unit for {fmt}: {json.dumps(unit)}")
    return units


def _search(field: str, value: str) -> str:
    return f'{field}:"{value}"'


def requests_for(fmt: str, unit: Mapping[str, Any]) -> dict[str, tuple[str, dict[str, Any]]]:
    """Named request paths (relative to the endpoint) and parameters for one selection unit."""
    if fmt == "openfda-510k-json":
        return {"clearance": ("/device/510k.json", {"search": _search("k_number", unit["k_number"]), "limit": 1})}
    if fmt == "openfda-pma-json":
        return {"approvals": ("/device/pma.json", {"search": _search("pma_number", unit["pma_number"]),
                                                   "limit": MAX_RESULTS})}
    if fmt == "openfda-classification-json":
        return {"classification": ("/device/classification.json",
                                   {"search": _search("product_code", unit["product_code"]), "limit": 1})}
    if fmt == "openfda-recall-json":
        return {"recall": ("/device/recall.json", {"search": _search("product_res_number", unit["recall_number"]),
                                                   "limit": 1}),
                "enforcement": ("/device/enforcement.json", {"search": _search("recall_number", unit["recall_number"]),
                                                             "limit": 1})}
    if fmt == "openfda-event-json":
        search = (f'device.device_report_product_code:"{unit["product_code"]}" AND '
                  f'date_received:[{unit["received_from"]} TO {unit["received_to"]}]')
        return {"reports": ("/device/event.json", {"search": search, "limit": MAX_RESULTS}),
                "counts": ("/device/event.json", {"search": search, "count": "event_type.exact"})}
    if fmt == "gudid-device-json":
        return {"device": ("/api/v3/devices/lookup.json", {"di": unit["di"]})}
    if fmt == "eudamed-actor-json":
        return {"actor": ("/api/actors", {"srn": unit["srn"]})}
    if fmt == "eudamed-device-json":
        return {"device": ("/api/devices/basicUdiData", {"basicUdi": unit["basic_udi_di"]})}
    if fmt == "eudamed-certificate-json":
        return {"certificate": ("/api/certificates", {"certificateNumber": unit["certificate_number"],
                                                      "notifiedBody": unit["notified_body"]})}
    raise SourcePackError("invalid_manifest", f"unknown medical-devices format {fmt!r}")


_PARSERS: dict[str, Callable[[Mapping[str, bytes], Mapping[str, Any]], list[dict[str, Any]]]] = {
    "openfda-510k-json": parse_510k,
    "openfda-pma-json": parse_pma,
    "openfda-classification-json": parse_classification,
    "openfda-recall-json": parse_recall,
    "openfda-event-json": parse_events,
    "gudid-device-json": parse_gudid,
    "eudamed-actor-json": parse_eudamed_actor,
    "eudamed-device-json": parse_eudamed_device,
    "eudamed-certificate-json": parse_eudamed_certificate,
}


def parse_unit(fmt: str, responses: Mapping[str, bytes], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    if fmt not in _PARSERS:
        raise MedicalDeviceFormatError("schema_drift", f"unknown medical-devices format {fmt!r}")
    records = _PARSERS[fmt](responses, unit)
    from src.kb.medical_devices_records import MedicalDeviceError, validate

    try:
        return [validate(r) for r in records]
    except MedicalDeviceError as exc:
        raise MedicalDeviceFormatError("schema_drift" if exc.code != "minimisation_violation" else exc.code,
                                       str(exc)) from exc


def medical_devices_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    declared = dict(source.get("medical_devices") or {})
    fmt = declared.get("format")
    if fmt not in FORMATS or FORMATS[fmt]["provider"] != declared.get("provider"):
        raise SourcePackError("invalid_manifest", "medical-devices sources declare a matching provider and format")
    if declared.get("live_verification") not in {"unverified-live", "verified-live"}:
        raise SourcePackError("invalid_manifest", "medical-devices sources state their LIVE_VERIFICATION status")
    if declared.get("minimisation") != MINIMISATION_ID:
        raise SourcePackError("invalid_manifest", f"medical-devices sources declare the {MINIMISATION_ID} policy")
    _units(fmt, dict(declared.get("selection") or {}))
    if dict(source.get("auth") or {}).get("kind") != "none":
        raise SourcePackError("invalid_manifest", "medical-devices sources are unauthenticated")
    return declared


class MedicalDevicesAdapter:
    """Fetch one declared selection unit per page from the source's endpoint host and emit its records."""

    accepts_transport = True
    connector = CONNECTOR

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None) -> None:
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        del secret  # every medical-devices source is unauthenticated
        self.source = json.loads(json.dumps(source))
        self.declared = medical_devices_declaration(self.source)
        self.format = self.declared["format"]
        self.provider = self.declared["provider"]
        self.units = _units(self.format, dict(self.declared.get("selection") or {}))
        if transport is None:
            from functools import partial

            transport = partial(HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"]))
        self.transport = transport
        self.definition = {
            "contract": ADAPTER_CONTRACT, "source_id": source["source_id"], "connector": source["connector"],
            "endpoint": source["endpoint"], "operations": list(source["operations"]),
            "source_hash": source["source_hash"], "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"], "limits": source["budgets"],
            "medical_devices": {"provider": self.provider, "format": self.format, "units": len(self.units)},
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

    def _get(self, path: str, params: Mapping[str, Any]) -> tuple[bytes, dict[str, Any]]:
        from src.ingestion.source_pack_runtime import _retry_after_ms

        endpoint = self.source["endpoint"].rstrip("/")
        url = endpoint + path
        host = (urlsplit(endpoint).hostname or "").casefold()
        ordered = dict(sorted(params.items()))
        response = self.transport(url=url, params=ordered, headers={"Accept": "application/json"},
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
        if status == 429:
            raise SourcePackError("rate_limited", "provider quota is temporarily exhausted",
                                  retry_after_ms=_retry_after_ms(headers_in.get("retry-after")))
        if status in {401, 403}:
            raise SourcePackError("authentication_failed", f"request refused (HTTP {status})")
        if status >= 500:
            raise SourcePackError("source_unavailable", f"provider returned HTTP {status}")
        if status >= 400 and status != 404:
            raise SourcePackError("schema_drift", f"request returned HTTP {status}")
        query = urlencode(ordered)
        return raw, {"path": path + ("?" + query if query else ""), "status": status,
                     "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
                     "origin": "fixture" if response.get("origin") == "fixture" else "live"}

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage

        self._check(request)
        index = 0 if cursor is None else int(cursor)
        if not 0 <= index < len(self.units):
            raise SourcePackError("cursor_drift", "cursor is outside the declared selection")
        unit = self.units[index]
        responses, requests = {}, []
        for name, (path, params) in requests_for(self.format, unit).items():
            raw, receipt = self._get(path, params)
            responses[name] = raw
            requests.append({"name": name, **receipt})
        try:
            records = parse_unit(self.format, responses, unit)
        except MedicalDeviceFormatError as exc:
            code = {"input_limit": "budget_exhausted", "minimisation_violation": "mapping_failed"}.get(
                exc.code, "schema_drift")
            raise SourcePackError(code, f"{exc.code}: {exc}") from exc
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        if len(records) > limit:
            raise SourcePackError("budget_exhausted", "unit has more records than the run's result budget")
        origin = "fixture" if all(r["origin"] == "fixture" for r in requests) else "live"
        receipt = {
            "contract": RECEIPT_CONTRACT, "source_id": self.source["source_id"], "provider": self.provider,
            "format": self.format, "unit_index": index, "unit": unit, "requests": requests,
            "records": len(records), "evidence_origin": origin, "minimisation": MINIMISATION_ID,
            "live_verification": self.declared["live_verification"], "final_page": index + 1 >= len(self.units),
        }
        out = []
        for record in records:
            record = {**record, "evidence_origin": origin}
            out.append({"id": record["record_key"], "title": _item_title(record), "url": record["locator"],
                        "language": "en", "published_at": _item_date(record), "updated_at": record["native_revision"],
                        "content": json.dumps(record, sort_keys=True, ensure_ascii=False),
                        "medical_device_record": record, "medical_device_receipt": receipt})
        next_cursor = None if receipt["final_page"] else str(index + 1)
        return RuntimePage(tuple(out), next_cursor, sum(r["bytes"] for r in requests), receipt=receipt)


def _item_title(record: Mapping[str, Any]) -> str:
    fields = record.get("fields") or {}
    for field in ("device_name", "trade_name", "brand_name", "name", "product_description", "event_type",
                  "certificate_type"):
        if fields.get(field):
            return str(fields[field])[:300]
    return record["record_key"]


def _item_date(record: Mapping[str, Any]) -> str | None:
    fields = record.get("fields") or {}
    for field in ("decision_date", "event_date_initiated", "date_received", "public_version_date", "issue_date",
                  "last_update_date"):
        if fields.get(field) and _DATE.fullmatch(str(fields[field])):
            return fields[field]
    return None


FIXTURE_SECRET = None
ADAPTERS = {CONNECTOR: MedicalDevicesAdapter}


def fixture_transport(pages: Sequence[Mapping[str, Any]]) -> Callable[..., Mapping[str, Any]]:
    """Replay authored responses keyed by URL path (+ sorted query); responses are marked as fixture evidence."""
    by_key = {page["request"]: page for page in pages}

    def transport(*, url, params, headers, timeout):
        del headers, timeout
        parts = urlsplit(url)
        query = urlencode(sorted(dict(params or {}).items()))
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


def fixture_request_key(source: Mapping[str, Any], path: str, params: Mapping[str, Any]) -> str:
    """The key :func:`fixture_transport` looks a request up by (endpoint path prefix + path + sorted query)."""
    prefix = urlsplit(source["endpoint"]).path.rstrip("/")
    query = urlencode(sorted(dict(params).items()))
    return prefix + path + ("?" + query if query else "")


def replay_native_fixture(source: Mapping[str, Any], fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    adapter = MedicalDevicesAdapter(source, transport=fixture_transport(list(fixture["native_pages"])))
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
    "ADAPTERS", "BOUNDED_COVERAGE", "CONNECTOR", "COUNT_SEMANTICS", "DECLINED", "EUDAMED_MODULES", "FEATURES",
    "FIXTURE_SECRET", "FORMATS", "IDENTIFIERS", "LIVE_VERIFICATION", "MAUDE_CAVEATS", "MINIMISATION",
    "PROVIDER_CONTRACTS", "RECORD_CONTRACT", "REVIEW_BOUNDARY", "MedicalDeviceFormatError", "MedicalDevicesAdapter",
    "fixture_request_key", "fixture_transport", "medical_devices_declaration", "parse_unit", "replay_native_fixture",
    "requests_for",
]
