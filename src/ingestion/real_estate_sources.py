"""Real-estate transactions, price indices and cadastral parcels for the Geospatial ``real-estate`` feature (#2228).

RE01 (#2460) records the access contract of every audited source in
:data:`PROVIDER_CONTRACTS` (documented in
``docs/roadmaps/geospatial-real-estate-source-audit.md``), the reuse conditions
that affect individuals (:data:`REUSE_CONDITIONS`), the bounded places, periods
and parcel sample (:data:`BOUNDED_COVERAGE`) and the live status of each source
(:data:`LIVE_VERIFICATION`). Every provider is ``unverified-live`` until a dated
bounded run (RE13, #2519).

Two acquisition paths, both through :mod:`src.ingestion.source_pack_runtime`
(source pack ``geospatial-real-estate``, ``packs/geospatial/source_packs/``):

* **Tabular publications** use the native ``real-estate`` connector
  (:class:`RealEstateAdapter`), one declared document per page, on the
  runtime's same-host HTTPS transport:

  - ``hmlr-ppd`` (RE03) - the HM Land Registry Price Paid Data monthly change
    file (16 columns, no header). Rows are kept only for the declared postcode
    districts. Record status ``A``/``C``/``D`` becomes an *added*, *changed*
    or *withdrawn* event; the file's ``Last-Modified`` month is its release.
  - ``hmlr-ukhpi`` (RE03) - a UK House Price Index full file of one monthly
    release; rows for the declared geography codes give index and average
    price observations as published, with the release as vintage.
  - ``dvf`` (RE05) - the Etalab geolocated DVF file of one commune and year of
    one semi-annual release. Rows are grouped by ``id_mutation``; the published
    ``valeur_fonciere`` is kept once per mutation (never apportioned across
    parcels or lots) with every ``id_parcelle`` it names.
  - ``eurostat-hpi`` (RE06) - Eurostat ``prc_hpi_q`` JSON-stat cubes through the
    existing :class:`~src.ingestion.connectors.dataset.eurostat.EurostatConnector`
    (``parse_cells`` keeps every flag); the cube's ``updated`` stamp is the
    vintage and the unit code with its label is the series edition.

* **INSPIRE cadastral parcels** (RE04) use the existing ``wfs`` connector
  (:mod:`src.ingestion.wfs_api`, :mod:`src.ingestion.geojson_features`), pinned
  to the RE01 bounding box and to the declared properties (``PROPERTYNAME``;
  anything else the service returns - rights holders included - is dropped
  before a record leaves the adapter). :class:`src.kb.real_estate.RealEstateProjector`
  projects the features into the Geospatial feature store and reads
  :func:`parcel_declaration` attributes from the stored feature revision.

Nothing is derived: prices keep their published text and currency; no price is
apportioned, converted, averaged, deflated, rebased or interpolated; no owner or
party name is read from any source (none of the selected files publishes one,
and a column naming one is refused).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlencode, urlsplit

from src.ingestion.source_packs import SourcePackError

CONNECTOR = "real-estate"
ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
TARGET_SCHEMA = "noesis-real-estate-record-v1"
PROVIDERS = ("hmlr-ppd", "hmlr-ukhpi", "dvf", "eurostat-hpi")
PARCEL_PROVIDERS = ("inspire-cp-fr", "inspire-cp-de-nw")
PROVIDER_HOSTS = {
    "hmlr-ppd": ("price-paid-data.publicdata.landregistry.gov.uk",),
    "hmlr-ukhpi": ("publicdata.landregistry.gov.uk",),
    "dvf": ("files.data.gouv.fr",),
    "eurostat-hpi": ("ec.europa.eu",),
}
MAX_ROWS = 200_000
MAX_GEOS = 10
EUROSTAT_ATTRIBUTION = "Source: Eurostat (reuse authorised, Commission Decision 2011/833/EU)."
OGL_ATTRIBUTION = ("Contains HM Land Registry data (c) Crown copyright and database right. This data is licensed under "
                   "the Open Government Licence v3.0.")
DVF_ATTRIBUTION = "Demandes de valeurs foncières géolocalisées, Etalab / DGFiP (Licence Ouverte 2.0)"
# Column names that would carry an owner, buyer, seller or rights holder. None of the selected publications
# has one; a file or feature that does is refused (tabular) or has the attribute dropped (WFS).
PARTY_COLUMNS = frozenset({
    "owner", "owners", "proprietaire", "propriétaire", "acquereur", "acquéreur", "vendeur", "buyer", "seller",
    "rights_holder", "rightholder", "eigentuemer", "eigentümer", "name_owner", "titulaire",
})
EXCLUSIONS = (
    "No property valuation, price estimate, per-square-metre price presented as a price, interpolated or averaged "
    "value, owner or party profiling, re-identification of individuals or investment advice. Prices and indices are "
    "shown as published, side by side, with their currency, unit, period and vintage."
)

# RE01 access decisions. Endpoints, file layouts, type names, field names and terms are recorded from the
# publishers' documentation as known without network access; every item marked ``verify`` must be checked
# against the live service and terms before a dated live run is accepted (RE13, #2519).
PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "hmlr-ppd": {
        "publisher": "HM Land Registry (Price Paid Data)",
        "delivers": ["transaction"],
        "access": "monthly change file over HTTPS, pp-monthly-update-new-version.csv (verify host; the complete and "
                  "yearly files and the linked-data report builder are alternatives not used)",
        "endpoint": "https://price-paid-data.publicdata.landregistry.gov.uk/pp-monthly-update-new-version.csv",
        "format": "CSV, 16 columns, no header row: transaction unique identifier, price, date of transfer, postcode, "
                  "property type (D/S/T/F/O), old/new (Y/N), duration (F/L/U), PAON, SAON, street, locality, "
                  "town/city, district, county, PPD category type (A/B), record status (A/C/D)",
        "identifiers": "transaction unique identifier ({GUID}), stable across additions, changes and deletions",
        "authentication": "none",
        "licence": "Open Government Licence v3.0, with the HM Land Registry attribution statement",
        "terms_url": "https://www.gov.uk/government/statistical-data-sets/price-paid-data-downloads",
        "attribution": OGL_ATTRIBUTION,
        "rate_limits": "none published; one file per monthly release (about 100k rows, verify)",
        "cadence": "monthly, on the 20th working day (verify); the file carries additions (A), changes (C) and "
                   "deletions (D) of earlier rows",
        "versioning": "the file's Last-Modified month is the release; C rows are revisions and D rows withdraw a "
                      "transaction without erasing its history",
        "personal_data": "addresses of sold properties are published; no buyer, seller or owner is. PPD terms "
                         "(verify) restrict using address data to contact or profile individuals",
        "access_decision": "unverified-live",
    },
    "hmlr-ukhpi": {
        "publisher": "HM Land Registry with ONS and Registers of Scotland (UK House Price Index)",
        "delivers": ["price_index_observation"],
        "access": "UK HPI full file per monthly release over HTTPS (verify path "
                  "/market-trend-data/house-price-index-data/UK-HPI-full-file-YYYY-MM.csv)",
        "format": "CSV with header: Date, RegionName, AreaCode, AveragePrice, Index, IndexSA, 1m%Change, "
                  "12m%Change, AveragePriceSA, SalesVolume, property-type prices (verify column names)",
        "identifiers": "ONS GSS area codes (E09..., E92000001, K02000001) and the month (verify)",
        "authentication": "none",
        "licence": "Open Government Licence v3.0",
        "terms_url": "https://www.gov.uk/government/collections/uk-house-price-index-reports",
        "attribution": OGL_ATTRIBUTION,
        "rate_limits": "none published; one file per release",
        "cadence": "monthly; the latest months are revised in later releases",
        "versioning": "each monthly release is a vintage; a changed value for an earlier month is a revision; index "
                      "reference Jan 2015 = 100 as published",
        "personal_data": "none (aggregates)",
        "access_decision": "unverified-live",
    },
    "dvf": {
        "publisher": "Direction générale des Finances publiques (DVF), geolocated by Etalab (DVF géolocalisées)",
        "delivers": ["transaction"],
        "access": "per-commune CSV per year and release on files.data.gouv.fr/geo-dvf/{release}/csv/{year}/communes/"
                  "{dep}/{commune}.csv (verify release folder naming; the raw DGFiP files are pipe-separated "
                  "national extracts not used)",
        "format": "CSV with header; one row per disposition, parcel and local: id_mutation, date_mutation, "
                  "numero_disposition, nature_mutation, valeur_fonciere, adresse_*, code_postal, code_commune, "
                  "id_parcelle, lot counts, type_local, surface_reelle_bati, nombre_pieces_principales, "
                  "nature_culture, surface_terrain, longitude, latitude",
        "identifiers": "id_mutation (Etalab mutation id), id_parcelle (14-character cadastral id: INSEE commune, "
                       "prefix, section, number)",
        "authentication": "none",
        "licence": "Licence Ouverte / Open Licence 2.0 with the DVF conditions (see reuse conditions)",
        "terms_url": "https://www.data.gouv.fr/fr/datasets/demandes-de-valeurs-foncieres-geolocalisees/",
        "attribution": DVF_ATTRIBUTION,
        "rate_limits": "none published; one file per commune, year and release",
        "cadence": "semi-annual (April and October), covering the last five years",
        "versioning": "each semi-annual release is a vintage; a mutation changed between releases is a revision and "
                      "one absent from the same commune-year file of a later release is a dated removal",
        "personal_data": "no names; transactions are re-identifiable through address and parcel, so re-identification "
                         "and indexing by external search engines are forbidden (décret 2018-1350, verify)",
        "access_decision": "unverified-live",
    },
    "eurostat-hpi": {
        "publisher": "Eurostat (House price index, prc_hpi_q)",
        "delivers": ["price_index_observation"],
        "access": "Eurostat dissemination API, JSON-stat 2.0, through the existing EurostatConnector",
        "endpoint": "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/prc_hpi_q",
        "format": "JSON-stat 2.0 cube with dimensions freq, purchase, unit, geo, time; status flags p, e, b, d",
        "identifiers": "dataset code, GEO code, purchase and unit codes",
        "authentication": "none",
        "licence": "Eurostat reuse policy (Commission Decision 2011/833/EU), reuse with attribution",
        "terms_url": "https://ec.europa.eu/eurostat/about-us/policies/copyright",
        "attribution": EUROSTAT_ATTRIBUTION,
        "rate_limits": "undocumented soft limits (verify); one request per declared GEO",
        "cadence": "quarterly, with revisions of earlier quarters",
        "versioning": "the cube's updated stamp is the vintage; a rebased index is a new unit code and label, kept "
                      "as a new series edition and never recomputed",
        "personal_data": "none (aggregates)",
        "access_decision": "unverified-live",
    },
    "inspire-cp-fr": {
        "publisher": "IGN / DGFiP (INSPIRE Cadastral Parcels, France, Géoplateforme)",
        "delivers": ["parcel"],
        "access": "WFS 2.0.0 GetFeature on data.geopf.fr/wfs/ows, type CP.CadastralParcel (verify type name), "
                  "GeoJSON output, pinned bbox and property list",
        "crs": "requested as ETRS89 / UTM 31N (EPSG:25831) so offline projection needs no pyproj (verify the service "
               "offers it; native Lambert-93 EPSG:2154 otherwise, which needs pyproj)",
        "identifiers": "INSPIRE localId and namespace; nationalCadastralReference = the 14-character id_parcelle",
        "authentication": "none",
        "licence": "Licence Ouverte 2.0 (verify for the INSPIRE CP layer)",
        "terms_url": "https://geoservices.ign.fr/",
        "attribution": "IGN - DGFiP, Parcellaire (INSPIRE CP)",
        "rate_limits": "undocumented (verify); COUNT/STARTINDEX paging, page size 50",
        "cadence": "quarterly to semi-annual republication (verify)",
        "versioning": "a changed geometry or reference is a new parcel revision; beginLifespanVersion as published",
        "personal_data": "no owner or rights-holder attributes are requested or kept",
        "access_decision": "unverified-live",
    },
    "inspire-cp-de-nw": {
        "publisher": "Geobasis NRW (INSPIRE Flurstücke / Cadastral Parcels, Nordrhein-Westfalen)",
        "delivers": ["parcel"],
        "access": "WFS 2.0.0 GetFeature on www.wfs.nrw.de/geobasis/wfs_nw_inspire-flurstuecke_alkis, type "
                  "cp:CadastralParcel (verify), GeoJSON output (verify availability), pinned bbox and property list",
        "crs": "ETRS89 / UTM 32N (EPSG:25832), the service's native CRS",
        "identifiers": "INSPIRE localId and namespace; nationalCadastralReference (Flurstückskennzeichen)",
        "authentication": "none",
        "licence": "Datenlizenz Deutschland - Zero 2.0",
        "terms_url": "https://www.govdata.de/dl-de/zero-2-0",
        "attribution": "Geobasis NRW (dl-de/zero-2-0)",
        "rate_limits": "undocumented (verify)",
        "cadence": "continuously updated cadastre (verify refresh)",
        "versioning": "a changed geometry or reference is a new parcel revision",
        "personal_data": "the INSPIRE CP schema has no owner attribute; nothing else is requested",
        "access_decision": "unverified-live",
    },
    "inspire-cp-nl": {
        "publisher": "Kadaster (INSPIRE Cadastral Parcels, PDOK)",
        "delivers": ["parcel"],
        "access": "WFS 2.0.0 on service.pdok.nl/kadaster/cp/wfs/v1_0 (verify)",
        "crs": "EPSG:28992 / EPSG:4258 (needs pyproj for projection)",
        "identifiers": "INSPIRE localId, nationalCadastralReference",
        "licence": "CC BY 4.0 (verify)",
        "access_decision": "not-selected",
        "reason": "outside the RE01 bounded countries (no linked transaction source for the Netherlands)",
    },
    "hmlr-inspire-index-polygons": {
        "publisher": "HM Land Registry (INSPIRE Index Polygons)",
        "delivers": ["parcel"],
        "access": "per-local-authority GML zip download (Atom-style listing); no WFS",
        "crs": "EPSG:27700 (needs pyproj)",
        "identifiers": "INSPIRE ID only; no title number, no link to PPD transactions",
        "licence": "INSPIRE Index Polygons licence (OGL with restrictions; verify)",
        "access_decision": "not-implemented",
        "reason": "no WFS path, no identifier shared with Price Paid Data, and the GML bulk download exceeds the "
                  "RE04 boundary; PPD transactions stay at postcode and address level",
    },
}
REUSE_CONDITIONS = {
    "hmlr-ppd": {
        "conditions": ["attribute HM Land Registry under OGL v3.0",
                       "do not use address data to contact, target or profile individuals (PPD terms, verify)"],
        "record_design": "addresses stored as published for place matching; no buyer, seller or owner field exists "
                         "or is accepted; no query takes a person's name",
    },
    "dvf": {
        "conditions": ["no re-identification of the persons concerned, directly or indirectly",
                       "no indexing of the data by external search engines",
                       "reuse under Licence Ouverte 2.0 with the DGFiP source cited (décret 2018-1350, verify)"],
        "record_design": "no name is published or stored; answers are by place code or parcel id only; exports carry "
                         "the no-re-identification notice; nothing is published for indexing",
    },
    "inspire-cp": {
        "conditions": ["owner and rights-holder attributes are never requested (PROPERTYNAME) nor kept"],
        "record_design": "parcels keep identifiers, label, area and lifespan dates as published",
    },
}
DVF_NOTICE = ("DVF: re-identification of the persons concerned and indexing by external search engines are "
              "forbidden; reuse cites DGFiP / Etalab.")
BOUNDED_COVERAGE = {
    "hmlr-ppd": {"places": ["postcode districts of one London borough (fixtures: ZZ1, ZZ2 - fictional)"],
                 "periods": "the monthly change files of the verification window"},
    "hmlr-ukhpi": {"places": ["the borough (GSS code) and England"], "periods": "the last 24 months of two releases"},
    "dvf": {"places": ["Paris 4e arrondissement (INSEE 75104)"], "periods": "one year in two semi-annual releases"},
    "eurostat-hpi": {"places": ["FR", "DE"], "periods": "quarters since 2098-Q1 (fixtures); unit I15_Q, TOTAL"},
    "inspire-cp-fr": {"places": ["a bounding box in Paris 4e arrondissement (EPSG:25831)"],
                      "parcel_sample": "at most 50 parcels, including those DVF mutations name"},
    "inspire-cp-de-nw": {"places": ["a bounding box in Köln-Altstadt (EPSG:25832)"],
                         "parcel_sample": "at most 50 parcels"},
    "live_verification_budget": "one run per source, within its declared byte and page budgets",
}
LIVE_VERIFICATION = {
    provider: {"status": "unverified-live", "intended": "live-verified after a dated bounded run (RE13, #2519)",
               "note": "no dated live run from this runtime; offline fixtures only"}
    for provider in (*PROVIDERS, *PARCEL_PROVIDERS)
}
PPD_COLUMNS = ("transaction_id", "price", "date_of_transfer", "postcode", "property_type", "old_new", "duration",
               "paon", "saon", "street", "locality", "town_city", "district", "county", "ppd_category",
               "record_status")
PPD_PROPERTY_TYPES = {"D": "Detached", "S": "Semi-Detached", "T": "Terraced", "F": "Flats/Maisonettes", "O": "Other"}
PPD_DURATIONS = {"F": "Freehold", "L": "Leasehold", "U": "Unknown"}
PPD_CATEGORIES = {"A": "Standard Price Paid", "B": "Additional Price Paid"}
PPD_EVENTS = {"A": "added", "C": "changed", "D": "withdrawn"}
UKHPI_MEASURES = {"Index": ("index", "index (reference Jan 2015 = 100, as published)", None),
                  "AveragePrice": ("average_price", "GBP", "GBP"),
                  "SalesVolume": ("sales_volume", "count", None)}
DVF_REQUIRED = ("id_mutation", "date_mutation", "nature_mutation", "valeur_fonciere", "code_commune", "id_parcelle")


class RealEstateFormatError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def published_money(text: Any, currency: str, *, decimal_comma: bool = False) -> dict[str, Any]:
    """A price exactly as published: its text, the decimal it states and its currency; never rounded or converted."""
    raw = _text(text)
    if raw is None:
        return {"value_text": None, "value": None, "currency": currency}
    normal = raw.replace(" ", "").replace(" ", "")
    if decimal_comma:
        normal = normal.replace(",", ".")
    try:
        number = Decimal(normal)
    except InvalidOperation as exc:
        raise RealEstateFormatError("schema_drift", f"price is not a number: {raw!r}") from exc
    return {"value_text": raw, "value": str(number), "currency": currency}


def _day(value: Any) -> str | None:
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", _text(value) or "")
    return match.group(1) if match else None


def _csv_rows(raw: bytes, *, header: bool, delimiter: str = ",") -> list[dict[str, str]] | list[list[str]]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise RealEstateFormatError("schema_drift", "file is not UTF-8") from exc
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    if len(rows) > MAX_ROWS:
        raise RealEstateFormatError("input_limit", f"file has more than {MAX_ROWS} rows")
    if not header:
        return rows
    if not rows:
        raise RealEstateFormatError("schema_drift", "file has no header row")
    names = [c.strip() for c in rows[0]]
    if {n.casefold() for n in names} & PARTY_COLUMNS:
        raise RealEstateFormatError("party_column", "file publishes an owner or party column; it is not read")
    return [dict(zip(names, row, strict=False)) for row in rows[1:]]


def _source(provider: str, url: str, origin: str, file_sha256: str) -> dict[str, Any]:
    contract = PROVIDER_CONTRACTS[provider]
    return {"provider": provider, "publisher": contract["publisher"], "url": url, "licence": contract["licence"],
            "attribution": contract["attribution"], "evidence_origin": origin, "file_sha256": file_sha256}


def _statement(record_type: str, provider: str, record_key: str, *, event: str, vintage: Mapping[str, Any],
               as_published: Mapping[str, Any], source: Mapping[str, Any], place_refs=(), parcel_refs=(),
               selection_key: str | None = None) -> dict[str, Any]:
    return {"contract": TARGET_SCHEMA, "record_type": record_type, "provider": provider, "record_key": record_key,
            "event": event, "vintage": dict(vintage), "as_published": dict(as_published),
            "place_refs": [dict(r) for r in place_refs], "parcel_refs": [dict(r) for r in parcel_refs],
            "selection_key": selection_key, "source": dict(source)}


def postcode_district(postcode: str | None) -> str | None:
    text = (postcode or "").strip().upper()
    return text.split(" ")[0] if text else None


# ------------------------------------------------------------------ parsers


def parse_ppd(raw: bytes, *, document: Mapping[str, Any], release: Mapping[str, Any], url: str,
              origin: str) -> list[dict[str, Any]]:
    """PPD monthly change rows for the declared postcode districts; status A/C/D as added/changed/withdrawn."""
    districts = {str(d).upper() for d in document.get("postcode_districts") or []}
    source = _source("hmlr-ppd", url, origin, _sha(raw))
    statements = []
    for number, row in enumerate(_csv_rows(raw, header=False), start=1):
        if len(row) != len(PPD_COLUMNS):
            raise RealEstateFormatError("schema_drift", f"row {number} has {len(row)} columns, not 16")
        item = {k: (v.strip() or None) for k, v in zip(PPD_COLUMNS, row, strict=True)}
        if postcode_district(item["postcode"]) not in districts:
            continue
        status = item["record_status"]
        if status not in PPD_EVENTS or item["ppd_category"] not in PPD_CATEGORIES:
            raise RealEstateFormatError("schema_drift", f"row {number} has an unknown record status or category")
        if item["property_type"] not in PPD_PROPERTY_TYPES or item["duration"] not in PPD_DURATIONS:
            raise RealEstateFormatError("schema_drift", f"row {number} has an unknown property type or duration")
        tx = str(item["transaction_id"] or "").strip("{}")
        if not tx:
            raise RealEstateFormatError("schema_drift", f"row {number} has no transaction identifier")
        address = {k: item[k] for k in ("paon", "saon", "street", "locality", "town_city", "district", "county",
                                        "postcode")}
        statements.append(_statement(
            "transaction", "hmlr-ppd", tx, event=PPD_EVENTS[status], vintage=release, source=source,
            as_published={"source_transaction_id": item["transaction_id"],
                           "price": published_money(item["price"], "GBP"),
                           "transfer_date": _day(item["date_of_transfer"]),
                           "transfer_date_text": item["date_of_transfer"],
                           "property_type": {"code": item["property_type"],
                                             "label": PPD_PROPERTY_TYPES[item["property_type"]]},
                           "new_build": item["old_new"] == "Y", "old_new": item["old_new"],
                           "tenure": {"code": item["duration"], "label": PPD_DURATIONS[item["duration"]]},
                           "category": {"code": item["ppd_category"], "label": PPD_CATEGORIES[item["ppd_category"]]},
                           "record_status": status, "address": address},
            place_refs=[{"scheme": "uk-postcode", "code": item["postcode"]},
                        {"scheme": "uk-postcode-district", "code": postcode_district(item["postcode"])}]))
    return statements


def parse_ukhpi(raw: bytes, *, document: Mapping[str, Any], release: Mapping[str, Any], url: str,
                origin: str) -> list[dict[str, Any]]:
    """UK HPI rows for the declared geography codes and months: index, average price and sales volume as published."""
    geographies = {str(g) for g in document.get("geographies") or []}
    since = str(document.get("since") or "")
    rows = _csv_rows(raw, header=True)
    if rows and not {"Date", "RegionName", "AreaCode", "AveragePrice", "Index"} <= set(rows[0]):
        raise RealEstateFormatError("schema_drift", "UK HPI file lacks Date, RegionName, AreaCode, AveragePrice, Index")
    source = _source("hmlr-ukhpi", url, origin, _sha(raw))
    statements = []
    for row in rows:
        code = _text(row.get("AreaCode"))
        if code not in geographies:
            continue
        match = re.match(r"^(\d{2})/(\d{2})/(\d{4})$", _text(row.get("Date")) or "")
        period = f"{match.group(3)}-{match.group(2)}" if match else _text(row.get("Date"))
        if not period or not re.fullmatch(r"\d{4}-\d{2}", period):
            raise RealEstateFormatError("schema_drift", f"UK HPI date {row.get('Date')!r} is not DD/MM/YYYY")
        if since and period < since:
            continue
        for column, (measure, unit, currency) in UKHPI_MEASURES.items():
            if column not in row:
                continue
            text = _text(row.get(column))
            index_id = f"ukhpi:{measure}"
            statements.append(_statement(
                "price_index_observation", "hmlr-ukhpi", f"{index_id}:{code}:{period}", event="observed",
                vintage=release, source=source,
                as_published={"index_id": index_id, "dataset": "UK House Price Index", "measure": measure,
                              "column": column, "geography": {"scheme": "ons-gss", "code": code,
                                                               "label": _text(row.get("RegionName"))},
                              "period": period, "frequency": "monthly",
                              "value": published_money(text, currency) if currency else
                              {"value_text": text, "value": text},
                              "unit": unit, "base_period": document.get("base_period") if measure == "index" else None,
                              "edition": "ukhpi-2015=100" if measure == "index" else "ukhpi",
                              "flags": []},
                place_refs=[{"scheme": "ons-gss", "code": code}]))
    return statements


def parse_dvf(raw: bytes, *, document: Mapping[str, Any], release: Mapping[str, Any], url: str,
              origin: str) -> list[dict[str, Any]]:
    """DVF mutations of one commune and year: one statement per id_mutation with one published value."""
    rows = _csv_rows(raw, header=True)
    if rows and not set(DVF_REQUIRED) <= set(rows[0]):
        raise RealEstateFormatError("schema_drift", "DVF file lacks " + ", ".join(sorted(set(DVF_REQUIRED) -
                                                                                        set(rows[0]))))
    commune = str(document["commune"])
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        if _text(row.get("code_commune")) != commune:
            raise RealEstateFormatError("schema_drift", "DVF row outside the declared commune")
        grouped.setdefault(str(row["id_mutation"]).strip(), []).append(row)
    source = _source("dvf", url, origin, _sha(raw))
    selection = f"dvf:{commune}:{document['year']}"
    statements = []
    for mutation_id, items in sorted(grouped.items()):
        values = {_text(r.get("valeur_fonciere")) for r in items}
        dates = {_text(r.get("date_mutation")) for r in items}
        natures = {_text(r.get("nature_mutation")) for r in items}
        if len(values) != 1 or len(dates) != 1 or len(natures) != 1:
            raise RealEstateFormatError("schema_drift", f"mutation {mutation_id} states several values, dates or "
                                                        "natures; it is not apportioned or chosen")
        parcels = sorted({p for p in (_text(r.get("id_parcelle")) for r in items) if p})
        locals_ = sorted({(_text(r.get("code_type_local")), _text(r.get("type_local")),
                           _text(r.get("surface_reelle_bati")), _text(r.get("nombre_pieces_principales")))
                          for r in items if _text(r.get("type_local"))}, key=lambda t: tuple(x or "" for x in t))
        addresses = sorted({(_text(r.get("adresse_numero")), _text(r.get("adresse_suffixe")),
                             _text(r.get("adresse_nom_voie")), _text(r.get("code_postal"))) for r in items},
                           key=lambda t: tuple(x or "" for x in t))
        lots = sorted({_text(r.get("nombre_lots")) for r in items if _text(r.get("nombre_lots"))})
        date = next(iter(dates))
        statements.append(_statement(
            "transaction", "dvf", mutation_id, event="observed", vintage=release, source=source,
            selection_key=selection,
            as_published={"source_transaction_id": mutation_id, "mutation_date": _day(date),
                           "transfer_date": _day(date), "nature_mutation": next(iter(natures)),
                           "price": published_money(next(iter(values)), "EUR", decimal_comma=True),
                           "price_scope": "one published valeur foncière for the whole mutation (every parcel, lot "
                                          "and local it covers); never apportioned",
                           "parcel_ids": parcels, "lot_counts": lots,
                           "locals": [{"code_type_local": c, "type_local": t, "surface_reelle_bati": s,
                                       "nombre_pieces_principales": p} for c, t, s, p in locals_],
                           "addresses": [{"numero": n, "suffixe": s, "voie": v, "code_postal": cp}
                                         for n, s, v, cp in addresses],
                           "commune": commune, "year": str(document["year"]), "dispositions": len(items)},
            place_refs=[{"scheme": "insee-commune", "code": commune}] + [
                {"scheme": "fr-postcode", "code": a[3]} for a in addresses if a[3]],
            parcel_refs=[{"scheme": "fr-id-parcelle", "code": p} for p in parcels]))
    return statements


def parse_eurostat_hpi(responses: Mapping[str, bytes], *, document: Mapping[str, Any], url: str,
                       origin: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """prc_hpi_q cells as published through EurostatConnector.parse_cells; the cube's updated stamp is the vintage."""
    from src.ingestion.connectors.dataset.base import RawSeries, SeriesRef
    from src.ingestion.connectors.dataset.eurostat import EurostatConnector

    connector = EurostatConnector(http_get=lambda _url: "")
    dataset = str(document["dataset"])
    statements, updated, shas = [], set(), []
    for geo in document["geo"]:
        raw = responses[geo]
        shas.append(_sha(raw))
        try:
            cube = connector.parse_cells(RawSeries(ref=SeriesRef(locator=f"{dataset}/{geo}", metadata={}),
                                                   content=raw.decode("utf-8"), content_type="application/json",
                                                   source_url=url, fetched_at=0), max_cells=5000)
        except (ValueError, KeyError, UnicodeDecodeError) as exc:
            raise RealEstateFormatError("schema_drift", f"JSON-stat cube could not be read: {exc}") from exc
        if not cube.get("updated"):
            raise RealEstateFormatError("schema_drift", "the cube states no updated stamp (vintage)")
        updated.add(str(cube["updated"]))
        units = cube["dimensions"].get("unit", {}).get("categories", {})
        purchases = cube["dimensions"].get("purchase", {}).get("categories", {})
        labels = cube["status_labels"]
        for cell in cube["cells"]:
            dims = dict(cell["dimensions"])
            if str(dims.get("geo")) != geo:
                raise RealEstateFormatError("schema_drift", "the cube's geography is not the requested one")
            unit_code, purchase = str(dims.get("unit") or ""), str(dims.get("purchase") or "")
            unit_label = _text(units.get(unit_code)) or unit_code
            base = re.search(r"(\d{4})\s*=\s*100", unit_label)
            edition = f"{unit_code}|{unit_label}"
            index_id = f"eurostat:{dataset}:{purchase}:{unit_code}"
            flag = cell["status"]
            statements.append(_statement(
                "price_index_observation", "eurostat-hpi",
                f"{index_id}:{geo}:{cell['time']}:{hashlib.sha256(edition.encode()).hexdigest()[:8]}",
                event="observed", vintage={}, source={},
                as_published={"index_id": index_id, "dataset": dataset, "measure": "index" if unit_code.startswith(
                    "I") else unit_code, "dimensions": dims,
                              "purchase": {"code": purchase, "label": _text(purchases.get(purchase))},
                              "geography": {"scheme": "eurostat-geo", "code": geo,
                                            "label": _text(cube["dimensions"].get("geo", {}).get("categories", {})
                                                           .get(geo))},
                              "period": str(cell["time"]), "frequency": "quarterly",
                              "value": {"value_text": None if cell["value"] is None else str(cell["value"]),
                                        "value": None if cell["value"] is None else str(cell["value"])},
                              "unit": unit_label, "unit_code": unit_code,
                              "base_period": f"{base.group(1)}=100" if base else None, "edition": edition,
                              "flags": [{"code": flag, "label": labels.get(flag)}] if flag else []},
                place_refs=[{"scheme": "eurostat-geo", "code": geo}]))
    if len(updated) != 1:
        raise RealEstateFormatError("schema_drift", "the declared countries' cubes state different updated stamps")
    stamp = next(iter(updated))
    release = {"release": stamp, "published_on": _day(stamp), "basis": "the JSON-stat cube's updated stamp"}
    source = {**_source("eurostat-hpi", url, origin, hashlib.sha256("".join(shas).encode()).hexdigest())}
    for item in statements:
        item["vintage"], item["source"] = dict(release), dict(source)
    return statements, release


