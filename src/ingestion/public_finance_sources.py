"""Public-finance acquisition for the Economics ``public-finance`` feature (#1909, B01/B03-B06).

One native connector, ``public-finance``, fetches a bounded, declared list of
published files per source - one file per page, one *release* (source
revision) per file - and parses each into records whose figures are exactly as
published. Supported documented formats:

* ``de-bundeshaushalt-csv`` - Bundeshaushalt open data (bundeshaushalt.de):
  one row per Einzelplan/Kapitel/Titel with the amount columns the file
  carries (``Soll``, ``Ist``); the ``Stand`` date, fiscal year and amount unit
  come from the file's header lines or the manifest;
* ``de-be-haushalt-csv`` - Berlin Senate finance budget data (daten.berlin.de):
  one row per Bereich/Einzelplan/Kapitel/Titel, year and amount type
  (``BetragTyp``); Bereiche 31-42 are the districts;
* ``eu-fts-csv`` - EU Financial Transparency System exports: one row per
  beneficiary commitment (or payment) with budget line, programme, amount and
  year;
* ``eurostat-gfs-jsonstat`` - Eurostat government finance statistics through
  the existing :class:`src.ingestion.connectors.dataset.eurostat.EurostatConnector`
  (JSON-stat), one declared series per page, into ``dataset-series-v1``.

Nothing is recomputed: amount text is kept verbatim beside the parsed decimal,
the unit the file states (e.g. thousand EUR) is recorded, and each figure
carries the accounting basis the source contract declares (cash, accrual,
ESA 2010, commitment or payment). Plan, supplementary-plan, outturn and
payment figures are distinct record kinds. A file with a column, amount type
or unit the source does not declare is refused as schema drift rather than
partially read; a release is all-or-nothing.

``PROVIDER_CONTRACTS`` records the B01 access decisions (``unverified-live``
until a dated live run; ``not-implemented`` with a reason).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

from src.ingestion.source_packs import SourcePackError

ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
RELEASE_CONTRACT = "noesis-public-finance-release-v1"
CONNECTOR = "public-finance"
FIGURE_KINDS = ("plan", "supplementary_plan", "outturn")
PAYMENT_KINDS = ("commitment", "payment")
ACCOUNTING_BASES = (
    "cash",
    "accrual",
    "esa2010",
    "commitment-appropriation",
    "payment-appropriation",
    "commitment",
    "payment",
)
# Hierarchy schemes: each keeps the source's own codes and labels; no cross-source hierarchy is synthesised.
SCHEMES = {
    "de-bund-haushalt": ("einzelplan", "kapitel", "titel"),
    "de-be-haushalt": ("bereich", "einzelplan", "kapitel", "titel"),
    "eu-budget-line": ("budget_line",),
}
# Amount units a file may state, with their scale to the currency unit.
UNITS = {
    "EUR": ("count", 1),
    "1.000 EUR": ("kilocount", 1000),
    "1000 EUR": ("kilocount", 1000),
    "TSD. EUR": ("kilocount", 1000),
    "TSD EUR": ("kilocount", 1000),
    "T EUR": ("kilocount", 1000),
    "MIO. EUR": ("megacount", 1_000_000),
    "MIO EUR": ("megacount", 1_000_000),
}
FORMATS: dict[str, dict[str, Any]] = {
    "de-bundeshaushalt-csv": {
        "provider": "bundeshaushalt",
        "jurisdiction": "DE",
        "scheme": "de-bund-haushalt",
        "records": "figures",
    },
    "de-be-haushalt-csv": {
        "provider": "berlin-senfin",
        "jurisdiction": "DE-BE",
        "scheme": "de-be-haushalt",
        "records": "figures",
    },
    "eu-fts-csv": {
        "provider": "eu-fts",
        "jurisdiction": "EU",
        "scheme": "eu-budget-line",
        "records": "payments",
    },
    "eurostat-gfs-jsonstat": {
        "provider": "eurostat-gfs",
        "jurisdiction": "EU",
        "scheme": None,
        "records": "series",
    },
}
GFS_DATASETS = ("gov_10a_main", "gov_10a_exp", "gov_10dd_edpt1")
MAX_ROWS = 500_000
REVIEW_BOUNDARY = (
    "Published figures only, each cited to its source revision and accounting basis: plan, supplementary-plan, "
    "outturn and payment records stay distinct; nothing forecasts a fiscal outcome, determines waste or fraud, or "
    "nets figures across accounting bases without a cited method."
)

# B01 access decisions. Endpoints, file layouts, identifiers and terms below are
# recorded from the providers' published documentation as known without network
# access; every item marked ``verify`` must be checked against the live terms and
# files before a dated live run is accepted (#2014).
PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "bundeshaushalt": {
        "publisher": "Bundesministerium der Finanzen (bundeshaushalt.de)",
        "delivers": ["budget-plan", "supplementary-plan", "outturn"],
        "access": "Open-data CSV downloads per budget year (Haushaltsplan Soll, Nachtragshaushalte, Ist) linked "
        "from bundeshaushalt.de/DE/Open-Data (verify file names, delimiter and column labels per year)",
        "format": "de-bundeshaushalt-csv",
        "authentication": "none",
        "rate_limits": "undocumented; one bounded download per declared file per run",
        "pagination": "none; one file per budget document",
        "file_formats": ["CSV (semicolon, German number format)", "XML (not used)"],
        "hierarchy": "Einzelplan (2 digits) / Kapitel (4 digits) / Titel (5 digits, optional Titelgruppe) with the "
        "published labels; Einnahme/Ausgabe side as published",
        "fiscal_year": "the Haushaltsjahr; a Nachtragshaushalt amends the same year's plan",
        "vintages": "the Stand date of each file; the Haushaltsrechnung (final Ist) supersedes monthly or "
        "preliminary Ist publications of the same year (verify cadence)",
        "currency_units": "EUR; amounts published in thousand EUR (verify the unit line per file)",
        "accounting_basis": "cash (kameralistische Haushaltsführung: Soll and Ist on a payment basis)",
        "cadence": "per budget law and supplementary budget; Ist after year end",
        "terms": "Datenlizenz Deutschland - Namensnennung 2.0 (verify on the open-data page)",
        "retained_evidence": "figures, file digest, Stand date and URL",
        "access_decision": "unverified-live",
        "reason": "fixture-verified parser; column labels, unit line and Stand line must be verified against a "
        "published file",
    },
    "berlin-senfin": {
        "publisher": "Senatsverwaltung für Finanzen Berlin (daten.berlin.de)",
        "delivers": ["budget-plan", "outturn"],
        "access": "Doppelhaushalt CSV per budget cycle on daten.berlin.de (verify the dataset id, column names and "
        "whether Ist is published in the same file)",
        "format": "de-be-haushalt-csv",
        "authentication": "none",
        "rate_limits": "undocumented; one bounded download per declared file per run",
        "pagination": "none",
        "file_formats": ["CSV (semicolon)"],
        "hierarchy": "Bereich (30 Hauptverwaltung, 31-42 the twelve districts) / Einzelplan / Kapitel / Titel with "
        "published labels (verify the Bereich numbering)",
        "fiscal_year": "Jahr column; a Doppelhaushalt covers two years",
        "vintages": "file publication (Last-Modified or the catalogue's modified date); a later file for the same "
        "cycle is a new vintage",
        "currency_units": "EUR (verify)",
        "accounting_basis": "cash",
        "cadence": "per budget cycle; Ist annually",
        "terms": "CC BY 3.0 DE for daten.berlin.de datasets (verify per dataset)",
        "retained_evidence": "figures, file digest, publication date and URL",
        "access_decision": "unverified-live",
        "reason": "fixture-verified parser; the CSV layout and the Bereich-to-district crosswalk must be verified",
    },
    "eu-fts": {
        "publisher": "European Commission, Financial Transparency System",
        "delivers": ["beneficiary-payments"],
        "access": "Yearly FTS downloads (CSV/XLSX) from ec.europa.eu/budget/financial-transparency-system "
        "(verify the per-year file URL and whether the export is served from the same host)",
        "format": "eu-fts-csv",
        "authentication": "none",
        "rate_limits": "undocumented; one bounded download per declared file per run",
        "pagination": "none; one file per year",
        "file_formats": ["CSV", "XLSX (not used)"],
        "hierarchy": "budget line number and name as published (e.g. 01 02 01 01), programme name, commitment "
        "position key",
        "fiscal_year": "the Year column (financial year of the commitment)",
        "vintages": "one publication per year, usually by 30 June of the following year; republished years are "
        "new vintages",
        "currency_units": "EUR",
        "accounting_basis": "commitment (legal commitments made in the year); a payment column, when published, "
        "is recorded on a payment basis",
        "cadence": "annual",
        "terms": "Commission reuse policy (Decision 2011/833/EU), attribution required (verify)",
        "retained_evidence": "rows as published, file digest and URL; natural persons may be published only in "
        "aggregated form (verify handling)",
        "access_decision": "unverified-live",
        "reason": "fixture-verified parser; the export columns must be verified",
    },
    "eu-budget-pages": {
        "publisher": "European Commission (commission.europa.eu EU budget pages)",
        "delivers": ["annual-budget-overview"],
        "access": "HTML pages and PDF documents; the adopted annual budget and amending budgets are legal acts "
        "acquired through CELLAR (legal.works)",
        "format": None,
        "authentication": "none",
        "hierarchy": "headings, programmes and budget lines inside PDF tables",
        "accounting_basis": "commitment and payment appropriations",
        "terms": "Commission reuse policy (verify)",
        "access_decision": "not-implemented",
        "reason": "no supported machine-readable access for figures; never scraped. Budget acts come from CELLAR, "
        "payments from FTS",
    },
    "eurostat-gfs": {
        "publisher": "Eurostat (government finance statistics)",
        "delivers": ["government-finance-series"],
        "access": "JSON-stat 2.0 through the dissemination API (gov_10a_main, gov_10a_exp by COFOG, "
        "gov_10dd_edpt1) via the existing Eurostat dataset connector",
        "format": "eurostat-gfs-jsonstat",
        "authentication": "none",
        "rate_limits": "fair use (verify); one request per declared series per run",
        "pagination": "none; bounded by the declared filters",
        "file_formats": ["JSON-stat 2.0"],
        "hierarchy": "dataset dimensions as published: geo, sector (S13, S1311...), na_item (TE, TR, B9...), cofog99, "
        "unit",
        "fiscal_year": "calendar year (time dimension)",
        "vintages": "the dataset's updated timestamp; EDP notifications in April and October revise earlier years",
        "currency_units": "unit dimension as published (MIO_EUR, PC_GDP...)",
        "accounting_basis": "ESA 2010 (accrual national accounts)",
        "cadence": "April and October notifications; quarterly for some tables",
        "terms": "Eurostat reuse policy, attribution required",
        "retained_evidence": "series vintages in the dataset store",
        "access_decision": "unverified-live",
        "reason": "fixture-verified through the Eurostat connector; dataset codes and filters need a dated live run",
    },
    "bundesrechnungshof": {
        "publisher": "Bundesrechnungshof",
        "delivers": ["audit-findings"],
        "access": "reports (Bemerkungen, Berichte) as HTML pages and PDF files; no documented machine-readable "
        "finding export",
        "format": "operator-finding-sheet",
        "authentication": "none",
        "hierarchy": "reports cite Einzelplan/Kapitel/Titel in text",
        "accounting_basis": "not applicable (findings cite figures on the audited basis)",
        "terms": "reuse of official reports with attribution (verify on the imprint)",
        "retained_evidence": "report URL, passage locator and the quoted passage an operator records",
        "access_decision": "not-implemented",
        "reason": "no supported machine access; never scraped. Findings enter as operator-recorded finding sheets "
        "(report, passage locator, quoted text, cited lines) without any verdict",
    },
    "imf-gfs": {
        "publisher": "International Monetary Fund (Government Finance Statistics)",
        "delivers": ["government-finance-series"],
        "access": "IMF data portal API (SDMX); verify the current API after the portal migration",
        "format": None,
        "authentication": "none (verify)",
        "accounting_basis": "GFSM 2014 (accrual)",
        "terms": "IMF terms of use restrict redistribution of some datasets (verify in writing)",
        "terms_decision": "blocked: reuse terms not confirmed; no acquisition is planned until the IMF terms for "
        "storing and redistributing GFS series are recorded in writing",
        "access_decision": "not-implemented",
        "reason": "reuse terms decision outstanding; Eurostat GFS covers EU members on ESA 2010",
    },
}


class PublicFinanceFormatError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


def _clean(value: Any) -> str | None:
    text = " ".join(str(value or "").split())
    return text or None


def code(value: Any) -> str | None:
    """A hierarchy code as published, whitespace removed; leading zeros are kept (they are part of the code)."""
    text = "".join(str(value or "").split())
    return text or None


def parse_amount(text: Any, number_format: str) -> Decimal | None:
    """The published amount text as a decimal; ``None`` for a blank cell (unknown stays unknown)."""
    raw = "".join(str(text or "").split())
    if not raw or raw in {"-", "–", "."}:
        return None
    if number_format == "de":
        raw = raw.replace(".", "").replace(",", ".")
    else:
        raw = raw.replace(",", "")
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise PublicFinanceFormatError(
            "schema_drift", f"amount {text!r} is not a number"
        ) from exc
    if not value.is_finite():
        raise PublicFinanceFormatError("schema_drift", "amount is not finite")
    return value


def unit_scale(unit: Any) -> tuple[str, str, int]:
    """(unit as published, pint unit, scale); an unknown unit is refused, never guessed."""
    label = _clean(unit)
    key = (label or "").upper()
    if key not in UNITS:
        raise PublicFinanceFormatError(
            "schema_drift", f"amount unit {unit!r} is not a declared unit"
        )
    pint_unit, scale = UNITS[key]
    return label, pint_unit, scale


def _day(value: Any) -> str | None:
    text = str(value or "").strip()
    for pattern in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text[:10], pattern).date().isoformat()
        except ValueError:
            continue
    return None


def _csv(raw: bytes) -> tuple[list[str], list[dict[str, str]]]:
    """Leading ``#`` comment lines and the rows of a delimited file (delimiter taken from the header)."""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252")
    lines = text.splitlines()
    comments = []
    while lines and (lines[0].startswith("#") or not lines[0].strip()):
        comments.append(lines.pop(0).lstrip("#").strip())
    if not lines:
        raise PublicFinanceFormatError("schema_drift", "file has no header row")
    header = lines[0]
    delimiter = max((";", ",", "\t"), key=header.count)
    reader = csv.DictReader(io.StringIO("\n".join(lines)), delimiter=delimiter)
    rows = []
    for row in reader:
        if None in row:
            raise PublicFinanceFormatError(
                "schema_drift", "a row has more cells than the header"
            )
        rows.append({str(k).strip(): (v or "").strip() for k, v in row.items()})
        if len(rows) > MAX_ROWS:
            raise PublicFinanceFormatError(
                "input_limit", "file has more rows than the parser accepts"
            )
    return [c for c in comments if c], rows


