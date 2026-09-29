"""Housing, land-value and urban-planning acquisition for the Geospatial ``housing`` feature (#1912, U01/U03-U06).

Two acquisition paths, both through the source-pack runtime:

* **FIS-Broker WFS layers** (BORIS Bodenrichtwerte, Bebauungspläne, Wohnlagen)
  use the existing ``wfs`` connector (:mod:`src.ingestion.wfs_api`,
  :mod:`src.ingestion.geojson_features`). Geometries land in the Geospatial
  feature store; each source additionally declares a ``housing`` block
  (:func:`layer_declaration`) naming which published attribute carries the zone
  or plan identifier, value, valuation date, procedural stage or category, so
  :mod:`src.kb.housing` can project the attributes exactly as delivered.
* **Tabular publications** use the native ``housing`` connector
  (:class:`HousingAdapter`), one declared document per page:

  - ``rent-index-table-csv`` - a Berlin Mietspiegel table in a declared column
    layout (cell key, the published dimensions, lower/middle/upper values),
    with the edition, its qualifying date, the publication URL and the page;
    for a PDF-only edition the file is the operator-extracted table and the
    publication URL and page stay on the declaration;
  - ``statbb-building-csv`` - Amt für Statistik Berlin-Brandenburg building
    permit (Baugenehmigungen) or completion (Baufertigstellungen) tables per
    Bezirk and period in a declared column layout;
  - ``destatis-genesis-ffcsv`` - Destatis GENESIS tables 31111 (permits) and
    31231 (completions) through the dataset connector
    :class:`src.ingestion.connectors.dataset.genesis.GenesisConnector`;
    observations land in the existing ``ObservationStore``.

Nothing is derived: values keep their published text, currency and unit; no
value is rounded, converted, averaged or interpolated between zones, cells or
reporting areas. A column, attribute, stage label, unit or code the
declaration does not name is refused (or, for a WFS feature, reported as a
projection outcome) rather than read partially. A publication that states no
date is never dated by its retrieval time.

``PROVIDER_CONTRACTS`` records the U01 access decisions (``unverified-live``
until a dated live run; ``not-implemented`` with a reason); every item marked
``verify`` must be checked against the live service and terms (#2029).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from src.ingestion.source_packs import SourcePackError

ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
PUBLICATION_CONTRACT = "noesis-housing-publication-v1"
CONNECTOR = "housing"
TARGET_SCHEMA = "noesis-housing-record-v1"
LAYERS = ("land-value", "development-plan", "residential-area")
FORMATS: dict[str, dict[str, str]] = {
    "rent-index-table-csv": {"provider": "berlin-mietspiegel", "kind": "rent-index"},
    "statbb-building-csv": {"provider": "statistik-bb", "kind": "building-statistics"},
    "destatis-genesis-ffcsv": {"provider": "destatis", "kind": "housing-indicator"},
}
STATISTIC_KINDS = ("permits", "completions")
# Measures a building-activity table may declare (the publisher's own figure, never a derived one).
MEASURES = (
    "dwellings_permitted",
    "residential_buildings_permitted",
    "floor_area_permitted",
    "dwellings_completed",
    "residential_buildings_completed",
    "floor_area_completed",
)
# Published units and the label they carry. Money is labelled, never converted; a physical unit may be checked
# through pint (src/integrations/units.py) for labelling only.
UNITS: dict[str, dict[str, str | None]] = {
    "EUR/m²": {
        "kind": "money-per-area",
        "currency": "EUR",
        "area": "m**2",
        "period": None,
    },
    "EUR/m² monthly": {
        "kind": "money-per-area-per-period",
        "currency": "EUR",
        "area": "m**2",
        "period": "month",
    },
    "count": {"kind": "count", "currency": None, "area": None, "period": None},
    "dwellings": {"kind": "count", "currency": None, "area": None, "period": None},
    "buildings": {"kind": "count", "currency": None, "area": None, "period": None},
    "m²": {"kind": "area", "currency": None, "area": "m**2", "period": None},
    "1000 m²": {
        "kind": "area",
        "currency": None,
        "area": "1000 * m**2",
        "period": None,
    },
}
# Development-plan procedural stages in their published order (BauGB §§ 2-10; Berlin AGBauGB).
PLAN_STAGES = (
    "aufstellungsbeschluss",
    "fruehzeitige_beteiligung",
    "oeffentliche_auslegung",
    "festgesetzt",
    "aufgehoben",
)
REFERENCE_SCHEMES = {
    # scheme: (jurisdictions its identifiers are looked up in, how the legal store keeps them)
    "berlin-gvbl": ("DE-BE",),
    "berlin-amtsblatt": ("DE-BE",),
    "juris": ("DE-BE", "DE"),
    "de-bgbl": ("DE",),
    "celex": ("EU",),
    "eli": ("EU",),
    "de-drucksache": ("DE",),
}
LINK_RELATIONS = ("legal_basis", "published_in", "stage_decided_by", "referenced_in")
MAX_ROWS = 100_000
REVIEW_BOUNDARY = (
    "Published values only, each with its source, currency, unit and valuation date, edition or vintage: values "
    "are valid only inside the published zone, cell or reporting area for the stated date. No property valuation, "
    "rent or investment advice, tenancy legal advice, and no value interpolated, averaged or merged between zones, "
    "cells, sources or editions."
)
NO_SURFACE = (
    "No source offers an interpolable value surface: a land value applies inside its published zone for its "
    "valuation date, a rent-index range to its published cell for its edition, and a statistic to its reporting "
    "area and period."
)

# U01 access decisions. Endpoints, type names, attributes, layouts and terms below are recorded from the
# providers' published documentation as known without network access; every item marked ``verify`` must be
# checked against the live service and terms before a dated live run is accepted (#2029).
PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "fis-broker-boris": {
        "publisher": "Senatsverwaltung für Stadtentwicklung, Bauen und Wohnen Berlin / Gutachterausschuss für "
        "Grundstückswerte in Berlin (Geoportal Berlin, FIS-Broker)",
        "delivers": ["land-value-zone"],
        "delivery_shape": "zone geometry with value (WFS feature per Bodenrichtwertzone)",
        "primary_publisher": True,
        "access": "WFS 2.0.0 GetFeature on gdi.berlin.de (successor of fbinter.stadt-berlin.de/fb/wfs); one layer "
        "per Stichtag (verify service and type names per year)",
        "format": "wfs-geojson",
        "authentication": "none",
        "rate_limits": "undocumented (verify); COUNT/STARTINDEX paging with the declared page size",
        "pagination": "WFS COUNT/STARTINDEX with numberMatched; SORTBY on the zone number",
        "identifiers": "Bodenrichtwertnummer (VBORIS WNUM) per Stichtag; WFS feature id",
        "crs": "EPSG:25833 (ETRS89 / UTM 33N); requested with SRSNAME and projected at acquisition",
        "date_semantics": "Stichtag (valuation date) per zone, usually 1 January; the value applies on that date "
        "and is not a price at any other date",
        "currency_unit": "EUR per square metre of land (EUR/m²), with Art der Nutzung, GFZ, Entwicklungszustand "
        "and beitragsrechtlicher Zustand as qualifiers (verify attribute names)",
        "cadence": "annual (Stichtag 1 January), published in spring",
        "terms": "Datenlizenz Deutschland - Zero 2.0 for Geoportal Berlin open data (verify for BORIS layers)",
        "access_decision": "unverified-live",
        "reason": "fixture-verified through the existing WFS path; type names, attribute names and terms need a "
        "dated live run",
    },
    "boris-berlin": {
        "publisher": "Gutachterausschuss für Grundstückswerte in Berlin (BORIS Berlin portal)",
        "delivers": ["land-value-zone"],
        "delivery_shape": "interactive map and per-zone report pages; the same zones as the FIS-Broker layer",
        "primary_publisher": True,
        "access": "web application (www.boris-berlin.de); no documented bulk or API access beyond the FIS-Broker "
        "WFS (verify)",
        "format": "none",
        "authentication": "none",
        "identifiers": "Bodenrichtwertnummer",
        "crs": "not applicable (map viewer)",
        "date_semantics": "Stichtag as in the WFS layers",
        "currency_unit": "EUR/m²",
        "cadence": "annual",
        "terms": "portal terms of use (verify)",
        "access_decision": "not-implemented",
        "reason": "no supported machine access; the portal is never scraped. Land values come from the FIS-Broker "
        "WFS layers of the same publisher",
    },
    "fis-broker-bplan": {
        "publisher": "Senatsverwaltung für Stadtentwicklung, Bauen und Wohnen Berlin (Geoportal Berlin, "
        "Bebauungspläne und vorhabenbezogene Bebauungspläne)",
        "delivers": ["development-plan-stage"],
        "delivery_shape": "plan area geometry with plan number, district, procedural stage and stage dates",
        "primary_publisher": True,
        "access": "WFS 2.0.0 GetFeature on gdi.berlin.de (verify type names; separate layers exist for plans in "
        "procedure and fixed plans)",
        "format": "wfs-geojson",
        "authentication": "none",
        "rate_limits": "undocumented (verify)",
        "pagination": "WFS COUNT/STARTINDEX with numberMatched",
        "identifiers": "Planname (for example 1-23a, VI-140ca) unique within Berlin; WFS feature id",
        "crs": "EPSG:25833",
        "date_semantics": "the stage attribute and the date of the stage (Aufstellungsbeschluss, Festsetzung, "
        "GVBl publication) as published; a missing date stays unknown",
        "currency_unit": "not applicable",
        "cadence": "continuous; layer refreshed irregularly",
        "terms": "Datenlizenz Deutschland - Zero 2.0 (verify)",
        "access_decision": "unverified-live",
        "reason": "fixture-verified through the existing WFS path; stage vocabulary and attribute names need a "
        "dated live run",
    },
    "fis-broker-wohnlagen": {
        "publisher": "Senatsverwaltung für Stadtentwicklung, Bauen und Wohnen Berlin (Wohnlagenkarte zum "
        "Berliner Mietspiegel)",
        "delivers": ["residential-area-category"],
        "delivery_shape": "block or address geometry with the Wohnlage category (einfach, mittel, gut) per "
        "Mietspiegel edition",
        "primary_publisher": True,
        "access": "WFS 2.0.0 GetFeature on gdi.berlin.de, one layer per Mietspiegel edition (verify type names "
        "and whether blocks or address points are served)",
        "format": "wfs-geojson",
        "authentication": "none",
        "identifiers": "block or address identifier; WFS feature id",
        "crs": "EPSG:25833",
        "date_semantics": "the Mietspiegel edition the map belongs to; no date per feature",
        "currency_unit": "none (a category, never a value or score)",
        "cadence": "per Mietspiegel edition (every two years, qualified edition)",
        "terms": "Datenlizenz Deutschland - Zero 2.0 (verify)",
        "access_decision": "unverified-live",
        "reason": "fixture-verified through the existing WFS path; layer shape (blocks versus addresses) needs a "
        "dated live run",
    },
    "berlin-mietspiegel": {
        "publisher": "Senatsverwaltung für Stadtentwicklung, Bauen und Wohnen Berlin (Berliner Mietspiegel)",
        "delivers": ["rent-index-cell"],
        "delivery_shape": "Mietspiegeltabelle: cells by Wohnlage, Baualter and Wohnfläche with lower, middle and "
        "upper values; published as PDF (Amtsblatt für Berlin) and as an online query",
        "primary_publisher": True,
        "access": "no documented machine-readable table (verify daten.berlin.de); the connector reads a declared "
        "CSV layout - for a PDF-only edition the operator-extracted table - and keeps the publication URL, edition "
        "and page on the declaration. The PDF is never scraped",
        "format": "rent-index-table-csv",
        "authentication": "none",
        "rate_limits": "one bounded download per declared document per run",
        "pagination": "none",
        "identifiers": "edition (for example Berliner Mietspiegel 2023) and the published cell key (for example "
        "table field letter and number)",
        "crs": "not applicable (the Wohnlage dimension joins the Wohnlagen layer by its published category)",
        "date_semantics": "each edition states its qualifying date (Stichtag der Datenerhebung) and the date it "
        "applies from; an edition never overwrites another",
        "currency_unit": "EUR per square metre of living space per month, net cold rent (Nettokaltmiete)",
        "cadence": "every two years (qualified Mietspiegel), with interim updates",
        "terms": "official publication; reuse with source attribution (verify)",
        "access_decision": "unverified-live",
        "reason": "fixture-verified parser on an operator-extracted table; no automated access to the PDF",
    },
    "statistik-bb": {
        "publisher": "Amt für Statistik Berlin-Brandenburg",
        "delivers": ["permit-statistic", "completion-statistic"],
        "delivery_shape": "tables of building permits (Baugenehmigungen) and completions (Baufertigstellungen) per "
        "Bezirk and period; Statistische Berichte F II (PDF) and download tables (verify CSV availability)",
        "primary_publisher": True,
        "access": "download tables (CSV/XLSX) on statistik-berlin-brandenburg.de in a declared column layout "
        "(verify files and columns); PDF Statistische Berichte are not scraped",
        "format": "statbb-building-csv",
        "authentication": "none",
        "rate_limits": "undocumented; one bounded download per declared document per run",
        "pagination": "none",
        "identifiers": "Bezirk codes 01-12 (ALKIS gem 001-012), a declared Land total code, reporting period",
        "crs": "not applicable (reporting areas join ALKIS district features by the published Bezirk code)",
        "date_semantics": "reporting period (month, quarter or year) and the publication date (declared or "
        "Last-Modified) as the vintage; revised figures for a period are a new vintage",
        "currency_unit": "counts of dwellings and buildings; floor area in 1000 m²",
        "cadence": "monthly and annual",
        "terms": "CC BY 3.0 DE / Datenlizenz Deutschland (verify per table)",
        "access_decision": "unverified-live",
        "reason": "fixture-verified parser; file URLs and the column layout must be verified",
    },
    "destatis": {
        "publisher": "Statistisches Bundesamt (Destatis), GENESIS-Online",
        "delivers": ["housing-indicator"],
        "delivery_shape": "tabular series (tables 31111 building permits, 31231 building completions) by Land "
        "and year",
        "primary_publisher": True,
        "access": "GENESIS-Online REST API 2020: metadata/table for the Updated stamp and data/tablefile?format="
        "ffcsv, through the dataset connector (src/ingestion/connectors/dataset/genesis.py); credentials in "
        "request headers (verify header names after the 2024 authentication change)",
        "format": "destatis-genesis-ffcsv",
        "authentication": "registered user or token (required-secret NOESIS_DESTATIS_GENESIS_TOKEN)",
        "rate_limits": "undocumented; large tables as background jobs (verify); one bounded table per document",
        "pagination": "none",
        "identifiers": "table codes 31111-xxxx and 31231-xxxx (verify the Länder tables), DLAND (AGS Land code), "
        "value codes per measure",
        "crs": "not applicable",
        "date_semantics": "the table's Updated stamp (Stand) is the vintage; Zeit is the reference year",
        "currency_unit": "counts (Anzahl), floor area (1000 m²), estimated costs (1000 EUR)",
        "cadence": "monthly and annual",
        "terms": "Datenlizenz Deutschland - Namensnennung 2.0 (verify)",
        "access_decision": "unverified-live",
        "reason": "credentialed API; fixture-verified connector only, including the login requirement",
    },
    "ibb-wohnungsmarktbericht": {
        "publisher": "Investitionsbank Berlin (IBB Wohnungsmarktbericht)",
        "delivers": ["cited-publication"],
        "delivery_shape": "annual PDF report with tables and charts (an aggregator of Statistik BB, Mietspiegel "
        "and market data)",
        "primary_publisher": False,
        "access": "PDF download only (verify); no data access path found",
        "format": "none",
        "authentication": "none",
        "identifiers": "report year",
        "crs": "not applicable",
        "date_semantics": "report year and publication date",
        "currency_unit": "mixed; offer rents are asking rents, not rent-index values",
        "cadence": "annual",
        "terms": "IBB publication terms (verify)",
        "access_decision": "not-implemented",
        "reason": "PDF-only secondary publication; recorded as a cited publication only, never scraped or "
        "extracted into values",
    },
    "daten-berlin": {
        "publisher": "Berlin Open Data portal (daten.berlin.de)",
        "delivers": ["catalogue"],
        "delivery_shape": "CKAN catalogue entries pointing to the publishers' own WFS services and files",
        "primary_publisher": False,
        "access": "CKAN API (verify); used to find the publishers' resources, not as a data source of its own",
        "format": "none",
        "authentication": "none",
        "identifiers": "dataset slugs",
        "crs": "not applicable",
        "date_semantics": "catalogue metadata dates, not data dates",
        "currency_unit": "not applicable",
        "cadence": "continuous",
        "terms": "per dataset (mostly dl-de-zero-2.0 or CC BY)",
        "access_decision": "not-implemented",
        "reason": "an aggregator: every housing value is acquired from its primary publisher's service or file",
    },
}


class HousingFormatError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode()
    ).hexdigest()


def _clean(value: Any) -> str | None:
    text = " ".join(str(value if value is not None else "").split())
    return text or None


def day(value: Any) -> str | None:
    """An ISO date from a published date text (ISO, German or ISO timestamp); None when not a date."""
    text = str(value or "").strip()
    for pattern in ("%Y-%m-%d", "%d.%m.%Y", "%Y%m%d"):
        try:
            return (
                datetime.strptime(
                    text[:10] if pattern != "%Y%m%d" else text[:8], pattern
                )
                .date()
                .isoformat()
            )
        except ValueError:
            continue
    return None


def parse_decimal(text: Any, number_format: str = "plain") -> Decimal | None:
    raw = "".join(str(text if text is not None else "").split())
    if not raw:
        return None
    if number_format == "de":
        raw = raw.replace(".", "").replace(",", ".")
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise HousingFormatError(
            "schema_drift", f"value {text!r} is not a number"
        ) from exc
    if not value.is_finite():
        raise HousingFormatError("schema_drift", "value is not finite")
    return value


def check_unit(unit: Any) -> dict[str, Any]:
    """The published unit and its label; an undeclared unit is refused, never guessed."""
    text = _clean(unit)
    if text not in UNITS:
        raise HousingFormatError(
            "schema_drift", f"unit {unit!r} is not a declared unit"
        )
    return {"published": text, **UNITS[text]}


def unit_label(unit: str) -> dict[str, Any]:
    """Label a published unit through the pint owner where installed (labelling only; money is never converted)."""
    label = check_unit(unit)
    physical = label.get("area")
    checked = None
    if physical:
        try:
            import pint

            checked = str(pint.UnitRegistry().parse_expression(physical).units)
        except ModuleNotFoundError:
            checked = None
    return {
        **label,
        "physical_unit": checked or physical,
        "label_method": "pint parse (labelling only)"
        if checked
        else "declared unit table (pint not installed)",
    }


def reporting_area(scheme: str, code: Any) -> str | None:
    """A reporting-area code normalised the same way on every side of a match (statistics, ALKIS features)."""
    from src.ingestion.demographic_sources import geography_code

    return geography_code(scheme, code)


def plan_key(plan_id: Any) -> str | None:
    """A Berlin plan number normalised for matching (case and spacing), keeping its published form elsewhere."""
    text = "".join(str(plan_id or "").split()).upper()
    return text or None


# ---------------------------------------------------------------------- WFS layer declarations


def layer_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    """The ``housing`` block of a WFS source: which published attribute carries which housing field."""
    declared = dict(source.get("housing") or {})
    layer = declared.get("layer")
    attributes = dict(declared.get("attributes") or {})
    if layer not in LAYERS:
        raise SourcePackError(
            "invalid_manifest", f"a housing WFS source declares a layer from {LAYERS}"
        )
    required = {
        "land-value": ("zone_id", "land_value", "valuation_date"),
        "development-plan": ("plan_id", "stage"),
        "residential-area": ("category",),
    }[layer]
    missing = [key for key in required if not attributes.get(key)]
    if missing:
        raise SourcePackError(
            "invalid_manifest",
            "housing layer attributes missing: " + ", ".join(missing),
        )
    if layer == "land-value":
        unit = declared.get("unit") or {}
        if not (attributes.get("unit") or unit.get("published")):
            raise SourcePackError(
                "invalid_manifest",
                "a land-value layer declares its unit or unit attribute",
            )
        if unit.get("published"):
            try:
                check_unit(unit["published"])
            except HousingFormatError as exc:
                raise SourcePackError("invalid_manifest", str(exc)) from exc
    if layer == "development-plan":
        stages = dict(declared.get("stages") or {})
        if not stages or set(stages.values()) - set(PLAN_STAGES):
            raise SourcePackError(
                "invalid_manifest",
                f"a plan layer maps each published stage label to one of {PLAN_STAGES}",
            )
        for code in dict(declared.get("stage_dates") or {}):
            if code not in PLAN_STAGES:
                raise SourcePackError(
                    "invalid_manifest", "stage dates are declared per known stage"
                )
        for scheme in dict(declared.get("reference_attributes") or {}).values():
            if dict(scheme).get("scheme") not in REFERENCE_SCHEMES:
                raise SourcePackError(
                    "invalid_manifest", "a reference attribute names a known scheme"
                )
    if layer == "residential-area":
        edition = dict(declared.get("edition") or {})
        if not edition.get("edition_id") or not day(edition.get("valid_from")):
            raise SourcePackError(
                "invalid_manifest",
                "a residential-area layer names its Mietspiegel edition and valid_from date",
            )
    return {
        **declared,
        "attributes": attributes,
        "namespace": str(declared.get("namespace") or "global"),
    }


# ---------------------------------------------------------------------- tabular publications


def _csv(raw: bytes) -> list[dict[str, str]]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252")
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise HousingFormatError("schema_drift", "file has no header row")
    delimiter = max((";", ",", "\t"), key=lines[0].count)
    reader = csv.DictReader(io.StringIO("\n".join(lines)), delimiter=delimiter)
    rows = []
    for row in reader:
        if None in row or any(v is None for v in row.values()):
            raise HousingFormatError(
                "schema_drift", "a row does not have the header's cells"
            )
        rows.append({str(k).strip(): (v or "").strip() for k, v in row.items()})
        if len(rows) > MAX_ROWS:
            raise HousingFormatError(
                "input_limit", "file has more rows than the parser accepts"
            )
    return rows


def _check_columns(rows: list[dict[str, str]], declared: set[str]) -> None:
    if not rows:
        raise HousingFormatError("schema_drift", "the file has no rows")
    if set(rows[0]) != declared:
        raise HousingFormatError(
            "schema_drift",
            f"columns {sorted(rows[0])} are not the declared {sorted(declared)}",
        )


# The official Zeichenerklärung of German statistical tables: the published sign, the value it stands for and its
# meaning. A rent-index table marks a cell without enough data with a dash; it states no value.
STATISTICS_SIGNS: dict[str, tuple[str | None, str]] = {
    "-": ("0", "nothing (exactly zero) as published"),
    "–": ("0", "nothing (exactly zero) as published"),
    ".": (None, "unknown or confidential"),
    "...": (None, "not yet available"),
    "x": (None, "not applicable"),
    "/": (None, "too uncertain to publish"),
}
RENT_INDEX_SIGNS: dict[str, tuple[str | None, str]] = {
    "-": (None, "no value published for this cell"),
    "–": (None, "no value published for this cell"),
}


def _value(
    text: Any, number_format: str, signs: Mapping[str, tuple[str | None, str]]
) -> dict[str, Any]:
    raw = _clean(text)
    if raw is None:
        return {
            "value_text": None,
            "value": None,
            "published": False,
            "sign": None,
            "sign_meaning": "empty cell",
        }
    if raw in signs:
        value, meaning = signs[raw]
        return {
            "value_text": raw,
            "value": value,
            "published": value is not None,
            "sign": raw,
            "sign_meaning": meaning,
        }
    number = parse_decimal(raw, number_format)
    return {
        "value_text": raw,
        "value": None if number is None else str(number),
        "published": number is not None,
        "sign": None,
        "sign_meaning": None,
    }


def parse_rent_index(raw: bytes, *, document: Mapping[str, Any]) -> dict[str, Any]:
    """A Mietspiegel table in its declared layout: one cell per row with its published range fields."""
    layout = dict(document.get("columns") or {})
    dimensions = dict(layout.get("dimensions") or {})
    ranges = {k: layout.get(k) for k in ("lower", "middle", "upper") if layout.get(k)}
    if not layout.get("cell_key") or not ranges or not dimensions:
        raise HousingFormatError(
            "invalid_declaration",
            "a rent-index layout names cell key, dimensions and ranges",
        )
    rows = _csv(raw)
    _check_columns(rows, {layout["cell_key"], *dimensions, *ranges.values()})
    unit = check_unit(document.get("unit"))
    number_format = str(document.get("number_format") or "de")
    cells, seen = [], set()
    for row in rows:
        key = _clean(row[layout["cell_key"]])
        if not key or key in seen:
            raise HousingFormatError(
                "schema_drift", "every cell has one distinct published key"
            )
        seen.add(key)
        cells.append(
            {
                "cell_key": key,
                "dimensions": {
                    name: _clean(row[column])
                    for column, name in sorted(dimensions.items())
                },
                "ranges": {
                    name: _value(row[column], number_format, RENT_INDEX_SIGNS)
                    for name, column in sorted(ranges.items())
                },
                "unit": unit["published"],
                "currency": unit["currency"],
            }
        )
    edition = {
        "edition_id": _clean(document.get("edition_id")),
        "edition": _clean(document.get("edition")),
        "qualifying_date": day(document.get("qualifying_date")),
        "qualifying_date_semantics": _clean(document.get("qualifying_date_semantics"))
        or "Stichtag der Datenerhebung as stated by the edition",
        "valid_from": day(document.get("valid_from")),
        "publication_url": _clean(document.get("publication_url")),
        "page": _clean(document.get("page")),
        "extraction": _clean(document.get("extraction")) or "published table",
    }
    if not edition["edition_id"] or not edition["edition"]:
        raise HousingFormatError(
            "invalid_declaration", "a rent-index document names its edition"
        )
    return {
        "kind": "rent-index",
        "edition": edition,
        "items": cells,
        "published_on": day(document.get("published_on")),
    }


def parse_building_statistics(
    raw: bytes, *, document: Mapping[str, Any]
) -> dict[str, Any]:
    """A permit or completion table per reporting area and period; every column is declared."""
    layout = dict(document.get("columns") or {})
    values = dict(layout.get("values") or {})
    if (
        document.get("statistic") not in STATISTIC_KINDS
        or not values
        or set(values.values()) - set(MEASURES)
        or not layout.get("geography")
        or not layout.get("period")
    ):
        raise HousingFormatError(
            "invalid_declaration",
            "a building-statistics layout names statistic, geography, period and measures",
        )
    rows = _csv(raw)
    declared = {layout["geography"], layout["period"], *values}
    if layout.get("geography_label"):
        declared.add(layout["geography_label"])
    _check_columns(rows, declared)
    units = {
        measure: check_unit(unit)["published"]
        for measure, unit in dict(document.get("units") or {}).items()
    }
    if set(values.values()) - set(units):
        raise HousingFormatError(
            "invalid_declaration", "every measure declares its unit"
        )
    totals = {
        str(k): dict(v) for k, v in dict(document.get("total_codes") or {}).items()
    }
    number_format = str(document.get("number_format") or "de")
    items, seen = [], set()
    for row in rows:
        code = _clean(row[layout["geography"]])
        if code in totals:
            area = {
                "scheme": totals[code]["scheme"],
                "code": totals[code]["code"],
                "label": _clean(row.get(layout.get("geography_label") or "")),
                "published_code": code,
            }
        else:
            normalised = reporting_area("berlin-bezirk", code)
            if not normalised:
                raise HousingFormatError(
                    "schema_drift", "a row states no reporting-area code"
                )
            area = {
                "scheme": "berlin-bezirk",
                "code": normalised,
                "label": _clean(row.get(layout.get("geography_label") or "")),
                "published_code": code,
            }
        period = _clean(row[layout["period"]])
        if not period or not re.fullmatch(r"\d{4}(-(0[1-9]|1[0-2]|Q[1-4]))?", period):
            raise HousingFormatError(
                "schema_drift", f"reference period {period!r} is not a declared form"
            )
        for column, measure in sorted(values.items()):
            key = (area["scheme"], area["code"], period, measure)
            if key in seen:
                raise HousingFormatError(
                    "schema_drift", "a table states one area, period and measure twice"
                )
            seen.add(key)
            items.append(
                {
                    "statistic": document["statistic"],
                    "reporting_area": area,
                    "period": period,
                    "measure": measure,
                    "unit": units[measure],
                    **_value(row[column], number_format, STATISTICS_SIGNS),
                    "locator": {"column": column},
                }
            )
    return {
        "kind": "building-statistics",
        "items": items,
        "published_on": day(document.get("published_on")),
    }


def parse_genesis(
    data_raw: bytes, meta_raw: bytes, *, document: Mapping[str, Any], url: str
) -> dict[str, Any]:
    """A GENESIS 31111/31231 table through the dataset connector; values stay in dataset-series-v1 records."""
    from src.ingestion.connectors.dataset.genesis import (
        GenesisConnector,
        GenesisFormatError,
    )

    try:
        records = GenesisConnector.parse_table(
            data_raw, meta_raw, document, source_url=url
        )
    except GenesisFormatError as exc:
        raise HousingFormatError(exc.code, str(exc)) from exc
    items = [record.to_dict() for record in records]
    published_on = items[0]["metadata"]["published_on"] if items else None
    return {
        "kind": "housing-indicator",
        "items": items,
        "published_on": published_on,
        "published_at": items[0]["metadata"]["published_at"] if items else None,
        "release_basis": "genesis_table_updated",
    }


def _last_modified(headers: Mapping[str, Any]) -> str | None:
    value = headers.get("last-modified")
    if not value:
        return None
    try:
        return parsedate_to_datetime(str(value)).date().isoformat()
    except (TypeError, ValueError):
        return None


def parse_publication(
    format_id: str,
    raw: bytes,
    *,
    document: Mapping[str, Any],
    headers: Mapping[str, Any] | None = None,
    url: str = "",
    metadata: bytes | None = None,
) -> dict[str, Any]:
    if format_id == "rent-index-table-csv":
        parsed = parse_rent_index(raw, document=document)
    elif format_id == "statbb-building-csv":
        parsed = parse_building_statistics(raw, document=document)
    elif format_id == "destatis-genesis-ffcsv":
        parsed = parse_genesis(raw, metadata or b"", document=document, url=url)
    else:
        raise HousingFormatError(
            "schema_drift", f"unknown housing format {format_id!r}"
        )
    basis = parsed.get("release_basis") or (
        "declared_publication" if parsed.get("published_on") else None
    )
    if parsed.get("published_on") is None:
        parsed["published_on"] = _last_modified(
            {str(k).casefold(): v for k, v in dict(headers or {}).items()}
        )
        basis = "http_last_modified" if parsed["published_on"] else None
    if parsed["published_on"] is None:
        # A retrieval time would pose as a publication date; the document is refused instead.
        raise HousingFormatError(
            "schema_drift", "the publication states no publication date"
        )
    return {
        "contract": PUBLICATION_CONTRACT,
        "provider": FORMATS[format_id]["provider"],
        "format": format_id,
        "kind": parsed["kind"],
        "published_on": parsed["published_on"],
        "published_at": parsed.get("published_at"),
        "publication_basis": basis,
        "edition": parsed.get("edition"),
        "file_sha256": hashlib.sha256(raw + (metadata or b"")).hexdigest(),
        "content_sha256": _digest([parsed.get("edition"), parsed["items"]]),
        "items": parsed["items"],
    }


def document_url(
    document: Mapping[str, Any], endpoint: str, *, part: str = "data"
) -> str:
    if document.get("url"):
        return str(document["url"])
    if document.get("table"):
        from src.ingestion.connectors.dataset.genesis import table_urls

        return table_urls(endpoint, str(document["table"]))[part]
    return ""


def tabular_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    declared = dict(source.get("housing") or {})
    format_id = declared.get("format")
    if format_id not in FORMATS or FORMATS[format_id]["provider"] != declared.get(
        "provider"
    ):
        raise SourcePackError(
            "invalid_manifest", "housing sources declare a matching provider and format"
        )
    documents = list(declared.get("documents") or [])
    if not documents:
        raise SourcePackError(
            "invalid_manifest", "a housing source declares its documents"
        )
    if len(documents) > int(dict(source.get("budgets") or {}).get("max_pages", 1)):
        raise SourcePackError(
            "invalid_manifest", "more declared documents than the source's page budget"
        )
    host = (urlsplit(source["endpoint"]).hostname or "").casefold()
    urls = [document_url(d, source["endpoint"]) for d in documents]
    if len(set(urls)) != len(urls):
        raise SourcePackError(
            "invalid_manifest", "each declared document is a distinct publication"
        )
    for document, url in zip(documents, urls):
        parts = urlsplit(url)
        if parts.scheme != "https" or (parts.hostname or "").casefold() != host:
            raise SourcePackError(
                "invalid_manifest",
                "declared documents are HTTPS resources on the endpoint's host",
            )
        if format_id == "destatis-genesis-ffcsv" and not re.fullmatch(
            r"(31111|31231)-\d{4}", str(document.get("table") or "")
        ):
            raise SourcePackError(
                "invalid_manifest",
                "GENESIS housing documents name a 31111 or 31231 table",
            )
        for reference in document.get("references") or []:
            if (
                reference.get("scheme") not in REFERENCE_SCHEMES
                or reference.get("relation") not in LINK_RELATIONS
            ):
                raise SourcePackError(
                    "invalid_manifest", "a reference names a known scheme and relation"
                )
    return {**declared, "namespace": str(declared.get("namespace") or "global")}


def unverified(provider: str) -> bool:
    return (
        PROVIDER_CONTRACTS.get(provider, {}).get("access_decision") != "verified-live"
    )


class HousingAdapter:
    """Fetch declared housing publications on the runtime's default transport; one page per document."""

    accepts_transport = True
    connector = CONNECTOR

    def __init__(
        self,
        source: Mapping[str, Any],
        *,
        transport: Callable[..., Mapping[str, Any]] | None = None,
        secret: str | None = None,
    ) -> None:
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        self.source = json.loads(json.dumps(source))
        self.declared = tabular_declaration(self.source)
        self.secret = secret
        if transport is None:
            from functools import partial

            # The runtime's default transport: same-host public redirects only, a byte ceiling and the timeout.
            transport = partial(
                HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"])
            )
        self.transport = transport
        self.definition = {
            "contract": ADAPTER_CONTRACT,
            "source_id": source["source_id"],
            "connector": source["connector"],
            "endpoint": source["endpoint"],
            "operations": list(source["operations"]),
            "source_hash": source["source_hash"],
            "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"],
            "limits": source["budgets"],
            "housing": {
                "provider": self.declared["provider"],
                "format": self.declared["format"],
                "keyed": bool(secret),
            },
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _check(self, request: Mapping[str, Any]) -> None:
        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError(
                "operation_forbidden", "operation is not declared by the source"
            )
        if set(request) - {"operation", "parameters", "limit", "from_ms", "to_ms"}:
            raise SourcePackError(
                "parameter_forbidden", "runtime adapter received undeclared controls"
            )
        if dict(request.get("parameters") or {}):
            raise SourcePackError(
                "parameter_forbidden", "housing runs fetch the declared documents only"
            )

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json, text/csv, text/plain"}
        if self.declared["format"] == "destatis-genesis-ffcsv":
            if not self.secret:
                raise SourcePackError(
                    "authentication_failed", "GENESIS requires its credential"
                )
            # GENESIS token login: the token as user name, an empty password (verify).
            headers["username"] = self.secret
            headers["password"] = ""
        return headers

    def _get(
        self, url: str, headers: Mapping[str, str]
    ) -> tuple[bytes, dict[str, Any], str]:
        from src.ingestion.source_pack_runtime import _retry_after_ms

        host = (urlsplit(self.source["endpoint"]).hostname or "").casefold()
        parts = urlsplit(url)
        if (parts.hostname or "").casefold() != host or parts.scheme != "https":
            raise SourcePackError(
                "network_policy",
                "declared documents are fetched from the endpoint's host only",
            )
        base, _, query = url.partition("?")
        response = self.transport(
            url=base,
            params=dict(parse_qsl(query, keep_blank_values=True)),
            headers=dict(headers),
            timeout=int(self.definition["limits"]["timeout_ms"]) / 1000,
        )
        final_host = (
            urlsplit(str(response.get("final_url") or url)).hostname or ""
        ).casefold()
        if final_host != host:
            raise SourcePackError(
                "network_policy", "response was served from another host"
            )
        status = int(response.get("status", 200))
        response_headers = {
            str(k).casefold(): v for k, v in dict(response.get("headers") or {}).items()
        }
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError(
                "response_too_large", "response exceeds its byte limit"
            )
        if status == 429:
            raise SourcePackError(
                "rate_limited",
                "provider quota is temporarily exhausted",
                retry_after_ms=_retry_after_ms(response_headers.get("retry-after")),
            )
        if status in {401, 403}:
            raise SourcePackError(
                "authentication_failed", f"request refused (HTTP {status})"
            )
        if status >= 500:
            raise SourcePackError(
                "source_unavailable", f"provider returned HTTP {status}"
            )
        if status >= 400:
            raise SourcePackError("schema_drift", f"request returned HTTP {status}")
        origin = "fixture" if response.get("origin") == "fixture" else "live"
        return raw, response_headers, origin

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage

        self._check(request)
        documents = list(self.declared["documents"])
        index = 0 if cursor is None else int(cursor) if str(cursor).isdigit() else -1
        if not 0 <= index < len(documents):
            raise SourcePackError("cursor_drift", "cursor names no declared document")
        document = dict(documents[index])
        fmt = self.declared["format"]
        headers = self._headers()
        endpoint = self.source["endpoint"]
        metadata, origins = None, []
        if fmt == "destatis-genesis-ffcsv":
            metadata, _meta_headers, origin = self._get(
                document_url(document, endpoint, part="metadata"), headers
            )
            origins.append(origin)
        url = document_url(document, endpoint)
        raw, response_headers, origin = self._get(url, headers)
        origins.append(origin)
        try:
            publication = parse_publication(
                fmt,
                raw,
                document=document,
                headers=response_headers,
                url=url,
                metadata=metadata,
            )
        except HousingFormatError as exc:
            raise SourcePackError(
                "response_too_large" if exc.code == "input_limit" else "schema_drift",
                f"{exc.code}: {exc}",
            ) from exc
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        if len(publication["items"]) > limit:
            # Never a truncated publication: a missing cell would read as a cell that was not published.
            raise SourcePackError(
                "budget_exhausted",
                "publication has more items than the run's result budget",
            )
        header = {
            key: publication[key]
            for key in (
                "contract",
                "provider",
                "format",
                "kind",
                "published_on",
                "published_at",
                "publication_basis",
                "edition",
                "file_sha256",
                "content_sha256",
            )
        } | {
            "document": document,
            "item_count": len(publication["items"]),
            # Only a fixture transport says so; the runtime's HTTPS transport is live evidence.
            "evidence_origin": "fixture" if set(origins) == {"fixture"} else "live",
            "url": url,
        }
        records = [
            {
                "id": f"{publication['file_sha256'][:16]}:{number}",
                "title": f"{document.get('label') or publication['provider']} ({publication['published_on']})",
                "url": url,
                "language": "de",
                "published_at": publication["published_on"],
                "content": json.dumps(item, sort_keys=True, ensure_ascii=False),
                "housing_publication": header,
                "housing_item": item,
            }
            for number, item in enumerate(publication["items"])
        ]
        receipt = {
            "status": 200,
            "provider": publication["provider"],
            "document": document.get("label"),
            "published_on": publication["published_on"],
            "publication_basis": publication["publication_basis"],
            "file_sha256": publication["file_sha256"],
            "items": len(records),
            "evidence_origin": header["evidence_origin"],
            "final_page": index + 1 >= len(documents),
        }
        next_cursor = str(index + 1) if index + 1 < len(documents) else None
        return RuntimePage(tuple(records), next_cursor, len(raw), receipt=receipt)


