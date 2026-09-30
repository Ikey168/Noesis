"""Insurance statistics, SFCR, NAIC and catastrophe-loss acquisition for the Market ``insurance`` feature (#2230).

One native connector, ``insurance``, fetches a source's declared documents one
page at a time and parses them into ``noesis-insurance-record-v1`` records
(:mod:`src.domains.market.insurance`). The source-pack runtime
(:mod:`src.ingestion.source_pack_runtime`) runs it with budgets, receipts and
quarantine. Formats:

* ``eiopa-statistics-csv`` / ``eiopa-statistics-xlsx`` (IN03): EIOPA insurance
  statistics through a **declared column mapping**. Each document is one release
  (vintage), declared with its release label and date. Country, line of business,
  reference period, unit and value are stored as published. A confidential or
  not-reported cell keeps its declared marker and is never read as zero. An
  undeclared non-numeric cell is a rejection.
* ``sfcr-pdf`` (IN04): an insurer's SFCR. The text comes through the existing
  PDF extraction path (:func:`src.ingestion.connectors.paper.pdf_parser.extract_pdf_text`),
  and the declared QRT cells (template, row, column) are quoted from their
  section. A cell that cannot be located, or whose line does not hold exactly the
  declared columns, is ``unknown`` with the reason and is never estimated.
* ``naic-publication`` and ``publication-reference`` (IN05, metadata-only
  sources): a publication reference (title, period, URL, digest) without figures.
  ``naic-market-share-csv`` reads company-level market-share rows keyed by NAIC
  company and group code, but only when the IN01 decision for ``naic-public`` is
  ``in-scope``. It is not today.
* ``catastrophe-estimates-csv`` (IN06): one publisher's loss estimates per row,
  where each row is one publication with its date. They are never merged across
  publishers.

Licence decisions (:data:`src.domains.market.insurance.LICENCE_DECISIONS`) are
configuration that the audit fixes. A declaration states the decision it runs
under (``insurance.access_decision``), and the adapter refuses a declaration
whose decision differs, an ``excluded`` provider, and a format a
``metadata-only`` provider may not use. ``LIVE_VERIFICATION`` marks every
implemented source ``unverified-live`` until a dated live run.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import zipfile
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

from src.domains.market.insurance import (
    CONTRACT,
    ESTIMATE_TYPES,
    LICENCE_DECISIONS,
    REPORTING_LEVELS,
    InsuranceError,
    decimal_text,
    fold,
    iso_day,
    lei_valid,
    normalize_lei,
    normalize_naic,
    validate_record,
)
from src.ingestion.source_packs import SourcePackError

ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
CONNECTOR = "insurance"
# format -> (providers allowed to use it, record kind it yields, the decision it needs)
FORMATS: dict[str, tuple[frozenset[str], str, str]] = {
    "eiopa-statistics-csv": (frozenset({"eiopa-insurance-statistics"}), "supervisory_indicator", "in-scope"),
    "eiopa-statistics-xlsx": (frozenset({"eiopa-insurance-statistics"}), "supervisory_indicator", "in-scope"),
    "sfcr-pdf": (frozenset({"insurer-sfcr"}), "insurer_report", "in-scope"),
    "naic-market-share-csv": (frozenset({"naic-public"}), "supervisory_indicator", "in-scope"),
    "catastrophe-estimates-csv": (
        frozenset({"florida-oir-claims", "noaa-ncei-billion-dollar"}),
        "catastrophe_loss_estimate",
        "in-scope",
    ),
    "publication-reference": (
        frozenset(p for p, d in LICENCE_DECISIONS.items() if d["decision"] == "metadata-only"),
        "publication_reference",
        "metadata-only",
    ),
}
REQUIRED_COLUMNS = {
    "eiopa-statistics-csv": ("country", "indicator", "reference", "amount"),
    "eiopa-statistics-xlsx": ("country", "indicator", "reference", "amount"),
    "naic-market-share-csv": ("company_code", "company_name", "year", "amount"),
    "catastrophe-estimates-csv": ("event_name", "amount"),
}
DEFAULT_MARKERS = ("c", "x", "n/a", "-", "..", "confidential", "not reported")
MAX_ROWS = 50_000
TEMPLATE_CODE = re.compile(r"\bS\.\d{2}\.\d{2}\.\d{2}\b")
NUMBER = re.compile(r"\(?-?\d[\d.,  ']*\)?%?")

PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "eiopa-insurance-statistics": {
        "endpoint": "https://www.eiopa.europa.eu/",
        "formats": ["eiopa-statistics-xlsx", "eiopa-statistics-csv"],
        "identifiers": "country (ISO alpha-2, EEA/EU totals), line of business as published, reference date",
        "rate_limits": "none documented (verify); one workbook per page",
    },
    "insurer-sfcr": {
        "endpoint": "each insurer's investor-relations host (declared per source)",
        "formats": ["sfcr-pdf"],
        "identifiers": "LEI as stated in the report, reporting level, reporting year",
        "rate_limits": "none documented; one report per page",
    },
    "naic-public": {
        "endpoint": "https://content.naic.org/",
        "formats": ["publication-reference", "naic-market-share-csv (only under an in-scope decision)"],
        "identifiers": "NAIC company code, NAIC group code, reporting year",
        "rate_limits": "none documented (verify)",
    },
    "florida-oir-claims": {
        "endpoint": "https://floir.com/",
        "formats": ["catastrophe-estimates-csv"],
        "identifiers": "event name, NHC storm ID where the report states one, report date",
        "rate_limits": "none documented (verify)",
    },
    "noaa-ncei-billion-dollar": {
        "endpoint": "https://www.ncei.noaa.gov/",
        "formats": ["catastrophe-estimates-csv"],
        "identifiers": "event name, begin and end dates",
        "rate_limits": "none documented; archive",
    },
}
for _provider, _entry in LICENCE_DECISIONS.items():
    PROVIDER_CONTRACTS.setdefault(_provider, {"endpoint": _entry["licence"]["terms_url"], "formats": []})
    PROVIDER_CONTRACTS[_provider].update(
        {
            "publisher": _entry["publisher"],
            "access_decision": _entry["decision"],
            "licence": dict(_entry["licence"]),
            "reason": _entry["reason"],
        }
    )
LIVE_VERIFICATION = {
    provider: {
        "status": "excluded" if contract["access_decision"] == "excluded" else "unverified-live",
        "note": contract["reason"]
        if contract["access_decision"] == "excluded"
        else "no dated live run from this runtime; offline fixtures only",
    }
    for provider, contract in PROVIDER_CONTRACTS.items()
}


class InsuranceFormatError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _header(value: Any) -> str:
    return " ".join(str(value or "").replace("﻿", "").split()).casefold()


def _cell(value: Any) -> str | None:
    text = " ".join(str(value if value is not None else "").replace(" ", " ").split()).strip()
    return text or None


# ------------------------------------------------------------------ tabular readers


def _mapped(header: Sequence[Any], body: Sequence[Sequence[Any]], declared: Mapping[str, Any]):
    positions = {_header(name): index for index, name in enumerate(header)}
    columns = dict(declared.get("columns") or {})
    mapping = {field: positions.get(_header(name)) for field, name in columns.items()}
    missing = sorted(field for field, index in mapping.items() if index is None)
    rows = []
    for number, cells in enumerate(body, start=2):
        if not any(_cell(c) for c in cells):
            continue
        if len(rows) >= MAX_ROWS:
            raise InsuranceFormatError("input_limit", "document exceeds the row bound")
        row: dict[str, Any] = {"_row": number}
        for field, index in mapping.items():
            row[field] = _cell(cells[index]) if index is not None and index < len(cells) else None
        rows.append(row)
    return rows, missing


def read_csv(raw: bytes, declared: Mapping[str, Any]):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= 50_000_000:
        raise InsuranceFormatError("input_limit", "document is missing or oversized")
    encoding = str(declared.get("encoding") or "utf-8")
    try:
        text = raw.decode(encoding)
    except (UnicodeDecodeError, LookupError) as exc:
        raise InsuranceFormatError("schema_drift", f"document is not {encoding} text") from exc
    if text.lstrip().startswith("<"):
        raise InsuranceFormatError("schema_drift", "an HTML or XML page was returned instead of the data file")
    reader = list(csv.reader(io.StringIO(text.lstrip("﻿")), delimiter=str(declared.get("delimiter") or ",")))
    if not reader:
        raise InsuranceFormatError("schema_drift", "document has no header row")
    return _mapped(reader[0], reader[1:], declared)


def _xml(raw: bytes):
    import defusedxml.ElementTree as ET

    try:
        return ET.fromstring(raw, forbid_entities=True, forbid_external=True)
    except Exception as exc:  # noqa: BLE001 - any XML failure is provider drift
        raise InsuranceFormatError("schema_drift", "workbook part is not well-formed XML") from exc


def _column_index(ref: str) -> int:
    letters = re.match(r"[A-Z]+", ref or "")
    index = 0
    for ch in letters.group(0) if letters else "A":
        index = index * 26 + (ord(ch) - 64)
    return index - 1


def read_xlsx(raw: bytes, declared: Mapping[str, Any]):
    """The declared sheet of an XLSX workbook as text cells (values exactly as stored), header row declared."""
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
          "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
    rel_ns = "{http://schemas.openxmlformats.org/package/2006/relationships}"
    try:
        book = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise InsuranceFormatError("schema_drift", "document is not an XLSX workbook") from exc
    if sum(i.file_size for i in book.infolist()) > 200_000_000:
        raise InsuranceFormatError("input_limit", "workbook expands beyond the byte bound")
    names = set(book.namelist())
    shared: list[str] = []
    if "xl/sharedStrings.xml" in names:
        for item in _xml(book.read("xl/sharedStrings.xml")).findall("m:si", ns):
            shared.append("".join(t.text or "" for t in item.iter(f"{{{ns['m']}}}t")))
    workbook = _xml(book.read("xl/workbook.xml"))
    rels = {
        r.get("Id"): r.get("Target")
        for r in _xml(book.read("xl/_rels/workbook.xml.rels")).iter(f"{rel_ns}Relationship")
    }
    wanted = declared.get("sheet")
    target = None
    for sheet in workbook.iter(f"{{{ns['m']}}}sheet"):
        if wanted is None or sheet.get("name") == wanted:
            target = rels.get(sheet.get(f"{{{ns['r']}}}id"))
            break
    if not target:
        raise InsuranceFormatError("schema_drift", f"workbook has no sheet {wanted!r}")
    path = target.lstrip("/") if target.startswith("/") else f"xl/{target}"
    rows: list[list[Any]] = []
    for row in _xml(book.read(path)).iter(f"{{{ns['m']}}}row"):
        cells: list[Any] = []
        for cell in row.findall("m:c", ns):
            index = _column_index(cell.get("r") or "")
            while len(cells) < index:
                cells.append(None)
            kind = cell.get("t")
            value = cell.find("m:v", ns)
            if kind == "s" and value is not None:
                text = shared[int(value.text)]
            elif kind == "inlineStr":
                text = "".join(t.text or "" for t in cell.iter(f"{{{ns['m']}}}t"))
            else:
                text = value.text if value is not None else None
            cells.append(text)
        rows.append(cells)
    skip = int(declared.get("header_row") or 1) - 1
    if len(rows) <= skip:
        raise InsuranceFormatError("schema_drift", "sheet has no header row")
    return _mapped(rows[skip], rows[skip + 1:], declared)


# ------------------------------------------------------------------ records


def _record(
    kind: str,
    provider: str,
    source_id: str,
    *,
    url: str,
    document_label: str,
    row: Any,
    file_sha256: str,
    publication_date: str | None,
    **fields: Any,
) -> dict[str, Any]:
    return {
        "contract": CONTRACT,
        "kind": kind,
        "source": {
            "provider": provider,
            "source_id": source_id,
            "url": url,
            "locator": {"document": document_label, **({"row": row} if row is not None else {})},
            "document_sha256": file_sha256,
        },
        "publication_date": publication_date,
        **fields,
    }


def _number(value: Any, declared: Mapping[str, Any]) -> str | None:
    return decimal_text(value, decimal=declared.get("decimal"))


def _empty(scope: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {"records": [], "rejected": [], "counts": {"rows": 0, "out_of_scope": 0, "outside_window": 0},
            "scope": dict(scope or {})}


def parse_eiopa(raw: bytes, declared: Mapping[str, Any], document: Mapping[str, Any], fmt: str) -> dict[str, Any]:
    rows, missing = read_xlsx(raw, declared) if fmt.endswith("xlsx") else read_csv(raw, declared)
    parsed = _empty({"countries": sorted(declared.get("countries") or [])})
    parsed["missing_columns"] = missing
    sha = _sha(raw)
    countries = {c.upper() for c in declared.get("countries") or []}
    years = declared.get("reference_years") or {}
    markers = {m.casefold() for m in (declared.get("markers") or DEFAULT_MARKERS)}
    indicators = dict(declared.get("indicators") or {})
    release_date = iso_day(document.get("release_date")) if document.get("release_date") else None
    for row in rows:
        parsed["counts"]["rows"] += 1
        country = (row.get("country") or "").upper()
        label = row.get("indicator") or ""
        spec = indicators.get(label) or indicators.get(label.casefold())
        if (countries and country not in countries) or (indicators and spec is None):
            parsed["counts"]["out_of_scope"] += 1
            continue
        reference = row.get("reference") or ""
        year_match = re.match(r"(\d{4})", reference)
        year = int(year_match.group(1)) if year_match else None
        if years and (year is None or not years.get("from", 0) <= year <= years.get("to", 9999)):
            parsed["counts"]["outside_window"] += 1
            continue
        raw_value = row.get("amount")
        marker, value, problem = None, None, None
        if raw_value is None:
            problem = "an empty cell is not a published marker"
        elif raw_value.casefold() in markers:
            marker = raw_value
        else:
            try:
                value = _number(raw_value, declared)
            except InsuranceError as exc:
                problem = str(exc)
            if value is None and problem is None:
                problem = "an undeclared marker"
        if problem:
            parsed["rejected"].append({"row": row["_row"], "code": "invalid_row",
                                       "reason": f"value {raw_value!r} is neither a number nor a declared marker "
                                                 f"({problem})"[:200]})
            continue
        spec = dict(spec or {})
        lob = row.get("line_of_business")
        dims = {"country": country, "line_of_business": lob, "reporting_level": row.get("reporting_level")}
        dims = {k: v for k, v in dims.items() if v is not None}
        indicator = spec.get("indicator") or re.sub(r"[^a-z0-9]+", "_", fold(label)).strip("_") or "indicator"
        source_id = "|".join([str(declared.get("dataset")), indicator, country, lob or "*", reference])
        period = {"reference_date": iso_day(reference)} if re.fullmatch(r"\d{4}-\d{2}-\d{2}", reference) else (
            {"reference_year": year} if year and re.fullmatch(r"\d{4}", reference) else {"label": reference}
        )
        parsed["records"].append(
            _record(
                "supervisory_indicator",
                declared["provider"],
                source_id,
                url=document["url"],
                document_label=document["label"],
                row=row["_row"],
                file_sha256=sha,
                publication_date=release_date,
                publisher=LICENCE_DECISIONS[declared["provider"]]["publisher"],
                dataset=str(declared.get("dataset")),
                indicator=indicator,
                indicator_label=label,
                definition=spec.get("definition"),
                dimensions=dims,
                period=period,
                value=value,
                marker=marker,
                unit=row.get("unit") or spec.get("unit") or declared.get("unit") or "as published",
                currency=row.get("currency") or spec.get("currency") or declared.get("currency"),
                release=str(document.get("release")),
            )
        )
    parsed["release"] = {"dataset": str(declared.get("dataset")), "release": str(document.get("release")),
                         "release_date": release_date}
    return parsed


def _pdf_text(raw: bytes) -> tuple[str | None, str]:
    if not raw.startswith(b"%PDF"):
        raise InsuranceFormatError("schema_drift", "document is not a PDF")
    from src.ingestion.connectors.paper.pdf_parser import extract_pdf_text

    try:
        text = extract_pdf_text(raw)
    except Exception:  # noqa: BLE001 - an unreadable PDF leaves every figure unknown
        return None, "pdf-unreadable"
    return text, "pymupdf" if text is not None else "none"


def quote_cell(text: str, figure: Mapping[str, Any], declared: Mapping[str, Any]) -> dict[str, Any]:
    """Locate one declared QRT cell in the extracted text; anything short of one exact reading is unknown."""
    base = {
        "template": figure["template"],
        "row": figure["row"],
        "column": figure["column"],
        "label": figure["label"],
        "unit": figure.get("unit"),
        "currency": figure.get("currency"),
    }

    def unknown(reason: str) -> dict[str, Any]:
        return {**base, "status": "unknown", "value": None, "reason": reason}

    starts = [m.start() for m in re.finditer(re.escape(figure["template"]), text)]
    if not starts:
        return unknown(f"template {figure['template']} not found in the extracted text")
    lines: list[str] = []
    for start in starts:
        following = TEMPLATE_CODE.search(text, start + len(figure["template"]))
        while following and following.group(0) == figure["template"]:
            following = TEMPLATE_CODE.search(text, following.end())
        section = text[start: following.start() if following else len(text)]
        lines += [line for line in section.splitlines() if re.search(rf"\b{figure['row']}\b", line)]
    if not lines:
        return unknown(f"row {figure['row']} not found in template {figure['template']}")
    if len(lines) > 1:
        return unknown(f"row {figure['row']} appears {len(lines)} times in template {figure['template']}")
    line = lines[0]
    after = line.split(figure["row"], 1)[1]
    tokens = [t for t in NUMBER.findall(after) if re.search(r"\d", t)]
    columns = list(figure.get("columns") or [figure["column"]])
    if len(tokens) != len(columns):
        return unknown(
            f"row {figure['row']} holds {len(tokens)} numbers for {len(columns)} declared columns; not read"
        )
    token = tokens[columns.index(figure["column"])]
    percent = token.endswith("%")
    try:
        value = decimal_text(token.rstrip("%"), decimal=declared.get("decimal"))
    except InsuranceError:
        return unknown(f"cell text {token!r} is not a number")
    if value is None:
        return unknown("cell is empty")
    return {**base, "status": "reported", "value": value, "unit": "%" if percent else base["unit"],
            "quoted": line.strip()[:1000]}


def parse_sfcr(raw: bytes, declared: Mapping[str, Any], document: Mapping[str, Any]) -> dict[str, Any]:
    parsed = _empty({"insurers": [i.get("name") for i in declared.get("insurers") or []]})
    parsed["missing_columns"] = []
    sha = _sha(raw)
    parsed["counts"]["rows"] = 1
    text, extractor = _pdf_text(raw)
    stated = dict(document.get("insurer") or {})
    lei = None
    if text:
        match = re.search(r"\b(?:LEI|Legal Entity Identifier)\b[^A-Z0-9]{0,40}([A-Z0-9]{18}[0-9]{2})\b", text)
        lei = match.group(1) if match and lei_valid(match.group(1)) else None
    sample = {
        (fold(i.get("name")), i.get("reporting_level")): i for i in declared.get("insurers") or []
    }
    levels = [lvl for (name, lvl) in sample if name == fold(stated.get("name"))]
    level = stated.get("reporting_level") or (levels[0] if len(levels) == 1 else None)
    member = sample.get((fold(stated.get("name")), level))
    if member is None:
        parsed["counts"]["out_of_scope"] += 1
        return parsed
    if text is not None and fold(member["name"]) not in fold(text):
        # The declared URL served a report that does not name the declared insurer: never attributed to it.
        parsed["counts"]["out_of_scope"] += 1
        parsed["rejected"].append({"row": 1, "code": "out_of_scope",
                                   "reason": f"the report does not name the declared insurer {member['name']!r}"})
        return parsed
    declared_lei = normalize_lei(stated.get("lei") or member.get("lei")) or None
    if lei and declared_lei and lei != declared_lei:
        parsed["counts"]["out_of_scope"] += 1
        parsed["rejected"].append({"row": 1, "code": "out_of_scope",
                                   "reason": f"the report states LEI {lei}, not the declared {declared_lei}"})
        return parsed
    figures = []
    for figure in declared.get("figures") or []:
        if figure.get("reporting_level") and figure["reporting_level"] != level:
            continue  # group and solo reports use different QRT variants
        if text is None:
            figures.append({"template": figure["template"], "row": figure["row"], "column": figure["column"],
                            "label": figure["label"], "unit": figure.get("unit"), "currency": figure.get("currency"),
                            "status": "unknown", "value": None,
                            "reason": "no text could be extracted from the PDF" if extractor != "none"
                            else "no PDF extractor is installed (PyMuPDF)"})
        else:
            figures.append(quote_cell(text, figure, declared))
    year = int(document.get("reporting_year") or 0)
    insurer = {"name": member["name"], "lei": lei or declared_lei, "country": member.get("country"),
               "reporting_level": level}
    key = insurer["lei"] or fold(member["name"])
    parsed["records"].append(
        _record(
            "insurer_report",
            declared["provider"],
            f"{key}|sfcr|{year}|{level}",
            url=document["url"],
            document_label=document["label"],
            row=None,
            file_sha256=sha,
            publication_date=iso_day(document.get("publication_date")) if document.get("publication_date") else None,
            insurer={k: v for k, v in insurer.items() if v is not None},
            report_type="sfcr",
            reporting_year=year,
            language=document.get("language"),
            document={"url": document["url"], "sha256": sha, "media_type": "application/pdf",
                      "extractor": extractor},
            figures=figures,
            corrects=document.get("corrects"),
            unknowns=["lei"] if not insurer["lei"] else [],
        )
    )
    return parsed


def parse_naic_csv(raw: bytes, declared: Mapping[str, Any], document: Mapping[str, Any]) -> dict[str, Any]:
    rows, missing = read_csv(raw, declared)
    parsed = _empty({"years": declared.get("reference_years")})
    parsed["missing_columns"] = missing
    sha = _sha(raw)
    for row in rows:
        parsed["counts"]["rows"] += 1
        code = normalize_naic(row.get("company_code"))
        if not re.fullmatch(r"\d{5}", code):
            parsed["rejected"].append({"row": row["_row"], "code": "invalid_row", "reason": "no NAIC company code"})
            continue
        try:
            value = _number(row.get("amount"), declared)
        except InsuranceError as exc:
            parsed["rejected"].append({"row": row["_row"], "code": "invalid_row", "reason": str(exc)[:200]})
            continue
        indicator = str(declared.get("indicator") or "direct_written_premiums")
        group = normalize_naic(row.get("group_code")) or None
        parsed["records"].append(
            _record(
                "supervisory_indicator",
                declared["provider"],
                f"{declared.get('dataset')}|{indicator}|{code}|{row.get('line') or '*'}|{row.get('year')}",
                url=document["url"],
                document_label=document["label"],
                row=row["_row"],
                file_sha256=sha,
                publication_date=iso_day(document.get("publication_date")) if document.get("publication_date")
                else None,
                publisher=LICENCE_DECISIONS[declared["provider"]]["publisher"],
                dataset=str(declared.get("dataset")),
                indicator=indicator,
                indicator_label=str(declared.get("indicator_label") or indicator),
                definition=declared.get("definition"),
                dimensions={"country": "US", "line_of_business": row.get("line") or "all lines"},
                insurer={"name": row.get("company_name"), "naic_company_code": code, "naic_group_code": group,
                         "country": "US", "reporting_level": "solo"},
                period={"reference_year": int(row["year"]) if (row.get("year") or "").isdigit() else None,
                        "label": row.get("year")},
                value=value,
                marker=None if value is not None else (row.get("amount") or "(empty)"),
                unit=str(declared.get("unit") or "USD"),
                currency=declared.get("currency") or "USD",
                release=str(document.get("release") or document.get("publication_date") or document["label"]),
            )
        )
    return parsed


def parse_estimates(raw: bytes, declared: Mapping[str, Any], document: Mapping[str, Any]) -> dict[str, Any]:
    rows, missing = read_csv(raw, declared)
    window = dict(declared.get("window") or {})
    parsed = _empty({"window": window, "events": declared.get("events")})
    parsed["missing_columns"] = missing
    sha = _sha(raw)
    events = {fold(e) for e in declared.get("events") or []}
    publisher = LICENCE_DECISIONS[declared["provider"]]["publisher"]
    for row in rows:
        parsed["counts"]["rows"] += 1
        name = row.get("event_name")
        if not name:
            parsed["rejected"].append({"row": row["_row"], "code": "invalid_row", "reason": "no event name"})
            continue
        if events and fold(name) not in events:
            parsed["counts"]["out_of_scope"] += 1
            continue
        try:
            begin = iso_day(row.get("begin_date")) if row.get("begin_date") else None
            published = iso_day(row.get("publication_date") or document.get("publication_date"))
            value = _number(row.get("amount"), declared)
            low, high = _number(row.get("low"), declared), _number(row.get("high"), declared)
        except InsuranceError as exc:
            parsed["rejected"].append({"row": row["_row"], "code": "invalid_row", "reason": str(exc)[:200]})
            continue
        if begin and ((window.get("from") and begin < window["from"]) or (window.get("to") and begin > window["to"])):
            parsed["counts"]["outside_window"] += 1
            continue
        if published is None:
            parsed["rejected"].append({"row": row["_row"], "code": "invalid_row",
                                       "reason": "neither the row nor the document states a publication date"})
            continue
        identifiers = {k: row.get(k) for k in ("glide", "nhc_storm_id", "usgs_event_id") if row.get(k)}
        measure = row.get("measure") or str(declared.get("measure"))
        estimate_type = row.get("estimate_type") or declared.get("estimate_type")
        reference = row.get("event_reference")
        parsed["records"].append(
            _record(
                "catastrophe_loss_estimate",
                declared["provider"],
                f"{reference or fold(name)}|{estimate_type}|{fold(measure)}",
                url=document["url"],
                document_label=document["label"],
                row=row["_row"],
                file_sha256=sha,
                publication_date=published,
                publisher=publisher,
                event={"name": name, "reference": reference, "identifiers": identifiers, "begin_date": begin,
                       "end_date": iso_day(row.get("end_date")) if row.get("end_date") else None,
                       "area": row.get("area")},
                estimate_type=estimate_type,
                measure=measure,
                value=value,
                range={"low": low, "high": high} if (low is not None or high is not None) else None,
                as_published=row.get("as_published"),
                unit=row.get("unit") or str(declared.get("unit") or "as published"),
                currency=row.get("currency") or declared.get("currency"),
                cites=[u for u in (row.get("cites") or "").split() if u.startswith("https://")],
            )
        )
    return parsed


def parse_reference(raw: bytes, declared: Mapping[str, Any], document: Mapping[str, Any]) -> dict[str, Any]:
    parsed = _empty({})
    parsed["missing_columns"] = []
    parsed["counts"]["rows"] = 1
    parsed["records"].append(
        _record(
            "publication_reference",
            declared["provider"],
            str(document.get("reference_id") or document["label"]),
            url=document["url"],
            document_label=document["label"],
            row=None,
            file_sha256=_sha(raw),
            publication_date=iso_day(document.get("publication_date")) if document.get("publication_date") else None,
            publisher=LICENCE_DECISIONS[declared["provider"]]["publisher"],
            title=str(document.get("title") or document["label"]),
            period=document.get("period"),
        )
    )
    return parsed


def _validated(parsed: dict[str, Any]) -> dict[str, Any]:
    """Validate every record as the store will; a record that fails becomes a rejection, never a failed page."""
    kept = []
    for record in parsed["records"]:
        try:
            validate_record(json.loads(json.dumps(record)))
        except InsuranceError as exc:
            parsed["rejected"].append({"row": record["source"]["locator"].get("row"),
                                       "source_id": record["source"]["source_id"], "code": exc.code,
                                       "reason": str(exc)[:200]})
            continue
        kept.append(record)
    parsed["records"] = kept
    return parsed


def parse_document(fmt: str, raw: bytes, declared: Mapping[str, Any], document: Mapping[str, Any]) -> dict[str, Any]:
    if fmt.startswith("eiopa-statistics"):
        parsed = parse_eiopa(raw, declared, document, fmt)
    elif fmt == "sfcr-pdf":
        parsed = parse_sfcr(raw, declared, document)
    elif fmt == "naic-market-share-csv":
        parsed = parse_naic_csv(raw, declared, document)
    elif fmt == "catastrophe-estimates-csv":
        parsed = parse_estimates(raw, declared, document)
    elif fmt == "publication-reference":
        parsed = parse_reference(raw, declared, document)
    else:
        raise InsuranceFormatError("schema_drift", f"unsupported format {fmt}")
    required = [c for c in REQUIRED_COLUMNS.get(fmt, ()) if c in parsed.get("missing_columns", [])]
    if required:
        raise InsuranceFormatError("schema_drift", f"document lacks the declared column(s) {', '.join(required)}")
    return _validated(parsed)


# ------------------------------------------------------------------ declaration and runtime adapter


def insurance_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    """Check a source's ``insurance`` declaration against the IN01 decisions; refuse what they do not grant."""
    declared = dict(source.get("insurance") or {})
    provider, fmt = declared.get("provider"), declared.get("format")
    if provider not in LICENCE_DECISIONS:
        raise SourcePackError("invalid_mapping", "insurance sources declare a known provider")
    decision = LICENCE_DECISIONS[provider]
    if decision["decision"] == "excluded":
        raise SourcePackError("licence_excluded", f"{provider} is excluded (IN01): {decision['reason']}")
    if declared.get("access_decision") != decision["decision"]:
        raise SourcePackError(
            "invalid_mapping",
            f"{provider} runs under the recorded decision {decision['decision']!r}; the declaration states "
            f"{declared.get('access_decision')!r}",
        )
    if fmt not in FORMATS:
        raise SourcePackError("invalid_mapping", f"format is one of {sorted(FORMATS)}")
    providers, kind, needed = FORMATS[fmt]
    if provider not in providers or decision["decision"] != needed or kind not in decision["kinds"]:
        raise SourcePackError(
            "metadata_only" if decision["decision"] == "metadata-only" else "invalid_mapping",
            f"{provider} ({decision['decision']}) may not use {fmt}",
        )
    if dict(source.get("auth") or {}).get("kind", "none") != "none":
        raise SourcePackError("licence_excluded", "credentialed (subscription) access is never used for insurance")
    if declared.get("estimate_type") and declared["estimate_type"] not in ESTIMATE_TYPES:
        raise SourcePackError("invalid_mapping", f"estimate_type is one of {ESTIMATE_TYPES}")
    for member in declared.get("insurers") or []:
        if member.get("reporting_level", "unknown") not in REPORTING_LEVELS or not member.get("name"):
            raise SourcePackError("invalid_mapping", "declared insurers carry a name and a reporting level")
        if member.get("lei") and not lei_valid(member["lei"]):
            raise SourcePackError("invalid_mapping", "a declared insurer LEI has invalid check digits")
    host = (urlsplit(source["endpoint"]).hostname or "").casefold()
    documents = list(declared.get("documents") or [])
    if not documents:
        raise SourcePackError("invalid_mapping", "insurance sources declare their documents")
    for document in documents:
        parts = urlsplit(str(document.get("url") or ""))
        if parts.scheme != "https" or (parts.hostname or "").casefold() != host:
            raise SourcePackError("invalid_mapping", "documents are HTTPS URLs on the endpoint host")
        if not document.get("label"):
            raise SourcePackError("invalid_mapping", "documents carry a label")
        if fmt.startswith("eiopa") and not document.get("release"):
            raise SourcePackError("invalid_mapping", "each EIOPA document declares its release (vintage)")
        if fmt == "sfcr-pdf" and not (document.get("insurer") and document.get("reporting_year")):
            raise SourcePackError("invalid_mapping", "each SFCR document declares its insurer and reporting year")
    return declared