# ------------------------------------------------------------------ declarations


def declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    """The ``real_estate`` block of a native source: provider, namespace and 1..max_pages declared documents."""
    declared = dict(source.get("real_estate") or {})
    provider = str(declared.get("provider") or "")
    if provider not in PROVIDERS:
        raise SourcePackError("invalid_manifest", f"real-estate sources declare a provider in {PROVIDERS}")
    host = (urlsplit(str(source.get("endpoint") or "")).hostname or "").casefold()
    if host not in PROVIDER_HOSTS[provider]:
        raise SourcePackError("invalid_manifest", f"{provider} is fetched from {PROVIDER_HOSTS[provider][0]} only")
    documents = [dict(d) for d in declared.get("documents") or []]
    if not 1 <= len(documents) <= int(dict(source.get("budgets") or {}).get("max_pages", 1)):
        raise SourcePackError("invalid_manifest", "a real-estate source declares 1..max_pages documents")
    for document in documents:
        path = str(document.get("path") or "")
        if not path.startswith("/") or "://" in path:
            raise SourcePackError("invalid_manifest", "a document names a path on the endpoint's host")
        if provider == "hmlr-ppd":
            districts = document.get("postcode_districts") or []
            if not 1 <= len(districts) <= 20 or document.get("release_from") != "last-modified":
                raise SourcePackError("invalid_manifest", "PPD documents name 1-20 postcode districts and take the "
                                                          "release from Last-Modified")
        elif provider == "hmlr-ukhpi":
            if not 1 <= len(document.get("geographies") or []) <= MAX_GEOS or not re.fullmatch(
                    r"\d{4}-\d{2}", str(document.get("release") or "")) or not _day(document.get("published_on")):
                raise SourcePackError("invalid_manifest", "UK HPI documents name 1-10 geography codes, a YYYY-MM "
                                                          "release and its publication date")
        elif provider == "dvf":
            if not re.fullmatch(r"\d[0-9AB]\d{3}", str(document.get("commune") or "")) or not re.fullmatch(
                    r"\d{4}", str(document.get("year") or "")) or not re.fullmatch(
                    r"\d{4}-\d{2}", str(document.get("release") or "")) or not _day(document.get("published_on")):
                raise SourcePackError("invalid_manifest", "DVF documents name an INSEE commune, a year, a YYYY-MM "
                                                          "release and its publication date")
        else:
            geos = list(document.get("geo") or [])
            if document.get("dataset") != "prc_hpi_q" or not 1 <= len(geos) <= MAX_GEOS or any(
                    not re.fullmatch(r"[A-Z0-9]{2,5}", str(g)) for g in geos):
                raise SourcePackError("invalid_manifest", "Eurostat documents name prc_hpi_q and 1-10 GEO codes")
            if not re.fullmatch(r"\d{4}-Q[1-4]", str(document.get("since") or "")):
                raise SourcePackError("invalid_manifest", "Eurostat documents bound the periods (since YYYY-Qn)")
    return {"provider": provider, "namespace": str(declared.get("namespace") or "global"), "documents": documents}


