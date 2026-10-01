"""Regulatory enforcement acquisition for the Legal pack (#2651, EN01, EN03-EN06).

One native connector, ``enforcement``, registered in the ``legal-research``
source pack, reads a bounded, declared selection from one publisher per source
and emits ``noesis-enforcement-record-v1`` records (:mod:`src.kb.enforcement_records`)
as the regulator published them:

* ``sec-litigation-release-html`` - SEC litigation releases (civil actions in
  federal court), one declared ``LR-`` release per unit; respondents as named
  (with a CIK where the release states one), charges as stated, the outcome
  sentences and the stated admission wording verbatim, sanctions as stated and
  the related court case as a citation;
* ``sec-admin-proceeding-html`` - SEC administrative-proceeding pages, one
  declared release (``33-``, ``34-``, ``IA-``, ``IC-``, ``AE-``) per unit, keyed
  by release and ``3-`` file number;
* ``fca-final-notice-pdf`` - FCA final notices (PDF), one declared notice per
  unit: the addressee and Firm Reference Number, the notice date, the rule
  breaches cited, the penalty after and before the settlement discount as
  published and any Upper Tribunal reference;
* ``echo-case-report-json`` - EPA ECHO civil (``get_case_report``) or criminal
  (``get_crcase_report``) case reports, one declared case number per unit:
  statutes and sections, defendants as named, facilities with FRS registry ids
  and the published coordinates, and federal penalty, state/local penalty,
  supplemental environmental project cost, cost recovery and compliance action
  cost as separate published fields;
* ``edpb-art60-html`` - EDPB register of Article 60 final decisions, one
  declared register entry per unit: lead and concerned supervisory
  authorities, legal references, decision and corrective measures as published,
  the controller only where the register publishes one, and fines as stated.

Every page is one selection unit and is all-or-nothing: a response from another
host is a network-policy failure; a declared notice the publisher no longer
serves (HTTP 404/410) becomes a removal revision, never a deletion. Receipts
name every request path, status and response digest, and count what the EN01
minimisation decision withheld. ``PROVIDER_CONTRACTS``, ``DECLINED``,
``IDENTIFIERS``, ``BOUNDED_COVERAGE``, ``MINIMISATION`` and
``LIVE_VERIFICATION`` are the machine-readable copy of the EN01 audit
(``docs/development/enforcement-evidence/source-audit.md``).

Nothing here scores risk or compliance, infers wrongdoing from an initiated
action, merges a settled "neither admit nor deny" outcome into a finding or
profiles a named individual.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import zlib
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlencode, urlsplit

from src.ingestion.source_packs import SourcePackError
from src.kb.enforcement_records import MINIMISATION

ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
RECORD_CONTRACT = "noesis-enforcement-record-v1"
CONNECTOR = "enforcement"
MAX_UNITS = 20
REVIEW_BOUNDARY = ("Enforcement actions, respondents, decisions, penalties and appeals are kept as each regulator "
                   "published them. An initiated action is not a finding of wrongdoing; a settlement 'without "
                   "admitting or denying' keeps that wording; penalties are never converted or summed; natural "
                   "persons are pseudonymised; nothing here is a risk or compliance score or legal advice.")
FORMATS: dict[str, dict[str, Any]] = {
    "sec-litigation-release-html": {"provider": "us-sec", "authority": "us-sec", "unit": "releases"},
    "sec-admin-proceeding-html": {"provider": "us-sec", "authority": "us-sec", "unit": "proceedings"},
    "fca-final-notice-pdf": {"provider": "uk-fca", "authority": "uk-fca", "unit": "notices"},
    "echo-case-report-json": {"provider": "us-epa-echo", "authority": "us-epa", "unit": "cases"},
    "edpb-art60-html": {"provider": "edpb", "authority": None, "unit": "entries"},
}
SEC_ATTRIBUTION = "Source: U.S. Securities and Exchange Commission (US government work)."
FCA_ATTRIBUTION = ("Contains information published by the Financial Conduct Authority under the Open Government "
                   "Licence v3.0 as stated in the FCA copyright notice (verify).")
EPA_ATTRIBUTION = "Source: U.S. EPA Enforcement and Compliance History Online (ECHO) (US government work)."
EDPB_ATTRIBUTION = "Source: European Data Protection Board, register of Article 60 final decisions."
LICENCES = {"us-sec": "us-government-work", "uk-fca": "ogl-3.0-fca-copyright-notice",
            "us-epa-echo": "us-government-work", "edpb": "edpb-reuse-with-acknowledgement"}
PUBLISHERS = {"us-sec": "U.S. Securities and Exchange Commission", "uk-fca": "Financial Conduct Authority",
              "us-epa-echo": "U.S. Environmental Protection Agency (ECHO)",
              "edpb": "European Data Protection Board (register of Article 60 final decisions)"}
SITES = {"us-sec": "https://www.sec.gov", "uk-fca": "https://www.fca.org.uk", "us-epa-echo": "https://echo.epa.gov",
         "edpb": "https://www.edpb.europa.eu"}

# EN01 access decisions (recorded 2026-09-30). The official pages could not be fetched from this runtime (the egress
# proxy refused sec.gov, fca.org.uk, echo.epa.gov and edpb.europa.eu); terms and endpoints below come from search
# snippets of those pages and are marked ``verify`` until a dated live run (EN14, #2720) confirms them.
PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "us-sec": {
        "publisher": PUBLISHERS["us-sec"],
        "endpoints": ["https://www.sec.gov/enforcement-litigation/litigation-releases/{lr-nnnnn}",
                      "https://www.sec.gov/enforcement-litigation/administrative-proceedings/{release}",
                      "https://www.sec.gov/rss/litigation/litreleases.xml (discovery only)"],
        "formats": ["sec-litigation-release-html", "sec-admin-proceeding-html"],
        "access": "bounded page acquisition of declared release pages; the Drupal field classes read "
                  "(field--name-field-release-number, -file-number, -respondents, -publish-date, -body) are verify",
        "authentication": "none; every request carries a declared User-Agent with a contact, as the SEC fair-access "
                          "policy requires (verify)",
        "rate_limits": "SEC fair access: at most 10 requests per second per user; excess is blocked for 10 minutes "
                       "(search snippet of the SEC webmaster FAQ, 2026-09-30, verify); at most 20 releases per run",
        "revisions": "article:modified_time (or the page digest) is the page revision; a changed release is a new "
                     "action revision; a withdrawn page (404/410) is a removal revision",
        "licence": "US government work, public domain (17 U.S.C. § 105)",
        "attribution": SEC_ATTRIBUTION,
        "documents": "the release page is a versioned notice document (digest kept); complaints, orders and "
                     "judgments are linked, not mirrored",
        "access_decision": "unverified-live",
    },
    "uk-fca": {
        "publisher": PUBLISHERS["uk-fca"],
        "endpoints": ["https://www.fca.org.uk/publication/final-notices/{slug}.pdf",
                      ("https://www.fca.org.uk/news/search-results?np_category=notices%20and%20decisions-final%20"
                       "notices (discovery only)")],
        "formats": ["fca-final-notice-pdf"],
        "access": "bounded acquisition of declared final-notice PDFs; text is read from the PDF text layer "
                  "(pdfminer.six when installed, otherwise a stdlib reader for simple text layers); a notice whose "
                  "text layer cannot be read fails the unit with schema_drift (verify on real notices)",
        "authentication": "none",
        "rate_limits": "none published (verify); at most 20 declared notices per run",
        "revisions": "the PDF digest is the document revision; a replaced PDF is a new revision of the notice and "
                     "action; a withdrawn notice (404/410) is a removal revision",
        "licence": "FCA copyright notice: information made available under the Open Government Licence; other "
                   "re-use needs FCA permission (search snippet of https://www.fca.org.uk/legal, 2026-09-30, verify)",
        "attribution": FCA_ATTRIBUTION,
        "documents": "the final notice PDF is the versioned notice document (digest, bytes, date); text is parsed, "
                     "not mirrored",
        "access_decision": "unverified-live",
    },
    "us-epa-echo": {
        "publisher": PUBLISHERS["us-epa-echo"],
        "endpoints": ["https://echodata.epa.gov/echo/case_rest_services.get_case_report?p_id={case}&output=JSON",
                      "https://echodata.epa.gov/echo/case_rest_services.get_crcase_report?p_id={case}&output=JSON"],
        "formats": ["echo-case-report-json"],
        "access": "documented ECHO Enforcement Case REST services (GET, XML/JSON/JSONP; search snippet of "
                  "https://echo.epa.gov/tools/web-services, 2026-09-30); the JSON key names are verify",
        "authentication": "none",
        "rate_limits": "none published (verify); one report per declared case; at most 20 cases per run",
        "revisions": "a changed case report is a new action revision (ICIS updates are republished weekly, verify)",
        "licence": "US government work, public domain",
        "attribution": EPA_ATTRIBUTION,
        "documents": "the case report response is the versioned document (digest kept)",
        "access_decision": "unverified-live",
    },
    "edpb": {
        "publisher": PUBLISHERS["edpb"],
        "endpoints": [("https://www.edpb.europa.eu/our-work-tools/consistency-findings/"
                       "register-for-article-60-final-decisions/decision-no-{entry}_en")],
        "formats": ["edpb-art60-html"],
        "access": "bounded page acquisition of declared register entries; the field classes read (LSA, CSA, legal "
                  "reference, decision, keywords, date, controller, identifier) are verify",
        "authentication": "none",
        "rate_limits": "none published (verify); at most 20 register entries per run",
        "revisions": "the page modified time (or digest) is the revision; a corrected entry is a new revision",
        "licence": "EDPB copyright notice: reuse authorised for commercial and non-commercial purposes with "
                   "acknowledgement and without distorting the meaning (search snippet of "
                   "https://www.edpb.europa.eu/copyright_en, 2026-09-30, verify)",
        "attribution": EDPB_ATTRIBUTION,
        "documents": "final decisions and English summaries linked, not mirrored; the register page digest is kept",
        "access_decision": "unverified-live",
    },
}
LIVE_VERIFICATION = {
    provider: {"status": contract["access_decision"], "intended": "verified-live",
               "note": "no dated live run from this runtime; offline fixtures only (EN14, #2720)"}
    for provider, contract in PROVIDER_CONTRACTS.items()
}
DECLINED = {
    "sec-trading-suspensions": {"source": "SEC trading suspensions",
                                "reason": "a suspension is not an enforcement action against a respondent"},
    "fca-decision-and-warning-notices": {"source": "FCA decision notices and warning notice statements",
                                         "reason": "not final; a referred decision notice is recorded only through "
                                                   "the final notice or appeal reference that follows it"},
    "fca-individual-notices": {"source": "FCA final notices addressed only to individuals",
                               "reason": "EN01 minimisation: individual-only actions are not recorded"},
    "echo-bulk-downloads": {"source": "ECHO case data downloads (ZIP)",
                            "reason": "bounded per-case acquisition is enough for entity and authority answers"},
    "national-dpa-registers": {"source": "national data-protection authority decision registers",
                               "reason": "outside the Article 60 register; different terms per authority"},
}
IDENTIFIERS = {
    "us-sec": ["litigation release LR-nnnnn", "administrative release (33-/34-/IA-/IC-/AE-nnnnn)",
               "administrative file number 3-nnnnn", "respondent CIK where the release states one",
               "federal civil action number (citation)"],
    "uk-fca": ["final notice slug", "Firm Reference Number (FRN)", "Upper Tribunal reference (FS/yyyy/nnnn)"],
    "us-epa-echo": ["ECHO case number", "ICIS activity id", "FRS registry id per facility", "court docket number"],
    "edpb": ["register entry number", "EDPBI identifier", "lead and concerned supervisory authority codes"],
}
BOUNDED_COVERAGE = {
    "seed": "companies already acquired by the Corporate Ownership sources (fixtures: the fictional Exampla group); "
            "actions are declared by release, notice, case number or register entry, never crawled",
    "window": "actions published or decided from 2020-01-01; earlier decisions of an in-window action are kept",
    "per_run": f"at most {MAX_UNITS} declared units per source",
    "places": "EPA facilities keep their FRS registry id and published coordinates only; no geocoding",
}
COUNTRY_CODES = {"IE", "FR", "DE", "NL", "LU", "BE", "ES", "IT", "AT", "SE", "DK", "FI", "PL", "PT", "CZ", "HU", "SK",
                 "SI", "HR", "RO", "BG", "GR", "EL", "CY", "MT", "LV", "LT", "EE", "NO", "IS", "LI"}
_LR = re.compile(r"^LR-\d{4,6}$")
_AP = re.compile(r"^(33|34|IA|IC|AE)-\d{4,6}$")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{2,150}$")
_CASE = re.compile(r"^[A-Z0-9][A-Z0-9-]{3,40}$")
_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
           "november", "december")
_US_DATE = re.compile(r"\b(" + "|".join(m.capitalize() for m in _MONTHS) + r")\s+(\d{1,2}),\s+(\d{4})")
_UK_DATE = re.compile(r"\b(\d{1,2})\s+(" + "|".join(m.capitalize() for m in _MONTHS) + r")\s+(\d{4})")
_ABBREVIATIONS = {"no", "nos", "inc", "co", "ltd", "v", "vs", "u.s", "mr", "ms", "mrs", "dr", "st", "al", "corp",
                  "plc", "s.d.n.y", "d.d.c", "n.d", "s.d", "e.d", "art", "para", "p", "cf", "e.g", "i.e", "b.v"}
ADMISSION = re.compile(r"(without\s+admitting\s+or\s+denying[^.;,]*|neither\s+admit(?:s|ted)?\s+nor\s+"
                       r"den(?:y|ies|ied)[^.;,]*|admit(?:s|ted)\s+(?:the\s+)?(?:facts|findings|allegations)[^.;,]*)",
                       re.IGNORECASE)
FIGURE = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?(?:\s?million)?|\d+(?:\.\d+)?(?:\s?million)?"
SETTLED = re.compile(r"\b(consent(?:ed|s)? to|agreed to (?:settle|resolve)|offer of settlement|settled|"
                     r"settlement)\b", re.IGNORECASE)
OUTCOME_CUES = re.compile(r"\b(consent(?:ed|s)?|final judgment|ordered|orders?|imposes|imposed|enjoin|cease-and-desist|"
                          r"censure|revok|prohibit|bar(?:red|s)?|penalt|fine|reprimand|dismiss)", re.IGNORECASE)
APPEAL_CUES = re.compile(r"\b(appeal(?:ed|s)?|Upper Tribunal|petition for review|referred the)\b", re.IGNORECASE)
_UT_REF = re.compile(r"\b(?:FS|FIN)/\d{4}/\d{4}\b")
_CIVIL_ACTION = re.compile(r"Civil Action No\.\s*([0-9]{1,2}:\d{2}-[a-z]{2}-\d{3,6}(?:-[A-Z]{2,4})?)\s*\(([^)]+)\)")
_FILE_NO = re.compile(r"\bFile No\.\s*(3-\d{4,6})")
_SEC_BASES = re.compile(
    r"(Sections?\s+\d+[A-Za-z]?(?:\([A-Za-z0-9]+\))*(?:\s+and\s+\d+[A-Za-z]?(?:\([A-Za-z0-9]+\))*)?\s+of\s+the\s+"
    r"(?:Securities\s+Act(?:\s+of\s+1933)?|Securities\s+Exchange\s+Act(?:\s+of\s+1934)?|Exchange\s+Act|"
    r"Investment\s+Advisers\s+Act(?:\s+of\s+1940)?|Advisers\s+Act|Investment\s+Company\s+Act(?:\s+of\s+1940)?)"
    r"|(?:Exchange\s+Act\s+)?Rules?\s+\d+[a-z]?-\d+[a-z]?(?:\([a-z0-9]+\))*"
    r"|\d{1,2}\s+U\.S\.C\.\s+§+\s*\d+[a-z0-9-]*(?:\([a-z0-9]+\))*)")
_FCA_BASES = re.compile(r"(Principle\s+\d{1,2}\b|\b(?:SYSC|COBS|MAR|PRIN|DEPP|SUP|CONC|ICOBS|MCOB|CASS|DISP|APER|"
                        r"COCON|FIT|GEN|MLR)\s+\d+(?:\.\d+)*[RGED]?\b|Financial Services and Markets Act 2000|"
                        r"Money Laundering Regulations \d{4}|section\s+\d+[A-Z]?(?:\(\d+\))?\s+of\s+the\s+Act)")
_MONEY = re.compile(r"(£|\$|€|EUR\s?|GBP\s?|USD\s?)\s?(\d[\d,\s]*(?:\.\d+)?)(\s?(?:million|m|billion|bn)\b)?",
                    re.IGNORECASE)
_CURRENCY = {"£": "GBP", "$": "USD", "€": "EUR", "eur": "EUR", "gbp": "GBP", "usd": "USD"}
SEC_PENALTIES = (("civil_penalty", r"civil (?:money )?penalt(?:y|ies)"), ("disgorgement", r"disgorgement"),
                 ("prejudgment_interest", r"prejudgment interest"))
ECHO_PENALTIES = (("federal_penalty", "FedPenalty", "Federal penalty"),
                  ("state_local_penalty", "StateLocalPenalty", "State/local penalty"),
                  ("sep_cost", "SEPCost", "Supplemental environmental project cost"),
                  ("cost_recovery", "CostRecovery", "Cost recovery"),
                  ("compliance_action_cost", "ComplianceActionCost", "Compliance action cost"))
ECHO_ACTION_TYPES = {"civil judicial": "civil_judicial_case", "administrative - formal": "administrative_case",
                     "administrative": "administrative_case", "criminal": "criminal_case"}


class EnforcementFormatError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ----------------------------------------------------------------- small helpers


def _clean(value: Any) -> str | None:
    text = " ".join(unescape(str(value if value is not None else "")).split())
    return text or None


def _day(value: Any) -> str | None:
    text = str(value or "").strip()
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", text)
    if match:
        return match.group(1)
    match = re.match(r"^(\d{2})/(\d{2})/(\d{4})$", text)
    if match:
        return f"{match.group(3)}-{match.group(1)}-{match.group(2)}"
    for pattern, order in ((_US_DATE, (3, 1, 2)), (_UK_DATE, (3, 2, 1))):
        found = pattern.search(text)
        if found:
            year, month, dom = (found.group(i) for i in order)
            return f"{year}-{_MONTHS.index(month.lower()) + 1:02d}-{int(dom):02d}"
    return None


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EnforcementFormatError("schema_drift", "response is not valid UTF-8 JSON") from exc


def sentences(text: Any) -> list[str]:
    """Split published prose into sentences without breaking at common legal abbreviations."""
    text = " ".join(str(text or "").split())
    out, start = [], 0
    for match in re.finditer(r"\.\s+(?=[A-Z\[(])", text):
        token = text[:match.start()].rsplit(" ", 1)[-1].lower().lstrip("(")
        if token in _ABBREVIATIONS or re.fullmatch(r"[a-z]", token):
            continue
        out.append(text[start:match.start() + 1].strip())
        start = match.end()
    if text[start:].strip():
        out.append(text[start:].strip())
    return out


def money(text: Any) -> tuple[str | None, str | None]:
    """(amount as a decimal string, ISO currency) of the first published figure; never converted."""
    match = _MONEY.search(str(text or ""))
    if not match:
        return None, None
    currency = _CURRENCY.get(match.group(1).strip().lower()) or _CURRENCY.get(match.group(1).strip())
    digits = re.sub(r"[,\s]", "", match.group(2))
    try:
        value = Decimal(digits)
    except InvalidOperation:
        return None, currency
    scale = (match.group(3) or "").strip().lower()
    if scale in {"million", "m"}:
        value *= Decimal(1_000_000)
    elif scale in {"billion", "bn"}:
        value *= Decimal(1_000_000_000)
    return format(value.normalize(), "f"), currency


_EXTRA_FORMS = re.compile(r"\b(b\.v|n\.v|s\.a|s\.a\.s|sarl|s\.p\.a|spa|oyj|kgaa|kg)\b\.?", re.IGNORECASE)


def party_type(name: Any) -> str:
    """``organisation`` only when the published name carries a legal-form or public-body token (EN01).

    The Legal courts feature's token list (:func:`src.ingestion.courts_justice_sources.party_type`) plus the
    continental legal forms regulators publish (B.V., N.V., S.A., SARL, S.p.A., KG); anything else is treated as a
    natural person and pseudonymised.
    """
    from src.ingestion.courts_justice_sources import party_type as courts_party_type

    if courts_party_type(name) == "organisation" or _EXTRA_FORMS.search(str(name or "")):
        return "organisation"
    return "natural_person"


def redact(text: Any, persons: Mapping[str, str], protect: Sequence[str] = ()) -> str | None:
    """Replace each natural person's published name (and surname) by their action-scoped pseudonym.

    A surname that also occurs in an organisation's published name (``protect``) is only replaced as part of the
    full name, so organisation names are never altered.
    """
    value = _clean(text)
    if value is None:
        return None
    for name, pseudonym in sorted(persons.items(), key=lambda item: -len(item[0])):
        value = re.sub(re.escape(name), f"[{pseudonym}]", value, flags=re.IGNORECASE)
        surname = name.split()[-1]
        if len(surname) > 3 and not any(surname.lower() in org.lower() for org in protect):
            value = re.sub(rf"\b(?:Mr|Ms|Mrs|Dr)\.?\s+{re.escape(surname)}\b|\b{re.escape(surname)}\b(?!\])",
                           f"[{pseudonym}]", value)
    return value


def _source(provider: str, record_id: str, url: str | None, fmt: str, *, revision: Any = None,
            locator: Mapping[str, Any] | None = None) -> dict[str, Any]:
    source = {"provider": provider, "provider_record_id": record_id, "publisher": PUBLISHERS[provider],
              "license": LICENCES[provider], "format": fmt}
    if url:
        source["url"] = url
    if revision:
        source["revision"] = str(revision)
    if locator:
        source["locator"] = dict(locator)
    return source


# ----------------------------------------------------------------- HTML pages (SEC, EDPB)

class _Page(HTMLParser):
    """Drupal field items (with their ``<time datetime>``), the page title, the modified-time meta and linked
    documents (list items whose class names a document)."""

    VOID = frozenset({"meta", "br", "img", "link", "input", "hr", "wbr", "source"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.fields: dict[str, list[str]] = {}
        self.times: dict[str, list[str]] = {}
        self.documents: list[dict[str, Any]] = []
        self.title: list[str] = []
        self.modified: str | None = None
        self._depth = 0
        self._fields: list[tuple[str, int]] = []
        self._item: tuple[int, list[str]] | None = None
        self._title_depth: int | None = None
        self._doc: tuple[int, dict[str, Any]] | None = None
        self._anchor: int | None = None

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
        name = next((c[len("field--name-field-"):] if c.startswith("field--name-field-") else c[len("field--name-"):]
                     for c in classes if c.startswith("field--name-")), None)
        if name:
            self._fields.append((name, self._depth))
        if self._fields and self._item is None and "field__item" in classes:
            self._item = (self._depth, [])
        if self._fields and tag == "time" and attrs.get("datetime"):
            self.times.setdefault(self._fields[-1][0], []).append(str(attrs["datetime"]))
        if tag == "li" and any("document" in c for c in classes):
            self._doc = (self._depth, {"url": None, "title": [], "date": None})
        if self._doc is not None and tag == "a" and not self._doc[1]["url"]:
            self._doc[1]["url"] = attrs.get("href")
            self._anchor = self._depth
        if self._doc is not None and tag == "time":
            self._doc[1]["date"] = attrs.get("datetime")

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
        if self._anchor == depth:
            self._anchor = None
        if self._doc is not None and self._doc[0] == depth:
            entry = self._doc[1]
            entry["title"] = " ".join(" ".join(entry["title"]).split())
            self.documents.append(entry)
            self._doc = None
        self._depth = max(0, depth - 1)

    def handle_data(self, data):
        if self._title_depth is not None:
            self.title.append(data)
        if self._item is not None:
            self._item[1].append(data + " ")
        if self._doc is not None and self._anchor is not None:
            self._doc[1]["title"].append(data)


def _page(raw: bytes) -> _Page:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise EnforcementFormatError("schema_drift", "page is not UTF-8") from exc
    parser = _Page()
    parser.feed(text)
    if not parser.title:
        raise EnforcementFormatError("schema_drift", "the page has no title")
    return parser


def _first(fields: Mapping[str, list[str]], *names: str) -> str | None:
    for name in names:
        if fields.get(name):
            return fields[name][0]
    return None


# ----------------------------------------------------------------- PDF text layer (FCA)

def _pdf_string(data: bytes, start: int) -> tuple[str, int]:
    out, depth, index = bytearray(), 1, start
    while index < len(data) and depth:
        char = data[index]
        if char == 0x5C:  # backslash escape
            index += 1
            nxt = data[index:index + 1]
            if nxt and nxt in b"01234567":
                octal = re.match(rb"[0-7]{1,3}", data[index:index + 3]).group(0)
                out.append(int(octal, 8) & 0xFF)
                index += len(octal)
                continue
            out += {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f"}.get(nxt, nxt)
        elif char == 0x28:
            depth += 1
            out.append(char)
        elif char == 0x29:
            depth -= 1
            if depth:
                out.append(char)
        else:
            out.append(char)
        index += 1
    return out.decode("latin-1"), index


def _content_text(content: bytes) -> str:
    lines, current, index = [], [], 0
    while index < len(content):
        char = content[index:index + 1]
        if char == b"(":
            text, index = _pdf_string(content, index + 1)
            current.append(text)
            continue
        match = re.match(rb"(T\*|Td|TD|ET|'|\")(?=[\s\[(]|$)", content[index:index + 3])
        if match and (index == 0 or content[index - 1:index] in b" \n\r\t)]"):
            if current:
                lines.append("".join(current))
                current = []
            index += len(match.group(1))
            continue
        index += 1
    if current:
        lines.append("".join(current))
    return "\n".join(lines)


def pdf_text(raw: bytes) -> str:
    """The text layer of a PDF: pdfminer.six when installed, else a stdlib reader for simple text layers."""
    if not raw.startswith(b"%PDF-"):
        raise EnforcementFormatError("schema_drift", "the final notice is not a PDF")
    try:
        from io import BytesIO

        from pdfminer.high_level import extract_text  # type: ignore[import-not-found]
    except ImportError:
        extract_text = None
    if extract_text is not None:
        try:
            text = extract_text(BytesIO(raw))
        except Exception as exc:
            raise EnforcementFormatError("schema_drift", "the PDF text layer could not be read") from exc
    else:
        parts = []
        for match in re.finditer(rb"\bobj\b((?:(?!endobj).)*?)stream\r?\n", raw, re.DOTALL):
            dictionary = match.group(1)
            end = raw.find(b"endstream", match.end())
            if end < 0:
                continue
            data = raw[match.end():end]
            if b"/FlateDecode" in dictionary:
                try:
                    data = zlib.decompressobj().decompress(data)
                except zlib.error:
                    continue
            elif b"/Filter" in dictionary:
                continue
            if b"BT" in data:
                parts.append(_content_text(data))
        text = "\n".join(parts)
    if "FINAL NOTICE" not in text.upper():
        raise EnforcementFormatError("schema_drift", "no readable final-notice text layer (verify the extractor)")
    return text


# ----------------------------------------------------------------- record builders

class _Builder:
    """Collects the records of one action and applies the minimisation decision to every text."""

    def __init__(self, fmt: str, native_id: str, url: str, revision: Any) -> None:
        from src.kb.enforcement_records import action_key

        self.fmt = fmt
        self.provider = FORMATS[fmt]["provider"]
        self.native_id = native_id
        self.url = url
        self.revision = _clean(revision)
        self.key = action_key(self.provider, native_id)
        self.records: list[dict[str, Any]] = []
        self.persons: dict[str, str] = {}
        self.organisations: list[str] = []
        self.withheld = {"natural_person_respondents": 0, "natural_person_penalties": 0}
        self.authority: str = FORMATS[fmt]["authority"] or ""

    def source(self, locator: str, url: str | None = None) -> dict[str, Any]:
        return _source(self.provider, self.native_id, url or self.url, self.fmt, revision=self.revision,
                       locator={"field": locator})

    def text(self, value: Any) -> str | None:
        return redact(value, self.persons, self.organisations)

    def respondents(self, parties: Sequence[tuple[str, str, Sequence[Mapping[str, Any]]]]) -> None:
        """(name as published, role as published, identifiers) -> respondent records; persons pseudonymised."""
        from src.kb.enforcement_records import child_key

        ordinal = 0
        for name, role, identifiers in parties:
            name = _clean(name)
            if not name:
                continue
            ordinal += 1
            body: dict[str, Any] = {"contract": RECORD_CONTRACT, "kind": "respondent",
                                    "source": self.source("respondents"), "action_key": self.key,
                                    "authority": self.authority, "ordinal": ordinal,
                                    "role_as_published": _clean(role) or "respondent"}
            if party_type(name) == "organisation":
                self.organisations.append(name)
                body.update(party_type="organisation", name_as_published=name,
                            identifiers=[dict(i) for i in identifiers], record_key=child_key("respondent", self.key,
                                                                                               "organisation", name))
            else:
                pseudonym = f"natural person {ordinal}"
                self.persons[name] = pseudonym
                self.withheld["natural_person_respondents"] += 1
                body.update(party_type="natural_person", pseudonym=pseudonym,
                            record_key=child_key("respondent", self.key, "natural_person", ordinal))
            self.records.append(body)

    def respondent_key(self, sentence: str) -> tuple[str | None, bool]:
        """The organisation respondent a sentence names, and whether it names a natural person."""
        names_person = any(re.search(re.escape(n), sentence, re.IGNORECASE) or
                           re.search(rf"\b{re.escape(n.split()[-1])}\b", sentence) for n in self.persons)
        for record in self.records:
            if record["kind"] == "respondent" and record["party_type"] == "organisation" and \
                    record["name_as_published"].lower() in sentence.lower():
                return record["record_key"], names_person
        return None, names_person

    def action(self, **fields: Any) -> dict[str, Any]:
        body = {"contract": RECORD_CONTRACT, "kind": "enforcement_action", "record_key": self.key,
                "source": self.source("action"), "authority": self.authority, "native_id": self.native_id,
                "url": self.url, "page_revision": self.revision, "publication_status": "published",
                "withheld_natural_persons": self.withheld["natural_person_respondents"], **fields}
        for field in ("title", "outcome_as_published", "admission_wording", "appeal_status_as_published"):
            body[field] = self.text(body.get(field))
        self.records.insert(0, body)
        return body

    def decision(self, ident: str, decision_type: Any, decided_on: Any, outcome: Any, *, settled: bool | None = None,
                 admission: Any = None, measures: Sequence[Any] = (), provisions: Sequence[Any] = (),
                 document_url: Any = None) -> str:
        """One published decision, keyed by its stable place in the notice (a later text is a revision of it)."""
        from src.kb.enforcement_records import child_key

        kind = _clean(decision_type) or "decision"
        key = child_key("decision", self.key, ident)
        self.records.append({"contract": RECORD_CONTRACT, "kind": "decision", "record_key": key,
                             "source": self.source("decision"), "action_key": self.key, "authority": self.authority,
                             "decision_type_as_published": kind, "decided_on": _day(decided_on),
                             "outcome_as_published": self.text(outcome), "settled": settled,
                             "admission_wording": self.text(admission),
                             "corrective_measures": [m for m in (self.text(x) for x in measures) if m],
                             "legal_provisions": [p for p in (_clean(x) for x in provisions) if p],
                             "document_url": document_url if str(document_url or "").startswith("https://")
                             else None})
        return key

    def penalty(self, penalty_type: str, label: Any, text: Any, *, decision: str | None, respondent: str | None = None,
                stage: str | None = "as_imposed", discount: Any = None, amount: Any = None,
                currency: Any = None, published: bool = True) -> None:
        from src.kb.enforcement_records import child_key

        if published and amount is None:
            amount, currency = money(text)
        status = "stated" if published else "not_published"
        self.records.append({"contract": RECORD_CONTRACT, "kind": "penalty",
                             "record_key": child_key("penalty", self.key, penalty_type, stage, respondent),
                             "source": self.source("penalties"), "action_key": self.key, "authority": self.authority,
                             "decision_key": decision, "respondent_key": respondent, "penalty_type": penalty_type,
                             "penalty_type_as_published": _clean(label) or penalty_type,
                             "amount_as_published": self.text(text) if published else None,
                             "amount": amount if published else None,
                             "currency": currency if published else None, "status": status, "stage": stage,
                             "discount_as_published": _clean(discount)})

    def appeal(self, forum: Any, status_text: Any, *, reference: Any = None, stated_on: Any = None) -> None:
        from src.kb.enforcement_records import child_key

        status = self.text(status_text)
        if not status:
            return
        self.records.append({"contract": RECORD_CONTRACT, "kind": "appeal",
                             "record_key": child_key("appeal", self.key, _clean(reference) or status[:200]),
                             "source": self.source("appeal"), "action_key": self.key, "authority": self.authority,
                             "forum_as_published": _clean(forum) or "appeal", "reference": _clean(reference),
                             "status_as_published": status, "stated_on": _day(stated_on)})

    def document(self, url: Any, title: Any, doc_type: Any, published_on: Any, *, raw: bytes | None = None,
                 media_type: str | None = None) -> None:
        from src.kb.enforcement_records import child_key

        link = str(url or "")
        if link.startswith("/"):
            link = SITES[self.provider] + link
        if not link.startswith("https://"):
            return
        self.records.append({"contract": RECORD_CONTRACT, "kind": "notice_document",
                             "record_key": child_key("document", self.key, link),
                             "source": self.source("documents", link), "action_key": self.key,
                             "authority": self.authority, "url": link, "title": self.text(title),
                             "document_type_as_published": _clean(doc_type), "published_on": _day(published_on),
                             "content_sha256": hashlib.sha256(raw).hexdigest() if raw is not None else None,
                             "bytes": len(raw) if raw is not None else None, "media_type": media_type,
                             "language": "en"})

    def result(self) -> tuple[list[dict[str, Any]], dict[str, int]]:
        if self.persons and not self.organisations:
            # EN01: an action naming only natural persons is not recorded.
            return [], {**self.withheld, "individual_only_actions": 1}
        return self.records, {**self.withheld, "individual_only_actions": 0}


def _outcome(texts: Sequence[str]) -> tuple[str | None, bool | None, str | None]:
    """Outcome sentences, whether a settlement or consent is stated, and the stated admission wording (verbatim)."""
    outcome = [s for s in texts if OUTCOME_CUES.search(s) and not APPEAL_CUES.search(s)]
    settled = True if any(SETTLED.search(s) for s in texts) else None
    admission = next((m.group(1).strip() for s in texts for m in [ADMISSION.search(s)] if m), None)
    return (" ".join(outcome[:4]) or None), settled, admission


# ----------------------------------------------------------------- SEC

def _sec(fmt: str, responses: Mapping[str, bytes], unit: Mapping[str, Any]) -> tuple[list, dict]:
    raw = responses["page"]
    page = _page(raw)
    release = unit["release"]
    path = requests_for(fmt, unit)["page"][0]
    url = SITES["us-sec"] + path
    fields = page.fields
    published = _first(page.times, "publish-date", "date") or _first(fields, "publish-date", "date")
    revision = _clean(page.modified) or "sha256:" + hashlib.sha256(raw).hexdigest()[:16]
    number = _clean(_first(fields, "release-number")) or release
    if number.upper() != release.upper():
        raise EnforcementFormatError("schema_drift", "the release is not the requested one")
    body = " ".join(fields.get("body") or [])
    texts = sentences(body)
    builder = _Builder(fmt, release.upper(), url, revision)
    parties = []
    for name in fields.get("respondents") or fields.get("defendants") or []:
        cik = re.search(re.escape(_clean(name) or "") + r"\s*\(CIK No\.\s*(\d{1,10})\)", body)
        identifiers = [{"scheme": "sec-cik", "value": cik.group(1), "type_as_published": "CIK No."}] if cik else []
        parties.append((name, "defendant" if fmt == "sec-litigation-release-html" else "respondent", identifiers))
    builder.respondents(parties)
    litigation = fmt == "sec-litigation-release-html"
    file_number = _clean(_first(fields, "file-number")) or next(iter(_FILE_NO.findall(body)), None)
    court_cases = [{"court": _clean(m.group(2)), "docket_number": m.group(1),
                    "note": "related court case as published; the docket is the Legal courts feature's"}
                   for m in _CIVIL_ACTION.finditer(body)]
    bases = sorted({_clean(m.group(1)) for m in _SEC_BASES.finditer(body)})
    outcome, settled, admission = _outcome(texts)
    filed = next((_day(s) for s in texts if re.search(r"\bfiled\b", s) and _day(s)), None)
    entered = next((_day(m.group(0)) for s in texts for m in [re.search(
        r"(?:entered|issued)\s+(?:a\s+|the\s+)?(?:final\s+)?(?:judgment|order)[^.]*?\bon\s+[A-Z][a-z]+\s+\d{1,2},\s+"
        r"\d{4}", s)] if m), None)
    decided = entered if litigation else _day(published)
    appeals = [s for s in texts if APPEAL_CUES.search(s)]
    identifiers = [{"scheme": "sec-release", "value": release.upper(), "type_as_published": "Release No."}]
    if file_number:
        identifiers.append({"scheme": "sec-file-number", "value": file_number, "type_as_published": "File No."})
    action_type_text = _clean(_first(fields, "action-type")) or ("Litigation Release" if litigation
                                                                 else "Administrative Proceeding")
    decision_documents = [d for d in page.documents if re.search(r"judgment|order", d.get("title") or "", re.IGNORECASE)]
    decision = None
    if outcome:
        decision = builder.decision("release-outcome", decision_documents[0]["title"] if decision_documents else
                                    ("Final judgment as described in the release" if litigation else "Order"),
                                    decided, outcome, settled=settled, admission=admission, provisions=bases,
                                    document_url=(SITES["us-sec"] + decision_documents[0]["url"]
                                                  if decision_documents and str(decision_documents[0]["url"])
                                                  .startswith("/") else None))
    for sentence in texts:
        respondent, names_person = builder.respondent_key(sentence)
        for penalty_type, pattern in SEC_PENALTIES:
            for match in re.finditer(rf"({pattern})\s+(?:of|totaling|in the amount of)\s+(\$\s?(?:{FIGURE}))"
                                     rf"|(\$\s?(?:{FIGURE}))\s+in\s+({pattern})", sentence, re.IGNORECASE):
                if names_person and respondent is None:
                    builder.withheld["natural_person_penalties"] += 1
                    continue
                label = match.group(1) or match.group(4)
                figure = match.group(2) or match.group(3)
                builder.penalty(penalty_type, label, figure, decision=decision, respondent=respondent)
    for sentence in appeals:
        builder.appeal("appeal as published", sentence, stated_on=_day(sentence))
    builder.document(url, _clean(" ".join(page.title)), "Litigation Release" if litigation else
                     "Administrative Proceeding release", published, raw=raw, media_type="text/html")
    for item in page.documents:
        builder.document(item.get("url"), item.get("title"), item.get("title"), item.get("date") or published)
    builder.action(authority_as_published=PUBLISHERS["us-sec"], identifiers=identifiers,
                   action_type="civil_action" if litigation else "administrative_proceeding",
                   action_type_as_published=action_type_text, title=_clean(" ".join(page.title)), legal_bases=bases,
                   initiated_on=filed, decided_on=decided, published_on=_day(published),
                   outcome_as_published=outcome, settled=settled, admission_wording=admission,
                   appeal_status_as_published=" ".join(appeals) or None, court_cases=court_cases,
                   related_references=sorted(set(re.findall(r"\bLR-\d{4,6}\b", body)) - {release.upper()}),
                   native={"release_number": number, "file_number": file_number})
    return builder.result()


def parse_sec_litigation_release(responses, unit):
    return _sec("sec-litigation-release-html", responses, unit)


def parse_sec_admin_proceeding(responses, unit):
    return _sec("sec-admin-proceeding-html", responses, unit)


# ----------------------------------------------------------------- FCA

def parse_fca_final_notice(responses: Mapping[str, bytes], unit: Mapping[str, Any]) -> tuple[list, dict]:
    fmt = "fca-final-notice-pdf"
    raw = responses["notice"]
    text = pdf_text(raw)
    url = SITES["uk-fca"] + requests_for(fmt, unit)["notice"][0]
    # Paragraph numbers ("1.1.") are layout, not wording; they are dropped before sentences are read.
    lines = [re.sub(r"^\d+(?:\.\d+)*\.?\s+(?=[A-Z])", "", " ".join(line.split()))
             for line in text.splitlines() if line.strip()]
    flat = " ".join(lines)
    addressee = next((line.split(":", 1)[1].strip() for line in lines if re.match(r"^To\s*:", line)), None)
    if not addressee:
        raise EnforcementFormatError("schema_drift", "the final notice names no addressee ('To:')")
    frn_line = next((line for line in lines if re.match(r"^(Firm\s+)?Reference Number\s*:", line, re.IGNORECASE)), None)
    individual = any(re.match(r"^Individual Reference Number\s*:", line, re.IGNORECASE) for line in lines)
    date_line = next((line.split(":", 1)[1] for line in lines if re.match(r"^Date\s*:", line)), None)
    revision = "sha256:" + hashlib.sha256(raw).hexdigest()[:16]
    builder = _Builder(fmt, unit["slug"], url, revision)
    frn = re.search(r"(\d{6,7})", frn_line or "")
    identifiers = [{"scheme": "fca-frn", "value": frn.group(1), "type_as_published": "Firm Reference Number"}] \
        if frn and not individual else []
    role = "addressee of the final notice"
    if individual:
        builder.persons[addressee] = "natural person 1"
        builder.withheld["natural_person_respondents"] += 1
        return builder.result()
    builder.respondents([(addressee, role, identifiers)])
    texts = sentences(flat)
    bases = []
    for match in _FCA_BASES.finditer(flat):
        value = _clean(match.group(1))
        if value and value not in bases:
            bases.append(value)
    action_sentences = [s for s in texts if re.search(r"\b(hereby\s+)?(imposes|publicly censures|cancels|prohibit)",
                                                      s, re.IGNORECASE)]
    outcome = " ".join(action_sentences[:3]) or None
    settled = True if any(SETTLED.search(s) for s in texts) else None
    admission = next((m.group(1).strip() for s in texts for m in [ADMISSION.search(s)] if m), None)
    decided = _day(date_line)
    decision = builder.decision("final-notice", "Final Notice", decided, outcome, settled=settled, admission=admission,
                                provisions=bases, document_url=url)
    discount = next((m.group(0) for s in texts for m in [re.search(r"\d{1,2}%\s*\((?:stage|Stage)\s*\d\)\s*discount",
                                                                    s)] if m), None)
    respondent = builder.records[0]["record_key"]
    for sentence in texts:
        before = re.search(rf"would have imposed a financial penalty of (£\s?(?:{FIGURE}))", sentence, re.IGNORECASE)
        imposed = re.search(rf"(?:imposes|imposed)[^.]*?a financial penalty of (£\s?(?:{FIGURE}))", sentence, re.IGNORECASE)
        if before:
            builder.penalty("financial_penalty", "financial penalty (before settlement discount)", before.group(1),
                            decision=decision, respondent=respondent, stage="before_settlement_discount",
                            discount=discount)
        elif imposed:
            builder.penalty("financial_penalty", "financial penalty", imposed.group(1), decision=decision,
                            respondent=respondent, stage="after_settlement_discount" if discount else "as_imposed",
                            discount=discount)
    appeals = [s for s in texts if re.search(r"Upper Tribunal|the Tribunal", s)]
    by_reference: dict[str | None, list[str]] = {}
    for sentence in appeals:
        reference = next(iter(_UT_REF.findall(sentence)), None) or next(iter(_UT_REF.findall(flat)), None)
        by_reference.setdefault(reference, []).append(sentence)
    for reference, stated in by_reference.items():
        dates = sorted(d for d in (_day(s) for s in stated) if d)
        builder.appeal("Upper Tribunal (Tax and Chancery Chamber)" if any("Upper Tribunal" in s for s in stated)
                       else "the Tribunal", " ".join(stated), reference=reference,
                       stated_on=dates[-1] if dates else None)
    builder.document(url, f"Final Notice: {addressee}", "Final Notice", decided, raw=raw,
                     media_type="application/pdf")
    builder.action(authority_as_published=PUBLISHERS["uk-fca"],
                   identifiers=[{"scheme": "fca-notice", "value": unit["slug"], "type_as_published": "final notice"}],
                   action_type="final_notice", action_type_as_published="Final Notice",
                   title=f"Final Notice: {addressee}", legal_bases=bases, initiated_on=None, decided_on=decided,
                   published_on=_day(unit.get("published_on")) or decided, outcome_as_published=outcome,
                   settled=settled, admission_wording=admission,
                   appeal_status_as_published=" ".join(appeals) or None, court_cases=[],
                   related_references=sorted(set(_UT_REF.findall(flat))),
                   publication_status="corrected" if re.search(r"\bcorrected on\b", flat, re.IGNORECASE) else "published",
                   native={"slug": unit["slug"], "frn_line": frn_line, "discount_as_published": discount})
    return builder.result()


# ----------------------------------------------------------------- EPA ECHO

def parse_echo_case(responses: Mapping[str, bytes], unit: Mapping[str, Any]) -> tuple[list, dict]:
    fmt = "echo-case-report-json"
    payload = _json(responses["case"])
    results = payload.get("Results") if isinstance(payload, Mapping) else None
    if not isinstance(results, Mapping):
        raise EnforcementFormatError("schema_drift", "the case report has no Results object")
    number = _clean(results.get("CaseNumber"))
    if number != unit["case_number"]:
        raise EnforcementFormatError("schema_drift", "the case is not the requested one")
    url = f"https://echo.epa.gov/enforcement-case-report?id={number}"
    raw = responses["case"]
    revision = _clean(results.get("LastUpdated")) or "sha256:" + hashlib.sha256(raw).hexdigest()[:16]
    builder = _Builder(fmt, number, url, revision)
    builder.respondents([(d.get("DefendantName"), "defendant" + (" (named in settlement)"
                                                                if d.get("NamedInSettlement") == "Y" else ""), [])
                         for d in results.get("Defendants") or [] if isinstance(d, Mapping)])
    case_type = _clean(results.get("CaseType")) or ("Criminal" if unit.get("kind") == "criminal" else "Civil")
    action_type = ECHO_ACTION_TYPES.get(case_type.lower(), "criminal_case" if unit.get("kind") == "criminal"
                                        else "civil_judicial_case")
    bases = [b for b in (_clean(f"{law.get('Law') or ''} {law.get('Sections') or ''}")
                         for law in results.get("LawsAndSections") or [] if isinstance(law, Mapping)) if b]
    milestones = sorted(((_day(m.get("ActualDate")), _clean(m.get("Milestone")))
                         for m in results.get("Milestones") or [] if isinstance(m, Mapping)),
                        key=lambda item: item[0] or "")
    filed = _day(results.get("DateFiled")) or next((d for d, name in milestones if name and "filed" in name.lower()),
                                                   None)
    outcome_text = _clean(results.get("EnforcementOutcome"))
    settled_on = _day(results.get("SettlementDate"))
    decision = None
    if outcome_text:
        decision = builder.decision("enforcement-outcome", outcome_text, settled_on or next((d for d, n in milestones
                                                                       if n and outcome_text.lower() in n.lower()),
                                                                      None),
                                    f"Enforcement outcome as published: {outcome_text}",
                                    settled=True if SETTLED.search(outcome_text) or "consent" in
                                    outcome_text.lower() else None, provisions=bases)
    penalties = results.get("Penalties") if isinstance(results.get("Penalties"), Mapping) else {}
    for penalty_type, field, label in ECHO_PENALTIES:
        value = penalties.get(field)
        if value in (None, ""):
            builder.penalty(penalty_type, label, None, decision=decision, published=False)
            continue
        amount, _ = money(f"${value}")
        builder.penalty(penalty_type, label, str(value), decision=decision, amount=amount, currency="USD")
    facilities = [{"frs_registry_id": _clean(f.get("RegistryID")), "name_as_published": _clean(f.get("FacilityName")),
                   "city": _clean(f.get("City")), "state": _clean(f.get("State")),
                   "latitude_as_published": _clean(f.get("Latitude")),
                   "longitude_as_published": _clean(f.get("Longitude"))}
                  for f in results.get("Facilities") or [] if isinstance(f, Mapping)]
    docket = _clean(results.get("CourtDocketNumber"))
    court_cases = [{"court": _clean(results.get("Court")), "docket_number": docket,
                    "note": "court docket number as published; the docket is the Legal courts feature's"}] \
        if docket else []
    builder.document(url, _clean(results.get("CaseName")), f"ECHO {case_type} case report", None, raw=raw,
                     media_type="application/json")
    builder.action(authority_as_published=_clean(results.get("LeadAgency")) or "EPA",
                   identifiers=[{"scheme": "echo-case-number", "value": number, "type_as_published": "Case Number"}]
                   + ([{"scheme": "icis-activity-id", "value": str(results["ActivityId"]),
                        "type_as_published": "Activity ID"}] if results.get("ActivityId") else []),
                   action_type=action_type, action_type_as_published=case_type,
                   title=_clean(results.get("CaseName")), legal_bases=bases, initiated_on=filed,
                   decided_on=decision and settled_on, published_on=None,
                   outcome_as_published=f"{outcome_text} ({_clean(results.get('CaseStatus')) or 'status not stated'})"
                   if outcome_text else None,
                   settled=True if outcome_text and ("consent" in outcome_text.lower() or
                                                     SETTLED.search(outcome_text)) else None,
                   admission_wording=None, appeal_status_as_published=None, court_cases=court_cases,
                   related_references=[], facilities=facilities,
                   native={"region": _clean(results.get("Region")), "case_status": _clean(results.get("CaseStatus")),
                           "milestones": [{"date": d, "milestone": n} for d, n in milestones]})
    return builder.result()


# ----------------------------------------------------------------- EDPB

def parse_edpb_entry(responses: Mapping[str, bytes], unit: Mapping[str, Any]) -> tuple[list, dict]:
    fmt = "edpb-art60-html"
    raw = responses["entry"]
    page = _page(raw)
    fields = page.fields
    entry = str(unit["entry"])
    title = _clean(" ".join(page.title)) or ""
    if not re.search(rf"\b{re.escape(entry)}\b", title):
        raise EnforcementFormatError("schema_drift", "the register entry is not the requested one")
    lsa = (_clean(_first(fields, "edpb-lsa", "lsa")) or "").upper()
    if lsa not in COUNTRY_CODES:
        raise EnforcementFormatError("schema_drift", "the entry states no lead supervisory authority code")
    url = SITES["edpb"] + requests_for(fmt, unit)["entry"][0]
    revision = _clean(page.modified) or "sha256:" + hashlib.sha256(raw).hexdigest()[:16]
    builder = _Builder(fmt, entry, url, revision)
    builder.authority = f"eu-sa-{lsa.lower()}"
    concerned = [f"eu-sa-{c.strip().lower()}" for item in fields.get("edpb-csa") or fields.get("csa") or []
                 for c in re.split(r"[,;]", item) if c.strip().upper() in COUNTRY_CODES]
    builder.respondents([(name, "controller", []) for name in fields.get("edpb-controller") or []])
    references = [r for r in (_clean(x) for x in fields.get("edpb-legal-reference") or []) if r]
    measures = [m for m in (_clean(x) for x in fields.get("edpb-decision") or []) if m]
    decided = _day(_first(page.times, "edpb-date") or _first(fields, "edpb-date"))
    summary = " ".join(fields.get("body") or [])
    texts = sentences(summary)
    fine_sentence = next((s for s in texts if re.search(r"\bfine\b", s, re.IGNORECASE) and _MONEY.search(s)), None)
    outcome = "; ".join(measures) or None
    decision = builder.decision("final-decision", "Final decision (Article 60 GDPR)", decided, outcome, measures=measures,
                                provisions=references,
                                document_url=next((SITES["edpb"] + d["url"] if str(d.get("url")).startswith("/")
                                                   else d.get("url") for d in page.documents
                                                   if "final decision" in (d.get("title") or "").lower()), None))
    if fine_sentence:
        builder.penalty("fine", "administrative fine", _MONEY.search(fine_sentence).group(0), decision=decision,
                        respondent=next((r["record_key"] for r in builder.records if r["kind"] == "respondent"
                                         and r["party_type"] == "organisation"), None))
    elif any("fine" in m.lower() for m in measures):
        builder.penalty("fine", "administrative fine", None, decision=decision, published=False)
    appeals = [s for s in texts if APPEAL_CUES.search(s)]
    for sentence in appeals:
        builder.appeal("appeal as published", sentence, stated_on=_day(sentence))
    builder.document(url, title, "Article 60 register entry", decided, raw=raw, media_type="text/html")
    for item in page.documents:
        builder.document(item.get("url"), item.get("title"), item.get("title"), item.get("date") or decided)
    identifier = _clean(_first(fields, "edpb-identifier"))
    builder.action(authority_as_published=f"Lead supervisory authority: {lsa}",
                   identifiers=[{"scheme": "edpb-register-entry", "value": entry, "type_as_published": "Decision no"}]
                   + ([{"scheme": "edpbi", "value": identifier, "type_as_published": "EDPBI"}] if identifier else []),
                   action_type="one_stop_shop_decision",
                   action_type_as_published="Final decision under Article 60 GDPR (one-stop-shop)", title=title,
                   legal_bases=references, initiated_on=None, decided_on=decided, published_on=None,
                   outcome_as_published=outcome, settled=True if any(re.search(r"amicable settlement", m, re.IGNORECASE)
                                                                     for m in measures) else None,
                   admission_wording=None, appeal_status_as_published=" ".join(appeals) or None, court_cases=[],
                   related_references=[], concerned_authorities=concerned,
                   native={"keywords": [k for k in (_clean(x) for x in fields.get("edpb-keywords") or []) if k],
                           "lsa": lsa, "controller_published": bool(fields.get("edpb-controller"))})
    return builder.result()


# ----------------------------------------------------------------- selection and requests


def _units(fmt: str, selection: Mapping[str, Any]) -> list[dict[str, Any]]:
    key = FORMATS[fmt]["unit"]
    units = [dict(u) if isinstance(u, Mapping) else {"id": u} for u in selection.get(key) or []]
    if not 1 <= len(units) <= MAX_UNITS:
        raise SourcePackError("invalid_manifest", f"an enforcement selection names 1-{MAX_UNITS} {key}")
    for unit in units:
        if fmt == "sec-litigation-release-html" and not _LR.fullmatch(str(unit.get("release") or "")):
            raise SourcePackError("invalid_manifest", "SEC litigation units name a release (LR-nnnnn)")
        if fmt == "sec-admin-proceeding-html" and not _AP.fullmatch(str(unit.get("release") or "")):
            raise SourcePackError("invalid_manifest", "SEC administrative units name a release (33-/34-/IA-/IC-/AE-)")
        if fmt == "fca-final-notice-pdf" and not _SLUG.fullmatch(str(unit.get("slug") or "")):
            raise SourcePackError("invalid_manifest", "FCA units name a final-notice slug")
        if fmt == "echo-case-report-json" and (not _CASE.fullmatch(str(unit.get("case_number") or ""))
                                               or unit.get("kind", "civil") not in {"civil", "criminal"}):
            raise SourcePackError("invalid_manifest", "ECHO units name a case number and civil or criminal")
        if fmt == "edpb-art60-html" and not re.fullmatch(r"\d{1,6}", str(unit.get("entry") or "")):
            raise SourcePackError("invalid_manifest", "EDPB units name a register entry number")
    return units


def requests_for(fmt: str, unit: Mapping[str, Any]) -> dict[str, tuple[str, dict[str, Any]]]:
    """Named request paths (relative to the endpoint host) and parameters for one selection unit."""
    if fmt == "sec-litigation-release-html":
        return {"page": (f"/enforcement-litigation/litigation-releases/{str(unit['release']).lower()}", {})}
    if fmt == "sec-admin-proceeding-html":
        return {"page": (f"/enforcement-litigation/administrative-proceedings/{str(unit['release']).lower()}", {})}
    if fmt == "fca-final-notice-pdf":
        return {"notice": (f"/publication/final-notices/{unit['slug']}.pdf", {})}
    if fmt == "echo-case-report-json":
        service = "get_crcase_report" if unit.get("kind") == "criminal" else "get_case_report"
        return {"case": (f"/echo/case_rest_services.{service}", {"output": "JSON", "p_id": unit["case_number"]})}
    if fmt == "edpb-art60-html":
        return {"entry": (("/our-work-tools/consistency-findings/register-for-article-60-final-decisions/"
                           f"decision-no-{unit['entry']}_en"), {})}
    raise SourcePackError("invalid_manifest", f"unknown enforcement format {fmt!r}")


_PARSERS: dict[str, Callable[[Mapping[str, bytes], Mapping[str, Any]], tuple[list, dict]]] = {
    "sec-litigation-release-html": parse_sec_litigation_release,
    "sec-admin-proceeding-html": parse_sec_admin_proceeding,
    "fca-final-notice-pdf": parse_fca_final_notice,
    "echo-case-report-json": parse_echo_case,
    "edpb-art60-html": parse_edpb_entry,
}


def parse_unit(fmt: str, responses: Mapping[str, bytes], unit: Mapping[str, Any]
               ) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Validated records of one unit and the minimisation counts (what was withheld)."""
    if fmt not in _PARSERS:
        raise EnforcementFormatError("schema_drift", f"unknown enforcement format {fmt!r}")
    records, withheld = _PARSERS[fmt](responses, unit)
    from src.kb.enforcement_records import EnforcementRecordError, validate_record

    try:
        return [validate_record(r) for r in records], withheld
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
    if FORMATS[fmt]["provider"] == "us-sec" and not str(declared.get("user_agent") or "").strip():
        raise SourcePackError("invalid_manifest", "SEC sources declare the User-Agent the fair-access policy asks for")
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

    def _get(self, path: str, params: Mapping[str, Any]) -> tuple[bytes, dict[str, Any]]:
        from src.ingestion.source_pack_runtime import _retry_after_ms

        endpoint = self.source["endpoint"].rstrip("/")
        url = endpoint + path
        host = (urlsplit(endpoint).hostname or "").casefold()
        ordered = dict(sorted(params.items()))
        headers = {"Accept": "application/json, text/html, application/pdf"}
        if self.declared.get("user_agent"):
            headers["User-Agent"] = str(self.declared["user_agent"])
        response = self.transport(url=url, params=ordered, headers=headers,
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
                     "origin": "fixture" if response.get("origin") == "fixture" else "live"}

    def _native_id(self, unit: Mapping[str, Any]) -> str:
        if self.format.startswith("sec-"):
            return str(unit["release"]).upper()
        return str(unit.get("slug") or unit.get("case_number") or unit.get("entry"))

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage
        from src.kb.enforcement import removal_marker
        from src.kb.enforcement_records import action_key

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
        removed = next((r for r in requests if r["status"] in {404, 410}), None)
        withheld: dict[str, int] = {}
        if removed:
            path = removed["path"]
            site = SITES[self.provider] if not self.format.startswith("echo") else "https://echodata.epa.gov"
            records = [removal_marker(self.provider, action_key(self.provider, self._native_id(unit)),
                                      http_status=removed["status"], url=site + path, unit=unit)]
        else:
            try:
                records, withheld = parse_unit(self.format, responses, unit)
            except EnforcementFormatError as exc:
                raise SourcePackError("budget_exhausted" if exc.code == "input_limit" else "schema_drift",
                                      f"{exc.code}: {exc}") from exc
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        if len(records) > limit:
            raise SourcePackError("budget_exhausted", "unit has more records than the run's result budget")
        origin = "fixture" if all(r["origin"] == "fixture" for r in requests) else "live"
        receipt = {
            "contract": "noesis-enforcement-acquisition-receipt-v1", "source_id": self.source["source_id"],
            "provider": self.provider, "format": self.format, "unit_index": index, "unit": unit,
            "requests": requests, "records": len(records), "withheld": withheld, "evidence_origin": origin,
            "live_verification": self.declared["live_verification"], "final_page": index + 1 >= len(self.units),
            "removed_by_source": bool(removed),
        }
        out = []
        for record in records:
            if record.get("contract") == RECORD_CONTRACT:
                record = {**record, "source": {**record["source"], "evidence_origin": origin}}
                item_id, title = record["record_key"], _item_title(record)
                url, published = record["source"].get("url"), _item_date(record)
                revision = str(record["source"].get("revision") or "")
                # A digest revision (``sha256:...``) is not a time; only dated revisions are timestamps.
                updated = revision if re.match(r"^\d{4}-\d{2}-\d{2}", revision) else None
            else:
                item_id, title, url, published, updated = (record["action_key"] + ":removal",
                                                           f"removed by the source: {record['action_key']}",
                                                           record["url"], None, None)
            out.append({"id": item_id, "title": title, "url": url, "language": "en", "published_at": published,
                        "updated_at": updated, "content": json.dumps(record, sort_keys=True, ensure_ascii=False),
                        "enforcement_record": record, "enforcement_receipt": receipt})
        next_cursor = None if receipt["final_page"] else str(index + 1)
        return RuntimePage(tuple(out), next_cursor, sum(r["bytes"] for r in requests), receipt=receipt)


