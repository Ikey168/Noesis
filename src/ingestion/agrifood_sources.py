"""Agriculture and food statistics sources for the Agriculture and Food Systems pack (#2213, AF01 and AF03-AF06).

Five providers run as sources of the ``agrifood`` source pack
(``config/source_packs/agrifood.json``, connector ``agrifood``) through
:mod:`src.ingestion.source_pack_runtime` - licence acceptance, budgets,
receipts, checkpoints and the runtime's same-host HTTPS transport - each under
a recorded access contract (:data:`PROVIDER_CONTRACTS`):

* **FAOSTAT** (``faostat``, #2338) - QCL production, area and yield, PP
  producer prices and FBS food balances, keyed by domain, area code, item code,
  element code and year, with the FAOSTAT flag and note verbatim and the
  domain's update date as the release;
* **USDA NASS Quick Stats** (``nass-quickstats``, #2342) - production, yield,
  area and price series keyed by ``short_desc``, geography, reference period
  and load time, with survey vs census and the ``(D)`` / ``(Z)`` / ``(NA)``
  suppression codes kept as text (never numbers); the key is the
  ``NOESIS_NASS_API_KEY`` secret reference and never appears in a URL we store;
* **USDA FAS PSD** (``fas-psd``, #2345) - supply and distribution balances per
  marketing year, each monthly release a vintage; current-year figures are
  labelled USDA projections or estimates, never observations;
* **Eurostat agriculture** (``eurostat-agri``, #2350) - crop production and
  agricultural price datasets through the existing SDMX connector
  (:meth:`src.ingestion.connectors.dataset.sdmx.SDMXConnector.csv_url` /
  ``parse_csv``) with the ``OBS_FLAG`` letters and the dataset's LAST UPDATE;
* **EU Agri-food data portal** (``agri-food-portal``, #2350) - weekly market
  prices keyed by product, member state, market and period.

Every selection is explicit (the bounded coverage of
``docs/roadmaps/agrifood-source-audit.md``): one page per selection, never an
enumeration. The pack computes no forecast, projection or food-security
indicator. Every provider is ``unverified-live`` until a dated live run
(#2370); request paths and field names marked *verify* come from public
documentation and must be checked before a live run.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import urlencode, urlsplit

from src.ingestion.source_packs import SourcePackError
from src.kb.agrifood_records import AgrifoodError, decimal_text, declaration, figure, flag

CONNECTOR = "agrifood"
ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
PROVIDERS = ("faostat", "nass-quickstats", "fas-psd", "eurostat-agri", "agri-food-portal")
PROVIDER_HOSTS = {
    "faostat": ("faostatservices.fao.org",),
    "nass-quickstats": ("quickstats.nass.usda.gov",),
    "fas-psd": ("apps.fas.usda.gov",),
    "eurostat-agri": ("ec.europa.eu",),
    "agri-food-portal": ("ec.europa.eu",),
}
SECRET_REFS = {"nass-quickstats": "NOESIS_NASS_API_KEY", "fas-psd": "NOESIS_FAS_API_KEY"}
PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "faostat": {
        "publisher": "Food and Agriculture Organization of the United Nations (FAOSTAT)",
        "access": "FAOSTAT API (faostatservices.fao.org/api/v1), HTTPS GET per domain with area, item, element and "
                  "year filters; bulk CSV downloads (bulks-faostat.fao.org) as the documented fallback",
        "endpoints": ["/en/data/{domain}?area=&item=&element=&year=&show_flags=true&show_notes=true&"
                      "output_type=objects (verify)", "/en/groupsanddomains (domain date_update; verify)"],
        "authentication": "none for the documented public endpoints; FAOSTAT has announced token-based API access "
                          "(verify before a live run; a token would be held as a NOESIS_ secret reference)",
        "licence": "CC BY 4.0 for FAOSTAT statistical data (verify the current FAO terms of use)",
        "terms_url": "https://www.fao.org/contact-us/terms/db-terms-of-use/en/",
        "attribution": "Source: FAO. FAOSTAT. https://www.fao.org/faostat/ (accessed on the retrieval date)",
        "rate_limits": "none published; one selection per page within the source budget",
        "flags": "faostat-flags: A official, B time series break, E estimated, I imputed, M missing (cannot "
                 "exist), O missing, P provisional, T unofficial, X international organisation; the Flag and "
                 "Flag Description and the Note are stored verbatim",
        "revision_behaviour": "a domain is updated as a whole (groupsanddomains date_update); each update is a new "
                              "vintage of every series it carries and earlier vintages stay queryable",
        "access_decision": "unverified-live",
        "fields": ["Domain Code", "Area Code", "Area", "Item Code", "Item Code (CPC)", "Item", "Element Code",
                   "Element", "Year", "Unit", "Value", "Flag", "Flag Description", "Note"],
    },
    "nass-quickstats": {
        "publisher": "U.S. Department of Agriculture, National Agricultural Statistics Service (Quick Stats)",
        "access": "Quick Stats API api_GET, HTTPS GET with explicit short_desc, geography and year filters",
        "endpoints": ["/api/api_GET/?key=&short_desc=&agg_level_desc=&state_fips_code=&county_code=&year__GE=&"
                      "year__LE=&format=JSON"],
        "authentication": "API key requested from NASS, sent as the key query parameter and held only as the "
                          "NOESIS_NASS_API_KEY secret reference; stored URLs and receipts never carry it",
        "licence": "U.S. Government work, public domain; cite NASS as the source",
        "terms_url": "https://quickstats.nass.usda.gov/api",
        "attribution": "Source: USDA National Agricultural Statistics Service, Quick Stats "
                       "(https://quickstats.nass.usda.gov/)",
        "rate_limits": "at most 50,000 records per request; the pack keeps one short_desc and geography per page",
        "flags": "nass-value-codes: (D) withheld to avoid disclosing individual operations, (Z) less than half "
                 "the rounding unit, (NA) not available, (X) not applicable, (S) insufficient reports, (H)/(L) "
                 "coefficient of variation; kept as the value text, never replaced with a number",
        "revision_behaviour": "estimates are revised in place (annual summaries, census benchmarking); load_time "
                              "is the vintage, so a revised figure is a new vintage beside the prior one; "
                              "forecast reference periods (e.g. YEAR - AUG FORECAST) are NASS's forecasts",
        "access_decision": "unverified-live",
        "fields": ["source_desc", "commodity_desc", "short_desc", "statisticcat_desc", "unit_desc",
                   "agg_level_desc", "state_fips_code", "state_name", "county_code", "county_name", "year",
                   "freq_desc", "reference_period_desc", "load_time", "Value", "CV (%)"],
    },
    "fas-psd": {
        "publisher": "U.S. Department of Agriculture, Foreign Agricultural Service (PSD Online)",
        "access": "PSD Online OpenData API, HTTPS GET per commodity, country and market year",
        "endpoints": ["/OpenData/api/psd/commodity/{commodity}/country/{country}/year/{market_year} (verify)",
                      "/OpenData/api/psd/commodityAttributes (verify)", "/OpenData/api/psd/unitsOfMeasure (verify)"],
        "authentication": "API key sent as the API_KEY header (verify), held only as the NOESIS_FAS_API_KEY secret "
                          "reference",
        "licence": "U.S. Government work, public domain; cite USDA FAS PSD Online",
        "terms_url": "https://apps.fas.usda.gov/psdonline/app/index.html#/app/about",
        "attribution": "Source: USDA Foreign Agricultural Service, Production, Supply and Distribution (PSD) "
                       "Online (https://apps.fas.usda.gov/psdonline/)",
        "rate_limits": "none published (verify); one commodity, country and market-year set per page",
        "flags": "psd-release-convention: PSD carries no per-figure flag; following the WASDE convention the "
                 "newest marketing year of a release is a USDA projection and the one before it a USDA estimate "
                 "(verify against WASDE 'Proj.'/'Est.' labels); both are labelled as USDA's, never as observations",
        "revision_behaviour": "monthly releases aligned with WASDE; the release month (calendarYear, month) is the "
                              "vintage, and every monthly release is stored beside the earlier ones",
        "access_decision": "unverified-live",
        "fields": ["commodityCode", "countryCode", "marketYear", "calendarYear", "month", "attributeId", "unitId",
                   "value"],
    },
    "eurostat-agri": {
        "publisher": "Eurostat",
        "access": "Eurostat SDMX 2.1 dissemination API in SDMX-CSV through the existing SDMX connector "
                  "(src/ingestion/connectors/dataset/sdmx.py)",
        "endpoints": ["/eurostat/api/dissemination/sdmx/2.1/data/{dataset}/{key}?format=SDMX-CSV&startPeriod=&"
                      "endPeriod="],
        "authentication": "none",
        "licence": "Eurostat copyright notice: reuse authorised with acknowledgement (CC BY 4.0 equivalent; "
                   "verify)",
        "terms_url": "https://ec.europa.eu/eurostat/about-us/policies/copyright",
        "attribution": "Source: Eurostat (https://ec.europa.eu/eurostat)",
        "rate_limits": "none published; one dataset key per page",
        "flags": "eurostat-obs-flags: b break, c confidential, d definition differs, e estimated, f forecast, n "
                 "not significant, p provisional, r revised, s Eurostat estimate, u low reliability, z not "
                 "applicable; ':' is a missing value; combined letters are kept as published",
        "revision_behaviour": "a dataset is republished as a whole; its LAST UPDATE stamp is the vintage",
        "access_decision": "unverified-live",
        "fields": ["DATAFLOW", "LAST UPDATE", "dimensions", "TIME_PERIOD", "OBS_VALUE", "OBS_FLAG"],
    },
    "agri-food-portal": {
        "publisher": "European Commission, DG AGRI (Agri-food data portal)",
        "access": "Agri-food data portal REST API (ec.europa.eu/agrifood/api), HTTPS GET per product and member "
                  "state (verify paths and parameters)",
        "endpoints": ["/agrifood/api/cereal/prices?memberStateCodes=&productCodes=&beginDate=&endDate= (verify)"],
        "authentication": "none",
        "licence": "European Commission reuse policy (Decision 2011/833/EU): reuse with acknowledgement (verify)",
        "terms_url": "https://commission.europa.eu/legal-notice_en",
        "attribution": "Source: European Commission, Agri-food data portal (https://agridata.ec.europa.eu/)",
        "rate_limits": "none published; one product and member state per page",
        "flags": "agri-food-portal: no per-figure flags; prices are quoted as published with their unit and stage",
        "revision_behaviour": "no release stamp is published; a changed answer is a new vintage dated by its first "
                              "retrieval",
        "access_decision": "unverified-live",
        "fields": ["memberStateCode", "memberStateName", "beginDate", "endDate", "weekNumber", "price", "unit",
                   "productName", "marketName", "stageName"],
    },
}
LIVE_VERIFICATION = {
    provider: {"status": "unverified-live", "note": "no dated live run from this runtime; offline fixtures only (#2370)"}
    for provider in PROVIDERS
}
EXCLUSIONS = ("yield, production or price forecasting by the pack", "food-security scoring beyond quoting the "
              "publisher", "blending or summing across sources", "marketing-year to calendar-year conversion",
              "imputation of withheld or missing values")
_FAO_MEASURES = {"5510": "production", "5312": "area_harvested", "5412": "yield", "5419": "yield",
                 "5532": "producer_price", "5530": "producer_price", "5531": "producer_price"}
_NASS_MEASURES = {"PRODUCTION": "production", "YIELD": "yield", "AREA HARVESTED": "area_harvested",
                  "AREA PLANTED": "area_planted", "PRICE RECEIVED": "producer_price"}
_NASS_CODES = {"(D)": "withheld", "(S)": "withheld", "(NA)": "missing", "(X)": "not-applicable",
               "(Z)": "below-rounding"}
_DMY = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")


class AgrifoodFormatError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _iso_dmy(value: Any) -> str | None:
    match = _DMY.fullmatch(str(value or "").strip())
    return f"{match.group(3)}-{match.group(2)}-{match.group(1)}" if match else None


def _source(provider: str, url: str, locator: str, origin: str) -> dict[str, Any]:
    return {"url": url, "locator": locator, "attribution": PROVIDER_CONTRACTS[provider]["attribution"],
            "evidence_origin": origin}


def _commodity(scheme: str, code: Any, label: Any = None) -> dict[str, Any]:
    return {"scheme": scheme, "code": str(code), "label": _text(label)}


def _place(scheme: str, code: Any, label: Any = None, level: str | None = None) -> dict[str, Any]:
    return {"scheme": scheme, "code": str(code), "label": _text(label), "level": level}


# ------------------------------------------------------------------ selections


def selection_entries(source: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    declared = dict(source.get("agrifood") or {})
    provider = str(declared.get("provider") or "")
    if provider not in PROVIDERS:
        raise SourcePackError("invalid_manifest", f"agrifood sources declare a provider in {PROVIDERS}")
    host = (urlsplit(str(source.get("endpoint") or "")).hostname or "").casefold()
    if host not in PROVIDER_HOSTS[provider]:
        raise SourcePackError("invalid_manifest", f"{provider} is fetched from {PROVIDER_HOSTS[provider][0]} only")
    entries = [dict(e) for e in declared.get("selection") or []]
    if not 1 <= len(entries) <= int(dict(source.get("budgets") or {}).get("max_pages", 1)):
        raise SourcePackError("invalid_manifest", "an agrifood source selects 1..max_pages series sets explicitly")
    for entry in entries:
        years = [int(y) for y in entry.get("years") or entry.get("market_years") or []]
        if provider in {"faostat", "nass-quickstats", "fas-psd"} and not 1 <= len(years) <= 10:
            raise SourcePackError("invalid_manifest", "a selection names 1..10 explicit years")
        if provider == "faostat" and not (entry.get("domain") in {"QCL", "PP", "FBS"} and entry.get("area")
                                          and entry.get("item") and entry.get("elements")):
            raise SourcePackError("invalid_manifest", "FAOSTAT selections name a domain (QCL, PP, FBS), an area, "
                                                      "an item and elements")
        if provider == "nass-quickstats" and not (entry.get("short_desc") and entry.get("agg_level_desc") in
                                                  {"NATIONAL", "STATE", "COUNTY"}):
            raise SourcePackError("invalid_manifest", "NASS selections name a short_desc and a NATIONAL, STATE or "
                                                      "COUNTY geography")
        if provider == "fas-psd" and not (re.fullmatch(r"\d{7}", str(entry.get("commodity") or ""))
                                          and entry.get("country") and entry.get("attributes")):
            raise SourcePackError("invalid_manifest", "PSD selections name a commodity code, a country and attributes")
        if provider == "eurostat-agri" and not (entry.get("dataset") and entry.get("key")
                                                and entry.get("commodity_dimension")):
            raise SourcePackError("invalid_manifest", "Eurostat selections name a dataset, a series key and the "
                                                      "commodity dimension")
        if provider == "agri-food-portal" and not (entry.get("sector") and entry.get("product")
                                                   and entry.get("member_state")):
            raise SourcePackError("invalid_manifest", "portal selections name a sector, a product and a member state")
    return provider, entries


def requests_for(provider: str, endpoint: str, entry: Mapping[str, Any]) -> list[tuple[str, str, dict[str, str]]]:
    """(role, absolute URL, query) for one selection; credentials are added by the adapter, never here."""
    base = endpoint.rstrip("/")
    if provider == "faostat":
        years = ",".join(str(y) for y in entry["years"])
        return [("domains", f"{base}/en/groupsanddomains", {}),
                ("data", f"{base}/en/data/{entry['domain']}",
                 {"area": str(entry["area"]), "item": str(entry["item"]), "element": ",".join(entry["elements"]),
                  "year": years, "show_flags": "true", "show_notes": "true", "output_type": "objects"})]
    if provider == "nass-quickstats":
        years = [int(y) for y in entry["years"]]
        query = {"short_desc": str(entry["short_desc"]), "agg_level_desc": str(entry["agg_level_desc"]),
                 "year__GE": str(min(years)), "year__LE": str(max(years)), "format": "JSON"}
        for field in ("state_fips_code", "county_code"):
            if entry.get(field):
                query[field] = str(entry[field])
        return [("data", f"{base}/api/api_GET/", query)]
    if provider == "fas-psd":
        out = [("attributes", f"{base}/api/psd/commodityAttributes", {}),
               ("units", f"{base}/api/psd/unitsOfMeasure", {})]
        for year in entry["market_years"]:
            out.append((f"data:{year}", f"{base}/api/psd/commodity/{entry['commodity']}/country/{entry['country']}/"
                                        f"year/{year}", {}))
        return out
    if provider == "eurostat-agri":
        from src.ingestion.connectors.dataset.sdmx import SDMXConnector

        params = {k: str(entry[k]) for k in ("startPeriod", "endPeriod") if entry.get(k)}
        try:
            url, query = SDMXConnector("ESTAT").csv_url(str(entry["dataset"]), str(entry["key"]), params)
        except ValueError as exc:
            raise SourcePackError("invalid_manifest", str(exc)) from exc
        if not url.startswith(base + "/"):
            raise SourcePackError("invalid_manifest", "the Eurostat source endpoint is the SDMX 2.1 data base URL")
        return [("data", url, query)]
    return [("prices", f"{base}/{entry['sector']}/prices",
             {"memberStateCodes": str(entry["member_state"]), "productCodes": str(entry["product"]),
              **{k: str(entry[k]) for k in ("beginDate", "endDate") if entry.get(k)}})]


# ------------------------------------------------------------------ parsers


def parse_faostat(responses: Mapping[str, Any], urls: Mapping[str, str], entry: Mapping[str, Any], *,
                  origin: str) -> list[dict[str, Any]]:
    domain = str(entry["domain"])
    updates = [d for d in (responses.get("domains") or {}).get("data") or []
               if str(d.get("domain_code") or "") == domain]
    if len(updates) != 1 or not _text(updates[0].get("date_update")):
        raise AgrifoodFormatError("schema_drift", f"FAOSTAT domain list does not state one update date for {domain}")
    updated = str(updates[0]["date_update"])[:10]
    release = {"key": f"faostat:{domain}:{updated}", "released_at": updated,
               "basis": "FAOSTAT domain update date (groupsanddomains date_update)"}
    out = [declaration("release", "faostat", domain, release=release,
                       source=_source("faostat", urls["domains"], "/data", origin),
                       as_published={"domain_code": domain, "domain_name": updates[0].get("domain_name"),
                                     "date_update": updates[0].get("date_update")})]
    rows = (responses.get("data") or {}).get("data")
    if not isinstance(rows, list):
        raise AgrifoodFormatError("schema_drift", "FAOSTAT data answer has no data list")
    items = {}
    for index, row in enumerate(rows):
        item_code, element_code = _text(row.get("Item Code")), _text(row.get("Element Code"))
        area_code, year = _text(row.get("Area Code")), _text(row.get("Year"))
        if not (item_code and element_code and area_code and year):
            raise AgrifoodFormatError("schema_drift", "a FAOSTAT row lacks its area, item, element or year code")
        commodity = _commodity("faostat-item", item_code, row.get("Item"))
        items[item_code] = commodity
        number = decimal_text(row.get("Value"))
        flag_code = _text(row.get("Flag"))
        status = "reported" if number is not None else "not-applicable" if flag_code == "M" else "missing"
        measure = {"kind": "food_balance" if domain == "FBS" else _FAO_MEASURES.get(element_code, "other"),
                   "element": str(row.get("Element") or element_code), "element_code": element_code,
                   "label": str(row.get("Element") or element_code)}
        out.append(figure(
            "food_balance" if domain == "FBS" else "observation", "faostat", domain, commodity=commodity,
            place=_place("fao-area", area_code, row.get("Area"), "country"), measure=measure, unit=row.get("Unit"),
            period={"type": "calendar-year", "value": year, "key": year, "reference": None,
                    "start": f"{year}-01-01", "end": f"{year}-12-31", "definition": None},
            value={"text": None if row.get("Value") in (None, "") else str(row.get("Value")), "number": number,
                   "status": status},
            flag_value=flag("faostat-flags", flag_code, label=row.get("Flag Description"), note=row.get("Note")),
            estimate_type="observation", release=release,
            as_published={k: row[k] for k in sorted(row) if k in PROVIDER_CONTRACTS["faostat"]["fields"]},
            source=_source("faostat", urls["data"], f"/data/{index}", origin)))
    for commodity in items.values():
        out.append(declaration("commodity", "faostat", domain, commodity=commodity,
                               source=_source("faostat", urls["data"], "/data", origin)))
    return out


def _nass_place(row: Mapping[str, Any]) -> dict[str, Any]:
    level = str(row.get("agg_level_desc") or "").upper()
    if level == "NATIONAL":
        return _place("iso3166-1", "US", "United States", "national")
    state = _text(row.get("state_fips_code"))
    if not state:
        raise AgrifoodFormatError("schema_drift", "a NASS state or county row lacks its state FIPS code")
    if level == "STATE":
        return _place("us-fips", state.zfill(2), _text(row.get("state_name")), "state")
    county = _text(row.get("county_code"))
    if level != "COUNTY" or not county:
        raise AgrifoodFormatError("schema_drift", f"unsupported NASS geography {level!r}")
    return _place("us-fips", state.zfill(2) + county.zfill(3),
                  ", ".join(x for x in (_text(row.get("county_name")), _text(row.get("state_name"))) if x), "county")


def parse_nass(responses: Mapping[str, Any], urls: Mapping[str, str], entry: Mapping[str, Any], *,
               origin: str) -> list[dict[str, Any]]:
    rows = (responses.get("data") or {}).get("data")
    if not isinstance(rows, list):
        raise AgrifoodFormatError("schema_drift", "Quick Stats answer has no data list")
    out, commodities = [], {}
    for index, row in enumerate(rows):
        if str(row.get("short_desc") or "") != str(entry["short_desc"]):
            raise AgrifoodFormatError("schema_drift", "Quick Stats returned another short_desc than selected")
        year, load_time = _text(row.get("year")), _text(row.get("load_time"))
        if not (year and load_time):
            raise AgrifoodFormatError("schema_drift", "a Quick Stats row lacks its year or load_time")
        reference = str(row.get("reference_period_desc") or "YEAR").strip().upper()
        forecast = "FORECAST" in reference
        period_type = "marketing-year" if reference == "MARKETING YEAR" else "calendar-year"
        key = year if reference in {"YEAR", "MARKETING YEAR"} else f"{year} {reference}"
        text = str(row.get("Value") or "").strip()
        code = text if text in _NASS_CODES or text in {"(H)", "(L)"} else None
        number = None if text in _NASS_CODES else decimal_text(text)
        status = _NASS_CODES.get(text, "reported" if number is not None else "missing")
        commodity = _commodity("nass-commodity", row.get("commodity_desc"), row.get("commodity_desc"))
        commodities[commodity["code"]] = commodity
        statcat = str(row.get("statisticcat_desc") or "").upper()
        loaded = load_time[:19]
        out.append(figure(
            "observation", "nass-quickstats", "quickstats", commodity=commodity, place=_nass_place(row),
            program=_text(row.get("source_desc")),
            measure={"kind": _NASS_MEASURES.get(statcat, "other"), "element": str(row["short_desc"]),
                     "element_code": None, "label": str(row["short_desc"])},
            unit=row.get("unit_desc"),
            period={"type": period_type, "value": year, "key": key, "reference": reference, "start": None,
                    "end": None, "definition": "NASS marketing year as published; never converted"
                    if period_type == "marketing-year" else None},
            value={"text": text or None, "number": number, "status": status},
            flag_value=flag("nass-value-codes", code, note=_text(row.get("CV (%)")) and f"CV (%) {row['CV (%)']}"),
            estimate_type="publisher-forecast" if forecast else "observation",
            release={"key": f"nass:load_time:{loaded}", "released_at": loaded,
                     "basis": "NASS load_time (when the figure was loaded to Quick Stats)"},
            as_published={k: row[k] for k in sorted(row) if k in PROVIDER_CONTRACTS["nass-quickstats"]["fields"]},
            source=_source("nass-quickstats", urls["data"], f"/data/{index}", origin)))
    for commodity in commodities.values():
        out.append(declaration("commodity", "nass-quickstats", "quickstats", commodity=commodity,
                               source=_source("nass-quickstats", urls["data"], "/data", origin)))
    return out


def psd_estimate_type(market_year: int, release_year: int, release_month: int) -> tuple[str, str]:
    """USDA's own label for a PSD figure by the WASDE convention (verify); the pack projects nothing."""
    newest = release_year if release_month >= 5 else release_year - 1
    if market_year >= newest:
        return "publisher-projection", "projection"
    if market_year == newest - 1:
        return "publisher-estimate", "estimated"
    return "observation", "not-flagged"


