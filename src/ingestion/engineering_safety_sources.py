"""Bounded native connectors for the Engineering Safety pack (#2059, ES03-ES09).

One connector, ``engineering-safety``, serves every audited provider through a
declared ``engineering_safety`` block (see
``docs/development/engineering-safety-evidence/source-audit.md``):

* ``faa-ad`` (ES03) - FAA airworthiness directives from the Federal Register
  API: one declared FR document number per page, its JSON metadata and its
  raw text. The AD text after ``§ 39.13 [Amended]`` is split into its
  lettered paragraphs (headings written as plain lines), scoped to the AD;
* ``easa-ad`` (ES04) - one declared EASA AD number per page from the EASA AD
  publishing tool's AD page (label/value rows);
* ``ntsb`` (ES05) - one declared NTSB number or recommendation number per
  page from CAROL's JSON case and recommendation views;
* ``phmsa`` (ES06) - declared PHMSA incident flat files (one file per page),
  filtered to a year window, with declared column names and units;
* ``csb`` (ES07) - one declared CSB investigation page per page, its details
  rows, key findings and recommendations table (sections scoped by heading,
  including headings written as bold paragraphs);
* ``nhtsa-odi`` / ``nhtsa-complaints`` (ES08) - the ODI investigations flat
  file filtered to declared makes and models, and complaints by declared
  make, model and year with a count bound;
* ``bfu`` (ES09) - one declared BFU report page (German, stored verbatim);
* ``bea`` (ES09) - ``link-only``: declared report entries become records with
  their metadata and link; nothing is fetched.

Each page yields ``noesis-engineering-safety-record-v1`` statements: what the
authority published, verbatim, with locators. Fetch-time values never enter a
statement, so re-acquiring unchanged content yields an identical statement.
Nothing here matches subjects, infers causes, or determines compliance.

Every native field name and path below is written from the providers'
public documentation as known when the audit was written and is marked
``(verify)`` there; the offline fixtures are authored, fictional envelopes.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

from src.ingestion.source_packs import SourcePackError
from src.kb.engineering_safety_records import AUTHORITIES, CONTRACT, clean

ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
CONNECTOR = "engineering-safety"
FIXTURE_SECRET = None
MAX_SELECTION = 50
MAX_COMPLAINTS = 500
MAX_DOCUMENTS = 10
MAX_ENTRIES = 50
ID_PATTERNS = {
    "document_number": re.compile(r"^\d{4}-\d{5}$"),
    "ad_number": re.compile(r"^\d{4}-\d{4}(?:R\d+)?$"),
    "ntsb_number": re.compile(r"^[A-Z]{3}\d{2}[A-Z]{2}\d{3}[A-Z]?$"),
    "recommendation_number": re.compile(r"^[AHMPRI]-\d{2}-\d{1,3}$"),
    "investigation": re.compile(r"^[a-z0-9][a-z0-9-]{1,80}$"),
    "report_id": re.compile(r"^[0-9A-Z][0-9A-Z-]{2,30}$"),
    "model_year": re.compile(r"^\d{4}$"),
}
SELECTORS: dict[str, tuple[frozenset[str], ...]] = {
    "faa-ad": (frozenset({"document_number"}),),
    "easa-ad": (frozenset({"ad_number"}),),
    "ntsb": (frozenset({"ntsb_number"}), frozenset({"recommendation_number"})),
    "csb": (frozenset({"investigation"}),),
    "nhtsa-complaints": (frozenset({"make", "model", "model_year"}),),
    "bfu": (frozenset({"report_id"}),),
}
DOCUMENT_PROVIDERS = frozenset({"phmsa", "nhtsa-odi"})
LINK_ONLY_PROVIDERS = frozenset({"bea"})

# ES01 access decisions (docs/development/engineering-safety-evidence/source-audit.md). ``verify`` lists what a
# dated live run must confirm before live acquisition is accepted (ES17, #2078).
PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "faa-ad": {
        "publisher": "Office of the Federal Register (FAA airworthiness directives)",
        "documentation": "https://www.federalregister.gov/developers/documentation/api/v1",
        "access": "GET /api/v1/documents/{document_number}.json, then its raw_text_url on the same host",
        "authentication": "none",
        "decision": "implement",
        "verify": [
            "raw_text_url host and path",
            "AD heading line format",
            "effective_on field",
            "rate limits",
        ],
    },
    "faa-drs": {
        "publisher": "FAA Dynamic Regulatory System",
        "documentation": "https://drs.faa.gov/",
        "decision": "not implemented",
        "reason": "no documented public API for bounded AD status retrieval (verify); the Federal Register "
        "publication is the authoritative AD text and number",
        "verify": ["whether DRS offers a documented export"],
    },
    "easa-ad": {
        "publisher": "European Union Aviation Safety Agency (AD publishing tool)",
        "documentation": "https://ad.easa.europa.eu/",
        "access": "GET /ad/{ad_number} (AD page with label/value rows); the AD PDF is linked, never mirrored",
        "authentication": "none",
        "decision": "implement",
        "verify": [
            "terms of use for automated retrieval",
            "page structure and row labels",
            "date format",
        ],
    },
    "ntsb": {
        "publisher": "National Transportation Safety Board (CAROL)",
        "documentation": "https://data.ntsb.gov/carol-main-public/",
        "access": "GET cases/{ntsb_number} and recommendations/{number} JSON views",
        "authentication": "none",
        "decision": "implement",
        "verify": [
            "CAROL JSON endpoint paths and field names",
            "report status values",
            "rate limits",
        ],
    },
    "ntsb-monthly": {
        "publisher": "National Transportation Safety Board (monthly datasets)",
        "documentation": "https://www.ntsb.gov/Pages/monthly.aspx",
        "decision": "not implemented",
        "reason": "Microsoft Access databases; CAROL serves the same bounded cases per NTSB number (verify)",
        "verify": [],
    },
    "phmsa": {
        "publisher": "Pipeline and Hazardous Materials Safety Administration",
        "documentation": "https://www.phmsa.dot.gov/data-and-statistics/pipeline/"
        "distribution-transmission-gathering-lng-and-liquid-accident-and-incident-data",
        "access": "declared tab-delimited incident files (unzipped), filtered to a year window",
        "authentication": "none",
        "decision": "implement",
        "verify": [
            "file URLs (published as zip archives)",
            "column names",
            "file vintage date",
        ],
    },
    "csb": {
        "publisher": "U.S. Chemical Safety and Hazard Investigation Board",
        "documentation": "https://www.csb.gov/investigations/",
        "access": "one declared investigation page per request; report PDFs and videos are linked, never mirrored",
        "authentication": "none",
        "decision": "implement",
        "verify": [
            "robots.txt and terms",
            "page section headings",
            "recommendations table columns",
        ],
    },
    "nhtsa-odi": {
        "publisher": "NHTSA Office of Defects Investigation",
        "documentation": "https://www.nhtsa.gov/nhtsa-datasets-and-apis",
        "access": "declared ODI investigations flat file (tab-delimited, no header), filtered to declared makes",
        "authentication": "none",
        "decision": "implement",
        "verify": [
            "flat-file URL and column order",
            "date format",
            "action-number format",
        ],
    },
    "nhtsa-complaints": {
        "publisher": "NHTSA Office of Defects Investigation",
        "documentation": "https://www.nhtsa.gov/nhtsa-datasets-and-apis",
        "access": "GET complaints/complaintsByVehicle?make=&model=&modelYear= per declared vehicle, count-bounded",
        "authentication": "none",
        "decision": "implement",
        "verify": [
            "response envelope",
            "date format",
            "whether partial VINs are served (never stored)",
        ],
    },
    "nhtsa-recalls": {
        "publisher": "National Highway Traffic Safety Administration",
        "decision": "not implemented",
        "reason": "recall campaigns are acquired by the Products safety feature (products-displays nhtsa-recalls, "
        "#1916); ODI investigations link to those notices by campaign number",
        "verify": [],
    },
    "bfu": {
        "publisher": "Bundesstelle für Flugunfalluntersuchung",
        "documentation": "https://www.bfu-web.de/",
        "access": "one declared report page per request (German, verbatim); PDFs linked only",
        "authentication": "none",
        "decision": "implement",
        "verify": [
            "terms of reuse",
            "report page structure and labels",
            "recommendation numbering",
        ],
    },
    "bea": {
        "publisher": "Bureau d'Enquêtes et d'Analyses pour la sécurité de l'aviation civile",
        "documentation": "https://bea.aero/",
        "access": "declared report entries (metadata and link); nothing is fetched",
        "authentication": "none",
        "decision": "link-only",
        "reason": "reports are PDFs without a documented machine interface and reuse terms are unconfirmed (verify)",
        "verify": ["terms of reuse", "whether a structured listing exists"],
    },
}
LIVE_VERIFICATION = {
    provider: {
        "status": "unverified-live"
        if contract["decision"] in {"implement", "link-only"}
        else "not-implemented",
        "note": "no dated live run from this runtime; offline fixtures only"
        if contract["decision"] in {"implement", "link-only"}
        else contract["reason"],
    }
    for provider, contract in PROVIDER_CONTRACTS.items()
}

_MONTHS = {
    m: i
    for i, m in enumerate(
        (
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ),
        start=1,
    )
}
_MONTHS.update(
    {
        "januar": 1,
        "februar": 2,
        "märz": 3,
        "mai": 5,
        "juni": 6,
        "juli": 7,
        "oktober": 10,
        "dezember": 12,
    }
)


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


def _text(value: Any) -> str | None:
    if value is None or isinstance(value, (Mapping, list)):
        return None
    text = re.sub(r"[ \t\r\f\v]+", " ", str(value)).strip()
    return text or None


def parse_date(value: Any, *, day_first: bool = True) -> str | None:
    """A published date as ISO ``YYYY-MM-DD``; ``None`` when absent or unparseable (the raw text is kept beside it)."""
    text = (_text(value) or "").rstrip(".")
    if not text:
        return None
    match = re.fullmatch(
        r"(\d{4})-(\d{2})-(\d{2})(?:[T ][0-9:.]+(?:Z|[+-]\d{2}:?\d{2})?)?", text
    )
    if match:
        year, month, day = (int(p) for p in match.groups())
    elif re.fullmatch(r"\d{8}", text):
        year, month, day = int(text[:4]), int(text[4:6]), int(text[6:])
    elif match := re.fullmatch(
        r"(\d{1,2})[./](\d{1,2})[./](\d{4})(?:\s+[0-9:]+)?", text
    ):
        first, second, year = (int(p) for p in match.groups())
        day, month = (first, second) if day_first else (second, first)
    elif match := re.fullmatch(r"([A-Za-zä]+)\s+(\d{1,2}),\s*(\d{4})", text):
        month, day, year = (
            _MONTHS.get(match.group(1).casefold(), 0),
            int(match.group(2)),
            int(match.group(3)),
        )
    elif match := re.fullmatch(r"(\d{1,2})\.?\s+([A-Za-zä]+)\s+(\d{4})", text):
        day, month, year = (
            int(match.group(1)),
            _MONTHS.get(match.group(2).casefold(), 0),
            int(match.group(3)),
        )
    else:
        return None
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def _authority(provider: str) -> dict[str, str]:
    declared, code, _ = AUTHORITIES[provider]
    return {"code": code, "declared": declared}


def _statement(
    provider: str, kind: str, native_id: str, **fields: Any
) -> dict[str, Any]:
    statement = {
        "contract": CONTRACT,
        "provider": provider,
        "record_kind": kind,
        "native_id": native_id,
        "authority": _authority(provider),
        "access": fields.pop("access", "acquired"),
    }
    for key, value in fields.items():
        statement[key] = value
    # Absent values are omitted at every depth, never emitted as null or "None".
    return clean(statement)


# ------------------------------------------------------------------ declarations


def declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    """The validated ``engineering_safety`` block of a source: bounded selection, documents or link-only entries."""
    if str(source.get("connector") or "") != CONNECTOR:
        raise SourcePackError("invalid_mapping", "not an engineering-safety source")
    declared = dict(source.get("engineering_safety") or {})
    provider = str(declared.get("provider") or "")
    if provider not in AUTHORITIES:
        raise SourcePackError(
            "invalid_mapping",
            f"{provider!r} is not an audited engineering-safety provider",
        )
    if (
        PROVIDER_CONTRACTS[provider]["decision"] == "link-only"
        or provider in LINK_ONLY_PROVIDERS
    ):
        entries = declared.get("entries")
        if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_ENTRIES:
            raise SourcePackError(
                "unbounded_source",
                f"link-only sources declare 1-{MAX_ENTRIES} report entries",
            )
        for entry in entries:
            if (
                not isinstance(entry, Mapping)
                or not entry.get("report_id")
                or not entry.get("url")
            ):
                raise SourcePackError(
                    "invalid_mapping",
                    "each link-only entry names its report id and url",
                )
            host = (urlsplit(str(entry["url"])).hostname or "").casefold()
            if not host.endswith(
                (urlsplit(source["endpoint"]).hostname or "")
                .casefold()
                .removeprefix("www.")
            ):
                raise SourcePackError(
                    "invalid_mapping",
                    "a link-only entry links to the provider's own host",
                )
        return {**declared, "provider": provider, "mode": "link-only"}
    if provider in DOCUMENT_PROVIDERS:
        documents = declared.get("documents")
        if not isinstance(documents, list) or not 1 <= len(documents) <= MAX_DOCUMENTS:
            raise SourcePackError(
                "unbounded_source", f"file sources declare 1-{MAX_DOCUMENTS} documents"
            )
        for document in documents:
            if (
                not isinstance(document, Mapping)
                or not document.get("url")
                or not parse_date(document.get("vintage"))
            ):
                raise SourcePackError(
                    "invalid_mapping", "each file declares its url and ISO vintage date"
                )
            if (
                urlsplit(str(document["url"])).hostname
                != urlsplit(source["endpoint"]).hostname
            ):
                raise SourcePackError(
                    "invalid_mapping",
                    "declared files are served from the source endpoint host",
                )
        window = dict(declared.get("window") or {})
        if provider == "phmsa":
            if (
                not isinstance(window.get("year_from"), int)
                or not isinstance(window.get("year_to"), int)
                or window["year_from"] > window["year_to"]
                or window["year_to"] - window["year_from"] > 10
            ):
                raise SourcePackError(
                    "unbounded_source",
                    "PHMSA sources declare a year window of at most 10 years",
                )
            if not dict(declared.get("columns") or {}).get("report_id"):
                raise SourcePackError(
                    "invalid_mapping", "PHMSA sources declare their column names"
                )
        else:
            vehicles = declared.get("vehicles")
            if (
                not isinstance(vehicles, list)
                or not 1 <= len(vehicles) <= MAX_SELECTION
                or not all(isinstance(v, Mapping) and v.get("make") for v in vehicles)
            ):
                raise SourcePackError(
                    "unbounded_source",
                    "ODI sources declare 1-50 makes (optionally models)",
                )
            if (
                not isinstance(declared.get("columns"), list)
                or "action_number" not in declared["columns"]
            ):
                raise SourcePackError(
                    "invalid_mapping",
                    "ODI sources declare the flat file's column order",
                )
        return {**declared, "provider": provider, "mode": "documents"}
    selection = declared.get("selection")
    if not isinstance(selection, list) or not 1 <= len(selection) <= MAX_SELECTION:
        raise SourcePackError(
            "unbounded_source",
            f"engineering-safety sources need 1-{MAX_SELECTION} selectors",
        )
    for item in selection:
        if (
            not isinstance(item, Mapping)
            or frozenset(set(item) - {"label"}) not in SELECTORS[provider]
        ):
            raise SourcePackError(
                "invalid_mapping",
                f"{provider} selectors use "
                + " or ".join("+".join(sorted(s)) for s in SELECTORS[provider]),
            )
        for key, pattern in ID_PATTERNS.items():
            if key in item and not pattern.fullmatch(str(item[key])):
                raise SourcePackError(
                    "invalid_mapping", f"{key} {item[key]!r} is not a valid identifier"
                )
    if provider == "nhtsa-complaints":
        bound = declared.get("max_complaints")
        if not isinstance(bound, int) or not 1 <= bound <= MAX_COMPLAINTS:
            raise SourcePackError(
                "unbounded_source",
                f"complaint sources declare max_complaints of 1-{MAX_COMPLAINTS}",
            )
    return {
        **declared,
        "provider": provider,
        "mode": "selection",
        "selection": [dict(item) for item in selection],
    }


# ------------------------------------------------------------------ HTML blocks


class _Blocks(HTMLParser):
    """Flatten an HTML page into headings, paragraphs, list items and table rows, in document order."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[dict[str, Any]] = []
        self._stack: list[str] = []
        self._buffer: list[str] = []
        self._bold: list[str] = []
        self._other = False
        self._row: list[tuple[str, str]] | None = None
        self._cell: list[str] | None = None
        self._cell_tag = ""
        self._href: list[str] = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if (
            tag in {"h1", "h2", "h3", "h4", "p", "li", "dt", "dd"}
            and self._cell is None
        ):
            self._flush()
            self._stack.append(tag)
            self._buffer, self._bold, self._other = [], [], False
        elif tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell, self._cell_tag = [], tag
        elif tag in {"strong", "b"}:
            self._stack.append(tag)
        elif tag == "a":
            href = dict(attrs).get("href")
            if href:
                self._href.append(href)
        elif tag == "br":
            self._data("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append((self._cell_tag, _text("".join(self._cell)) or ""))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.blocks.append(
                    {
                        "type": "row",
                        "cells": [c for _, c in self._row],
                        "header": all(t == "th" for t, _ in self._row),
                        "label_row": len(self._row) == 2 and self._row[0][0] == "th",
                    }
                )
            self._row = None
        elif tag in {"strong", "b"} and tag in self._stack:
            self._stack.remove(tag)
        elif (
            tag in {"h1", "h2", "h3", "h4", "p", "li", "dt", "dd"}
            and tag in self._stack
        ):
            self._flush()

    def _data(self, data):
        if self._cell is not None:
            self._cell.append(data)
            return
        if not self._stack:
            return
        self._buffer.append(data)
        if any(t in {"strong", "b"} for t in self._stack):
            self._bold.append(data)
        elif data.strip():
            self._other = True

    def handle_data(self, data):
        self._data(data)

    def _flush(self):
        block = next(
            (t for t in reversed(self._stack) if t not in {"strong", "b"}), None
        )
        text = _text("".join(self._buffer))
        if block and text:
            kind = "heading" if block.startswith("h") else block
            if block == "p" and self._bold and not self._other:
                kind = "heading"  # a heading written as a bold paragraph
            self.blocks.append({"type": kind, "text": text, "level": block})
        self._stack = [t for t in self._stack if t in {"strong", "b"}]
        self._buffer, self._bold, self._other = [], [], False

    def close(self):
        super().close()
        self._flush()


