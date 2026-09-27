"""Bounded acquisition of clinical trial, regulatory and review-registry records.

Five sources are acquired as native connectors of the ``clinical-evidence``
source pack (``config/source_packs/clinical-evidence.json``), so every run goes
through the shared source-pack runtime: explicit budgets, cursors bound to the
selection, per-page receipts with response hashes, license acceptance,
schedules and the maintenance orchestrator. Nothing here schedules anything.

* ``ctgov`` – ClinicalTrials.gov API v2 (``/api/v2/studies``) for current
  records and results sections; version history via the site's
  ``/api/int/studies/{NCT}/history`` endpoint, which is *not* part of the
  documented v2 contract and is recorded as such.
* ``ctis`` – EU CTIS public portal JSON (``ctis-public-api/retrieve``): the
  endpoint the public portal uses, not a versioned public API.
* ``eu-ctr`` – EU Clinical Trials Register full-text protocol download
  (``ctr-search/rest/download/full``); results are recorded as *available*
  with their URL, their content is not acquired.
* ``openfda`` – openFDA drug label, Drugs@FDA and FAERS count endpoints, with
  optional API key (``NOESIS_OPENFDA_API_KEY``), openFDA's disclaimer kept on
  every record and FAERS figures kept as reporting counts.
* ``ema-medicines`` – EMA's published medicines data export (JSON).

PROSPERO has no documented machine interface that this module could rely on;
its registrations enter only as user-supplied record exports
(:func:`parse_prospero_export`). WHO ICTRP and Cochrane are recorded as not
implemented with their reasons in :data:`PROVIDER_CONTRACTS`.

Parsers are fail-closed and never invent values: a field that is not in the
payload stays unknown, every extracted field keeps a JSON pointer or line
locator, and label sections about dosing are deliberately not retained (the
pack produces no dosing or treatment recommendation).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

from src.ingestion.source_packs import SourcePackError
from src.kb.clinical_records import (
    CONTRACT,
    COUNT_SEMANTICS,
    ClinicalRecordError,
    normalize_text,
    prospective,
    record,
)

ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
EXTRACTOR = "clinical-sources:1.0.0"
FIXTURE_SECRET = None
MAX_ITEMS = 50
MAX_VERSIONS = 40
MAX_TEXT = 4000

PROVIDER_HOSTS = {
    "ctgov": {"clinicaltrials.gov"},
    "ctis": {"euclinicaltrials.eu"},
    "euctr": {"www.clinicaltrialsregister.eu"},
    "openfda": {"api.fda.gov"},
    "ema": {"www.ema.europa.eu"},
}
_NOT_ADVICE = "Records describe registrations, results, regulatory status and reports; they are not medical advice."
PROVIDER_CONTRACTS = {
    "ctgov": {
        "documentation": "https://clinicaltrials.gov/data-api/api",
        "access": "REST API v2 (JSON): /api/v2/studies/{NCT} and /api/v2/studies?query.cond&query.intr; "
                  "version history via /api/int/studies/{NCT}/history[/{version}] (site-internal, not in the "
                  "documented v2 contract)",
        "authentication": "none",
        "rate_limits": "documented guidance of roughly 50 requests per minute per IP; runs are bounded far below",
        "pagination": "pageToken/nextPageToken with pageSize (search); one pinned study per page (selection)",
        "cadence": "daily at most; records change on each posted registry version",
        "terms": "US National Library of Medicine terms for ClinicalTrials.gov data; public domain US government "
                 "data, cite ClinicalTrials.gov and the NCT number",
        "retained_evidence": "raw JSON response per request (sha256 in the page receipt), JSON pointers per field, "
                             "registry version number and date per trial revision",
        "identifiers": ["NCT", "secondary ids: EudraCT, CTIS (EU CT), other registries, sponsor protocol ids",
                        "PMID for registry-declared references"],
        "cross_references": "secondaryIdInfos (EUDRACT_NUMBER, CTIS, REGISTRY) declare EU identifiers; "
                            "referencesModule declares PMIDs with type RESULT/BACKGROUND/DERIVED",
        "status": "implemented",
        "unavailable_fallback": "record the provider failure; keep the last revision and mark the source stale",
    },
    "ctis": {
        "documentation": "https://euclinicaltrials.eu/",
        "access": "public portal JSON used by the CTIS search page: ctis-public-api/retrieve/{EU CT number}; "
                  "not a versioned public API",
        "authentication": "none",
        "rate_limits": "not documented; bounded to at most 50 pinned trials per run",
        "pagination": "one pinned trial per page",
        "cadence": "weekly at most",
        "terms": "EMA CTIS public website; information published under Regulation (EU) No 536/2014 transparency "
                 "rules; cite the EU CT number and portal URL",
        "retained_evidence": "raw JSON per trial, JSON pointers, member-state decisions and dates",
        "identifiers": ["EU CT number", "secondary: NCT, ISRCTN, EudraCT (transitioned trials)"],
        "cross_references": "secondaryIdentifyingNumbers declare NCT and other registry numbers",
        "status": "implemented",
        "unavailable_fallback": "record the provider failure; the portal shape is undocumented, so a changed shape "
                                "fails closed as schema_drift",
    },
    "euctr": {
        "documentation": "https://www.clinicaltrialsregister.eu/",
        "access": "public full-text protocol download: ctr-search/rest/download/full?query={EudraCT}&mode=current_page",
        "authentication": "none",
        "rate_limits": "not documented; bounded to at most 50 pinned trials per run",
        "pagination": "one pinned EudraCT number per page; each page may hold several member-state protocols",
        "cadence": "monthly at most (legacy register; new trials are in CTIS)",
        "terms": "EMA EU Clinical Trials Register; public information under Directive 2001/20/EC transparency",
        "retained_evidence": "raw text per trial, line locators with section codes (A.2, E.8.1.1, ...)",
        "identifiers": ["EudraCT", "secondary: NCT (A.5.2), ISRCTN (A.5.1), WHO UTN (A.5.3), sponsor protocol"],
        "cross_references": "A.5.2 declares the ClinicalTrials.gov number",
        "results": "the register's result summaries are recorded as available (URL), content not acquired",
        "status": "implemented",
        "unavailable_fallback": "record the provider failure; keep last revision marked stale",
    },
    "openfda": {
        "documentation": "https://open.fda.gov/apis/",
        "access": "REST JSON: /drug/label.json, /drug/drugsfda.json, /drug/event.json (count queries)",
        "authentication": "optional API key (api_key query parameter) from NOESIS_OPENFDA_API_KEY; never stored",
        "rate_limits": "240 requests/minute; 1,000 requests/day per IP without a key, 120,000/day with a key",
        "pagination": "limit/skip; count queries return the top terms only (bounded by limit)",
        "cadence": "weekly at most; labels and FAERS are refreshed by FDA on their own schedule (meta.last_updated)",
        "terms": "openFDA Terms of Service; results carry openFDA's disclaimer, which is stored on every record",
        "retained_evidence": "raw JSON per request, meta.disclaimer/terms/license/last_updated, JSON pointers",
        "identifiers": ["SPL set_id and label version", "application numbers (NDA/ANDA/BLA)", "generic/brand names"],
        "cross_references": "openfda.application_number joins labels to Drugs@FDA; no trial registry identifiers",
        "count_semantics": COUNT_SEMANTICS,
        "status": "implemented",
        "unavailable_fallback": "record the provider failure; keep last revision marked stale",
    },
    "ema": {
        "documentation": "https://www.ema.europa.eu/en/medicines/download-medicine-data",
        "access": "published medicines data export (JSON file on the download-medicine-data page); rows are "
                  "filtered to pinned EMA product numbers",
        "authentication": "none",
        "rate_limits": "static file; one request per run",
        "pagination": "none (single export file)",
        "cadence": "daily at most (EMA regenerates the export)",
        "terms": "EMA website terms (reuse with acknowledgement of the source)",
        "retained_evidence": "raw export bytes (sha256), row pointer per record, EPAR URL",
        "identifiers": ["EMA product number (EMEA/H/C/......)", "INN / active substance",
                        "therapeutic area as MeSH-labelled text"],
        "cross_references": "no trial identifiers; joins to trials only through intervention terms",
        "safety_communications": "not acquired (separate EMA publications); the record kind exists",
        "status": "implemented",
        "unavailable_fallback": "record the provider failure; keep last revision marked stale",
    },
    "prospero": {
        "documentation": "https://www.crd.york.ac.uk/prospero/",
        "access": "no documented public API or bulk export for machine access was identified; automated retrieval "
                  "of record pages is not a supported means",
        "authentication": "n/a",
        "status": "not-implemented",
        "reason": "only supported means are used; PROSPERO registrations enter as user-supplied record exports "
                  "(import_prospero_registration) linked to the systematic-review store",
        "identifiers": ["CRD number"],
        "cross_references": "reviews cite trials in text; no structured trial identifiers are assumed",
        "unavailable_fallback": "import a record export supplied by the user; otherwise the registration stays unknown",
    },
    "who-ictrp": {
        "documentation": "https://www.who.int/tools/clinical-trials-registry-platform",
        "access": "machine access (web service, full export) is offered by WHO under agreement; no open API",
        "status": "not-implemented",
        "reason": "no supported open machine access; records from primary registries (CT.gov, CTIS, EU CTR) are "
                  "used directly instead",
        "identifiers": ["primary registry ids", "WHO UTN"],
    },
    "cochrane": {
        "documentation": "https://www.cochranelibrary.com/",
        "status": "not-implemented",
        "reason": "licensed content (Cochrane Library / CENTRAL); no licence is configured",
    },
    "europe-pmc": {
        "documentation": "https://europepmc.org/RestfulWebService",
        "access": "existing Science source (primary-scientific-evidence pack, europe-pmc source)",
        "status": "reused",
        "identifiers": ["PMID", "PMCID", "DOI", "accession annotations (Annotations API, type Accession Numbers)"],
        "cross_references": "text-mined accession numbers (nct, eudract) via the Annotations API",
    },
    "pubmed": {
        "documentation": "https://www.ncbi.nlm.nih.gov/books/NBK25501/",
        "access": "existing Science connector (esearch/esummary); secondary-source ids from efetch DataBankList",
        "status": "reused",
        "rate_limits": "3 requests/second without NCBI_API_KEY, 10 with a key",
        "identifiers": ["PMID", "DOI", "MEDLINE secondary source ids (ClinicalTrials.gov, EudraCT, ISRCTN)"],
        "cross_references": "DataBankList/AccessionNumberList declares registry numbers for the article",
    },
    "medrxiv-biorxiv": {
        "documentation": "https://api.biorxiv.org/",
        "access": "existing Science connectors (medrxiv, biorxiv)",
        "status": "reused",
        "identifiers": ["DOI", "published DOI of the journal version"],
        "cross_references": "preprint-to-paper families via PaperFamilyStore",
    },
}
LIVE_VERIFICATION = {
    provider: ({"status": "unverified-live",
                "note": "no successful live run from a permitted network; see scripts/clinical_live_check.py"}
               if contract["status"] == "implemented" else
               {"status": contract["status"], "note": contract.get("reason") or contract.get("access", "")})
    for provider, contract in PROVIDER_CONTRACTS.items()
}
# Dated bounded live check (H14). The build environment's egress proxy rejected
# every provider host, so nothing is live-verified; the recorded report keeps
# the exact failure codes.
LIVE_CHECK = {
    "date": "2026-09-27",
    "report": "docs/development/clinical-evidence/live-check-2026-09-27.json",
    "result": "blocked: the build environment's network policy rejected every provider host",
    "failure_code": "source_unavailable",
    "cause": "egress proxy refused CONNECT (Tunnel connection failed: 403 Forbidden)",
}
for _provider in ("ctgov", "ctis", "euctr", "openfda", "ema"):
    LIVE_VERIFICATION[_provider] = {**LIVE_VERIFICATION[_provider], "last_check": LIVE_CHECK}

# --------------------------------------------------------------- identifiers

_ID_PATTERNS = [
    ("nct", re.compile(r"\bNCT\d{8}\b")),
    ("eu-ct", re.compile(r"\b\d{4}-\d{6}-\d{2}-\d{2}\b")),
    ("eudract", re.compile(r"\b\d{4}-\d{6}-\d{2}\b(?!-\d)")),
    ("isrctn", re.compile(r"\bISRCTN\d{8}\b")),
    ("prospero", re.compile(r"\bCRD\d{11}\b")),
]


def find_identifiers(text):
    """Registry identifiers mentioned in free text (for candidates, never accepted links)."""
    found = []
    for kind, pattern in _ID_PATTERNS:
        for match in pattern.finditer(str(text or "")):
            if (kind, match.group(0)) not in found:
                found.append((kind, match.group(0)))
    return found


def classify_identifier(value, declared_type=None):
    """Kind of a declared identifier from its declared type, else its shape."""
    declared = str(declared_type or "").upper()
    value = str(value or "").strip()
    if declared in {"EUDRACT_NUMBER", "EUDRACT"} and re.fullmatch(r"\d{4}-\d{6}-\d{2}", value):
        return "eudract"
    if declared in {"CTIS", "EU_CT", "EU CT"} and re.fullmatch(r"\d{4}-\d{6}-\d{2}-\d{2}", value):
        return "eu-ct"
    for kind, pattern in _ID_PATTERNS:
        if re.fullmatch(pattern.pattern.replace(r"\b", "").replace("(?!-\\d)", ""), value):
            return kind
    if re.fullmatch(r"U\d{4}-\d{4}-\d{4}", value):
        return "who-utn"
    return "other"


# -------------------------------------------------------------- helpers


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _digest(value: Any) -> str:
    return _sha(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())


def _clip(value, limit=MAX_TEXT):
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text[:limit] if text else None


def _date(value):
    """ISO date or month from registry date strings; anything else stays unknown."""
    if not value:
        return None
    text = str(value).strip()
    if re.fullmatch(r"\d{8}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    match = re.match(r"^(\d{4}-\d{2}(?:-\d{2})?)", text)
    return match.group(1) if match else None


def _get(value, *path, default=None):
    for key in path:
        if isinstance(value, Mapping):
            value = value.get(key)
        elif isinstance(value, list) and isinstance(key, int) and 0 <= key < len(value):
            value = value[key]
        else:
            return default
        if value is None:
            return default
    return value


def _json(raw: bytes, provider: str) -> Any:
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise SourcePackError("schema_drift", f"{provider} returned non-JSON content") from exc


# ------------------------------------------------------ ClinicalTrials.gov

CTGOV_STATUS = {
    "NOT_YET_RECRUITING": "not-yet-recruiting", "RECRUITING": "recruiting",
    "ENROLLING_BY_INVITATION": "enrolling-by-invitation", "ACTIVE_NOT_RECRUITING": "active-not-recruiting",
    "SUSPENDED": "suspended", "TERMINATED": "terminated", "COMPLETED": "completed", "WITHDRAWN": "withdrawn",
}
CTGOV_PHASE = {"EARLY_PHASE1": "early-phase-1", "PHASE1": "phase-1", "PHASE2": "phase-2", "PHASE3": "phase-3",
               "PHASE4": "phase-4", "NA": "not-applicable"}
CTGOV_ALLOCATION = {"RANDOMIZED": "randomized", "NON_RANDOMIZED": "non-randomized", "NA": "not-applicable"}
CTGOV_MASKING = {"NONE": "none", "SINGLE": "single", "DOUBLE": "double", "TRIPLE": "triple",
                 "QUADRUPLE": "quadruple"}
_P = "/protocolSection"


def ctgov_study_url(nct):
    return f"https://clinicaltrials.gov/study/{nct}"


def _ctgov_protocol(study, *, pointer_root=""):
    protocol = study.get("protocolSection") if isinstance(study, Mapping) else None
    nct = _get(protocol, "identificationModule", "nctId")
    if not isinstance(protocol, Mapping) or not isinstance(nct, str) or not re.fullmatch(r"NCT\d{8}", nct):
        raise SourcePackError("schema_drift", "ClinicalTrials.gov study lacks protocolSection.identificationModule.nctId")
    return protocol, nct


def _ctgov_trial(study, nct, native_version, *, pointer_root=""):
    """Project one protocol section (current record or a registry version) into H02 records."""
    protocol, found = _ctgov_protocol(study)
    if found != nct:
        raise SourcePackError("schema_drift", "ClinicalTrials.gov returned another study")
    root = pointer_root + _P
    ident, status = protocol.get("identificationModule") or {}, protocol.get("statusModule") or {}
    design = protocol.get("designModule") or {}
    info = design.get("designInfo") or {}
    enrollment = design.get("enrollmentInfo") or {}
    count = enrollment.get("count")
    count = count if type(count) is int and count >= 0 else None
    etype = str(enrollment.get("type") or "").upper()
    secondary = []
    for index, item in enumerate(ident.get("secondaryIdInfos") or []):
        value = str(item.get("id") or "").strip()
        if not value:
            continue
        kind = classify_identifier(value, item.get("type"))
        secondary.append({"kind": kind, "value": value, "declared_by": "ctgov:" + str(item.get("type") or "OTHER"),
                          "domain": item.get("domain"),
                          "locator": {"json_pointer": f"{root}/identificationModule/secondaryIdInfos/{index}"}})
    org_id = _get(ident, "orgStudyIdInfo", "id")
    if org_id:
        secondary.append({"kind": "sponsor-protocol", "value": str(org_id), "declared_by": "ctgov:orgStudyId",
                          "domain": None, "locator": {"json_pointer": f"{root}/identificationModule/orgStudyIdInfo/id"}})
    first_submitted = _date(status.get("studyFirstSubmitDate"))
    start = _date(_get(status, "startDateStruct", "date"))
    arms = protocol.get("armsInterventionsModule") or {}
    outcomes_module = protocol.get("outcomesModule") or {}
    arm_records, outcome_records = [], []
    url = ctgov_study_url(nct)
    for index, arm in enumerate(arms.get("armGroups") or []):
        label = _clip(arm.get("label"), 2000)
        if not label:
            continue
        arm_records.append(record(
            "trial-arm", registry="ctgov", identifier=nct, arm_key=normalize_text(label), label=label,
            arm_type=arm.get("type"), description=_clip(arm.get("description")),
            interventions=[str(n) for n in arm.get("interventionNames") or []][:50],
            source_url=url, native_version=native_version, present=True))
    for role, key in (("primary", "primaryOutcomes"), ("secondary", "secondaryOutcomes"), ("other", "otherOutcomes")):
        for index, outcome in enumerate(outcomes_module.get(key) or []):
            measure = _clip(outcome.get("measure"))
            if not measure:
                continue
            outcome_records.append(record(
                "outcome-measure", registry="ctgov", identifier=nct, outcome_key=normalize_text(measure),
                measure=measure, role=role, time_frame=_clip(outcome.get("timeFrame"), 2000),
                description=_clip(outcome.get("description")), source_url=url, native_version=native_version,
                present=True, locator={"json_pointer": f"{root}/outcomesModule/{key}/{index}"}))
    references = []
    for index, ref in enumerate(_get(protocol, "referencesModule", "references", default=[]) or []):
        pmid = str(ref.get("pmid") or "").strip()
        if pmid and re.fullmatch(r"\d{1,9}", pmid):
            references.append({"pmid": pmid, "doi": None, "type": ref.get("type"),
                               "citation": _clip(ref.get("citation"), 2000),
                               "locator": {"json_pointer": f"{root}/referencesModule/references/{index}"}})
    trial = record(
        "registered-trial", registry="ctgov", identifier=nct,
        title=_clip(ident.get("officialTitle") or ident.get("briefTitle"), 4000), source_url=url,
        native_version=native_version,
        status={"native": status.get("overallStatus"), "normalized": CTGOV_STATUS.get(status.get("overallStatus"),
                                                                                     "unknown"),
                "as_of": _date(status.get("statusVerifiedDate")),
                "locator": {"json_pointer": f"{root}/statusModule/overallStatus"}},
        phase={"native": design.get("phases"),
               "normalized": [CTGOV_PHASE.get(p, "unknown") for p in design.get("phases") or []] or ["unknown"],
               "locator": {"json_pointer": f"{root}/designModule/phases"}},
        sponsor={"lead": _get(protocol, "sponsorCollaboratorsModule", "leadSponsor", "name"),
                 "class": _get(protocol, "sponsorCollaboratorsModule", "leadSponsor", "class")},
        secondary_identifiers=secondary,
        conditions=[{"term": str(c), "locator": {"json_pointer": f"{root}/conditionsModule/conditions/{i}"}}
                    for i, c in enumerate(_get(protocol, "conditionsModule", "conditions", default=[]) or [])],
        interventions=[{"type": i.get("type"), "name": i.get("name"), "other_names": i.get("otherNames") or [],
                        "locator": {"json_pointer": f"{root}/armsInterventionsModule/interventions/{n}"}}
                       for n, i in enumerate(arms.get("interventions") or []) if i.get("name")],
        design={"study_type": design.get("studyType"),
                "allocation": CTGOV_ALLOCATION.get(info.get("allocation"), "unknown"),
                "masking": CTGOV_MASKING.get(_get(info, "maskingInfo", "masking"), "unknown"),
                "masked_roles": _get(info, "maskingInfo", "whoMasked", default=[]) or [],
                "intervention_model": info.get("interventionModel"), "primary_purpose": info.get("primaryPurpose"),
                "enrollment_planned": count if etype == "ESTIMATED" else None,
                "enrollment_actual": count if etype == "ACTUAL" else None,
                "locator": {"json_pointer": f"{root}/designModule"}},
        registration={"first_submitted": first_submitted,
                      "first_posted": _date(_get(status, "studyFirstPostDateStruct", "date")),
                      "start_date": start,
                      "primary_completion_date": _date(_get(status, "primaryCompletionDateStruct", "date")),
                      "completion_date": _date(_get(status, "completionDateStruct", "date")),
                      "results_first_posted": _date(_get(status, "resultsFirstPostDateStruct", "date")),
                      "last_update_posted": _date(_get(status, "lastUpdatePostDateStruct", "date")),
                      "prospective": prospective(first_submitted, start),
                      "locator": {"json_pointer": f"{root}/statusModule"}},
        declared_references=references,
        results_indicator={"has_results": study.get("hasResults") if isinstance(study.get("hasResults"), bool)
                           else None},
        arm_keys=sorted(r["arm_key"] for r in arm_records),
        outcome_keys=sorted(r["outcome_key"] for r in outcome_records),
        brief_summary=_clip(_get(protocol, "descriptionModule", "briefSummary")),
    )
    return trial, arm_records, outcome_records


def _ctgov_results(study, nct, native_version):
    results = study.get("resultsSection")
    if not isinstance(results, Mapping):
        return None
    status = _get(study, "protocolSection", "statusModule", default={}) or {}
    measures = []
    for index, measure in enumerate(_get(results, "outcomeMeasuresModule", "outcomeMeasures", default=[]) or []):
        values = []
        for klass in measure.get("classes") or []:
            for category in klass.get("categories") or []:
                for item in category.get("measurements") or []:
                    values.append({"group_id": item.get("groupId"), "value": item.get("value"),
                                   "spread": item.get("spread"), "lower": item.get("lowerLimit"),
                                   "upper": item.get("upperLimit"), "class": klass.get("title"),
                                   "category": category.get("title")})
        measures.append({
            "title": _clip(measure.get("title")), "role": str(measure.get("type") or "").lower() or None,
            "time_frame": _clip(measure.get("timeFrame"), 2000), "param_type": measure.get("paramType"),
            "unit": measure.get("unitOfMeasure"),
            "groups": [{"id": g.get("id"), "title": g.get("title")} for g in measure.get("groups") or []],
            "measurements": values[:200],
            "analyses": [{"p_value": a.get("pValue"), "statistical_method": a.get("statisticalMethod"),
                          "param_type": a.get("paramType"), "param_value": a.get("paramValue"),
                          "ci_pct": a.get("ciPctValue"), "ci_lower": a.get("ciLowerLimit"),
                          "ci_upper": a.get("ciUpperLimit"), "group_ids": a.get("groupIds") or []}
                         for a in measure.get("analyses") or []][:50],
            "locator": {"json_pointer": f"/resultsSection/outcomeMeasuresModule/outcomeMeasures/{index}"},
        })
    ae = results.get("adverseEventsModule") or {}
    adverse = None
    if ae:
        adverse = {"frequency_threshold": ae.get("frequencyThreshold"), "time_frame": _clip(ae.get("timeFrame"), 2000),
                   "groups": [{"id": g.get("id"), "title": g.get("title"),
                               "serious_affected": g.get("seriousNumAffected"),
                               "serious_at_risk": g.get("seriousNumAtRisk"),
                               "other_affected": g.get("otherNumAffected"), "other_at_risk": g.get("otherNumAtRisk")}
                              for g in ae.get("eventGroups") or []],
                   "reported_as": "trial-reported adverse events per arm, as posted",
                   "locator": {"json_pointer": "/resultsSection/adverseEventsModule"}}
    return record(
        "result-posting", registry="ctgov", identifier=nct, source_url=ctgov_study_url(nct) + "?tab=results",
        native_version=native_version,
        posted={"first_posted": _date(_get(status, "resultsFirstPostDateStruct", "date")),
                "first_submitted": _date(status.get("resultsFirstSubmitDate")),
                "last_update": _date(_get(status, "lastUpdatePostDateStruct", "date")),
                "locator": {"json_pointer": "/protocolSection/statusModule/resultsFirstPostDateStruct"}},
        outcome_results=measures, adverse_events=adverse, content_acquired=True,
        locator={"json_pointer": "/resultsSection"})


def ctgov_derived_terms(study):
    derived = study.get("derivedSection") if isinstance(study, Mapping) else None
    if not isinstance(derived, Mapping):
        return None
    browse = {}
    for key, name in (("conditionBrowseModule", "conditions"), ("interventionBrowseModule", "interventions")):
        module = derived.get(key) or {}
        browse[name] = {"meshes": [{"id": m.get("id"), "term": m.get("term")} for m in module.get("meshes") or []
                                   if m.get("id")],
                        "ancestors": [{"id": m.get("id"), "term": m.get("term")} for m in module.get("ancestors") or []
                                      if m.get("id")]}
    return {"source": "ctgov-derived-mesh", "note": "registry-derived MeSH browse terms for the whole study", **browse}


def parse_ctgov_study(study, *, native_version=None):
    """A current v2 record without history: one revision marked ``current-record``."""
    protocol, nct = _ctgov_protocol(study)
    version = native_version or {"version": None,
                                 "date": _date(_get(protocol, "statusModule", "lastUpdatePostDateStruct", "date")),
                                 "basis": "current-record"}
    trial, arms, outcomes = _ctgov_trial(study, nct, version)
    records = [trial, *arms, *outcomes]
    results = _ctgov_results(study, nct, {"version": None, "date": _date(_get(
        protocol, "statusModule", "lastUpdatePostDateStruct", "date")), "basis": "current-record"})
    if results:
        records.append(results)
    return {"nct": nct, "records": records, "annotations": [a for a in [ctgov_derived_terms(study)] if a]}


def parse_ctgov_history(changes):
    """The version list: [{version, date, status, modules}] in registry order."""
    items = changes.get("changes") if isinstance(changes, Mapping) else changes
    if not isinstance(items, list):
        raise SourcePackError("schema_drift", "ClinicalTrials.gov history lacks a changes list")
    versions = []
    for item in items:
        if not isinstance(item, Mapping) or not isinstance(item.get("version"), int) or not _date(item.get("date")):
            raise SourcePackError("schema_drift", "history entries need an integer version and a date")
        versions.append({"version": item["version"], "date": _date(item["date"]),
                         "status": item.get("status"), "modules": list(item.get("moduleLabels") or [])})
    return sorted(versions, key=lambda v: v["version"])


def parse_ctgov_versions(nct, versions, payloads, current=None):
    """Registry versions become trial revisions; outcomes and arms keep present/absent history."""
    records, seen_arms, seen_outcomes = [], {}, {}
    for version, payload in zip(versions, payloads):
        study = payload.get("study") if isinstance(payload, Mapping) and "study" in payload else payload
        native = {"version": str(version["version"]), "date": version["date"], "basis": "registry-history"}
        trial, arms, outcomes = _ctgov_trial(study, nct, native)
        records.append(trial)
        present_arms = {a["arm_key"] for a in arms}
        present_outcomes = {o["outcome_key"] for o in outcomes}
        records.extend(arms)
        records.extend(outcomes)
        for key, previous in seen_arms.items():
            if key not in present_arms:
                records.append(record("trial-arm", **{**_strip(previous), "native_version": native, "present": False}))
        for key, previous in seen_outcomes.items():
            if key not in present_outcomes:
                records.append(record("outcome-measure", **{**_strip(previous), "native_version": native,
                                                            "present": False, "locator": None}))
        seen_arms.update({a["arm_key"]: a for a in arms})
        seen_outcomes.update({o["outcome_key"]: o for o in outcomes})
    annotations = []
    if current is not None and versions:
        latest = {"version": str(versions[-1]["version"]), "date": versions[-1]["date"], "basis": "registry-history"}
        parsed = parse_ctgov_study(current, native_version=latest)
        annotations = parsed["annotations"]
        records.extend(r for r in parsed["records"] if r["record_kind"] == "result-posting")
        current_trial = next(r for r in parsed["records"] if r["record_kind"] == "registered-trial")
        latest_trial = next(r for r in reversed(records) if r["record_kind"] == "registered-trial")
        # The registry's current results flag describes its latest version.
        index = len(records) - 1 - next(i for i, r in enumerate(reversed(records))
                                        if r["record_kind"] == "registered-trial")
        records[index] = record("registered-trial", **{**_strip(latest_trial),
                                                       "results_indicator": current_trial["results_indicator"]})
        if _protocol_view(current_trial) != _protocol_view(latest_trial):
            # The current record should equal the latest registry version. When it
            # does not, it is asserted under that version so the store retains
            # both and reports the conflict instead of choosing one.
            records.append(current_trial)
    return records, annotations


def _protocol_view(trial):
    return json.dumps({k: v for k, v in trial.items() if k not in {"results_indicator", "native_version", "unknowns"}},
                      sort_keys=True)


def _strip(item):
    return {k: v for k, v in item.items() if k not in {"contract", "record_kind", "unknowns"}}


def parse_ctgov_search(payload):
    if not isinstance(payload, Mapping) or not isinstance(payload.get("studies"), list):
        raise SourcePackError("schema_drift", "ClinicalTrials.gov search response has no studies list")
    parsed = [parse_ctgov_study(study) for study in payload["studies"]]
    token = payload.get("nextPageToken")
    return parsed, (str(token) if token else None), payload.get("totalCount")


# ------------------------------------------------------------------- CTIS

_CTIS_STATUS = {"authorised": "authorised", "ongoing": "ongoing", "ended": "ended", "completed": "completed",
                "halted": "halted", "temporarily halted": "halted", "suspended": "suspended",
                "not authorised": "not-authorised", "withdrawn": "withdrawn", "revoked": "not-authorised"}


def _phase_text(text):
    text = str(text or "").lower()
    found = []
    for pattern, value in ((r"phase\s*iv\b|phase\s*4", "phase-4"), (r"phase\s*iii\b|phase\s*3", "phase-3"),
                           (r"phase\s*ii\b|phase\s*2", "phase-2"), (r"phase\s*i\b|phase\s*1", "phase-1")):
        if re.search(pattern, text):
            found.append(value)
    return sorted(set(found)) or ["unknown"]


def _allocation_text(text):
    text = str(text or "").lower()
    if not text:
        return "unknown"
    if re.search(r"non[- ]?randomi[sz]ed", text):
        return "non-randomized"
    if re.search(r"randomi[sz]ed", text):
        return "randomized"
    return "unknown"


def _masking_text(text):
    text = str(text or "").lower()
    for pattern, value in ((r"quadruple", "quadruple"), (r"triple", "triple"), (r"double", "double"),
                           (r"single", "single"), (r"\bopen\b|open[- ]label|unblinded|not blinded", "none")):
        if re.search(pattern, text):
            return value
    return "unknown"


def ctis_trial_url(number):
    return f"https://euclinicaltrials.eu/search-for-clinical-trials/?lang=en&EUCT={number}"


def parse_ctis_trial(payload):
    if not isinstance(payload, Mapping) or not re.fullmatch(r"\d{4}-\d{6}-\d{2}-\d{2}", str(payload.get("ctNumber"))):
        raise SourcePackError("schema_drift", "CTIS record lacks a valid ctNumber")
    number = payload["ctNumber"]
    part1 = _get(payload, "authorizedApplication", "authorizedPartI")
    if not isinstance(part1, Mapping):
        raise SourcePackError("schema_drift", "CTIS record lacks authorizedApplication.authorizedPartI")
    root = "/authorizedApplication/authorizedPartI"
    details = part1.get("trialDetails") or {}
    identifiers = _get(details, "clinicalTrialIdentifiers", default={}) or {}
    info = details.get("trialInformation") or {}
    design = (_get(details, "protocolInformation", "studyDesign", "periodDetails", default=[]) or [{}])[0] or {}
    secondary = []
    numbers = identifiers.get("secondaryIdentifyingNumbers") or {}
    for key, kind in (("nctNumber", "nct"), ("isrctnNumber", "isrctn"), ("eudraCtNumber", "eudract"),
                      ("whoUniversalTrialNumber", "who-utn")):
        value = _get(numbers, key, "number")
        if value:
            secondary.append({"kind": classify_identifier(value, "EUDRACT" if kind == "eudract" else None)
                              if kind != "who-utn" else "who-utn", "value": str(value).strip(),
                              "declared_by": f"ctis:{key}", "domain": None,
                              "locator": {"json_pointer": f"{root}/trialDetails/clinicalTrialIdentifiers/"
                                                          f"secondaryIdentifyingNumbers/{key}/number"}})
    native_status = payload.get("ctStatus")
    states = []
    for index, msc in enumerate(payload.get("memberStatesConcerned") or []):
        states.append({"state": msc.get("mscName"), "authority": msc.get("authority"),
                       "status": msc.get("status"), "decision": str(msc.get("decision") or "").lower() or None,
                       "decision_date": _date(msc.get("decisionDate")),
                       "locator": {"json_pointer": f"/memberStatesConcerned/{index}"}})
    version = {"version": None, "date": _date(payload.get("lastUpdated")), "basis": "current-record"}
    url = ctis_trial_url(number)
    outcomes = []
    for role, key in (("primary", "primaryEndPoints"), ("secondary", "secondaryEndPoints")):
        for index, point in enumerate(_get(info, "endPoint", key, default=[]) or []):
            measure = _clip(point.get("endPoint"))
            if measure:
                outcomes.append(record(
                    "outcome-measure", registry="ctis", identifier=number, outcome_key=normalize_text(measure),
                    measure=measure, role=role, time_frame=_clip(point.get("timeFrame"), 2000), description=None,
                    source_url=url, native_version=version, present=True,
                    locator={"json_pointer": f"{root}/trialDetails/trialInformation/endPoint/{key}/{index}"}))
    sponsors = part1.get("sponsors") or []
    lead = next((s for s in sponsors if s.get("primary")), sponsors[0] if sponsors else {})
    subjects = part1.get("rowSubjectCount")
    results = _get(payload, "results", "summaryResults", default=[]) or []
    trial = record(
        "registered-trial", registry="ctis", identifier=number,
        title=_clip(identifiers.get("fullTitle") or identifiers.get("shortTitle"), 4000), source_url=url,
        native_version=version,
        status={"native": native_status, "normalized": _CTIS_STATUS.get(str(native_status or "").lower(), "unknown"),
                "locator": {"json_pointer": "/ctStatus"}},
        phase={"native": _get(info, "trialCategory", "trialPhase"),
               "normalized": _phase_text(_get(info, "trialCategory", "trialPhase")),
               "locator": {"json_pointer": f"{root}/trialDetails/trialInformation/trialCategory/trialPhase"}},
        sponsor={"lead": _get(lead, "organisation", "name"), "class": None},
        secondary_identifiers=secondary,
        conditions=[{"term": str(c.get("medicalCondition")),
                     "locator": {"json_pointer": f"{root}/trialDetails/trialInformation/medicalCondition/"
                                                 f"partIMedicalConditions/{i}"}}
                    for i, c in enumerate(_get(info, "medicalCondition", "partIMedicalConditions", default=[]) or [])
                    if c.get("medicalCondition")],
        interventions=[{"type": p.get("productRole"), "name": p.get("productName"), "other_names": [],
                        "locator": {"json_pointer": f"{root}/products/{i}"}}
                       for i, p in enumerate(part1.get("products") or []) if p.get("productName")],
        design={"study_type": "INTERVENTIONAL", "allocation": _allocation_text(design.get("allocationMethod")),
                "masking": _masking_text(design.get("blindingMethod")),
                "enrollment_planned": subjects if type(subjects) is int else None, "enrollment_actual": None,
                "locator": {"json_pointer": f"{root}/trialDetails/protocolInformation/studyDesign/periodDetails/0"}},
        registration={"first_submitted": _date(payload.get("submissionDate")), "first_posted": _date(
            payload.get("publishDate")), "start_date": _date(payload.get("startDateEU")),
            "prospective": prospective(_date(payload.get("submissionDate")), _date(payload.get("startDateEU"))),
            "locator": {"json_pointer": "/submissionDate"}},
        member_states=states,
        results_indicator={"has_results": bool(results) if "results" in payload else None},
        outcome_keys=sorted(o["outcome_key"] for o in outcomes), arm_keys=[],
    )
    records = [trial, *outcomes]
    if results:
        first = results[0]
        records.append(record(
            "result-posting", registry="ctis", identifier=number, source_url=url, native_version=version,
            posted={"first_posted": _date(first.get("submissionDate")), "first_submitted": None,
                    "last_update": _date(results[-1].get("submissionDate")),
                    "locator": {"json_pointer": "/results/summaryResults/0"}},
            outcome_results=[], adverse_events=None, content_acquired=False,
            results_summary_url=first.get("url") if str(first.get("url") or "").startswith("https://") else None,
            locator={"json_pointer": "/results/summaryResults"}))
    return {"ct_number": number, "records": records, "annotations": []}


# ------------------------------------------------------------------ EU CTR

_EUCTR_STATUS = {"completed": "completed", "ongoing": "ongoing", "prematurely ended": "ended",
                 "temporarily halted": "halted", "restarted": "ongoing", "not authorised": "not-authorised",
                 "prohibited by ca": "not-authorised", "suspended by ca": "suspended"}
_EUCTR_LINE = re.compile(r"^(?:(?P<code>[A-Z](?:\.\d+)+(?:\.\d+)*)\s+)?(?P<label>[^:]{1,200}):\s*(?P<value>.*)$")


def euctr_trial_url(eudract):
    return f"https://www.clinicaltrialsregister.eu/ctr-search/search?query={eudract}"


def _euctr_entries(text):
    entries, current = [], None
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("EudraCT Number:"):
            current = {"fields": {}, "labels": {}, "start_line": number}
            entries.append(current)
        if current is None:
            continue
        match = _EUCTR_LINE.match(stripped)
        if not match:
            continue
        code, label, value = match.group("code"), match.group("label").strip(), match.group("value").strip()
        key = code or label
        current["fields"].setdefault(key, (value, number))
        current["labels"].setdefault(label, (value, number))
    return entries


def parse_euctr_text(raw, *, eudract):
    """Full-text protocol download → one registered trial (member-state protocols kept per state)."""
    text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
    entries = [e for e in _euctr_entries(text) if e["labels"].get("EudraCT Number", ("",))[0] == eudract]
    if not entries:
        raise SourcePackError("schema_drift", "EU CTR download has no protocol for the requested EudraCT number")
    first = entries[0]

    def field(entry, key):
        value = (entry["fields"].get(key) or entry["labels"].get(key) or (None, None))
        return (value[0] or None), value[1]

    def yes(entry, key):
        value, _ = field(entry, key)
        return None if value is None else value.strip().lower() == "yes"

    states, statuses = [], set()
    for entry in entries:
        authority, line = field(entry, "National Competent Authority")
        status, _ = field(entry, "Trial Status")
        statuses.add(_EUCTR_STATUS.get(str(status or "").lower(), "unknown"))
        states.append({"state": (authority or "").split(" - ")[0] or None, "authority": authority, "status": status,
                       "decision": "not-authorised" if str(status or "").lower() in {"not authorised",
                                                                                      "prohibited by ca"} else None,
                       "decision_date": None, "locator": {"line": line}})
    phases = [p for code, p in (("E.7.1", "phase-1"), ("E.7.2", "phase-2"), ("E.7.3", "phase-3"), ("E.7.4", "phase-4"))
              if yes(first, code)]
    randomised = yes(first, "E.8.1.1")
    masking = ("double" if yes(first, "E.8.1.4") else "single" if yes(first, "E.8.1.3") else
               "none" if yes(first, "E.8.1.2") else "unknown")
    planned, _ = field(first, "F.4.2.2")
    secondary = []
    for code, kind in (("A.5.2", "nct"), ("A.5.1", "isrctn"), ("A.5.3", "who-utn"), ("A.4.1", "sponsor-protocol")):
        value, line = field(first, code)
        if value:
            kind = classify_identifier(value) if kind in {"nct", "isrctn"} else kind
            secondary.append({"kind": kind, "value": value.strip(), "declared_by": f"euctr:{code}", "domain": None,
                              "locator": {"line": line, "section": code}})
    version = {"version": None, "date": None, "basis": "current-record"}
    url = euctr_trial_url(eudract)
    outcomes = []
    for role, code, time_code in (("primary", "E.5.1", "E.5.1.1"), ("secondary", "E.5.2", "E.5.2.1")):
        value, line = field(first, code)
        if value:
            timeframe, _ = field(first, time_code)
            outcomes.append(record(
                "outcome-measure", registry="euctr", identifier=eudract, outcome_key=normalize_text(value),
                measure=_clip(value), role=role, time_frame=_clip(timeframe, 2000), description=None, source_url=url,
                native_version=version, present=True, locator={"line": line, "section": code}))
    results_line, results_line_no = field(first, "Trial results")
    has_results = None if results_line is None else "view results" in results_line.lower()
    products = []
    for entry in entries:
        for key, (value, line) in entry["fields"].items():
            if key in {"D.3.1", "D.3.8"} and value and value.casefold() not in {p["name"].casefold() for p in products}:
                products.append({"type": "IMP", "name": value, "other_names": [], "locator": {"line": line,
                                                                                              "section": key}})
        placebo, line = (entry["labels"].get("D.8.1 Is a Placebo used in this Trial?") or
                         entry["fields"].get("D.8.1") or (None, None))
        if placebo and placebo.strip().lower() == "yes" and "placebo" not in {p["name"].casefold() for p in products}:
            products.append({"type": "PLACEBO", "name": "Placebo", "other_names": [],
                             "locator": {"line": line, "section": "D.8.1"}})
    condition, condition_line = field(first, "E.1.1")
    first_entered, entered_line = field(first, "Date on which this record was first entered in the EudraCT database")
    trial = record(
        "registered-trial", registry="euctr", identifier=eudract, title=_clip(field(first, "A.3")[0], 4000),
        source_url=url, native_version=version,
        status={"native": sorted({s["status"] for s in states if s["status"]}) or None,
                "normalized": statuses.pop() if len(statuses) == 1 else "unknown",
                "locator": {"line": first["start_line"]}},
        phase={"native": phases or None, "normalized": phases or ["unknown"], "locator": {"section": "E.7"}},
        sponsor={"lead": field(first, "B.1.1")[0], "class": None},
        secondary_identifiers=secondary,
        conditions=[{"term": condition, "locator": {"line": condition_line, "section": "E.1.1"}}] if condition else [],
        interventions=products,
        design={"study_type": "INTERVENTIONAL",
                "allocation": "unknown" if randomised is None else "randomized" if randomised else "non-randomized",
                "masking": masking, "controlled": yes(first, "E.8.1"),
                "enrollment_planned": int(planned) if planned and planned.isdigit() else None,
                "enrollment_actual": None, "source_member_state": states[0]["state"],
                "locator": {"section": "E.8", "line": first["start_line"]}},
        registration={"first_submitted": _date(first_entered), "prospective": None,
                      "locator": {"line": entered_line}},
        member_states=states,
        results_indicator={"has_results": has_results},
        outcome_keys=sorted(o["outcome_key"] for o in outcomes), arm_keys=[],
    )
    records = [trial, *outcomes]
    if has_results:
        records.append(record(
            "result-posting", registry="euctr", identifier=eudract,
            source_url=f"https://www.clinicaltrialsregister.eu/ctr-search/trial/{eudract}/results",
            native_version=version, posted={"first_posted": None, "first_submitted": None, "last_update": None,
                                            "locator": {"line": results_line_no}},
            outcome_results=[], adverse_events=None, content_acquired=False,
            results_summary_url=f"https://www.clinicaltrialsregister.eu/ctr-search/trial/{eudract}/results",
            locator={"line": results_line_no, "quote": results_line}))
    return {"eudract": eudract, "records": records, "annotations": []}


# ------------------------------------------------------------------ openFDA

LABEL_SECTIONS = ("indications_and_usage", "boxed_warning", "warnings_and_cautions", "warnings", "contraindications",
                  "adverse_reactions")
# Dosing text is never retained: the pack produces no dosing recommendation.
EXCLUDED_LABEL_SECTIONS = ("dosage_and_administration", "dosage_forms_and_strengths", "overdosage")


def _openfda_meta(payload):
    meta = payload.get("meta") if isinstance(payload, Mapping) else None
    if not isinstance(meta, Mapping) or not str(meta.get("disclaimer") or "").strip():
        raise SourcePackError("schema_drift", "openFDA response lacks meta.disclaimer; refusing to store it without")
    return {"text": str(meta["disclaimer"]), "terms": meta.get("terms"), "license": meta.get("license"),
            "last_updated": _date(meta.get("last_updated"))}


def _openfda_product(openfda):
    openfda = openfda or {}
    return {"brand_names": sorted(set(openfda.get("brand_name") or [])),
            "generic_names": sorted(set(openfda.get("generic_name") or [])),
            "application_numbers": sorted(set(openfda.get("application_number") or [])),
            "manufacturer": (openfda.get("manufacturer_name") or [None])[0],
            "substances": sorted(set(openfda.get("substance_name") or []))}


def parse_openfda_labels(payload, *, query):
    disclaimer = _openfda_meta(payload)
    results = payload.get("results")
    if not isinstance(results, list):
        raise SourcePackError("schema_drift", "openFDA label response lacks results")
    records = []
    for index, item in enumerate(results):
        set_id, version = item.get("set_id"), item.get("version")
        if not set_id or version is None:
            raise SourcePackError("schema_drift", "label lacks set_id or version")
        sections = {}
        for key in LABEL_SECTIONS:
            value = item.get(key)
            if value:
                sections[key] = {"text": _clip(" ".join(value) if isinstance(value, list) else value),
                                 "locator": {"json_pointer": f"/results/{index}/{key}"}}
        product = _openfda_product(item.get("openfda"))
        records.append(record(
            "regulatory-record", authority="FDA", provider="openfda", regulatory_kind="label-revision",
            native_id=str(set_id), title="Label: " + (", ".join(product["brand_names"] or product["generic_names"])
                                                      or str(set_id)),
            source_url=f"https://api.fda.gov/drug/label.json?search=set_id:{quote(str(set_id))}",
            native_version={"version": str(version), "date": _date(item.get("effective_time")),
                            "basis": "label-version"},
            product={"name": (product["brand_names"] or product["generic_names"] or [None])[0], **product},
            dates={"effective": _date(item.get("effective_time"))}, sections=sections,
            omitted_sections=[{"section": key, "reason": "dosing text is outside the pack's non-advice boundary"}
                              for key in EXCLUDED_LABEL_SECTIONS if item.get(key)],
            documents=[{"url": "https://dailymed.nlm.nih.gov/dailymed/lookup.cfm?setid=" + quote(str(set_id)),
                        "kind": "label"}],
            disclaimer=disclaimer))
    return records


def parse_openfda_approvals(payload):
    disclaimer = _openfda_meta(payload)
    results = payload.get("results")
    if not isinstance(results, list):
        raise SourcePackError("schema_drift", "Drugs@FDA response lacks results")
    records = []
    for index, item in enumerate(results):
        number = str(item.get("application_number") or "")
        if not re.fullmatch(r"(NDA|ANDA|BLA)\d{6}", number):
            raise SourcePackError("schema_drift", "Drugs@FDA result lacks an application number")
        submissions = sorted(({"type": s.get("submission_type"), "number": s.get("submission_number"),
                               "status": s.get("submission_status"),
                               "status_date": _date(s.get("submission_status_date")),
                               "class_code": s.get("submission_class_code"),
                               "class_description": s.get("submission_class_code_description")}
                              for s in item.get("submissions") or []),
                             key=lambda s: (s["status_date"] or "", str(s["number"])))
        original = next((s for s in submissions if s["type"] == "ORIG" and s["status"] == "AP"), None)
        product = _openfda_product(item.get("openfda"))
        names = sorted({p.get("brand_name") for p in item.get("products") or [] if p.get("brand_name")})
        ingredients = sorted({a.get("name") for p in item.get("products") or []
                              for a in p.get("active_ingredients") or [] if a.get("name")})
        records.append(record(
            "regulatory-record", authority="FDA", provider="openfda", regulatory_kind="approval", native_id=number,
            title=f"FDA application {number}", source_url=f"https://api.fda.gov/drug/drugsfda.json?search="
                                                          f"application_number:{number}",
            native_version={"version": None, "date": submissions[-1]["status_date"] if submissions else None,
                            "basis": "observation"},
            product={"name": names[0] if names else None, "brand_names": names or product["brand_names"],
                     "generic_names": product["generic_names"], "active_ingredients": ingredients,
                     "application_numbers": [number], "sponsor": item.get("sponsor_name")},
            dates={"original_approval": original["status_date"] if original else None},
            status={"marketing_status": sorted({p.get("marketing_status") for p in item.get("products") or []
                                                if p.get("marketing_status")})},
            submissions=submissions,
            documents=[{"url": "https://www.accessdata.fda.gov/scripts/cder/daf/index.cfm?event=overview.process"
                               f"&ApplNo={number[-6:]}", "kind": "drugs-at-fda"}],
            disclaimer=disclaimer))
    return records


def parse_openfda_event_counts(payload, *, generic_name, count_field, total_payload=None, limit=25):
    disclaimer = _openfda_meta(payload)
    results = payload.get("results")
    if not isinstance(results, list) or any(not isinstance(r, Mapping) or "term" not in r or type(r.get("count"))
                                            is not int for r in results):
        raise SourcePackError("schema_drift", "FAERS count response lacks term/count results")
    total = _get(total_payload, "meta", "results", "total") if total_payload else None
    search = f'patient.drug.openfda.generic_name:"{generic_name}"'
    return record(
        "regulatory-record", authority="FDA", provider="openfda", regulatory_kind="adverse-event-summary",
        native_id=f"faers:{generic_name.lower()}:{count_field}",
        title=f"FAERS report counts: {generic_name}", source_url="https://api.fda.gov/drug/event.json?"
                                                                 + urlencode({"search": search, "count": count_field}),
        native_version={"version": None, "date": disclaimer["last_updated"], "basis": "observation"},
        product={"name": generic_name, "generic_names": [generic_name]},
        dates={"data_last_updated": disclaimer["last_updated"]},
        counts=[{"term": str(r["term"]), "reports": r["count"]} for r in results[:limit]],
        count_query={"search": search, "count": count_field, "limit": limit,
                     "terminology": "MedDRA preferred terms as reported (not mapped to MeSH)"},
        reports_total=total if type(total) is int else None, count_semantics=COUNT_SEMANTICS,
        disclaimer=disclaimer)


# ---------------------------------------------------------------------- EMA

_EMA_STATUS = {"authorised": "authorised", "withdrawn": "withdrawn", "refused": "refused", "suspended": "suspended",
               "not renewed": "not-renewed", "revoked": "revoked", "lapsed": "lapsed"}
_EMA_FIELDS = ("name_of_medicine", "ema_product_number", "medicine_status")


def parse_ema_medicines(payload, *, product_numbers):
    rows = payload.get("data") if isinstance(payload, Mapping) else payload
    if not isinstance(rows, list):
        raise SourcePackError("schema_drift", "EMA export is not a list of medicines")
    wanted, records = set(product_numbers), []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping) or any(key not in row for key in _EMA_FIELDS):
            raise SourcePackError("schema_drift", "EMA export rows lack name/product number/status")
        number = str(row["ema_product_number"]).strip()
        if number not in wanted:
            continue
        status = str(row.get("medicine_status") or "")
        mesh = [t.strip() for t in re.split(r"[;\n]", str(row.get("therapeutic_area_mesh") or "")) if t.strip()]
        url = row.get("medicine_url")
        records.append(record(
            "regulatory-record", authority="EMA", provider="ema", regulatory_kind="authorisation-status",
            native_id=number, title=f"EMA: {row['name_of_medicine']}",
            source_url=url if str(url or "").startswith("https://") else
            "https://www.ema.europa.eu/en/medicines/download-medicine-data",
            native_version={"version": str(row["revision_number"]) if row.get("revision_number") is not None else None,
                            "date": _date(row.get("last_updated_date")), "basis": "revision-number"},
            product={"name": row["name_of_medicine"], "inn": row.get("inn_common_name"),
                     "active_substance": row.get("active_substance"), "ema_product_number": number,
                     "holder": row.get("marketing_authorisation_developer_applicant_holder"),
                     "generic_names": [n for n in [row.get("inn_common_name")] if n]},
            status={"native": status, "normalized": _EMA_STATUS.get(status.lower(), "unknown"),
                    "locator": {"json_pointer": f"/data/{index}/medicine_status"}},
            dates={"marketing_authorisation": _date(row.get("marketing_authorisation_date")),
                   "last_updated": _date(row.get("last_updated_date"))},
            documents=[{"url": url, "kind": "epar"}] if str(url or "").startswith("https://") else [],
            mesh_terms=mesh))
    missing = sorted(wanted - {r["native_id"] for r in records})
    return records, missing


# ----------------------------------------------------------------- PROSPERO


def parse_prospero_export(payload, *, supplied_by):
    """A PROSPERO record export supplied by the user (the only supported route)."""
    if not isinstance(payload, Mapping):
        raise ClinicalRecordError("invalid_export", "PROSPERO export must be an object of record fields")
    number = str(payload.get("registration_number") or payload.get("CRD") or "").strip()
    if not re.fullmatch(r"CRD\d{8,14}", number):
        raise ClinicalRecordError("invalid_export", "PROSPERO export lacks a CRD registration number")
    return record(
        "review-registration", registry="prospero", identifier=number, title=_clip(payload.get("title"), 4000),
        source_url=f"https://www.crd.york.ac.uk/prospero/display_record.php?RecordID={number[3:]}",
        native_version={"version": str(payload.get("version")) if payload.get("version") is not None else None,
                        "date": _date(payload.get("last_updated") or payload.get("date_of_registration")),
                        "basis": "registration"},
        review_question=_clip(payload.get("review_question")), condition=_clip(payload.get("condition_or_domain")),
        population=_clip(payload.get("participants")), interventions=_clip(payload.get("interventions")),
        comparators=_clip(payload.get("comparators")), outcomes=_clip(payload.get("main_outcomes")),
        registration_date=_date(payload.get("date_of_registration")),
        status={"native": payload.get("stage_of_review")},
        source={"kind": "user-supplied-export", "supplied_by": supplied_by,
                "note": "not acquired from PROSPERO by this system; fields as supplied"})


# ------------------------------------------------------------------ adapters


def _summary(records):
    trial = next((r for r in records if r["record_kind"] == "registered-trial"), None)
    if trial:
        parts = [trial.get("title") or trial["identifier"], "Status: " + str(trial["status"].get("normalized")),
                 "Conditions: " + ", ".join(c["term"] for c in trial.get("conditions") or []),
                 "Interventions: " + ", ".join(str(i["name"]) for i in trial.get("interventions") or []),
                 trial.get("brief_summary") or ""]
        return "\n".join(p for p in parts if p)
    return "\n".join(f"{r.get('title') or r.get('native_id')}" for r in records) or "clinical record"


class _ClinicalAdapter:
    """One page per pinned item (or per search page); the cursor is bound to the selection."""

    accepts_transport = True
    connector = ""

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None) -> None:
        from functools import partial

        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        self.source = json.loads(json.dumps(source))
        self.config = dict(self.source.get("clinical") or {})
        if not str(self.config.get("namespace") or "").strip():
            raise SourcePackError("invalid_mapping", "clinical sources name the namespace their records belong to")
        self.secret = secret
        self.work = self._work()
        if not 1 <= len(self.work) <= MAX_ITEMS:
            raise SourcePackError("unbounded_source", f"{self.connector} sources pin 1-{MAX_ITEMS} items")
        self.execution = "network" if transport is None else "injected"
        self.transport = transport or partial(HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"]))
        self.base = "https://" + (urlsplit(self.source["endpoint"]).hostname or "")
        self.definition = {
            "contract": ADAPTER_CONTRACT, "source_id": source["source_id"], "connector": source["connector"],
            "endpoint": source["endpoint"], "operations": list(source["operations"]),
            "source_hash": source["source_hash"], "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"], "limits": source["budgets"],
            "clinical": {"items": len(self.work), "namespace": self.config["namespace"],
                         "credential": "configured" if secret else "none"},
        }
        self.requests: list[dict[str, Any]] = []

    def _work(self) -> list[Any]:
        raise NotImplementedError

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _get(self, url: str, *, accept="application/json") -> tuple[int, bytes]:
        from src.ingestion.source_pack_runtime import _retry_after_ms

        if urlsplit(url).hostname != urlsplit(self.source["endpoint"]).hostname:
            raise SourcePackError("network_policy", "requests stay on the declared host")
        target, params = url, {}
        if self.secret and self.connector == "openfda":
            # The key travels only as a request parameter; it never enters the
            # durable URL, the page receipt or the stored record.
            parts = urlsplit(url)
            target = f"{parts.scheme}://{parts.netloc}{parts.path}"
            params = {**dict(parse_qsl(parts.query, keep_blank_values=True)), "api_key": self.secret}
        response = self.transport(url=target, params=params, headers={"Accept": accept},
                                  timeout=int(self.definition["limits"]["timeout_ms"]) / 1000)
        status = int(response.get("status", 200))
        headers = {str(k).casefold(): v for k, v in dict(response.get("headers") or {}).items()}
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError("response_too_large", "source response exceeds its byte limit")
        if self.secret and len(self.secret) >= 8 and self.secret.encode() in raw:
            raise SourcePackError("authentication_failed", "provider echoed the credential; response discarded")
        if status in {401, 403}:
            raise SourcePackError("authentication_failed", f"provider refused access (HTTP {status})")
        if status == 429:
            raise SourcePackError("rate_limited", "provider rate limit reached",
                                  retry_after_ms=_retry_after_ms(headers.get("retry-after")))
        if status >= 500:
            raise SourcePackError("source_unavailable", f"provider returned HTTP {status}")
        self.requests.append({"path": urlsplit(url).path + (f"?{urlsplit(url).query}" if urlsplit(url).query else ""),
                              "status": status, "sha256": _sha(raw), "bytes": len(raw)})
        return status, raw

    def _scope(self) -> str:
        return _digest({"endpoint": self.source["endpoint"], "work": self.work, "config": self.config})

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage

        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError("operation_forbidden", "operation is not declared by the source")
        if dict(request.get("parameters") or {}):
            raise SourcePackError("parameter_forbidden", "clinical runs use the pinned selection")
        scope = self._scope()
        state = {} if cursor is None else json.loads(cursor)
        if cursor is not None and state.get("scope") != scope:
            raise SourcePackError("cursor_drift", "cursor belongs to a different selection")
        self.requests = []
        records, next_state, outcome, item = self._page(state)
        raw_len = sum(r["bytes"] for r in self.requests)
        return RuntimePage(tuple(records), json.dumps({**next_state, "scope": scope}, sort_keys=True)
                           if next_state is not None else None, raw_len,
                           receipt={"status": 200, "outcome": outcome, "item": item, "requests": list(self.requests),
                                    "work_size": len(self.work), "execution": self.execution})

    def _pinned_page(self, state):
        index = int(state.get("i", 0))
        if index >= len(self.work):
            return [], None, "exhausted", None
        item = self.work[index]
        records, outcome = self._item(item)
        return records, ({"i": index + 1} if index + 1 < len(self.work) else None), outcome, item

    def _page(self, state):
        return self._pinned_page(state)

    def _item(self, item):
        raise NotImplementedError

    def _page_record(self, identifier_value, title, url, parsed, extra=None):
        records = parsed["records"]
        return {"id": f"{self.connector}:{identifier_value}", "title": title or identifier_value, "language": "en",
                "url": url, "content": _summary(records),
                "clinical_records": records, "clinical_annotations": parsed.get("annotations") or [],
                "clinical_namespace": self.config["namespace"],
                "clinical_capture": {"connector": self.connector, "requests": list(self.requests),
                                     "extractor": EXTRACTOR, **(extra or {})}}


class CtgovAdapter(_ClinicalAdapter):
    connector = "ctgov"

    def _work(self):
        if self.config.get("query"):
            query = dict(self.config["query"])
            if set(query) - {"cond", "intr", "term"} or not query:
                raise SourcePackError("invalid_mapping", "ctgov queries use cond/intr/term")
            size = int(self.config.get("page_size", 20))
            if not 1 <= size <= 100:
                raise SourcePackError("unbounded_source", "ctgov page_size is 1-100")
            return [{"query": query, "page_size": size}]
        ids = [str(v) for v in self.config.get("nct_ids") or []]
        if any(not re.fullmatch(r"NCT\d{8}", v) for v in ids):
            raise SourcePackError("invalid_mapping", "ctgov sources pin NCT numbers")
        return ids

    def _study_url(self, nct):
        return f"{self.base}/api/v2/studies/{nct}"

    def _page(self, state):
        if not self.config.get("query"):
            return self._pinned_page(state)
        spec = self.work[0]
        params = {f"query.{k}": v for k, v in sorted(spec["query"].items())}
        params.update({"pageSize": str(spec["page_size"]), "countTotal": "true"})
        if state.get("token"):
            params["pageToken"] = state["token"]
        page = int(state.get("page", 0))
        status, raw = self._get(f"{self.base}/api/v2/studies?" + urlencode(sorted(params.items())))
        if status >= 400:
            raise SourcePackError("schema_drift", f"ClinicalTrials.gov search returned HTTP {status}")
        parsed, token, total = parse_ctgov_search(_json(raw, "ClinicalTrials.gov"))
        records = [self._page_record(p["nct"], next((r.get("title") for r in p["records"]
                                                     if r["record_kind"] == "registered-trial"), None),
                                     ctgov_study_url(p["nct"]), p, {"search_total": total, "search_page": page})
                   for p in parsed]
        return records, ({"token": token, "page": page + 1} if token else None), "returned", f"search-page-{page}"

    def _item(self, nct):
        status, raw = self._get(self._study_url(nct))
        if status == 404:
            return [], "not_found"
        if status >= 400:
            raise SourcePackError("schema_drift", f"ClinicalTrials.gov returned HTTP {status}")
        current = _json(raw, "ClinicalTrials.gov")
        if self.config.get("history"):
            status, raw = self._get(f"{self.base}/api/int/studies/{nct}/history")
            if status >= 400:
                raise SourcePackError("schema_drift", f"ClinicalTrials.gov history returned HTTP {status}")
            versions = parse_ctgov_history(_json(raw, "ClinicalTrials.gov history"))
            limit = min(int(self.config.get("max_versions", MAX_VERSIONS)), MAX_VERSIONS)
            truncated = len(versions) > limit
            versions = versions[-limit:]
            payloads = []
            for version in versions:
                status, raw = self._get(f"{self.base}/api/int/studies/{nct}/history/{version['version']}")
                if status >= 400:
                    raise SourcePackError("schema_drift", "ClinicalTrials.gov version was unavailable")
                payloads.append(_json(raw, "ClinicalTrials.gov version"))
            records, annotations = parse_ctgov_versions(nct, versions, payloads, current)
            parsed = {"records": records, "annotations": annotations}
            extra = {"versions": [v["version"] for v in versions], "versions_truncated": truncated}
        else:
            parsed, extra = parse_ctgov_study(current), {"versions": None}
        title = next((r.get("title") for r in parsed["records"] if r["record_kind"] == "registered-trial"), nct)
        return [self._page_record(nct, title, ctgov_study_url(nct), parsed, extra)], "returned"


class CtisAdapter(_ClinicalAdapter):
    connector = "ctis"

    def _work(self):
        ids = [str(v) for v in self.config.get("ct_numbers") or []]
        if any(not re.fullmatch(r"\d{4}-\d{6}-\d{2}-\d{2}", v) for v in ids):
            raise SourcePackError("invalid_mapping", "ctis sources pin EU CT numbers")
        return ids

    def _item(self, number):
        status, raw = self._get(f"{self.source['endpoint'].rstrip('/')}/retrieve/{number}")
        if status == 404:
            return [], "not_found"
        if status >= 400:
            raise SourcePackError("schema_drift", f"CTIS returned HTTP {status}")
        parsed = parse_ctis_trial(_json(raw, "CTIS"))
        if parsed["ct_number"] != number:
            raise SourcePackError("schema_drift", "CTIS returned another trial")
        title = parsed["records"][0].get("title")
        return [self._page_record(number, title, ctis_trial_url(number), parsed)], "returned"


class EuctrAdapter(_ClinicalAdapter):
    connector = "eu-ctr"

    def _work(self):
        ids = [str(v) for v in self.config.get("eudract_numbers") or []]
        if any(not re.fullmatch(r"\d{4}-\d{6}-\d{2}", v) for v in ids):
            raise SourcePackError("invalid_mapping", "eu-ctr sources pin EudraCT numbers")
        return ids

    def _item(self, eudract):
        url = (f"{self.source['endpoint'].rstrip('/')}/rest/download/full?"
               + urlencode([("query", eudract), ("mode", "current_page")]))
        status, raw = self._get(url, accept="text/plain")
        if status == 404:
            return [], "not_found"
        if status >= 400:
            raise SourcePackError("schema_drift", f"EU CTR returned HTTP {status}")
        parsed = parse_euctr_text(raw, eudract=eudract)
        return [self._page_record(eudract, parsed["records"][0].get("title"), euctr_trial_url(eudract), parsed)], \
            "returned"


class OpenfdaAdapter(_ClinicalAdapter):
    connector = "openfda"

    def _work(self):
        products = [dict(p) for p in self.config.get("products") or []]
        if any(set(p) != {"generic_name"} or not re.fullmatch(r"[A-Za-z0-9 .\-]{2,100}", str(p["generic_name"]))
               for p in products):
            raise SourcePackError("invalid_mapping", "openfda sources pin products by generic_name")
        endpoints = set(self.config.get("endpoints") or [])
        if not endpoints or endpoints - {"label", "drugsfda", "event-counts"}:
            raise SourcePackError("invalid_mapping", "openfda endpoints are label, drugsfda and event-counts")
        return [p["generic_name"].lower() for p in products]

    def _url(self, path, params):
        return f"{self.base}{path}?" + urlencode(params)

    def _item(self, name):
        endpoints = set(self.config["endpoints"])
        search_label = f'openfda.generic_name:"{name}"'
        records, outcome = [], "returned"
        if "label" in endpoints:
            status, raw = self._get(self._url("/drug/label.json", [("search", search_label),
                                                                   ("limit", str(int(self.config.get("max_labels", 3))))]))
            if status == 404:
                outcome = "partial"
            elif status >= 400:
                raise SourcePackError("schema_drift", f"openFDA label returned HTTP {status}")
            else:
                records += parse_openfda_labels(_json(raw, "openFDA"), query=search_label)
        if "drugsfda" in endpoints:
            status, raw = self._get(self._url("/drug/drugsfda.json", [("search", search_label), ("limit", "5")]))
            if status == 404:
                outcome = "partial"
            elif status >= 400:
                raise SourcePackError("schema_drift", f"Drugs@FDA returned HTTP {status}")
            else:
                records += parse_openfda_approvals(_json(raw, "openFDA"))
        if "event-counts" in endpoints:
            field = str(self.config.get("event_count_field") or "patient.reaction.reactionmeddrapt.exact")
            if field not in {"patient.reaction.reactionmeddrapt.exact", "patient.reaction.reactionoutcome"}:
                raise SourcePackError("invalid_mapping", "unsupported FAERS count field")
            search = f'patient.drug.openfda.generic_name:"{name}"'
            status, counts = self._get(self._url("/drug/event.json", [("search", search), ("count", field)]))
            total_status, total = self._get(self._url("/drug/event.json", [("search", search), ("limit", "1")]))
            if status == 404:
                outcome = "partial"
            elif status >= 400 or total_status >= 400 and total_status != 404:
                raise SourcePackError("schema_drift", "FAERS count query failed")
            else:
                records.append(parse_openfda_event_counts(
                    _json(counts, "openFDA"), generic_name=name, count_field=field,
                    total_payload=_json(total, "openFDA") if total_status < 400 else None))
        if not records:
            return [], "not_found"
        parsed = {"records": records}
        return [self._page_record(name, f"openFDA: {name}", "https://open.fda.gov/apis/drug/", parsed)], outcome


class EmaAdapter(_ClinicalAdapter):
    connector = "ema-medicines"

    def _work(self):
        numbers = [str(v) for v in self.config.get("product_numbers") or []]
        if any(not re.fullmatch(r"EMEA/H/C/\d{6}", v) for v in numbers):
            raise SourcePackError("invalid_mapping", "ema-medicines sources pin EMA product numbers")
        path = str(self.config.get("export_path") or "")
        if not path.startswith("/en/documents/") or ".." in path:
            raise SourcePackError("invalid_mapping", "ema-medicines names the export file path under /en/documents/")
        return [{"export": path, "products": sorted(numbers)}]

    def _item(self, spec):
        status, raw = self._get(f"{self.base}{spec['export']}")
        if status >= 400:
            raise SourcePackError("schema_drift", f"EMA export returned HTTP {status}")
        records, missing = parse_ema_medicines(_json(raw, "EMA"), product_numbers=spec["products"])
        parsed = {"records": records}
        return [self._page_record("medicines-export", "EMA medicines export (selected products)",
                                  f"{self.base}{spec['export']}", parsed,
                                  {"products_missing": missing})], "returned" if not missing else "partial"


ADAPTERS = {"ctgov": CtgovAdapter, "ctis": CtisAdapter, "eu-ctr": EuctrAdapter, "openfda": OpenfdaAdapter,
            "ema-medicines": EmaAdapter}
CONNECTOR_PROVIDER = {"ctgov": "ctgov", "ctis": "ctis", "eu-ctr": "euctr", "openfda": "openfda",
                      "ema-medicines": "ema"}


def fixture_transport(pages: Sequence[Mapping[str, Any]]) -> Callable[..., Mapping[str, Any]]:
    """Pages are keyed by URL path plus query; bodies are JSON values or text."""

    by_key = {page["request"]: page for page in pages}

    def transport(*, url, params, headers, timeout, **_):
        del params, headers, timeout
        parts = urlsplit(url)
        page = by_key.get(parts.path + (f"?{parts.query}" if parts.query else ""))
        if page is None:
            return {"status": 404, "headers": {}, "content": b""}
        body = page.get("body")
        content = json.dumps(body).encode() if isinstance(body, (dict, list)) else str(body or "").encode()
        return {"status": int(page.get("status", 200)), "headers": dict(page.get("headers") or {}),
                "content": content}

    return transport


def replay_native_fixture(source: Mapping[str, Any], fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    adapter = ADAPTERS[source["connector"]](source, transport=fixture_transport(list(fixture["native_pages"])))
    records: list[dict[str, Any]] = []
    cursor = None
    for _ in range(int(source["budgets"]["max_pages"])):
        page = adapter.fetch_page({"operation": min(source["operations"]), "parameters": {}}, cursor=cursor)
        records.extend(dict(item) for item in page.records)
        cursor = page.next_cursor
        if cursor is None:
            break
    return records


__all__ = [
    "ADAPTERS", "CONTRACT", "LIVE_VERIFICATION", "PROVIDER_CONTRACTS", "PROVIDER_HOSTS", "classify_identifier",
    "find_identifiers", "fixture_transport", "parse_ctgov_history", "parse_ctgov_search", "parse_ctgov_study",
    "parse_ctgov_versions", "parse_ctis_trial", "parse_ema_medicines", "parse_euctr_text",
    "parse_openfda_approvals", "parse_openfda_event_counts", "parse_openfda_labels", "parse_prospero_export",
    "replay_native_fixture",
]
