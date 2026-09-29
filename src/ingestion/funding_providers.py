"""Bounded acquisition of funding programmes and calls.

Four official sources are supported, each under a recorded access contract
(:data:`PROVIDER_CONTRACTS`). None of them publishes a documented, stable
open-call API that this module can rely on, so the contracts are explicit
about what is used and what is *not* claimed:

* **NLnet** – public HTML pages (``/propose/`` and fund pages). No API.
* **EU Funding & Tenders** – the public portal's search service
  (``api.tech.ec.europa.eu/search-api``, public ``SEDIA`` key embedded in the
  portal) and per-topic JSON (``topicDetails``). Grants only; procurement
  tenders are split out and never ingested as grant opportunities.
* **Förderdatenbank** – public HTML programme pages and result listings. A
  listing is a *directory entry*, never an open application window.
* **EXIST** – public HTML programme pages. Programme rules only; no user
  affiliation or individual eligibility is inferred.

All network access goes through :class:`~src.ingestion.provider_execution.DurableHTTP`
(explicit budget, exact hosts, no redirects, durable replay). Captures are
stored through the existing ``DocumentStore`` and normalized through
:class:`~src.kb.funding_opportunities.FundingOpportunityStore`. Parsers never
invent values: anything not found in the page stays unknown, and every
extracted requirement or amount carries a quote locator.

Coverage is deliberately bounded to explicitly selected pages/topics; nothing
here implies comprehensive coverage of any provider.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import urljoin, urlsplit

from services.ingest.common.document_model import Document
from src.ingestion.document_store import DocumentStore
from src.ingestion.provider_execution import DurableHTTP, ProviderError, canonical, digest
from src.kb.funding_opportunities import FundingOpportunityStore
from src.kb.funding_records import FundingRecordError, record

PROVIDER_HOSTS = {
    "nlnet": {"nlnet.nl"},
    "eu-ft": {"api.tech.ec.europa.eu", "ec.europa.eu"},
    "foerderdatenbank": {"www.foerderdatenbank.de"},
    "exist": {"www.exist.de", "exist.de"},
}
PROVIDER_CONTRACTS = {
    "nlnet": {
        "access": "web",
        "entry_points": ["https://nlnet.nl/propose/", "https://nlnet.nl/<fund>/"],
        "authentication": "none",
        "pagination": "none (single pages)",
        "cadence": "daily at most; rounds close every two months",
        "terms": "Public web pages; cite source URL. No API is documented; none is assumed.",
        "retained_evidence": "raw HTML capture (snapshot digest), paragraph quote locators",
        "coverage": "Funds listed on /propose/ and explicitly selected fund pages only",
        "distinguishes": {"programme": "fund page", "call": "fund + announced deadline round", "award": "project pages (not acquired)"},
        "unavailable_fallback": "record provider failure; keep last known revision marked stale",
    },
    "eu-ft": {
        "access": "portal-search-json",
        "entry_points": [
            "https://api.tech.ec.europa.eu/search-api/prod/rest/search",
            "https://ec.europa.eu/info/funding-tenders/opportunities/data/topicDetails/<identifier>.json",
        ],
        "authentication": "public portal key 'SEDIA' passed as a query parameter (not a user credential)",
        "pagination": "pageNumber/pageSize, bounded by max_pages",
        "cadence": "daily at most",
        "terms": "Commission reuse policy (Decision 2011/833/EU); cite topic URL. Portal endpoints are not a versioned public API contract.",
        "retained_evidence": "raw JSON capture, JSON pointers per field",
        "coverage": "Explicitly selected search queries/topics; grants only; tenders separated",
        "distinguishes": {"programme": "framework programme", "call": "topic (round = call identifier)", "award": "CORDIS/Kohesio (not acquired)"},
        "unavailable_fallback": "record provider failure; keep last known revision marked stale",
    },
    "foerderdatenbank": {
        "access": "web",
        "entry_points": ["https://www.foerderdatenbank.de/FDB/Content/DE/Foerderprogramm/<level>/<body>/<slug>.html"],
        "authentication": "none",
        "pagination": "result-list pages followed only when explicitly selected",
        "cadence": "weekly at most",
        "terms": "Public directory pages of the Federal Ministry (BMWE); directory, not the funder of record.",
        "retained_evidence": "raw HTML capture, definition-list field locators",
        "coverage": "Selected programme pages; directory entries link to administering bodies",
        "distinguishes": {"programme": "directory entry", "call": "not asserted", "award": "not acquired"},
        "unavailable_fallback": "record provider failure; broken administering links retained as unresolved",
    },
    "exist": {
        "access": "web",
        "entry_points": ["https://www.exist.de/EXIST/Navigation/<lang>/<programme>/..."],
        "authentication": "none",
        "pagination": "none",
        "cadence": "weekly at most",
        "terms": "Public programme pages of BMWE/Projektträger Jülich; cite page URL.",
        "retained_evidence": "raw HTML capture, paragraph quote locators, linked guideline URLs",
        "coverage": "Selected programme variants (e.g. Gründungsstipendium, Forschungstransfer)",
        "distinguishes": {"programme": "programme variant", "call": "dated submission rounds when stated", "award": "not acquired"},
        "unavailable_fallback": "record provider failure; keep last known revision marked stale",
    },
}
LIVE_VERIFICATION = {
    provider: {"status": "unverified", "note": "live access has not been confirmed from this runtime; see scripts/funding_live_check.py"}
    for provider in PROVIDER_CONTRACTS
}
_EU_STATUS = {"31094501": "forthcoming", "31094502": "open", "31094503": "closed"}
_FDB_INSTRUMENT = {
    "zuschuss": "grant", "darlehen": "loan", "bürgschaft": "guarantee", "garantie": "guarantee",
    "beteiligung": "equity", "sonstige": "unknown", "steuerliche förderung": "credit",
}
_TZ_ABBREVIATIONS = {"CET": ("Europe/Amsterdam", 1), "CEST": ("Europe/Amsterdam", 2), "UTC": ("UTC", 0), "GMT": ("UTC", 0)}
_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}


# --------------------------------------------------------------------- helpers


def _official(provider, url):
    parsed = urlsplit(str(url))
    if parsed.scheme != "https" or parsed.hostname not in PROVIDER_HOSTS[provider] or parsed.username or parsed.password:
        raise ProviderError("source_identity", "record must identify its declared official origin")
    return f"https://{parsed.hostname}{parsed.path}" + (f"?{parsed.query}" if parsed.query else "")


def paragraphs(raw):
    """Main-content paragraphs with heading context and element locators."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(raw, "html.parser")
    for element in soup.select("script,style,nav,footer,noscript"):
        element.decompose()
    main = soup.find("main") or soup.find("article") or soup.body or soup
    heading, result = None, []
    for index, element in enumerate(main.select("h1,h2,h3,h4,p,li,dd,dt,td")):
        text = element.get_text(" ", strip=True)
        if not text:
            continue
        if element.name in {"h1", "h2", "h3", "h4"}:
            heading = text
        result.append({"index": index, "tag": element.name, "section": heading, "text": text})
    if len(result) > 10000:
        raise ProviderError("input_limit", "too many HTML paragraphs")
    return result, soup