def _require(
    rows: Sequence[Mapping[str, str]], header: Sequence[str], required: Sequence[str]
) -> None:
    missing = [c for c in required if c not in header]
    if missing:
        raise PublicFinanceFormatError(
            "schema_drift", f"missing columns: {', '.join(missing)}"
        )
    if not rows:
        raise PublicFinanceFormatError("schema_drift", "file has no data rows")


def _comment(comments: Sequence[str], *prefixes: str) -> str | None:
    for line in comments:
        for prefix in prefixes:
            if line.casefold().startswith(prefix.casefold()):
                return line[len(prefix) :].strip(" :")
    return None


def _figure(
    *,
    scheme: str,
    codes: Mapping[str, Any],
    labels: Mapping[str, Any],
    side: str | None,
    fiscal_year: str,
    kind: str,
    plan_key: str,
    amount_text: str,
    amount: Decimal | None,
    unit: str,
    locator: Mapping[str, Any],
    district: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    codes = {k: code(v) for k, v in codes.items()}
    return {
        "scheme": scheme,
        "codes": codes,
        "labels": {k: _clean(v) for k, v in labels.items()},
        "side": side,
        "district": dict(district) if district else None,
        "fiscal_year": fiscal_year,
        "figure_kind": kind,
        "plan_key": plan_key,
        "amount_text": amount_text,
        "amount": None if amount is None else str(amount),
        "unit": unit,
        "locator": dict(locator),
    }


_SIDES = {
    "e": "revenue",
    "einnahme": "revenue",
    "einnahmen": "revenue",
    "einnahmetitel": "revenue",
    "a": "expenditure",
    "ausgabe": "expenditure",
    "ausgaben": "expenditure",
    "ausgabetitel": "expenditure",
}


def _side(value: Any) -> str | None:
    text = str(value or "").strip().casefold()
    if not text:
        return None
    if text not in _SIDES:
        raise PublicFinanceFormatError(
            "schema_drift", f"revenue/expenditure marker {value!r} is not documented"
        )
    return _SIDES[text]


def _amount_kinds(document: Mapping[str, Any]) -> dict[str, tuple[str, str]]:
    """Amount column (or amount type) -> (figure kind, plan key) as the source manifest declares it."""
    out = {}
    for column, spec in dict(document.get("amounts") or {}).items():
        spec = dict(spec)
        if spec.get("kind") not in FIGURE_KINDS:
            raise PublicFinanceFormatError(
                "schema_drift", f"amount {column!r} declares no figure kind"
            )
        out[str(column).casefold()] = (
            spec["kind"],
            str(spec.get("plan_key") or spec["kind"]),
        )
    if not out:
        raise PublicFinanceFormatError(
            "schema_drift", "the document declares no amount columns"
        )
    return out


def parse_bundeshaushalt(raw: bytes, *, document: Mapping[str, Any]) -> dict[str, Any]:
    comments, rows = _csv(raw)
    header = list(rows[0]) if rows else []
    _require(rows, header, ("Einzelplan", "Kapitel", "Titel"))
    kinds = _amount_kinds(document)
    amount_columns = [
        c for c in header if c.casefold() in {"soll", "ist"} or c.casefold() in kinds
    ]
    undeclared = [c for c in amount_columns if c.casefold() not in kinds]
    if undeclared:
        # Never drop an amount the file states: an amount column the source does not declare is refused.
        raise PublicFinanceFormatError(
            "schema_drift", f"undeclared amount columns: {', '.join(undeclared)}"
        )
    missing = [c for c in kinds if c not in {h.casefold() for h in header}]
    if missing:
        raise PublicFinanceFormatError(
            "schema_drift", f"declared amount columns missing: {', '.join(missing)}"
        )
    unit_label = _comment(
        comments, "Betragsangaben in", "Beträge in", "Einheit"
    ) or document.get("unit")
    unit, _pint, _scale = unit_scale(unit_label)
    stand = _day(_comment(comments, "Stand") or "")
    year_comment = _comment(comments, "Haushaltsjahr")
    figures = []
    for number, row in enumerate(rows, start=2):
        year = (
            _clean(row.get("Haushaltsjahr"))
            or year_comment
            or document.get("fiscal_year")
        )
        if not year or not re.fullmatch(r"\d{4}", str(year)):
            raise PublicFinanceFormatError(
                "schema_drift", "a row states no four-digit Haushaltsjahr"
            )
        codes = {
            "einzelplan": row["Einzelplan"],
            "kapitel": row["Kapitel"],
            "titel": row["Titel"],
        }
        if not all(code(v) for v in codes.values()):
            raise PublicFinanceFormatError(
                "schema_drift", f"row {number} lacks an Einzelplan, Kapitel or Titel"
            )
        labels = {
            "einzelplan": row.get("Einzelplantext") or row.get("Einzelplan-Text"),
            "kapitel": row.get("Kapiteltext") or row.get("Kapitel-Text"),
            "titel": row.get("Titeltext") or row.get("Titel-Text"),
        }
        if row.get("Titelgruppe"):
            codes["titelgruppe"] = row["Titelgruppe"]
        side = _side(row.get("Einnahme/Ausgabe") or row.get("E/A"))
        for column in amount_columns:
            kind, plan_key = kinds[column.casefold()]
            figures.append(
                _figure(
                    scheme="de-bund-haushalt",
                    codes=codes,
                    labels=labels,
                    side=side,
                    fiscal_year=str(year),
                    kind=kind,
                    plan_key=plan_key,
                    amount_text=row[column],
                    amount=parse_amount(row[column], "de"),
                    unit=unit,
                    locator={"row": number, "column": column},
                )
            )
    return {
        "published_on": stand,
        "published_at": None,
        "figures": figures,
        "unit": unit,
    }


def parse_berlin(
    raw: bytes, *, document: Mapping[str, Any], districts: Mapping[str, Any]
) -> dict[str, Any]:
    comments, rows = _csv(raw)
    header = list(rows[0]) if rows else []
    _require(
        rows,
        header,
        ("Bereich", "Einzelplan", "Kapitel", "Titel", "Jahr", "BetragTyp", "Betrag"),
    )
    kinds = _amount_kinds(document)
    unit, _pint, _scale = unit_scale(
        _comment(comments, "Betragsangaben in", "Einheit") or document.get("unit")
    )
    stand = _day(_comment(comments, "Stand") or "")
    figures = []
    for number, row in enumerate(rows, start=2):
        typ = row["BetragTyp"].casefold()
        if typ not in kinds:
            # An amount type the source does not declare is refused rather than silently skipped.
            raise PublicFinanceFormatError(
                "schema_drift", f"undeclared BetragTyp {row['BetragTyp']!r}"
            )
        kind, plan_key = kinds[typ]
        year = _clean(row["Jahr"])
        if not year or not re.fullmatch(r"\d{4}", year):
            raise PublicFinanceFormatError(
                "schema_drift", f"row {number} states no four-digit Jahr"
            )
        bereich = code(row["Bereich"])
        district = districts.get(bereich or "")
        figures.append(
            _figure(
                scheme="de-be-haushalt",
                codes={
                    "bereich": bereich,
                    "einzelplan": row["Einzelplan"],
                    "kapitel": row["Kapitel"],
                    "titel": row["Titel"],
                },
                labels={
                    "bereich": row.get("Bereichsbezeichnung"),
                    "einzelplan": row.get("Einzelplanbezeichnung"),
                    "kapitel": row.get("Kapitelbezeichnung"),
                    "titel": row.get("Titelbezeichnung"),
                },
                side=_side(row.get("Titelart")),
                fiscal_year=year,
                kind=kind,
                plan_key=plan_key,
                amount_text=row["Betrag"],
                amount=parse_amount(row["Betrag"], "de"),
                unit=unit,
                locator={"row": number, "column": "Betrag"},
                district=None
                if district is None
                else {
                    "bereich": bereich,
                    "code": code(district["code"]),
                    "name": district.get("name"),
                },
            )
        )
    return {
        "published_on": stand,
        "published_at": None,
        "figures": figures,
        "unit": unit,
    }


_FTS_REQUIRED = (
    "Year",
    "Name of beneficiary",
    "Budget line name and number",
    "Programme name",
    "Amount",
)
_FTS_KINDS = {
    "commitment": "commitment",
    "commitments": "commitment",
    "payment": "payment",
    "payments": "payment",
}


def parse_fts(raw: bytes, *, document: Mapping[str, Any]) -> dict[str, Any]:
    comments, rows = _csv(raw)
    header = list(rows[0]) if rows else []
    _require(rows, header, _FTS_REQUIRED)
    declared_kind = document.get("amount_kind")
    payments = []
    occurrences: dict[tuple[str, str], int] = {}
    for number, row in enumerate(rows, start=2):
        year = _clean(row["Year"])
        if not year or not re.fullmatch(r"\d{4}", year):
            raise PublicFinanceFormatError(
                "schema_drift", f"row {number} states no four-digit Year"
            )
        kind_text = _clean(row.get("Type of amount"))
        kind = (
            _FTS_KINDS.get((kind_text or "").casefold()) if kind_text else declared_kind
        )
        if kind not in PAYMENT_KINDS:
            raise PublicFinanceFormatError(
                "schema_drift",
                f"row {number} states no commitment or payment kind ({kind_text!r})",
            )
        line_text = _clean(row["Budget line name and number"]) or ""
        number_part, _, name_part = line_text.partition(" - ")
        budget_line = (
            code(number_part)
            if re.fullmatch(r"[0-9A-Z .]+", number_part.strip())
            else None
        )
        currency = _clean(row.get("Currency")) or document.get("currency") or "EUR"
        beneficiary = _clean(row["Name of beneficiary"])
        if not beneficiary:
            raise PublicFinanceFormatError(
                "schema_drift", f"row {number} names no beneficiary"
            )
        amount = parse_amount(row["Amount"], "en")
        body = {
            "fiscal_year": year,
            "beneficiary": beneficiary,
            "beneficiary_country": _clean(
                row.get("Country") or row.get("Country / Territory")
            ),
            "beneficiary_identifiers": [
                {"scheme": "vat", "value": v, "country": _clean(row.get("Country"))}
                for v in [
                    _clean(
                        row.get("VAT number") or row.get("VAT Number of beneficiary")
                    )
                ]
                if v
            ],
            "beneficiary_city": _clean(row.get("City")),
            "budget_line": budget_line,
            "budget_line_text": line_text,
            "budget_line_name": _clean(name_part) if budget_line else None,
            "programme": _clean(row["Programme name"]),
            "position_key": _clean(row.get("Commitment position key")),
            "grant_reference": _clean(row.get("Grant reference")),
            "funding_type": _clean(row.get("Funding type")),
            "subject": _clean(row.get("Subject of grant or contract")),
            "payment_kind": kind,
            "amount_text": row["Amount"],
            "amount": None if amount is None else str(amount),
            "currency": currency,
            "unit": currency,
        }
        # Rows without a position key are told apart by everything they state; exact repeats by their occurrence.
        identity = body["position_key"] or _digest(
            {k: v for k, v in body.items() if k not in {"amount", "amount_text"}}
        )
        # A commitment and a payment of one position are two records (the kind is part of their key); only an
        # exact repeat of the same kind is numbered.
        occurrence = occurrences[(identity, kind)] = (
            occurrences.get((identity, kind), 0) + 1
        )
        body["payment_key"] = identity + (f"#{occurrence}" if occurrence > 1 else "")
        body["locator"] = {"row": number}
        payments.append(body)
    return {
        "published_on": _day(_comment(comments, "Published", "Stand") or ""),
        "published_at": None,
        "payments": payments,
    }


def parse_gfs(raw: bytes, *, spec: Mapping[str, Any], url: str) -> dict[str, Any]:
    """One declared GFS series through the existing Eurostat connector (JSON-stat), with its provider vintage."""
    from src.ingestion.connectors.dataset.base import RawSeries
    from src.ingestion.connectors.dataset.eurostat import EurostatConnector

    try:
        cube = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublicFinanceFormatError(
            "schema_drift", "the response is not a JSON-stat document"
        ) from exc
    geo = (
        dict(
            dict(dict(cube.get("dimension") or {}).get("geo") or {}).get("category")
            or {}
        ).get("index")
        or {}
    )
    if set(geo) != {str(spec["geography"])}:
        raise PublicFinanceFormatError(
            "schema_drift", "the cube's geography is not the requested one"
        )
    connector = EurostatConnector(http_get=lambda _url: raw.decode("utf-8"))
    request = {
        "dataset": spec["dataset"],
        "geography": spec["geography"],
        **dict(spec.get("filters") or {}),
    }
    ref = next(iter(connector.discover({**request, "series_key": "dimensions"})))
    try:
        records = connector.parse(
            RawSeries(
                ref=ref,
                content=raw.decode("utf-8"),
                content_type="application/json",
                source_url=url,
                fetched_at=0,
            )
        )
    except (ValueError, KeyError) as exc:
        raise PublicFinanceFormatError(
            "schema_drift", f"JSON-stat cube could not be read: {exc}"
        ) from exc
    if len(records) != 1:
        raise PublicFinanceFormatError(
            "schema_drift", "the cube has no time dimension or series"
        )
    record = records[0]
    # The retrieval time belongs to the projection that stores the vintage, not to the parsed release (a replay
    # of the same file must parse to the same record).
    record.metadata.pop("acquired_at_ms", None)
    if record.metadata.get("vintage_basis") != "eurostat_dataset_updated":
        # Without the provider's own update time a retrieval time would pose as a vintage.
        raise PublicFinanceFormatError(
            "schema_drift", "the cube states no updated timestamp (provider vintage)"
        )
    if record.metadata.get("unselected_multi_dimensions"):
        raise PublicFinanceFormatError(
            "schema_drift",
            "the cube has several categories in unfiltered dimensions "
            f"({', '.join(record.metadata['unselected_multi_dimensions'])}); declare a filter for each",
        )
    published = datetime.fromtimestamp(record.as_of / 1000, tz=timezone.utc)
    return {
        "published_on": published.date().isoformat(),
        "published_at": published.replace(tzinfo=None).isoformat(timespec="seconds"),
        "series": record.to_dict(),
    }


def _last_modified(headers: Mapping[str, Any]) -> tuple[str | None, str | None]:
    value = headers.get("last-modified")
    if not value:
        return None, None
    try:
        stamp = parsedate_to_datetime(str(value))
    except (TypeError, ValueError):
        return None, None
    return stamp.date().isoformat(), stamp.replace(tzinfo=None).isoformat(
        timespec="seconds"
    )


def parse_release(
    format_id: str,
    raw: bytes,
    *,
    declared: Mapping[str, Any],
    document: Mapping[str, Any],
    headers: Mapping[str, Any] | None = None,
    url: str = "",
) -> dict[str, Any]:
    if format_id not in FORMATS:
        raise PublicFinanceFormatError(
            "schema_drift", f"unknown public-finance format {format_id!r}"
        )
    if format_id == "de-bundeshaushalt-csv":
        release = parse_bundeshaushalt(raw, document=document)
    elif format_id == "de-be-haushalt-csv":
        release = parse_berlin(
            raw, document=document, districts=dict(declared.get("districts") or {})
        )
    elif format_id == "eu-fts-csv":
        release = parse_fts(raw, document=document)
    else:
        release = parse_gfs(raw, spec=document, url=url)
    if release["published_on"] is None and document.get("published_on"):
        release["published_on"] = _day(document["published_on"])
    if release["published_on"] is None:
        release["published_on"], release["published_at"] = _last_modified(
            {str(k).casefold(): v for k, v in dict(headers or {}).items()}
        )
    if release["published_on"] is None:
        raise PublicFinanceFormatError(
            "schema_drift", "release states no publication date (Stand)"
        )
    items = release.get("figures") or release.get("payments") or [release.get("series")]
    release.update(
        {
            "contract": RELEASE_CONTRACT,
            "provider": FORMATS[format_id]["provider"],
            "format": format_id,
            "jurisdiction": FORMATS[format_id]["jurisdiction"],
            "record_type": FORMATS[format_id]["records"],
            "file_sha256": hashlib.sha256(raw).hexdigest(),
            "content_sha256": _digest(items),
            "item_count": len(items),
        }
    )
    return release


def public_finance_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    declared = dict(source.get("public_finance") or {})
    format_id = declared.get("format")
    if format_id not in FORMATS or FORMATS[format_id]["provider"] != declared.get(
        "provider"
    ):
        raise SourcePackError(
            "invalid_manifest",
            "public-finance sources declare a matching provider and format",
        )
    if (
        declared.get("accounting_basis") not in ACCOUNTING_BASES
        and format_id != "eu-fts-csv"
    ):
        raise SourcePackError(
            "invalid_manifest", "a public-finance source declares its accounting basis"
        )
    documents = list(declared.get("documents") or [])
    if not documents:
        raise SourcePackError(
            "invalid_manifest", "a public-finance source declares its documents"
        )
    if len(documents) > int(dict(source.get("budgets") or {}).get("max_pages", 1)):
        raise SourcePackError(
            "invalid_manifest", "more declared documents than the source's page budget"
        )
    host = (urlsplit(source["endpoint"]).hostname or "").casefold()
    for document in documents:
        parts = urlsplit(document_url(document))
        if parts.scheme != "https" or (parts.hostname or "").casefold() != host:
            raise SourcePackError(
                "invalid_manifest",
                "declared documents are HTTPS files on the endpoint's host",
            )
        if format_id == "eurostat-gfs-jsonstat":
            if document.get("dataset") not in GFS_DATASETS or not document.get(
                "geography"
            ):
                raise SourcePackError(
                    "invalid_manifest",
                    "GFS documents name a selected dataset and a geography",
                )
        elif format_id != "eu-fts-csv":
            try:
                _amount_kinds(document)
            except PublicFinanceFormatError as exc:
                raise SourcePackError("invalid_manifest", str(exc)) from exc
        elif (
            document.get("amount_kind") is not None
            and document["amount_kind"] not in PAYMENT_KINDS
        ):
            raise SourcePackError(
                "invalid_manifest",
                "an FTS document's amount kind is commitment or payment",
            )
    return declared


def unverified(provider: str) -> bool:
    return (
        PROVIDER_CONTRACTS.get(provider, {}).get("access_decision") != "verified-live"
    )


def document_url(document: Mapping[str, Any]) -> str:
    """The declared file URL; a GFS series is addressed through the Eurostat connector's own URL builder."""
    if document.get("dataset"):
        from src.ingestion.connectors.dataset.eurostat import EurostatConnector

        return EurostatConnector()._url(
            str(document["dataset"]),
            str(document.get("geography") or ""),
            dict(sorted(dict(document.get("filters") or {}).items())),
        )
    return str(document.get("url") or "")


class PublicFinanceAdapter:
    """Fetch the declared files on the runtime's default transport; one page (one release) per file."""

    accepts_transport = True
    connector = CONNECTOR

    def __init__(
        self,
        source: Mapping[str, Any],
        *,
        transport: Callable[..., Mapping[str, Any]] | None = None,
        secret: str | None = None,
    ) -> None:
        del secret
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        self.source = json.loads(json.dumps(source))
        self.declared = public_finance_declaration(self.source)
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
            "public_finance": {
                "provider": self.declared["provider"],
                "format": self.declared["format"],
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
                "parameter_forbidden",
                "public-finance runs fetch the declared files only",
            )

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage, _retry_after_ms

        self._check(request)
        documents = list(self.declared["documents"])
        index = 0 if cursor is None else int(cursor) if str(cursor).isdigit() else -1
        if not 0 <= index < len(documents):
            raise SourcePackError("cursor_drift", "cursor names no declared document")
        document = dict(documents[index])
        url = document_url(document)
        host = (urlsplit(self.source["endpoint"]).hostname or "").casefold()
        if (urlsplit(url).hostname or "").casefold() != host or urlsplit(
            url
        ).scheme != "https":
            raise SourcePackError(
                "network_policy",
                "declared files are fetched from the endpoint's host only",
            )
        response = self.transport(
            url=url,
            params={},
            headers={
                "Accept": "text/csv, application/json, text/plain, application/octet-stream"
            },
            timeout=int(self.definition["limits"]["timeout_ms"]) / 1000,
        )
        final_host = (
            urlsplit(str(response.get("final_url") or url)).hostname or ""
        ).casefold()
        if final_host != host:
            raise SourcePackError("network_policy", "file was served from another host")
        status = int(response.get("status", 200))
        headers = {
            str(k).casefold(): v for k, v in dict(response.get("headers") or {}).items()
        }
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError("response_too_large", "file exceeds its byte limit")
        if status == 429:
            raise SourcePackError(
                "rate_limited",
                "provider quota is temporarily exhausted",
                retry_after_ms=_retry_after_ms(headers.get("retry-after")),
            )
        if status in {401, 403}:
            raise SourcePackError(
                "authentication_failed", f"download refused (HTTP {status})"
            )
        if status >= 500:
            raise SourcePackError(
                "source_unavailable", f"provider returned HTTP {status}"
            )
        if status >= 400:
            raise SourcePackError("schema_drift", f"download returned HTTP {status}")
        try:
            release = parse_release(
                self.declared["format"],
                raw,
                declared=self.declared,
                document=document,
                headers=headers,
                url=url,
            )
        except PublicFinanceFormatError as exc:
            raise SourcePackError(
                "response_too_large" if exc.code == "input_limit" else "schema_drift",
                f"{exc.code}: {exc}",
            ) from exc
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        if release["item_count"] > limit:
            # Never a truncated release: a missing line would read as a line without figures.
            raise SourcePackError(
                "budget_exhausted", "release has more rows than the run's result budget"
            )
        header = {
            "contract": RELEASE_CONTRACT,
            "provider": release["provider"],
            "format": release["format"],
            "jurisdiction": release["jurisdiction"],
            "record_type": release["record_type"],
            "document": {k: v for k, v in document.items() if k not in {"amounts"}},
            "amounts": document.get("amounts"),
            "accounting_basis": self.declared.get("accounting_basis"),
            "published_on": release["published_on"],
            "published_at": release.get("published_at"),
            "file_sha256": release["file_sha256"],
            "content_sha256": release["content_sha256"],
            "item_count": release["item_count"],
            "unit": release.get("unit"),
            # Only a fixture transport says so; the runtime's HTTPS transport is live evidence.
            "evidence_origin": "fixture"
            if response.get("origin") == "fixture"
            else "live",
            "url": url,
        }
        if release["record_type"] == "series":
            items = [release["series"]]
        else:
            items = release.get("figures") or release.get("payments")
        records = [
            {
                "id": f"{release['file_sha256'][:16]}:{number}",
                "title": f"{document.get('label') or release['provider']} ({release['published_on']})",
                "url": url,
                "language": "de" if release["jurisdiction"].startswith("DE") else "en",
                "published_at": release["published_on"],
                "content": json.dumps(item, sort_keys=True, ensure_ascii=False),
                "public_finance_release": header,
                "public_finance_item": item,
            }
            for number, item in enumerate(items)
        ]
        receipt = {
            "status": status,
            "provider": release["provider"],
            "document": document.get("label"),
            "published_on": release["published_on"],
            "file_sha256": release["file_sha256"],
            "items": len(records),
            "evidence_origin": header["evidence_origin"],
            "final_page": index + 1 >= len(documents),
        }
        next_cursor = str(index + 1) if index + 1 < len(documents) else None
        return RuntimePage(tuple(records), next_cursor, len(raw), receipt=receipt)


FIXTURE_SECRET = None
ADAPTERS = {CONNECTOR: PublicFinanceAdapter}


def fixture_transport(
    pages: Sequence[Mapping[str, Any]],
) -> Callable[..., Mapping[str, Any]]:
    """Replay authored files keyed by URL path (+ query); responses are marked as fixture evidence."""
    by_key = {page["request"]: page for page in pages}

    def transport(*, url, params, headers, timeout):
        del params, headers, timeout
        parts = urlsplit(url)
        key = parts.path + ("?" + parts.query if parts.query else "")
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


def fixture_request(document: Mapping[str, Any]) -> str:
    parts = urlsplit(document_url(document))
    return parts.path + ("?" + parts.query if parts.query else "")


def replay_native_fixture(
    source: Mapping[str, Any], fixture: Mapping[str, Any]
) -> list[dict[str, Any]]:
    adapter = PublicFinanceAdapter(
        source, transport=fixture_transport(list(fixture["native_pages"]))
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


def day_ms(day: str) -> int:
    return int(
        datetime.combine(
            date.fromisoformat(day), datetime.min.time(), tzinfo=timezone.utc
        ).timestamp()
        * 1000
    )
