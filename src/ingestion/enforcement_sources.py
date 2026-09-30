"""Regulatory enforcement acquisition for the Legal pack (#2651, EN01, EN03-EN06).

One native connector, ``enforcement``, registered in the ``legal-research``
source pack, reads a bounded, declared selection from one publisher per source
and emits ``noesis-enforcement-record-v1`` records
(:mod:`src.kb.enforcement_records`) as the regulator published them:

* ``sec-release-html`` - SEC litigation releases and administrative
  proceedings: one declared release page per unit, keyed by release or file
  number; respondents as named (organisations only), charges and legal bases
  as stated, sanctions as stated, the published admission wording of a
  settlement, related court cases as citations and the notice as a versioned
  document;
* ``fca-final-notice-text`` - FCA final notices (published as PDF; the text
  layer is read, through the optional ``pdfminer.six`` dependency when the
  response is a PDF): notice and firm reference number (FRN), the penalty
  imposed and the penalty before the settlement discount as separate published
  figures, the Principles and Handbook rules cited and Upper Tribunal
  references for appeals;
* ``echo-case-json`` - EPA ECHO case information web service: one case number
  per unit; statutes and sections, defendants (organisations only), facilities
  by FRS registry id with the coordinates ECHO publishes, federal penalty,
  state/local penalty, supplemental environmental project cost, cost recovery
  and compliance action cost as **separate** published fields;
* ``edpb-art60-html`` - EDPB register of Article 60 final decisions: one
  register entry per unit; lead and concerned supervisory authorities, GDPR
  provisions, corrective measures and fines as published, the controller only
  as published (entries without a published controller stay unnamed).

Every page is one selection unit and is all-or-nothing; a response from
another host is a network-policy failure; a declared unit answering 404 or 410
becomes a ``removed_by_source`` revision of the action (never a deletion).
Receipts name every request path, status and response digest.
``PROVIDER_CONTRACTS``, ``MINIMISATION``, ``IDENTIFIERS``, ``DECLINED``,
``BOUNDED_COVERAGE`` and ``LIVE_VERIFICATION`` are the machine-readable copy of
the EN01 audit (``docs/development/enforcement-evidence/source-audit.md``).

Nothing here scores risk or compliance, infers wrongdoing from an initiated
action, turns a settled matter into a finding or profiles a named individual:
natural persons are counted, never named (their published names are replaced
by ``[individual]`` in any stored text).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlencode, urlsplit

from src.ingestion.source_packs import SourcePackError

ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
RECORD_CONTRACT = "noesis-enforcement-record-v1"
CONNECTOR = "enforcement"
MAX_UNITS = 20
INDIVIDUAL = "[individual]"
SEC_USER_AGENT_ENV = "NOESIS_SEC_USER_AGENT"
REVIEW_BOUNDARY = ("Enforcement records are kept as each regulator published them. Nothing here scores risk or "
                   "compliance, infers wrongdoing from an initiated action, merges a settled 'neither admit nor "
                   "deny' outcome into a finding, profiles a named individual or gives legal advice.")
FORMATS: dict[str, dict[str, Any]] = {
    "sec-release-html": {"provider": "us-sec", "authority": "us-sec", "unit": "releases",
                         "coverage": "enforcement-sec"},
    "fca-final-notice-text": {"provider": "uk-fca", "authority": "uk-fca", "unit": "notices",
                              "coverage": "enforcement-fca"},
    "echo-case-json": {"provider": "us-epa-echo", "authority": "us-epa", "unit": "cases",
                       "coverage": "enforcement-epa"},
    "edpb-art60-html": {"provider": "edpb-art60", "authority": None, "unit": "entries",
                        "coverage": "enforcement-edpb"},
}
PUBLISHERS = {"us-sec": "U.S. Securities and Exchange Commission",
              "uk-fca": "Financial Conduct Authority",
              "us-epa-echo": "U.S. Environmental Protection Agency (ECHO)",
              "edpb-art60": "European Data Protection Board (register of Article 60 final decisions)"}
LICENCES = {"us-sec": "us-government-work", "uk-fca": "fca-website-terms", "us-epa-echo": "us-government-work",
            "edpb-art60": "edpb-reuse-with-attribution"}
AUTHORITIES = {
    "us-sec": {"name": "U.S. Securities and Exchange Commission", "jurisdiction": "US",
               "url": "https://www.sec.gov/enforcement-litigation"},
    "uk-fca": {"name": "Financial Conduct Authority", "jurisdiction": "GB",
               "url": "https://www.fca.org.uk/news/search-results"},
    "us-epa": {"name": "U.S. Environmental Protection Agency", "jurisdiction": "US",
               "url": "https://echo.epa.gov/"},
}

# EN01 access decisions. Endpoints, fields and terms are recorded from the publishers' documentation as known
# without network access (the publishers' hosts were unreachable from the authoring environment); every item marked
# ``verify`` must be checked against the live pages, terms and a real response before a dated live run (EN14, #2720).
PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "us-sec": {
        "publisher": PUBLISHERS["us-sec"],
        "endpoints": ["https://www.sec.gov/enforcement-litigation/litigation-releases/lr-{number} (verify)",
                      "https://www.sec.gov/enforcement-litigation/administrative-proceedings/{release} (verify)"],
        "formats": ["sec-release-html"],
        "access": "bounded page acquisition of declared release pages; the Drupal field classes read "
                  "(field--name-field-release-number, -respondents, -file-number, -cik, body) are verify",
        "authentication": "none; the SEC fair-access policy requires a declared User-Agent naming the operator and a "
                          f"contact address, read from {SEC_USER_AGENT_ENV}; without it a live run is refused as "
                          "source_unavailable (never sent anonymously)",
        "rate_limits": "SEC fair access: at most 10 requests per second (verify); at most 20 declared releases per "
                       "source per run",
        "revisions": "article:modified_time (or the page digest) is the page revision; a changed release is a new "
                     "revision of the action, notice, respondent and penalty records; a 404/410 is a "
                     "removed_by_source revision",
        "licence": "US government work, public domain (17 U.S.C. § 105); credit the SEC",
        "attribution": "Source: U.S. Securities and Exchange Commission.",
        "personal_data": "releases name individuals; names are not stored (MINIMISATION)",
        "access_decision": "unverified-live",
    },
    "uk-fca": {
        "publisher": PUBLISHERS["uk-fca"],
        "endpoints": ["https://www.fca.org.uk/publication/final-notices/{slug}.pdf (verify)",
                      "https://www.fca.org.uk/news/search-results?np_category=notices and decisions-final notices"],
        "formats": ["fca-final-notice-text"],
        "access": "bounded acquisition of declared final-notice documents (firms only); the notice is a PDF whose "
                  "text layer is parsed; PDF text extraction needs the optional pdfminer.six dependency and is "
                  "reported as source_unavailable when absent (verify the notice layout: 'To:', 'Firm Reference "
                  "Number:', 'Date:', section 1 'ACTION')",
        "authentication": "none",
        "rate_limits": "none published (verify); at most 20 declared notices per run",
        "revisions": "the notice digest is the revision; an amended notice ('This Final Notice was amended on ...') "
                     "is a new revision of the notice and action records",
        "licence": "FCA website terms: FCA material may be reproduced free of charge with acknowledgement, "
                   "accurately and not in a misleading context (verify; not the Open Government Licence)",
        "attribution": "Source: Financial Conduct Authority.",
        "personal_data": "notices addressed to individuals are declined; individuals named inside firm notices are "
                         "never extracted",
        "access_decision": "unverified-live",
    },
    "us-epa-echo": {
        "publisher": PUBLISHERS["us-epa-echo"],
        "endpoints": [("https://echodata.epa.gov/echo/case_rest_services.get_case_info?p_id={case_number}"
                       "&output=JSON (verify)")],
        "formats": ["echo-case-json"],
        "access": "documented ECHO web services (case_rest_services), one declared case number per unit (verify the "
                  "get_case_info response keys)",
        "authentication": "none",
        "rate_limits": "no published quota; ECHO asks clients to avoid bulk polling (verify); at most 20 declared "
                       "cases per run",
        "revisions": "a changed case payload is a new revision; ECHO refreshes weekly; penalties, SEP cost, cost "
                     "recovery and compliance action cost are separate published fields",
        "licence": "US government work, public domain; ECHO data disclaimer applies (verify)",
        "attribution": "Source: U.S. EPA Enforcement and Compliance History Online (ECHO).",
        "personal_data": "defendants may be individuals (counted, not named); facilities are organisations' sites",
        "access_decision": "unverified-live",
    },
    "edpb-art60": {
        "publisher": PUBLISHERS["edpb-art60"],
        "endpoints": [("https://www.edpb.europa.eu/our-work-tools/consistency-findings/"
                       "register-for-article-60-final-decisions_en"),
                      "https://www.edpb.europa.eu/art-60-final-decisions/{slug}_en (verify the entry path)"],
        "formats": ["edpb-art60-html"],
        "access": "bounded acquisition of declared register entry pages; the entry field labels (LSA, CSAs, legal "
                  "reference(s), decision, fine, controller) are verify",
        "authentication": "none",
        "rate_limits": "none published (verify); at most 20 declared entries per run",
        "revisions": "the entry page digest is the revision; a withdrawn entry (404/410) is a removed_by_source "
                     "revision",
        "licence": "EDPB legal notice: reuse authorised with acknowledgement of the source (Commission Decision "
                   "2011/833/EU applied by the EDPB; verify)",
        "attribution": "Source: European Data Protection Board, register of Article 60 final decisions.",
        "personal_data": "entries are often anonymised; the controller is stored only as published and only when "
                         "it is an organisation",
        "access_decision": "unverified-live",
    },
}
LIVE_VERIFICATION = {
    provider: {"status": contract["access_decision"], "intended": "verified-live",
               "note": "no dated live run from this runtime; offline fixtures only (EN14, #2720)"}
    for provider, contract in PROVIDER_CONTRACTS.items()
}
MINIMISATION = {
    "decision": "EN01 data-minimisation decision for named individuals",
    "stored": ["organisational respondents: name, role and published identifiers (CIK, FRN, LEI, company number)",
               "the count of individual respondents on each action",
               "facility name, FRS id and published coordinates (organisations' sites)"],
    "redacted": [f"an individual's published name in a stored title or outcome text is replaced by {INDIVIDUAL!r}"],
    "excluded": [("individuals' names, roles, dates of birth, addresses, nationalities and personal registration "
                  "numbers (IRN, CRD)"), "notices and releases whose only respondents are individuals",
                 "FCA notices addressed to individuals", "EPA criminal cases whose defendants are all individuals"],
    "retention": "revisions are kept as the audit trail of what the regulator published; an action the source "
                 "removes becomes a removed_by_source revision and is excluded from answers unless history is "
                 "requested",
    "access": "any principal with knowledge:legal:read and namespace read access may query organisational "
              "records; nothing personal is stored, so there is nothing further to restrict",
    "matching": "individuals are never offered to identity matching (EN07)",
}
DECLINED = {
    "sec-trading-suspensions": {"source": "SEC trading suspensions",
                                "reason": "a suspension is not an enforcement action against a respondent"},
    "fca-individual-notices": {"source": "FCA final notices addressed to individuals",
                               "reason": "EN01 minimisation: individuals are not profiled"},
    "fca-register-api": {"source": "FCA Financial Services Register API",
                         "reason": "registration-gated key; FRNs are taken as published in the notice"},
    "echo-bulk-downloads": {"source": "ECHO ICIS-FE&C bulk downloads",
                            "reason": "bulk ingestion is outside the bounded declared selection"},
    "national-dpa-registers": {"source": "national supervisory authorities' own decision registers",
                               "reason": "outside the first bounded coverage; only the EDPB Article 60 register"},
}
IDENTIFIERS = {
    "us-sec": ["litigation release number LR-nnnnn", "administrative proceeding release number (33-, 34-, IA-)",
               "administrative proceeding file number 3-nnnnn", "respondent CIK where published",
               "federal civil action number (citation)"],
    "uk-fca": ["notice slug", "firm reference number (FRN)", "Upper Tribunal reference (citation)"],
    "us-epa-echo": ["ECHO case number", "facility FRS registry id", "federal civil action number (citation)"],
    "edpb-art60": ["register entry slug", "national decision reference as published", "lead supervisory authority"],
}
BOUNDED_COVERAGE = {
    "seed": "companies and groups already acquired by the Corporate Ownership sources (fixtures: the fictional "
            "Exampla and Northwind groups); actions are declared by release number, notice, case number or register "
            "entry, never crawled",
    "window": "actions published from 2020-01-01; the history of an in-window action is kept",
    "per_run": f"at most {MAX_UNITS} declared units per source",
    "respondents": "actions with at least one organisational respondent",
}
EDPB_SA = {"dutch": "NL", "netherlands": "NL", "german": "DE", "germany": "DE", "french": "FR", "france": "FR",
           "irish": "IE", "ireland": "IE", "luxembourg": "LU", "spanish": "ES", "spain": "ES", "italian": "IT",
           "italy": "IT", "belgian": "BE", "belgium": "BE", "austrian": "AT", "austria": "AT", "swedish": "SE",
           "sweden": "SE", "danish": "DK", "denmark": "DK", "polish": "PL", "poland": "PL", "finnish": "FI",
           "finland": "FI", "portuguese": "PT", "portugal": "PT", "norwegian": "NO", "norway": "NO"}
_SEC_PATH = re.compile(r"^(litigation-releases/lr-\d{4,6}|administrative-proceedings/(33|34|ia|ic)-\d{4,6})$")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{2,150}$")
_ECHO_CASE = re.compile(r"^[0-9A-Z]{2}-\d{4}-\d{4}$")
_CURRENCY = {"$": "USD", "£": "GBP", "€": "EUR", "USD": "USD", "GBP": "GBP", "EUR": "EUR"}
_AMOUNT = re.compile(r"(US\$|\$|£|€|EUR|GBP|USD)\s?(\d[\d,]*(?:\.\d+)?(?:\s(?:million|billion))?)")


class EnforcementFormatError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _clean(value: Any) -> str | None:
    text = " ".join(unescape(str(value if value is not None else "")).split())
    return text or None


def _day(value: Any) -> str | None:
    from datetime import datetime

    text = _clean(value) or ""
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", text)
    if match:
        return match.group(1)
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%m/%d/%Y", "%d %B %Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()  # noqa: DTZ007 - a calendar date only
        except ValueError:
            continue
    return None


# European legal forms the courts tokens do not carry (B.V., N.V., S.A., SE, ApS, AB, Oy, S.p.A., S.r.l., SAS).
_EU_FORMS = re.compile(r"(?:^|\s)(b\.v\.|bv|n\.v\.|nv|s\.a\.|sa|se|aps|a/s|ab|oy|oyj|s\.p\.a\.|spa|s\.r\.l\.|srl|"
                       r"sas|sarl|s\.à r\.l\.|kg|ug|e\.v\.)(?=$|[\s,])", re.IGNORECASE)


def party_type(name: Any) -> str:
    """``organisation`` only when the published name carries a legal-form or public-body token (as courts, CJ01)."""
    from src.ingestion.courts_justice_sources import party_type as courts_party_type

    if courts_party_type(name) == "organisation" or _EU_FORMS.search(str(name or "")):
        return "organisation"
    return "natural_person"


def minimise(text: Any, individuals: Sequence[str]) -> str | None:
    """Replace each individual's published name (and surname) in a stored text by ``[individual]``."""
    out = _clean(text)
    if not out:
        return None
    for name in sorted({n for n in individuals if n}, key=len, reverse=True):
        out = re.sub(re.escape(name), INDIVIDUAL, out, flags=re.IGNORECASE)
        parts = name.split()
        if len(parts) > 1 and len(parts[-1]) > 2:
            out = re.sub(r"\b(?:(?:Mr|Ms|Mrs|Miss|Mx|Dr)\.?\s+)?" + re.escape(parts[-1]) + r"\b", INDIVIDUAL, out)
    return out