def _locator(paragraph, quote=None):
    return {"section": paragraph["section"] or "(page)", "paragraph": paragraph["index"],
            "quote": (quote or paragraph["text"])[:400]}


def _find(paras, pattern, flags=re.IGNORECASE):
    compiled = re.compile(pattern, flags)
    for paragraph in paras:
        match = compiled.search(paragraph["text"])
        if match:
            return match, paragraph
    return None, None


def _amount(text):
    """Parse '50,000' / '50.000' / '2.500,50' / '5k' to a decimal string."""
    value = text.strip().replace("\u00a0", "").replace(" ", "")
    multiplier = 1
    if value.lower().endswith("k"):
        value, multiplier = value[:-1], 1000
    if "," in value and "." in value:
        decimal_mark = "," if value.rindex(",") > value.rindex(".") else "."
        value = value.replace("." if decimal_mark == "," else ",", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}([.,]\d{3})+", value):
        value = re.sub(r"[.,]", "", value)
    else:
        value = value.replace(",", ".")
    number = Decimal(value) * multiplier
    return str(number.quantize(Decimal(1))) if number == number.to_integral_value() else str(number)


def _english_deadline(match):
    """Convert 'June 1st 2026 12:00 CEST' to a deadline with explicit offset."""
    month, day, year, clock, zone = match.group("month"), match.group("day"), match.group("year"), match.group("time"), match.group("tz")
    date = f"{int(year):04d}-{_MONTHS[month.lower()]:02d}-{int(day):02d}"
    deadline = {"kind": "submission", "text": match.group(0), "date": date, "timezone": None, "instant": None}
    if clock and zone and zone.upper() in _TZ_ABBREVIATIONS:
        name, hours = _TZ_ABBREVIATIONS[zone.upper()]
        hh, mm = (int(v) for v in clock.split(":"))
        local = datetime(int(year), _MONTHS[month.lower()], int(day), hh, mm, tzinfo=timezone(timedelta(hours=hours)))
        deadline.update(timezone=f"{name} ({zone.upper()} as stated)", instant=local.isoformat())
    return deadline


_DEADLINE_RE = (
    r"(?P<month>January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s+(?P<year>\d{4})"
    r"(?:,?\s+(?:at\s+)?(?P<time>\d{1,2}:\d{2})\s*(?P<tz>CEST|CET|UTC|GMT)?)?"
)


def _pattern_requirements(provider, paras, patterns):
    """Extract requirement sentences for declared patterns, with quote locators."""
    requirements = []
    hard_marker = re.compile(r"\b(must|required|only|mandatory|eligible|müssen|nur|förderberechtigt)\b", re.IGNORECASE)
    for requirement_id, category, pattern, rule in patterns:
        compiled = re.compile(pattern, re.IGNORECASE)
        candidates = []
        for paragraph in paras:
            match = compiled.search(paragraph["text"])
            if match:
                sentence = next((s for s in re.split(r"(?<=[.!?])\s+", paragraph["text"]) if match.group(0) in s), paragraph["text"])
                candidates.append((paragraph, sentence))
        if not candidates:
            continue
        # Prefer the sentence that states the condition normatively.
        paragraph, sentence = next((c for c in candidates if hard_marker.search(c[1])), candidates[0])
        hard = True if hard_marker.search(sentence) else None
        item = {"requirement_id": f"{provider}:{requirement_id}", "category": category, "text": sentence,
                "hard": hard, "locator": _locator(paragraph, sentence)}
        if rule is not None:
            item["machine_rule"] = rule
        requirements.append(item)
    return requirements


