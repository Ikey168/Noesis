"""Court dockets and justice statistics acquisition for the Legal pack (#2218, CJ01, CJ03-CJ06).

The Legal pack's court and justice-statistics features extend the Legal source
adapters in :mod:`src.ingestion.legal_sources` with one native connector,
``courts-justice``, registered beside them in the ``legal-research`` source
pack. It reads a bounded, declared selection from one documented provider per
source and emits ``noesis-court-justice-record-v1`` records as the provider
published them:

* ``courtlistener-docket-json`` - CourtListener REST API v4: a docket (court,
  docket number, case name as published, filing and termination dates), its
  RECAP docket entries (number, date, description verbatim, document links and
  availability; documents are linked, never mirrored) and its parties,
  minimised at ingestion (see ``MINIMISATION``);
* ``courtlistener-cluster-json`` - CourtListener opinion clusters: the
  published citations, the disposition quoted verbatim, and each opinion
  (author or per curiam, type, the opinions it cites and its text as paragraph
  passages with locators);
* ``fbi-cde-summarized-json`` - FBI Crime Data Explorer summarized offence
  counts for a state or agency ORI and a period, with rates, populations and
  participated populations (reporting coverage) as published, the declared
  UCR/NIBRS definition and the data refresh date as the vintage;
* ``police-uk-crimes-json`` - data.police.uk street-level crimes for one
  declared force area (neighbourhood or polygon) and month, aggregated to
  counts per crime category and outcome category as published; locations and
  persistent IDs are not persisted and the publisher's anonymisation (snap
  points) is recorded as a coverage note;
* ``eurostat-crime-jsonstat`` - Eurostat crime and criminal justice datasets
  (ICCS) through the existing :class:`~src.ingestion.connectors.dataset.eurostat.EurostatConnector`
  (``parse_cells``: no dimension collapsed, status flags kept verbatim) with
  the dataset's ESMS comparability section captured as a comparability note
  for the declared countries.

Every page is one selection unit and is all-or-nothing: a list longer than
one page is ``budget_exhausted``, never truncated, and a response from another
host is a network-policy failure. Receipts name every request path, status and
response digest; API keys travel in a header and never appear in a receipt or
a record. ``PROVIDER_CONTRACTS``, ``MINIMISATION``, ``BOUNDED_COVERAGE`` and
``LIVE_VERIFICATION`` are the machine-readable copy of the CJ01 audit
(``docs/development/courts-justice-evidence/source-audit.md``).

Nothing here predicts outcomes, labels a case won or lost, scores recidivism
or risk, rates neighbourhood safety, ranks places or gives legal advice.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlencode, urlsplit

from src.ingestion.source_packs import SourcePackError

ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
RECORD_CONTRACT = "noesis-court-justice-record-v1"
CONNECTOR = "courts-justice"
PAGE_SIZE = 100
MAX_UNITS = 20
REVIEW_BOUNDARY = ("Docket, opinion and statistics records are kept as published. Case outcomes are quoted "
                   "disposition text only; nothing here is legal advice, an outcome prediction, a risk score, a "
                   "safety rating or a ranking of places.")
FORMATS: dict[str, dict[str, Any]] = {
    "courtlistener-docket-json": {"provider": "courtlistener", "jurisdiction": "US", "unit": "dockets",
                                  "keyed": True, "feature": "courts"},
    "courtlistener-cluster-json": {"provider": "courtlistener", "jurisdiction": "US", "unit": "clusters",
                                   "keyed": True, "feature": "courts"},
    "fbi-cde-summarized-json": {"provider": "fbi-cde", "jurisdiction": "US", "unit": "series", "keyed": True,
                                "feature": "justice-statistics"},
    "police-uk-crimes-json": {"provider": "police-uk", "jurisdiction": "GB", "unit": "areas", "keyed": False,
                              "feature": "justice-statistics"},
    "eurostat-crime-jsonstat": {"provider": "eurostat", "jurisdiction": "EU", "unit": "datasets", "keyed": False,
                                "feature": "justice-statistics"},
}
OGL_ATTRIBUTION = "Contains public sector information licensed under the Open Government Licence v3.0."
EUROSTAT_ATTRIBUTION = "Source: Eurostat (reuse authorised, Commission Decision 2011/833/EU)."
CL_ATTRIBUTION = "Source: CourtListener, Free Law Project (court records are public; credit the source)."
FBI_ATTRIBUTION = "Source: FBI Crime Data Explorer, Uniform Crime Reporting Program (US government work)."

# CJ01 access decisions. Endpoints, fields and terms are recorded from the providers' published documentation as
# known without network access; every item marked ``verify`` must be checked against the live documentation, terms
# and a real response before a dated live run is accepted (CJ14, #2434).
PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "courtlistener": {
        "publisher": "Free Law Project (CourtListener REST API v4, RECAP archive)",
        "endpoints": ["/api/rest/v4/dockets/{id}/", "/api/rest/v4/docket-entries/?docket={id}",
                      "/api/rest/v4/parties/?docket={id}", "/api/rest/v4/clusters/{id}/",
                      "/api/rest/v4/opinions/?cluster={id}"],
        "formats": ["courtlistener-docket-json", "courtlistener-cluster-json"],
        "authentication": "API token (required-secret NOESIS_COURTLISTENER_API_TOKEN) sent as the "
        "'Authorization: Token' header, never in a URL, receipt or record; the RECAP docket-entries and parties "
        "endpoints may need an additional entitlement (verify) - without it the unit fails authentication_failed",
        "rate_limits": "5,000 requests per hour per authenticated user (verify); one bounded selection per run",
        "pagination": "cursor pagination with page_size <= 100; a list with a next link is budget_exhausted, "
        "never truncated",
        "identifiers": ["CourtListener docket id", "court id", "docket number as published", "PACER case id",
                        "cluster id", "opinion id", "reporter citations as published"],
        "revisions": "date_modified on dockets, entries, clusters and opinions; a changed record is a new "
        "revision; earlier revisions stay queryable",
        "licence": "court records are public; CourtListener asks for attribution and no bulk scraping of the web "
        "site (verify the API terms of service); RECAP documents are linked, never mirrored",
        "attribution": CL_ATTRIBUTION,
        "access_decision": "unverified-live",
        "reason": "fixture-verified parsers in the documented v4 JSON shape; field names (court_id, "
        "recap_documents, party_types, sub_opinions, opinions_cited) and the RECAP entitlement must be verified",
    },
    "fbi-cde": {
        "publisher": "Federal Bureau of Investigation (Crime Data Explorer API)",
        "endpoints": ["/summarized/state/{state}/{offense}?from=MM-YYYY&to=MM-YYYY",
                      "/summarized/agency/{ori}/{offense}?from=MM-YYYY&to=MM-YYYY"],
        "formats": ["fbi-cde-summarized-json"],
        "authentication": "api.data.gov key (required-secret NOESIS_FBI_CDE_API_KEY) sent as the X-Api-Key "
        "header, never in a URL or receipt",
        "rate_limits": "api.data.gov default 1,000 requests per hour per key (verify)",
        "pagination": "none; one summarized response per state or agency, offence and period",
        "identifiers": ["state postal abbreviation", "agency ORI", "offence code as the API names it",
                        "MM-YYYY period", "cde_properties.last_refresh_date as the data release"],
        "revisions": "a changed last_refresh_date with changed figures is a new vintage; earlier vintages stay "
        "queryable",
        "licence": "US government work, public domain; cite the FBI UCR Program (verify)",
        "attribution": FBI_ATTRIBUTION,
        "access_decision": "unverified-live",
        "reason": "fixture-verified parser for the documented summarized shape (offenses.actuals/rates, "
        "populations.population/participated_population, cde_properties); the response keys and the SRS vs "
        "NIBRS provenance of summarized counts must be verified; offence definitions are cited from the UCR "
        "handbook declared in the selection, not from the API",
    },
    "police-uk": {
        "publisher": "Home Office / police forces of England, Wales and Northern Ireland (data.police.uk API)",
        "endpoints": ["/api/crime-last-updated", "/api/crime-categories?date=YYYY-MM",
                      "/api/crimes-street/{category}?date=YYYY-MM&poly=...",
                      "/api/{force}/{neighbourhood}/boundary"],
        "formats": ["police-uk-crimes-json"],
        "authentication": "none",
        "rate_limits": "15 requests per second with a burst of 30 (verify); street-level queries return at most "
        "10,000 crimes and HTTP 503 above that",
        "pagination": "none; a 503 for an oversized area fails the unit (source_unavailable), never truncated",
        "identifiers": ["force id", "neighbourhood id", "crime category url", "month (YYYY-MM)",
                        "crime-last-updated date as the monthly release"],
        "revisions": "each monthly release is a vintage; a revised month (changed counts under a new "
        "crime-last-updated date) is a new vintage",
        "licence": "Open Government Licence v3.0",
        "attribution": OGL_ATTRIBUTION,
        "access_decision": "unverified-live",
        "reason": "anonymous documented API; fixture-verified; category and outcome names are kept as published",
        "anonymisation": "locations are snapped by the publisher to anonymous map points; Noesis stores no "
        "location, street or persistent crime ID and never tries to recover a location",
    },
    "eurostat": {
        "publisher": "Eurostat (dissemination API, JSON-stat 2.0; ESMS reference metadata)",
        "endpoints": ["/api/dissemination/statistics/1.0/data/{dataset}?format=JSON&geo=..",
                      "/cache/metadata/en/{dataset_family}_esms.htm"],
        "formats": ["eurostat-crime-jsonstat"],
        "authentication": "none",
        "rate_limits": "undocumented fair use (verify); one request per declared country and one ESMS page",
        "pagination": "none; a cube larger than the declared cell ceiling fails the unit",
        "identifiers": ["dataset code (crim_off_cat, crim_just_*)", "ICCS code", "unit code", "GEO code",
                        "period", "cube 'updated' stamp as the vintage"],
        "revisions": "a new 'updated' stamp is a new vintage; earlier vintages stay queryable",
        "licence": "Eurostat reuse policy (Commission Decision 2011/833/EU): reuse with attribution",
        "attribution": EUROSTAT_ATTRIBUTION,
        "access_decision": "unverified-live",
        "reason": "reached through the existing EurostatConnector (parse_cells keeps every flag); the ESMS HTML "
        "section ids are operator-declared and must be verified",
    },
}
LIVE_VERIFICATION = {
    provider: {"status": contract["access_decision"],
               "note": "no dated live run from this runtime; offline fixtures only (CJ14, #2434)"}
    for provider, contract in PROVIDER_CONTRACTS.items()
}
# CJ01 party-data minimisation decision (also docs/development/courts-justice-evidence/source-audit.md).
MINIMISATION = {
    "stored_for_every_party": ["role(s) as published", "party type flag (organisation | natural_person)",
                               "docket-scoped ordinal"],
    "stored_for_organisations": ["name as published"],
    "stored_for_natural_persons": ["a docket-scoped pseudonym ('natural person 1 (Defendant)')"],
    "never_stored": ["natural-person names in party records", "addresses", "dates of birth", "attorneys and "
                     "their contact details", "party extra_info", "CourtListener party ids of natural persons",
                     "crime locations, streets and persistent crime IDs"],
    "classification": "a party is an organisation only when its published name carries a legal-form or "
    "public-body token (Inc., LLC, Corp., Ltd, Bank, Department, Agency, County, City of, State of, United "
    "States, ...); every other party - including an unclear one - is treated as a natural person",
    "pseudonymisation": "always for natural persons in party records; case captions and docket-entry "
    "descriptions are court-published text and are kept verbatim, never parsed into person records",
    "no_profile": "natural persons are never matched to entities, aggregated across dockets, used as a query "
    "key or used as a subscription target; no personal profile is assembled",
}
# CJ01 bounded first coverage: nothing implies complete coverage of a court, a statute or a statistics programme.
BOUNDED_COVERAGE = {
    "dockets": "the declared CourtListener dockets (at most 20 per source) of the declared federal courts "
    "(fixtures: one fictional D.D.C. docket filed in 2099) and the opinion clusters named in the selection; "
    "seed provisions 42 U.S.C. § 1983 and 15 U.S.C. § 45; organisational parties only as published",
    "fbi-cde": "the declared states or agency ORIs, offences (UCR SRS Part I offences) and a 12-month window per "
    "unit (fixtures: a placeholder state 'EX' and agency ORI EX0000100, burglary, 2098)",
    "police-uk": "the declared force neighbourhoods or polygons and months (fixtures: one fictional "
    "neighbourhood, 2099-01)",
    "eurostat": "the declared crime datasets (crim_off_cat), ICCS codes, units and countries (fixtures: DE and "
    "FR, ICCS0401 robbery, 2096-2098)",
}
ORGANISATION_TOKENS = re.compile(
    r"\b(inc|incorporated|corp|corporation|co|company|companies|llc|l\.l\.c|llp|lp|ltd|limited|plc|gmbh|ag|n\.a|"
    r"bank|trust|association|assn|union|university|college|foundation|institute|agency|department|dept|office|"
    r"commission|board|bureau|authority|county|city|state|states|commonwealth|government|district|school|"
    r"hospital|church|council|committee|partners|partnership|group|holdings|services|systems|technologies|"
    r"industries|corp\.|federal|republic|municipality|township|village|people)\b\.?", re.I)


class CourtsJusticeFormatError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                                     default=str).encode()).hexdigest()


def _clean(value: Any) -> str | None:
    text = " ".join(str(value if value is not None else "").split())
    return text or None


def _verbatim(value: Any) -> str | None:
    """Text kept exactly as published (only surrounding whitespace removed)."""
    text = str(value if value is not None else "").strip()
    return text or None


def _day(value: Any) -> str | None:
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", str(value or "").strip())
    return match.group(1) if match else None


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CourtsJusticeFormatError("schema_drift", "response is not valid UTF-8 JSON") from exc


def _mapping(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CourtsJusticeFormatError("schema_drift", f"{what} is not an object")
    return dict(value)


def _results(payload: Any, what: str) -> list[dict[str, Any]]:
    """A CourtListener list page that must be the whole list."""
    payload = _mapping(payload, what)
    results = payload.get("results")
    if not isinstance(results, list):
        raise CourtsJusticeFormatError("schema_drift", f"{what} has no results list")
    count = payload.get("count")
    if payload.get("next") or (isinstance(count, int) and count > len(results)):
        raise CourtsJusticeFormatError("input_limit", f"{what} is longer than one page; the unit is not truncated")
    return [_mapping(item, what) for item in results]


def party_type(name: Any) -> str:
    """``organisation`` only when the published name carries a legal-form or public-body token (CJ01)."""
    return "organisation" if ORGANISATION_TOKENS.search(str(name or "")) else "natural_person"


def _record(fmt: str, kind: str, record_key: str, *, native_revision: Any, title: Any, locator: str,
            published_at: Any, fields: Mapping[str, Any], jurisdiction: str | None = None) -> dict[str, Any]:
    spec = FORMATS[fmt]
    if not str(locator or "").startswith("https://"):
        raise CourtsJusticeFormatError("schema_drift", f"{record_key} has no HTTPS locator")
    return {"contract": RECORD_CONTRACT, "format": fmt, "provider": spec["provider"],
            "jurisdiction": jurisdiction or spec["jurisdiction"], "record_kind": kind, "record_key": record_key,
            "native_revision": _clean(native_revision), "title": _clean(title) or record_key, "locator": locator,
            "published_at": _day(published_at), "fields": dict(fields)}


# ----------------------------------------------------------------- CourtListener

CL_SITE = "https://www.courtlistener.com"


def _cl_url(value: Any, fallback: str) -> str:
    text = str(value or "")
    if text.startswith("/"):
        return CL_SITE + text
    return text if text.startswith("https://") else fallback


def _parties(docket_id: int, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Minimise at ingestion: only role, type flag and (organisations only) the name as published."""
    out = []
    counters: dict[str, int] = {}
    for ordinal, item in enumerate(items, start=1):
        roles = sorted({_clean(t.get("name")) for t in item.get("party_types") or []
                        if isinstance(t, Mapping) and _clean(t.get("name"))})
        kind = party_type(item.get("name"))
        if kind == "organisation":
            out.append({"ordinal": ordinal, "party_type": kind, "name_as_published": _clean(item.get("name")),
                        "roles": roles, "party_key": f"courts:party:courtlistener:{docket_id}:{ordinal}"})
        else:
            counters["natural_person"] = counters.get("natural_person", 0) + 1
            label = f"natural person {counters['natural_person']}" + (f" ({', '.join(roles)})" if roles else "")
            out.append({"ordinal": ordinal, "party_type": kind, "name_as_published": None, "pseudonym": label,
                        "roles": roles, "party_key": None})
    return out