FIXTURE_SECRET = "fixture-credential-not-a-real-key"
ADAPTERS = {CONNECTOR: HousingAdapter}


def fixture_transport(
    pages: Sequence[Mapping[str, Any]],
) -> Callable[..., Mapping[str, Any]]:
    """Replay authored responses keyed by URL path (+ sorted query); responses are marked as fixture evidence."""
    by_key = {page["request"]: page for page in pages}

    def transport(*, url, params, headers, timeout):
        del headers, timeout
        query = urlencode(sorted(dict(params or {}).items()))
        key = urlsplit(url).path + ("?" + query if query else "")
        page = by_key.get(key)
        if page is None:
            raise SourcePackError("fixture_missing", f"no native page for {key}")
        body = page.get("body")
        content = (
            body.encode()
            if isinstance(body, str)
            else b""
            if body is None
            else json.dumps(body).encode()
        )
        return {
            "status": int(page.get("status", 200)),
            "headers": dict(page.get("headers") or {}),
            "content": content,
            "origin": "fixture",
            **({"final_url": page["final_url"]} if page.get("final_url") else {}),
        }

    return transport


def fixture_request(
    document: Mapping[str, Any], endpoint: str = "", *, part: str = "data"
) -> str:
    """The key :func:`fixture_transport` files a response under (path and sorted query)."""
    parts = urlsplit(document_url(document, endpoint, part=part))
    query = urlencode(sorted(parse_qsl(parts.query)))
    return parts.path + ("?" + query if query else "")


def replay_native_fixture(
    source: Mapping[str, Any], fixture: Mapping[str, Any]
) -> list[dict[str, Any]]:
    adapter = HousingAdapter(
        source,
        transport=fixture_transport(list(fixture["native_pages"])),
        secret=FIXTURE_SECRET,
    )
    records, cursor = [], None
    while True:
        page = adapter.fetch_page(
            {
                "operation": min(source["operations"]),
                "parameters": {},
                "limit": int(source["budgets"]["max_results"]),
            },
            cursor=cursor,
        )
        records += [dict(item) for item in page.records]
        cursor = page.next_cursor
        if cursor is None:
            return records