# --------------------------------------------------------------------- NLnet

NLNET_PATTERNS = [
    ("open-licence", "licensing", r"(free|open)[ -]?(and open )?source|open licen[cs]e|free software", {"fact": "project.open_source", "op": "is_true"}),
    ("european-dimension", "theme", r"european dimension", None),
    ("eligible-applicants", "applicant-type", r"(individuals?|anyone)[^.]{0,80}(organisations?|organizations?|companies)", None),
]


def parse_nlnet_propose(raw, *, source_url="https://nlnet.nl/propose/"):
    """Current NLnet calls: one call record per open fund and announced deadline round."""
    url = _official("nlnet", source_url)
    paras, soup = paragraphs(raw)
    match, paragraph = _find(paras, r"deadline[^.]{0,80}?" + _DEADLINE_RE)
    deadline = None
    if match:
        inner = re.search(_DEADLINE_RE, match.group(0))
        deadline = {**_english_deadline(inner), "locator": _locator(paragraph)}
    funds = []
    for select in soup.select("select"):
        if not re.search(r"call|fund", select.get("name", "") + select.get("id", ""), re.IGNORECASE):
            continue
        for option in select.select("option"):
            value, label = option.get("value", "").strip(), option.get_text(" ", strip=True)
            if value and label and not option.has_attr("disabled"):
                funds.append((value, label, {"selector": f"select[name={select.get('name')}] option[value={value}]", "quote": label}))
    if not funds:
        raise ProviderError("schema_drift", "no fund selection found on the proposal page; acquisition is partial")
    requirements = _pattern_requirements("nlnet", paras, NLNET_PATTERNS)
    amount_match, amount_para = _find(paras, r"between\s+(?:€\s?|EUR\s?)?(?P<min>[\d.,\s]+k?)\s+and\s+(?:€\s?|EUR\s?)?(?P<max>[\d.,\s]+k?)\s*(?:euro|EUR|€)")
    terms = {"currency": None, "eligible_costs": None}
    if amount_match:
        terms = {"currency": "EUR", "award_range": {
            "min": _amount(amount_match.group("min")), "max": _amount(amount_match.group("max")),
            "basis": "per-project", "locator": _locator(amount_para, amount_match.group(0))}, "eligible_costs": None}
    records = []
    for value, label, locator in funds:
        records.append(record(
            "nlnet", "call", value, label, source_url=url, round_id=deadline["date"] if deadline else None,
            authority={"kind": "funder", "name": "NLnet Foundation"},
            funder={"name": "NLnet Foundation", "id": "nlnet"},
            programme={"id": value, "title": label},
            instrument={"kinds": ["grant"], "native_label": "donation/grant", "locator": locator},
            financial_terms=terms,
            status={"native": "listed on proposal form", "asserted": "open", "locator": locator},
            deadlines=[deadline] if deadline else [],
            requirements=requirements,
            documents=[{"url": url, "title": "Proposal form and guidance", "kind": "form"}],
            sections=[{"text": p["text"], "locator": _locator(p)} for p in paras[:200]],
        ))
    return {"records": records, "coverage": {"complete": True, "listing": "nlnet:propose"}}


def parse_nlnet_fund(raw, *, source_url):
    """A fund page as a programme record (themes, conditions, amounts where stated)."""
    url = _official("nlnet", source_url)
    paras, soup = paragraphs(raw)
    title = (soup.find("h1") or soup.find("title"))
    if title is None:
        raise ProviderError("schema_drift", "fund page has no title")
    slug = urlsplit(url).path.strip("/").split("/")[0] or "nlnet"
    requirements = _pattern_requirements("nlnet", paras, NLNET_PATTERNS)
    amount_match, amount_para = _find(paras, r"between\s+(?:€\s?|EUR\s?)?(?P<min>[\d.,\s]+k?)\s+and\s+(?:€\s?|EUR\s?)?(?P<max>[\d.,\s]+k?)\s*(?:euro|EUR|€)")
    budget_match, budget_para = _find(paras, r"(?:€\s?|EUR\s?)(?P<amount>[\d.,]+)\s*(?P<unit>million|M)\b")
    terms = {"currency": "EUR" if amount_match or budget_match else None, "eligible_costs": None}
    if amount_match:
        terms["award_range"] = {"min": _amount(amount_match.group("min")), "max": _amount(amount_match.group("max")),
                                "basis": "per-project", "locator": _locator(amount_para, amount_match.group(0))}
    if budget_match:
        millions = Decimal(budget_match.group("amount").replace(",", "")) * 1_000_000
        terms["programme_budget"] = {"amount": str(millions.quantize(Decimal(1)) if millions == millions.to_integral_value() else millions),
                                     "basis": "programme-total", "locator": _locator(budget_para, budget_match.group(0))}
    themes = [p["text"] for p in paras if p["tag"] == "li" and p["section"] and re.search(r"theme|topic|scope", p["section"], re.IGNORECASE)][:30]
    return {"records": [record(
        "nlnet", "programme", slug, title.get_text(" ", strip=True), source_url=url,
        authority={"kind": "funder", "name": "NLnet Foundation"},
        funder={"name": "NLnet Foundation", "id": "nlnet"},
        instrument={"kinds": ["grant"], "native_label": "donation/grant"},
        financial_terms=terms, requirements=requirements, themes=themes,
        # Recurring rounds are calls with their own deadlines (see /propose/),
        # not rolling submissions; the fund page itself asserts no window.
        status={"native": "recurring rounds" if re.search(r"every two months|bi-?monthly", " ".join(p["text"] for p in paras), re.IGNORECASE) else None,
                "asserted": "unknown"},
        sections=[{"text": p["text"], "locator": _locator(p)} for p in paras[:200]],
    )], "coverage": {"complete": False, "listing": "nlnet:fund:" + slug}}