_HONORIFIC = re.compile(r"\b(Mr|Ms|Mrs|Miss|Mx|Dr)\.?\s+[A-Z][a-z]+")


def _organisational(sentences: Sequence[str], individuals: Sequence[str]) -> list[str]:
    """Sentences that name no individual (by a listed name or an honorific), minimised."""
    out = []
    for sentence in sentences:
        text = minimise(sentence, individuals)
        if text and INDIVIDUAL not in text and not _HONORIFIC.search(text):
            out.append(text)
    return out


def amounts(text: Any) -> list[tuple[str, str]]:
    """(amount text as published, ISO currency) pairs in a text, in order."""
    return [(m.group(0).strip(), _CURRENCY[m.group(1).replace("US$", "$")]) for m in _AMOUNT.finditer(str(text or ""))]


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


def _references(*texts: Any, context: str | None = None) -> list[str]:
    """Exact legal bases a published text cites (the EN08 citation parser), as their published wording."""
    from src.kb.enforcement_links import parse_legal_bases

    found: list[str] = []
    for text in texts:
        for item in parse_legal_bases(text, context=context):
            if item["kind"] != "case" and item["raw"] not in found:
                found.append(item["raw"])
    return found


def _case_references(*texts: Any) -> list[str]:
    from src.kb.enforcement_links import parse_legal_bases

    return sorted({item["raw"] for text in texts for item in parse_legal_bases(text) if item["kind"] == "case"})