def parcel_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    """The ``real_estate`` block of an INSPIRE parcel WFS source: provider, country and the published attributes."""
    declared = dict(source.get("real_estate") or {})
    wfs = dict(source.get("wfs") or {})
    if declared.get("provider") not in PARCEL_PROVIDERS or declared.get("kind") != "parcels":
        raise SourcePackError("invalid_manifest", f"parcel sources declare kind parcels and a provider in "
                                                  f"{PARCEL_PROVIDERS}")
    attributes = dict(declared.get("attributes") or {})
    if not {"local_id", "namespace", "national_reference"} <= set(attributes):
        raise SourcePackError("invalid_manifest", "parcel sources name the localId, namespace and "
                                                  "nationalCadastralReference attributes")
    if not wfs.get("bbox") or not wfs.get("property_names"):
        raise SourcePackError("unbounded_source", "parcel sources pin a bbox and the requested properties")
    if set(attributes.values()) - set(wfs["property_names"]):
        raise SourcePackError("invalid_manifest", "every parcel attribute is a requested property")
    if {n.casefold() for n in wfs["property_names"]} & PARTY_COLUMNS:
        raise SourcePackError("invalid_manifest", "owner or rights-holder properties are never requested")
    return {"provider": declared["provider"], "country": str(declared.get("country") or ""),
            "namespace": str(declared.get("namespace") or "global"), "attributes": attributes,
            "reference_scheme": str(declared.get("reference_scheme") or "national-cadastral-reference")}