# --------------------------------------------------------------- EU F&T


def _first(metadata, key):
    value = metadata.get(key)
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _eu_instant(value):
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    text = re.sub(r"\.\d+", "", text)
    text = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", text)
    return datetime.fromisoformat(text).isoformat()


def _eu_budget(budget, pointer, currency="EUR"):
    """Topic budget (topic-total) and per-project contribution, never conflated."""
    terms = {"currency": currency, "eligible_costs": None}
    if not isinstance(budget, dict):
        return {"currency": None, "eligible_costs": None}
    actions = [a for values in (budget.get("budgetTopicActionMap") or {}).values() for a in values or []]
    if not actions:
        return {"currency": None, "eligible_costs": None}
    action = actions[0]
    total = sum(Decimal(str(v)) for v in (action.get("budgetYearMap") or {}).values())
    if total:
        terms["programme_budget"] = {"amount": str(total), "basis": "topic-total",
                                     "expected_awards": action.get("expectedGrants") or None,
                                     "locator": {"json_pointer": pointer + "/budgetYearMap"}}
    low, high = action.get("minContribution"), action.get("maxContribution")
    if low is not None or high is not None:
        terms["award_range"] = {"min": None if low is None else str(Decimal(str(low))),
                                "max": None if high is None else str(Decimal(str(high))),
                                "basis": "per-project", "locator": {"json_pointer": pointer + "/minContribution"}}
    return terms


def _eu_deadlines(dates, model, pointer):
    result = []
    for index, value in enumerate(dates or []):
        instant = _eu_instant(value)
        result.append({"kind": "submission", "text": str(value), "timezone": "UTC (portal displays Europe/Brussels)",
                       "instant": instant, "date": instant[:10] if instant else None,
                       "stage": (f"stage-{index + 1}" if model and "two-stage" in model else None),
                       "locator": {"json_pointer": f"{pointer}/{index}"}})
    return result


def _eu_topic_url(identifier):
    return ("https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/opportunities/topic-details/"
            + identifier.lower())


def parse_eu_search(payload):
    """Portal search results → grant topic records; tenders separated."""
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise ProviderError("schema_drift", "search response has no results list")
    records, tenders, skipped = [], [], []
    for index, result in enumerate(payload["results"]):
        metadata = result.get("metadata") or {}
        identifier = _first(metadata, "identifier")
        kind = str(_first(metadata, "type") or "")
        if kind == "0":
            tenders.append({"identifier": identifier, "title": _first(metadata, "title"),
                            "note": "procurement tender; not a grant opportunity"})
            continue
        if not identifier:
            skipped.append({"index": index, "reason": "missing topic identifier"})
            continue
        budget = _first(metadata, "budgetOverview")
        try:
            budget = json.loads(budget) if isinstance(budget, str) else budget
        except ValueError:
            budget = None
        pointer = f"/results/{index}/metadata"
        model = _first(metadata, "deadlineModel")
        status = _EU_STATUS.get(str(_first(metadata, "status") or ""), "unknown")
        opening = _eu_instant(_first(metadata, "startDate"))
        deadlines = _eu_deadlines(metadata.get("deadlineDate") or [], model, pointer + "/deadlineDate")
        if opening:
            deadlines.insert(0, {"kind": "opening", "text": str(_first(metadata, "startDate")),
                                 "timezone": "UTC (portal displays Europe/Brussels)", "instant": opening,
                                 "date": opening[:10], "locator": {"json_pointer": pointer + "/startDate"}})
        records.append(record(
            "eu-ft", "call", identifier, _first(metadata, "title") or identifier,
            source_url=_eu_topic_url(identifier), round_id=_first(metadata, "callIdentifier"),
            authority={"kind": "funder", "name": "European Commission"},
            funder={"name": "European Commission", "id": _first(metadata, "frameworkProgramme")},
            programme={"id": _first(metadata, "frameworkProgramme"), "title": _first(metadata, "callTitle")},
            instrument={"kinds": ["grant"], "native_label": _first(metadata, "typesOfAction"),
                        "locator": {"json_pointer": pointer + "/typesOfAction"}},
            financial_terms=_eu_budget(budget, pointer + "/budgetOverview"),
            status={"native": _first(metadata, "status"), "asserted": status, "locator": {"json_pointer": pointer + "/status"}},
            deadlines=deadlines,
            themes=[k for k in metadata.get("keywords") or [] if isinstance(k, str)][:50],
            documents=[{"url": _eu_topic_url(identifier), "title": "Topic conditions and documents", "kind": "call-text"}],
        ))
    total, size, page = (int(payload.get(k) or 0) for k in ("totalResults", "pageSize", "pageNumber"))
    has_more = bool(size and page and page * size < total)
    return {"records": records, "tenders": tenders, "skipped": skipped,
            "page": {"number": page, "size": size, "total": total, "has_more": has_more},
            # One page is never a complete listing; see FundingClient.eu_search_all.
            "coverage": {"complete": False, "listing": "eu-ft:search"}}


