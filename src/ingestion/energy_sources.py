"""Bounded, receipted acquisition of electricity and energy-balance series (EN01, EN03-EN07).

Every source has a recorded access decision (:data:`PROVIDER_CONTRACTS`,
audit: ``docs/roadmaps/energy-systems-source-audit.md``): endpoints,
authentication (secret refs only), licence and attribution, rate limits,
release/revision behaviour and the bounded coverage the pack acquires
(:data:`BOUNDED_COVERAGE`). Live state is kept separately in
:data:`LIVE_VERIFICATION`; nothing is live-verified until a dated run says so.

* **ENTSO-E** is *not* acquired a second time. Plans, the securityToken slot,
  host policy and XML parsers are the Climate and Environment ``entsoe``
  adapter's (:mod:`src.ingestion.environment_providers`), extended there with
  day-ahead prices, installed capacity and cross-border flows. Realised
  generation and load come through its existing ``grid_event`` parser and are
  converted with :func:`src.kb.energy_records.from_grid_event`.
* **US EIA Open Data API v2** routes for the Hourly Electric Grid Monitor
  (generation by fuel, demand, interchange) and EIA-860M operating generator
  capacity; demand *forecasts* (type ``DF``) are refused.
* **Ember** monthly generation and demand and yearly capacity, with the
  dataset release declared per selection; Ember's own shares are quoted as
  Ember's (``publisher_figures``), no emissions are computed.
* **Eurostat energy balances** (``nrg_bal_c``) through the existing SDMX
  connector's SDMX-CSV URL builder and reader, flags kept verbatim.
* **Energy-Charts** ``public_power`` only, under the documented licence
  decision; values Energy-Charts re-publishes from ENTSO-E are marked
  ``derived_from`` and never reconciled with ENTSO-E records. Its price
  endpoints are link-only (not acquired).

One plan/parse pipeline serves two paths: :func:`acquire` (explicit bounded
selection, injected or DurableHTTP-backed fetch, one receipt per step) and the
source-pack runtime (``energy`` connector, :class:`EnergySourceAdapter`),
whose pages the :class:`src.kb.energy_store.EnergyProjector` stores.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode, urlsplit

from src.ingestion import environment_providers as envp
from src.ingestion.provider_execution import ProviderError
from src.kb import energy_records as enr

ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
RECORD_SCHEMA = "noesis-energy-record-v1"
FIXTURE_SECRET = "fixture-credential-not-a-real-key"
MAX_STEPS = 40
PROVIDER_HOSTS = {
    "entsoe": envp.PROVIDER_HOSTS["entsoe"],
    "eia": {"api.eia.gov"},
    "ember": {"api.ember-energy.org"},
    "eurostat": {"ec.europa.eu"},
    "energy-charts": {"api.energy-charts.info"},
}
# (slot kind, parameter/header name, secret ref). ENTSO-E reuses the environment adapter's slot.
SECRETS = {"entsoe": envp.SECRETS["entsoe"], "eia": ("param", "api_key", "NOESIS_EIA_API_KEY"),
           "ember": ("param", "api_key", "NOESIS_EMBER_API_KEY")}
_SECRET_PARAMS = {slot[1] for slot in SECRETS.values() if slot[0] == "param"}
NEVER = ("No price forecasting, dispatch modelling, emissions estimation beyond quoting the publisher, "
         "gap-filling, cross-source blending or trading advice.")
_UNAVAILABLE = ("record the failed step with its code in a receipt; stored vintages stay as they are and the "
                "provider reads as stale; values are never inferred")
LICENCES = {
    "entsoe": enr.ENTSOE_LICENCE,
    "eia": {"id": "eia-public-domain", "terms_url": "https://www.eia.gov/about/copyrights_reuse.php"},
    "ember": {"id": "ember-cc-by-4.0", "terms_url": "https://ember-energy.org/creative-commons/"},
    "eurostat": {"id": "eurostat-reuse", "terms_url": "https://ec.europa.eu/eurostat/about-us/policies/copyright"},
    "energy-charts": {"id": "energy-charts-cc-by-4.0", "terms_url": "https://www.energy-charts.info/"},
}
ATTRIBUTION = {
    "entsoe": enr.ENTSOE_ATTRIBUTION,
    "eia": "Source: U.S. Energy Information Administration (EIA)",
    "ember": "Ember (ember-energy.org), CC BY 4.0",
    "eurostat": "Source: Eurostat",
    "energy-charts": "Energy-Charts, Fraunhofer ISE (energy-charts.info), CC BY 4.0",
}
PROVIDER_CONTRACTS = {
    "entsoe": {
        "decision": "acquire (reuse the Climate and Environment entsoe adapter; no second acquisition path)",
        "documentation": "https://transparencyplatform.zendesk.com/hc/en-us/sections/12783116987028-Restful-API",
        "endpoints": ["https://web-api.tp.entsoe.eu/api (A75 generation per type, A65 actual load, A44 day-ahead prices, "
                      "A68 installed capacity per type, A71 installed capacity per production unit, A11 physical flows)"],
        "reuses": "src/ingestion/environment_providers.py: plan('entsoe'), SECRETS['entsoe'], PROVIDER_HOSTS['entsoe'], "
                  "parse_entsoe (grid events) and parse_entsoe_energy (prices, capacity, flows)",
        "authentication": "required securityToken query parameter (secret ref NOESIS_ENTSOE_SECURITY_TOKEN)",
        "terms": "ENTSO-E Transparency Platform terms and conditions; reuse with attribution",
        "attribution": ATTRIBUTION["entsoe"],
        "rate_limits": "400 requests/min per token documented; selections stay below 40 requests",
        "revisions": "documents carry mRID + revisionNumber and createdDateTime; revision 1 is stored as provisional, "
                     "revision > 1 as revised; every revision is a new vintage",
        "excluded": "A65 processType A01 (day-ahead load forecast) and any other forecast document are refused; "
                    "A80 unavailability stays a Climate and Environment record",
        "unavailable_fallback": _UNAVAILABLE,
    },
    "eia": {
        "decision": "acquire (unverified-live)",
        "documentation": "https://www.eia.gov/opendata/documentation.php",
        "endpoints": ["https://api.eia.gov/v2/electricity/rto/fuel-type-data/data/ (net generation by energy source)",
                      "https://api.eia.gov/v2/electricity/rto/region-data/data/ (type D demand, NG net generation; "
                      "DF demand forecast refused)",
                      "https://api.eia.gov/v2/electricity/rto/interchange-data/data/ (BA-to-BA interchange)",
                      "https://api.eia.gov/v2/electricity/operating-generator-capacity/data/ (EIA-860M plant/generator "
                      "capacity)"],
        "authentication": "required api_key query parameter (secret ref NOESIS_EIA_API_KEY); free registration",
        "terms": "U.S. government data, public domain; cite EIA as the source (EIA copyrights and reuse page)",
        "attribution": ATTRIBUTION["eia"],
        "rate_limits": "per-key throttling documented by EIA (verify the current figure); at most 5000 rows per "
                       "request with offset/length paging; selections pin length and read one page, truncation recorded",
        "revisions": "Hourly Electric Grid Monitor (EIA-930) values are preliminary and revised by respondents; the "
                     "API publishes no per-value release date, so a changed value set is a new vintage dated by the "
                     "declared EIA release (EIA-860M monthly release) or by retrieval time, labelled",
        "units": "value-units as published (megawatthours for hourly energy, MW for capacity)",
        "verify": "route paths, facet names (respondent, fueltype, type, fromba, toba, plantid, generatorid), column "
                  "names of operating-generator-capacity and the hour-beginning/ending convention",
        "unavailable_fallback": _UNAVAILABLE,
    },
    "ember": {
        "decision": "acquire (unverified-live)",
        "documentation": "https://ember-energy.org/data/api/",
        "endpoints": ["https://api.ember-energy.org/v1/electricity-generation/monthly",
                      "https://api.ember-energy.org/v1/electricity-demand/monthly",
                      "https://api.ember-energy.org/v1/installed-capacity/yearly"],
        "authentication": "required api_key query parameter (secret ref NOESIS_EMBER_API_KEY); free registration",
        "terms": "CC BY 4.0 (Ember data licence); attribute Ember",
        "attribution": ATTRIBUTION["ember"],
        "rate_limits": "not documented as a figure (verify); one request per endpoint and country in a selection",
        "revisions": "Ember publishes dataset releases (monthly and yearly); the selection declares the release "
                     "label and date, and a new release is a new vintage beside the prior one",
        "quoted_only": "share_of_generation_pct and any intensity figures are Ember's computations, quoted as "
                       "publisher_figures; emissions are not acquired and the pack computes none",
        "verify": "endpoint names, parameter names (entity_code, start_date, end_date) and response fields",
        "unavailable_fallback": _UNAVAILABLE,
    },
    "eurostat": {
        "decision": "acquire (unverified-live) through the existing SDMX connector",
        "documentation": "https://ec.europa.eu/eurostat/web/energy/data/energy-balances",
        "endpoints": ["https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data/nrg_bal_c/{key}?format=SDMX-CSV"],
        "reuses": "src/ingestion/connectors/dataset/sdmx.py SDMXConnector('ESTAT').csv_url and parse_csv",
        "authentication": "none",
        "terms": "Eurostat copyright and reuse policy (Commission Decision 2011/833/EU); cite Eurostat",
        "attribution": ATTRIBUTION["eurostat"],
        "rate_limits": "none documented as a figure; one request per declared key",
        "revisions": "LAST UPDATE column dates the vintage; observation flags (p provisional, e estimated, b break in "
                     "series) are kept verbatim; a new LAST UPDATE is a new vintage",
        "verify": "dimension order of nrg_bal_c (freq.nrg_bal.siec.unit.geo), the OBS_FLAG column name",
        "unavailable_fallback": _UNAVAILABLE,
    },
    "energy-charts": {
        "decision": "acquire public_power only; price endpoints link-only (not acquired)",
        "documentation": "https://api.energy-charts.info/",
        "endpoints": ["https://api.energy-charts.info/public_power?country={cc}&start={date}&end={date}"],
        "link_only": ["https://api.energy-charts.info/price (day-ahead prices originate from power exchanges whose "
                      "redistribution terms Energy-Charts does not grant; ENTSO-E A44 is the price source)"],
        "authentication": "none",
        "terms": "Energy-Charts states its data are licensed CC BY 4.0 unless stated otherwise (verify the per-series "
                 "licence note at the first live run)",
        "attribution": ATTRIBUTION["energy-charts"],
        "rate_limits": "not documented as a figure; one request per country and day window",
        "revisions": "no release or revision marker is published; a changed value set is a new vintage dated by "
                     "retrieval time, labelled",
        "derived": "public power for European countries re-publishes ENTSO-E Transparency data (for Germany also "
                   "other sources); records are marked derived_from entsoe, kept separate and never reconciled",
        "unavailable_fallback": _UNAVAILABLE,
    },
}
BOUNDED_COVERAGE = {
    "zones": [{"eic": "10Y1001A1001A82H", "name": "DE-LU", "borders": {"10YFR-RTE------C": "FR", "10YPL-AREA-----S": "PL"}},
              {"eic": "10YFR-RTE------C", "name": "FR", "borders": {}}],
    "countries": {"DE": {"iso3": "DEU", "eurostat": "DE", "energy_charts": "de"},
                  "FR": {"iso3": "FRA", "eurostat": "FR", "energy_charts": "fr"}},
    "balancing_areas": ["CISO", "ERCO", "PJM"],
    "plants": "EIA plant ids and ENTSO-E production units named explicitly per selection (at most 20)",
    "datasets": {"generation_by_fuel": ["entsoe A75", "eia fuel-type-data", "ember electricity-generation", "energy-charts public_power"],
                 "load": ["entsoe A65 A16", "eia region-data D", "ember electricity-demand", "energy-charts public_power Load"],
                 "day_ahead_price": ["entsoe A44"],
                 "installed_capacity": ["entsoe A68/A71", "eia operating-generator-capacity", "ember installed-capacity"],
                 "cross_border_flows": ["entsoe A11", "eia interchange-data"],
                 "energy_balances": ["eurostat nrg_bal_c"]},
    "windows": {"hourly": "at most 7 days per selection", "monthly": "at most 36 months", "yearly": "at most 10 years"},
}
LIVE_VERIFICATION = {
    provider: {"status": "unverified-live", "checked_at": None, "report": None,
               "note": "no dated bounded live run yet (EN14, #2271); offline fixtures only"}
    for provider in PROVIDER_CONTRACTS
}
for _provider in ("entsoe", "eia", "ember"):
    LIVE_VERIFICATION[_provider]["credential"] = f"required ({SECRETS[_provider][2]})"
_EIA_ROUTES = {
    "fuel-type-data": {"path": "electricity/rto/fuel-type-data", "record_type": "generation"},
    "region-data": {"path": "electricity/rto/region-data", "record_type": None},
    "interchange-data": {"path": "electricity/rto/interchange-data", "record_type": "cross_border_flow"},
    "operating-generator-capacity": {"path": "electricity/operating-generator-capacity", "record_type": "capacity"},
}
_EIA_REGION_TYPES = {"D": "load", "NG": "generation"}
_EMBER_ENDPOINTS = {
    "electricity-generation/monthly": {"record_type": "generation", "value": "generation_twh", "unit": "TWh",
                                       "resolution": "P1M", "shares": ("share_of_generation_pct",)},
    "electricity-demand/monthly": {"record_type": "load", "value": "demand_twh", "unit": "TWh", "resolution": "P1M",
                                   "shares": ("demand_mwh_per_capita",)},
    "installed-capacity/yearly": {"record_type": "capacity", "value": "capacity_gw", "unit": "GW", "resolution": "P1Y",
                                  "shares": ()},
}
_ENERGY_CHARTS_SKIP = {"Residual load": "publisher-computed (not an observation of generation)",
                       "Renewable share of generation": "publisher-computed share",
                       "Renewable share of load": "publisher-computed share",
                       "Cross border electricity trading": "trading balance, not a physical flow"}
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# --------------------------------------------------------------------- helpers


def _json(content):
    try:
        return json.loads(content, parse_float=str, parse_int=str)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProviderError("schema_drift", "provider response is not valid JSON") from exc


def _num(value, field):
    if value is None:
        return None
    text = str(value).strip()
    if text in {"", "null", "NaN", ":"}:
        return None
    try:
        return enr.decimal_text(text, field)
    except enr.EnergyRecordError as exc:
        raise ProviderError("schema_drift", f"{field} is not numeric") from exc


def _utc(value):
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build(*args, **kwargs):
    try:
        return enr.record(*args, **kwargs)
    except enr.EnergyRecordError as exc:
        raise ProviderError(exc.code if exc.code in {"forecast_refused", "derived_value_refused"} else "schema_drift",
                            str(exc)) from exc


def _bounded(values, field, limit):
    if not isinstance(values, list) or not 1 <= len(values) <= limit:
        raise ProviderError("unbounded_selection", f"{field} must list 1-{limit} explicit items")
    return [str(v) for v in values]


def _declared_release(selection, retrieved_at, *, key_prefix):
    release = dict(selection.get("release") or {})
    if release:
        if not _ISO.fullmatch(str(release.get("published_on") or "")) or not release.get("label"):
            raise ProviderError("unbounded_selection", "a declared release states its label and published_on date")
        return {"key": f"{key_prefix}:{release['label']}", "released_at": release["published_on"],
                "basis": "declared_release", "label": release["label"], "revision": None}
    return {"key": f"{key_prefix}:retrieved:{retrieved_at}", "released_at": None, "basis": "retrieval_time",
            "label": None, "revision": None}


def _next_period(start, resolution):
    if resolution == "PT1H":
        return _utc(datetime.fromisoformat(start.replace("Z", "+00:00")) + timedelta(hours=1))
    if resolution == "P1M":
        year, month = int(start[:4]), int(start[5:7])
        return f"{year + (month == 12):04d}-{month % 12 + 1:02d}"
    if resolution == "P1Y":
        return f"{int(start[:4]) + 1:04d}"
    return None


# ------------------------------------------------------------------ selections


def plan(provider, selection):
    """Ordered, bounded request steps for one explicit selection."""

    selection = dict(selection or {})
    if provider == "entsoe":
        documents = _bounded(selection.get("documents"), "documents", 8)
        refused = sorted(set(documents) & {"load-forecast", "unavailability"})
        if refused:
            raise ProviderError("forecast_refused" if "load-forecast" in refused else "unbounded_selection",
                                f"not energy observations: {refused} (forecasts are refused; unavailability stays "
                                "with Climate and Environment)")
        steps = envp.plan("entsoe", selection)
        for step in steps:
            step["provider"] = "entsoe"
        return steps
    if provider not in PROVIDER_CONTRACTS:
        raise ProviderError("not_implemented", f"{provider} has no energy adapter")
    steps = []
    if provider == "eia":
        route = _EIA_ROUTES.get(str(selection.get("route")))
        if route is None:
            raise ProviderError("unbounded_selection", "EIA selections name one declared route")
        length = int(selection.get("length", 500))
        if not 1 <= length <= 5000:
            raise ProviderError("unbounded_selection", "EIA selections read at most 5000 rows")
        base = {"data[0]": "value", "start": str(selection["start"]), "end": str(selection["end"]),
                "offset": "0", "length": str(length), "sort[0][column]": "period", "sort[0][direction]": "asc"}
        name = selection["route"]
        url = f"https://api.eia.gov/v2/{route['path']}/data/"
        if name == "operating-generator-capacity":
            base.update({"frequency": "monthly", "data[0]": "nameplate-capacity-mw", "data[1]": "net-summer-capacity-mw"})
            for plant in _bounded(selection.get("plants"), "plants", 20):
                steps.append({"parse": "eia", "url": url, "params": {**base, "facets[plantid][]": plant},
                              "context": {"route": name, "plant": plant}})
        elif name == "interchange-data":
            base["frequency"] = "hourly"
            for area in _bounded(selection.get("balancing_areas"), "balancing_areas", 6):
                steps.append({"parse": "eia", "url": url, "params": {**base, "facets[fromba][]": area},
                              "context": {"route": name, "area": area}})
        else:
            base["frequency"] = "hourly"
            types = [None]
            if name == "region-data":
                types = _bounded(selection.get("types"), "types", 2)
                if "DF" in types:
                    raise ProviderError("forecast_refused", "EIA type DF is a demand forecast; forecasts are refused")
                if set(types) - set(_EIA_REGION_TYPES):
                    raise ProviderError("unbounded_selection", "EIA region-data types are D (demand) and NG (net generation)")
            for area in _bounded(selection.get("balancing_areas"), "balancing_areas", 6):
                for kind in types:
                    params = {**base, "facets[respondent][]": area}
                    if kind:
                        params["facets[type][]"] = kind
                    steps.append({"parse": "eia", "url": url, "params": params,
                                  "context": {"route": name, "area": area, "type": kind}})
    elif provider == "ember":
        endpoint = _EMBER_ENDPOINTS.get(str(selection.get("endpoint")))
        if endpoint is None:
            raise ProviderError("unbounded_selection", "Ember selections name one declared endpoint")
        for entity in _bounded(selection.get("entities"), "entities", 10):
            if not re.fullmatch(r"[A-Z]{3}", entity):
                raise ProviderError("unbounded_selection", "Ember entities are ISO 3166 alpha-3 codes")
            steps.append({"parse": "ember", "url": f"https://api.ember-energy.org/v1/{selection['endpoint']}",
                          "params": {"entity_code": entity, "start_date": str(selection["start_date"]),
                                     "end_date": str(selection["end_date"])},
                          "context": {"endpoint": selection["endpoint"], "entity": entity}})
    elif provider == "eurostat":
        from src.ingestion.connectors.dataset.sdmx import SDMXConnector

        dataset = str(selection.get("dataset") or "")
        if dataset not in {"nrg_bal_c", "nrg_bal_s"}:
            raise ProviderError("unbounded_selection", "Eurostat selections name nrg_bal_c or nrg_bal_s")
        for key in _bounded(selection.get("keys"), "keys", 6):
            try:
                url, query = SDMXConnector("ESTAT").csv_url(dataset, key, dict(selection.get("params") or {}))
            except ValueError as exc:
                raise ProviderError("unbounded_selection", str(exc)) from exc
            steps.append({"parse": "eurostat", "url": url, "params": query,
                          "context": {"dataset": dataset, "key": key}})
    elif provider == "energy-charts":
        endpoint = str(selection.get("endpoint") or "public_power")
        if endpoint != "public_power":
            raise ProviderError("link_only", "Energy-Charts price endpoints are link-only under the licence decision")
        for country in _bounded(selection.get("countries"), "countries", 4):
            if not re.fullmatch(r"[a-z]{2}(-[a-z]{2})?", country):
                raise ProviderError("unbounded_selection", "Energy-Charts countries are lower-case codes")
            steps.append({"parse": "energy_charts", "url": "https://api.energy-charts.info/public_power",
                          "params": {"country": country, "start": str(selection["start"]), "end": str(selection["end"])},
                          "context": {"country": country}})
    if not steps or len(steps) > MAX_STEPS:
        raise ProviderError("unbounded_selection", "selection compiles to no or too many requests")
    for step in steps:
        step["provider"] = provider
    return steps


# --------------------------------------------------------------------- parsers


def parse_eia(content, context, carry, *, url):
    payload = _json(content)
    response = payload.get("response") if isinstance(payload, dict) else None
    rows = response.get("data") if isinstance(response, dict) else None
    if not isinstance(rows, list):
        raise ProviderError("schema_drift", "EIA response lacks response.data")
    retrieved, route = context["retrieved_at"], context["route"]
    selection = context.get("selection") or {}
    groups: dict[tuple, dict[str, Any]] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or "period" not in row:
            raise ProviderError("schema_drift", f"EIA row {index} has no period")
        period = str(row["period"])
        flags = {"row": index, "period_label": period,
                 "period_convention": "EIA period label as published (UTC for hourly; verify hour-beginning/ending)"}
        if route == "operating-generator-capacity":
            plant, generator = str(row.get("plantid") or context.get("plant")), str(row.get("generatorid") or "")
            key = ("capacity", plant, generator)
            entry = groups.setdefault(key, {"rows": [], "row": row})
            entry["rows"].append((period, row.get("net-summer-capacity-mw"),
                                  {**flags, "nameplate_capacity_mw": row.get("nameplate-capacity-mw"),
                                   "status": row.get("status"), "operating_year_month": row.get("operating-year-month")}))
            continue
        if route == "region-data":
            kind = str(row.get("type") or "")
            if kind == "DF":
                raise ProviderError("forecast_refused", "EIA returned demand-forecast rows; forecasts are refused")
            record_type = _EIA_REGION_TYPES.get(kind)
            if record_type is None:
                continue
            key = (record_type, str(row.get("respondent")), kind)
        elif route == "fuel-type-data":
            key = ("generation", str(row.get("respondent")), str(row.get("fueltype")))
        else:
            key = ("cross_border_flow", str(row.get("fromba")), str(row.get("toba")))
        entry = groups.setdefault(key, {"rows": [], "row": row})
        entry["rows"].append((period, row.get("value"), {**flags, "value_units": row.get("value-units")}))
    total = _num(response.get("total"), "total")
    truncated = total is not None and int(total) > len(rows)
    release = _declared_release(selection, retrieved, key_prefix=f"eia:{route}")
    records = []
    for key, entry in sorted(groups.items()):
        row = entry["row"]
        monthly = route == "operating-generator-capacity"
        resolution = "P1M" if monthly else "PT1H"
        values = []
        for period, value, flags in entry["rows"]:
            start = period if monthly else period + ":00:00Z"
            values.append({"start": start, "end": _next_period(start, resolution), "value": _num(value, "value"),
                           "flags": flags})
        values.sort(key=lambda v: v["start"])
        common = {"source_url": url, "attribution": ATTRIBUTION["eia"], "licence": LICENCES["eia"],
                  "resolution": resolution, "values": values, "release": release, "retrieved_at": retrieved,
                  "reference_period": {"start": values[0]["start"], "end": values[-1]["end"]},
                  "status": "provisional",
                  "status_basis": ("EIA-860M monthly data are preliminary until the annual EIA-860 (verify)" if monthly
                                   else "EIA-930 hourly data are preliminary and revised by respondents (verify)"),
                  "locator": {"route": route, "request_facets": {k: v for k, v in (context.get("params") or {}).items()
                                                                if k.startswith("facets")}, "api_version": payload.get("apiVersion")}}
        if key[0] == "capacity":
            _, plant, generator = key
            records.append(_build(
                "capacity", "eia", "eia:operating-generator-capacity", f"plant:{plant}:generator:{generator}",
                f"Net summer capacity of {row.get('plantName') or plant} generator {generator}",
                subject={"kind": "unit", "scheme": "eia-generator", "code": f"{plant}:{generator}",
                         "name": row.get("plantName")},
                unit="MW", facets={"energy_source_code": row.get("energy_source_code"),
                                   "technology": row.get("technology"), "measure": "net-summer-capacity-mw"},
                capacity={"level": "unit", "effective_from": row.get("operating-year-month"),
                          "effective_to": row.get("planned-retirement-year-month") or None,
                          "operating_status": row.get("status"),
                          "plant": {"eia_plant_id": plant, "plant_name": row.get("plantName"),
                                    "balancing_authority": row.get("balancing_authority_code"), "state": row.get("stateid")}},
                **common))
            continue
        record_type, first, second = key
        units = {v["flags"].get("value_units") for v in values} - {None}
        unit = sorted(units)[0] if len(units) == 1 else None
        if unit is None:
            raise ProviderError("schema_drift", "EIA series has no single value-units")
        area = {"kind": "balancing-area", "scheme": "eia-ba", "code": first,
                "name": row.get("respondent-name") or row.get("fromba-name")}
        if record_type == "cross_border_flow":
            records.append(_build(
                "cross_border_flow", "eia", "eia:rto/interchange-data", f"interchange:{first}:{second}",
                f"Interchange {first} to {second}", subject=area,
                counterpart={"kind": "balancing-area", "scheme": "eia-ba", "code": second, "name": row.get("toba-name")},
                unit=unit, facets={"direction": "net", "sign_convention": "as published by EIA (positive = out of fromba; verify)"},
                **common))
            continue
        facet = ({"fuel": {"code": second, "label": row.get("type-name"), "scheme": "eia-fueltype"}}
                 if route == "fuel-type-data" else {"series_type": second, "label": row.get("type-name")})
        records.append(_build(
            record_type, "eia", f"eia:rto/{route}", f"{route}:{first}:{second}",
            f"{row.get('type-name') or second} ({first})", subject=area, unit=unit, facets=facet, **common))
    return records, {**carry, **({"eia_truncated": [*carry.get("eia_truncated", []), url]} if truncated else {})}


def parse_ember(content, context, carry, *, url):
    payload = _json(content)
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ProviderError("schema_drift", "Ember response lacks data")
    spec = _EMBER_ENDPOINTS[context["endpoint"]]
    retrieved = context["retrieved_at"]
    release = _declared_release(context.get("selection") or {}, retrieved, key_prefix=f"ember:{context['endpoint']}")
    groups: dict[tuple, list] = {}
    names = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or "date" not in row or "entity_code" not in row:
            raise ProviderError("schema_drift", f"Ember row {index} lacks date or entity_code")
        if spec["value"] not in row:
            raise ProviderError("schema_drift", f"Ember row {index} lacks {spec['value']}")
        series = str(row.get("series") or "Total")
        groups.setdefault((row["entity_code"], series, str(row.get("is_aggregate_series", "false")).lower()), []).append((index, row))
        names[row["entity_code"]] = row.get("entity")
    records = []
    for (entity, series, aggregate), items in sorted(groups.items()):
        values, shares = [], {}
        for index, row in items:
            date = str(row["date"])
            start = date[:7] if spec["resolution"] == "P1M" else date[:4]
            values.append({"start": start, "end": _next_period(start, spec["resolution"]),
                           "value": _num(row.get(spec["value"]), spec["value"]), "flags": {"row": index}})
            for share in spec["shares"]:
                if row.get(share) is not None:
                    shares.setdefault(share, {})[start] = _num(row.get(share), share)
        values.sort(key=lambda v: v["start"])
        figures = ({"publisher": "Ember", "note": "computed and published by Ember; quoted as published, not computed "
                                                   "by Noesis", **shares} if shares else {})
        record_type = spec["record_type"]
        records.append(_build(
            record_type, "ember", f"ember:{context['endpoint']}", f"{context['endpoint']}:{entity}:{series}",
            f"Ember {series} {record_type} ({names.get(entity) or entity})", source_url=url,
            attribution=ATTRIBUTION["ember"], licence=LICENCES["ember"],
            subject={"kind": "country", "scheme": "iso3166-alpha3", "code": entity, "name": names.get(entity)},
            unit=spec["unit"], resolution=spec["resolution"],
            reference_period={"start": values[0]["start"], "end": values[-1]["end"]}, values=values, release=release,
            status="unknown", status_basis="Ember marks no provisional/final status per value; the dataset release is the vintage",
            retrieved_at=retrieved,
            facets={"fuel": {"code": series, "label": series, "scheme": "ember-series"} if record_type != "load" else None,
                    "aggregate_series": aggregate == "true"},
            capacity={"level": "zone", "effective_from": values[0]["start"], "effective_to": values[-1]["end"],
                      "operating_status": None, "plant": None} if record_type == "capacity" else None,
            publisher_figures=figures, locator={"endpoint": context["endpoint"], "entity_code": entity}))
    return records, carry


def parse_eurostat(content, context, carry, *, url):
    from src.ingestion.connectors.dataset.base import RawSeries, SeriesRef
    from src.ingestion.connectors.dataset.sdmx import SDMXConnector
    from src.integrations.common import IntegrationError

    connector = SDMXConnector("ESTAT")
    ref = SeriesRef(locator=f"{context['dataset']}/{context['key']}", metadata={"flow": context["dataset"]},
                    title=f"Eurostat {context['dataset']}")
    try:
        series = connector.parse_csv(RawSeries(ref, content, content_type="text/csv", source_url=url, fetched_at=0))
    except IntegrationError as exc:
        raise ProviderError("schema_drift", f"{exc.code}: {exc}") from exc
    retrieved = context["retrieved_at"]
    declared = context.get("selection") or {}
    records = []
    for item in series:
        meta = item.metadata
        dims = {k.upper(): v for k, v in meta["dimensions"].items()}
        for needed in ("NRG_BAL", "SIEC", "UNIT", "GEO"):
            if not dims.get(needed):
                raise ProviderError("schema_drift", f"Eurostat energy-balance series lacks {needed}")
        values, flagged = [], set()
        for observation in item.observations:
            attributes = dict(meta["observation_attributes"].get(observation.period) or {})
            flag = attributes.get("OBS_FLAG") or attributes.get("OBS_STATUS")
            if flag:
                flagged |= set(flag)
            values.append({"start": observation.period, "end": _next_period(observation.period, "P1Y")
                           if re.fullmatch(r"\d{4}", observation.period) else None,
                           "value": _num(meta["original_values"].get(observation.period), "OBS_VALUE"),
                           "flags": {**attributes, "row": meta["row_lines"].get(observation.period)}})
        values.sort(key=lambda v: v["start"])
        stamp = meta.get("provider_last_update_at")
        if stamp:
            release = {"key": f"eurostat:{context['dataset']}:{stamp}", "released_at": stamp + ("Z" if len(stamp) == 19 else ""),
                       "basis": "provider_last_update", "label": f"LAST UPDATE {meta.get('provider_last_update')}",
                       "revision": None}
        else:
            release = _declared_release(declared, retrieved, key_prefix=f"eurostat:{context['dataset']}")
        status = "provisional" if "p" in flagged else "unknown"
        records.append(_build(
            "energy_balance", "eurostat", f"eurostat:{context['dataset']}",
            f"{dims['NRG_BAL']}:{dims['SIEC']}:{dims['UNIT']}:{dims['GEO']}",
            f"Eurostat energy balance {dims['NRG_BAL']} {dims['SIEC']} ({dims['GEO']}, {dims['UNIT']})",
            source_url=url, attribution=ATTRIBUTION["eurostat"], licence=LICENCES["eurostat"],
            subject={"kind": "country", "scheme": "eurostat-geo", "code": dims["GEO"], "name": None},
            unit=dims["UNIT"], resolution="P1Y" if dims.get("FREQ") == "A" else None,
            reference_period={"start": values[0]["start"], "end": values[-1]["end"]}, values=values,
            release=release, status=status,
            status_basis=("Eurostat flag p (provisional) on at least one value" if status == "provisional"
                          else "no provisional flag; Eurostat does not declare unflagged values final"),
            retrieved_at=retrieved,
            facets={"nrg_bal": dims["NRG_BAL"], "siec": dims["SIEC"], "freq": dims.get("FREQ"),
                    "flags_seen": sorted(flagged)},
            locator={"dataflow": meta.get("dataflow"), "key": context["key"], "dimensions": meta["dimensions"],
                     "raw_sha256": meta.get("raw_sha256")}))
    return records, carry


def parse_energy_charts(content, context, carry, *, url):
    payload = _json(content)
    stamps = payload.get("unix_seconds") if isinstance(payload, dict) else None
    types = payload.get("production_types") if isinstance(payload, dict) else None
    if not isinstance(stamps, list) or not isinstance(types, list):
        raise ProviderError("schema_drift", "Energy-Charts public_power lacks unix_seconds/production_types")
    retrieved, country = context["retrieved_at"], context["country"]
    starts = [datetime.fromtimestamp(int(s), tz=UTC) for s in stamps]
    step = starts[1] - starts[0] if len(starts) > 1 else timedelta(minutes=15)
    minutes = int(step.total_seconds() // 60)
    resolution = f"PT{minutes // 60}H" if minutes % 60 == 0 else f"PT{minutes}M"
    records, skipped = [], []
    for item in types:
        name = str(item.get("name") or "")
        data = item.get("data")
        if name in _ENERGY_CHARTS_SKIP:
            skipped.append({"name": name, "reason": _ENERGY_CHARTS_SKIP[name]})
            continue
        if not isinstance(data, list) or len(data) != len(starts):
            raise ProviderError("schema_drift", f"Energy-Charts series {name!r} is misaligned")
        values = [{"start": _utc(begin), "end": _utc(begin + step), "value": _num(value, name),
                   "flags": {"index": index}} for index, (begin, value) in enumerate(zip(starts, data, strict=True))]
        record_type = "load" if name == "Load" else "generation"
        records.append(_build(
            record_type, "energy-charts", "energy-charts:public_power", f"public_power:{country}:{name}",
            f"Energy-Charts public power {name} ({country})", source_url=url + "?" + urlencode(
                {k: v for k, v in sorted((context.get("params") or {}).items())}),
            attribution=ATTRIBUTION["energy-charts"], licence=LICENCES["energy-charts"],
            subject={"kind": "country", "scheme": "energy-charts-country", "code": country, "name": None},
            unit="MW", resolution=resolution,
            reference_period={"start": values[0]["start"] if values else retrieved,
                              "end": values[-1]["end"] if values else None},
            values=values, release={"key": f"energy-charts:public_power:retrieved:{retrieved}", "released_at": None,
                                    "basis": "retrieval_time", "label": None, "revision": None},
            status="unknown", status_basis="Energy-Charts publishes no provisional/final marker", retrieved_at=retrieved,
            facets={"fuel": None if record_type == "load" else {"code": name, "label": name, "scheme": "energy-charts-production-type"},
                    "endpoint": "public_power"},
            derived_from={"provider": "entsoe", "basis": "Energy-Charts re-publishes ENTSO-E Transparency Platform data "
                                                        "for European public power (per its documentation; verify per country)"},
            locator={"endpoint": "public_power", "country": country, "production_type": name,
                     "deprecated": payload.get("deprecated")}))
    return records, {**carry, **({"energy_charts_skipped": skipped} if skipped else {})}


def parse_entsoe_generation_load(content, context, carry, *, url):
    """Realised generation/load via the environment parser, converted to energy records."""

    events, carry = envp.parse_entsoe(content, context, carry, url=url)
    records = []
    for event in events:
        try:
            converted = enr.from_grid_event(event, retrieved_at=context["retrieved_at"])
        except enr.EnergyRecordError as exc:
            raise ProviderError("schema_drift", str(exc)) from exc
        if converted is not None:
            records.append(converted)
    return records, carry


PARSERS: dict[str, Callable[..., tuple[list[dict[str, Any]], dict[str, Any]]]] = {
    "eia": parse_eia, "ember": parse_ember, "eurostat": parse_eurostat, "energy_charts": parse_energy_charts,
    "entsoe": parse_entsoe_generation_load, "entsoe_energy": envp.parse_entsoe_energy,
}


def parse_step(step, content, carry, *, retrieved_at, selection=None):
    context = {**dict(step.get("context") or {}), "params": step.get("params"), "retrieved_at": retrieved_at,
               "selection": dict(selection or {})}
    records, carry = PARSERS[step["parse"]](content, context, dict(carry or {}), url=step["url"])
    try:
        return [enr.validate(r) for r in records], carry
    except enr.EnergyRecordError as exc:
        raise ProviderError("schema_drift", f"record failed validation: {exc}") from exc


def coverage(provider, carry, records):
    notes = {k: v for k, v in carry.items() if k.endswith(("no_data", "truncated", "skipped")) and v}
    return {"complete": False, "provider": provider, "records": len(records), "notes": notes,
            "basis": "explicit bounded selection; never comprehensive provider coverage"}


# ------------------------------------------------------------- acquisition


def _with_secret(provider, secret, params, headers):
    slot = SECRETS.get(provider)
    if slot is None:
        return params, headers
    if not secret:
        raise ProviderError("authentication_failed", f"{provider} requires {slot[2]}")
    if slot[0] == "header":
        return params, {**headers, slot[1]: secret}
    return {**params, slot[1]: secret}, headers


def _public(params):
    return {k: v for k, v in dict(params or {}).items() if k not in _SECRET_PARAMS | {"securityToken"}}


def acquire(conn, provider, selection, *, namespace, scopes, principal_id, fetch, secret=None, run_id=None,
            execution="injected", now=None):
    """Run one bounded selection step by step; each step is receipted, failures change no stored value.

    ``fetch(url=..., params=..., headers=...)`` returns ``{"status", "headers", "content"}``: a fixture
    transport offline, a DurableHTTP-backed fetch (:func:`durable_fetch`) live.
    """

    from src.kb.energy_store import EnergyStore, iso

    store = EnergyStore(conn, now=now)
    steps = plan(provider, selection)
    for step in steps:
        if urlsplit(step["url"]).hostname not in PROVIDER_HOSTS[provider]:
            raise ProviderError("network_policy", "energy requests stay on the provider's declared hosts")
    run_id = run_id or "energy-run:" + enr.digest([namespace, provider, selection, store.now()])[:24]
    carry, results, failures = {}, [], []
    counts = {"series": 0, "vintages": 0, "unchanged": 0}
    for index, step in enumerate(steps):
        request = {"url": step["url"], "params": _public(step["params"]), "parse": step["parse"], "step": index}
        try:
            params, headers = _with_secret(provider, secret, dict(step["params"]), {"Accept": "*/*"})
            response = fetch(url=step["url"], params=params, headers=headers)
            status = int(response.get("status", 200))
            raw = response.get("content", b"")
            raw = raw.encode() if isinstance(raw, str) else bytes(raw)
            if status in {401, 403}:
                raise ProviderError("authentication_failed", f"{provider} refused the credential (HTTP {status})")
            if status == 429:
                raise ProviderError("rate_limited", f"{provider} rate limit reached")
            if status >= 400:
                raise ProviderError("source_unavailable" if status >= 500 else "schema_drift",
                                    f"{provider} returned HTTP {status}")
            if secret and len(secret) >= 8 and secret.encode() in raw:
                raise ProviderError("schema_drift", "provider echoed a credential; response is not safe evidence")
            retrieved = iso(store.now())
            records, carry = parse_step(step, raw, carry, retrieved_at=retrieved, selection=selection)
        except ProviderError as exc:
            failures.append(store.fail(namespace, provider, exc, source=f"{provider}:{step['parse']}", request=request,
                                       run_id=run_id, principal_id=principal_id, execution=execution))
            continue
        receipt_id = store.receipt(namespace, provider=provider, source=f"{provider}:{step['parse']}", request=request,
                                   response_sha256=hashlib.sha256(raw).hexdigest(), size=len(raw), status="ok",
                                   execution=execution, run_id=run_id, records=len(records), principal_id=principal_id,
                                   coverage=coverage(provider, carry, records))
        applied = store.apply(namespace, records, receipt_id=receipt_id, run_id=run_id, principal_id=principal_id,
                              scopes=scopes, execution=execution)
        for key in counts:
            counts[key] += applied[key]
        results.append({"receipt_id": receipt_id, "records": len(records), **applied})
    return {"ok": not failures, "provider": provider, "run_id": run_id, "steps": len(steps), "applied": counts,
            "receipts": results, "failures": failures, "coverage": coverage(provider, carry, []),
            "live_verification": LIVE_VERIFICATION[provider]["status"]}


def durable_fetch(http, *, principal_id, observation, secret_slot=None):
    """A ``fetch`` over :class:`~src.ingestion.provider_execution.DurableHTTP` (exact hosts, budget, receipts)."""

    counter = {"n": 0}

    def fetch(*, url, params, headers):
        counter["n"] += 1
        secret_params = {k: v for k, v in params.items() if k in _SECRET_PARAMS | {"securityToken"}}
        public = {k: v for k, v in params.items() if k not in secret_params}
        secret_headers = {k: v for k, v in headers.items() if secret_slot and k == secret_slot}
        public_headers = {k: v for k, v in headers.items() if k not in secret_headers}
        captured = http.request(f"{observation}:{counter['n']}", url, principal_id=principal_id, params=public,
                                headers=public_headers, secret_params=secret_params or None,
                                secret_headers=secret_headers or None, max_bytes=20_000_000)
        return {"status": 200, "headers": {}, "content": captured.content}

    return fetch


# ------------------------------------------------------- source-pack adapter


def runtime_record(record, *, response_sha256):
    return {"id": f"{record['provider']}:{record['dataset']}:{record['native_id']}:{record['release']['key']}",
            "title": record["title"], "language": "en", "url": record["source_url"], "energy_record": record,
            "response_sha256": response_sha256}


class EnergySourceAdapter:
    """``energy`` native connector: one plan step per runtime page."""

    accepts_transport = True

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None, now: Callable[[], int] | None = None) -> None:
        from functools import partial

        from src.ingestion.source_pack_runtime import HTTPSPageAdapter
        from src.ingestion.source_packs import SourcePackError

        self.source = json.loads(json.dumps(source))
        spec = dict(self.source.get("energy") or {})
        self.provider = str(spec.get("provider") or "")
        self.selection = dict(spec.get("selection") or {})
        try:
            self.steps = plan(self.provider, self.selection)
        except ProviderError as exc:
            raise SourcePackError("unbounded_source", str(exc)) from exc
        for step in self.steps:
            if urlsplit(step["url"]).hostname not in PROVIDER_HOSTS[self.provider]:
                raise SourcePackError("network_policy", "energy requests stay on the provider's declared hosts")
        self.secret = secret
        self.now = now
        self.transport = transport or partial(HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"]))
        self.definition = {
            "contract": ADAPTER_CONTRACT, "source_id": source["source_id"], "connector": source["connector"],
            "endpoint": source["endpoint"], "operations": list(source["operations"]),
            "source_hash": source["source_hash"], "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"], "limits": source["budgets"],
            "energy": {"provider": self.provider, "steps": len(self.steps),
                       "live_verification": LIVE_VERIFICATION[self.provider]["status"]},
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _scope(self) -> str:
        return enr.digest({"source_hash": self.source["source_hash"], "steps": self.steps})

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        import time

        from src.ingestion.source_pack_runtime import RuntimePage, _retry_after_ms
        from src.ingestion.source_packs import SourcePackError

        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError("operation_forbidden", "operation is not declared by the source")
        if dict(request.get("parameters") or {}):
            raise SourcePackError("parameter_forbidden", "energy runs use the pinned selection")
        state = {} if cursor is None else json.loads(cursor)
        if cursor is not None and state.get("scope") != self._scope():
            raise SourcePackError("cursor_drift", "cursor belongs to a different selection")
        index = int(state.get("i", 0))
        if index >= len(self.steps):
            return RuntimePage((), None, 0, receipt={"status": 200})
        step = self.steps[index]
        try:
            params, headers = _with_secret(self.provider, self.secret, dict(step["params"]), {"Accept": "*/*"})
        except ProviderError as exc:
            raise SourcePackError("authentication_failed", str(exc)) from exc
        response = self.transport(url=step["url"], params=params, headers=headers,
                                  timeout=int(self.definition["limits"]["timeout_ms"]) / 1000)
        status = int(response.get("status", 200))
        response_headers = {str(k).casefold(): v for k, v in dict(response.get("headers") or {}).items()}
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError("response_too_large", "source response exceeds its byte limit")
        if status == 429:
            raise SourcePackError("rate_limited", f"{self.provider} rate limit reached",
                                  retry_after_ms=_retry_after_ms(response_headers.get("retry-after")))
        if status in {401, 403}:
            raise SourcePackError("authentication_failed", f"{self.provider} refused the credential (HTTP {status})")
        if status >= 500:
            raise SourcePackError("source_unavailable", f"{self.provider} returned HTTP {status}")
        if status >= 400:
            raise SourcePackError("schema_drift", f"{self.provider} returned HTTP {status}")
        if self.secret and len(self.secret) >= 8 and self.secret.encode() in raw:
            raise SourcePackError("schema_drift", "provider echoed a credential; response is not safe evidence")
        response_sha256 = hashlib.sha256(raw).hexdigest()
        clock = self.now or (lambda: int(time.time() * 1000))
        retrieved = datetime.fromtimestamp(clock() / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            records, carry = parse_step(step, raw, state.get("carry") or {}, retrieved_at=retrieved,
                                        selection=self.selection)
        except ProviderError as exc:
            raise SourcePackError("schema_drift" if exc.code != "authentication_failed" else exc.code, str(exc)) from exc
        more = index + 1 < len(self.steps)
        next_cursor = (json.dumps({"i": index + 1, "scope": self._scope(), "carry": carry}, sort_keys=True)
                       if more else None)
        return RuntimePage(tuple(runtime_record(r, response_sha256=response_sha256) for r in records), next_cursor,
                           len(raw), receipt={"status": status, "step": index, "steps": len(self.steps),
                                              "parse": step["parse"], "response_sha256": response_sha256,
                                              "provider": self.provider, "request": {"url": step["url"],
                                                                                     "params": _public(step["params"])},
                                              "coverage": coverage(self.provider, carry, records)})


ADAPTERS = {"energy": EnergySourceAdapter}


def _fixture_body(page):
    if "body_base64" in page:
        return base64.b64decode(page["body_base64"])
    body = page.get("body")
    return json.dumps(body).encode() if isinstance(body, (dict, list)) else (body or "").encode()


def fixture_transport(pages: Sequence[Mapping[str, Any]]) -> Callable[..., Mapping[str, Any]]:
    """Serve authored native pages keyed by URL + public query (credentials are ignored)."""

    by_key = {(page["url"], enr.canonical(dict(page.get("params") or {}))): page for page in pages}

    def transport(*, url, params, headers, timeout=None, **_):
        del headers, timeout
        page = by_key.get((url, enr.canonical(_public(params))))
        if page is None:
            return {"status": 404, "headers": {}, "content": b""}
        return {"status": int(page.get("status", 200)), "headers": dict(page.get("headers") or {}),
                "content": _fixture_body(page)}

    return transport


def fixture_clock(fixture: Mapping[str, Any]) -> Callable[[], int]:
    """The fixture's declared retrieval time (deterministic replay)."""

    stamp = datetime.fromisoformat(str(fixture.get("retrieved_at") or "2026-09-29T00:00:00Z").replace("Z", "+00:00"))
    return lambda: int(stamp.timestamp() * 1000)


def replay_native_fixture(source: Mapping[str, Any], fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    adapter = EnergySourceAdapter(source, transport=fixture_transport(list(fixture["native_pages"])),
                                  secret=FIXTURE_SECRET, now=fixture_clock(fixture))
    records: list[dict[str, Any]] = []
    cursor = None
    for _ in range(int(source["budgets"]["max_pages"])):
        page = adapter.fetch_page({"operation": min(source["operations"]), "parameters": {}}, cursor=cursor)
        records.extend(dict(item) for item in page.records)
        cursor = page.next_cursor
        if cursor is None:
            break
    return records