def request_for(provider: str, document: Mapping[str, Any]) -> dict[str, tuple[str, dict[str, str]]]:
    """Named (path, query) requests of one declared document."""
    if provider == "eurostat-hpi":
        out = {}
        for geo in document["geo"]:
            query = {"format": "JSON", "lang": "EN", "geo": geo, "sinceTimePeriod": document["since"]}
            query.update({str(k): str(v) for k, v in dict(document.get("filters") or {}).items()})
            out[geo] = (document["path"], query)
        return out
    return {"file": (document["path"], {str(k): str(v) for k, v in dict(document.get("query") or {}).items()})}


def _last_modified(headers: Mapping[str, Any]) -> str | None:
    value = headers.get("last-modified")
    if not value:
        return None
    try:
        return parsedate_to_datetime(str(value)).date().isoformat()
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ runtime adapter


class RealEstateAdapter:
    """One page per declared document of a PPD, UK HPI, DVF or Eurostat HPI source."""

    accepts_transport = True
    connector = CONNECTOR

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None) -> None:
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        del secret  # every selected source is open; no credential is sent
        self.source = json.loads(json.dumps(source))
        self.declared = declaration(self.source)
        self.provider = self.declared["provider"]
        if transport is None:
            from functools import partial

            transport = partial(HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"]))
        self.transport = transport
        self.definition = {
            "contract": ADAPTER_CONTRACT, "source_id": source["source_id"], "connector": source["connector"],
            "endpoint": source["endpoint"], "operations": list(source["operations"]),
            "source_hash": source["source_hash"], "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"], "limits": source["budgets"],
            "real_estate": {"provider": self.provider, "documents": len(self.declared["documents"]),
                            "live_verification": LIVE_VERIFICATION[self.provider]["status"]},
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _check(self, request: Mapping[str, Any]) -> None:
        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError("operation_forbidden", "operation is not declared by the source")
        if set(request) - {"operation", "parameters", "limit", "from_ms", "to_ms"}:
            raise SourcePackError("parameter_forbidden", "runtime adapter received undeclared controls")
        if dict(request.get("parameters") or {}):
            raise SourcePackError("parameter_forbidden", "real-estate runs fetch the declared documents only")

    def _get(self, path: str, query: Mapping[str, str]) -> tuple[bytes, dict[str, Any], str, str]:
        base = self.source["endpoint"].rstrip("/") + path
        url = base + ("?" + urlencode(sorted(query.items())) if query else "")
        response = self.transport(url=base, params=dict(query), headers={"Accept": "text/csv, application/json"},
                                  timeout=int(self.definition["limits"]["timeout_ms"]) / 1000)
        host = (urlsplit(self.source["endpoint"]).hostname or "").casefold()
        if (urlsplit(str(response.get("final_url") or url)).hostname or "").casefold() != host:
            raise SourcePackError("network_policy", "response was served from another host")
        status = int(response.get("status", 200))
        headers = {str(k).casefold(): v for k, v in dict(response.get("headers") or {}).items()}
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError("response_too_large", "response exceeds its byte limit")
        if status == 429:
            from src.ingestion.source_pack_runtime import _retry_after_ms

            raise SourcePackError("rate_limited", f"{self.provider} rate limit reached",
                                  retry_after_ms=_retry_after_ms(headers.get("retry-after")))
        if status in {401, 403}:
            raise SourcePackError("authentication_failed", f"request refused (HTTP {status})")
        if status >= 500:
            raise SourcePackError("source_unavailable", f"provider returned HTTP {status}")
        if status >= 400:
            raise SourcePackError("schema_drift", f"request returned HTTP {status}")
        return raw, headers, url, "fixture" if response.get("origin") == "fixture" else "live"

    def _release(self, document: Mapping[str, Any], headers: Mapping[str, Any]) -> dict[str, Any]:
        if document.get("release_from") == "last-modified":
            modified = _last_modified(headers)
            if modified is None:
                # Never dated by the retrieval time.
                raise SourcePackError("schema_drift", "the file states no Last-Modified date; its release is unknown")
            return {"release": modified[:7], "published_on": modified,
                    "basis": "the monthly file's Last-Modified date"}
        return {"release": str(document["release"]), "published_on": _day(document["published_on"]),
                "basis": "the declared release and its publication date"}

    def _page(self, document: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any], int]:
        requests = request_for(self.provider, document)
        if self.provider == "eurostat-hpi":
            responses, size, origins = {}, 0, set()
            for geo, (path, query) in requests.items():
                raw, _headers, _url, origin = self._get(path, query)
                responses[geo], size = raw, size + len(raw)
                origins.add(origin)
            base = self.source["endpoint"].rstrip("/") + document["path"]
            statements, release = parse_eurostat_hpi(responses, document=document, url=base,
                                                     origin="fixture" if origins == {"fixture"} else "live")
            return statements, release, size
        path, query = requests["file"]
        raw, headers, url, origin = self._get(path, query)
        release = self._release(document, headers)
        parser = {"hmlr-ppd": parse_ppd, "hmlr-ukhpi": parse_ukhpi, "dvf": parse_dvf}[self.provider]
        return parser(raw, document=document, release=release, url=url, origin=origin), release, len(raw)

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage

        self._check(request)
        documents = self.declared["documents"]
        index = 0 if cursor is None else int(cursor) if str(cursor).isdigit() else -1
        if not 0 <= index < len(documents):
            raise SourcePackError("cursor_drift", "cursor names no declared document")
        document = documents[index]
        try:
            statements, release, size = self._page(document)
        except RealEstateFormatError as exc:
            code = "response_too_large" if exc.code == "input_limit" else "schema_drift"
            raise SourcePackError(code, f"{exc.code}: {exc}") from exc
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        if len(statements) > limit:
            # Never a truncated publication: a missing row would read as a withdrawn or absent transaction.
            raise SourcePackError("budget_exhausted", "document has more records than the run's result budget")
        records = []
        for item in statements:
            content = json.dumps(item, sort_keys=True, ensure_ascii=False)
            records.append({"id": f"{item['provider']}|{item['record_key']}|{_sha(content.encode())[:12]}",
                            "title": f"{item['provider']} {item['record_type']} {item['record_key']}",
                            "url": item["source"]["url"], "language": "und", "content": content,
                            "published_at": release.get("published_on"), "real_estate_record": item})
        selection = f"dvf:{document['commune']}:{document['year']}" if self.provider == "dvf" else None
        receipt = {"status": 200, "provider": self.provider, "document": document.get("label"),
                   "release": release, "records": len(records),
                   "evidence_origin": records[0]["real_estate_record"]["source"]["evidence_origin"] if records else
                   None, "snapshot": {"selection_key": selection, "complete": True} if selection else None,
                   "final_page": index + 1 >= len(documents),
                   "live_verification": LIVE_VERIFICATION[self.provider]["status"]}
        next_cursor = str(index + 1) if index + 1 < len(documents) else None
        return RuntimePage(tuple(records), next_cursor, size, receipt=receipt)