def parse_eu_topic(payload, *, source_url=None):
    """Authoritative topic details: conditions, actions, stages and budget."""
    details = (payload or {}).get("TopicDetails") if isinstance(payload, dict) else None
    if not isinstance(details, dict) or not details.get("identifier"):
        raise ProviderError("schema_drift", "topic details lack an identifier")
    identifier = details["identifier"]
    actions = details.get("actions") or [{}]
    action = actions[0]
    model = action.get("deadlineModel")
    status = {"open": "open", "forthcoming": "forthcoming", "closed": "closed"}.get(
        str((action.get("status") or {}).get("abbreviation", "")).lower(), "unknown")
    deadlines = _eu_deadlines(action.get("deadlineDates"), model, "/TopicDetails/actions/0/deadlineDates")
    if action.get("plannedOpeningDate"):
        opening = _eu_instant(action["plannedOpeningDate"])
        deadlines.insert(0, {"kind": "opening", "text": action["plannedOpeningDate"], "timezone": "UTC (portal displays Europe/Brussels)",
                             "instant": opening, "date": opening[:10], "locator": {"json_pointer": "/TopicDetails/actions/0/plannedOpeningDate"}})
    requirements = []
    conditions = details.get("conditions") or details.get("topicConditions") or ""
    if conditions:
        paras, _ = paragraphs(conditions.encode() if isinstance(conditions, str) else conditions)
        for p in paras:
            if re.search(r"eligib|consortium|legal entit|established in|member state|associated countr", p["text"], re.IGNORECASE):
                # Consortium composition (entity counts, distinct countries) is
                # not expressible as a single-fact rule; it stays unparsed until
                # a reviewed human interpretation exists.
                requirements.append({
                    "requirement_id": f"eu-ft:{identifier}:cond-{p['index']}",
                    "category": "consortium" if re.search(r"consortium|legal entities", p["text"], re.IGNORECASE) else "establishment",
                    "text": p["text"], "hard": True, "locator": {"json_pointer": "/TopicDetails/conditions", "paragraph": p["index"], "quote": p["text"][:400]},
                })
    url = source_url or _eu_topic_url(identifier)
    return {"records": [record(
        "eu-ft", "call", identifier, details.get("title") or identifier, source_url=url,
        round_id=details.get("callIdentifier"),
        authority={"kind": "funder", "name": "European Commission"},
        funder={"name": "European Commission", "id": details.get("frameworkProgramme")},
        programme={"id": details.get("frameworkProgramme"), "title": details.get("callTitle")},
        instrument={"kinds": ["grant"], "native_label": ", ".join(t.get("typeOfAction", "") for t in action.get("types") or []) or None,
                    "locator": {"json_pointer": "/TopicDetails/actions/0/types"}},
        financial_terms=_eu_budget(details.get("budgetOverviewJSONItem"), "/TopicDetails/budgetOverviewJSONItem"),
        status={"native": (action.get("status") or {}).get("abbreviation"), "asserted": status,
                "locator": {"json_pointer": "/TopicDetails/actions/0/status"}},
        deadlines=deadlines, requirements=requirements,
        themes=[k for k in details.get("keywords") or [] if isinstance(k, str)][:50],
        documents=[{"url": u.get("url"), "title": u.get("title") or "document", "kind": "guideline"}
                   for u in details.get("callDocuments") or details.get("topicDocuments") or [] if isinstance(u, dict) and u.get("url")],
    )], "coverage": {"complete": False, "listing": "eu-ft:topic:" + identifier}}


# ------------------------------------------------------- Förderdatenbank


def _fdb_fields(soup):
    fields = {}
    for dl in soup.select("dl"):
        for dt in dl.select("dt"):
            dd = dt.find_next_sibling("dd")
            if dd is None:
                continue
            key = dt.get_text(" ", strip=True).rstrip(":").strip()
            fields[key] = {"text": dd.get_text(" ", strip=True),
                           "links": [a.get("href") for a in dd.select("a[href]")],
                           "locator": {"selector": f"dl dt:-soup-contains('{key}') + dd", "quote": dd.get_text(" ", strip=True)[:400]}}
    return fields


def _provider_for(url):
    host = urlsplit(url).hostname or ""
    for provider, hosts in PROVIDER_HOSTS.items():
        if host in hosts:
            return provider
    return None


