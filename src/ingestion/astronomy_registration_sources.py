"""Space-object registration, operator and re-entry acquisition for the Astronomy pack (#2224, SO03-SO06).

One native connector, ``astronomy-registration``, beside the Astronomy pack's
``astronomy`` connector (:mod:`src.ingestion.astronomy_sources`), in the same
``astronomy-and-space`` source pack. It fetches a source's declared documents
one page at a time on the runtime's same-host transport and parses them into
``noesis-astronomy-registration-record-v1`` records
(:mod:`src.kb.astronomy_registration`), projected by
:class:`src.kb.astronomy_registration.RegistrationProjector`. Documented shapes
(SO01, ``docs/development/astronomy-evidence/space-object-registration-audit.md``):

* ``unoosa-index-json`` - UNOOSA Online Index search results: international
  designator, national designator, name, State of registry, launch date,
  status, function, whether the object is registered with the UN and the
  registration document symbols. Keyed by COSPAR designator; status is stored
  as published with the index retrieval date (a change is a revision).
* ``unoosa-registration-text`` - the text rendition of a UN registration
  document or notification (ST/SG/SER.E, A/AC.105/INF). The declaration names
  the symbol, date, language and registrant; numbered paragraphs are object
  entries, quoted verbatim with their paragraph locator. Values are read only
  through labels declared for the document's language; nothing is translated.
* ``discos-json`` - ESA DISCOSweb JSON:API objects (with operators) or
  re-entries. Gated: the adapter refuses to run without a token and stores only
  the permitted subset (or, with ``mode: citation-only``, a citation).
* ``aerospace-reentry-html`` - The Aerospace Corporation's per-object
  re-entry page: each published prediction row and the confirmed report,
  through declared columns.

Bounded coverage: only declared designators, years, symbols or objects are
emitted; other rows are counted in the page receipt (``out_of_scope``). A row
that cannot be parsed becomes a ``rejection``, which the runtime quarantines.
Parsers never compute: no re-entry window, footprint or probability, and no
attribution beyond what a document states. ``PROVIDER_CONTRACTS`` records the
SO01 decisions; every source is ``unverified-live`` until a dated live run.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

from src.ingestion.source_packs import SourcePackError
from src.kb.astronomy_records import iso_day, iso_time, normalize_bibcode, normalize_cospar, normalize_doi, normalize_norad
from src.kb.astronomy_registration import CONTRACT, LANGUAGES, PROVIDERS, AstronomyError, validate_record

ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
CONNECTOR = "astronomy-registration"
FORMATS = {
    "unoosa-index-json": "unoosa-index",
    "unoosa-registration-text": "unoosa-registration-documents",
    "discos-json": "esa-discos",
    "aerospace-reentry-html": "aerospace-reentry",
}
DISCOS_MODES = ("permitted-subset", "citation-only")
MAX_ROWS = 10_000
ATTRIBUTION = {
    "unoosa-index": "United Nations Office for Outer Space Affairs, Online Index of Objects Launched into Outer Space.",
    "unoosa-registration-documents": "United Nations, registration document as cited.",
    "esa-discos": "ESA Space Debris Office, DISCOS (account-restricted; not redistributed).",
    "aerospace-reentry": "Reentry data courtesy of The Aerospace Corporation (CORDS).",
}
PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "unoosa-index": {
        "publisher": "UNOOSA Online Index of Objects Launched into Outer Space",
        "endpoint": "https://www.unoosa.org/oosa/osoindex/",
        "format": "unoosa-index-json",
        "access_decision": "unverified-live",
        "auth": "none",
        "reason": "fixture-verified parser; the JSON search path and field names are assumptions (verify)",
    },
    "unoosa-registration-documents": {
        "publisher": "United Nations registration documents (ST/SG/SER.E, A/AC.105/INF) and notifications",
        "endpoint": "https://www.unoosa.org/oosa/en/spaceobjectregister/",
        "format": "unoosa-registration-text",
        "access_decision": "unverified-live",
        "auth": "none",
        "reason": "fixture-verified parser of the text rendition through declared per-language labels; PDF layout is "
        "not parsed (verify the rendition)",
    },
    "esa-discos": {
        "publisher": "ESA DISCOSweb (objects, operators, re-entries)",
        "endpoint": "https://discosweb.esoc.esa.int/api/",
        "format": "discos-json",
        "access_decision": "unverified-live",
        "auth": "required-secret NOESIS_ESA_DISCOS_TOKEN",
        "reason": "account-gated: disabled without a token; permitted subset only (identifiers, name, class, operator "
        "attribution, re-entry epoch) or citation-only; never redistributed (verify the terms with ESA)",
    },
    "aerospace-reentry": {
        "publisher": "The Aerospace Corporation, reentry predictions (CORDS)",
        "endpoint": "https://aerospace.org/reentries",
        "format": "aerospace-reentry-html",
        "access_decision": "unverified-live",
        "auth": "none",
        "reason": "fixture-verified HTML table parser through declared columns; the page layout is an assumption "
        "(verify)",
    },
    "space-track-decay": {
        "publisher": "Space-Track.org decay and TIP messages",
        "access_decision": "not-implemented",
        "auth": "account",
        "reason": "account-restricted redistribution terms (verify); Aerospace and DISCOS cover v1",
    },
    "national-registries": {
        "publisher": "National registries of space objects (web sites)",
        "access_decision": "link-only",
        "auth": "none",
        "reason": "cited, never scraped; their content reaches the UN as ST/SG/SER.E documents",
    },
}
LIVE_VERIFICATION = {
    provider: {
        "status": "unverified-live" if contract["access_decision"] == "unverified-live" else contract["access_decision"],
        "note": "no dated live run from this runtime; offline fixtures only"
        if contract["access_decision"] == "unverified-live"
        else contract["reason"],
    }
    for provider, contract in PROVIDER_CONTRACTS.items()
}
# The labels the registration-document parser reads, per language. Only declared labels are read; another
# language must declare its own (a value is never translated).
DEFAULT_LABELS = {
    "en": {
        "cospar": "International designator",
        "norad": "Catalogue number",
        "name": "Name of space object",
        "national_designator": "National designator",
        "state": "State of registry",
        "launch_date": "Date of launch",
        "launch_site": "Territory or location of launch",
        "nodal_period": "Nodal period",
        "inclination": "Inclination",
        "apogee": "Apogee",
        "perigee": "Perigee",
        "function": "General function",
        "status": "Status",
        "status_date": "Date of change of status",
        "new_state": "State assuming supervision",
        "previous_state": "State previously supervising",
        "transfer_date": "Date of change in supervision",
        "reentry_date": "Date of re-entry",
        "reentry_location": "Location of re-entry",
        "operator": "Operator",
        "owner": "Owner",
        "operator_identifier": "Operator LEI",
        "instrument": "Instrument",
        "reference": "Reference",
    }
}
ORBIT_LABELS = ("nodal_period", "inclination", "apogee", "perigee")
DEFAULT_AEROSPACE_COLUMNS = {
    "issued": "Prediction Date (UTC)",
    "reentry_time": "Predicted Reentry Time (UTC)",
    "window": "Window",
    "report": "Report",
    "location": "Location",
    "latitude": "Latitude",
    "longitude": "Longitude",
}
DEFAULT_REPORT_KINDS = {"Prediction": "prediction", "Confirmed": "post_event"}
FIXTURE_SECRET = "fixture-discos-token-not-a-credential"


class RegistrationFormatError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _text(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise RegistrationFormatError("schema_drift", "document is not UTF-8 text") from exc


def _json(raw: bytes) -> Any:
    try:
        return json.loads(_text(raw))
    except ValueError as exc:
        raise RegistrationFormatError("schema_drift", "document is not JSON") from exc


def _cell(value: Any) -> str | None:
    text = re.sub(r"\s+", " ", str(value if value is not None else "")).strip()
    return None if text in {"", "-", "null", "None", "n/a", "N/A"} else text


def _source(provider: str, source_record_id: str, document: Mapping[str, Any], locator: str) -> dict[str, Any]:
    out = {
        "provider": provider,
        "source_record_id": source_record_id,
        "url": document["url"],
        "locator": locator,
        "attribution": ATTRIBUTION[provider],
        "document": document["label"],
    }
    stated = document.get("source_as_of") or document.get("published_on")
    if stated:
        out["published_at"] = stated
    return out


class _Result:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self.rejected: list[dict[str, Any]] = []
        self.counts = {"rows": 0, "out_of_scope": 0}

    def add(self, record: Mapping[str, Any], *, row: Any) -> None:
        try:
            self.records.append(validate_record(record))
        except AstronomyError as exc:
            self.reject("invalid_record", exc.message, row=row)

    def reject(self, code: str, reason: str, *, row: Any) -> None:
        self.rejected.append({"code": code, "reason": reason[:300], "row": str(row)})


def _in_scope(cospar: str | None, declared: Mapping[str, Any]) -> bool:
    designators = {normalize_cospar(d) for d in declared.get("designators") or []}
    years = {str(y) for y in declared.get("years") or []}
    if not designators and not years:
        return True
    if not cospar:
        return False
    return cospar in designators or cospar[:4] in years


# ------------------------------------------------------------------ UNOOSA index (SO03)


def parse_unoosa_index(raw: bytes, declared: Mapping[str, Any], document: Mapping[str, Any]) -> _Result:
    body = _json(raw)
    rows = body.get("results") if isinstance(body, Mapping) else None
    if not isinstance(rows, list):
        raise RegistrationFormatError("schema_drift", "index results are a list under 'results'")
    if len(rows) > MAX_ROWS:
        raise RegistrationFormatError("input_limit", "index document exceeds the row limit")
    result = _Result()
    for number, row in enumerate(rows, start=1):
        result.counts["rows"] += 1
        if not isinstance(row, Mapping):
            result.reject("invalid_record", "index row is not an object", row=number)
            continue
        cospar = normalize_cospar(row.get("internationalDesignator"))
        if not cospar:
            result.reject("invalid_record", "index row states no international designator", row=number)
            continue
        if not _in_scope(cospar, declared):
            result.counts["out_of_scope"] += 1
            continue
        registered = _cell(row.get("unRegistered"))
        documents = [d for d in (row.get("registrationDocuments") or []) if _cell(d)]
        record = {
            "kind": "registration_entry",
            "source": _source("unoosa-index", cospar, document, f"results[{number - 1}]"),
            "entry_kind": "index_entry",
            "cospar": cospar,
            "norad": normalize_norad(row.get("catalogueNumber")),
            "object_name": _cell(row.get("objectName")),
            "national_designator": _cell(row.get("nationalDesignator")),
            "registering_state": _cell(row.get("stateOfRegistry")),
            "un_registered": None if registered is None else registered.lower() in {"yes", "true", "y"},
            "un_document": _cell(documents[0]) if documents else None,
            "launch_date": iso_day(row.get("dateOfLaunch")),
            "status": _cell(row.get("status")),
            "function": _cell(row.get("function")),
            "decay_date": iso_day(row.get("dateOfDecay")),
        }
        result.add(record, row=number)
    return result


# ------------------------------------------------------------------ UN registration documents (SO04)

_PARAGRAPH = re.compile(r"^(\d+)\.\s+(.*)$")


def _labelled(lines: Sequence[str], labels: Mapping[str, str]) -> dict[str, list[str]]:
    by_label = {v.casefold(): k for k, v in labels.items()}
    out: dict[str, list[str]] = {}
    for line in lines:
        if ":" not in line:
            continue
        label, value = line.split(":", 1)
        key = by_label.get(label.strip().casefold())
        if key and value.strip():
            out.setdefault(key, []).append(value.strip())
    return out


def _registered_value(text: str) -> dict[str, str]:
    match = re.match(r"^(-?\d+(?:\.\d+)?)\s*(.*)$", text)
    if match:
        return {"value": match.group(1), **({"unit": match.group(2)} if match.group(2) else {})}
    return {"value": text}


def _reference(text: str) -> dict[str, Any]:
    """``text | identifier`` for an instrument, ``text | doi:...`` or ``text | bibcode:...`` for a paper."""
    parts = [p.strip() for p in text.split("|")]
    ref: dict[str, Any] = {"text": parts[0] or text}
    for part in parts[1:]:
        low = part.lower()
        if low.startswith("doi:") and normalize_doi(part[4:]):
            ref["doi"] = normalize_doi(part[4:])
        elif low.startswith("bibcode:") and normalize_bibcode(part[8:].strip()):
            ref["bibcode"] = normalize_bibcode(part[8:].strip())
        elif part:
            ref["identifier"] = part
    return ref


def parse_registration_text(raw: bytes, declared: Mapping[str, Any], document: Mapping[str, Any]) -> _Result:
    text = _text(raw)
    language = document.get("language") or "en"
    labels = dict((declared.get("labels") or {}).get(language) or DEFAULT_LABELS.get(language) or {})
    if not labels:
        raise RegistrationFormatError("schema_drift", f"no labels are declared for language {language!r}")
    symbol, date = document.get("symbol"), document.get("published_on")
    result = _Result()
    lines = text.splitlines()
    if len(lines) > MAX_ROWS:
        raise RegistrationFormatError("input_limit", "document exceeds the line limit")
    stated_symbol = next((line.strip() for line in lines[:5] if line.strip().startswith(("ST/SG/", "A/AC.105/"))), None)
    if stated_symbol and stated_symbol != symbol:
        result.reject("symbol_mismatch", f"the text states {stated_symbol}, the declaration {symbol}", row="header")
        return result
    header, paragraphs, current = [], [], None
    for line in lines:
        match = _PARAGRAPH.match(line.strip())
        if match:
            current = {"number": match.group(1), "lines": [line.rstrip()]}
            paragraphs.append(current)
        elif current is not None:
            if line.strip():
                current["lines"].append(line.rstrip())
        else:
            header.append(line.strip())
    head = _labelled(header, labels)
    instruments = [_reference(v) for v in head.get("instrument", [])]
    registrant = document.get("registrant")
    registrant_kind = document.get("registrant_kind")
    for paragraph in paragraphs:
        result.counts["rows"] += 1
        body = [re.sub(r"^\d+\.\s+", "", paragraph["lines"][0].strip())] + [
            line.strip() for line in paragraph["lines"][1:]
        ]
        values = _labelled(body, labels)

        def one(key: str) -> str | None:
            return (values.get(key) or [None])[0]

        cospar, norad, name = normalize_cospar(one("cospar")), normalize_norad(one("norad")), one("name")
        if not (cospar or norad or name):
            result.counts["narrative"] = result.counts.get("narrative", 0) + 1
            continue  # a narrative paragraph names no object; it is not an entry
        if cospar and not _in_scope(cospar, declared):
            result.counts["out_of_scope"] += 1
            continue
        if one("new_state"):
            entry_kind = "transfer_of_supervision"
        elif one("status_date"):
            entry_kind = "change_of_status"
        elif one("reentry_date"):
            entry_kind = "re_entry_notice"
        elif one("launch_date") or any(one(k) for k in ORBIT_LABELS):
            entry_kind = "registration"
        else:
            entry_kind = "additional_information"
        locator = {"paragraph": paragraph["number"]}
        source_record_id = f"{symbol}#para {paragraph['number']}"
        record: dict[str, Any] = {
            "kind": "registration_entry",
            "source": _source(
                "unoosa-registration-documents", source_record_id, document, f"{symbol} para {paragraph['number']}"
            ),
            "entry_kind": entry_kind,
            "cospar": cospar,
            "norad": norad,
            "object_name": name,
            "national_designator": one("national_designator"),
            "registering_state": one("state") or registrant,
            "registrant_kind": registrant_kind,
            "un_document": symbol,
            "document_date": date,
            "document_locator": locator,
            "language": language,
            "quotation": "\n".join(paragraph["lines"]),
            "launch_date": iso_day(one("launch_date")),
            "launch_site": one("launch_site"),
            "function": one("function"),
            "status": one("status") if entry_kind != "change_of_status" else None,
            "instruments": instruments + [_reference(v) for v in values.get("instrument", [])] or None,
            "references": [_reference(v) for v in values.get("reference", [])] or None,
        }
        orbit = {k: _registered_value(one(k)) for k in ORBIT_LABELS if one(k)}
        if orbit and entry_kind == "registration":
            record["registered_orbit"] = orbit
        if entry_kind == "change_of_status":
            record["status_change"] = {"status": one("status") or "not stated",
                                       "effective_date": iso_day(one("status_date"))}
        if entry_kind == "transfer_of_supervision":
            record["supervision"] = {"from": one("previous_state"), "to": one("new_state"),
                                     "effective_date": iso_day(one("transfer_date"))}
        if entry_kind == "re_entry_notice":
            record["reentry"] = {"date": iso_day(one("reentry_date")), "location": one("reentry_location")}
        result.add(record, row=f"para {paragraph['number']}")
        for role in ("operator", "owner"):
            for operator in values.get(role, []):
                identifier = one("operator_identifier") if role == "operator" else None
                result.add(
                    {
                        "kind": "operator_assertion",
                        "source": _source(
                            "unoosa-registration-documents",
                            f"{source_record_id}#{role}#{operator}",
                            document,
                            f"{symbol} para {paragraph['number']}",
                        ),
                        "operator_name": operator,
                        "role": role,
                        "cospar": cospar,
                        "norad": norad,
                        "object_name": name,
                        "asserted_on": date,
                        "identifier": {"scheme": "lei", "value": identifier} if identifier else None,
                        "un_document": symbol,
                        "document_locator": locator,
                    },
                    row=f"para {paragraph['number']} {role}",
                )
    return result


# ------------------------------------------------------------------ ESA DISCOS (SO05)


def parse_discos(raw: bytes, declared: Mapping[str, Any], document: Mapping[str, Any]) -> _Result:
    body = _json(raw)
    data = body.get("data") if isinstance(body, Mapping) else None
    if not isinstance(data, list):
        raise RegistrationFormatError("schema_drift", "DISCOS responses are JSON:API documents with a data list")
    included = {(i.get("type"), str(i.get("id"))): i for i in body.get("included") or [] if isinstance(i, Mapping)}
    wanted = {str(o) for o in declared.get("objects") or []}
    mode = declared.get("mode") or "permitted-subset"
    resource = document.get("resource") or "objects"
    result = _Result()
    base = "https://discosweb.esoc.esa.int/objects/"

    def object_ids(attrs: Mapping[str, Any]) -> dict[str, Any]:
        return {"cospar": normalize_cospar(attrs.get("cosparId")), "norad": normalize_norad(attrs.get("satno"))}

    for number, row in enumerate(data, start=1):
        result.counts["rows"] += 1
        if not isinstance(row, Mapping) or not str(row.get("id") or "").isdigit():
            result.reject("invalid_record", "DISCOS row states no numeric id", row=number)
            continue
        attrs = dict(row.get("attributes") or {})
        if resource == "objects":
            discos_id = str(row["id"])
            ids = object_ids(attrs)
            if wanted and not ({discos_id, ids["cospar"], ids["norad"]} & wanted):
                result.counts["out_of_scope"] += 1
                continue
            if mode == "citation-only":
                result.add(
                    {
                        "kind": "discos_citation",
                        "source": _source("esa-discos", discos_id, document, f"data[{number - 1}]"),
                        "discos_id": discos_id,
                        "url": base + discos_id,
                        "note": "citation only: the DISCOS terms permit no storage of this object's attributes",
                        "restricted": True,
                        **ids,
                    },
                    row=number,
                )
                continue
            result.add(
                {
                    "kind": "discos_object",
                    "source": _source("esa-discos", discos_id, document, f"data[{number - 1}]"),
                    "discos_id": discos_id,
                    "object_name": _cell(attrs.get("name")),
                    "object_class": _cell(attrs.get("objectClass")),
                    "restricted": True,
                    **ids,
                },
                row=number,
            )
            operators = ((row.get("relationships") or {}).get("operators") or {}).get("data") or []
            for ref in operators:
                operator = included.get(("operator", str(ref.get("id"))))
                name = _cell(((operator or {}).get("attributes") or {}).get("name"))
                if not name:
                    result.reject("invalid_record", "operator relationship without an included name", row=number)
                    continue
                result.add(
                    {
                        "kind": "operator_assertion",
                        "source": _source("esa-discos", f"{discos_id}#operator#{ref.get('id')}", document,
                                          f"data[{number - 1}].relationships.operators"),
                        "operator_name": name,
                        "role": "operator",
                        "discos_id": discos_id,
                        "restricted": True,
                        **ids,
                    },
                    row=number,
                )
        else:
            epoch = iso_time(attrs.get("epoch")) or iso_day(attrs.get("epoch"))
            refs = ((row.get("relationships") or {}).get("objects") or {}).get("data") or []
            if not epoch or not refs:
                result.reject("invalid_record", "re-entry states no epoch or object", row=number)
                continue
            for ref in refs:
                discos_id = str(ref.get("id"))
                obj = included.get(("object", discos_id)) or {}
                ids = object_ids(obj.get("attributes") or {})
                if wanted and not ({discos_id, ids["cospar"], ids["norad"]} & wanted):
                    result.counts["out_of_scope"] += 1
                    continue
                if mode == "citation-only":
                    result.add(
                        {
                            "kind": "discos_citation",
                            "source": _source("esa-discos", discos_id, document, f"data[{number - 1}]"),
                            "discos_id": discos_id,
                            "url": base + discos_id,
                            "note": "citation only: re-entry epoch not stored under the DISCOS terms",
                            "restricted": True,
                            **ids,
                        },
                        row=number,
                    )
                    continue
                result.add(
                    {
                        "kind": "reentry_report",
                        "source": _source("esa-discos", f"reentry#{discos_id}", document, f"data[{number - 1}]"),
                        "report_kind": "post_event",
                        "issued_at": document.get("source_as_of") or epoch,
                        "reported_time": epoch,
                        "reported_time_text": str(attrs.get("epoch")),
                        "discos_id": discos_id,
                        "restricted": True,
                        **ids,
                    },
                    row=number,
                )
    return result


# ------------------------------------------------------------------ Aerospace re-entries (SO06)


class _Tables(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self._row is not None and self._cell is not None:
            self._row.append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def parse_aerospace(raw: bytes, declared: Mapping[str, Any], document: Mapping[str, Any]) -> _Result:
    parser = _Tables()
    parser.feed(_text(raw))
    if len(parser.rows) > MAX_ROWS:
        raise RegistrationFormatError("input_limit", "page exceeds the row limit")
    columns = {**DEFAULT_AEROSPACE_COLUMNS, **dict(declared.get("columns") or {})}
    kinds = {**DEFAULT_REPORT_KINDS, **dict(declared.get("report_kinds") or {})}
    result = _Result()
    if not parser.rows:
        raise RegistrationFormatError("schema_drift", "no prediction table on the page")
    header = parser.rows[0]
    index = {key: header.index(label) for key, label in columns.items() if label in header}
    if "issued" not in index or "report" not in index:
        raise RegistrationFormatError("schema_drift", "declared issue and report columns are missing")
    obj = dict(document.get("object") or {})
    norad, cospar = normalize_norad(obj.get("norad")), normalize_cospar(obj.get("cospar"))
    if not norad:
        raise RegistrationFormatError("schema_drift", "a re-entry page declares its NORAD number")
    wanted = {str(o) for o in declared.get("objects") or []}
    for number, row in enumerate(parser.rows[1:], start=1):
        result.counts["rows"] += 1
        if wanted and norad not in wanted:
            result.counts["out_of_scope"] += 1
            continue

        def cell(key: str) -> str | None:
            return _cell(row[index[key]]) if key in index and index[key] < len(row) else None

        issued = iso_time(cell("issued")) or iso_day(cell("issued"))
        report = kinds.get(cell("report") or "")
        if not issued or not report:
            result.reject("invalid_record", "row states no issue time or an undeclared report label", row=number)
            continue
        reported = cell("reentry_time")
        location = cell("location")
        lat, lon = cell("latitude"), cell("longitude")
        result.add(
            {
                "kind": "reentry_report",
                "source": _source("aerospace-reentry", norad, document, f"table row {number}"),
                "report_kind": report,
                "issued_at": issued,
                "norad": norad,
                "cospar": cospar,
                "object_name": _cell(obj.get("name")),
                "reported_time": iso_time(reported) or iso_day(reported),
                "reported_time_text": reported,
                "uncertainty": {"text": cell("window")} if cell("window") else None,
                "location": {"text": location, "latitude": lat, "longitude": lon} if location else None,
            },
            row=number,
        )
    return result


# ------------------------------------------------------------------ documents and adapter


def parse_document(fmt: str, raw: bytes, declared: Mapping[str, Any], document: Mapping[str, Any]) -> dict[str, Any]:
    parsers: dict[str, Callable[..., _Result]] = {
        "unoosa-index-json": parse_unoosa_index,
        "unoosa-registration-text": parse_registration_text,
        "discos-json": parse_discos,
        "aerospace-reentry-html": parse_aerospace,
    }
    if fmt not in parsers:
        raise RegistrationFormatError("schema_drift", f"unknown format {fmt!r}")
    result = parsers[fmt](raw, declared, document)
    return {"records": result.records, "rejected": result.rejected, "counts": dict(result.counts),
            "file_sha256": _sha(raw)}


def registration_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    declared = dict(source.get("astronomy_registration") or {})
    if declared.get("format") not in FORMATS:
        raise SourcePackError("invalid_mapping", f"registration format is one of {sorted(FORMATS)}")
    if declared.get("provider") != FORMATS[declared["format"]] or declared["provider"] not in PROVIDERS:
        raise SourcePackError("invalid_mapping", "the declared provider does not publish this format")
    if source.get("mapping", {}).get("target_schema") != CONTRACT:
        raise SourcePackError("invalid_mapping", f"registration sources map to {CONTRACT}")
    documents = declared.get("documents") or []
    if not documents or len(documents) > 60:
        raise SourcePackError("invalid_mapping", "registration sources declare 1-60 documents")
    host = (urlsplit(str(source.get("endpoint") or "")).hostname or "").casefold()
    for document in documents:
        parts = urlsplit(str(document.get("url") or ""))
        if parts.scheme != "https" or (parts.hostname or "").casefold() != host:
            raise SourcePackError("invalid_mapping", "registration documents are HTTPS URLs on the endpoint host")
        if not document.get("label"):
            raise SourcePackError("invalid_mapping", "documents carry a label")
        if declared["format"] == "unoosa-registration-text":
            if not document.get("symbol") or not iso_day(document.get("published_on")):
                raise SourcePackError("invalid_mapping", "a registration document declares its symbol and date")
            if document.get("language", "en") not in LANGUAGES:
                raise SourcePackError("invalid_mapping", f"language is one of {LANGUAGES}")
        if declared["format"] == "aerospace-reentry-html" and not normalize_norad(
            (document.get("object") or {}).get("norad")
        ):
            raise SourcePackError("invalid_mapping", "a re-entry page declares its object's NORAD number")
    if declared["format"] == "discos-json":
        if declared.get("mode", "permitted-subset") not in DISCOS_MODES:
            raise SourcePackError("invalid_mapping", f"DISCOS mode is one of {DISCOS_MODES}")
        if (source.get("auth") or {}).get("kind") != "required-secret":
            raise SourcePackError("invalid_mapping", "DISCOS is account-gated and needs a secret reference")
    for key, bound in (("designators", 50), ("objects", 50), ("years", 10)):
        if len(declared.get(key) or []) > bound:
            raise SourcePackError("unbounded_source", f"declared {key} exceed the v1 bound of {bound}")
    return declared


class RegistrationAdapter:
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
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        self.source = json.loads(json.dumps(source))
        self.declared = registration_declaration(self.source)
        self.secret = secret
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
            "astronomy_registration": {
                "provider": self.declared["provider"],
                "format": self.declared["format"],
                "documents": [d["label"] for d in self.declared["documents"]],
                "live_verification": LIVE_VERIFICATION[self.declared["provider"]]["status"],
            },
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage, _retry_after_ms

        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError("operation_forbidden", "operation is not declared by the source")
        if set(request) - {"operation", "parameters", "limit", "from_ms", "to_ms"}:
            raise SourcePackError("parameter_forbidden", "runtime adapter received undeclared controls")
        if dict(request.get("parameters") or {}):
            raise SourcePackError("parameter_forbidden", "registration runs fetch the declared documents only")
        fmt = self.declared["format"]
        headers = {"Accept": "application/json" if fmt.endswith("-json") else "text/plain, text/html"}
        if fmt == "discos-json":
            if not self.secret:
                raise SourcePackError(
                    "credential_missing", "ESA DISCOS is disabled: no account token is configured"
                )
            headers["Authorization"] = f"Bearer {self.secret}"
            headers["DiscosWeb-Api-Version"] = "2"
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
        response = self.transport(
            url=url, params={}, headers=headers, timeout=int(self.definition["limits"]["timeout_ms"]) / 1000
        )
        final_host = (urlsplit(str(response.get("final_url") or url)).hostname or "").casefold()
        if final_host != host:
            raise SourcePackError("network_policy", "document was served from another host")
        status = int(response.get("status", 200))
        response_headers = {str(k).casefold(): v for k, v in dict(response.get("headers") or {}).items()}
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError("response_too_large", "document exceeds its byte limit")
        if status == 429:
            raise SourcePackError("rate_limited", "provider quota is temporarily exhausted",
                                  retry_after_ms=_retry_after_ms(response_headers.get("retry-after")))
        if status in {401, 403}:
            raise SourcePackError("authentication_failed", f"document refused (HTTP {status})")
        if status >= 500:
            raise SourcePackError("source_unavailable", f"provider returned HTTP {status}")
        if status >= 400:
            raise SourcePackError("schema_drift", f"document returned HTTP {status}")
        try:
            parsed = parse_document(fmt, raw, self.declared, document)
        except RegistrationFormatError as exc:
            raise SourcePackError("response_too_large" if exc.code == "input_limit" else "schema_drift",
                                  f"{exc.code}: {exc}") from exc
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        if len(parsed["records"]) > limit:
            raise SourcePackError("budget_exhausted", "document has more records than the run's result budget")
        records = []
        for record in parsed["records"]:
            source = record["source"]
            records.append(
                {
                    "id": f"{source['provider']}:{record['kind']}:{source['source_record_id']}",
                    "title": f"{record['kind']}: {source['source_record_id']}"[:300],
                    "url": source.get("url") or url,
                    "language": record.get("language") or "en",
                    "published_at": source.get("published_at"),
                    # DISCOS values are account-restricted: the document keeps only the citation.
                    "content": json.dumps(
                        {"kind": record["kind"], "source": source, "restricted": True}
                        if record.get("restricted")
                        else record,
                        sort_keys=True,
                        ensure_ascii=False,
                    ),
                    "registration_record": record,
                }
            )
        for item in parsed["rejected"]:
            records.append(
                {
                    "id": f"{self.declared['provider']}:rejected:{document['label']}:{item['row']}",
                    "url": url,
                    "rejection": {k: item[k] for k in ("code", "reason", "row")},
                }
            )
        receipt = {
            "status": status,
            "document": document["label"],
            "format": fmt,
            "file_sha256": parsed["file_sha256"],
            "source_as_of": document.get("source_as_of") or document.get("published_on"),
            "counts": {**parsed["counts"], "records": len(parsed["records"]), "rejected": len(parsed["rejected"])},
            "live_verification": LIVE_VERIFICATION[self.declared["provider"]]["status"],
            "final_page": index + 1 >= len(documents),
        }
        next_cursor = str(index + 1) if index + 1 < len(documents) else None
        return RuntimePage(tuple(records), next_cursor, len(raw), receipt=receipt)


ADAPTERS = {CONNECTOR: RegistrationAdapter}


def fixture_transport(pages: Sequence[Mapping[str, Any]]) -> Callable[..., Mapping[str, Any]]:
    """Replay authored documents keyed by URL path (+ query)."""
    by_key = {page["request"]: page for page in pages}

    def transport(*, url, params, headers, timeout):
        del params, headers, timeout
        parts = urlsplit(url)
        key = parts.path + ("?" + parts.query if parts.query else "")
        page = by_key.get(key)
        if page is None:
            raise SourcePackError("fixture_missing", f"no native page for {key}")
        body = page.get("body")
        content = body.encode() if isinstance(body, str) else b"" if body is None else json.dumps(body).encode()
        return {"status": int(page.get("status", 200)), "headers": dict(page.get("headers") or {}),
                "content": content, **({"final_url": page["final_url"]} if page.get("final_url") else {})}

    return transport


def replay_native_fixture(source: Mapping[str, Any], fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    adapter = RegistrationAdapter(source, transport=fixture_transport(list(fixture["native_pages"])),
                                  secret=FIXTURE_SECRET)
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
