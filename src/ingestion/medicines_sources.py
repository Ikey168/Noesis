"""Bounded acquisition of medicines-regulation records for the Clinical Evidence pack (#2214, MR03-MR07).

Access decisions (MR01, ``docs/roadmaps/clinical-medicines-source-audit.md``) are
recorded in :data:`PROVIDER_CONTRACTS` and merged into
:data:`src.ingestion.clinical_providers.PROVIDER_CONTRACTS`. Every acquisition
is a native connector of the ``clinical-evidence`` source pack, so runs go
through the shared source-pack runtime (budgets, cursors bound to the pinned
selection, page receipts with response hashes, licence acceptance, schedules):

* ``medicines`` connector, provider ``ema-epar`` - the EMA medicines data
  export (authorisation status, dates, EPAR revision number, latest procedure)
  plus the product-information (SmPC) document and, for a withdrawn medicine,
  its public statement, per pinned EMA product number;
* ``medicines`` connector, provider ``dailymed`` - DailyMed web services: the
  SPL version history (``/services/v2/spls/{setid}/history.json``) and the
  current SPL document (``/services/v2/spls/{setid}.xml``) with sections keyed
  by LOINC code, per pinned set id;
* ``medicines`` connector, provider ``fda-dsc`` - pinned FDA Drug Safety
  Communication pages (HTML): issue date, dated updates, named substances and
  products, quoted verbatim with locators;
* the existing ``openfda`` connector (``src/ingestion/clinical_providers.py``)
  with its ``drugsfda-submissions`` endpoint - Drugs@FDA applications,
  products and submissions through the same openFDA client and API-key
  handling (:func:`parse_drugsfda_submissions`); there is no second openFDA
  client;
* :class:`RxNavClient` - bounded RxNav lookups for identity resolution (MR07),
  each request receipted with the RxNorm release used.

Parsers are fail-closed and never paraphrase: label and communication text is
kept verbatim with a locator; dosing sections are listed as omitted and their
text is not retained. Nothing here schedules anything or gives advice.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from src.ingestion.source_packs import SourcePackError
from src.kb.clinical_medicines import record

EXTRACTOR = "medicines-sources:1.0.0"
FIXTURE_SECRET = None
MAX_ITEMS = 50
MAX_TEXT = 20000
MAX_LOOKUPS = 50
PROVIDER_HOSTS = {
    "ema-epar": {"www.ema.europa.eu"},
    "dailymed": {"dailymed.nlm.nih.gov"},
    "fda-dsc": {"www.fda.gov"},
    "rxnorm": {"rxnav.nlm.nih.gov"},
    "drugs-at-fda": {"api.fda.gov"},
}
EMA_ATTRIBUTION = "Source: European Medicines Agency (EMA), https://www.ema.europa.eu; reproduced as published."
NLM_ATTRIBUTION = "Source: DailyMed, U.S. National Library of Medicine; label content as submitted to FDA."
FDA_ATTRIBUTION = "Source: U.S. Food and Drug Administration, Drug Safety Communication (public domain)."
PROVIDER_CONTRACTS = {
    "ema-epar": {
        "documentation": "https://www.ema.europa.eu/en/medicines/download-medicine-data",
        "access": "published medicines data export (JSON) filtered to pinned EMA product numbers; the EPAR product "
                  "information (SmPC, Annex I) and, for a withdrawn medicine, its public statement, per pinned path "
                  "under /en/documents/",
        "authentication": "none",
        "rate_limits": "none documented; bounded to at most 50 pinned products and 3 requests per product",
        "pagination": "one pinned product per page (the export is fetched once per run)",
        "cadence": "daily at most; EMA regenerates the export and republishes product information on each "
                   "procedure affecting it",
        "terms": "EMA legal notice: reproduction authorised provided the source is acknowledged; attribution kept on "
                 "every record",
        "retained_evidence": "raw export and document bytes (sha256 in the page receipt), row pointer per field, SmPC "
                             "section numbers and line locators",
        "identifiers": ["EMA product number (EMEA/H/C/......)", "procedure number (EMEA/H/C/....../II/....)",
                        "INN / active substance"],
        "cross_references": "SmPC text may cite trials (NCT, EudraCT) and publications; cited only as written",
        "revision_behaviour": "the export carries a revision number and last-updated date per medicine; the product "
                              "information in force is published at a stable URL, so earlier revisions are those "
                              "acquired earlier (retained), keyed by EMA product number and revision",
        "status": "implemented",
        "unavailable_fallback": "record the provider failure; keep the last revision marked stale",
    },
    "drugs-at-fda": {
        "documentation": "https://open.fda.gov/apis/drug/drugsfda/",
        "access": "reuses the existing openfda provider (src/ingestion/clinical_providers.py, OpenfdaAdapter, "
                  "/drug/drugsfda.json) with the drugsfda-submissions endpoint; no second openFDA client",
        "authentication": "the openfda provider's optional API key (NOESIS_OPENFDA_API_KEY); never stored",
        "rate_limits": "openFDA limits (240/min; 1,000/day without a key)",
        "pagination": "one pinned generic name per page, limit 5 applications",
        "cadence": "weekly at most (openFDA meta.last_updated)",
        "terms": "openFDA Terms of Service; openFDA's disclaimer stored on every record",
        "retained_evidence": "raw JSON, JSON pointers per submission and product, meta.disclaimer",
        "identifiers": ["NDA/ANDA/BLA application number", "submission type and number", "product number"],
        "cross_references": "application number joins SPL labels (DailyMed approval id) to Drugs@FDA",
        "revision_behaviour": "each submission (ORIG, SUPPL incl. LABELING) is a dated record keyed by application "
                              "and submission type/number; marketing status is current-only, so a changed status is "
                              "a new record and earlier statuses stay as history",
        "status": "implemented",
        "reuses": "openfda",
        "unavailable_fallback": "record the provider failure; keep last records marked stale",
    },
    "dailymed": {
        "documentation": "https://dailymed.nlm.nih.gov/dailymed/app-support-web-services.cfm",
        "access": "DailyMed web services v2: /dailymed/services/v2/spls/{setid}/history.json (version list) and "
                  "/dailymed/services/v2/spls/{setid}.xml (current SPL document)",
        "authentication": "none",
        "rate_limits": "none documented; bounded to at most 50 pinned set ids and 2 requests per set id",
        "pagination": "one pinned set id per page",
        "cadence": "weekly at most; a new SPL version is published on each label update",
        "terms": "NLM terms and conditions: DailyMed content is public; cite DailyMed and the set id; no NLM "
                 "endorsement implied",
        "retained_evidence": "raw XML/JSON (sha256), set id, version number and effective time, LOINC section code, "
                             "title and XPath locator per section",
        "identifiers": ["SPL set id", "SPL version number", "NDA/ANDA/BLA approval id", "LOINC section codes"],
        "cross_references": "the SPL approval id is the Drugs@FDA application number",
        "revision_behaviour": "history.json lists every published version with its date; the web services serve the "
                              "current version's document only, so each version's text is the one acquired while it "
                              "was current (earlier versions are retained, never rewritten); versions listed but "
                              "never acquired are reported as text-not-acquired",
        "status": "implemented",
        "unavailable_fallback": "record the provider failure; keep last revisions marked stale",
    },
    "fda-dsc": {
        "documentation": "https://www.fda.gov/drugs/drug-safety-and-availability/drug-safety-communications",
        "access": "pinned Drug Safety Communication pages (HTML) under /drugs/drug-safety-and-availability/; the "
                  "index page is HTML with no documented feed, so communications are pinned by the operator rather "
                  "than discovered",
        "authentication": "none",
        "rate_limits": "none documented; bounded to at most 50 pinned pages, one request each",
        "pagination": "one pinned communication per page",
        "cadence": "weekly at most; FDA adds dated updates to an existing communication page",
        "terms": "US government work (public domain); cite FDA and the page URL",
        "retained_evidence": "raw HTML (sha256), paragraph and table-row locators for every quote, update and name",
        "identifiers": ["communication URL", "issue date", "named generic and brand names as published"],
        "cross_references": "communications may cite publications or FAERS; cited only as written",
        "revision_behaviour": "the issue date is published on the page; an update is a dated paragraph added to the "
                              "same page and becomes a new revision of the same record (the original is kept)",
        "status": "implemented",
        "unavailable_fallback": "record the provider failure; keep the last revision marked stale",
    },
    "rxnorm": {
        "documentation": "https://lhncbc.nlm.nih.gov/RxNav/APIs/",
        "access": "RxNav REST: /REST/version.json, /REST/rxcui.json?name=&search=0 (exact), "
                  "/REST/rxcui/{rxcui}/properties.json and /related.json?tty=IN; used by identity resolution",
        "authentication": "none",
        "rate_limits": "20 requests per second per IP (NLM guidance); bounded to 50 names per resolution run",
        "pagination": "none",
        "cadence": "monthly RxNorm releases; the release version is stored on every match",
        "terms": "NLM RxNav terms; RxNorm is a UMLS-derived work (NLM-produced SAB content usable without a UMLS "
                 "licence); cite RxNorm and the release",
        "retained_evidence": "request path and response sha256 per lookup, RxNorm release version, RxCUI and term type",
        "identifiers": ["RxCUI", "term type (IN, BN, SCD, SBD)"],
        "cross_references": "US products only; EU products are matched by active substance through reviewed matches",
        "status": "implemented",
        "unavailable_fallback": "report the names that could not be looked up as unmatched; no match is guessed",
    },
}
# The bounded medicine set (MR01): EU and US, active substances and products, with a withdrawn product and a
# product with a safety communication. All identifiers are fictional placeholders in the offline fixtures.
BOUNDED_SET = {
    "substances": ["noetiglutide", "fixturamab"],
    "eu_products": {"EMEA/H/C/009001": "Noetiglu (noetiglutide), authorised",
                    "EMEA/H/C/009002": "Fixturamab (fixturamab), withdrawn"},
    "us_products": {"NDA299001": "NOETIGLU (noetiglutide)"},
    "spl_set_ids": ["9a1f0c3e-0000-4000-8000-000000009001"],
    "safety_communications": ["noetiglutide pancreatitis communication (fixture) with one update"],
}
LIVE_VERIFICATION = {provider: {"status": "unverified-live",
                                "note": "no dated live run of the medicines sources yet (#2429); fixture evidence only"}
                     for provider in PROVIDER_CONTRACTS}


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
    """ISO date from provider date strings (YYYYMMDD, YYYY-MM-DD, M-D-YYYY, 'Apr 15, 2025'); else unknown."""
    if not value:
        return None
    text = str(value).strip()
    if re.fullmatch(r"\d{8}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    match = re.match(r"^(\d{4}-\d{2}(?:-\d{2})?)", text)
    if match:
        return match.group(1)
    match = re.fullmatch(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})", text)
    if match:
        return f"{match.group(3)}-{int(match.group(1)):02d}-{int(match.group(2)):02d}"
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _json(raw: bytes, provider: str) -> Any:
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise SourcePackError("schema_drift", f"{provider} returned non-JSON content") from exc


# ------------------------------------------------------------------ cited references

_REFERENCE_PATTERNS = [
    ("nct", re.compile(r"\bNCT\d{8}\b")),
    ("eu-ct", re.compile(r"\b\d{4}-\d{6}-\d{2}-\d{2}\b")),
    ("eudract", re.compile(r"\b\d{4}-\d{6}-\d{2}\b(?!-\d)")),
    ("pmid", re.compile(r"\bPMID:?\s*(\d{1,9})\b")),
    ("doi", re.compile(r"\b(10\.\d{4,9}/[^\s;,)]+[^\s;,.)])")),
]


def cited_references(text, locator):
    """Identifiers the regulator's text itself cites, each with the citing sentence and a locator."""
    found = []
    for sentence in re.split(r"(?<=[.;])\s+", str(text or "")):
        for kind, pattern in _REFERENCE_PATTERNS:
            for match in pattern.finditer(sentence):
                value = match.group(1) if match.groups() else match.group(0)
                entry = {"kind": kind, "value": value.lower() if kind == "doi" else value,
                         "citing_text": sentence.strip()[:4000], "locator": dict(locator)}
                if entry not in found:
                    found.append(entry)
    return found