def _sentences(text: Any, *words: str) -> list[str]:
    # Abbreviations (Mr., No., Inc., U.S.) never end a sentence.
    text = re.sub(r"\b(Mr|Ms|Mrs|Dr|Mx|No|Inc|Co|Corp|Ltd|U\.S|Sec|Art)\.\s", r"\1. ", _clean(text) or "")
    parts = re.split(r"(?<=[.;])\s+(?=[A-Z])", text)
    return [p.replace(" ", " ") for p in parts if any(w in p.lower() for w in words)]


def _settlement(text: Any) -> dict[str, Any]:
    """The settlement wording as published: never turned into an admission or a finding."""
    clean = _clean(text) or ""
    admission = re.search(r"without admitting or denying[^.;,]*|neither admit(?:s|ted)? nor den(?:y|ies|ied)[^.;,]*|"
                          r"admitted the (?:facts|findings)[^.;,]*", clean, re.IGNORECASE)
    settled = bool(re.search(r"consented to|agreed to (?:settle|resolve)|settlement|settled", clean, re.IGNORECASE))
    return {"settled_as_published": True if settled else None,
            "admission_as_published": admission.group(0).strip() if admission else None}


def _authority_record(authority: str, provider: str, *, name: str | None = None, jurisdiction: str | None = None,
                      url: str | None = None) -> dict[str, Any]:
    from src.kb.enforcement_records import authority_key

    spec = AUTHORITIES.get(authority, {})
    return {"contract": RECORD_CONTRACT, "kind": "authority", "record_key": authority_key(authority),
            "source": _source(provider, authority, url or spec.get("url")), "authority": authority,
            "name_as_published": name or spec.get("name"), "jurisdiction": jurisdiction or spec.get("jurisdiction"),
            "url": url or spec.get("url"), "identifiers": []}


def _child(kind: str, action: str, authority: str, number: str, provider: str, source_url: str | None,
           parts: Sequence[Any], /, **fields: Any) -> dict[str, Any]:
    from src.kb.enforcement_records import child_key

    return {"contract": RECORD_CONTRACT, "kind": kind, "record_key": child_key(kind, action, *parts),
            "source": _source(provider, number, source_url, locator={"field": kind}), "action_key": action,
            "authority": authority, "action_number": number, **fields}


def _respondents(action: str, authority: str, number: str, provider: str, url: str,
                 names: Sequence[tuple[str, str, str, list[dict[str, Any]]]]) -> tuple[list[dict], list[str]]:
    """(respondent records for organisations, individuals' names to redact) - individuals are never recorded."""
    records, individuals = [], []
    for name, role, role_text, identifiers in names:
        text = _clean(name)
        if not text:
            continue
        if party_type(text) != "organisation":
            individuals.append(text)
            continue
        records.append(_child("respondent", action, authority, number, provider, url, [text, role],
                              name_as_published=text, respondent_type="organisation", role=role,
                              role_as_published=role_text, identifiers=identifiers, country=None))
    return records, individuals


def _penalty(action: str, authority: str, number: str, provider: str, url: str, penalty_type: str, label: str,
             text: str | None, currency: str | None, imposed_on: str | None = None,
             note: str | None = None, ordinal: int = 0) -> dict[str, Any]:
    stated = bool(text)
    parts = [penalty_type, label] + ([ordinal] if ordinal else [])
    return _child("penalty", action, authority, number, provider, url, parts,
                  penalty_type=penalty_type, penalty_type_as_published=label,
                  amount_as_published=text if stated else None, currency=currency if stated else None,
                  amount_status="stated" if stated else "not_published", imposed_on=imposed_on, note=note)