def html_blocks(raw: bytes | str) -> list[dict[str, Any]]:
    parser = _Blocks()
    parser.feed(raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw)
    parser.close()
    return parser.blocks


def _label(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().rstrip(":").casefold()


def label_rows(blocks: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Label/value rows (``<th>label</th><td>value</td>``, or ``<dt>``/``<dd>`` pairs), first occurrence wins."""
    rows: dict[str, str] = {}
    pending = None
    for block in blocks:
        if block["type"] == "row" and block.get("label_row"):
            rows.setdefault(_label(block["cells"][0]), block["cells"][1])
        elif block["type"] == "dt":
            pending = _label(block["text"])
        elif block["type"] == "dd" and pending:
            rows.setdefault(pending, block["text"])
            pending = None
    return rows


def sections(
    blocks: Sequence[Mapping[str, Any]],
) -> list[tuple[str, int, list[Mapping[str, Any]]]]:
    """(heading, block index, items) per heading; items never carry over into the next heading's section."""
    result: list[tuple[str, int, list[Mapping[str, Any]]]] = []
    for index, block in enumerate(blocks):
        if block["type"] == "heading":
            result.append((block["text"], index, []))
        elif result:
            result[-1][2].append(block)
    return result


def _table_after(
    items: Sequence[Mapping[str, Any]], key_column: str
) -> list[dict[str, str]]:
    """Rows of the first table in a section whose header row names ``key_column``, as header -> cell dicts."""
    header: list[str] | None = None
    rows = []
    for item in items:
        if item["type"] != "row":
            continue
        if item["header"] and any(_label(c) == key_column for c in item["cells"]):
            header = [_label(c) for c in item["cells"]]
            continue
        if header and not item["header"]:
            rows.append(dict(zip(header, item["cells"], strict=False)))
    return rows


# ------------------------------------------------------------------ applicability


_PRODUCT = (
    r"(?:airplanes|aeroplanes|helicopters|rotorcraft|gliders|balloons|propellers|(?:turbofan |turboprop |"
    r"turboshaft |reciprocating )?engines)"
)
_MODELS = re.compile(
    r"\bModels?\s+(?P<models>.+?)\s+(?P<product>" + _PRODUCT + r")\b", re.S
)
_SERIAL_RANGE = re.compile(
    r"serial numbers?\s+(?:\(S/Ns?\)\s+)?(?P<start>[A-Z0-9][A-Z0-9-]*)\s+(?:through|to|up to and including)\s+"
    r"(?P<end>[A-Z0-9][A-Z0-9-]*)",
    re.I,
)
_SERIAL_ONE = re.compile(
    r"serial numbers?\s+(?:\(S/Ns?\)\s+)?(?P<one>[A-Z0-9][A-Z0-9-]*\d)(?!\s+(?:through|to)\b)",
    re.I,
)
_PART_NUMBER = re.compile(
    r"(?:part numbers?\s+\(P/N\)|P/N)\s+(?P<pn>[A-Z0-9][A-Z0-9-]*[A-Z0-9])"
)


def parse_applicability(
    text: str, holder: str | None = None
) -> tuple[str, dict[str, Any] | None]:
    """(``parsed``|``unparsed``, structure) for one published applicability clause.

    Parsed only when unambiguous: one model list, and one serial statement for all of them. Enumerated sub-items,
    several models with several serial ranges, or no model list fall back to the verbatim text.
    """
    body = re.sub(r"\s+", " ", text).strip()
    if re.search(r"(?:^|\s)\((?:\d+|[ivx]+)\)\s", body):
        return "unparsed", None
    match = _MODELS.search(body)
    if not match:
        return "unparsed", None
    models = [
        m.strip(" ,")
        for m in re.split(r",\s*(?:and\s+)?|\s+and\s+", match.group("models"))
        if m.strip(" ,")
    ]
    if not models or any(len(m) > 30 or not re.search(r"\d", m) for m in models):
        return "unparsed", None
    ranges = [[m.group("start"), m.group("end")] for m in _SERIAL_RANGE.finditer(body)]
    singles = [
        [m.group("one"), m.group("one")]
        for m in _SERIAL_ONE.finditer(body)
        if not any(
            m.start() >= r.start() and m.start() < r.end()
            for r in _SERIAL_RANGE.finditer(body)
        )
    ]
    if len(models) > 1 and len(ranges) + len(singles) > 1:
        return "unparsed", None
    parsed: dict[str, Any] = {
        "models": models,
        "product": match.group("product").casefold(),
    }
    prefix = body[: match.start()].strip()
    stated_holder = (
        re.sub(r"^This AD applies to\s+", "", prefix, flags=re.I).strip(" ,") or holder
    )
    if stated_holder:
        parsed["type_certificate_holder"] = stated_holder
    if re.search(r"\ball serial numbers\b", body, re.I):
        parsed["serials"] = {"all": True}
    elif ranges or singles:
        parsed["serials"] = {"ranges": ranges + singles}
    parts = sorted({m.group("pn") for m in _PART_NUMBER.finditer(body)})
    if parts:
        parsed["part_numbers"] = parts
    return "parsed", parsed


def _model_subjects(
    parsed: Mapping[str, Any] | None,
    holder: str | None,
    locator: Mapping[str, Any],
    kind: str = "aircraft_model",
) -> list[dict[str, Any]]:
    subjects = []
    if parsed:
        product = parsed.get("product", "")
        subject_kind = "engine_model" if "engine" in product else kind
        for model in parsed["models"]:
            fields = {"model": model}
            if parsed.get("type_certificate_holder"):
                fields["type_certificate_holder"] = parsed["type_certificate_holder"]
            subjects.append(
                {
                    "kind": subject_kind,
                    "fields": fields,
                    "role": "applicability",
                    "source_string": " ".join(
                        filter(None, [parsed.get("type_certificate_holder"), model])
                    ),
                    "locator": dict(locator),
                }
            )
    if holder:
        subjects.append(
            {
                "kind": "organisation",
                "fields": {"name": holder},
                "role": "type_certificate_holder",
                "source_string": holder,
                "locator": dict(locator),
            }
        )
    return subjects


# ------------------------------------------------------------------ FAA (ES03)


_AD_HEADING = re.compile(
    r"^(?P<ad>\d{4}-\d{2}-\d{2})\s+(?P<holder>[^:\n]+?):\s*Amendment\s+(?P<amendment>39-\d+);\s*"
    r"Docket\s+No\.\s*(?P<docket>FAA-\d{4}-\d+);\s*(?:Project|Product)\s+Identifier\s+(?P<project>[A-Z0-9-]+?)"
    r"\.?\s*$",
    re.M,
)
# A heading line: "(c) Applicability" alone, or "(a) Effective Date. This AD is ..." with the body on the same line.
# Only spaces and tabs separate the parts, so a match never runs into the next line.
_PARAGRAPH = re.compile(
    r"^\((?P<label>[a-z]{1,2})\)[ \t]+(?P<heading>[A-Z][^.\n]{1,80}?)(?:\.[ \t]+(?P<rest>\S[^\n]*))?[ \t]*$",
    re.M,
)
_AD_END = re.compile(r"^Issued (?:on|in)\b", re.M)
_OTHER_HEADINGS = (
    "alternative methods of compliance",
    "related information",
    "other faa ad provisions",
    "special flight permit",
    "credit for previous actions",
    "definitions",
    "exceptions",
)


def _letters(index: int) -> str:
    return (
        "abcdefghijklmnopqrstuvwxyz"[index]
        if index < 26
        else "a" + "abcdefghijklmnopqrstuvwxyz"[index - 26]
    )


def ad_paragraphs(text: str, start: int, end: int) -> list[dict[str, Any]]:
    """Lettered AD paragraphs between ``start`` and ``end``: headings are plain lines in sequence (a), (b), ...

    Only the next expected letter opens a paragraph, so a roman sub-item such as ``(i)`` inside ``(g)`` never
    starts a new one; each body runs to the next heading, never beyond the AD's own section.
    """
    found = []
    expected = 0
    for match in _PARAGRAPH.finditer(text, start, end):
        if match.group("label") != _letters(expected):
            continue
        found.append(match)
        expected += 1
    result = []
    for index, match in enumerate(found):
        body_end = found[index + 1].start() if index + 1 < len(found) else end
        body_start = match.start("rest") if match.group("rest") else match.end() + 1
        body = text[body_start:body_end].strip()
        result.append(
            {
                "label": f"({match.group('label')})",
                "heading": match.group("heading").strip(),
                "text": body,
                "span": [
                    body_start,
                    body_start + len(text[body_start:body_end].rstrip()),
                ],
            }
        )
    return result


def parse_faa_document(
    meta: Mapping[str, Any], text: str, *, raw_text_url: str
) -> list[tuple[str, dict]]:
    """(pointer, statement) per AD a Federal Register rule document publishes."""
    number = _text(meta.get("document_number"))
    if not number or not ID_PATTERNS["document_number"].fullmatch(number):
        raise SourcePackError(
            "schema_drift", "Federal Register document lacks a valid document_number"
        )
    published = parse_date(meta.get("publication_date"))
    title = _text(meta.get("title")) or ""
    cfr = [
        f"{r.get('title')} CFR {r.get('part')}"
        for r in meta.get("cfr_references") or []
        if isinstance(r, Mapping) and r.get("title") and r.get("part")
    ]
    marker = text.find("§ 39.13")
    starts = [m for m in _AD_HEADING.finditer(text) if m.start() > marker >= 0]
    result = []
    for index, heading in enumerate(starts):
        limit = starts[index + 1].start() if index + 1 < len(starts) else len(text)
        ending = _AD_END.search(text, heading.end(), limit)
        end = ending.start() if ending else limit
        ad, holder = heading.group("ad"), heading.group("holder").strip()
        paragraphs = ad_paragraphs(text, heading.end(), end)
        applicability, statements, relations, subjects = [], [], [], []
        effective = None
        for paragraph in paragraphs:
            locator = {
                "document": raw_text_url,
                "fr_document_number": number,
                "paragraph": paragraph["label"],
                "heading": paragraph["heading"],
                "span": paragraph["span"],
            }
            name = paragraph["heading"].casefold()
            body = paragraph["text"]
            if not body:
                continue
            if name.startswith("effective date"):
                stated = re.search(
                    r"effective\s+(?:on\s+)?(?P<d>[A-Z][a-z]+\s+\d{1,2},\s*\d{4})", body
                )
                effective = parse_date(stated.group("d")) if stated else None
                statements.append(
                    {
                        "kind": "other",
                        "label": paragraph["label"],
                        "text": body,
                        "locator": locator,
                    }
                )
            elif name.startswith("affected ad"):
                for sentence in re.split(r"(?<=\.)\s+", body):
                    for cited in re.finditer(
                        r"AD\s+(?P<n>\d{4}-\d{2}-\d{2})", sentence
                    ):
                        verb = sentence.casefold()
                        relation = (
                            "supersedes"
                            if ("replaces" in verb or "supersedes" in verb)
                            else "revises"
                            if "revises" in verb
                            else "cross_reference"
                        )
                        relations.append(
                            {
                                "relation": relation,
                                "target_provider": "faa-ad",
                                "target_native_id": cited.group("n"),
                                "text": sentence.strip(),
                                "locator": locator,
                            }
                        )
                statements.append(
                    {
                        "kind": "other",
                        "label": paragraph["label"],
                        "text": body,
                        "locator": locator,
                    }
                )
            elif name.startswith("applicability"):
                state, parsed = parse_applicability(body, holder)
                applicability.append(
                    {
                        "text": body,
                        "parse_state": state,
                        "parsed": parsed,
                        "locator": locator,
                    }
                )
                subjects += _model_subjects(parsed, None, locator)
            elif name.startswith("subject"):
                statements.append(
                    {
                        "kind": "subject_code",
                        "label": paragraph["label"],
                        "text": body,
                        "locator": locator,
                    }
                )
            elif name.startswith("unsafe condition"):
                statements.append(
                    {
                        "kind": "unsafe_condition",
                        "label": paragraph["label"],
                        "text": body,
                        "locator": locator,
                    }
                )
            elif name == "compliance":
                statements.append(
                    {
                        "kind": "compliance_time",
                        "label": paragraph["label"],
                        "text": body,
                        "locator": locator,
                    }
                )
            elif name.startswith("material incorporated by reference"):
                statements.append(
                    {
                        "kind": "incorporated_material",
                        "label": paragraph["label"],
                        "text": body,
                        "locator": locator,
                    }
                )
            elif name.startswith(_OTHER_HEADINGS):
                statements.append(
                    {
                        "kind": "other",
                        "label": paragraph["label"],
                        "text": body,
                        "locator": locator,
                    }
                )
            else:
                statements.append(
                    {
                        "kind": "required_action",
                        "label": paragraph["label"],
                        "text": body,
                        "locator": locator,
                    }
                )
        subjects.append(
            {
                "kind": "organisation",
                "fields": {"name": holder},
                "role": "type_certificate_holder",
                "source_string": holder,
                "locator": {
                    "document": raw_text_url,
                    "span": [heading.start("holder"), heading.end("holder")],
                },
            }
        )
        statement = _statement(
            "faa-ad",
            "directive",
            ad,
            identifiers={
                "fr_document_number": number,
                "amendment": heading.group("amendment"),
                "docket": heading.group("docket"),
                "project_identifier": heading.group("project"),
                "cfr": cfr,
            },
            revision_label="correction"
            if re.search(r";\s*Correction\b", title)
            else None,
            revision_date=published,
            revision_date_basis="publication" if published else None,
            effective_date=effective or parse_date(meta.get("effective_on")),
            title=title,
            language="en",
            url=_text(meta.get("html_url")) or raw_text_url,
            applicability=applicability,
            statements=statements,
            relations=relations,
            subjects=subjects,
        )
        result.append((f"/ad/{index}", statement))
    return result


# ------------------------------------------------------------------ EASA (ES04)


def parse_easa_page(raw: bytes | str, *, url: str, requested: str) -> dict[str, Any]:
    blocks = html_blocks(raw)
    rows = label_rows(blocks)
    number = _text(rows.get("ad number") or rows.get("ad no."))
    if not number or not ID_PATTERNS["ad_number"].fullmatch(number):
        raise SourcePackError("schema_drift", "EASA AD page lacks a valid AD number")
    if number != requested:
        raise SourcePackError("schema_drift", "EASA answered another AD number")
    base, _, suffix = number.partition("R")
    issued = parse_date(rows.get("issue date"))

    def locator(label: str) -> dict[str, Any]:
        return {"url": url, "row": label}

    statements, relations, applicability, subjects = [], [], [], []
    for label, kind in (
        ("subject", "subject_code"),
        ("reason", "unsafe_condition"),
        ("required action(s) and compliance time(s)", "required_action"),
        ("required actions", "required_action"),
        ("compliance time", "compliance_time"),
    ):
        if rows.get(label):
            statements.append(
                {"kind": kind, "text": rows[label], "locator": locator(label)}
            )
    for label in ("approval holder / type designation", "applicability"):
        if rows.get(label):
            holder = rows[label].split(":", 1)[0] if ":" in rows[label] else None
            state, parsed = parse_applicability(rows[label].split(":", 1)[-1], holder)
            applicability.append(
                {
                    "text": rows[label],
                    "parse_state": state,
                    "parsed": parsed,
                    "locator": locator(label),
                }
            )
            subjects += _model_subjects(parsed, holder, locator(label))
            break
    supersedure = rows.get("supersedure") or ""
    for cited in re.finditer(
        r"EASA\s+AD\s+(?P<n>\d{4}-\d{4})(?P<r>R\d+)?", supersedure
    ):
        relations.append(
            {
                "relation": "supersedes",
                "target_provider": "easa-ad",
                "target_native_id": cited.group("n"),
                "target_revision_label": cited.group("r"),
                "text": supersedure,
                "locator": locator("supersedure"),
            }
        )
    for label in ("related ad(s)", "cross reference"):
        stated = rows.get(label) or ""
        for cited in re.finditer(
            r"(?P<who>FAA|EASA)\s+AD\s+(?P<n>\d{4}-\d{2,4}(?:-\d{2})?)(?P<r>R\d+)?",
            stated,
        ):
            provider = "faa-ad" if cited.group("who") == "FAA" else "easa-ad"
            relations.append(
                {
                    "relation": "cross_reference",
                    "target_provider": provider,
                    "target_native_id": cited.group("n"),
                    "target_revision_label": cited.group("r"),
                    "text": stated,
                    "locator": locator(label),
                }
            )
    return _statement(
        "easa-ad",
        "directive",
        base,
        identifiers={"ad_number": number},
        revision_label=f"R{suffix}" if suffix else None,
        revision_date=issued,
        revision_date_basis="issue" if issued else None,
        effective_date=parse_date(rows.get("effective date")),
        title=rows.get("subject"),
        language="en",
        url=url,
        applicability=applicability,
        statements=statements,
        relations=relations,
        subjects=subjects,
    )


# ------------------------------------------------------------------ NTSB (ES05)


def _float(value: Any) -> float | None:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if -180.0 <= number <= 180.0 else None


def parse_ntsb_case(case: Mapping[str, Any], *, url: str) -> list[tuple[str, dict]]:
    number = _text(case.get("NtsbNumber"))
    if not number or not ID_PATTERNS["ntsb_number"].fullmatch(number):
        raise SourcePackError("schema_drift", "NTSB case lacks a valid NtsbNumber")
    status = (_text(case.get("ReportType")) or "").casefold()
    report_status = (
        "final"
        if status.startswith("final")
        else "preliminary"
        if status.startswith("prelim")
        else None
    )
    report_date = parse_date(case.get("ReportDate"), day_first=False)
    statements = []
    if _text(case.get("ProbableCause")):
        statements.append(
            {
                "kind": "probable_cause",
                "text": _text(case["ProbableCause"]),
                "locator": {
                    "url": url,
                    "json_pointer": "/ProbableCause",
                    "report_status": report_status or "unknown",
                },
            }
        )
    for index, finding in enumerate(case.get("Findings") or []):
        text = _text(
            finding.get("FindingText") if isinstance(finding, Mapping) else finding
        )
        if text:
            statements.append(
                {
                    "kind": "finding",
                    "label": _text(finding.get("FindingNumber"))
                    if isinstance(finding, Mapping)
                    else None,
                    "text": text,
                    "locator": {
                        "url": url,
                        "json_pointer": f"/Findings/{index}/FindingText",
                        "report_status": report_status or "unknown",
                    },
                }
            )
    subjects = []
    for index, vehicle in enumerate(case.get("Vehicles") or []):
        if not isinstance(vehicle, Mapping):
            continue
        fields = {
            k: _text(vehicle.get(v))
            for k, v in (
                ("make", "Make"),
                ("model", "Model"),
                ("serial_number", "SerialNumber"),
                ("registration", "RegistrationNumber"),
            )
        }
        fields = {k: v for k, v in fields.items() if v}
        if fields.get("model"):
            subjects.append(
                {
                    "kind": "aircraft",
                    "fields": fields,
                    "role": "vehicle",
                    "source_string": " ".join(
                        filter(None, [fields.get("make"), fields.get("model")])
                    ),
                    "locator": {"url": url, "json_pointer": f"/Vehicles/{index}"},
                }
            )
        operator = _text(vehicle.get("Operator"))
        if operator:
            subjects.append(
                {
                    "kind": "organisation",
                    "fields": {"name": operator},
                    "role": "operator",
                    "source_string": operator,
                    "locator": {
                        "url": url,
                        "json_pointer": f"/Vehicles/{index}/Operator",
                    },
                }
            )
    place = ", ".join(
        filter(None, (_text(case.get(k)) for k in ("City", "State", "Country")))
    )
    occurrence = {
        "occurred_on": parse_date(case.get("EventDate"), day_first=False),
        "occurred_declared": _text(case.get("EventDate")),
        "place": place or None,
        "country": _text(case.get("Country")),
        "latitude": _float(case.get("Latitude")),
        "longitude": _float(case.get("Longitude")),
        "severity": _text(case.get("HighestInjuryLevel")),
        "locator": {"url": url, "json_pointer": "/"},
    }
    relations = [
        {
            "relation": "issued_recommendation",
            "target_provider": "ntsb",
            "target_native_id": r,
            "locator": {"url": url, "json_pointer": f"/RecommendationNumbers/{i}"},
        }
        for i, r in enumerate(case.get("RecommendationNumbers") or [])
        if _text(r)
    ]
    statement = _statement(
        "ntsb",
        "investigation",
        number,
        identifiers={"ntsb_number": number, "docket_url": _text(case.get("DocketUrl"))},
        revision_date=report_date,
        revision_date_basis="report" if report_date else None,
        report_status=report_status,
        title=_text(case.get("Title")) or f"NTSB {number}",
        language="en",
        url=_text(case.get("ReportUrl")) or url,
        statements=statements,
        subjects=subjects,
        occurrence={k: v for k, v in occurrence.items() if v is not None},
        relations=relations,
    )
    return [("", statement)]


def parse_ntsb_recommendation(
    item: Mapping[str, Any], *, url: str
) -> list[tuple[str, dict]]:
    number = _text(item.get("RecommendationNumber"))
    if not number or not ID_PATTERNS["recommendation_number"].fullmatch(number):
        raise SourcePackError(
            "schema_drift", "NTSB recommendation lacks a valid RecommendationNumber"
        )
    addressee = _text(item.get("Addressee"))
    responses = []
    for index, change in enumerate(item.get("StatusHistory") or []):
        if isinstance(change, Mapping) and _text(change.get("Status")):
            responses.append(
                {
                    "status": _text(change["Status"]),
                    "status_date": parse_date(
                        change.get("StatusDate"), day_first=False
                    ),
                    "status_date_declared": _text(change.get("StatusDate")),
                    "addressee": addressee,
                    "locator": {"url": url, "json_pointer": f"/StatusHistory/{index}"},
                }
            )
    dates = [r["status_date"] for r in responses if r.get("status_date")]
    issued = parse_date(item.get("IssueDate"), day_first=False)
    revision_date = (
        max(dates + ([issued] if issued else [])) if dates or issued else None
    )
    statements = (
        [
            {
                "kind": "recommendation_text",
                "text": _text(item["RecommendationText"]),
                "locator": {"url": url, "json_pointer": "/RecommendationText"},
            }
        ]
        if _text(item.get("RecommendationText"))
        else []
    )
    relations = [
        {
            "relation": "recommendation_of",
            "target_provider": "ntsb",
            "target_native_id": n,
            "locator": {"url": url, "json_pointer": f"/NtsbNumbers/{i}"},
        }
        for i, n in enumerate(item.get("NtsbNumbers") or [])
        if _text(n)
    ]
    subjects = (
        [
            {
                "kind": "organisation",
                "fields": {"name": addressee},
                "role": "addressee",
                "source_string": addressee,
                "locator": {"url": url, "json_pointer": "/Addressee"},
            }
        ]
        if addressee
        else []
    )
    return [
        (
            "",
            _statement(
                "ntsb",
                "safety_recommendation",
                number,
                identifiers={"issue_date": issued},
                revision_date=revision_date,
                revision_date_basis="status" if revision_date else None,
                title=_text(item.get("Subject")),
                language="en",
                url=url,
                statements=statements,
                responses=responses,
                relations=relations,
                subjects=subjects,
            ),
        )
    ]


# ------------------------------------------------------------------ PHMSA (ES06)


def parse_phmsa_file(
    raw: bytes, declared: Mapping[str, Any], document: Mapping[str, Any]
) -> tuple[list[tuple[str, dict]], int]:
    columns = dict(declared["columns"])
    window = dict(declared["window"])
    text = raw.decode(str(declared.get("encoding") or "utf-8"), "replace")
    reader = csv.DictReader(
        io.StringIO(text), delimiter=str(declared.get("delimiter") or "\t")
    )
    if not reader.fieldnames or columns["report_id"] not in reader.fieldnames:
        raise SourcePackError(
            "schema_drift", "PHMSA file lacks the declared report id column"
        )
    vintage = parse_date(document["vintage"])
    url = str(document["url"])
    result, out_of_scope = [], 0
    for index, row in enumerate(reader):

        def col(name: str) -> str | None:
            return _text(row.get(columns[name])) if columns.get(name) else None

        report = col("report_id")
        year = col("year")
        if (
            not report
            or not year
            or not year.isdigit()
            or not window["year_from"] <= int(year) <= window["year_to"]
        ):
            out_of_scope += 1
            continue
        locator = {"document": url, "vintage": vintage, "row": index + 2}
        operator_id, operator = col("operator_id"), col("operator_name")
        subjects = (
            [
                {
                    "kind": "pipeline_operator",
                    "fields": {
                        k: v
                        for k, v in (("operator_id", operator_id), ("name", operator))
                        if v
                    },
                    "role": "operator",
                    "source_string": operator or operator_id,
                    "locator": locator,
                }
            ]
            if operator or operator_id
            else []
        )
        statements = [
            {
                "kind": "cause_category",
                "label": name,
                "text": col(name),
                "locator": {**locator, "column": columns[name]},
            }
            for name in ("cause", "cause_details")
            if col(name)
        ]
        if col("narrative"):
            statements.append(
                {
                    "kind": "summary",
                    "text": col("narrative"),
                    "locator": {**locator, "column": columns["narrative"]},
                }
            )
        quantities = []
        for quantity in declared.get("quantities") or []:
            value = _text(row.get(quantity["column"]))
            if value is not None:
                quantities.append(
                    {
                        "name": quantity["name"],
                        "value": value,
                        "unit": quantity.get("unit"),
                        "locator": {**locator, "column": quantity["column"]},
                    }
                )
        consequences = {
            name: col(name)
            for name in (
                "fatalities",
                "injuries",
                "total_cost",
                "commodity",
                "significant",
                "serious",
            )
            if col(name)
        }
        place = ", ".join(filter(None, (col("city"), col("state"))))
        occurrence = {
            "occurred_on": parse_date(col("local_datetime"), day_first=False),
            "occurred_declared": col("local_datetime"),
            "place": place or None,
            "country": "US",
            "latitude": _float(col("latitude")),
            "longitude": _float(col("longitude")),
            "consequences": consequences,
            "locator": locator,
        }
        result.append(
            (
                f"/rows/{index}",
                _statement(
                    "phmsa",
                    "occurrence",
                    report,
                    identifiers={
                        "system": _text(document.get("system")),
                        "file": _text(document.get("label")),
                    },
                    revision_date=vintage,
                    revision_date_basis="file-vintage",
                    title=f"PHMSA incident report {report}",
                    language="en",
                    url=url,
                    subjects=subjects,
                    statements=statements,
                    quantities=quantities,
                    occurrence={
                        k: v for k, v in occurrence.items() if v not in (None, {})
                    },
                ),
            )
        )
    return result, out_of_scope


# ------------------------------------------------------------------ CSB (ES07)


_SECTION_KINDS = {
    "key findings": "finding",
    "findings": "finding",
    "causal factors": "root_cause",
    "root causes": "root_cause",
    "probable cause": "probable_cause",
    "ursachen": "probable_cause",
    "schlussfolgerungen": "finding",
    "befunde": "finding",
}


def _section_statements(blocks, url: str, language: str) -> list[dict[str, Any]]:
    statements = []
    for heading, _index, items in sections(blocks):
        kind = _SECTION_KINDS.get(_label(heading))
        if not kind:
            continue
        ordinal = 0
        for item in items:
            if item["type"] not in {"li", "p"}:
                continue
            ordinal += 1
            statements.append(
                {
                    "kind": kind,
                    "label": str(ordinal),
                    "text": item["text"],
                    "language": language,
                    "locator": {"url": url, "section": heading, "item": ordinal},
                }
            )
    return statements


def parse_csb_page(raw: bytes | str, *, url: str, slug: str) -> list[tuple[str, dict]]:
    blocks = html_blocks(raw)
    title = next(
        (b["text"] for b in blocks if b["type"] == "heading" and b["level"] == "h1"),
        None,
    )
    rows = label_rows(blocks)
    if not title:
        raise SourcePackError("schema_drift", "CSB investigation page lacks a title")
    released = parse_date(rows.get("final report released on"), day_first=False)
    status = (rows.get("investigation status") or "").casefold()
    report_status = (
        "final"
        if released or status.startswith("closed") or "final" in status
        else "preliminary"
    )
    locator = {"url": url, "section": "details"}
    subjects = []
    for label, kind, role in (
        ("company", "organisation", "company"),
        ("facility", "facility", "facility"),
    ):
        if rows.get(label):
            fields = {"name": rows[label]}
            if kind == "facility" and rows.get("location"):
                fields["address"] = rows["location"]
            subjects.append(
                {
                    "kind": kind,
                    "fields": fields,
                    "role": role,
                    "source_string": rows[label],
                    "locator": {**locator, "row": label},
                }
            )
    recommendations = []
    for heading, _index, items in sections(blocks):
        if not _label(heading).startswith("recommendation"):
            continue
        recommendations += _table_after(items, "number")
    relations, records = [], []
    for index, row in enumerate(recommendations):
        number = _text(row.get("number"))
        if not number:
            continue
        relations.append(
            {
                "relation": "issued_recommendation",
                "target_provider": "csb",
                "target_native_id": number,
                "locator": {"url": url, "section": "recommendations", "row": index + 1},
            }
        )
        status_date = parse_date(row.get("status date"), day_first=False)
        recipient = _text(row.get("recipient"))
        records.append(
            (
                f"/recommendations/{index}",
                _statement(
                    "csb",
                    "safety_recommendation",
                    number,
                    revision_date=status_date,
                    revision_date_basis="status" if status_date else None,
                    title=f"CSB recommendation {number}",
                    language="en",
                    url=url,
                    statements=[
                        {
                            "kind": "recommendation_text",
                            "text": _text(row.get("text")),
                            "locator": {
                                "url": url,
                                "section": "recommendations",
                                "row": index + 1,
                            },
                        }
                    ]
                    if _text(row.get("text"))
                    else [],
                    responses=[
                        {
                            "status": _text(row.get("status")),
                            "status_date": status_date,
                            "status_date_declared": _text(row.get("status date")),
                            "addressee": recipient,
                            "locator": {
                                "url": url,
                                "section": "recommendations",
                                "row": index + 1,
                            },
                        }
                    ]
                    if _text(row.get("status"))
                    else [],
                    relations=[
                        {
                            "relation": "recommendation_of",
                            "target_provider": "csb",
                            "target_native_id": slug,
                            "locator": {
                                "url": url,
                                "section": "recommendations",
                                "row": index + 1,
                            },
                        }
                    ],
                    subjects=[
                        {
                            "kind": "organisation",
                            "fields": {"name": recipient},
                            "role": "addressee",
                            "source_string": recipient,
                            "locator": {
                                "url": url,
                                "section": "recommendations",
                                "row": index + 1,
                            },
                        }
                    ]
                    if recipient
                    else [],
                ),
            )
        )
    occurrence = {
        "occurred_on": parse_date(rows.get("accident occurred on"), day_first=False),
        "occurred_declared": rows.get("accident occurred on"),
        "place": rows.get("location"),
        "severity": rows.get("accident type"),
        "locator": locator,
    }
    investigation = _statement(
        "csb",
        "investigation",
        slug,
        identifiers={"page": slug},
        revision_date=released,
        revision_date_basis="report" if released else None,
        report_status=report_status,
        title=title,
        language="en",
        url=url,
        statements=_section_statements(blocks, url, "en"),
        subjects=subjects,
        relations=relations,
        occurrence={k: v for k, v in occurrence.items() if v},
    )
    return [("", investigation), *records]


# ------------------------------------------------------------------ NHTSA ODI (ES08)


_ACTION = r"(?:PE|EA|DP|RQ|AQ|RP)\d{2}-?\d{3}"


def _action(value: str) -> str:
    return value.replace("-", "").upper()


def parse_odi_file(
    raw: bytes, declared: Mapping[str, Any], document: Mapping[str, Any]
) -> tuple[list[tuple[str, dict]], int]:
    columns = list(declared["columns"])
    wanted = [
        (str(v["make"]).casefold(), str(v.get("model") or "").casefold())
        for v in declared["vehicles"]
    ]
    opened_from = parse_date(dict(declared.get("window") or {}).get("opened_from"))
    text = raw.decode(str(declared.get("encoding") or "utf-8"), "replace")
    url = str(document["url"])
    groups: dict[str, list[tuple[int, dict[str, str]]]] = {}
    out_of_scope = 0
    for index, line in enumerate(text.splitlines()):
        if not line.strip():
            continue
        cells = line.split("\t")
        if len(cells) != len(columns):
            raise SourcePackError(
                "schema_drift",
                f"ODI line {index + 1} has {len(cells)} fields, not {len(columns)}",
            )
        row = {name: (cells[i].strip()) for i, name in enumerate(columns)}
        make, model = row.get("make", "").casefold(), row.get("model", "").casefold()
        opened = parse_date(row.get("opened"))
        if not any(make == m and (not mo or model == mo) for m, mo in wanted) or (
            opened_from and (not opened or opened < opened_from)
        ):
            out_of_scope += 1
            continue
        if not re.fullmatch(_ACTION, row.get("action_number", "")):
            raise SourcePackError(
                "schema_drift", f"ODI line {index + 1} lacks a valid action number"
            )
        groups.setdefault(_action(row["action_number"]), []).append((index, row))
    result = []
    for action, rows in sorted(groups.items()):
        first_index, first = rows[0]
        locator = {"document": url, "line": first_index + 1}
        subjects, seen = [], set()
        for index, row in rows:
            fields = {
                "make": row.get("make"),
                "model": row.get("model"),
                "model_year": row.get("model_year"),
            }
            fields = {k: v for k, v in fields.items() if v}
            key = tuple(sorted(fields.items()))
            if key in seen:
                continue
            seen.add(key)
            subjects.append(
                {
                    "kind": "vehicle",
                    "fields": fields,
                    "role": "subject vehicle",
                    "source_string": " ".join(fields.values()),
                    "locator": {"document": url, "line": index + 1},
                }
            )
        if first.get("manufacturer"):
            subjects.append(
                {
                    "kind": "organisation",
                    "fields": {"name": first["manufacturer"]},
                    "role": "manufacturer",
                    "source_string": first["manufacturer"],
                    "locator": locator,
                }
            )
        if first.get("component"):
            subjects.append(
                {
                    "kind": "component",
                    "fields": {"component": first["component"]},
                    "role": "component (as published)",
                    "source_string": first["component"],
                    "locator": locator,
                }
            )
        relations = []
        for campaign in re.findall(r"\d{2}[VETCIX]\d{6}", first.get("campaign") or ""):
            relations.append(
                {
                    "relation": "cites_recall",
                    "target_provider": "nhtsa",
                    "target_native_id": campaign,
                    "locator": {**locator, "column": "campaign"},
                }
            )
        summary = first.get("summary") or ""
        for match in re.finditer(
            r"upgraded\s+(?:to|into)\s+(?:an?\s+)?(?:[A-Za-z ]+\()?(?P<id>"
            + _ACTION
            + ")",
            summary,
            re.I,
        ):
            relations.append(
                {
                    "relation": "upgraded_to",
                    "target_provider": "nhtsa-odi",
                    "target_native_id": _action(match.group("id")),
                    "text": match.group(0),
                    "locator": {**locator, "column": "summary"},
                }
            )
        for match in re.finditer(
            r"(?:upgraded|opened)\s+from\s+(?:[A-Za-z ]+\()?(?P<id>" + _ACTION + ")",
            summary,
            re.I,
        ):
            relations.append(
                {
                    "relation": "upgraded_from",
                    "target_provider": "nhtsa-odi",
                    "target_native_id": _action(match.group("id")),
                    "text": match.group(0),
                    "locator": {**locator, "column": "summary"},
                }
            )
        opened, closed = (
            parse_date(first.get("opened")),
            parse_date(first.get("closed")),
        )
        statements = (
            [
                {
                    "kind": "summary",
                    "text": summary,
                    "locator": {**locator, "column": "summary"},
                }
            ]
            if summary
            else []
        )
        result.append(
            (
                f"/actions/{action}",
                _statement(
                    "nhtsa-odi",
                    "defect_investigation",
                    action,
                    identifiers={
                        "action_type": action[:2],
                        "opened": opened,
                        "closed": closed,
                    },
                    revision_date=closed or opened,
                    revision_date_basis="status" if (closed or opened) else None,
                    title=_text(first.get("subject")) or f"ODI {action}",
                    language="en",
                    url=url,
                    statements=statements,
                    subjects=subjects,
                    relations=relations,
                ),
            )
        )
    return result, out_of_scope


def parse_complaints(
    payload: Mapping[str, Any], selector: Mapping[str, Any], *, url: str, bound: int
) -> list[tuple[str, dict]]:
    results = payload.get("results")
    if not isinstance(results, list):
        raise SourcePackError(
            "schema_drift", "NHTSA complaints response lacks a results list"
        )
    count = payload.get("count", len(results))
    if not isinstance(count, int) or count > bound or len(results) > bound:
        # Never a truncated set: raise the declared bound or narrow the vehicle.
        raise SourcePackError(
            "response_too_large", "complaint count exceeds the declared bound"
        )
    result = []
    for index, item in enumerate(results):
        if not isinstance(item, Mapping) or not _text(item.get("odiNumber")):
            raise SourcePackError("schema_drift", "NHTSA complaint lacks an odiNumber")
        number = _text(item["odiNumber"])
        locator = {"url": url, "json_pointer": f"/results/{index}"}
        fields = {
            "make": selector["make"],
            "model": selector["model"],
            "model_year": str(selector["model_year"]),
        }
        consequences = {
            k: str(item[k]).lower() if isinstance(item[k], bool) else str(item[k])
            for k in ("crash", "fire", "numberOfInjuries", "numberOfDeaths")
            if item.get(k) is not None
        }
        filed = parse_date(item.get("dateComplaintFiled"), day_first=False)
        result.append(
            (
                f"/results/{index}",
                _statement(
                    "nhtsa-complaints",
                    "complaint",
                    number,
                    identifiers={"components": _text(item.get("components"))},
                    revision_date=filed,
                    revision_date_basis="publication" if filed else None,
                    title=f"NHTSA complaint {number} (unverified consumer report)",
                    language="en",
                    url=url,
                    statements=[
                        {
                            "kind": "summary",
                            "text": _text(item["summary"]),
                            "locator": {**locator, "field": "summary"},
                        }
                    ]
                    if _text(item.get("summary"))
                    else [],
                    subjects=[
                        {
                            "kind": "vehicle",
                            "fields": fields,
                            "role": "complaint vehicle",
                            "source_string": " ".join(fields.values()),
                            "locator": locator,
                        }
                    ],
                    occurrence={
                        k: v
                        for k, v in {
                            "occurred_on": parse_date(
                                item.get("dateOfIncident"), day_first=False
                            ),
                            "occurred_declared": _text(item.get("dateOfIncident")),
                            "consequences": consequences,
                            "locator": locator,
                        }.items()
                        if v
                    },
                ),
            )
        )
    return result


# ------------------------------------------------------------------ BFU and BEA (ES09)


def parse_bfu_page(
    raw: bytes | str, *, url: str, requested: str
) -> list[tuple[str, dict]]:
    blocks = html_blocks(raw)
    rows = label_rows(blocks)
    number = _text(rows.get("aktenzeichen"))
    if not number or number.replace(" ", "") != requested.replace(" ", ""):
        raise SourcePackError(
            "schema_drift", "BFU page lacks the requested Aktenzeichen"
        )
    kind = (rows.get("berichtsart") or "").casefold()
    report_status = (
        "final"
        if "untersuchungsbericht" in kind
        else "preliminary"
        if "zwischen" in kind
        else None
    )
    published = parse_date(rows.get("veröffentlicht") or rows.get("datum des berichts"))
    locator = {"url": url, "section": "Angaben"}
    subjects = []
    aircraft = rows.get("luftfahrzeug") or rows.get("hersteller / muster")
    if aircraft:
        maker, _, model = aircraft.partition("/")
        fields = (
            {"make": maker.strip(), "model": model.strip()}
            if model
            else {"model": aircraft}
        )
        subjects.append(
            {
                "kind": "aircraft",
                "fields": fields,
                "role": "vehicle",
                "source_string": aircraft,
                "locator": {**locator, "row": "luftfahrzeug"},
            }
        )
    records = []
    relations = []
    for heading, _index, items in sections(blocks):
        if not _label(heading).startswith("sicherheitsempfehlung"):
            continue
        for index, row in enumerate(_table_after(items, "nummer")):
            rec = _text(row.get("nummer"))
            if not rec:
                continue
            rec_id = f"{number}/{rec}"
            row_locator = {"url": url, "section": heading, "row": index + 1}
            relations.append(
                {
                    "relation": "issued_recommendation",
                    "target_provider": "bfu",
                    "target_native_id": rec_id,
                    "locator": row_locator,
                }
            )
            addressee = _text(row.get("adressat"))
            records.append(
                (
                    f"/recommendations/{index}",
                    _statement(
                        "bfu",
                        "safety_recommendation",
                        rec_id,
                        identifiers={"number": rec},
                        revision_date=published,
                        revision_date_basis="report" if published else None,
                        title=f"BFU Sicherheitsempfehlung {rec}",
                        language="de",
                        url=url,
                        statements=[
                            {
                                "kind": "recommendation_text",
                                "text": _text(row.get("text")),
                                "language": "de",
                                "locator": row_locator,
                            }
                        ]
                        if _text(row.get("text"))
                        else [],
                        responses=[
                            {
                                "status": _text(row.get("status")),
                                "addressee": addressee,
                                "locator": row_locator,
                            }
                        ]
                        if _text(row.get("status"))
                        else [],
                        relations=[
                            {
                                "relation": "recommendation_of",
                                "target_provider": "bfu",
                                "target_native_id": number,
                                "locator": row_locator,
                            }
                        ],
                        subjects=[
                            {
                                "kind": "organisation",
                                "fields": {"name": addressee},
                                "role": "addressee",
                                "source_string": addressee,
                                "locator": row_locator,
                            }
                        ]
                        if addressee
                        else [],
                    ),
                )
            )
    occurrence = {
        "occurred_on": parse_date(rows.get("ereignisdatum") or rows.get("datum")),
        "occurred_declared": rows.get("ereignisdatum") or rows.get("datum"),
        "place": rows.get("ort"),
        "severity": rows.get("art des ereignisses"),
        "locator": locator,
    }
    investigation = _statement(
        "bfu",
        "investigation",
        number,
        identifiers={"aktenzeichen": number},
        revision_date=published,
        revision_date_basis="report" if published else None,
        report_status=report_status,
        title=next(
            (
                b["text"]
                for b in blocks
                if b["type"] == "heading" and b["level"] == "h1"
            ),
            None,
        ),
        language="de",
        url=url,
        statements=_section_statements(blocks, url, "de"),
        subjects=subjects,
        relations=relations,
        occurrence={k: v for k, v in occurrence.items() if v},
    )
    return [("", investigation), *records]


def link_only_entries(declared: Mapping[str, Any]) -> list[tuple[str, dict]]:
    """BEA (ES09): declared report entries as link-only investigation records; nothing is fetched or quoted."""
    result = []
    for index, entry in enumerate(declared["entries"]):
        published = parse_date(entry.get("published"))
        fields = {
            k: v
            for k, v in (
                ("make", _text(entry.get("aircraft_make"))),
                ("model", _text(entry.get("aircraft"))),
            )
            if v
        }
        subjects = (
            [
                {
                    "kind": "aircraft",
                    "fields": fields,
                    "role": "vehicle (as the report title names it)",
                    "source_string": " ".join(fields.values()),
                    "locator": {"declared_entry": index},
                }
            ]
            if fields.get("model")
            else []
        )
        result.append(
            (
                f"/entries/{index}",
                _statement(
                    declared["provider"],
                    "investigation",
                    str(entry["report_id"]),
                    access="link-only",
                    revision_date=published,
                    revision_date_basis="declared-entry" if published else None,
                    title=_text(entry.get("title")),
                    language=_text(entry.get("language")),
                    url=str(entry["url"]),
                    occurrence={
                        "occurred_on": parse_date(entry.get("event_date")),
                        "locator": {"declared_entry": index},
                    }
                    if parse_date(entry.get("event_date"))
                    else None,
                    subjects=subjects,
                ),
            )
        )
    return result


# ------------------------------------------------------------------ adapter


class EngineeringSafetyAdapter:
    """One selector, declared file or link-only entry list per page; statements carry locators, never verdicts."""

    accepts_transport = True

    def __init__(
        self,
        source: Mapping[str, Any],
        *,
        transport: Callable[..., Mapping[str, Any]] | None = None,
        secret: str | None = None,
    ) -> None:
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        self.source = json.loads(json.dumps(source))
        self.declared = declaration(self.source)
        self.provider = self.declared["provider"]
        if transport is None:
            from functools import partial

            transport = partial(
                HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"])
            )
        self.transport = transport
        del secret  # every audited source is keyless; nothing is sent
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
            "engineering_safety": {
                "provider": self.provider,
                "mode": self.declared["mode"],
                "record_contract": CONTRACT,
            },
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _units(self) -> list[dict[str, Any]]:
        mode = self.declared["mode"]
        if mode == "selection":
            return self.declared["selection"]
        if mode == "documents":
            return [dict(d) for d in self.declared["documents"]]
        return [{"entries": len(self.declared["entries"])}]

    def _get(
        self,
        url: str,
        params: Mapping[str, Any] | None = None,
        accept: str = "application/json",
    ) -> tuple[int, bytes]:
        from src.ingestion.source_pack_runtime import _retry_after_ms

        response = self.transport(
            url=url,
            params=dict(params or {}),
            headers={"Accept": accept},
            timeout=int(self.definition["limits"]["timeout_ms"]) / 1000,
        )
        if (
            urlsplit(str(response.get("final_url") or url)).hostname or ""
        ).casefold() != (urlsplit(url).hostname or "").casefold():
            raise SourcePackError(
                "network_policy", "source was served from another host"
            )
        status = int(response.get("status", 200))
        headers = {
            str(k).casefold(): v for k, v in dict(response.get("headers") or {}).items()
        }
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError(
                "response_too_large", "source response exceeds its byte limit"
            )
        if status == 429:
            raise SourcePackError(
                "rate_limited",
                "provider quota is temporarily exhausted",
                retry_after_ms=_retry_after_ms(headers.get("retry-after")),
            )
        if status in {401, 403}:
            raise SourcePackError(
                "authentication_failed", f"source refused the request (HTTP {status})"
            )
        if status >= 500:
            raise SourcePackError(
                "source_unavailable", f"source returned HTTP {status}"
            )
        if status >= 400 and status != 404:
            raise SourcePackError("schema_drift", f"source returned HTTP {status}")
        return status, raw

    @staticmethod
    def _json(raw: bytes) -> Any:
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SourcePackError(
                "schema_drift", "source returned a non-JSON body"
            ) from exc

    def _fetch(
        self, unit: Mapping[str, Any]
    ) -> tuple[list[tuple[str, dict]], int, str, bytes]:
        """(pairs, out-of-scope count, outcome, raw bytes) for one unit."""
        base = self.source["endpoint"].rstrip("/")
        provider = self.provider
        if self.declared["mode"] == "link-only":
            return link_only_entries(self.declared), 0, "declared", b""
        if self.declared["mode"] == "documents":
            status, raw = self._get(str(unit["url"]), accept="text/plain")
            if status == 404:
                return [], 0, "not_found", raw
            pairs, skipped = (
                parse_phmsa_file if provider == "phmsa" else parse_odi_file
            )(raw, self.declared, unit)
            return pairs, skipped, "returned" if pairs else "no_rows_in_scope", raw
        if provider == "faa-ad":
            status, raw = self._get(f"{base}/documents/{unit['document_number']}.json")
            if status == 404:
                return [], 0, "not_found", raw
            meta = self._json(raw)
            agencies = {
                str(a.get("slug") or "")
                for a in meta.get("agencies") or []
                if isinstance(a, Mapping)
            }
            if (
                "federal-aviation-administration" not in agencies
                or "airworthiness directives"
                not in str(meta.get("title") or "").casefold()
            ):
                return [], 1, "out_of_scope", raw
            text_url = str(meta.get("raw_text_url") or "")
            if urlsplit(text_url).hostname != urlsplit(base).hostname:
                raise SourcePackError(
                    "network_policy",
                    "raw text is not served from the Federal Register host",
                )
            status, body = self._get(text_url, accept="text/plain")
            if status == 404:
                raise SourcePackError(
                    "schema_drift", "Federal Register raw text is missing"
                )
            pairs = parse_faa_document(
                meta, body.decode("utf-8", "replace"), raw_text_url=text_url
            )
            return pairs, 0, "returned" if pairs else "no_ad_in_document", raw + body
        if provider == "easa-ad":
            url = f"{base}/ad/{unit['ad_number']}"
            status, raw = self._get(url, accept="text/html")
            if status == 404:
                return [], 0, "not_found", raw
            return (
                [("", parse_easa_page(raw, url=url, requested=unit["ad_number"]))],
                0,
                "returned",
                raw,
            )
        if provider == "ntsb":
            if "ntsb_number" in unit:
                url = f"{base}/cases/{unit['ntsb_number']}"
                status, raw = self._get(url)
                if status == 404:
                    return [], 0, "not_found", raw
                pairs = parse_ntsb_case(self._json(raw), url=url)
                if pairs[0][1]["native_id"] != unit["ntsb_number"]:
                    raise SourcePackError("schema_drift", "NTSB answered another case")
                return pairs, 0, "returned", raw
            url = f"{base}/recommendations/{unit['recommendation_number']}"
            status, raw = self._get(url)
            if status == 404:
                return [], 0, "not_found", raw
            pairs = parse_ntsb_recommendation(self._json(raw), url=url)
            if pairs[0][1]["native_id"] != unit["recommendation_number"]:
                raise SourcePackError(
                    "schema_drift", "NTSB answered another recommendation"
                )
            return pairs, 0, "returned", raw
        if provider == "csb":
            url = f"{base}/{unit['investigation']}/"
            status, raw = self._get(url, accept="text/html")
            if status == 404:
                return [], 0, "not_found", raw
            return (
                parse_csb_page(raw, url=url, slug=unit["investigation"]),
                0,
                "returned",
                raw,
            )
        if provider == "nhtsa-complaints":
            url = f"{base}/complaints/complaintsByVehicle"
            params = {
                "make": unit["make"],
                "model": unit["model"],
                "modelYear": str(unit["model_year"]),
            }
            status, raw = self._get(url, params)
            if status == 404:
                return [], 0, "not_found", raw
            pairs = parse_complaints(
                self._json(raw),
                unit,
                url=url,
                bound=int(self.declared["max_complaints"]),
            )
            return pairs, 0, "returned" if pairs else "not_found", raw
        url = f"{base}/DE/Publikationen/Untersuchungsberichte/{unit['report_id']}.html"
        status, raw = self._get(url, accept="text/html")
        if status == 404:
            return [], 0, "not_found", raw
        return (
            parse_bfu_page(raw, url=url, requested=unit["report_id"]),
            0,
            "returned",
            raw,
        )

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage

        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError(
                "operation_forbidden", "operation is not declared by the source"
            )
        if set(request) - {"operation", "parameters", "limit", "from_ms", "to_ms"}:
            raise SourcePackError(
                "parameter_forbidden", "runtime adapter received undeclared controls"
            )
        if (
            dict(request.get("parameters") or {})
            or request.get("from_ms") is not None
            or request.get("to_ms") is not None
        ):
            raise SourcePackError(
                "parameter_forbidden",
                "engineering-safety runs use the pinned declaration",
            )
        units = self._units()
        scope_hash = _digest(
            {"endpoint": self.source["endpoint"], "declared": self.declared}
        )
        try:
            state = (
                {"index": 0, "scope": scope_hash}
                if cursor is None
                else json.loads(cursor)
            )
            index = int(state["index"])
        except (ValueError, KeyError, TypeError) as exc:
            raise SourcePackError(
                "cursor_drift", "cursor is not a valid checkpoint"
            ) from exc
        if state.get("scope") != scope_hash:
            raise SourcePackError(
                "cursor_drift", "cursor belongs to a different declaration"
            )
        if index >= len(units):
            return RuntimePage(
                (), None, 0, receipt={"status": 200, "selection_index": index}
            )
        unit = dict(units[index])
        pairs, out_of_scope, outcome, raw = self._fetch(unit)
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        if len(pairs) > limit:
            raise SourcePackError(
                "response_too_large",
                "page has more records than the run's result budget",
            )
        records = [self._record(pointer, statement) for pointer, statement in pairs]
        next_cursor = (
            json.dumps({"index": index + 1, "scope": scope_hash}, sort_keys=True)
            if index + 1 < len(units)
            else None
        )
        return RuntimePage(
            tuple(records),
            next_cursor,
            len(raw),
            receipt={
                "status": 200,
                "selection_index": index,
                "selector": unit,
                "selection_size": len(units),
                "scope_hash": scope_hash,
                "response_sha256": hashlib.sha256(raw).hexdigest(),
                "selector_outcome": outcome,
                "records": len(records),
                "out_of_scope": out_of_scope,
                "final_page": next_cursor is None,
            },
        )

    def _record(self, pointer: str, statement: Mapping[str, Any]) -> dict[str, Any]:
        statement = {**statement, "payload_pointer": pointer}
        body = {k: v for k, v in statement.items() if k != "payload_pointer"}
        declared = AUTHORITIES[statement["provider"]][0]
        # One document per publication: an AD corrected in another Federal Register document, or an EASA revision
        # suffix, is a separate publication of the same record and may arrive in the same run.
        identifiers = dict(statement.get("identifiers") or {})
        publication = identifiers.get("fr_document_number") or identifiers.get(
            "ad_number"
        )
        return {
            "id": f"{statement['provider']}:{statement['record_kind']}:{statement['native_id']}"
            + (f":{publication}" if publication else ""),
            "title": f"{declared} {statement['native_id']}: {statement.get('title') or statement['record_kind']}",
            "url": statement.get("url") or self.source["endpoint"],
            "language": statement.get("language") or "en",
            **(
                {"published_at": statement["revision_date"]}
                if statement.get("revision_date")
                else {}
            ),
            "content": json.dumps(
                body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ),
            "engineering_safety_record": statement,
        }


ADAPTERS: dict[str, Any] = {CONNECTOR: EngineeringSafetyAdapter}


def fixture_transport(
    pages: Sequence[Mapping[str, Any]],
) -> Callable[..., Mapping[str, Any]]:
    """Replay authored native responses keyed by URL path plus the encoded query."""
    from src.ingestion.product_sources import safety_fixture_transport

    return safety_fixture_transport(pages)


def replay_native_fixture(
    source: Mapping[str, Any], fixture: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Decode an authored native fixture through the real adapter, page by page."""
    adapter = EngineeringSafetyAdapter(
        source, transport=fixture_transport(list(fixture.get("native_pages") or []))
    )
    operation = min(source["operations"])
    records: list[dict[str, Any]] = []
    cursor = None
    for _ in range(int(source["budgets"]["max_pages"])):
        page = adapter.fetch_page(
            {
                "operation": operation,
                "parameters": {},
                "limit": int(source["budgets"]["max_results"]),
            },
            cursor=cursor,
        )
        records.extend(dict(item) for item in page.records)
        cursor = page.next_cursor
        if cursor is None:
            break
    return records