# ------------------------------------------------------------------ EMA (MR03)

_EMA_STATUS = {"authorised": "authorised", "withdrawn": "withdrawn", "refused": "refused", "suspended": "suspended",
               "not renewed": "not-renewed", "revoked": "revoked", "lapsed": "lapsed"}
_EMA_EVENTS = (  # export field -> (event kind, status)
    ("marketing_authorisation_date", "grant", "authorised"),
    ("date_of_refusal_of_marketing_authorisation", "refusal", "refused"),
    ("suspension_of_marketing_authorisation_date", "suspension", "suspended"),
    ("withdrawal_of_marketing_authorisation_date", "withdrawal", "withdrawn"),
)
_EMA_STATUS_EVENT = {"not-renewed": "not-renewed", "revoked": "revocation", "lapsed": "lapse"}
SMPC_OMITTED = {"4.2": "posology and method of administration", "4.9": "overdose"}
_SMPC_TOP = re.compile(r"^(\d{1,2})\.\s+([A-Z][A-Z0-9 ,/()'&-]+)$")
_SMPC_SUB = re.compile(r"^(\d{1,2}\.\d{1,2})\.?\s+(\S.*)$")


def ema_row(payload, product_number):
    rows = payload.get("data") if isinstance(payload, Mapping) else payload
    if not isinstance(rows, list):
        raise SourcePackError("schema_drift", "EMA export is not a list of medicines")
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping) or not {"name_of_medicine", "ema_product_number",
                                                  "medicine_status"} <= set(row):
            raise SourcePackError("schema_drift", "EMA export rows lack name/product number/status")
        if str(row["ema_product_number"]).strip() == product_number:
            return index, row
    return None, None