def _document(action: str, authority: str, number: str, provider: str, url: str, *, doc_type: str, title: Any,
              date: Any, raw: bytes, amended_on: Any = None, correction: Any = None) -> dict[str, Any]:
    return _child("enforcement_decision", action, authority, number, provider, url, [url],
                  document_type_as_published=doc_type, title=_clean(title), document_date=_day(date), url=url,
                  content_sha256=hashlib.sha256(raw).hexdigest(), language="en", amended_on=_day(amended_on),
                  correction_as_published=_clean(correction),
                  source_status="corrected" if amended_on else "published")


# ----------------------------------------------------------------- HTML helpers


class _Fields(HTMLParser):
    """Drupal ``field--name-*`` items, ``<dt>/<dd>`` pairs, ``<time>`` values, links, headings and meta tags."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.fields: dict[str, list[str]] = {}
        self.pairs: dict[str, str] = {}
        self.meta: dict[str, str] = {}
        self.links: list[tuple[str, str]] = []
        self.title = ""
        self._field: list[tuple[str, int]] = []
        self._depth = 0
        self._item: list[str] | None = None
        self._item_depth = -1
        self._dt: list[str] | None = None
        self._dd: list[str] | None = None
        self._last_dt = ""
        self._h1: list[str] | None = None
        self._a: tuple[str, list[str]] | None = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta" and attrs.get("content") is not None:
            self.meta[str(attrs.get("property") or attrs.get("name") or "")] = str(attrs["content"])
        if tag in {"meta", "br", "img", "link", "input"}:
            return
        self._depth += 1
        classes = str(attrs.get("class") or "").split()
        for cls in classes:
            if cls.startswith("field--name-"):
                self._field.append((cls[len("field--name-"):], self._depth))
        if "field__item" in classes and self._field:
            self._item, self._item_depth = [], self._depth
        if tag == "time" and self._item is not None and attrs.get("datetime"):
            self._item.append(str(attrs["datetime"]) + " ")
        if tag == "dt":
            self._dt = []
        if tag == "dd":
            self._dd = []
        if tag == "h1":
            self._h1 = []
        if tag == "a" and attrs.get("href"):
            self._a = (str(attrs["href"]), [])

    def handle_endtag(self, tag):
        if tag in {"meta", "br", "img", "link", "input"}:
            return
        if self._item is not None and self._depth == self._item_depth:
            self.fields.setdefault(self._field[-1][0], []).append(" ".join("".join(self._item).split()))
            self._item = None
        while self._field and self._field[-1][1] >= self._depth:
            self._field.pop()
        if tag == "dt" and self._dt is not None:
            self._last_dt = " ".join("".join(self._dt).split()).rstrip(":").casefold()
            self._dt = None
        if tag == "dd" and self._dd is not None:
            self.pairs[self._last_dt] = " ".join("".join(self._dd).split())
            self._dd = None
        if tag == "h1" and self._h1 is not None:
            self.title = " ".join("".join(self._h1).split())
            self._h1 = None
        if tag == "a" and self._a is not None:
            self.links.append((self._a[0], " ".join("".join(self._a[1]).split())))
            self._a = None
        self._depth -= 1

    def handle_data(self, data):
        for bucket in (self._item, self._dt, self._dd, self._h1, self._a[1] if self._a else None):
            if bucket is not None:
                bucket.append(data)


def _page(raw: bytes) -> _Fields:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise EnforcementFormatError("schema_drift", "page is not UTF-8") from exc
    parser = _Fields()
    parser.feed(text)
    parser.close()
    return parser


def _first(fields: Mapping[str, list[str]], name: str) -> str | None:
    values = fields.get(name) or []
    return values[0] if values else None


def _absolute(site: str, href: str) -> str:
    return href if href.startswith("https://") else site + ("" if href.startswith("/") else "/") + href


# ----------------------------------------------------------------- SEC


SEC_SITE = "https://www.sec.gov"


def sec_release_number(path: str) -> str:
    """The release number a declared page path names (``LR-99901``, ``33-99901``): the action's key."""
    return str(path).rsplit("/", 1)[-1].upper()