def parse_psd(responses: Mapping[str, Any], urls: Mapping[str, str], entry: Mapping[str, Any], *,
              origin: str) -> list[dict[str, Any]]:
    attributes = {str(a.get("attributeId")): str(a.get("attributeName") or "").strip()
                  for a in responses.get("attributes") or []}
    units = {str(u.get("unitId")): str(u.get("unitDescription") or "").strip() for u in responses.get("units") or []}
    wanted = {str(a) for a in entry["attributes"]}
    commodity = _commodity("psd-commodity", entry["commodity"], entry.get("commodity_name"))
    out, releases = [], {}
    for role in sorted(k for k in responses if k.startswith("data:")):
        rows = responses[role]
        if not isinstance(rows, list):
            raise AgrifoodFormatError("schema_drift", "a PSD answer is not a list")
        for index, row in enumerate(rows):
            attribute = str(row.get("attributeId"))
            if attribute not in wanted:
                continue
            if attribute not in attributes or str(row.get("unitId")) not in units:
                raise AgrifoodFormatError("schema_drift", "a PSD row names an attribute or unit the lists lack")
            try:
                market_year, release_year, month = (int(row["marketYear"]), int(row["calendarYear"]),
                                                    int(row["month"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise AgrifoodFormatError("schema_drift", "a PSD row lacks its market year or release month") from exc
            released = f"{release_year:04d}-{month:02d}"
            release = {"key": f"psd:{released}", "released_at": released,
                       "basis": "PSD release month (calendarYear and month; WASDE-aligned)"}
            releases[released] = release
            estimate, flag_class = psd_estimate_type(market_year, release_year, month)
            number = decimal_text(row.get("value"))
            out.append(figure(
                "food_balance", "fas-psd", "psd", commodity=commodity,
                place=_place("psd-country", row.get("countryCode") or entry["country"], entry.get("country_name"),
                             "country"),
                measure={"kind": "supply_distribution", "element": attributes[attribute], "element_code": attribute,
                         "label": attributes[attribute]},
                unit=units[str(row["unitId"])],
                period={"type": "marketing-year", "value": str(market_year), "key": str(market_year),
                        "reference": None, "start": None, "end": None,
                        "definition": "USDA PSD market year as published (the country's local marketing year); "
                                      "never converted to a calendar year"},
                value={"text": None if row.get("value") is None else str(row["value"]), "number": number,
                       "status": "reported" if number is not None else "missing"},
                flag_value=flag("psd-release-convention", None, default_classes=(flag_class,),
                                note="USDA projection" if estimate == "publisher-projection" else
                                "USDA estimate" if estimate == "publisher-estimate" else None),
                estimate_type=estimate, release=release,
                as_published={k: row[k] for k in sorted(row) if k in PROVIDER_CONTRACTS["fas-psd"]["fields"]},
                source=_source("fas-psd", urls[role], f"/{index}", origin)))
    for release in releases.values():
        out.append(declaration("release", "fas-psd", "psd", release=release,
                               source=_source("fas-psd", urls[sorted(k for k in urls if k.startswith("data:"))[0]],
                                              "/", origin)))
    out.append(declaration("commodity", "fas-psd", "psd", commodity=commodity,
                           source=_source("fas-psd", urls["attributes"], "/", origin)))
    return out


def parse_eurostat(content: bytes, url: str, entry: Mapping[str, Any], *, origin: str) -> list[dict[str, Any]]:
    from src.ingestion.connectors.dataset.base import RawSeries, SeriesRef
    from src.ingestion.connectors.dataset.sdmx import SDMXConnector
    from src.integrations.common import IntegrationError

    dataset = str(entry["dataset"])
    try:
        records = SDMXConnector("ESTAT", max_bytes=20_000_000, max_observations=100_000).parse_csv(
            RawSeries(SeriesRef(dataset, metadata={"flow": dataset}), content, source_url=url))
    except IntegrationError as exc:
        raise AgrifoodFormatError("schema_drift", f"{exc.code}: {exc}") from exc
    out, commodities, releases = [], {}, {}
    scheme = str(entry.get("commodity_scheme") or f"eurostat-{entry['commodity_dimension']}")
    labels = dict(entry.get("labels") or {})
    for record in records:
        meta = record.metadata
        dims = {k.casefold(): v for k, v in meta["dimensions"].items()}
        released = meta.get("provider_last_update_at")
        if not released:
            raise AgrifoodFormatError("schema_drift", "the Eurostat answer states no LAST UPDATE")
        release = {"key": f"eurostat:{dataset}:{released}", "released_at": released,
                   "basis": "Eurostat dataset LAST UPDATE (SDMX-CSV)"}
        releases[released] = release
        code = dims.get(str(entry["commodity_dimension"]).casefold())
        geo = dims.get("geo")
        if not code or not geo:
            raise AgrifoodFormatError("schema_drift", "a Eurostat series lacks its commodity or geo code")
        commodity = _commodity(scheme, code, labels.get(code))
        commodities[code] = commodity
        if entry.get("measure_dimension"):
            element = dims.get(str(entry["measure_dimension"]).casefold()) or ""
            measure_spec = dict(dict(entry.get("measures") or {}).get(element) or {})
        else:
            element = dataset
            measure_spec = dict(entry.get("measure") or {})
        if not measure_spec.get("kind"):
            raise AgrifoodFormatError("schema_drift", f"measure {element!r} is not declared by the selection")
        unit = (dims.get(str(entry["unit_dimension"]).casefold()) if entry.get("unit_dimension")
                else measure_spec.get("unit"))
        for obs in record.observations:
            text = meta["original_values"].get(obs.period)
            number = decimal_text(text) if text not in (None, "", ":") else None
            attrs = {k.upper(): v for k, v in dict(meta["observation_attributes"].get(obs.period) or {}).items()}
            flag_value = flag("eurostat-obs-flags", attrs.get("OBS_FLAG"))
            status = ("reported" if number is not None else "not-applicable" if "not-applicable" in
                      flag_value["classes"] else "withheld" if "confidential" in flag_value["classes"] else "missing")
            period_type = "calendar-year" if re.fullmatch(r"\d{4}", obs.period) else "other"
            out.append(figure(
                "observation", "eurostat-agri", dataset, commodity=commodity,
                place=_place("eurostat-geo", geo, labels.get(geo), "country"),
                measure={"kind": measure_spec["kind"], "element": element, "element_code": element,
                         "label": str(measure_spec.get("label") or element)},
                unit=unit,
                period={"type": period_type, "value": obs.period, "key": obs.period, "reference": None,
                        "start": None, "end": None, "definition": None},
                value={"text": text or None, "number": number, "status": status}, flag_value=flag_value,
                estimate_type="publisher-forecast" if "forecast" in flag_value["classes"] else "observation",
                release=release,
                as_published={"dimensions": meta["dimensions"], "TIME_PERIOD": obs.period, "OBS_VALUE": text,
                              "OBS_FLAG": attrs.get("OBS_FLAG"), "LAST UPDATE": meta.get("provider_last_update")},
                source=_source("eurostat-agri", url, f"row {meta['row_lines'][obs.period]}", origin)))
    for release in releases.values():
        out.append(declaration("release", "eurostat-agri", dataset, release=release,
                               source=_source("eurostat-agri", url, "LAST UPDATE", origin)))
    for commodity in commodities.values():
        out.append(declaration("commodity", "eurostat-agri", dataset, commodity=commodity,
                               source=_source("eurostat-agri", url, entry["commodity_dimension"], origin)))
    return out


def parse_portal(responses: Mapping[str, Any], urls: Mapping[str, str], entry: Mapping[str, Any], *,
                 origin: str) -> list[dict[str, Any]]:
    rows = responses.get("prices")
    if not isinstance(rows, list):
        raise AgrifoodFormatError("schema_drift", "the portal price answer is not a list")
    sector = str(entry["sector"])
    content = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()[:16]
    release = {"key": f"agri-food-portal:{sector}:content:{content}", "released_at": None,
               "basis": "content of the portal answer (the portal publishes no release stamp)"}
    out, commodities = [], {}
    for index, row in enumerate(rows):
        start, end = _iso_dmy(row.get("beginDate")), _iso_dmy(row.get("endDate"))
        member = _text(row.get("memberStateCode"))
        if not (start and member):
            raise AgrifoodFormatError("schema_drift", "a portal price row lacks its member state or begin date")
        week = row.get("weekNumber")
        commodity = _commodity("agrifood-portal-product", entry["product"], row.get("productName"))
        commodities[commodity["code"]] = commodity
        market = " - ".join(x for x in (_text(row.get("marketName")), _text(row.get("stageName"))) if x) or None
        number = decimal_text(row.get("price"))
        value_key = f"{start[:4]}-W{int(week):02d}" if isinstance(week, int) else f"{start}/{end or ''}"
        out.append(figure(
            "observation", "agri-food-portal", f"{sector}-prices", commodity=commodity,
            place=_place("eu-member-state", member, row.get("memberStateName"), "country"), market=market,
            measure={"kind": "market_price", "element": "price", "element_code": None, "label": "market price"},
            unit=row.get("unit"),
            period={"type": "week" if isinstance(week, int) else "date-range", "value": value_key, "key": value_key,
                    "reference": None, "start": start, "end": end, "definition": None},
            value={"text": _text(row.get("price")), "number": number,
                   "status": "reported" if number is not None else "missing"},
            flag_value=flag("agri-food-portal", None), estimate_type="observation", release=release,
            as_published={k: row[k] for k in sorted(row) if k in PROVIDER_CONTRACTS["agri-food-portal"]["fields"]},
            source=_source("agri-food-portal", urls["prices"], f"/{index}", origin)))
    for commodity in commodities.values():
        out.append(declaration("commodity", "agri-food-portal", f"{sector}-prices", commodity=commodity,
                               source=_source("agri-food-portal", urls["prices"], "/", origin)))
    return out


# ------------------------------------------------------------------ runtime adapter


class AgrifoodSourceAdapter:
    """One page per selection on the runtime's default transport (same host, byte ceiling, timeout)."""

    accepts_transport = True
    connector = CONNECTOR

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None) -> None:
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        self.source = json.loads(json.dumps(source))
        self.provider, self.entries = selection_entries(self.source)
        if transport is None:
            from functools import partial

            transport = partial(HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"]))
        self.transport = transport
        self._secret = secret
        self.definition = {
            "contract": ADAPTER_CONTRACT, "source_id": source["source_id"], "connector": source["connector"],
            "endpoint": source["endpoint"], "operations": list(source["operations"]),
            "source_hash": source["source_hash"], "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"], "limits": source["budgets"],
            "agrifood": {"provider": self.provider, "selected": len(self.entries)},
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _check(self, request: Mapping[str, Any]) -> None:
        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError("operation_forbidden", "operation is not declared by the source")
        if set(request) - {"operation", "parameters", "limit", "from_ms", "to_ms"}:
            raise SourcePackError("parameter_forbidden", "runtime adapter received undeclared controls")
        if dict(request.get("parameters") or {}):
            raise SourcePackError("parameter_forbidden", "agrifood runs fetch the declared selection only")

    def _get(self, url: str, query: Mapping[str, str]) -> tuple[Any, str, str]:
        """(body or None when the provider has no data, citation URL without credentials, origin)."""
        cited = url + ("?" + urlencode(sorted(query.items())) if query else "")
        params, headers = dict(query), {"Accept": "application/json"}
        if self.provider in SECRET_REFS:
            if not self._secret:
                raise SourcePackError("authentication_failed", f"the {SECRET_REFS[self.provider]} secret is not "
                                                               "configured")
            if self.provider == "nass-quickstats":
                params["key"] = self._secret
            else:
                headers["API_KEY"] = self._secret
        if self.provider == "eurostat-agri":
            headers["Accept"] = "text/csv"
        response = self.transport(url=url, params=params, headers=headers,
                                  timeout=int(self.definition["limits"]["timeout_ms"]) / 1000)
        host = (urlsplit(self.source["endpoint"]).hostname or "").casefold()
        if (urlsplit(str(response.get("final_url") or url)).hostname or "").casefold() != host:
            raise SourcePackError("network_policy", "response was served from another host")
        status = int(response.get("status", 200))
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError("response_too_large", "response exceeds its byte limit")
        origin = "fixture" if response.get("origin") == "fixture" else "live"
        if status == 404 or (status == 400 and self.provider == "nass-quickstats" and b"no data" in raw.lower()):
            return None, cited, origin
        if status == 429:
            from src.ingestion.source_pack_runtime import _retry_after_ms

            headers_in = {str(k).casefold(): v for k, v in dict(response.get("headers") or {}).items()}
            raise SourcePackError("rate_limited", "provider quota is temporarily exhausted",
                                  retry_after_ms=_retry_after_ms(headers_in.get("retry-after")))
        if status in {401, 403}:
            raise SourcePackError("authentication_failed", f"request refused (HTTP {status})")
        if status >= 500:
            raise SourcePackError("source_unavailable", f"provider returned HTTP {status}")
        if status >= 400:
            raise SourcePackError("schema_drift", f"request returned HTTP {status}")
        if self.provider == "eurostat-agri":
            return raw, cited, origin
        try:
            return json.loads(raw.decode("utf-8")), cited, origin
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourcePackError("schema_drift", "response is not JSON") from exc

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage

        self._check(request)
        index = 0 if cursor is None else int(cursor) if str(cursor).isdigit() else -1
        if not 0 <= index < len(self.entries):
            raise SourcePackError("cursor_drift", "cursor names no declared selection")
        entry = self.entries[index]
        responses, urls, origins, missing, size = {}, {}, set(), [], 0
        for role, url, query in requests_for(self.provider, self.source["endpoint"], entry):
            body, cited, origin = self._get(url, query)
            origins.add(origin)
            urls[role] = cited
            if body is None:
                missing.append(role)
                continue
            size += len(body) if isinstance(body, bytes) else len(json.dumps(body))
            responses[role] = body
        origin = "live" if "live" in origins else "fixture"
        data_roles = [r for r in urls if r in {"data", "prices"} or r.startswith("data:")]
        if all(r in missing for r in data_roles):
            outcome, statements = "no_data", []
        else:
            try:
                if self.provider == "faostat":
                    statements = parse_faostat(responses, urls, entry, origin=origin)
                elif self.provider == "nass-quickstats":
                    statements = parse_nass(responses, urls, entry, origin=origin)
                elif self.provider == "fas-psd":
                    statements = parse_psd(responses, urls, entry, origin=origin)
                elif self.provider == "eurostat-agri":
                    statements = parse_eurostat(responses["data"], urls["data"], entry, origin=origin)
                else:
                    statements = parse_portal(responses, urls, entry, origin=origin)
            except (AgrifoodFormatError, AgrifoodError, KeyError, TypeError, ValueError) as exc:
                raise SourcePackError("schema_drift", f"{getattr(exc, 'code', 'parse')}: {exc}") from exc
            outcome = "found" if any(s["record_type"] in {"observation", "food_balance"} for s in statements) \
                else "no_data"
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        if len(statements) > limit:
            raise SourcePackError("budget_exhausted", "selection has more statements than the run's result budget")
        records = []
        for item in statements:
            content = json.dumps(item, sort_keys=True, ensure_ascii=False)
            key = item.get("series_key") or f"{item['record_type']}:{item['provider']}:{item['dataset']}"
            period = (item.get("period") or {}).get("key") or (item.get("commodity") or item.get("release") or {}).get(
                "code") or (item.get("release") or {}).get("key") or ""
            records.append({
                "id": f"{key}|{period}|" + hashlib.sha256(content.encode()).hexdigest()[:12],
                "title": _title(item), "url": item["source"]["url"], "language": "en", "content": content,
                "agrifood_record": item})
        label = {k: entry[k] for k in sorted(entry) if not isinstance(entry[k], (dict, list)) or k in {
            "years", "market_years", "elements", "attributes"}}
        receipt = {"status": 200, "provider": self.provider, "selection": label, "outcome": outcome,
                   "missing": missing, "statements": len(records), "evidence_origin": origin,
                   "final_page": index + 1 >= len(self.entries)}
        next_cursor = str(index + 1) if index + 1 < len(self.entries) else None
        return RuntimePage(tuple(records), next_cursor, size, receipt=receipt)


def _title(item: Mapping[str, Any]) -> str:
    if item["record_type"] in {"observation", "food_balance"}:
        commodity, place = item["commodity"], item["place"]
        return (f"{commodity.get('label') or commodity['code']} - {item['measure']['label']} - "
                f"{place.get('label') or place['code']} - {item['period']['key']}")
    if item["record_type"] == "release":
        return f"{item['provider']} {item['dataset']} release {item['release']['key']}"
    return f"{item['provider']} commodity {item['commodity']['code']}"


FIXTURE_SECRET = "fixture-credential"
ADAPTERS = {CONNECTOR: AgrifoodSourceAdapter}


def fixture_transport(pages: Sequence[Mapping[str, Any]]) -> Callable[..., Mapping[str, Any]]:
    """Replay authored responses keyed by URL path and sorted query (the credential is never part of the key)."""
    by_key = {page["request"]: page for page in pages}

    def transport(*, url, params, headers, timeout):
        del headers, timeout
        parts = urlsplit(url)
        query = {k: v for k, v in dict(params or {}).items() if k != "key"}
        key = parts.path + ("?" + urlencode(sorted(query.items())) if query else "")
        page = by_key.get(key)
        if page is None:
            raise SourcePackError("fixture_missing", f"no native page for {key}")
        body = page.get("body")
        content = body.encode() if isinstance(body, str) else b"" if body is None else json.dumps(body).encode()
        return {"status": int(page.get("status", 200)), "headers": dict(page.get("headers") or {}),
                "content": content, "origin": "fixture"}

    return transport


def replay_native_fixture(source: Mapping[str, Any], fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    adapter = AgrifoodSourceAdapter(source, transport=fixture_transport(list(fixture["native_pages"])),
                                    secret=FIXTURE_SECRET)
    records, cursor = [], None
    while True:
        page = adapter.fetch_page({"operation": min(source["operations"]), "parameters": {},
                                   "limit": int(source["budgets"]["max_results"])}, cursor=cursor)
        records += [dict(item) for item in page.records]
        cursor = page.next_cursor
        if cursor is None:
            return records


__all__ = [
    "ADAPTERS", "CONNECTOR", "EXCLUSIONS", "FIXTURE_SECRET", "LIVE_VERIFICATION", "PROVIDERS", "PROVIDER_CONTRACTS",
    "PROVIDER_HOSTS", "SECRET_REFS", "AgrifoodFormatError", "AgrifoodSourceAdapter", "fixture_transport",
    "parse_eurostat", "parse_faostat", "parse_nass", "parse_portal", "parse_psd", "psd_estimate_type",
    "replay_native_fixture", "requests_for", "selection_entries",
]
