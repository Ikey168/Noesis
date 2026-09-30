"""Competition cases and state-aid awards acquisition for Corporate Ownership (#2217, CS01, CS03-CS06).

One native connector, ``competition``, registered in the ``corporate-ownership``
source pack, reads a bounded, declared selection from one publisher per source
and emits ``noesis-competition-record-v1`` records (:mod:`src.kb.competition_records`)
as the authority published them:

* ``ec-case-json`` - the European Commission competition case search: one
  declared case number (``M.``, ``AT.``, ``SA.``) per unit; the case, every
  published event as a dated stage, the parties (or member state) as named,
  and each decision document with type, date, language, URL, CELEX, OJ
  reference and decision number (linked, not mirrored);
* ``tam-awards-json`` - the State Aid Transparency Award Module public search:
  one member state and SA measure per unit; each award with beneficiary name,
  national identifier and type, granting authority, instrument, objective,
  amount or range and currency verbatim, granting date and status;
* ``govuk-cma-case-json`` - GOV.UK Content API ``cma_case`` items: case type,
  state, sector and dates from the metadata, each ``change_history`` entry as a
  stage citing the page revision it was published in, attachments as linked
  documents under the Open Government Licence;
* ``ftc-case-html`` / ``doj-case-html`` - FTC legal-library and DOJ Antitrust
  Division case pages: matter/docket numbers, status and type as published,
  respondents or defendants as named, and each dated timeline document as a
  stage and a linked document; court docket numbers are citations only.

Every page is one selection unit and is all-or-nothing: a result longer than
one page is ``budget_exhausted``, never truncated; a response from another
host is a network-policy failure. Receipts name every request path, status and
response digest. ``PROVIDER_CONTRACTS``, ``INSTRUMENTS``, ``IDENTIFIERS``,
``DECLINED``, ``BOUNDED_COVERAGE`` and ``LIVE_VERIFICATION`` are the
machine-readable copy of the CS01 audit
(``docs/development/competition-evidence/source-audit.md``).

Nothing here predicts case outcomes, assesses market power or aid
compatibility, or gives legal advice; stage names, states and statuses are
the authority's own wording.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlencode, urlsplit

from src.ingestion.source_packs import SourcePackError

ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
RECORD_CONTRACT = "noesis-competition-record-v1"
CONNECTOR = "competition"
PAGE_SIZE = 100
MAX_UNITS = 20
REVIEW_BOUNDARY = ("Competition case, stage, party, document and award records are kept as the authority published "
                   "them. Nothing here predicts outcomes, assesses market power or the compatibility or legality of "
                   "aid, or gives legal advice.")
FORMATS: dict[str, dict[str, Any]] = {
    "ec-case-json": {"provider": "ec-competition", "authority": "ec", "unit": "cases"},
    "tam-awards-json": {"provider": "eu-tam", "authority": "ec", "unit": "measures"},
    "govuk-cma-case-json": {"provider": "uk-cma", "authority": "uk-cma", "unit": "cases"},
    "ftc-case-html": {"provider": "us-ftc", "authority": "us-ftc", "unit": "cases"},
    "doj-case-html": {"provider": "us-doj", "authority": "us-doj", "unit": "cases"},
}
EC_ATTRIBUTION = "Source: European Commission, DG Competition (reuse authorised, Commission Decision 2011/833/EU)."
OGL_ATTRIBUTION = "Contains public sector information licensed under the Open Government Licence v3.0."
FTC_ATTRIBUTION = "Source: U.S. Federal Trade Commission (US government work)."
DOJ_ATTRIBUTION = "Source: U.S. Department of Justice, Antitrust Division (US government work)."
LICENCES = {"ec-competition": "ec-reuse-2011-833", "eu-tam": "ec-reuse-2011-833", "uk-cma": "ogl-3.0",
            "us-ftc": "us-government-work", "us-doj": "us-government-work"}
PUBLISHERS = {"ec-competition": "European Commission, DG Competition",
              "eu-tam": "European Commission, State Aid Transparency Award Module",
              "uk-cma": "Competition and Markets Authority (GOV.UK)", "us-ftc": "U.S. Federal Trade Commission",
              "us-doj": "U.S. Department of Justice, Antitrust Division"}

# CS01 access decisions. Endpoints, fields and terms are recorded from the publishers' documentation and pages as
# known without network access; every item marked ``verify`` must be checked against the live pages, terms and a
# real response before a dated live run is accepted (CS14, #2368).
PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "ec-competition": {
        "publisher": PUBLISHERS["ec-competition"],
        "endpoints": ["https://competition-cases.ec.europa.eu/api/cases/{case_number} (verify)"],
        "formats": ["ec-case-json"],
        "access": "bounded case-detail acquisition from the case search application's JSON backend, one declared "
                  "case number per unit (verify the backend path and field names)",
        "authentication": "none",
        "rate_limits": "none published (verify); at most 20 declared cases per source per run",
        "revisions": "a changed case payload is a new case revision; each published event is an append-only stage "
                     "record; earlier revisions stay queryable",
        "licence": "Commission reuse policy, Commission Decision 2011/833/EU: reuse with acknowledgement",
        "attribution": EC_ATTRIBUTION,
        "documents": "decision documents linked, not mirrored (type, date, language, URL, OJ reference, CELEX)",
        "access_decision": "unverified-live",
    },
    "eu-tam": {
        "publisher": PUBLISHERS["eu-tam"],
        "endpoints": ["https://webgate.ec.europa.eu/competition/transparency/public/api/awards"
                      "?countryCode=..&saNumber=..&page=0&size=100 (verify)"],
        "formats": ["tam-awards-json"],
        "access": "bounded public-search export by member state and SA measure (verify the export path and fields)",
        "authentication": "none",
        "rate_limits": "none published (verify); page size 100; a longer result is budget_exhausted, never "
                       "truncated",
        "revisions": "an award is keyed by TAM award id and member state; a correction or withdrawal is a new "
                     "revision; earlier revisions stay queryable",
        "licence": "Commission reuse policy (Decision 2011/833/EU); data published by the granting member states",
        "attribution": EC_ATTRIBUTION,
        "documents": "none (tabular); amounts, ranges and currencies are kept verbatim, never converted or summed",
        "access_decision": "unverified-live",
    },
    "uk-cma": {
        "publisher": PUBLISHERS["uk-cma"],
        "endpoints": ["https://www.gov.uk/api/content/cma-cases/{slug}"],
        "formats": ["govuk-cma-case-json"],
        "access": "GOV.UK Content API (documented, no key), one declared slug per unit",
        "authentication": "none",
        "rate_limits": "GOV.UK guidance of 10 requests per second per client (verify)",
        "revisions": "public_updated_at is the page revision; change_history entries are append-only stages citing "
                     "the page revision they were published in",
        "licence": "Open Government Licence v3.0",
        "attribution": OGL_ATTRIBUTION,
        "documents": "attachments linked (title, URL, content type, publication date)",
        "access_decision": "unverified-live",
    },
    "us-ftc": {
        "publisher": PUBLISHERS["us-ftc"],
        "endpoints": ["https://www.ftc.gov/legal-library/browse/cases-proceedings/{slug}"],
        "formats": ["ftc-case-html"],
        "access": "bounded page acquisition of declared case pages; the Drupal field classes read "
                  "(field--name-field-matter-number, field--name-field-respondents, case-timeline__item) are verify",
        "authentication": "none",
        "rate_limits": "none published for ftc.gov pages (verify); at most 20 declared pages per run",
        "revisions": "article:modified_time (or the page digest) is the page revision; timeline items are "
                     "append-only stages",
        "licence": "US government work, public domain (17 U.S.C. § 105)",
        "attribution": FTC_ATTRIBUTION,
        "documents": "timeline documents linked (title, date, URL)",
        "access_decision": "unverified-live",
    },
    "us-doj": {
        "publisher": PUBLISHERS["us-doj"],
        "endpoints": ["https://www.justice.gov/atr/case/{slug}"],
        "formats": ["doj-case-html"],
        "access": "bounded page acquisition of declared Antitrust Division case pages (field classes verify)",
        "authentication": "none",
        "rate_limits": "none published (verify); at most 20 declared pages per run",
        "revisions": "as for the FTC pages",
        "licence": "US government work, public domain",
        "attribution": DOJ_ATTRIBUTION,
        "documents": "case documents linked; court docket numbers stored as citations only (court dockets are the "
                     "Legal pack's courts feature)",
        "access_decision": "unverified-live",
    },
}
LIVE_VERIFICATION = {
    provider: {"status": contract["access_decision"], "intended": "verified-live",
               "note": "no dated live run from this runtime; offline fixtures only (CS14, #2368)"}
    for provider, contract in PROVIDER_CONTRACTS.items()
}
INSTRUMENTS = {
    "ec-competition": {"in_scope": {"M.": "merger", "AT.": "antitrust", "SA.": "state_aid"},
                       "excluded": {"FS.": "Foreign Subsidies Regulation - separate regime",
                                    "DMA.": "Digital Markets Act - separate regime"}},
    "eu-tam": {"in_scope": {"award": "state_aid"},
               "excluded": {"below-threshold aid": "not published in TAM", "de minimis": "national registers",
                            "scheme budgets": "not awards"}},
    "uk-cma": {"in_scope": {"mergers": "merger", "markets": "market_investigation",
                            "ca98-and-civil-cartels": "antitrust", "criminal-cartels": "antitrust"},
               "excluded": {"consumer-enforcement": "not a competition case",
                            "regulatory-references-and-appeals": "not a competition case",
                            "subsidy-advice": "advisory report, not an award"}},
    "us-ftc": {"in_scope": {"Mergers": "merger", "Competition": "antitrust"},
               "excluded": {"Consumer Protection": "outside scope",
                            "HSR early termination notices": "declined (DECLINED['hsr-early-termination'])"}},
    "us-doj": {"in_scope": {"Civil Merger": "merger", "Civil Non-Merger": "antitrust"},
               "excluded": {"Criminal": "declined (DECLINED['doj-criminal'])"}},
}
DECLINED = {
    "hsr-early-termination": {"source": "api.ftc.gov HSR early-termination notices",
                              "reason": "grants suspended since 2021 and a notice is not a case action; documented, "
                                        "not acquired"},
    "doj-criminal": {"source": "DOJ Antitrust Division criminal case pages",
                     "reason": "defendants are frequently natural persons; not needed for company-to-case answers"},
    "court-dockets": {"source": "federal court dockets cited by FTC/DOJ actions",
                      "reason": "docket numbers are stored as citations only; dockets belong to the Legal pack's "
                                "courts feature"},
}
IDENTIFIERS = {
    "ec-competition": ["case number M./AT./SA.nnnnn", "decision number C(yyyy) nnnn", "CELEX", "OJ reference"],
    "eu-tam": ["TAM award id + member state", "SA measure number", "beneficiary national id with published type"],
    "uk-cma": ["GOV.UK slug", "content id", "case reference in the body when published"],
    "us-ftc": ["FTC matter number", "administrative docket number", "federal civil action number (citation)"],
    "us-doj": ["case page slug", "federal civil action number (citation)"],
}
BOUNDED_COVERAGE = {
    "seed": "companies and groups already acquired by the Corporate Ownership sources (fixtures: the fictional "
            "Exampla and Northwind groups); cases are declared by number, slug or measure, never crawled",
    "window": "cases opened or awards granted from 2020-01-01; earlier stages of an in-window case are kept",
    "per_run": f"at most {MAX_UNITS} declared units per source; TAM units of at most {PAGE_SIZE} awards",
}
# TAM national identifier types -> the scheme an ownership record carries for the same register (CS07 uses them
# for exact-identifier candidates). Unmapped types keep a ``tam:<type>`` scheme and never match deterministically.
TAM_ID_SCHEMES = {"KVK": "nl-kvk", "LEI": "lei", "CRN": "gb-coh", "HRB": "de-hrb", "SIREN": "fr-siren",
                  "VAT": "vat"}
COUNTRIES = {"united kingdom": "GB", "uk": "GB", "germany": "DE", "netherlands": "NL", "the netherlands": "NL",
             "france": "FR", "united states": "US", "usa": "US", "italy": "IT", "spain": "ES", "belgium": "BE",
             "ireland": "IE", "luxembourg": "LU", "sweden": "SE", "denmark": "DK", "austria": "AT", "poland": "PL",
             "switzerland": "CH", "japan": "JP", "china": "CN", "finland": "FI", "portugal": "PT"}
EC_ROLES = {"notifying party": "notifying_party", "notifying parties": "notifying_party", "target": "target",
            "target undertaking": "target", "addressee": "addressee", "complainant": "complainant",
            "beneficiary": "beneficiary", "member state": "other"}
_EC_CASE = re.compile(r"^(M|AT|SA)\.\d{3,6}$")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{2,150}$")
_MEMBER_STATE = re.compile(r"^[A-Z]{2}$")
_SA = re.compile(r"^SA\.\d{1,6}$")


class CompetitionFormatError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                                     default=str).encode()).hexdigest()


def _clean(value: Any) -> str | None:
    text = " ".join(unescape(str(value if value is not None else "")).split())
    return text or None


def _day(value: Any) -> str | None:
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", str(value or "").strip())
    return match.group(1) if match else None


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CompetitionFormatError("schema_drift", "response is not valid UTF-8 JSON") from exc


def _mapping(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CompetitionFormatError("schema_drift", f"{what} is not an object")
    return dict(value)


def country_code(name: Any) -> str | None:
    text = _clean(name)
    if not text:
        return None
    if re.fullmatch(r"[A-Z]{2}", text):
        return text
    return COUNTRIES.get(text.casefold())


def _source(provider: str, record_id: str, url: str | None, *, revision: Any = None,
            locator: Mapping[str, Any] | None = None) -> dict[str, Any]:
    source = {"provider": provider, "provider_record_id": record_id, "publisher": PUBLISHERS[provider],
              "license": LICENCES[provider]}
    if url:
        source["url"] = url
    if revision:
        source["revision"] = str(revision)
    if locator:
        source["locator"] = dict(locator)
    return source


def _references(*texts: Any) -> dict[str, list[str]]:
    """Exact legal references and case numbers a published text states (the CS08 citation parser)."""
    from src.kb.competition_citations import parse_references

    legal, cases = [], []
    for text in texts:
        for item in parse_references(text):
            (cases if item["kind"] == "case" else legal).append(item["raw"])
    return {"legal": sorted(set(legal)), "cases": sorted(set(cases))}


def _case_records(provider: str, authority: str, number: str, *, url: str, instrument: str,
                  instrument_as_published: str, title: Any, state: Any, sectors: Sequence[Any], opened: Any,
                  closed: Any, revision: Any, member_state: Any = None, related: Sequence[str] = (),
                  dockets: Sequence[Mapping[str, Any]] = (), legal: Sequence[str] = (),
                  native: Mapping[str, Any] | None = None) -> dict[str, Any]:
    from src.kb.competition_records import case_key

    return {"contract": RECORD_CONTRACT, "kind": "competition_case", "record_key": case_key(authority, number),
            "source": _source(provider, number, url, revision=revision),
            "authority": authority, "case_number": number, "instrument": instrument,
            "instrument_as_published": instrument_as_published, "title": _clean(title),
            "state_as_published": _clean(state), "sectors": [s for s in (_clean(x) for x in sectors) if s],
            "opened_on": _day(opened), "closed_on": _day(closed), "member_state": _clean(member_state),
            "case_url": url, "related_case_numbers": sorted({r for r in related if r and r != number}),
            "court_dockets": [dict(d) for d in dockets], "legal_references": sorted(set(legal)),
            "page_revision": _clean(revision), "native": dict(native or {})}


def _stage(provider: str, authority: str, number: str, url: str, name: Any, date: Any,
           document: Mapping[str, Any] | None = None, page_revision: Any = None) -> dict[str, Any] | None:
    from src.kb.competition_records import case_key, child_key

    stage = _clean(name)
    if not stage:
        return None
    case = case_key(authority, number)
    body = {"contract": RECORD_CONTRACT, "kind": "case_stage",
            "record_key": child_key("stage", case, stage, _day(date)),
            "source": _source(provider, number, (document or {}).get("url") or url,
                              locator={"field": "stage"}),
            "case_key": case, "authority": authority, "case_number": number, "stage_as_published": stage,
            "stage_date": _day(date), "date_status": "stated" if _day(date) else "unknown"}
    if document and document.get("url"):
        body["document"] = {k: v for k, v in dict(document).items() if v}
    if page_revision:
        body["page_revision"] = str(page_revision)
    return body


def _party(provider: str, authority: str, number: str, url: str, name: Any, role: str, role_as_published: Any,
           country: Any = None, identifiers: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any] | None:
    from src.kb.competition_records import case_key, child_key

    text = _clean(name)
    if not text:
        return None
    case = case_key(authority, number)
    published_role = _clean(role_as_published) or role
    return {"contract": RECORD_CONTRACT, "kind": "case_party",
            "record_key": child_key("party", case, text, published_role),
            "source": _source(provider, number, url, locator={"field": "parties"}),
            "case_key": case, "authority": authority, "case_number": number, "name_as_published": text,
            "role": role, "role_as_published": published_role, "country_as_published": _clean(country),
            "country": country_code(country), "identifiers": [dict(i) for i in identifiers]}


def _document(provider: str, authority: str, number: str, *, url: Any, doc_type: Any, date: Any,
              language: Any = None, citation: Mapping[str, Any] | None = None,
              legal: Sequence[str] = ()) -> dict[str, Any] | None:
    from src.kb.competition_records import case_key, child_key

    link = str(url or "")
    if not link.startswith("https://") or not _clean(doc_type):
        return None
    case = case_key(authority, number)
    cited = {k: _clean(v) for k, v in dict(citation or {}).items() if _clean(v)}
    return {"contract": RECORD_CONTRACT, "kind": "decision_document",
            "record_key": child_key("document", case, link),
            "source": _source(provider, number, link, locator={"field": "documents"}),
            "case_key": case, "authority": authority, "case_number": number,
            "document_type_as_published": _clean(doc_type), "document_date": _day(date),
            "language": _clean(language), "url": link, "citation": cited, "legal_references": sorted(set(legal))}


# ----------------------------------------------------------------- European Commission case search

EC_SITE = "https://competition-cases.ec.europa.eu"


def parse_ec_case(responses: Mapping[str, bytes], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    case = _mapping(_json(responses["case"]), "case")
    number = _clean(case.get("caseNumber"))
    if number != unit["case_number"]:
        raise CompetitionFormatError("schema_drift", "the case is not the requested one")
    prefix = number.split(".")[0] + "."
    instrument = INSTRUMENTS["ec-competition"]["in_scope"].get(prefix)
    if instrument is None:
        raise CompetitionFormatError("out_of_scope", f"{prefix} cases are outside the declared instruments")
    url = f"{EC_SITE}/cases/{number}"
    provider, authority = "ec-competition", "ec"
    decisions = [_mapping(d, "decision") for d in case.get("decisions") or []]
    legal = [str(ref) for d in decisions for ref in d.get("legalBasis") or []] + \
        [str(ref) for ref in case.get("legalBasis") or []]
    related = [str(r) for r in case.get("relatedCases") or []]
    dockets = [{"court": _clean(c.get("court")), "docket_number": _clean(c.get("number")),
                "note": "appeal reference as published; the court docket is not acquired here"}
               for c in (_mapping(x, "court case") for x in case.get("courtCases") or []) if _clean(c.get("number"))]
    sectors = [s.get("description") or s.get("code") if isinstance(s, Mapping) else s
               for s in case.get("caseSectors") or []]
    records = [_case_records(provider, authority, number, url=url, instrument=instrument,
                             instrument_as_published=_clean(case.get("caseInstrument")) or prefix,
                             title=case.get("caseTitle"), state=case.get("caseStatus"), sectors=sectors,
                             opened=case.get("notificationDate") or case.get("openingDate"),
                             closed=case.get("closingDate"), revision=case.get("lastUpdated"),
                             member_state=case.get("memberState"), related=related, dockets=dockets, legal=legal,
                             native={"policy_area": _clean(case.get("policyArea")),
                                     "sector_codes": [s.get("code") for s in case.get("caseSectors") or []
                                                      if isinstance(s, Mapping)]})]
    for event in case.get("events") or []:
        event = _mapping(event, "event")
        document = event.get("document") if isinstance(event.get("document"), Mapping) else None
        item = _stage(provider, authority, number, url, event.get("type"), event.get("date"),
                      {"title": _clean((document or {}).get("title")), "url": (document or {}).get("url"),
                       "type": _clean((document or {}).get("type"))} if document else None)
        if item:
            records.append(item)
    for company in case.get("companies") or []:
        company = _mapping(company, "company")
        role_text = _clean(company.get("role")) or "party"
        item = _party(provider, authority, number, url, company.get("name"),
                      EC_ROLES.get(role_text.casefold(), "other"), role_text, company.get("country"),
                      [{"scheme": str(i.get("scheme")).lower(), "value": str(i.get("value")),
                        "type_as_published": _clean(i.get("scheme"))}
                       for i in company.get("identifiers") or [] if isinstance(i, Mapping) and i.get("value")])
        if item:
            records.append(item)
    for decision in decisions:
        item = _document(provider, authority, number, url=decision.get("url"), doc_type=decision.get("type"),
                         date=decision.get("date"), language=decision.get("language"),
                         citation={"celex": decision.get("celex"), "oj_reference": decision.get("ojReference"),
                                   "decision_number": decision.get("decisionNumber")},
                         legal=[str(ref) for ref in decision.get("legalBasis") or []])
        if item:
            records.append(item)
    return records


# ----------------------------------------------------------------- State Aid Transparency Award Module

TAM_SITE = "https://webgate.ec.europa.eu/competition/transparency/public"
TAM_STATUS = {"published": "published", "corrected": "corrected", "modified": "corrected", "withdrawn": "withdrawn",
              "deleted": "withdrawn"}


def parse_tam(responses: Mapping[str, bytes], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    from src.kb.competition_records import award_key

    payload = _mapping(_json(responses["awards"]), "award search")
    content = payload.get("content")
    if not isinstance(content, list):
        raise CompetitionFormatError("schema_drift", "the award search has no content list")
    total = payload.get("totalElements")
    if (isinstance(total, int) and total > len(content)) or payload.get("last") is False:
        raise CompetitionFormatError("input_limit", "the award search is longer than one page; not truncated")
    records = []
    for award in (_mapping(a, "award") for a in content):
        member_state = _clean(award.get("countryCode"))
        award_id = _clean(award.get("awardId"))
        if member_state != unit["member_state"] or not award_id:
            raise CompetitionFormatError("schema_drift", "an award is outside the requested member state")
        sa_number = _clean(award.get("saNumber"))
        if sa_number and not _SA.fullmatch(sa_number):
            sa_number = None
        id_type, id_value = _clean(award.get("beneficiaryNationalIdType")), _clean(award.get("beneficiaryNationalId"))
        identifiers = []
        if id_value:
            identifiers.append({"scheme": TAM_ID_SCHEMES.get(str(id_type or "").upper(), f"tam:{str(id_type or 'unspecified').lower()}"),
                                "value": id_value, "type_as_published": id_type or "unspecified"})
        status_text = _clean(award.get("status")) or "Published"
        url = f"{TAM_SITE}/award/{member_state}/{award_id}"
        records.append({
            "contract": RECORD_CONTRACT, "kind": "state_aid_award", "record_key": award_key(member_state, award_id),
            "source": _source("eu-tam", f"{member_state}:{award_id}", url, revision=award.get("lastModified"),
                              locator={"field": "awardId"}),
            "award_id": award_id, "member_state": member_state, "sa_number": sa_number,
            "beneficiary_name_as_published": _clean(award.get("beneficiaryName")) or "(not published)",
            "beneficiary_identifiers": identifiers,
            "beneficiary_type_as_published": _clean(award.get("beneficiaryType")),
            "granting_authority": _clean(award.get("grantingAuthority")),
            "aid_instrument_as_published": _clean(award.get("aidInstrument")),
            "aid_objective_as_published": _clean(award.get("objective")),
            "amount_as_published": _clean(award.get("nominalAmount")),
            "amount_range_as_published": _clean(award.get("amountRange")),
            "currency": _clean(award.get("currency")), "award_date": _day(award.get("grantingDate")),
            "region_as_published": _clean(award.get("region")), "sector_as_published": _clean(award.get("sector")),
            "status": TAM_STATUS.get(status_text.casefold(), "published"), "status_as_published": status_text,
            "native": {"measure_title": _clean(award.get("measureTitle"))},
        })
    return records


# ----------------------------------------------------------------- GOV.UK CMA case pages

GOVUK_SITE = "https://www.gov.uk"


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data):
        self.parts.append(data)


def html_text(value: Any) -> str:
    parser = _Text()
    parser.feed(str(value or ""))
    return " ".join(" ".join(parser.parts).split())


def parse_cma_case(responses: Mapping[str, bytes], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    item = _mapping(_json(responses["content"]), "content item")
    slug = unit["slug"]
    if item.get("base_path") != f"/cma-cases/{slug}" or item.get("document_type") != "cma_case":
        raise CompetitionFormatError("schema_drift", "the content item is not the requested CMA case")
    details = _mapping(item.get("details") or {}, "details")
    metadata = _mapping(details.get("metadata") or {}, "metadata")
    case_type = _clean(metadata.get("case_type")) or ""
    instrument = INSTRUMENTS["uk-cma"]["in_scope"].get(case_type)
    if instrument is None:
        raise CompetitionFormatError("out_of_scope", f"CMA case type {case_type!r} is outside the declared instruments")
    provider, authority = "uk-cma", "uk-cma"
    url = GOVUK_SITE + item["base_path"]
    revision = _clean(item.get("public_updated_at"))
    body = html_text(details.get("body"))
    refs = _references(body)
    sectors = metadata.get("market_sector") or []
    records = [_case_records(provider, authority, slug, url=url, instrument=instrument,
                             instrument_as_published=case_type, title=item.get("title"),
                             state=metadata.get("case_state"), sectors=sectors if isinstance(sectors, list) else [sectors],
                             opened=metadata.get("opened_date"), closed=metadata.get("closed_date"), revision=revision,
                             related=refs["cases"], legal=refs["legal"],
                             native={"content_id": _clean(item.get("content_id")),
                                     "outcome_type_as_published": _clean(metadata.get("outcome_type")),
                                     "body_text": body[:4000]})]
    for entry in details.get("change_history") or []:
        entry = _mapping(entry, "change history entry")
        stage = _stage(provider, authority, slug, url, entry.get("note"), entry.get("public_timestamp"),
                       page_revision=_clean(entry.get("public_timestamp")))
        if stage:
            records.append(stage)
    title = _clean(item.get("title")) or ""
    if instrument == "merger" and " / " in title:
        # CMA pages publish no structured party list; merger inquiries name the undertakings in the title.
        named = re.sub(r"\s+merger inquiry$", "", title, flags=re.I)
        for name in named.split(" / "):
            party = _party(provider, authority, slug, url, name, "named_in_title", "named in case title")
            if party:
                records.append(party)
    for attachment in details.get("attachments") or []:
        attachment = _mapping(attachment, "attachment")
        document = _document(provider, authority, slug, url=attachment.get("url"), doc_type=attachment.get("title"),
                             date=attachment.get("public_timestamp"), language="en",
                             citation={"decision_number": attachment.get("reference")})
        if document:
            records.append(document)
    return records


# ----------------------------------------------------------------- FTC and DOJ case pages

class _CasePage(HTMLParser):
    """Drupal field items, the page title, the modified-time meta and dated timeline documents."""

    TIMELINE = {"case-timeline__item", "case-document"}
    VOID = {"meta", "br", "img", "link", "input", "hr", "wbr", "source"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.fields: dict[str, list[str]] = {}
        self.timeline: list[dict[str, Any]] = []
        self.title: list[str] = []
        self.modified: str | None = None
        self._depth = 0
        self._fields: list[tuple[str, int]] = []
        self._item: tuple[int, list[str]] | None = None
        self._title_depth: int | None = None
        self._entry: tuple[int, dict[str, Any]] | None = None
        self._link_depth: int | None = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta" and attrs.get("property") == "article:modified_time":
            self.modified = attrs.get("content")
        if tag in self.VOID:
            return
        self._depth += 1
        classes = set(str(attrs.get("class") or "").split())
        if tag == "h1" and "page-title" in classes:
            self._title_depth = self._depth
        name = next((c[len("field--name-"):] for c in classes if c.startswith("field--name-")), None)
        if name:
            self._fields.append((name, self._depth))
        if self._fields and self._item is None and "field__item" in classes:
            self._item = (self._depth, [])
        if tag == "li" and classes & self.TIMELINE:
            self._entry = (self._depth, {"date": None, "url": None, "title": []})
        if self._entry is not None and tag == "time":
            self._entry[1]["date"] = attrs.get("datetime")
        if self._entry is not None and tag == "a" and self._link_depth is None:
            self._entry[1]["url"] = attrs.get("href")
            self._link_depth = self._depth

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        depth = self._depth
        if self._item is not None and self._item[0] == depth:
            text = " ".join(" ".join(self._item[1]).split())
            if text and self._fields:
                self.fields.setdefault(self._fields[-1][0], []).append(text)
            self._item = None
        if self._fields and self._fields[-1][1] == depth:
            self._fields.pop()
        if self._title_depth == depth:
            self._title_depth = None
        if self._link_depth == depth:
            self._link_depth = None
        if self._entry is not None and self._entry[0] == depth:
            entry = self._entry[1]
            entry["title"] = " ".join(" ".join(entry["title"]).split())
            self.timeline.append(entry)
            self._entry = None
        self._depth = max(0, depth - 1)

    def handle_data(self, data):
        if self._title_depth is not None:
            self.title.append(data)
        if self._item is not None:
            self._item[1].append(data + " ")
        if self._entry is not None and self._link_depth is not None:
            self._entry[1]["title"].append(data)


def _page(raw: bytes) -> _CasePage:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CompetitionFormatError("schema_drift", "page is not UTF-8") from exc
    parser = _CasePage()
    parser.feed(text)
    if not parser.title:
        raise CompetitionFormatError("schema_drift", "the case page has no title")
    return parser


def _first(fields: Mapping[str, list[str]], name: str) -> str | None:
    return (fields.get(name) or [None])[0]


def _us_case(fmt: str, responses: Mapping[str, bytes], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    from src.ingestion.courts_justice_sources import party_type

    raw = responses["page"]
    page = _page(raw)
    spec = FORMATS[fmt]
    provider, authority = spec["provider"], spec["authority"]
    site = "https://www.ftc.gov" if provider == "us-ftc" else "https://www.justice.gov"
    path = ("/legal-library/browse/cases-proceedings/" if provider == "us-ftc" else "/atr/case/") + unit["slug"]
    url = site + path
    fields = page.fields
    revision = _clean(page.modified) or "sha256:" + hashlib.sha256(raw).hexdigest()[:16]
    title = _clean(" ".join(page.title))
    body = " ".join(fields.get("body") or [])
    if provider == "us-ftc":
        mission = {m.casefold() for m in fields.get("field-mission") or []}
        tags = fields.get("field-tags") or []
        if "competition" not in mission:
            raise CompetitionFormatError("out_of_scope", "the FTC matter is not a competition matter")
        instrument = "merger" if any("merger" in t.casefold() for t in tags) else "antitrust"
        instrument_text = ", ".join(tags) or "Competition"
        number = _clean(_first(fields, "field-matter-number")) or unit["slug"]
        state = _first(fields, "field-case-status")
        parties = [(p, "respondent", "Respondent") for p in fields.get("field-respondents") or []]
        docket = _clean(_first(fields, "field-docket-number"))
        dockets = [{"court": _clean(_first(fields, "field-court")) or "FTC administrative docket",
                    "docket_number": docket, "note": "cited as published; not acquired here"}] if docket else []
        opened = None
    else:
        case_type = _clean(_first(fields, "field-case-type")) or ""
        instrument = INSTRUMENTS["us-doj"]["in_scope"].get(case_type)
        if instrument is None:
            raise CompetitionFormatError("out_of_scope", f"DOJ case type {case_type!r} is declined or out of scope")
        instrument_text = case_type
        number = unit["slug"]
        state = _first(fields, "field-case-status")
        parties = [(p, "defendant", "Defendant") for p in fields.get("field-defendants") or []]
        docket = _clean(_first(fields, "field-docket-number"))
        dockets = [{"court": _clean(_first(fields, "field-court")), "docket_number": docket,
                    "note": "court docket number stored as a citation only; the docket belongs to the Legal pack"}] \
            if docket else []
        opened = _first(fields, "field-case-open-date")
    refs = _references(body, *[e["title"] for e in page.timeline])
    records = [_case_records(provider, authority, number, url=url, instrument=instrument,
                             instrument_as_published=instrument_text, title=title, state=state, sectors=[],
                             opened=_day(opened) or _parse_us_date(opened), closed=None, revision=revision,
                             related=refs["cases"], dockets=dockets, legal=refs["legal"],
                             native={"slug": unit["slug"], "type_of_action": _clean(_first(fields, "field-type-of-action")),
                                     "body_text": body[:4000]})]
    for entry in page.timeline:
        link = str(entry.get("url") or "")
        if link.startswith("/"):
            link = site + link
        document = {"title": entry["title"] or None, "url": link if link.startswith("https://") else None}
        stage = _stage(provider, authority, number, url, entry["title"], entry.get("date"),
                       document if document["url"] else None)
        if stage:
            records.append(stage)
        doc = _document(provider, authority, number, url=link, doc_type=entry["title"], date=entry.get("date"),
                        language="en", citation={"docket_number": docket}, legal=_references(entry["title"])["legal"])
        if doc:
            records.append(doc)
    for name, role, role_text in parties:
        if party_type(name) != "organisation":
            continue  # natural persons named in a case are not recorded as parties (CS01)
        item = _party(provider, authority, number, url, name, role, role_text)
        if item:
            records.append(item)
    return records


def _parse_us_date(value: Any) -> str | None:
    from datetime import datetime

    text = _clean(value)
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(str(text), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_ftc_case(responses: Mapping[str, bytes], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    return _us_case("ftc-case-html", responses, unit)


def parse_doj_case(responses: Mapping[str, bytes], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    return _us_case("doj-case-html", responses, unit)


# ----------------------------------------------------------------- selection and requests


def _units(fmt: str, selection: Mapping[str, Any]) -> list[dict[str, Any]]:
    key = FORMATS[fmt]["unit"]
    units = [dict(u) if isinstance(u, Mapping) else {"id": u} for u in selection.get(key) or []]
    if not 1 <= len(units) <= MAX_UNITS:
        raise SourcePackError("invalid_manifest", f"a competition selection names 1-{MAX_UNITS} {key}")
    for unit in units:
        if fmt == "ec-case-json" and not _EC_CASE.fullmatch(str(unit.get("case_number") or "")):
            raise SourcePackError("invalid_manifest", "EC units name a case number (M./AT./SA. and digits)")
        if fmt == "tam-awards-json" and (not _MEMBER_STATE.fullmatch(str(unit.get("member_state") or ""))
                                         or not _SA.fullmatch(str(unit.get("sa_number") or ""))):
            raise SourcePackError("invalid_manifest", "TAM units name a member state and an SA measure")
        if fmt in {"govuk-cma-case-json", "ftc-case-html", "doj-case-html"} \
                and not _SLUG.fullmatch(str(unit.get("slug") or "")):
            raise SourcePackError("invalid_manifest", "CMA, FTC and DOJ units name a page slug")
    return units


def requests_for(fmt: str, unit: Mapping[str, Any]) -> dict[str, tuple[str, dict[str, Any]]]:
    """Named request paths (relative to the endpoint) and parameters for one selection unit."""
    if fmt == "ec-case-json":
        return {"case": (f"/api/cases/{unit['case_number']}", {})}
    if fmt == "tam-awards-json":
        return {"awards": ("/api/awards", {"countryCode": unit["member_state"], "saNumber": unit["sa_number"],
                                           "page": 0, "size": PAGE_SIZE})}
    if fmt == "govuk-cma-case-json":
        return {"content": (f"/api/content/cma-cases/{unit['slug']}", {})}
    if fmt == "ftc-case-html":
        return {"page": (f"/legal-library/browse/cases-proceedings/{unit['slug']}", {})}
    if fmt == "doj-case-html":
        return {"page": (f"/atr/case/{unit['slug']}", {})}
    raise SourcePackError("invalid_manifest", f"unknown competition format {fmt!r}")


_PARSERS: dict[str, Callable[[Mapping[str, bytes], Mapping[str, Any]], list[dict[str, Any]]]] = {
    "ec-case-json": parse_ec_case,
    "tam-awards-json": parse_tam,
    "govuk-cma-case-json": parse_cma_case,
    "ftc-case-html": parse_ftc_case,
    "doj-case-html": parse_doj_case,
}


def parse_unit(fmt: str, responses: Mapping[str, bytes], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    if fmt not in _PARSERS:
        raise CompetitionFormatError("schema_drift", f"unknown competition format {fmt!r}")
    records = _PARSERS[fmt](responses, unit)
    from src.kb.competition_records import CompetitionRecordError, validate_record

    try:
        return [validate_record(r) for r in records]
    except CompetitionRecordError as exc:
        raise CompetitionFormatError("schema_drift", str(exc)) from exc


def competition_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    declared = dict(source.get("competition") or {})
    fmt = declared.get("format")
    if fmt not in FORMATS or FORMATS[fmt]["provider"] != declared.get("provider"):
        raise SourcePackError("invalid_manifest", "competition sources declare a matching provider and format")
    if declared.get("live_verification") not in {"unverified-live", "verified-live"}:
        raise SourcePackError("invalid_manifest", "competition sources state their LIVE_VERIFICATION status")
    _units(fmt, dict(declared.get("selection") or {}))
    if dict(source.get("auth") or {}).get("kind") != "none":
        raise SourcePackError("invalid_manifest", "competition sources are unauthenticated")
    return declared


class CompetitionAdapter:
    """Fetch one declared selection unit per page from the source's endpoint host and emit its records."""

    accepts_transport = True
    connector = CONNECTOR

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None) -> None:
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        del secret  # every competition source is unauthenticated
        self.source = json.loads(json.dumps(source))
        self.declared = competition_declaration(self.source)
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
            "competition": {"provider": self.provider, "format": self.format, "units": len(self.units)},
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _check(self, request: Mapping[str, Any]) -> None:
        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError("operation_forbidden", "operation is not declared by the source")
        if set(request) - {"operation", "parameters", "limit", "from_ms", "to_ms"}:
            raise SourcePackError("parameter_forbidden", "runtime adapter received undeclared controls")
        if dict(request.get("parameters") or {}):
            raise SourcePackError("parameter_forbidden", "competition runs fetch the declared selection only")

    def _get(self, path: str, params: Mapping[str, Any]) -> tuple[bytes, dict[str, Any]]:
        from src.ingestion.source_pack_runtime import _retry_after_ms

        endpoint = self.source["endpoint"].rstrip("/")
        url = endpoint + path
        host = (urlsplit(endpoint).hostname or "").casefold()
        ordered = dict(sorted(params.items()))
        response = self.transport(url=url, params=ordered, headers={"Accept": "application/json, text/html"},
                                  timeout=int(self.definition["limits"]["timeout_ms"]) / 1000)
        final_host = (urlsplit(str(response.get("final_url") or url)).hostname or "").casefold()
        if final_host != host:
            raise SourcePackError("network_policy", "competition response was served from another host")
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
        if status >= 400:
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
        except CompetitionFormatError as exc:
            raise SourcePackError("budget_exhausted" if exc.code == "input_limit" else "schema_drift",
                                  f"{exc.code}: {exc}") from exc
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        if len(records) > limit:
            raise SourcePackError("budget_exhausted", "unit has more records than the run's result budget")
        origin = "fixture" if all(r["origin"] == "fixture" for r in requests) else "live"
        receipt = {
            "contract": "noesis-competition-acquisition-receipt-v1", "source_id": self.source["source_id"],
            "provider": self.provider, "format": self.format, "unit_index": index, "unit": unit,
            "requests": requests, "records": len(records), "evidence_origin": origin,
            "live_verification": self.declared["live_verification"], "final_page": index + 1 >= len(self.units),
        }
        out = []
        for record in records:
            record = {**record, "source": {**record["source"], "evidence_origin": origin}}
            out.append({"id": record["record_key"], "title": _item_title(record), "url": record["source"].get("url"),
                        "language": "en", "published_at": _item_date(record),
                        "updated_at": record["source"].get("revision"),
                        "content": json.dumps(record, sort_keys=True, ensure_ascii=False),
                        "competition_record": record, "competition_receipt": receipt})
        next_cursor = None if receipt["final_page"] else str(index + 1)
        return RuntimePage(tuple(out), next_cursor, sum(r["bytes"] for r in requests), receipt=receipt)