def parse_foerderdatenbank_programme(raw, *, source_url):
    """A programme page as a directory entry with administering-body references."""
    url = _official("foerderdatenbank", source_url)
    paras, soup = paragraphs(raw)
    title = soup.find("h1")
    if title is None:
        raise ProviderError("schema_drift", "programme page has no title")
    fields = _fdb_fields(soup)
    if not fields:
        raise ProviderError("schema_drift", "programme page has no field definitions")
    path = urlsplit(url).path
    provider_id = re.sub(r"^/FDB/Content/DE/Foerderprogramm/|\.html$", "", path)
    labels = [s.strip() for s in re.split(r",|;", (fields.get("Förderart") or {}).get("text", "")) if s.strip()]
    kinds = sorted({_FDB_INSTRUMENT.get(label.lower(), "unknown") for label in labels}) or ["unknown"]
    references = []
    for key in ("Weiterführende Links", "Ansprechpunkt"):
        for href in (fields.get(key) or {}).get("links", []):
            if not href or href.startswith("mailto:"):
                continue
            absolute = urljoin(url, href)
            target = _provider_for(absolute)
            references.append({"kind": "administering-body" if key == "Ansprechpunkt" else "programme",
                               "url": absolute, "provider": target,
                               "provider_id": _exist_id(absolute) if target == "exist" else None,
                               "name": (fields.get(key) or {}).get("text", "")[:200]})
    requirements = []
    if fields.get("Förderberechtigte"):
        requirements.append({"requirement_id": f"foerderdatenbank:{provider_id}:berechtigte", "category": "applicant-type",
                             "text": fields["Förderberechtigte"]["text"], "hard": None,
                             "locator": fields["Förderberechtigte"]["locator"]})
    if fields.get("Fördergebiet"):
        requirements.append({"requirement_id": f"foerderdatenbank:{provider_id}:gebiet", "category": "establishment",
                             "text": fields["Fördergebiet"]["text"], "hard": None,
                             "locator": fields["Fördergebiet"]["locator"]})
    return {"records": [record(
        "foerderdatenbank", "directory_entry", provider_id, title.get_text(" ", strip=True), source_url=url,
        language="de", authority={"kind": "directory", "name": "Förderdatenbank des Bundes"},
        funder={"name": (fields.get("Fördergeber") or {}).get("text"), "id": None},
        instrument={"kinds": kinds, "native_label": (fields.get("Förderart") or {}).get("text"),
                    "locator": (fields.get("Förderart") or {}).get("locator")},
        financial_terms={"currency": None, "eligible_costs": None},
        requirements=requirements, references=references,
        themes=[s.strip() for s in re.split(r",", (fields.get("Förderbereich") or {}).get("text", "")) if s.strip()],
        sections=[{"text": p["text"], "locator": _locator(p)} for p in paras[:200]],
    )], "coverage": {"complete": False, "listing": "foerderdatenbank:programme"}}


def parse_foerderdatenbank_results(raw, *, source_url):
    """Result list → distinct programme page URLs (duplicates removed, order kept)."""
    url = _official("foerderdatenbank", source_url)
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(raw, "html.parser")
    seen, programmes, duplicates = set(), [], 0
    for anchor in soup.select("a[href]"):
        target = urljoin(url, anchor["href"]).split("#")[0]
        if "/FDB/Content/DE/Foerderprogramm/" not in target or not target.endswith(".html"):
            continue
        if target in seen:
            duplicates += 1
            continue
        seen.add(target)
        programmes.append({"url": _official("foerderdatenbank", target), "title": anchor.get_text(" ", strip=True)})
    next_link = soup.select_one("a[rel=next], a.forward, li.next a")
    return {"programmes": programmes, "duplicates_removed": duplicates,
            "next_page": urljoin(url, next_link["href"]) if next_link else None}


# ------------------------------------------------------------------- EXIST

EXIST_PATTERNS = [
    ("university-affiliation", "affiliation", r"(universit|higher education institution|research institution|Hochschule)[^.]{0,120}(support|mentor|host|appl|through|via)",
     {"fact": "applicant.university_affiliation", "op": "is_true"}),
    ("academic-status", "applicant-type", r"(students|graduates|scientists|researchers)[^.]{0,120}(eligible|apply|can)",
     {"fact": "applicant.academic_status", "op": "in", "value": ["student", "graduate", "researcher", "doctoral"]}),
    ("team-size", "applicant-type", r"teams? of up to (three|3)",
     {"fact": "applicant.team_size", "op": "lte", "value": 3}),
    ("not-founded", "project-stage", r"(not yet been founded|before (the )?(company is )?founded|prior to founding|not yet (been )?incorporated)",
     {"fact": "applicant.incorporated", "op": "is_false"}),
    ("submission-route", "submission-route", r"(application|apply)[^.]{0,80}(submitted|made)[^.]{0,80}(university|research institution|Projektträger|project management agency)", None),
    ("duration", "duration", r"(maximum|up to|for)\s+(12|18|twelve|eighteen) months", None),
]


def _exist_id(url):
    path = urlsplit(url).path.rstrip("/")
    return re.sub(r"\.html$", "", path.replace("/EXIST/Navigation/", "").replace("/EXIST/Redaktion/", "")) or "exist"