FIXTURE_SECRET = None
ADAPTERS = {CONNECTOR: RealEstateAdapter}


def fixture_transport(pages: Sequence[Mapping[str, Any]]) -> Callable[..., Mapping[str, Any]]:
    """Replay authored responses keyed by URL path and sorted query; responses are marked as fixture evidence."""
    by_key = {page["request"]: page for page in pages}

    def transport(*, url, params, headers, timeout, **_):
        del headers, timeout
        key = urlsplit(url).path + ("?" + urlencode(sorted(dict(params or {}).items())) if params else "")
        page = by_key.get(key)
        if page is None:
            raise SourcePackError("fixture_missing", f"no native page for {key}")
        body = page.get("body")
        content = body.encode() if isinstance(body, str) else b"" if body is None else json.dumps(body).encode()
        return {"status": int(page.get("status", 200)), "headers": dict(page.get("headers") or {}),
                "content": content, "origin": "fixture"}

    return transport


def fixture_request(path: str, query: Mapping[str, str] | None = None) -> str:
    """The key :func:`fixture_transport` files a response under."""
    return path + ("?" + urlencode(sorted(dict(query).items())) if query else "")


def replay_native_fixture(source: Mapping[str, Any], fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    adapter = RealEstateAdapter(source, transport=fixture_transport(list(fixture["native_pages"])))
    records, cursor = [], None
    while True:
        page = adapter.fetch_page({"operation": min(source["operations"]), "parameters": {},
                                   "limit": int(source["budgets"]["max_results"])}, cursor=cursor)
        records += [dict(item) for item in page.records]
        cursor = page.next_cursor
        if cursor is None:
            return records


__all__ = [
    "ADAPTERS", "BOUNDED_COVERAGE", "CONNECTOR", "DVF_NOTICE", "EXCLUSIONS", "FIXTURE_SECRET", "LIVE_VERIFICATION",
    "PARCEL_PROVIDERS", "PARTY_COLUMNS", "PROVIDERS", "PROVIDER_CONTRACTS", "REUSE_CONDITIONS", "RealEstateAdapter",
    "RealEstateFormatError", "declaration", "fixture_request", "fixture_transport", "parcel_declaration",
    "parse_dvf", "parse_eurostat_hpi", "parse_ppd", "parse_ukhpi", "postcode_district", "published_money",
    "replay_native_fixture", "request_for",
]