def parse_sec_release(responses: Mapping[str, bytes], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    from src.kb.enforcement_records import action_key

    raw = responses["page"]
    page = _page(raw)
    path = str(unit["path"])
    url = f"{SEC_SITE}/enforcement-litigation/{path}"
    administrative = path.startswith("administrative-proceedings/")
    release = _first(page.fields, "field-release-number")
    number = sec_release_number(path)
    if not release or release.replace(" ", "").upper().removeprefix("NO.") != number:
        raise EnforcementFormatError("schema_drift", "SEC release page does not state the declared release number")
    file_number = _first(page.fields, "field-file-number")
    authority, provider = "us-sec", "us-sec"
    action = action_key(authority, number)
    revision = page.meta.get("article:modified_time") or hashlib.sha256(raw).hexdigest()[:16]
    body = " ".join(page.fields.get("body") or [])
    ciks = {}
    for item in page.fields.get("field-respondent-cik") or []:
        name, _, cik = item.rpartition(" CIK ")
        if cik.strip():
            ciks[_clean(name) or ""] = cik.strip()
    names = [(n, "respondent" if administrative else "defendant", "Respondent" if administrative else "Defendant",
              [{"scheme": "sec-cik", "value": ciks[n], "type_as_published": "CIK"}] if n in ciks else [])
             for n in (page.fields.get("field-respondents") or [])]
    respondents, individuals = _respondents(action, authority, number, provider, url, names)
    if not respondents:
        raise EnforcementFormatError("schema_drift", "declined: a release whose respondents are all individuals is "
                                                     "outside the bounded coverage (EN01)")
    dated = _day(_first(page.fields, "field-publish-date"))
    court_cases = []
    for match in re.finditer(r"(?:Civil Action|Case) No\.\s*([0-9]{1,2}:\d{2}-[a-z]{2}-\d{3,6}(?:-[A-Z]{2,4})?)"
                             r"(?:\s*\(([^)]*?)\))?", body):
        court_cases.append({"docket_number": match.group(1),
                            "court": (_clean(match.group(2)) or "").split(",")[0] or None,
                            "caption": minimise(page.title, individuals)})
    # Sentences about an individual are left out altogether (EN01): no individual's charges, outcome or sanction.
    charges = [s for s in _organisational(_sentences(body, "violations of", "charg", "alleg"), individuals)
               if "consented" not in s.lower()]
    outcome = _organisational(_sentences(body, "consented", "final judgment", "ordered", "imposes", "agreed to",
                                         "dismiss"), individuals)
    penalties = []
    for sentence in _organisational(_sentences(body, "penalt", "disgorgement", "prejudgment interest"), individuals):
        lowered = sentence.lower()
        found = amounts(sentence)
        for kind, word in (("disgorgement", "disgorgement"), ("prejudgment_interest", "prejudgment interest"),
                           ("civil_penalty", "civil penalty")):
            if word in lowered:
                position = lowered.index(word)
                nearest = [a for a in found if sentence.find(a[0]) > position] or found
                penalties.append(_penalty(action, authority, number, provider, url, kind, word,
                                          nearest[0][0] if nearest else None, nearest[0][1] if nearest else None,
                                          imposed_on=dated,
                                          ordinal=sum(p["penalty_type"] == kind for p in penalties)))
    action_type = "administrative_proceeding" if administrative else "civil_action"
    records = [
        _authority_record(authority, provider),
        {"contract": RECORD_CONTRACT, "kind": "enforcement_action", "record_key": action,
         "source": _source(provider, number, url, revision=revision), "authority": authority, "action_number": number,
         "action_type": action_type,
         "action_type_as_published": "Administrative Proceeding" if administrative else "Litigation Release",
         "title": minimise(page.title, individuals), "status_as_published": None, "source_status": "published",
         "initiated_on": _day(_first(page.fields, "field-filed-date")), "decided_on": None, "published_on": dated,
         "legal_bases": _references(body), "charges_as_published": charges,
         "outcome_as_published": " ".join(o for o in outcome if o) or None, "settlement": _settlement(body),
         "related_identifiers": [{"scheme": "sec-release", "value": release, "type_as_published": "Release No."}]
         + ([{"scheme": "sec-file-number", "value": file_number, "type_as_published": "File No."}]
            if file_number else []),
         "court_cases": court_cases, "related_references": _case_references(body),
         "natural_person_respondents": len(individuals), "page_revision": revision, "action_url": url,
         "native": {"release_number": release}},
        _document(action, authority, number, provider, url, doc_type=("Administrative Proceeding" if administrative
                                                                       else "Litigation Release"),
                  title=minimise(page.title, individuals), date=dated, raw=raw),
    ]
    return records + respondents + penalties


# ----------------------------------------------------------------- FCA


FCA_SITE = "https://www.fca.org.uk"


def _pdf_text(raw: bytes) -> str:
    try:
        from io import BytesIO

        from pdfminer.high_level import extract_text
    except ImportError as exc:  # optional dependency: an explicit degraded state, never a silent skip
        raise SourcePackError("source_unavailable", "FCA notices are PDFs; text extraction needs the optional "
                                                    "pdfminer.six dependency") from exc
    return extract_text(BytesIO(raw))


def parse_fca_notice(responses: Mapping[str, bytes], unit: Mapping[str, Any], *, content_type: str = "text/plain"
                     ) -> list[dict[str, Any]]:
    from src.kb.enforcement_records import action_key

    raw = responses["notice"]
    text = _pdf_text(raw) if "pdf" in content_type else raw.decode("utf-8-sig", errors="strict")
    flat = " ".join(text.split())
    slug = str(unit["slug"])
    url = f"{FCA_SITE}/publication/final-notices/{slug}.pdf"
    to = re.search(r"\bTo:\s*(.+?)\s+(?:Firm Reference Number|FRN|Reference Number|Address|Date):", flat)
    if not to:
        raise EnforcementFormatError("schema_drift", "FCA notice has no 'To:' line")
    addressee = _clean(to.group(1)) or ""
    if party_type(addressee) != "organisation":
        raise EnforcementFormatError("schema_drift", "declined: FCA notices addressed to individuals are not acquired "
                                                     "(EN01 minimisation)")
    frn = re.search(r"(?:Firm Reference Number|FRN):\s*(\d{6,7})", flat)
    dated = re.search(r"\bDate:\s*(\d{1,2} [A-Z][a-z]+ \d{4})", flat)
    amended = re.search(r"This Final Notice was amended on (\d{1,2} [A-Z][a-z]+ \d{4})([^.]*\.)?", flat)
    authority, provider, number = "uk-fca", "uk-fca", slug
    action = action_key(authority, number)
    section1 = re.search(r"1\.\s*ACTION(.*?)(?:2\.\s*SUMMARY|$)", flat)
    action_text = section1.group(1) if section1 else ""
    reasons = re.search(r"2\.\s*SUMMARY OF REASONS(.*?)(?:\d\.\s*[A-Z][A-Z ]{5,}|$)", flat)
    reasons_text = reasons.group(1) if reasons else ""
    # Paragraph numbers (1.1., 2.3.) are layout, not wording.
    action_text, reasons_text = (re.sub(r"(?:^|\s)\d{1,2}\.\d{1,3}\.(?=\s|$)", " ", t).strip()
                                 for t in (action_text, reasons_text))
    penalties = []
    imposed = re.search(r"financial penalty of ((?:£|€|\$)\s?[\d,]+(?:\.\d+)?)", action_text)
    decided = _day(dated.group(1)) if dated else None
    if imposed:
        penalties.append(_penalty(action, authority, number, provider, url, "financial_penalty",
                                  "financial penalty imposed", imposed.group(1), amounts(imposed.group(1))[0][1],
                                  imposed_on=decided))
    else:
        penalties.append(_penalty(action, authority, number, provider, url, "financial_penalty",
                                  "financial penalty imposed", None, None, imposed_on=decided,
                                  note="no penalty figure stated in section 1 of the notice"))
    before = re.search(r"(?:would have imposed|would otherwise have imposed|would have been) a financial penalty of "
                       r"((?:£|€|\$)\s?[\d,]+(?:\.\d+)?)", action_text)
    discount = re.search(r"(\d{1,2}%\s*\([^)]*\)\s*discount|\d{1,2}% discount)", action_text)
    if before:
        penalties.append(_penalty(action, authority, number, provider, url, "penalty_before_settlement_discount",
                                  "financial penalty before settlement discount", before.group(1),
                                  amounts(before.group(1))[0][1], imposed_on=decided,
                                  note=_clean(discount.group(1)) if discount else None))
    appeal_records = []
    tribunal = re.search(r"referred (?:the matter|the Decision Notice|this matter) to the (Upper Tribunal)"
                         r"[^.]*?\(reference ([A-Z]{2,4}/\d{4}/\d{3,5})\)", flat)
    if tribunal:
        outcome = re.search(r"(?:On|on) (\d{1,2} [A-Z][a-z]+ \d{4}),? the Tribunal ([^.]*\.)", flat)
        appeal_records.append(_child(
            "appeal", action, authority, number, provider, url, [tribunal.group(2)], forum_as_published=tribunal.group(1),
            reference=tribunal.group(2), status_as_published=_clean(outcome.group(2)) if outcome else "referred",
            lodged_on=None, decided_on=_day(outcome.group(1)) if outcome else None, court_docket=None))
    respondents = [_child("respondent", action, authority, number, provider, url, [addressee, "firm"],
                          name_as_published=addressee, respondent_type="organisation", role="firm",
                          role_as_published="To", identifiers=[{"scheme": "gb-fca-frn", "value": frn.group(1),
                                                               "type_as_published": "Firm Reference Number"}]
                          if frn else [], country="GB")]
    settlement = _settlement(action_text)
    records = [
        _authority_record(authority, provider),
        {"contract": RECORD_CONTRACT, "kind": "enforcement_action", "record_key": action,
         "source": _source(provider, number, url, revision=hashlib.sha256(raw).hexdigest()[:16]),
         "authority": authority, "action_number": number, "action_type": "final_notice",
         "action_type_as_published": "Final Notice", "title": f"Final Notice: {addressee}",
         "status_as_published": "Final Notice", "source_status": "corrected" if amended else "published",
         "initiated_on": None, "decided_on": decided, "published_on": decided,
         "legal_bases": _references(action_text, reasons_text), "charges_as_published":
             _organisational(_sentences(reasons_text, "breach", "failed", "failing"), [])[:5],
         "outcome_as_published": _clean(" ".join(_organisational(_sentences(
             action_text, "imposes", "publish", "censure", "prohibit", "cancel", "tribunal"), []))) or None,
         "settlement": settlement,
         "related_identifiers": [{"scheme": "gb-fca-frn", "value": frn.group(1),
                                  "type_as_published": "Firm Reference Number"}] if frn else [],
         "court_cases": [], "related_references": _case_references(flat), "natural_person_respondents": 0,
         "action_url": url},
        _document(action, authority, number, provider, url, doc_type="Final Notice", title=f"Final Notice: {addressee}",
                  date=dated.group(1) if dated else None, raw=raw, amended_on=amended.group(1) if amended else None,
                  correction=amended.group(0) if amended else None),
    ]
    return records + respondents + penalties + appeal_records


# ----------------------------------------------------------------- EPA ECHO


ECHO_SITE = "https://echodata.epa.gov"
ECHO_PENALTIES = (("FedPenalty", "federal_penalty", "Federal Penalty"),
                  ("StateLocalPenalty", "state_local_penalty", "State/Local Penalty"),
                  ("SEPCost", "supplemental_environmental_project", "SEP Cost"),
                  ("CostRecovery", "cost_recovery", "Cost Recovery"),
                  ("ComplianceActionCost", "compliance_action_cost", "Compliance Action Cost"))
ECHO_LAWS = {"CAA": "Clean Air Act", "CWA": "Clean Water Act", "RCRA": "Resource Conservation and Recovery Act",
             "SDWA": "Safe Drinking Water Act", "CERCLA": "CERCLA", "TSCA": "Toxic Substances Control Act",
             "FIFRA": "Federal Insecticide, Fungicide, and Rodenticide Act",
             "EPCRA": "Emergency Planning and Community Right-to-Know Act"}


def parse_echo_case(responses: Mapping[str, bytes], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    from src.kb.enforcement_records import action_key

    try:
        payload = json.loads(responses["case"].decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EnforcementFormatError("schema_drift", "ECHO response is not valid UTF-8 JSON") from exc
    results = payload.get("Results") if isinstance(payload, Mapping) else None
    if not isinstance(results, Mapping) or not results.get("CaseNumber"):
        raise EnforcementFormatError("schema_drift", "ECHO response has no Results.CaseNumber")
    number = str(results["CaseNumber"]).strip()
    if number != unit["case_number"]:
        raise EnforcementFormatError("schema_drift", "ECHO returned a different case than declared")
    authority, provider = "us-epa", "us-epa-echo"
    action = action_key(authority, number)
    url = f"https://echo.epa.gov/enforcement-case-report?id={number}"
    civil = str(results.get("CivilCriminal") or "").casefold()
    names = [(d.get("DefendantName"), "defendant", "Defendant", []) for d in results.get("Defendants") or []
             if isinstance(d, Mapping)]
    respondents, individuals = _respondents(action, authority, number, provider, url, names)
    if not respondents:
        raise EnforcementFormatError("schema_drift", "declined: a case whose defendants are all individuals is "
                                                     "outside the bounded coverage (EN01)")
    laws = []
    for law in results.get("Laws") or []:
        acronym = str(law.get("Law") or "").strip().upper()
        for section in [s.strip() for s in str(law.get("Sections") or "").split(",") if s.strip()] or [""]:
            name = ECHO_LAWS.get(acronym, acronym)
            laws.append(f"{name} Section {section}".strip() if section else name)
    facilities = []
    for item in results.get("Facilities") or []:
        lat, lon = item.get("Latitude"), item.get("Longitude")
        coords = lat not in (None, "") and lon not in (None, "")
        facilities.append({"frs_id": str(item.get("RegistryID") or "") or None,
                           "name_as_published": _clean(item.get("FacilityName")),
                           "latitude": float(lat) if coords else None, "longitude": float(lon) if coords else None,
                           "coordinate_source": "ECHO facility coordinates (FRS)" if coords else None,
                           "state": _clean(item.get("State"))})
    settled_on = _day(results.get("SettlementDate"))
    penalties = []
    published = results.get("Penalties") if isinstance(results.get("Penalties"), Mapping) else {}
    for field, kind, label in ECHO_PENALTIES:
        value = _clean(published.get(field))
        found = amounts(value)
        penalties.append(_penalty(action, authority, number, provider, url, kind, label,
                                  found[0][0] if found else None, found[0][1] if found else None, imposed_on=settled_on,
                                  note=None if found else f"{field} not published for this case"))
    court_cases = []
    if results.get("CourtDocketNumber"):
        court_cases.append({"docket_number": str(results["CourtDocketNumber"]), "court": _clean(results.get("Court")),
                            "caption": minimise(results.get("CaseName"), individuals)})
    outcome = _clean(results.get("EnforcementOutcome"))
    records = [
        _authority_record(authority, provider),
        {"contract": RECORD_CONTRACT, "kind": "enforcement_action", "record_key": action,
         "source": _source(provider, number, url, revision=results.get("DataRefreshDate")),
         "authority": authority, "action_number": number,
         "action_type": "criminal" if civil == "criminal" else (
             "civil_judicial" if "judicial" in str(results.get("CaseCategoryDesc") or "").casefold()
             else "administrative_formal"),
         "action_type_as_published": _clean(results.get("CaseCategoryDesc")) or "not published",
         "title": minimise(results.get("CaseName"), individuals),
         "status_as_published": _clean(results.get("CaseStatusDesc")), "source_status": "published",
         "initiated_on": _day(results.get("FiledDate")), "decided_on": settled_on, "published_on": None,
         "legal_bases": laws, "charges_as_published": [minimise(v, individuals) for v in
                                                      [results.get("ViolationDescription")] if v],
         "outcome_as_published": minimise(outcome, individuals), "settlement": _settlement(outcome),
         "related_identifiers": [{"scheme": "frs", "value": f["frs_id"], "type_as_published": "FRS Registry ID"}
                                 for f in facilities if f["frs_id"]],
         "court_cases": court_cases, "related_references": [], "natural_person_respondents": len(individuals),
         "facilities": facilities, "page_revision": _clean(results.get("DataRefreshDate")), "action_url": url},
    ]
    return records + respondents + penalties


# ----------------------------------------------------------------- EDPB Article 60


EDPB_SITE = "https://www.edpb.europa.eu"


def _sa(text: Any) -> dict[str, Any] | None:
    name = _clean(text)
    if not name:
        return None
    lowered = name.casefold()
    country = next((code for word, code in EDPB_SA.items() if word in lowered), None)
    return {"name_as_published": name, "country": country,
            "code": f"eu-dpa-{country.lower()}" if country else None}


def parse_edpb_entry(responses: Mapping[str, bytes], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    from src.kb.enforcement_records import action_key

    raw = responses["entry"]
    page = _page(raw)
    pairs = page.pairs
    slug = str(unit["slug"])
    url = f"{EDPB_SITE}/art-60-final-decisions/{slug}_en"
    lead = _sa(pairs.get("lead supervisory authority") or pairs.get("lsa"))
    if not lead or not lead["code"]:
        raise EnforcementFormatError("schema_drift", "EDPB entry has no recognisable lead supervisory authority")
    authority, provider, number = lead["code"], "edpb-art60", slug
    action = action_key(authority, number)
    concerned = [c for c in (_sa(part) for part in re.split(r";|\n", pairs.get("concerned supervisory authorities")
                                                              or pairs.get("csas") or "")) if c]
    controller = _clean(pairs.get("controller/processor") or pairs.get("controller"))
    names = [(controller, "controller", "Controller/processor", [])] if controller and \
        controller.casefold() not in {"not published", "anonymised", "n/a"} else []
    respondents, individuals = _respondents(action, authority, number, provider, url, names)
    measures = [m.strip() for m in re.split(r";|,", pairs.get("decision") or pairs.get("corrective measures") or "")
                if m.strip()]
    fine_text = _clean(pairs.get("fine"))
    found = amounts(fine_text)
    decided = _day(pairs.get("date of final decision"))
    penalties = [_penalty(action, authority, number, provider, url, "administrative_fine", "Fine",
                          found[0][0] if found else None, found[0][1] if found else None, imposed_on=decided,
                          note=None if found else f"fine as published: {fine_text or 'not published'}")] \
        if fine_text or any("fine" in m.casefold() for m in measures) else []
    register = _clean(pairs.get("register number") or pairs.get("national decision reference"))
    summary = next((href for href, label in page.links if "summary" in label.casefold()), None)
    legal = pairs.get("legal reference(s)") or pairs.get("legal references") or ""
    records = [
        _authority_record(authority, provider, name=lead["name_as_published"], jurisdiction=lead["country"],
                          url=f"{EDPB_SITE}/about-edpb/about-edpb/members_en"),
        {"contract": RECORD_CONTRACT, "kind": "enforcement_action", "record_key": action,
         "source": _source(provider, number, url, revision=hashlib.sha256(raw).hexdigest()[:16]),
         "authority": authority, "action_number": number, "action_type": "art60_final_decision",
         "action_type_as_published": "Article 60 final decision",
         "title": minimise(page.title, individuals), "status_as_published": "Final decision",
         "source_status": "published", "initiated_on": None, "decided_on": decided, "published_on": None,
         "legal_bases": _references(legal, context="gdpr") or [x.strip() for x in legal.split(";") if x.strip()],
         "charges_as_published": [], "outcome_as_published": "; ".join(measures) or None,
         "settlement": {"settled_as_published": None, "admission_as_published": None},
         "related_identifiers": [{"scheme": "edpb-register", "value": register,
                                  "type_as_published": "Register number"}] if register else [],
         "court_cases": [], "related_references": [], "natural_person_respondents": len(individuals),
         "lead_authority": lead, "concerned_authorities": concerned, "corrective_measures_as_published": measures,
         "action_url": url},
    ]
    if summary:
        records.append(_child("enforcement_decision", action, authority, number, provider, url, [summary],
                              document_type_as_published="Summary of the final decision", title=page.title,
                              document_date=decided, url=_absolute(EDPB_SITE, summary), content_sha256=None,
                              language="en", amended_on=None, correction_as_published=None,
                              source_status="published"))
    return records + respondents + penalties


# ----------------------------------------------------------------- selection and requests


def _units(fmt: str, selection: Mapping[str, Any]) -> list[dict[str, Any]]:
    key = FORMATS[fmt]["unit"]
    units = [dict(u) if isinstance(u, Mapping) else {"id": u} for u in selection.get(key) or []]
    if not 1 <= len(units) <= MAX_UNITS:
        raise SourcePackError("invalid_manifest", f"an enforcement selection names 1-{MAX_UNITS} {key}")
    for unit in units:
        if fmt == "sec-release-html" and not _SEC_PATH.fullmatch(str(unit.get("path") or "")):
            raise SourcePackError("invalid_manifest", "SEC units name a litigation-release or administrative-"
                                                      "proceeding page path")
        if fmt in {"fca-final-notice-text", "edpb-art60-html"} and not _SLUG.fullmatch(str(unit.get("slug") or "")):
            raise SourcePackError("invalid_manifest", "FCA and EDPB units name a page slug")
        if fmt == "echo-case-json" and not _ECHO_CASE.fullmatch(str(unit.get("case_number") or "")):
            raise SourcePackError("invalid_manifest", "ECHO units name a case number (RR-YYYY-NNNN)")
    return units


def requests_for(fmt: str, unit: Mapping[str, Any]) -> dict[str, tuple[str, dict[str, Any]]]:
    """Named request paths (relative to the endpoint) and parameters for one selection unit."""
    if fmt == "sec-release-html":
        return {"page": (f"/enforcement-litigation/{unit['path']}", {})}
    if fmt == "fca-final-notice-text":
        return {"notice": (f"/publication/final-notices/{unit['slug']}.pdf", {})}
    if fmt == "echo-case-json":
        return {"case": ("/echo/case_rest_services.get_case_info", {"output": "JSON", "p_id": unit["case_number"]})}
    if fmt == "edpb-art60-html":
        return {"entry": (f"/art-60-final-decisions/{unit['slug']}_en", {})}
    raise SourcePackError("invalid_manifest", f"unknown enforcement format {fmt!r}")


def removal_marker(fmt: str, unit: Mapping[str, Any], status: int, path: str) -> dict[str, Any]:
    """A removed_by_source marker for a declared unit the publisher no longer serves (applied by the store)."""
    from src.kb.enforcement_records import action_key

    spec = FORMATS[fmt]
    number = {"sec-release-html": sec_release_number(str(unit.get("path"))), "fca-final-notice-text": unit.get("slug"),
              "echo-case-json": unit.get("case_number"), "edpb-art60-html": unit.get("slug")}[fmt]
    return {"contract": RECORD_CONTRACT, "kind": "removal", "provider": spec["provider"],
            "authority": spec["authority"], "action_number": number,
            "record_key": action_key(spec["authority"], number) if number and spec["authority"] else None,
            "unit": dict(unit), "http_status": status, "path": path}


_PARSERS: dict[str, Callable[..., list[dict[str, Any]]]] = {
    "sec-release-html": parse_sec_release,
    "fca-final-notice-text": parse_fca_notice,
    "echo-case-json": parse_echo_case,
    "edpb-art60-html": parse_edpb_entry,
}


def parse_unit(fmt: str, responses: Mapping[str, bytes], unit: Mapping[str, Any], *,
               content_types: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    if fmt not in _PARSERS:
        raise EnforcementFormatError("schema_drift", f"unknown enforcement format {fmt!r}")
    if fmt == "fca-final-notice-text":
        records = parse_fca_notice(responses, unit, content_type=(content_types or {}).get("notice", "text/plain"))
    else:
        records = _PARSERS[fmt](responses, unit)
    from src.kb.enforcement_records import EnforcementRecordError, validate_record

    try:
        return [validate_record(r) for r in records]
    except EnforcementRecordError as exc:
        raise EnforcementFormatError("schema_drift", str(exc)) from exc


def enforcement_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    declared = dict(source.get("enforcement") or {})
    fmt = declared.get("format")
    if fmt not in FORMATS or FORMATS[fmt]["provider"] != declared.get("provider"):
        raise SourcePackError("invalid_manifest", "enforcement sources declare a matching provider and format")
    if declared.get("live_verification") not in {"unverified-live", "verified-live"}:
        raise SourcePackError("invalid_manifest", "enforcement sources state their LIVE_VERIFICATION status")
    _units(fmt, dict(declared.get("selection") or {}))
    if dict(source.get("auth") or {}).get("kind") != "none":
        raise SourcePackError("invalid_manifest", "enforcement sources are unauthenticated")
    return declared


class EnforcementAdapter:
    """Fetch one declared selection unit per page from the source's endpoint host and emit its records."""

    accepts_transport = True
    connector = CONNECTOR

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None) -> None:
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        del secret  # every enforcement source is unauthenticated
        self.source = json.loads(json.dumps(source))
        self.declared = enforcement_declaration(self.source)
        self.format = self.declared["format"]
        self.provider = self.declared["provider"]
        self.units = _units(self.format, dict(self.declared.get("selection") or {}))
        self.live = transport is None
        if transport is None:
            from functools import partial

            transport = partial(HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"]))
        self.transport = transport
        self.definition = {
            "contract": ADAPTER_CONTRACT, "source_id": source["source_id"], "connector": source["connector"],
            "endpoint": source["endpoint"], "operations": list(source["operations"]),
            "source_hash": source["source_hash"], "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"], "limits": source["budgets"],
            "enforcement": {"provider": self.provider, "format": self.format, "units": len(self.units)},
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _check(self, request: Mapping[str, Any]) -> None:
        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError("operation_forbidden", "operation is not declared by the source")
        if set(request) - {"operation", "parameters", "limit", "from_ms", "to_ms"}:
            raise SourcePackError("parameter_forbidden", "runtime adapter received undeclared controls")
        if dict(request.get("parameters") or {}):
            raise SourcePackError("parameter_forbidden", "enforcement runs fetch the declared selection only")

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json, text/html, application/pdf, text/plain"}
        if self.provider == "us-sec":
            agent = os.environ.get(SEC_USER_AGENT_ENV, "").strip()
            if agent:
                headers["User-Agent"] = agent
            elif self.live:
                raise SourcePackError("source_unavailable", f"SEC fair access requires a declared User-Agent; set "
                                                            f"{SEC_USER_AGENT_ENV} (operator name and contact)")
        return headers

    def _get(self, path: str, params: Mapping[str, Any]) -> tuple[bytes, dict[str, Any], str]:
        from src.ingestion.source_pack_runtime import _retry_after_ms

        endpoint = self.source["endpoint"].rstrip("/")
        url = endpoint + path
        host = (urlsplit(endpoint).hostname or "").casefold()
        ordered = dict(sorted(params.items()))
        response = self.transport(url=url, params=ordered, headers=self._headers(),
                                  timeout=int(self.definition["limits"]["timeout_ms"]) / 1000)
        final_host = (urlsplit(str(response.get("final_url") or url)).hostname or "").casefold()
        if final_host != host:
            raise SourcePackError("network_policy", "enforcement response was served from another host")
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
        if status >= 400 and status not in {404, 410}:
            raise SourcePackError("schema_drift", f"request returned HTTP {status}")
        query = urlencode(ordered)
        return raw, {"path": path + ("?" + query if query else ""), "status": status,
                     "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
                     "origin": "fixture" if response.get("origin") == "fixture" else "live"}, \
            str(headers_in.get("content-type") or "text/plain")

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage

        self._check(request)
        index = 0 if cursor is None else int(cursor)
        if not 0 <= index < len(self.units):
            raise SourcePackError("cursor_drift", "cursor is outside the declared selection")
        unit = self.units[index]
        responses, requests, types, removed = {}, [], {}, None
        for name, (path, params) in requests_for(self.format, unit).items():
            raw, receipt, content_type = self._get(path, params)
            requests.append({"name": name, **receipt})
            if receipt["status"] in {404, 410}:
                removed = removal_marker(self.format, unit, receipt["status"], receipt["path"])
                break
            responses[name], types[name] = raw, content_type
        origin = "fixture" if all(r["origin"] == "fixture" for r in requests) else "live"
        if removed is not None:
            records = [removed]
        else:
            try:
                records = parse_unit(self.format, responses, unit, content_types=types)
            except EnforcementFormatError as exc:
                raise SourcePackError("schema_drift", f"{exc.code}: {exc}") from exc
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        if len(records) > limit:
            raise SourcePackError("budget_exhausted", "unit has more records than the run's result budget")
        receipt = {
            "contract": "noesis-enforcement-acquisition-receipt-v1", "source_id": self.source["source_id"],
            "provider": self.provider, "format": self.format, "unit_index": index, "unit": unit,
            "requests": requests, "records": len(records), "evidence_origin": origin,
            "removed_by_source": removed is not None,
            "live_verification": self.declared["live_verification"], "final_page": index + 1 >= len(self.units),
        }
        out = []
        for record in records:
            if record["kind"] == "removal":
                record = {**record, "evidence_origin": origin}
                key = record.get("record_key") or f"removal:{self.source['source_id']}:{index}"
                out.append({"id": key, "title": f"removed by source: {key}", "url": None, "language": "en",
                            "published_at": None, "updated_at": None,
                            "content": json.dumps(record, sort_keys=True, ensure_ascii=False),
                            "enforcement_record": record, "enforcement_receipt": receipt})
                continue
            record = {**record, "source": {**record["source"], "evidence_origin": origin}}
            out.append({"id": record["record_key"], "title": _item_title(record), "url": record["source"].get("url"),
                        "language": "en", "published_at": _item_date(record),
                        "updated_at": record["source"].get("revision"),
                        "content": json.dumps(record, sort_keys=True, ensure_ascii=False),
                        "enforcement_record": record, "enforcement_receipt": receipt})
        next_cursor = None if receipt["final_page"] else str(index + 1)
        return RuntimePage(tuple(out), next_cursor, sum(r["bytes"] for r in requests), receipt=receipt)


def _item_title(record: Mapping[str, Any]) -> str:
    for field in ("title", "name_as_published", "document_type_as_published", "penalty_type_as_published",
                  "forum_as_published"):
        if record.get(field):
            return str(record[field])
    return record["record_key"]


def _item_date(record: Mapping[str, Any]) -> str | None:
    for field in ("published_on", "decided_on", "initiated_on", "document_date", "imposed_on"):
        if record.get(field) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(record[field])):
            return record[field]
    return None


FIXTURE_SECRET = None
ADAPTERS = {CONNECTOR: EnforcementAdapter}


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
    adapter = EnforcementAdapter(source, transport=fixture_transport(list(fixture["native_pages"])))
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
    "LIVE_VERIFICATION", "MINIMISATION", "PROVIDER_CONTRACTS", "RECORD_CONTRACT", "REVIEW_BOUNDARY",
    "EnforcementAdapter", "EnforcementFormatError", "amounts", "enforcement_declaration", "fixture_transport",
    "minimise", "parse_unit", "party_type", "replay_native_fixture", "requests_for",
]