def _item_title(record: Mapping[str, Any]) -> str:
    for field in ("title", "name_as_published", "pseudonym", "decision_type_as_published",
                  "penalty_type_as_published", "forum_as_published"):
        if record.get(field):
            return str(record[field])
    return record["record_key"]


def _item_date(record: Mapping[str, Any]) -> str | None:
    for field in ("published_on", "decided_on", "initiated_on", "stated_on"):
        if record.get(field) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(record[field])):
            return record[field]
    return None


FIXTURE_SECRET = None
ADAPTERS = {CONNECTOR: EnforcementAdapter}


def fixture_transport(pages: Sequence[Mapping[str, Any]]) -> Callable[..., Mapping[str, Any]]:
    """Replay authored responses keyed by URL path (+ sorted query); binary bodies are base64 (``body_base64``)."""
    by_key = {page["request"]: page for page in pages}

    def transport(*, url, params, headers, timeout):
        del headers, timeout
        parts = urlsplit(url)
        query = urlencode(sorted(dict(params or {}).items()))
        key = parts.path + ("?" + query if query else "")
        page = by_key.get(key)
        if page is None:
            raise SourcePackError("fixture_missing", f"no native page for {key}")
        if page.get("body_base64") is not None:
            content = base64.b64decode(page["body_base64"])
        else:
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
    "EnforcementAdapter", "EnforcementFormatError", "enforcement_declaration", "fixture_transport", "money",
    "parse_unit", "pdf_text", "redact", "replay_native_fixture", "requests_for", "sentences",
]