def _entries(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for item in items:
        documents = []
        for doc in item.get("recap_documents") or []:
            if not isinstance(doc, Mapping):
                raise CourtsJusticeFormatError("schema_drift", "a RECAP document is not an object")
            documents.append({"recap_document_id": doc.get("id"), "document_number": _clean(doc.get("document_number")),
                              "attachment_number": doc.get("attachment_number"),
                              "description": _verbatim(doc.get("description")),
                              "is_available": doc.get("is_available"), "page_count": doc.get("page_count"),
                              "link": _cl_url(doc.get("absolute_url"), CL_SITE)})
        out.append({"entry_id": item.get("id"), "entry_number": item.get("entry_number"),
                    "date_filed": _day(item.get("date_filed")), "description": _verbatim(item.get("description")),
                    "date_modified": _clean(item.get("date_modified")), "documents": documents,
                    "document_policy": "linked, not mirrored"})
    out.sort(key=lambda e: (e["entry_number"] is None, e["entry_number"] or 0, e["date_filed"] or "",
                            str(e["entry_id"])))
    return out


def parse_docket(responses: Mapping[str, bytes], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    docket = _mapping(_json(responses["docket"]), "docket")
    docket_id = docket.get("id")
    if not isinstance(docket_id, int) or docket_id != int(unit["id"]):
        raise CourtsJusticeFormatError("schema_drift", "the docket is not the requested one")
    court_id = _clean(docket.get("court_id")) or _clean(str(docket.get("court") or "").rstrip("/").rsplit("/", 1)[-1])
    if not court_id or not _clean(docket.get("docket_number")):
        raise CourtsJusticeFormatError("schema_drift", "a docket states no court or docket number")
    if unit.get("court") and unit["court"] != court_id:
        raise CourtsJusticeFormatError("schema_drift", "the docket belongs to another court than declared")
    entries = _entries(_results(_json(responses["entries"]), "docket entries"))
    parties = _parties(docket_id, _results(_json(responses["parties"]), "parties"))
    fields = {
        "courtlistener_docket_id": docket_id, "court_id": court_id,
        "docket_number": _clean(docket.get("docket_number")), "case_name": _verbatim(docket.get("case_name")),
        "date_filed": _day(docket.get("date_filed")), "date_terminated": _day(docket.get("date_terminated")),
        "date_modified": _clean(docket.get("date_modified")), "pacer_case_id": _clean(docket.get("pacer_case_id")),
        "cause": _verbatim(docket.get("cause")), "nature_of_suit": _verbatim(docket.get("nature_of_suit")),
        "entries": entries, "parties": parties,
    }
    locator = _cl_url(docket.get("absolute_url"), f"{CL_SITE}/docket/{docket_id}/")
    return [_record("courtlistener-docket-json", "docket", f"courts:docket:courtlistener:{docket_id}",
                    native_revision=docket.get("date_modified"), title=docket.get("case_name"), locator=locator,
                    published_at=docket.get("date_filed"), fields=fields)]


def citation_text(citation: Mapping[str, Any]) -> str | None:
    volume, reporter, page = citation.get("volume"), _clean(citation.get("reporter")), _clean(citation.get("page"))
    return f"{volume} {reporter} {page}" if volume is not None and reporter and page else None


def _paragraphs(text: Any) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", str(text or "")) if p.strip()]


def parse_cluster(responses: Mapping[str, bytes], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    cluster = _mapping(_json(responses["cluster"]), "cluster")
    cluster_id = cluster.get("id")
    if not isinstance(cluster_id, int) or cluster_id != int(unit["id"]):
        raise CourtsJusticeFormatError("schema_drift", "the cluster is not the requested one")
    opinions = []
    for item in _results(_json(responses["opinions"]), "opinions"):
        if item.get("cluster_id") not in (None, cluster_id):
            raise CourtsJusticeFormatError("schema_drift", "an opinion belongs to another cluster")
        cited = []
        for url in item.get("opinions_cited") or []:
            match = re.search(r"/opinions/(\d+)/?$", str(url))
            if match:
                cited.append(int(match.group(1)))
        opinions.append({"opinion_id": item.get("id"), "type": _clean(item.get("type")),
                         "author_str": _clean(item.get("author_str")), "per_curiam": bool(item.get("per_curiam")),
                         "date_modified": _clean(item.get("date_modified")), "opinions_cited": sorted(cited),
                         "paragraphs": _paragraphs(item.get("plain_text")),
                         "text_sha256": hashlib.sha256(str(item.get("plain_text") or "").encode()).hexdigest()})
    opinions.sort(key=lambda o: str(o["opinion_id"]))
    citations = [c for c in (citation_text(_mapping(c, "citation")) for c in cluster.get("citations") or []) if c]
    docket_id = cluster.get("docket_id")
    if docket_id is None:
        match = re.search(r"/dockets/(\d+)/?$", str(cluster.get("docket") or ""))
        docket_id = int(match.group(1)) if match else None
    fields = {
        "courtlistener_cluster_id": cluster_id, "courtlistener_docket_id": docket_id,
        "court_id": _clean(unit.get("court")), "case_name": _verbatim(cluster.get("case_name")),
        "date_filed": _day(cluster.get("date_filed")), "judges": _verbatim(cluster.get("judges")),
        "citations": citations, "disposition": _verbatim(cluster.get("disposition")),
        "precedential_status": _clean(cluster.get("precedential_status")),
        "date_modified": _clean(cluster.get("date_modified")), "opinions": opinions,
    }
    locator = _cl_url(cluster.get("absolute_url"), f"{CL_SITE}/opinion/{cluster_id}/")
    return [_record("courtlistener-cluster-json", "opinion-cluster", f"courts:cluster:courtlistener:{cluster_id}",
                    native_revision=cluster.get("date_modified"), title=cluster.get("case_name"), locator=locator,
                    published_at=cluster.get("date_filed"), fields=fields)]


# ----------------------------------------------------------------- statistics helpers


def _definition(declared: Any, *, classification: str) -> dict[str, Any]:
    item = dict(declared or {})
    for key in ("code", "label", "text", "source_url"):
        if not _clean(item.get(key)):
            raise SourcePackError("invalid_manifest", f"a {classification} definition declares code, label, text and "
                                                      "source_url")
    if not str(item["source_url"]).startswith("https://"):
        raise SourcePackError("invalid_manifest", "a definition cites an HTTPS source")
    return {"classification": item.get("classification") or classification, "code": _clean(item["code"]),
            "label": _clean(item["label"]), "text": _verbatim(item["text"]), "source_url": item["source_url"],
            "in_force_from": _day(item.get("in_force_from")), "in_force_to": _day(item.get("in_force_to"))}


# ----------------------------------------------------------------- FBI Crime Data Explorer


def _mmyyyy(value: str) -> str:
    match = re.fullmatch(r"(\d{2})-(\d{4})", value)
    if not match:
        raise CourtsJusticeFormatError("schema_drift", f"period {value!r} is not MM-YYYY")
    return f"{match.group(2)}-{match.group(1)}"


def parse_fbi(responses: Mapping[str, bytes], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    payload = _mapping(_json(responses["summarized"]), "summarized response")
    offenses = _mapping(payload.get("offenses"), "offenses")
    populations = _mapping(payload.get("populations") or {}, "populations")
    properties = _mapping(payload.get("cde_properties") or {}, "cde_properties")
    refresh = _mapping(properties.get("last_refresh_date") or {}, "last_refresh_date")
    release = _clean(refresh.get("UCR") or next(iter(refresh.values()), None))
    if not release:
        raise CourtsJusticeFormatError("schema_drift", "the response states no data refresh date (vintage)")
    scope, code = unit["scope"], str(unit["code"])
    place = {"scheme": "us-state" if scope == "state" else "fbi-ori", "code": code, "label": _clean(unit.get("label"))}
    definition = _definition(unit.get("definition"), classification=unit.get("programme") or "UCR-SRS")
    population = {k: dict(v) for k, v in _mapping(populations.get("population") or {}, "population").items()}
    participated = {k: dict(v) for k, v in _mapping(populations.get("participated_population") or {},
                                                   "participated_population").items()}
    observations = []
    for measure, unit_label in (("actuals", "count"), ("rates", "per 100,000 inhabitants")):
        for series_label, values in sorted(_mapping(offenses.get(measure) or {}, measure).items()):
            if "united states" in series_label.casefold():
                continue  # the national comparison series the API adds is not the requested place
            for period_raw, value in sorted(_mapping(values, series_label).items()):
                period = _mmyyyy(period_raw)
                coverage = {"population": next((p.get(period_raw) for p in population.values()), None),
                            "participated_population": next((p.get(period_raw) for p in participated.values()),
                                                            None),
                            "programme": unit.get("programme") or "UCR-SRS",
                            "estimated": properties.get("estimated")}
                observations.append({
                    "series_key": f"fbi-cde:{scope}:{code}:{unit['offense']}:{measure}:{series_label}",
                    "indicator": f"{unit['offense']}:{series_label}", "measure": measure,
                    "place": place, "period": period, "value": value, "unit": unit_label,
                    "flags": [] if value is not None else ["value not published"],
                    "suppressed": value is None, "coverage": coverage, "definition_code": definition["code"],
                })
    if not observations:
        raise CourtsJusticeFormatError("schema_drift", "the response has no offence series")
    fields = {"dataset_key": f"fbi-cde:summarized:{scope}:{code}:{unit['offense']}",
              "classification": definition["classification"], "release": {"label": release, "released_at": _day(release)},
              "definitions": [definition], "observations": observations,
              "coverage_notes": [{"kind": "reporting_coverage", "covers": [code],
                                  "text": "Counts reflect the agencies that reported to the UCR Program for each "
                                          "month; participated_population as published states the population "
                                          "those agencies cover.", "source_url": "https://cde.ucr.cjis.gov/"}],
              "request": {"scope": scope, "code": code, "offense": unit["offense"], "from": unit["from"],
                          "to": unit["to"]}}
    key = f"justice:release:fbi-cde:{scope}:{code}:{unit['offense']}:{unit['from']}:{unit['to']}"
    return [_record("fbi-cde-summarized-json", "statistics-release", key, native_revision=release,
                    title=f"FBI CDE {unit['offense']} {scope} {code} {unit['from']}..{unit['to']}",
                    locator="https://cde.ucr.cjis.gov/", published_at=release, fields=fields)]


# ----------------------------------------------------------------- data.police.uk


def parse_police(responses: Mapping[str, bytes], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    updated = _mapping(_json(responses["last_updated"]), "crime-last-updated")
    release = _day(updated.get("date"))
    if not release:
        raise CourtsJusticeFormatError("schema_drift", "crime-last-updated states no date")
    categories = _json(responses["categories"])
    if not isinstance(categories, list):
        raise CourtsJusticeFormatError("schema_drift", "crime-categories is not a list")
    names = {str(c.get("url")): _clean(c.get("name")) for c in categories if isinstance(c, Mapping)}
    crimes = _json(responses["crimes"])
    if not isinstance(crimes, list):
        raise CourtsJusticeFormatError("schema_drift", "crimes-street is not a list")
    month = str(unit["month"])
    counts: dict[str, int] = {}
    outcomes: dict[tuple[str, str], int] = {}
    for crime in crimes:
        crime = _mapping(crime, "crime")
        if str(crime.get("month") or "") != month:
            raise CourtsJusticeFormatError("schema_drift", "a crime belongs to another month than requested")
        category = str(crime.get("category") or "")
        if not category:
            raise CourtsJusticeFormatError("schema_drift", "a crime states no category")
        counts[category] = counts.get(category, 0) + 1
        status = crime.get("outcome_status")
        outcome = _clean(status.get("category")) if isinstance(status, Mapping) else None
        outcomes[(category, outcome or "no outcome recorded")] = outcomes.get(
            (category, outcome or "no outcome recorded"), 0) + 1
    place = ({"scheme": "police-uk-neighbourhood", "code": f"{unit['force']}/{unit['neighbourhood']}",
              "label": _clean(unit.get("label"))} if unit.get("neighbourhood")
             else {"scheme": "police-uk-polygon", "code": f"{unit['force']}/poly-{_digest(unit.get('poly'))[:12]}",
                   "label": _clean(unit.get("label"))})
    definitions = [{"classification": "police-uk-category", "code": code, "label": names.get(code) or code,
                    "text": f"data.police.uk crime category '{names.get(code) or code}' as the publisher names it",
                    "source_url": "https://data.police.uk/docs/method/crime-categories/", "in_force_from": None,
                    "in_force_to": None} for code in sorted(counts)]
    observations = []
    for code, count in sorted(counts.items()):
        observations.append({"series_key": f"police-uk:{place['code']}:{code}:crimes", "indicator": f"{code}:crimes",
                             "measure": "crimes", "place": place, "period": month, "value": count, "unit": "count",
                             "flags": [], "suppressed": False, "coverage": {"force": unit["force"]},
                             "definition_code": code})
    for (code, outcome), count in sorted(outcomes.items()):
        observations.append({"series_key": f"police-uk:{place['code']}:{code}:outcome:{outcome}",
                             "indicator": f"{code}:outcome", "measure": "outcomes", "outcome_category": outcome,
                             "place": place, "period": month, "value": count, "unit": "count", "flags": [],
                             "suppressed": False, "coverage": {"force": unit["force"]}, "definition_code": code})
    fields = {"dataset_key": f"police-uk:street:{place['code']}", "classification": "police-uk-category",
              "release": {"label": release, "released_at": release}, "definitions": definitions,
              "observations": observations,
              "coverage_notes": [{"kind": "anonymisation", "covers": [place["code"]],
                                  "text": "Crime locations are anonymised by the publisher (snapped to map points); "
                                          "Noesis stores no location, street or persistent crime ID and counts are "
                                          "per declared area only.",
                                  "source_url": "https://data.police.uk/about/#location-anonymisation"}],
              "request": {"force": unit["force"], "neighbourhood": unit.get("neighbourhood"), "month": month,
                          "category": unit.get("category") or "all-crime"}}
    key = f"justice:release:police-uk:{place['code']}:{month}"
    return [_record("police-uk-crimes-json", "statistics-release", key, native_revision=release,
                    title=f"data.police.uk {place['code']} {month}", locator="https://data.police.uk/data/",
                    published_at=release, fields=fields)]


# ----------------------------------------------------------------- Eurostat


class _SectionText(HTMLParser):
    """Text of the elements whose ``id`` is one of the declared ESMS section ids."""

    def __init__(self, ids: set[str]) -> None:
        super().__init__()
        self.ids, self.stack, self.found = ids, [], {}

    def handle_starttag(self, tag, attrs):
        ident = dict(attrs).get("id")
        if self.stack:
            self.stack[-1][2] += 1 if tag not in {"br", "img", "hr", "meta", "link", "input"} else 0
        elif ident in self.ids:
            self.stack.append([ident, tag, 0])
            self.found.setdefault(ident, [])

    def handle_endtag(self, tag):
        if not self.stack:
            return
        if self.stack[-1][2] > 0:
            self.stack[-1][2] -= 1
        else:
            self.stack.pop()

    def handle_data(self, data):
        if self.stack:
            self.found[self.stack[-1][0]].append(data)


def esms_sections(raw: bytes, ids: Sequence[str]) -> dict[str, str]:
    parser = _SectionText(set(ids))
    parser.feed(raw.decode("utf-8", "replace"))
    return {k: " ".join("".join(v).split()) for k, v in parser.found.items() if "".join(v).strip()}


def parse_eurostat(responses: Mapping[str, bytes], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    from src.ingestion.connectors.dataset.base import RawSeries, SeriesRef
    from src.ingestion.connectors.dataset.eurostat import EurostatConnector

    connector = EurostatConnector(http_get=lambda _url: "")
    definitions = {d["code"]: d for d in (_definition(item, classification="ICCS")
                                          for item in unit.get("definitions") or [])}
    observations, updated = [], set()
    for geo in unit["geo"]:
        raw = responses[f"cube:{geo}"]
        try:
            cube = connector.parse_cells(RawSeries(ref=SeriesRef(locator=f"{unit['dataset']}/{geo}", metadata={}),
                                                   content=raw.decode("utf-8"), content_type="application/json",
                                                   source_url=f"eurostat:{unit['dataset']}:{geo}", fetched_at=0),
                                         max_cells=5000)
        except (ValueError, KeyError, UnicodeDecodeError) as exc:
            raise CourtsJusticeFormatError("schema_drift", f"JSON-stat cube could not be read: {exc}") from exc
        if not cube.get("updated"):
            raise CourtsJusticeFormatError("schema_drift", "the cube states no updated stamp (vintage)")
        updated.add(str(cube["updated"]))
        labels = cube["status_labels"]
        for cell in cube["cells"]:
            dims = dict(cell["dimensions"])
            if str(dims.get("geo")) != geo:
                raise CourtsJusticeFormatError("schema_drift", "the cube's geography is not the requested one")
            iccs = str(dims.get("iccs") or "")
            unit_code = str(dims.get("unit") or "")
            flag = cell["status"]
            observations.append({
                "series_key": f"eurostat:{unit['dataset']}:{geo}:{iccs}:{unit_code}",
                "indicator": f"{unit['dataset']}:{iccs}", "measure": unit_code, "place": {
                    "scheme": "eurostat-geo", "code": geo,
                    "label": _clean(cube["dimensions"].get("geo", {}).get("categories", {}).get(geo))},
                "period": str(cell["time"]), "value": cell["value"],
                "unit": _clean(cube["dimensions"].get("unit", {}).get("categories", {}).get(unit_code)) or unit_code,
                "flags": [{"code": flag, "label": labels.get(flag)}] if flag else [],
                "suppressed": cell["value"] is None, "coverage": {"dimensions": dims},
                "definition_code": iccs,
            })
        for annotation in json.loads(raw.decode("utf-8")).get("extension", {}).get("annotation") or []:
            if isinstance(annotation, Mapping) and _clean(annotation.get("title")):
                definitions.setdefault("__footnotes__", {"footnotes": []})["footnotes"].append(
                    {"geo": geo, "type": _clean(annotation.get("type")), "text": _verbatim(annotation.get("title"))})
    if len(updated) != 1:
        raise CourtsJusticeFormatError("schema_drift", "the declared countries' cubes state different updated stamps")
    release = next(iter(updated))
    footnotes = definitions.pop("__footnotes__", {"footnotes": []})["footnotes"]
    notes = [{"kind": "national_definition_footnote", "covers": [f["geo"]], "text": f["text"],
              "source_url": "https://ec.europa.eu/eurostat/databrowser/view/" + unit["dataset"]} for f in footnotes]
    esms = dict(unit.get("esms") or {})
    if esms:
        sections = esms_sections(responses["esms"], list(esms.get("sections") or {}))
        for section_id, covers in sorted(dict(esms.get("sections") or {}).items()):
            if section_id not in sections:
                raise CourtsJusticeFormatError("schema_drift", f"the ESMS page has no section {section_id!r}")
            notes.append({"kind": "esms_comparability", "covers": list(covers or unit["geo"]),
                          "text": sections[section_id], "section": section_id,
                          "source_url": "https://ec.europa.eu/eurostat" + esms["path"],
                          "content_sha256": hashlib.sha256(responses["esms"]).hexdigest()})
    missing = sorted({o["definition_code"] for o in observations} - set(definitions))
    if missing:
        raise CourtsJusticeFormatError("schema_drift", f"no declared ICCS definition for {', '.join(missing)}")
    fields = {"dataset_key": f"eurostat:{unit['dataset']}", "classification": "ICCS",
              "release": {"label": release, "released_at": _day(release)},
              "definitions": [definitions[k] for k in sorted(definitions)], "observations": observations,
              "coverage_notes": notes,
              "request": {"dataset": unit["dataset"], "geo": list(unit["geo"]), "filters": dict(unit.get("filters") or {})}}
    key = f"justice:release:eurostat:{unit['dataset']}:{'-'.join(unit['geo'])}"
    return [_record("eurostat-crime-jsonstat", "statistics-release", key, native_revision=release,
                    title=f"Eurostat {unit['dataset']} {', '.join(unit['geo'])}",
                    locator="https://ec.europa.eu/eurostat/databrowser/view/" + unit["dataset"],
                    published_at=release, fields=fields)]


# ----------------------------------------------------------------- selection and requests


def _units(fmt: str, selection: Mapping[str, Any]) -> list[dict[str, Any]]:
    key = FORMATS[fmt]["unit"]
    units = [dict(u) if isinstance(u, Mapping) else {"id": u} for u in selection.get(key) or []]
    if not 1 <= len(units) <= MAX_UNITS:
        raise SourcePackError("invalid_manifest", f"a courts-justice selection names 1-{MAX_UNITS} {key}")
    for unit in units:
        if fmt.startswith("courtlistener") and not str(unit.get("id") or "").isdigit():
            raise SourcePackError("invalid_manifest", "CourtListener units name a numeric id")
        if fmt == "fbi-cde-summarized-json":
            if unit.get("scope") not in {"state", "agency"} or not re.fullmatch(
                    r"[A-Z]{2}" if unit.get("scope") == "state" else r"[A-Z]{2}[A-Z0-9]{7}", str(unit.get("code"))):
                raise SourcePackError("invalid_manifest", "FBI CDE units name scope state (postal code) or agency "
                                                          "(ORI)")
            if not re.fullmatch(r"[a-z-]{2,40}", str(unit.get("offense") or "")) or not all(
                    re.fullmatch(r"\d{2}-\d{4}", str(unit.get(k) or "")) for k in ("from", "to")):
                raise SourcePackError("invalid_manifest", "FBI CDE units name an offence and a MM-YYYY window")
            _definition(unit.get("definition"), classification="UCR-SRS")
        if fmt == "police-uk-crimes-json":
            if not re.fullmatch(r"[a-z0-9-]{2,60}", str(unit.get("force") or "")) \
                    or not re.fullmatch(r"\d{4}-\d{2}", str(unit.get("month") or "")):
                raise SourcePackError("invalid_manifest", "police-uk units name a force and a YYYY-MM month")
            if not unit.get("poly") or len(str(unit["poly"]).split(":")) > 100:
                raise SourcePackError("invalid_manifest", "police-uk units declare a bounded polygon (poly)")
        if fmt == "eurostat-crime-jsonstat":
            geos = list(unit.get("geo") or [])
            if not re.fullmatch(r"crim_[a-z0-9_]{2,30}", str(unit.get("dataset") or "")) \
                    or not 1 <= len(geos) <= 10 or any(not re.fullmatch(r"[A-Z0-9]{2,5}", str(g)) for g in geos):
                raise SourcePackError("invalid_manifest", "Eurostat units name a crim_* dataset and 1-10 GEO codes")
    return units


def requests_for(fmt: str, unit: Mapping[str, Any]) -> dict[str, tuple[str, dict[str, Any]]]:
    """Named request paths (relative to the endpoint) and parameters for one selection unit."""
    if fmt == "courtlistener-docket-json":
        docket = int(unit["id"])
        return {"docket": (f"/dockets/{docket}/", {}),
                "entries": ("/docket-entries/", {"docket": docket, "page_size": PAGE_SIZE}),
                "parties": ("/parties/", {"docket": docket, "page_size": PAGE_SIZE})}
    if fmt == "courtlistener-cluster-json":
        cluster = int(unit["id"])
        return {"cluster": (f"/clusters/{cluster}/", {}),
                "opinions": ("/opinions/", {"cluster": cluster, "page_size": PAGE_SIZE})}
    if fmt == "fbi-cde-summarized-json":
        return {"summarized": (f"/summarized/{unit['scope']}/{unit['code']}/{unit['offense']}",
                               {"from": unit["from"], "to": unit["to"]})}
    if fmt == "police-uk-crimes-json":
        return {"last_updated": ("/crime-last-updated", {}),
                "categories": ("/crime-categories", {"date": unit["month"]}),
                "crimes": (f"/crimes-street/{unit.get('category') or 'all-crime'}",
                           {"date": unit["month"], "poly": unit["poly"]})}
    if fmt == "eurostat-crime-jsonstat":
        out = {}
        for geo in unit["geo"]:
            params: dict[str, Any] = {"format": "JSON", "geo": geo}
            params.update({k: v for k, v in dict(unit.get("filters") or {}).items()})
            out[f"cube:{geo}"] = (f"/api/dissemination/statistics/1.0/data/{unit['dataset']}", params)
        esms = dict(unit.get("esms") or {})
        if esms:
            if not re.fullmatch(r"/cache/metadata/en/[a-z0-9_]+\.htm", str(esms.get("path") or "")):
                raise SourcePackError("invalid_manifest", "an ESMS path is /cache/metadata/en/<name>.htm")
            out["esms"] = (esms["path"], {})
        return out
    raise SourcePackError("invalid_manifest", f"unknown courts-justice format {fmt!r}")


_PARSERS: dict[str, Callable[[Mapping[str, bytes], Mapping[str, Any]], list[dict[str, Any]]]] = {
    "courtlistener-docket-json": parse_docket,
    "courtlistener-cluster-json": parse_cluster,
    "fbi-cde-summarized-json": parse_fbi,
    "police-uk-crimes-json": parse_police,
    "eurostat-crime-jsonstat": parse_eurostat,
}


def parse_unit(fmt: str, responses: Mapping[str, bytes], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    if fmt not in _PARSERS:
        raise CourtsJusticeFormatError("schema_drift", f"unknown courts-justice format {fmt!r}")
    return _PARSERS[fmt](responses, unit)


def courts_justice_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    declared = dict(source.get("courts_justice") or {})
    fmt = declared.get("format")
    if fmt not in FORMATS or FORMATS[fmt]["provider"] != declared.get("provider"):
        raise SourcePackError("invalid_manifest", "courts-justice sources declare a matching provider and format")
    if declared.get("live_verification") not in {"unverified-live", "verified-live"}:
        raise SourcePackError("invalid_manifest", "courts-justice sources state their LIVE_VERIFICATION status")
    _units(fmt, dict(declared.get("selection") or {}))
    if FORMATS[fmt]["keyed"] != (dict(source.get("auth") or {}).get("kind") == "required-secret"):
        raise SourcePackError("invalid_manifest", "keyed courts-justice formats declare a required secret")
    return declared


class CourtsJusticeAdapter:
    """Fetch one declared selection unit per page from the source's endpoint host and emit its records."""

    accepts_transport = True
    connector = CONNECTOR

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None) -> None:
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        self.source = json.loads(json.dumps(source))
        self.declared = courts_justice_declaration(self.source)
        self.format = self.declared["format"]
        self.provider = self.declared["provider"]
        self.units = _units(self.format, dict(self.declared.get("selection") or {}))
        self.secret = secret
        if transport is None:
            from functools import partial

            transport = partial(HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"]))
        self.transport = transport
        self.definition = {
            "contract": ADAPTER_CONTRACT, "source_id": source["source_id"], "connector": source["connector"],
            "endpoint": source["endpoint"], "operations": list(source["operations"]),
            "source_hash": source["source_hash"], "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"], "limits": source["budgets"],
            "courts_justice": {"provider": self.provider, "format": self.format, "units": len(self.units),
                               "keyed": bool(FORMATS[self.format]["keyed"])},
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _check(self, request: Mapping[str, Any]) -> None:
        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError("operation_forbidden", "operation is not declared by the source")
        if set(request) - {"operation", "parameters", "limit", "from_ms", "to_ms"}:
            raise SourcePackError("parameter_forbidden", "runtime adapter received undeclared controls")
        if dict(request.get("parameters") or {}):
            raise SourcePackError("parameter_forbidden", "courts-justice runs fetch the declared selection only")

    def _get(self, path: str, params: Mapping[str, Any]) -> tuple[bytes, dict[str, Any]]:
        from src.ingestion.source_pack_runtime import _retry_after_ms

        endpoint = self.source["endpoint"].rstrip("/")
        url = endpoint + path
        host = (urlsplit(endpoint).hostname or "").casefold()
        headers = {"Accept": "application/json, text/html"}
        if FORMATS[self.format]["keyed"]:
            if not self.secret:
                raise SourcePackError("authentication_failed", "this courts-justice source needs its API secret")
            if self.provider == "courtlistener":
                headers["Authorization"] = f"Token {self.secret}"
            else:
                headers["X-Api-Key"] = self.secret
        ordered = dict(sorted(params.items()))
        response = self.transport(url=url, params=ordered, headers=headers,
                                  timeout=int(self.definition["limits"]["timeout_ms"]) / 1000)
        final_host = (urlsplit(str(response.get("final_url") or url)).hostname or "").casefold()
        if final_host != host:
            raise SourcePackError("network_policy", "courts-justice response was served from another host")
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
        except CourtsJusticeFormatError as exc:
            raise SourcePackError("budget_exhausted" if exc.code == "input_limit" else "schema_drift",
                                  f"{exc.code}: {exc}") from exc
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        if len(records) > limit:
            raise SourcePackError("budget_exhausted", "unit has more records than the run's result budget")
        origin = "fixture" if all(r["origin"] == "fixture" for r in requests) else "live"
        receipt = {
            "contract": "noesis-court-justice-acquisition-receipt-v1", "source_id": self.source["source_id"],
            "provider": self.provider, "format": self.format, "unit_index": index, "unit": unit,
            "requests": requests, "records": len(records), "evidence_origin": origin,
            "live_verification": self.declared["live_verification"], "final_page": index + 1 >= len(self.units),
        }
        out = []
        for record in records:
            record = {**record, "evidence_origin": origin}
            out.append({"id": record["record_key"], "title": record["title"], "url": record["locator"],
                        "language": "en", "published_at": record["published_at"],
                        "updated_at": record["native_revision"],
                        "content": json.dumps(record, sort_keys=True, ensure_ascii=False),
                        "court_justice_record": record, "court_justice_receipt": receipt})
        next_cursor = None if receipt["final_page"] else str(index + 1)
        return RuntimePage(tuple(out), next_cursor, sum(r["bytes"] for r in requests), receipt=receipt)


FIXTURE_SECRET = "fixture-credential-not-a-real-key"
ADAPTERS = {CONNECTOR: CourtsJusticeAdapter}


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
    adapter = CourtsJusticeAdapter(source, transport=fixture_transport(list(fixture["native_pages"])),
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
    "ADAPTERS", "BOUNDED_COVERAGE", "CONNECTOR", "FIXTURE_SECRET", "FORMATS", "LIVE_VERIFICATION", "MINIMISATION",
    "PROVIDER_CONTRACTS", "RECORD_CONTRACT", "REVIEW_BOUNDARY", "CourtsJusticeAdapter", "CourtsJusticeFormatError",
    "citation_text", "courts_justice_declaration", "esms_sections", "fixture_transport", "parse_unit", "party_type",
    "replay_native_fixture", "requests_for",
]