def _item_title(record: Mapping[str, Any]) -> str:
    for field in ("title", "stage_as_published", "name_as_published", "document_type_as_published",
                  "beneficiary_name_as_published"):
        if record.get(field):
            return str(record[field])
    return record["record_key"]


def _item_date(record: Mapping[str, Any]) -> str | None:
    for field in ("opened_on", "stage_date", "document_date", "award_date"):
        if record.get(field) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(record[field])):
            return record[field]
    return None


FIXTURE_SECRET = None
ADAPTERS = {CONNECTOR: CompetitionAdapter}


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


def replay_native_fixture(source: Mapping[str, Any], fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    adapter = CompetitionAdapter(source, transport=fixture_transport(list(fixture["native_pages"])))
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
    "ADAPTERS", "BOUNDED_COVERAGE", "CONNECTOR", "DECLINED", "FIXTURE_SECRET", "FORMATS", "IDENTIFIERS",
    "INSTRUMENTS", "LIVE_VERIFICATION", "PROVIDER_CONTRACTS", "RECORD_CONTRACT", "REVIEW_BOUNDARY",
    "TAM_ID_SCHEMES", "CompetitionAdapter", "CompetitionFormatError", "competition_declaration", "country_code",
    "fixture_transport", "html_text", "parse_unit", "replay_native_fixture", "requests_for",
]
