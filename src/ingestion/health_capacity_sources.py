"""Health-system capacity acquisition for the Clinical Evidence ``health_capacity`` feature (#2215, HS01/HS03-HS05).

The ``health-capacity`` connector is the surveillance connector
(:class:`src.ingestion.surveillance_sources.SurveillanceAdapter`, subclassed,
not copied) restricted to capacity documents: every declared document names its
capacity domain (``beds``, ``workforce`` or ``expenditure``) and is read through
the existing parsers and clients -

* ``who-gho-odata`` - the existing WHO GHO OData path (``/api/Indicator`` and
  ``/api/{IndicatorCode}``), keyed by indicator code, spatial dimension, time
  dimension and (where the answer states it) publish state;
* ``oecd-sdmx-csv`` - OECD Health Statistics through the existing
  :class:`src.ingestion.connectors.dataset.sdmx.SDMXConnector` (``OECD``
  provider, SDMX-CSV), keyed by dataflow, version, dimension key and period with
  ``OBS_STATUS`` and the OECD source and country notes kept verbatim;
* ``eurostat-sdmx-csv`` - the existing Eurostat path (``hlth_rs_*`` health care
  resources, ``hlth_sha11_*`` expenditure by financing scheme) with the existing
  ``OBS_FLAG`` mapping.

Series, definitions (declared per indicator or measure code as the publisher
states them, with valid-from dates), definition revisions and vintages are
stored by the surveillance store (``noesis-surveillance-record-v1``); no second
series or vintage store exists. :mod:`src.kb.health_capacity` composes the
capacity view over it. Nothing here ranks, scores, harmonises or re-estimates.

``CAPACITY_CONTRACTS`` records the HS01 access decisions per source (the WHO GHO
and Eurostat decisions confirm reuse of the existing ``who-gho-odata`` and
``eurostat-sdmx-csv`` paths); ``PROVIDER_CONTRACTS`` holds the one new provider
(``oecd-health``) and is merged into
:data:`src.ingestion.clinical_providers.PROVIDER_CONTRACTS`. Every item marked
*verify* must be checked against the live service before the dated live run
(HS12, #2481).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from src.ingestion.source_packs import SourcePackError
from src.ingestion.surveillance_sources import (
    CAPACITY_DOMAINS,
    FIXTURE_SECRET,
    SurveillanceAdapter,
    fixture_transport,
)

CONNECTOR = "health-capacity"
FORMATS = ("who-gho-odata", "oecd-sdmx-csv", "eurostat-sdmx-csv")
BOUNDARY = (
    "Published health-system capacity indicators (beds, workforce, expenditure by financing scheme) as each source "
    "released them, with definitions, flags and vintages, sources side by side: no ranking, performance or quality "
    "score, no harmonised, adjusted or re-estimated value and no combined metric."
)

# HS01 bounded coverage: indicators with the source's own codes, places and years (see the source audit).
BOUNDED_COVERAGE: dict[str, Any] = {
    "places": {
        "countries": ["DEU / DE (Germany)", "FRA / FR (France)"],
        "aggregates_kept_as_aggregates": ["WHO region EUR", "OECD", "EU27_2020"],
    },
    "years": {"live": "2015-2023 (verify availability per indicator)", "fixtures": "2096-2098 (fictional)"},
    "indicators": {
        "who-gho": {
            "beds": ["WHS6_102 (hospital beds per 10 000 population)"],
            "workforce": [
                "HWF_0001 (medical doctors per 10 000 population)",
                "HWF_0006 (nursing and midwifery personnel per 10 000 population; verify code)",
            ],
            "expenditure": [
                "GHED_CHEGDP_SHA2011 (current health expenditure as percent of GDP)",
                "GHED_GGHE-DCHE_SHA2011 (domestic general government health expenditure as percent of current "
                "health expenditure; verify code)",
            ],
        },
        "oecd-health": {
            "beds": ["OECD.ELS.HD,DSD_HEALTH_REAC_HOSP@DF_BEDS_FUNC,1.0 (hospital beds; verify dataflow)"],
            "workforce": ["OECD.ELS.HD,DSD_HEALTH_EMP_REAC@DF_PHYS,1.0 (practising physicians; verify dataflow)"],
            "expenditure": ["OECD.ELS.HD,DSD_SHA@DF_SHA,1.0 (health expenditure by financing scheme; verify)"],
        },
        "eurostat-health": {
            "beds": ["hlth_rs_bds1 facility HBEDT (available beds in hospitals)"],
            "workforce": ["hlth_rs_prs2 isco08 OC221 (medical doctors), OC2221 (nursing professionals)"],
            "expenditure": ["hlth_sha11_hf icha11_hf TOT_HF, HF1 (all and government/compulsory schemes)"],
        },
    },
    "excluded": [
        "health-system performance rankings or quality scores",
        "per-capita and absolute currency amounts (bounded set is shares and densities; no currency conversion)",
    ],
}

_COMMON = {
    "status": "implemented",
    "access_decision": "unverified-live",
    "unavailable_fallback": "record the provider failure, keep every stored vintage and mark the source stale",
}
CAPACITY_CONTRACTS: dict[str, dict[str, Any]] = {
    "who-gho": {
        **_COMMON,
        "reuses": "the existing who-gho-odata path of src/ingestion/surveillance_sources.py (parse_gho, gho_urls); "
        "no second GHO client",
        "documentation": "https://www.who.int/data/gho/info/gho-odata-api",
        "endpoints": "ghoapi.azureedge.net/api/Indicator?$filter=IndicatorCode eq '<code>' (indicator name) and "
        "ghoapi.azureedge.net/api/<IndicatorCode>[?$filter=SpatialDim eq '<ISO3>'] (values)",
        "terms": "WHO data terms of use; most GHO data CC BY-NC-SA 3.0 IGO (verify)",
        "attribution": "World Health Organization, Global Health Observatory, indicator <code> (retrieved <date>)",
        "rate_limits": "undocumented (verify); at most 10 pages per indicator per run",
        "keys": "IndicatorCode, SpatialDim (ISO3, WHO region, GLOBAL), TimeDim (year), Dim1-3 and PublishState "
        "where the answer states it",
        "definitions": "the GHO Indicator Metadata Registry pages (definition, unit, method of estimation) are "
        "not served by the OData API; they are declared per document with their locator and valid-from date and "
        "stored as definition revisions with the release's retrieval time",
        "revision_behaviour": "values change in place without a release stamp; a changed value on re-acquisition "
        "is a new vintage (release clock: Last-Modified or the latest value Date, labelled); a changed metadata "
        "definition is a new definition edition and a marked break",
    },
    "oecd-health": {
        **_COMMON,
        "reuses": "the existing SDMX connector (SDMXConnector('OECD').csv_url / parse_csv); adapter wiring in "
        "src/ingestion/surveillance_sources.py (parse_oecd)",
        "documentation": "https://data-explorer.oecd.org/ (OECD Data Explorer and its SDMX REST API; verify the "
        "API documentation page)",
        "endpoints": "sdmx.oecd.org/public/rest/data/<AGENCY,DSD@DATAFLOW,VERSION>/<key>?format=csvfile",
        "terms": "OECD terms and conditions; OECD data CC BY 4.0 since 2024 (verify)",
        "attribution": "OECD (<year>), OECD Health Statistics, dataflow <id> version <v> (retrieved <date>)",
        "rate_limits": "the OECD API documents a per-IP request limit (verify, around 20 requests per minute); one "
        "request per declared dataflow key per run",
        "keys": "dataflow reference and version, dimension key (REF_AREA, FREQ, MEASURE, UNIT_MEASURE, ...), "
        "TIME_PERIOD",
        "definitions": "OECD Health Statistics definitions, sources and methods (per-variable definitions and "
        "country-specific deviations) are declared per document verbatim (source_note, country_notes) with their "
        "locator; OBS_STATUS (B break, D definition differs, E estimated, P provisional ...) is kept per value",
        "revision_behaviour": "the dataset is updated in place; a new dataflow version is a new declared document "
        "and therefore a new release and vintage; the csvfile answer has no update stamp, so the release clock is "
        "HTTP Last-Modified or the declared publication date, labelled",
    },
    "eurostat-health": {
        **_COMMON,
        "reuses": "the existing eurostat-sdmx-csv path of src/ingestion/surveillance_sources.py (parse_eurostat "
        "through SDMXConnector('ESTAT')) and its OBS_FLAG mapping; no second Eurostat client",
        "documentation": "https://ec.europa.eu/eurostat/web/health/database",
        "endpoints": "ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data/<hlth_rs_*|hlth_sha11_*>/<key>"
        "?format=SDMX-CSV",
        "terms": "Eurostat reuse policy (Commission Decision 2011/833/EU), attribution required",
        "attribution": "Eurostat, dataset <code> (LAST UPDATE <stamp>)",
        "rate_limits": "fair use; one request per declared dataset key per run",
        "keys": "dataset, dimension key (freq, unit, facility / isco08 / icha11_hf, geo), TIME_PERIOD",
        "definitions": "Eurostat metadata (ESMS: hlth_res_esms, hlth_sha11_esms) with the System of Health "
        "Accounts edition; declared per measure code with valid-from dates",
        "revision_behaviour": "LAST UPDATE is the release clock; every new update is a new vintage; flags b "
        "(break) and d (definition differs) are kept verbatim",
    },
}

PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "oecd-health": {
        "documentation": CAPACITY_CONTRACTS["oecd-health"]["documentation"],
        "publisher": "Organisation for Economic Co-operation and Development (OECD Health Statistics)",
        "access": CAPACITY_CONTRACTS["oecd-health"]["endpoints"],
        "authentication": "none",
        "rate_limits": CAPACITY_CONTRACTS["oecd-health"]["rate_limits"],
        "pagination": "none; bounded by the declared series key",
        "cadence": "annual main release with in-place updates (verify)",
        "terms": CAPACITY_CONTRACTS["oecd-health"]["terms"],
        "retained_evidence": "raw SDMX-CSV digest, dataflow and version, row numbers, OBS_STATUS and attributes per "
        "value, declared notes",
        "identifiers": ["dataflow AGENCY,DSD@DATAFLOW,VERSION", "REF_AREA (ISO 3166-1 alpha-3, OECD aggregates)",
                        "MEASURE and UNIT_MEASURE codes"],
        "cross_references": "a publication citing the dataflow links by explicit citation",
        "delivers": ["observation"],
        "dates": "TIME_PERIOD (reference year); no reporting date per value; Last-Modified or the declared "
        "publication date as the release clock",
        "case_definitions": "OECD definitions, sources and methods declared per measure with valid-from dates",
        "geography_codes": "ISO 3166-1 alpha-3 countries and OECD aggregates (never treated as countries)",
        "condition_identifiers": "capacity domain (beds, workforce, expenditure) with the MEASURE code",
        "units": "per 1000 population, percent of GDP, percent of current health expenditure (as declared)",
        "status": "implemented",
        "access_decision": "unverified-live",
        "reason": "fixture-verified through the SDMX connector's SDMX-CSV reader; the dataflow ids, versions, "
        "dimension names and the csvfile column layout need a dated live run (#2481)",
        "unavailable_fallback": CAPACITY_CONTRACTS["oecd-health"]["unavailable_fallback"],
    }
}
PROVIDER_HOSTS = {"oecd-health": {"sdmx.oecd.org"}}
LIVE_VERIFICATION = {
    provider: {"status": "unverified-live",
               "note": "no dated live run of the health-capacity sources yet (#2481); fixture evidence only"}
    for provider in PROVIDER_CONTRACTS
}


def capacity_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    """The capacity-only restrictions on a surveillance declaration: known formats and a domain per document."""
    declared = dict(source.get("surveillance") or {})
    if declared.get("format") not in FORMATS:
        raise SourcePackError(
            "invalid_manifest", f"health-capacity sources use one of the formats {FORMATS}"
        )
    for document in declared.get("documents") or []:
        if document.get("capacity_domain") not in CAPACITY_DOMAINS:
            raise SourcePackError(
                "invalid_manifest",
                f"each health-capacity document names its capacity_domain ({', '.join(CAPACITY_DOMAINS)})",
            )
    return declared


class HealthCapacityAdapter(SurveillanceAdapter):
    """The surveillance connector restricted to declared capacity documents (one release per document)."""

    connector = CONNECTOR

    def __init__(
        self,
        source: Mapping[str, Any],
        *,
        transport: Callable[..., Mapping[str, Any]] | None = None,
        secret: str | None = None,
    ) -> None:
        capacity_declaration(source)
        super().__init__(source, transport=transport, secret=secret)
        self.definition["health_capacity"] = {
            "domains": sorted({d["capacity_domain"] for d in self.declared["documents"]})
        }


ADAPTERS = {CONNECTOR: HealthCapacityAdapter}


def replay_native_fixture(
    source: Mapping[str, Any], fixture: Mapping[str, Any]
) -> list[dict[str, Any]]:
    adapter = HealthCapacityAdapter(
        source, transport=fixture_transport(list(fixture["native_pages"])), secret=FIXTURE_SECRET
    )
    records, cursor = [], None
    while True:
        page = adapter.fetch_page(
            {"operation": min(source["operations"]), "parameters": {},
             "limit": int(source["budgets"]["max_results"])},
            cursor=cursor,
        )
        records += [dict(item) for item in page.records]
        cursor = page.next_cursor
        if cursor is None:
            return records


__all__ = [
    "ADAPTERS", "BOUNDARY", "BOUNDED_COVERAGE", "CAPACITY_CONTRACTS", "CONNECTOR", "FIXTURE_SECRET", "FORMATS",
    "HealthCapacityAdapter", "LIVE_VERIFICATION", "PROVIDER_CONTRACTS", "PROVIDER_HOSTS", "capacity_declaration",
    "fixture_transport", "replay_native_fixture",
]