def parse_smpc_text(text, *, url, product_number):
    """SmPC (Annex I) sections by section number, verbatim, with line locators; dosing sections omitted."""
    lines = str(text or "").splitlines()
    start = next((i for i, line in enumerate(lines) if "SUMMARY OF PRODUCT CHARACTERISTICS" in line.upper()), -1) + 1
    end = next((i for i, line in enumerate(lines) if i >= start and re.match(r"^\s*ANNEX II\b", line)), len(lines))
    headings = []
    for number in range(start, end):
        line = lines[number].strip()
        top, sub = _SMPC_TOP.match(line), _SMPC_SUB.match(line)
        if sub:
            headings.append((number, sub.group(1), sub.group(2).strip()))
        elif top:
            headings.append((number, top.group(1), top.group(2).strip()))
    sections, omitted = [], []
    for position, (number, code, title) in enumerate(headings):
        stop = headings[position + 1][0] if position + 1 < len(headings) else end
        body = [line.strip() for line in lines[number + 1:stop] if line.strip()]
        if code in SMPC_OMITTED:
            omitted.append({"code": code, "code_system": "smpc", "title": title,
                            "reason": "dosing text is outside the pack's non-advice boundary; not retained"})
            continue
        if not body:
            continue
        sections.append({"code": code, "code_system": "smpc", "title": title, "text": "\n".join(body)[:200000],
                         "locator": {"url": url, "lines": [number + 1, stop], "section": code}})
    if not sections:
        raise SourcePackError("schema_drift", f"no SmPC sections found in the product information of {product_number}")
    return sections, omitted