def parse_exist_programme(raw, *, source_url):
    url = _official("exist", source_url)
    paras, soup = paragraphs(raw)
    title = soup.find("h1")
    if title is None:
        raise ProviderError("schema_drift", "EXIST page has no title")
    requirements = _pattern_requirements("exist", paras, EXIST_PATTERNS)
    stipend = [(m, p) for p in paras for m in re.finditer(
        r"(?:€|EUR)\s?(?P<amount>[\d.,]+)\s*(?:per month|a month|monthly)", p["text"], re.IGNORECASE)]
    terms = {"currency": None, "eligible_costs": None}
    if stipend:
        values = sorted(Decimal(_amount(m.group("amount"))) for m, _ in stipend)
        terms = {"currency": "EUR", "award_range": {"min": str(values[0]), "max": str(values[-1]), "basis": "per-person-month",
                                                     "locator": _locator(stipend[0][1], stipend[0][0].group(0))},
                 "eligible_costs": None}
    costs = [p["text"] for p in paras if p["tag"] == "li" and p["section"] and re.search(r"cost|expens|fund|support|Förder", p["section"], re.IGNORECASE)]
    if costs:
        terms["eligible_costs"] = costs[:20]
    kinds = ["stipend"] if stipend else ["grant"] if re.search(r"\bgrant\b|Zuschuss", " ".join(p["text"] for p in paras), re.IGNORECASE) else ["unknown"]
    rolling = _find(paras, r"(no (fixed )?deadline|at any time|jederzeit)")[0]
    return {"records": [record(
        "exist", "programme", _exist_id(url), title.get_text(" ", strip=True), source_url=url,
        language="de" if "/DE/" in url else "en",
        authority={"kind": "funder", "name": "Federal Ministry (BMWE), EXIST programme"},
        funder={"name": "Bundesministerium für Wirtschaft und Energie", "id": "exist"},
        instrument={"kinds": kinds, "native_label": None},
        financial_terms=terms, requirements=requirements,
        status={"native": None, "asserted": "rolling" if rolling else "unknown"},
        documents=[{"url": urljoin(url, a["href"]), "title": a.get_text(" ", strip=True) or "document", "kind": "guideline"}
                   for a in soup.select("a[href$='.pdf']")][:20],
        sections=[{"text": p["text"], "locator": _locator(p)} for p in paras[:200]],
    )], "coverage": {"complete": False, "listing": "exist:" + _exist_id(url)}}


# ------------------------------------------------------ client & evidence


class FundingClient:
    """Bounded provider access through one explicit DurableHTTP budget."""

    NLNET_PROPOSE = "https://nlnet.nl/propose/"
    EU_SEARCH = "https://api.tech.ec.europa.eu/search-api/prod/rest/search"
    EU_TOPIC = "https://ec.europa.eu/info/funding-tenders/opportunities/data/topicDetails/{}.json"

    def __init__(self, http: DurableHTTP, *, principal_id):
        if http.provider not in PROVIDER_HOSTS or not http.hosts <= PROVIDER_HOSTS[http.provider]:
            raise ValueError("funding client requires an exact provider-specific host policy")
        self.http, self.principal_id = http, principal_id

    def _fetch(self, key, url, **kwargs):
        return self.http.request(key, url, principal_id=self.principal_id, **kwargs)

    def _require(self, provider):
        if self.http.provider != provider:
            raise ValueError(f"client is bound to {self.http.provider}, not {provider}")

    def nlnet_calls(self, observation):
        self._require("nlnet")
        captured = self._fetch(observation + ":propose", self.NLNET_PROPOSE, headers={"Accept": "text/html"})
        return parse_nlnet_propose(captured.content), captured

    def nlnet_fund(self, url, observation):
        self._require("nlnet")
        captured = self._fetch(observation + ":fund:" + url, _official("nlnet", url), headers={"Accept": "text/html"})
        return parse_nlnet_fund(captured.content, source_url=url), captured

    def eu_search(self, observation, *, text, page=1, page_size=50):
        self._require("eu-ft")
        if not 1 <= page <= 20 or not 1 <= page_size <= 100:
            raise ValueError("bounded pagination required")
        captured = self._fetch(f"{observation}:search:{page}", self.EU_SEARCH, method="GET",
                               params={"text": text, "pageSize": str(page_size), "pageNumber": str(page)},
                               secret_params={"apiKey": "SEDIA"}, max_bytes=8_000_000)
        return parse_eu_search(captured.json()), captured

    def eu_search_all(self, observation, *, text, page_size=50, max_pages=5):
        """Follow pagination within an explicit page bound; completeness only if all pages were read."""
        merged, captures, page = {"records": [], "tenders": [], "skipped": []}, [], 1
        while True:
            parsed, captured = self.eu_search(observation, text=text, page=page, page_size=page_size)
            captures.append(captured)
            for key in merged:
                merged[key].extend(parsed[key])
            if not parsed["page"]["has_more"]:
                complete = not merged["skipped"]
                break
            if page >= max_pages:
                complete = False
                break
            page += 1
        merged["coverage"] = {"complete": complete, "listing": "eu-ft:search:" + text, "pages": page,
                              "total": parsed["page"]["total"]}
        return merged, captures

    def eu_topic(self, identifier, observation):
        self._require("eu-ft")
        if not re.fullmatch(r"[A-Za-z0-9._-]{3,200}", identifier):
            raise ValueError("invalid topic identifier")
        captured = self._fetch(f"{observation}:topic:{identifier}", self.EU_TOPIC.format(identifier.lower()), max_bytes=5_000_000)
        return parse_eu_topic(captured.json()), captured

    def foerderdatenbank_programme(self, url, observation):
        self._require("foerderdatenbank")
        captured = self._fetch(observation + ":programme:" + url, _official("foerderdatenbank", url), headers={"Accept": "text/html"})
        return parse_foerderdatenbank_programme(captured.content, source_url=url), captured

    def foerderdatenbank_results(self, url, observation):
        self._require("foerderdatenbank")
        captured = self._fetch(observation + ":results:" + url, _official("foerderdatenbank", url), headers={"Accept": "text/html"})
        return parse_foerderdatenbank_results(captured.content, source_url=url), captured

    def exist_programme(self, url, observation):
        self._require("exist")
        captured = self._fetch(observation + ":exist:" + url, _official("exist", url), headers={"Accept": "text/html"})
        return parse_exist_programme(captured.content, source_url=url), captured