class InsuranceAdapter:
    """Fetch the declared documents one page at a time on the runtime's same-host transport."""

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
        self.declared = insurance_declaration(self.source)
        if transport is None:
            from functools import partial

            transport = partial(HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"]))
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
            "insurance": {
                "provider": self.declared["provider"],
                "format": self.declared["format"],
                "access_decision": self.declared["access_decision"],
                "documents": [d["label"] for d in self.declared["documents"]],
            },
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _check(self, request: Mapping[str, Any]) -> None:
        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError("operation_forbidden", "operation is not declared by the source")
        if set(request) - {"operation", "parameters", "limit", "from_ms", "to_ms"}:
            raise SourcePackError("parameter_forbidden", "runtime adapter received undeclared controls")
        if dict(request.get("parameters") or {}):
            raise SourcePackError("parameter_forbidden", "insurance runs fetch the declared documents only")

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage, _retry_after_ms

        self._check(request)
        documents = self.declared["documents"]
        try:
            index = 0 if cursor is None else int(cursor)
        except ValueError as exc:
            raise SourcePackError("cursor_drift", "cursor is not a document index") from exc
        if not 0 <= index < len(documents):
            raise SourcePackError("cursor_drift", "cursor is outside the declared documents")
        document = documents[index]
        url = document["url"]
        host = (urlsplit(url).hostname or "").casefold()
        fmt = self.declared["format"]
        accept = {
            "sfcr-pdf": "application/pdf",
            "eiopa-statistics-xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "publication-reference": "application/pdf, text/html",
        }.get(fmt, "text/csv, text/plain")
        response = self.transport(
            url=url, params={}, headers={"Accept": accept},
            timeout=int(self.definition["limits"]["timeout_ms"]) / 1000,
        )
        final_host = (urlsplit(str(response.get("final_url") or url)).hostname or "").casefold()
        if final_host != host:
            raise SourcePackError("network_policy", "document was served from another host")
        status = int(response.get("status", 200))
        headers = {str(k).casefold(): v for k, v in dict(response.get("headers") or {}).items()}
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError("response_too_large", "document exceeds its byte limit")
        if status == 429:
            raise SourcePackError("rate_limited", "provider quota is temporarily exhausted",
                                  retry_after_ms=_retry_after_ms(headers.get("retry-after")))
        if status in {401, 403}:
            raise SourcePackError("authentication_failed", f"document refused (HTTP {status})")
        if status >= 500:
            raise SourcePackError("source_unavailable", f"provider returned HTTP {status}")
        if status >= 400:
            raise SourcePackError("schema_drift", f"document returned HTTP {status}")
        try:
            parsed = parse_document(fmt, raw, self.declared, document)
        except InsuranceFormatError as exc:
            raise SourcePackError(
                "response_too_large" if exc.code == "input_limit" else "schema_drift", f"{exc.code}: {exc}"
            ) from exc
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        if len(parsed["records"]) > limit:
            raise SourcePackError("budget_exhausted", "document has more records than the run's result budget")
        records = []
        for record in parsed["records"]:
            label = (record.get("event") or {}).get("name") or (record.get("insurer") or {}).get("name") or record.get(
                "indicator_label") or record.get("title")
            records.append(
                {
                    "id": f"{self.declared['provider']}:{record['source']['source_id']}:{record['publication_date']}",
                    "title": f"{record['kind']}: {label or record['source']['source_id']}"[:300],
                    "url": url,
                    "language": document.get("language") or "en",
                    "published_at": record.get("publication_date"),
                    "content": json.dumps(record, sort_keys=True, ensure_ascii=False),
                    "insurance_record": record,
                }
            )
        for item in parsed["rejected"]:
            records.append(
                {
                    "id": f"{self.declared['provider']}:rejected:{document['label']}:{item.get('row')}",
                    "url": url,
                    "rejection": {"code": item["code"], "reason": item["reason"], "row": item.get("row")},
                }
            )
        receipt = {
            "status": status,
            "document": document["label"],
            "file_sha256": _sha(raw),
            "access_decision": self.declared["access_decision"],
            "licence": LICENCE_DECISIONS[self.declared["provider"]]["licence"],
            "counts": {**parsed["counts"], "records": len(parsed["records"]), "rejected": len(parsed["rejected"])},
            "missing_columns": parsed.get("missing_columns", []),
            "scope": parsed["scope"],
            "final_page": index + 1 >= len(documents),
            **({"release": parsed["release"]} if parsed.get("release") else {}),
        }
        next_cursor = str(index + 1) if index + 1 < len(documents) else None
        return RuntimePage(tuple(records), next_cursor, len(raw), receipt=receipt)


FIXTURE_SECRET = None
ADAPTERS = {CONNECTOR: InsuranceAdapter}


def fixture_transport(pages: Sequence[Mapping[str, Any]]) -> Callable[..., Mapping[str, Any]]:
    """Replay authored documents keyed by URL path (+ query); binary bodies travel as base64."""
    import base64

    by_key = {page["request"]: page for page in pages}

    def transport(*, url, params, headers, timeout):
        del params, headers, timeout
        parts = urlsplit(url)
        key = parts.path + ("?" + parts.query if parts.query else "")
        page = by_key.get(key)
        if page is None:
            raise SourcePackError("fixture_missing", f"no native page for {key}")
        if page.get("body_base64") is not None:
            content = base64.b64decode(page["body_base64"])
        else:
            body = page.get("body")
            content = body.encode() if isinstance(body, str) else b"" if body is None else json.dumps(body).encode()
        return {
            "status": int(page.get("status", 200)),
            "headers": dict(page.get("headers") or {}),
            "content": content,
            **({"final_url": page["final_url"]} if page.get("final_url") else {}),
        }

    return transport


def replay_native_fixture(source: Mapping[str, Any], fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    adapter = InsuranceAdapter(source, transport=fixture_transport(list(fixture["native_pages"])))
    records, cursor = [], None
    while True:
        page = adapter.fetch_page(
            {"operation": min(source["operations"]), "parameters": {},
             "limit": int(source["budgets"]["max_results"])},
            cursor=cursor,
        )
        records.extend(dict(item) for item in page.records)
        cursor = page.next_cursor
        if cursor is None:
            return records