def statement_reason(text, *, url):
    """The withdrawal reason quoted from an EMA public statement: its first paragraph mentioning the reason."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", str(text or "")) if p.strip()]
    for pattern in (r"\breason", r"\bwithdr[ae]w\w*\b.*\.\s*$"):
        for index, paragraph in enumerate(paragraphs):
            if re.search(pattern, paragraph, re.I | re.S):
                return {"text": " ".join(paragraph.split())[:4000], "locator": {"url": url, "paragraph": index + 1}}
    return None


def parse_ema_epar(payload, *, product_number, export_url, pi_text=None, pi_url=None, statement_text=None,
                   statement_url=None):
    """Product, dated authorisation events and the SmPC label revision of one EMA product."""
    index, row = ema_row(payload, product_number)
    if row is None:
        return []
    pointer = f"/data/{index}"
    common = {"provider": "ema", "native_id": product_number, "jurisdiction": "EU", "authority": "EMA",
              "attribution": EMA_ATTRIBUTION}
    url = row.get("medicine_url") if str(row.get("medicine_url") or "").startswith("https://") else export_url
    revision = str(row["revision_number"]) if row.get("revision_number") is not None else None
    revised = _date(row.get("last_updated_date"))
    status_text = str(row.get("medicine_status") or "")
    normalized = _EMA_STATUS.get(status_text.casefold(), "unknown")
    substance = row.get("active_substance") or row.get("inn_common_name")
    substances = [{"name": s.strip(), "role": "active substance", "locator": {"json_pointer": pointer +
                                                                              "/active_substance"}}
                  for s in re.split(r"[;/]", str(substance or "")) if s.strip()]
    records = [record(
        "medicinal-product", **common, source_url=url,
        native_version={"version": revision, "date": revised, "basis": "epar-revision"},
        name=str(row["name_of_medicine"]), active_substances=substances,
        brand_names=[{"name": str(row["name_of_medicine"]), "locator": {"json_pointer": pointer + "/name_of_medicine"}}],
        holder=row.get("marketing_authorisation_developer_applicant_holder"),
        identifiers=[{"kind": "ema-product", "value": product_number,
                      "locator": {"json_pointer": pointer + "/ema_product_number"}}],
        status={"native": status_text, "normalized": normalized,
                "locator": {"json_pointer": pointer + "/medicine_status"}})]
    product = {"provider": "ema", "native_id": product_number}
    procedure = {"kind": "product-number", "number": product_number}
    reason = statement_reason(statement_text, url=statement_url) if statement_text else None
    dated = set()
    for field, kind, status in _EMA_EVENTS:
        effective = _date(row.get(field))
        if not effective:
            continue
        dated.add(status)
        event = {"kind": kind, "native_status": status_text if status == normalized else status, "status": status,
                 "effective_date": effective, "locator": {"json_pointer": f"{pointer}/{field}"}}
        if kind == "withdrawal" and reason:
            event["reason"] = reason
        records.append(record("marketing-authorisation", **common, source_url=url,
                              native_version={"version": None, "date": effective, "basis": "epar-revision"},
                              product=product, procedure=procedure, event=event))
    if normalized not in dated and normalized not in {"authorised", "unknown"}:
        records.append(record(
            "marketing-authorisation", **common, source_url=url,
            native_version={"version": revision, "date": revised, "basis": "epar-revision"},
            product=product, procedure=procedure,
            event={"kind": _EMA_STATUS_EVENT.get(normalized, "status"), "native_status": status_text,
                   "status": normalized, "effective_date": None,
                   "locator": {"json_pointer": pointer + "/medicine_status"},
                   **({"reason": reason} if reason else {})}))
    latest = str(row.get("latest_procedure_affecting_product_information") or "").strip()
    if latest:
        records.append(record(
            "marketing-authorisation", **common, source_url=url,
            native_version={"version": revision, "date": revised, "basis": "epar-revision"},
            product=product, procedure={"kind": "procedure", "number": latest},
            event={"kind": "variation", "native_status": latest, "status": "authorised", "effective_date": revised,
                   "locator": {"json_pointer": pointer + "/latest_procedure_affecting_product_information"}}))
    if pi_text is not None and revision is not None:
        sections, omitted = parse_smpc_text(pi_text, url=pi_url, product_number=product_number)
        references = [ref for section in sections for ref in cited_references(section["text"], section["locator"])]
        records.append(record(
            "label-revision", **common, source_url=pi_url,
            native_version={"version": revision, "date": revised, "basis": "epar-revision"},
            product=product, document={"kind": "smpc", "id": f"{product_number}:smpc", "version": revision,
                                       "effective_date": revised, "revision_date": revised, "url": pi_url,
                                       "title": f"{row['name_of_medicine']}: Summary of product characteristics"},
            sections=sections, omitted_sections=omitted, cited_references=references, active_substances=substances))
    return records


# ------------------------------------------------------------------ Drugs@FDA (MR04)

_FDA_SUBMISSION_STATUS = {"AP": "approved", "TA": "pending"}


def parse_drugsfda_submissions(payload):
    """Drugs@FDA applications as a product, one dated record per submission and per product marketing status."""
    from src.ingestion.clinical_providers import _openfda_meta

    disclaimer = _openfda_meta(payload)
    results = payload.get("results")
    if not isinstance(results, list):
        raise SourcePackError("schema_drift", "Drugs@FDA response lacks results")
    records = []
    for index, item in enumerate(results):
        number = str(item.get("application_number") or "")
        if not re.fullmatch(r"(NDA|ANDA|BLA)\d{6}", number):
            raise SourcePackError("schema_drift", "Drugs@FDA result lacks an application number")
        pointer = f"/results/{index}"
        source_url = f"https://api.fda.gov/drug/drugsfda.json?search=application_number:{number}"
        common = {"provider": "openfda", "native_id": number, "jurisdiction": "US", "authority": "FDA",
                  "source_url": source_url, "disclaimer": disclaimer,
                  "attribution": "Source: openFDA Drugs@FDA (U.S. Food and Drug Administration)."}
        products = item.get("products") or []
        ingredients = sorted({(a.get("name") or "").strip() for p in products for a in p.get("active_ingredients") or []
                              if (a.get("name") or "").strip()})
        brands = sorted({p.get("brand_name") for p in products if p.get("brand_name")})
        statuses = sorted({p.get("marketing_status") for p in products if p.get("marketing_status")})
        normalized = ("discontinued" if statuses and all(s.casefold() == "discontinued" for s in statuses)
                      else "marketed" if statuses else "unknown")
        records.append(record(
            "medicinal-product", **common,
            native_version={"version": None, "date": disclaimer["last_updated"], "basis": "observation"},
            name=brands[0] if brands else number,
            active_substances=[{"name": name, "role": "active ingredient",
                                "locator": {"json_pointer": f"{pointer}/products"}} for name in ingredients],
            brand_names=[{"name": name, "locator": {"json_pointer": f"{pointer}/products"}} for name in brands],
            holder=item.get("sponsor_name"),
            identifiers=[{"kind": "fda-application", "value": number,
                          "locator": {"json_pointer": f"{pointer}/application_number"}}],
            products=[{"product_number": p.get("product_number"), "brand_name": p.get("brand_name"),
                       "marketing_status": p.get("marketing_status"),
                       "locator": {"json_pointer": f"{pointer}/products/{i}"}} for i, p in enumerate(products)],
            status={"native": "; ".join(statuses) or None, "normalized": normalized,
                    "locator": {"json_pointer": f"{pointer}/products"}}))
        product = {"provider": "openfda", "native_id": number}
        procedure = {"kind": "application", "number": number}
        for position, submission in enumerate(item.get("submissions") or []):
            stype, snumber = submission.get("submission_type"), submission.get("submission_number")
            if not stype or snumber is None:
                raise SourcePackError("schema_drift", "Drugs@FDA submission lacks type or number")
            code = str(submission.get("submission_class_code") or "")
            kind = ("grant" if stype == "ORIG" and submission.get("submission_status") == "AP"
                    else "labeling-revision" if code.upper() == "LABELING" else "supplement")
            effective = _date(submission.get("submission_status_date"))
            locator = {"json_pointer": f"{pointer}/submissions/{position}"}
            records.append(record(
                "marketing-authorisation", **common,
                native_version={"version": f"{stype}-{snumber}", "date": effective, "basis": "submission"},
                product=product, procedure=procedure,
                event={"kind": kind, "native_status": submission.get("submission_status"),
                       "status": _FDA_SUBMISSION_STATUS.get(str(submission.get("submission_status")), "unknown"),
                       "effective_date": effective, "locator": locator},
                submission={"type": stype, "number": str(snumber), "status": submission.get("submission_status"),
                            "status_date": effective, "class_code": code or None,
                            "class_description": submission.get("submission_class_code_description"),
                            "locator": locator}))
        for position, product_row in enumerate(products):
            status = str(product_row.get("marketing_status") or "")
            if not status:
                continue
            discontinued = status.casefold() == "discontinued"
            records.append(record(
                "marketing-authorisation", **common,
                native_version={"version": None, "date": disclaimer["last_updated"], "basis": "observation"},
                product=product, procedure=procedure,
                event={"kind": "discontinuation" if discontinued else "marketing-status",
                       "native_status": f"product {product_row.get('product_number')}: {status}",
                       "status": "discontinued" if discontinued else "marketed", "effective_date": None,
                       "locator": {"json_pointer": f"{pointer}/products/{position}/marketing_status"}}))
    return records


# ------------------------------------------------------------------ DailyMed SPL (MR05)

HL7 = "{urn:hl7-org:v3}"
LOINC_SYSTEM = "2.16.840.1.113883.6.1"
SPL_OMITTED = {"34068-7": "dosage and administration", "43678-2": "dosage forms and strengths",
               "34088-5": "overdosage"}


def parse_spl_history(payload, set_id):
    data = payload.get("data") if isinstance(payload, Mapping) else None
    history = (data or {}).get("history") if isinstance(data, Mapping) else None
    if not isinstance(history, list):
        raise SourcePackError("schema_drift", "DailyMed history response lacks data.history")
    versions = []
    for index, entry in enumerate(history):
        if not isinstance(entry, Mapping) or entry.get("spl_version") in (None, ""):
            raise SourcePackError("schema_drift", "DailyMed history entry lacks spl_version")
        versions.append({"version": str(entry["spl_version"]), "published_date": _date(entry.get("published_date")),
                         "locator": {"json_pointer": f"/data/history/{index}"}})
    return {"set_id": set_id, "versions": sorted(versions, key=lambda v: int(v["version"])
                                                 if v["version"].isdigit() else 0)}


def _text_of(element):
    parts = []
    for node in element.iter():
        if node is not element and node.tag == HL7 + "section":
            continue
        if node.text and node.text.strip():
            parts.append(node.text.strip())
        if node is not element and node.tail and node.tail.strip():
            parts.append(node.tail.strip())
    return " ".join(" ".join(parts).split())


def parse_spl_xml(raw, *, set_id, url):
    """One SPL document as a label revision: set id, version, effective time, LOINC-keyed sections."""
    import defusedxml.ElementTree as ET

    try:
        root = ET.fromstring(raw)
    except Exception as exc:  # noqa: BLE001 - any parser failure is schema drift
        raise SourcePackError("schema_drift", "DailyMed SPL is not well-formed XML") from exc
    found = root.find(HL7 + "setId")
    version = root.find(HL7 + "versionNumber")
    effective = root.find(HL7 + "effectiveTime")
    if found is None or found.get("root") != set_id or version is None or not version.get("value"):
        raise SourcePackError("schema_drift", "SPL lacks the pinned set id or a version number")
    title_node = root.find(HL7 + "title")
    title = _clip("".join(title_node.itertext())) if title_node is not None else None
    sections, omitted = [], []

    def walk(element, path, parent):
        position = 0
        for component in element.findall(HL7 + "component"):
            section = component.find(HL7 + "section")
            if section is None:
                continue
            position += 1
            here = f"{path}/component[{position}]/section"
            code = section.find(HL7 + "code")
            code_value = code.get("code") if code is not None and code.get("codeSystem") == LOINC_SYSTEM else None
            title_el = section.find(HL7 + "title")
            heading = _clip("".join(title_el.itertext())) if title_el is not None else None
            if code is not None and not heading:
                heading = code.get("displayName")
            if code_value in SPL_OMITTED:
                omitted.append({"code": code_value, "code_system": "loinc", "title": heading,
                                "reason": "dosing text is outside the pack's non-advice boundary; not retained"})
            else:
                text_el = section.find(HL7 + "text")
                text = _text_of(text_el) if text_el is not None else ""
                if text:
                    entry = {"code": code_value, "code_system": "loinc", "title": heading, "text": text[:200000],
                             "locator": {"url": url, "xpath": here, "section_id": section.get("ID")}}
                    if parent:
                        entry["parent"] = parent
                    sections.append(entry)
            walk(section, here, code_value or parent)

    body = root.find(f"{HL7}component/{HL7}structuredBody")
    if body is None:
        raise SourcePackError("schema_drift", "SPL lacks a structured body")
    walk(body, "/document/component/structuredBody", None)
    substances = sorted({" ".join("".join(n.itertext()).split()) for n in root.iter(HL7 + "activeIngredientSubstance")
                         for n in n.findall(HL7 + "name")} - {""})
    brands = sorted({" ".join("".join(n.itertext()).split()) for m in root.iter(HL7 + "manufacturedProduct")
                     for n in m.findall(HL7 + "name")} - {""})
    approvals = sorted({i.get("extension") for a in root.iter(HL7 + "approval") for i in a.findall(HL7 + "id")
                        if i.get("extension")})
    return {"set_id": set_id, "version": str(version.get("value")),
            "effective_date": _date((effective.get("value") or "")[:8]) if effective is not None else None,
            "title": title, "sections": sections, "omitted": omitted, "substances": substances, "brands": brands,
            "applications": approvals}


def spl_records(parsed, history, *, url):
    set_id = parsed["set_id"]
    listed = next((v for v in history["versions"] if v["version"] == parsed["version"]), None)
    common = {"provider": "dailymed", "native_id": set_id, "jurisdiction": "US", "authority": "FDA",
              "attribution": NLM_ATTRIBUTION}
    locator = {"url": url, "xpath": "/document"}
    references = [ref for section in parsed["sections"] for ref in cited_references(section["text"],
                                                                                    section["locator"])]
    return [record(
        "label-revision", **common, source_url=url,
        native_version={"version": parsed["version"], "date": parsed["effective_date"], "basis": "spl-version"},
        product={"provider": "dailymed", "native_id": set_id},
        document={"kind": "spl", "id": set_id, "version": parsed["version"],
                  "effective_date": parsed["effective_date"],
                  "revision_date": (listed or {}).get("published_date"), "url": url, "title": parsed["title"]},
        sections=parsed["sections"], omitted_sections=parsed["omitted"], cited_references=references,
        active_substances=[{"name": s, "role": "active ingredient", "locator": locator} for s in parsed["substances"]],
        brand_names=[{"name": b, "locator": locator} for b in parsed["brands"]])]


# ------------------------------------------------------------------ FDA Drug Safety Communications (MR06)


class _Page(HTMLParser):
    """Headings, paragraphs and table rows of an FDA page, in order, with their index."""

    BLOCKS = {"h1", "h2", "h3", "p", "li"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks: list[dict[str, Any]] = []
        self.rows: list[list[str]] = []
        self._tag = None
        self._buffer: list[str] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        del attrs
        if tag in self.BLOCKS and self._cell is None:
            self._flush()
            self._tag = tag
        elif tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append(" ".join(" ".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None
        elif tag == self._tag:
            self._flush()

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)
        elif self._tag:
            self._buffer.append(data)

    def _flush(self):
        text = " ".join(" ".join(self._buffer).split())
        if self._tag and text:
            self.blocks.append({"tag": self._tag, "text": text, "index": len(self.blocks)})
        self._tag, self._buffer = None, []

    def close(self):
        super().close()
        self._flush()


_DSC_ISSUED = re.compile(r"^\[?(\d{1,2}-\d{1,2}-\d{4})\]?\s+FDA Drug Safety Communication", re.I)
_DSC_UPDATE = re.compile(r"^\[?(\d{1,2}-\d{1,2}-\d{4})\]?\s*(?:[-:]\s*)?UPDATE\b", re.I)
MAX_QUOTES = 8


def parse_dsc_html(raw, *, url):
    """One Drug Safety Communication: issue date, dated updates, named substances/products, verbatim quotes."""
    page = _Page()
    try:
        page.feed(raw.decode("utf-8") if isinstance(raw, bytes) else str(raw))
        page.close()
    except UnicodeDecodeError as exc:
        raise SourcePackError("schema_drift", "FDA page is not UTF-8 HTML") from exc
    title = next((b["text"] for b in page.blocks if b["tag"] == "h1"), None)
    if not title:
        raise SourcePackError("schema_drift", "Drug Safety Communication page lacks a title (h1)")
    issued, updates, quotes = None, [], []
    for block in page.blocks:
        locator = {"url": url, "block": block["index"], "tag": block["tag"]}
        match = _DSC_ISSUED.match(block["text"])
        if match and issued is None:
            issued = _date(match.group(1))
            continue
        match = _DSC_UPDATE.match(block["text"])
        if match:
            updates.append({"date": _date(match.group(1)), "text": block["text"][:20000], "locator": locator})
            continue
        if block["tag"] in {"p", "li"} and len(quotes) < MAX_QUOTES:
            quotes.append({"text": block["text"][:20000], "locator": locator})
    substances, products = [], []
    header = next((i for i, row in enumerate(page.rows) if any("generic name" in c.casefold() for c in row)), None)
    if header is not None:
        columns = [c.casefold() for c in page.rows[header]]
        generic = next(i for i, c in enumerate(columns) if "generic name" in c)
        brand = next((i for i, c in enumerate(columns) if "brand name" in c), None)
        for number, row in enumerate(page.rows[header + 1:], start=header + 1):
            locator = {"url": url, "table_row": number}
            if generic < len(row) and row[generic]:
                substances.append({"name": row[generic], "role": "generic name", "locator": locator})
            if brand is not None and brand < len(row) and row[brand]:
                products.append({"name": row[brand], "role": "brand name", "locator": locator})
    references = [ref for q in quotes + updates for ref in cited_references(q["text"], q["locator"])]
    return {"title": title, "issued": issued, "updates": sorted(updates, key=lambda u: u["date"] or ""),
            "named_substances": substances, "named_products": products, "quotes": quotes,
            "cited_references": references}


def dsc_records(parsed, *, url, native_id):
    latest = max([u["date"] for u in parsed["updates"] if u["date"]] + ([parsed["issued"]] if parsed["issued"]
                                                                          else []), default=None)
    return [record(
        "safety-communication", provider="fda-dsc", native_id=native_id, jurisdiction="US", authority="FDA",
        source_url=url, attribution=FDA_ATTRIBUTION,
        native_version={"version": None, "date": latest, "basis": "publication-date"},
        title=parsed["title"], issued=parsed["issued"], updates=parsed["updates"],
        named_substances=parsed["named_substances"], named_products=parsed["named_products"], quotes=parsed["quotes"],
        cited_references=parsed["cited_references"])]


# ------------------------------------------------------------------ adapter


def _clinical_base():
    from src.ingestion.clinical_providers import _ClinicalAdapter

    return _ClinicalAdapter


class MedicinesAdapter(_clinical_base()):
    """The ``medicines`` native connector: one page per pinned product, set id or communication."""

    connector = "medicines"

    def __init__(self, source, *, transport=None, secret=None):
        config = dict(source.get("medicines") or {})
        super().__init__({**source, "clinical": config}, transport=transport, secret=secret)
        self.definition["medicines"] = {"provider": config.get("provider")}
        self._export = None

    def _work(self):
        provider = self.config.get("provider")
        if provider == "ema-epar":
            path = str(self.config.get("export_path") or "")
            if not path.startswith("/en/documents/") or ".." in path:
                raise SourcePackError("invalid_mapping", "ema-epar names the export file path under /en/documents/")
            items = []
            for product in self.config.get("products") or []:
                number = str(product.get("product_number") or "")
                if not re.fullmatch(r"EMEA/H/C/\d{6}", number):
                    raise SourcePackError("invalid_mapping", "ema-epar pins EMA product numbers")
                for key in ("product_information_path", "public_statement_path"):
                    value = product.get(key)
                    if value is not None and (not str(value).startswith("/en/documents/") or ".." in str(value)):
                        raise SourcePackError("invalid_mapping", f"{key} is a path under /en/documents/")
                items.append({"product_number": number, **{k: product[k] for k in (
                    "product_information_path", "public_statement_path") if product.get(k)}})
            return items
        if provider == "dailymed":
            ids = [str(v) for v in self.config.get("set_ids") or []]
            if any(not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", v) for v in ids):
                raise SourcePackError("invalid_mapping", "dailymed pins SPL set ids (UUIDs)")
            return ids
        if provider == "fda-dsc":
            paths = [str(v) for v in self.config.get("communications") or []]
            if any(not v.startswith("/drugs/drug-safety-and-availability/") or ".." in v for v in paths):
                raise SourcePackError("invalid_mapping", "fda-dsc pins pages under /drugs/drug-safety-and-availability/")
            return paths
        raise SourcePackError("invalid_mapping", "medicines provider is ema-epar, dailymed or fda-dsc")

    def _item(self, item):
        provider = self.config["provider"]
        if provider == "ema-epar":
            return self._ema(item)
        if provider == "dailymed":
            return self._dailymed(item)
        return self._dsc(item)

    def _text(self, path, accept):
        status, raw = self._get(f"{self.base}{path}", accept=accept)
        if status == 404:
            return None
        if status >= 400:
            raise SourcePackError("schema_drift", f"provider returned HTTP {status}")
        if raw[:5] == b"%PDF-":
            from src.ingestion.connectors.paper.pdf_parser import extract_pdf_text

            text = extract_pdf_text(raw)
            if text is None:
                raise SourcePackError("source_unavailable", "PDF text extraction is not available in this deployment")
            return text
        return raw.decode("utf-8", errors="strict")

    def _ema(self, item):
        export_url = f"{self.base}{self.config['export_path']}"
        if self._export is None:
            status, raw = self._get(export_url)
            if status >= 400:
                raise SourcePackError("schema_drift", f"EMA export returned HTTP {status}")
            self._export = _json(raw, "EMA")
        number, outcome = item["product_number"], "returned"
        pi_text = pi_url = statement = statement_url = None
        if item.get("product_information_path"):
            pi_url = f"{self.base}{item['product_information_path']}"
            pi_text = self._text(item["product_information_path"], "application/pdf, text/plain")
            outcome = "returned" if pi_text is not None else "partial"
        if item.get("public_statement_path"):
            statement_url = f"{self.base}{item['public_statement_path']}"
            statement = self._text(item["public_statement_path"], "application/pdf, text/plain")
        records = parse_ema_epar(self._export, product_number=number, export_url=export_url, pi_text=pi_text,
                                 pi_url=pi_url, statement_text=statement, statement_url=statement_url)
        if not records:
            return [], "not_found"
        return [self._page_record(number, records[0]["name"], records[0]["source_url"], {"records": records},
                                  {"provider": "ema-epar"})], outcome

    def _dailymed(self, set_id):
        status, raw = self._get(f"{self.base}/dailymed/services/v2/spls/{set_id}/history.json")
        if status == 404:
            return [], "not_found"
        if status >= 400:
            raise SourcePackError("schema_drift", f"DailyMed history returned HTTP {status}")
        history = parse_spl_history(_json(raw, "DailyMed"), set_id)
        url = f"{self.base}/dailymed/services/v2/spls/{set_id}.xml"
        status, raw = self._get(url, accept="application/xml")
        if status >= 400:
            raise SourcePackError("schema_drift", f"DailyMed SPL returned HTTP {status}")
        parsed = parse_spl_xml(raw, set_id=set_id, url=url)
        records = spl_records(parsed, history, url=url)
        acquired = {parsed["version"]}
        return [self._page_record(set_id, parsed["title"] or set_id,
                                  f"{self.base}/dailymed/lookup.cfm?setid={quote(set_id)}", {"records": records},
                                  {"provider": "dailymed", "history": history["versions"],
                                   "versions_text_not_acquired": [v["version"] for v in history["versions"]
                                                                  if v["version"] not in acquired]})], "returned"

    def _dsc(self, path):
        url = f"{self.base}{path}"
        status, raw = self._get(url, accept="text/html")
        if status == 404:
            return [], "not_found"
        if status >= 400:
            raise SourcePackError("schema_drift", f"FDA page returned HTTP {status}")
        parsed = parse_dsc_html(raw, url=url)
        native_id = path.rstrip("/").rsplit("/", 1)[-1]
        records = dsc_records(parsed, url=url, native_id=native_id)
        return [self._page_record(native_id, parsed["title"], url, {"records": records},
                                  {"provider": "fda-dsc"})], "returned"


ADAPTERS = {"medicines": MedicinesAdapter}


def fixture_transport(pages: Sequence[Mapping[str, Any]]) -> Callable[..., Mapping[str, Any]]:
    from src.ingestion.clinical_providers import fixture_transport as clinical_fixture_transport

    return clinical_fixture_transport(pages)


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


# ------------------------------------------------------------------ RxNav (MR07)


class RxNavClient:
    """Bounded RxNav lookups with receipts; the RxNorm release is read once and stored on every match."""

    BASE = "https://rxnav.nlm.nih.gov"

    def __init__(self, transport=None, *, max_lookups=MAX_LOOKUPS, timeout_s=30, max_bytes=5_000_000):
        from functools import partial

        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        self.execution = "network" if transport is None else "injected"
        self.transport = transport or partial(HTTPSPageAdapter._request, max_bytes=max_bytes)
        self.max_lookups, self.timeout_s, self.max_bytes = int(max_lookups), timeout_s, max_bytes
        self.receipts: list[dict[str, Any]] = []
        self._release = None
        self._lookups = 0

    def _get(self, path, params=None):
        url = self.BASE + path + ("?" + urlencode(params) if params else "")
        if urlsplit(url).hostname not in PROVIDER_HOSTS["rxnorm"]:
            raise SourcePackError("network_policy", "RxNav requests stay on rxnav.nlm.nih.gov")
        response = self.transport(url=url, params={}, headers={"Accept": "application/json"}, timeout=self.timeout_s)
        status = int(response.get("status", 200))
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > self.max_bytes:
            raise SourcePackError("response_too_large", "RxNav response exceeds its byte limit")
        self.receipts.append({"path": urlsplit(url).path + (f"?{urlsplit(url).query}" if urlsplit(url).query else ""),
                              "status": status, "sha256": _sha(raw), "bytes": len(raw)})
        if status == 429:
            raise SourcePackError("rate_limited", "RxNav rate limit reached")
        if status >= 500:
            raise SourcePackError("source_unavailable", f"RxNav returned HTTP {status}")
        if status >= 400:
            return None
        return _json(raw, "RxNav")

    def release(self):
        if self._release is None:
            payload = self._get("/REST/version.json") or {}
            version = payload.get("version")
            if not version:
                raise SourcePackError("schema_drift", "RxNav version response lacks version")
            self._release = str(version)
        return self._release

    def lookup(self, name):
        """Exact-name RxCUIs of one name with term type and, for non-ingredient concepts, the ingredients."""
        if self._lookups >= self.max_lookups:
            raise SourcePackError("budget_exhausted", f"at most {self.max_lookups} RxNav names per run")
        self._lookups += 1
        payload = self._get("/REST/rxcui.json", {"name": name, "search": "0"}) or {}
        ids = ((payload.get("idGroup") or {}).get("rxnormId")) or []
        concepts = []
        for rxcui in ids[:5]:
            properties = (self._get(f"/REST/rxcui/{rxcui}/properties.json") or {}).get("properties") or {}
            concept = {"rxcui": str(rxcui), "name": properties.get("name") or name, "tty": properties.get("tty"),
                       "ingredients": []}
            if concept["tty"] != "IN":
                related = self._get(f"/REST/rxcui/{rxcui}/related.json", {"tty": "IN"}) or {}
                for group in (related.get("relatedGroup") or {}).get("conceptGroup") or []:
                    for item in group.get("conceptProperties") or []:
                        concept["ingredients"].append({"rxcui": str(item.get("rxcui")), "name": item.get("name")})
            concepts.append(concept)
        return concepts


__all__ = ["ADAPTERS", "BOUNDED_SET", "LIVE_VERIFICATION", "PROVIDER_CONTRACTS", "PROVIDER_HOSTS", "RxNavClient",
           "cited_references", "fixture_transport", "parse_drugsfda_submissions", "parse_dsc_html", "parse_ema_epar",
           "parse_smpc_text", "parse_spl_history", "parse_spl_xml", "replay_native_fixture"]