class FundingEvidenceStore:
    """Persist captures as documents and apply normalized records."""

    def __init__(self, conn, *, now=None):
        self.conn = conn
        self.documents = DocumentStore(conn)
        self.opportunities = FundingOpportunityStore(conn, now=now)

    def ingest(self, provider, parsed, captured, *, namespace, scopes, reuse_notice):
        if not reuse_notice:
            raise ValueError("explicit reuse notice required")
        captures = captured if isinstance(captured, list) else [captured]
        if not captures or any(hashlib.sha256(c.content).hexdigest() != c.receipt.get("digest") for c in captures):
            raise ProviderError("source_changed", "capture digest mismatch")
        records = parsed["records"]
        capture_digests = [c.receipt["digest"] for c in captures]
        observed_at_ms = max(c.receipt["observed_at_ms"] for c in captures)
        observation_id = "funding-observation:" + digest([namespace, provider, capture_digests])[:32]
        documents = []
        for item in records:
            documents.append(Document(
                document_id="funding:" + digest([namespace, provider, item["record_kind"], item["provider_id"], item.get("round_id")])[:32],
                source_type="web", source_id=f"{provider}:{item['provider_id']}", language=item["language"],
                ingested_at=observed_at_ms, url=item["source_url"], title=item["title"],
                content="\n\n".join(s["text"] for s in item.get("sections") or []) or canonical({k: v for k, v in item.items() if k != "sections"}),
                metadata={"funding_contract": item["contract"], "namespace": namespace,
                          "funding_record_json": canonical({k: v for k, v in item.items() if k != "sections"}),
                          "source_license": reuse_notice, "native_capture_sha256": ",".join(capture_digests),
                          "record_kind": item["record_kind"]},
            ))
        outcome = self.documents.upsert(documents) if documents else None
        if outcome is not None and outcome.invalid:
            raise ProviderError("document_validation", "funding evidence failed document validation")
        executions = sorted({c.receipt.get("execution") or "unknown" for c in captures})
        evidence = {"capture_digests": capture_digests, "execution": executions[0] if len(executions) == 1 else "mixed",
                    "snapshots": [(c.receipt.get("snapshot") or {}).get("digest") for c in captures],
                    "documents": [{"document_id": c["document_id"], "revision_id": c["revision_id"]}
                                  for c in (outcome.changes if outcome else [])]}
        applied = self.opportunities.ingest(
            namespace, provider, records, observation_id=observation_id,
            observed_at_ms=observed_at_ms, scopes=scopes,
            evidence=evidence, coverage=parsed.get("coverage"))
        return {**applied, "evidence": evidence, "execution": evidence["execution"],
                "tenders_excluded": len(parsed.get("tenders") or [])}

    def fail(self, provider, error, *, namespace, scopes, observation_id, observed_at_ms):
        """Record a failed/partial acquisition; this never closes any call."""
        code = getattr(error, "code", type(error).__name__)
        return self.opportunities.record_failure(
            namespace, provider, observation_id=observation_id, failure_code=code,
            observed_at_ms=observed_at_ms, scopes=scopes)


def acquire(client, provider, fetch, *, namespace, scopes, reuse_notice, observation, store=None):
    """Run one bounded fetch; parse failures and transport failures leave calls untouched."""
    store = store or FundingEvidenceStore(client.http.conn)
    try:
        parsed, captured = fetch()
    except (ProviderError, FundingRecordError, ValueError) as exc:
        failure = store.fail(provider, exc, namespace=namespace, scopes=scopes,
                             observation_id=observation, observed_at_ms=client.http.now())
        return {"ok": False, "provider": provider, "failure": failure}
    return {"ok": True, "provider": provider,
            **store.ingest(provider, parsed, captured, namespace=namespace, scopes=scopes, reuse_notice=reuse_notice)}
