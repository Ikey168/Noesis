"""Transparency-register and meeting-declaration acquisition for the Political ``lobbying`` feature (#1911, T01/T03-T05).

One native connector, ``lobbying-register``, fetches one documented,
machine-readable publication per source (a register export or a meeting list)
and parses it into an *export*: its publication date, the file digest and every
entry as the register states it. Five documented formats are supported:

* ``eu-tr-xml`` - the EU Transparency Register open-data XML export
  (registrants, declared clients, fields of interest, EU legislative files,
  declared cost/revenue ranges and EU grants);
* ``de-lobbyregister-json`` - the German Lobbyregister JSON export
  (register number, legal form, fields of interest, clients and principals,
  regulatory projects with Bundestag printed-paper numbers, the annual
  financial-expenditure range and disclosed statements);
* ``ep-meetings-csv`` - European Parliament meeting declarations of MEPs;
* ``ec-meetings-json`` - Commission meetings of Commissioners, cabinet
  members and Directors-General with interest representatives;
* ``uk-orcl-csv`` - the UK Register of Consultant Lobbyists (one revision per
  registrant and quarterly return).

Registers are never merged: each entry keeps its own register and native
identifier, names, declared ranges and references exactly as filed. A spend
range stays the declared lower and upper bound (either may be open) with its
currency and period; nothing here computes a midpoint, total or estimate, and
nothing states influence, wrongdoing or undeclared lobbying. Legislative
references are recorded only from structured register fields, or from exact
procedure-reference / CELEX patterns inside the field that names legislative
files; a shared keyword is never a reference.

An export is all-or-nothing: an entry count above the run's result budget is a
``budget_exhausted`` failure rather than a truncated export, because missing
entries of a full register export would read as deregistrations.

``PROVIDER_CONTRACTS`` records the T01 access decisions (``unverified-live``
until a dated live run; ``not-implemented`` with a reason).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

from src.ingestion.source_packs import SourcePackError

ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
EXPORT_CONTRACT = "noesis-lobbying-export-v1"
CONNECTOR = "lobbying-register"
# register id -> entry kind it publishes and the jurisdiction its references belong to
REGISTERS = {
    "eu-tr": {
        "kind": "registrant",
        "jurisdiction": "EU",
        "label": "EU Transparency Register",
    },
    "de-lobbyregister": {
        "kind": "registrant",
        "jurisdiction": "DE",
        "label": "Lobbyregister beim Deutschen Bundestag",
    },
    "uk-orcl": {
        "kind": "registrant",
        "jurisdiction": "GB",
        "label": "UK Register of Consultant Lobbyists",
    },
    "ep-meetings": {
        "kind": "meeting",
        "jurisdiction": "EU",
        "label": "European Parliament meeting declarations",
    },
    "ec-meetings": {
        "kind": "meeting",
        "jurisdiction": "EU",
        "label": "European Commission meeting declarations",
    },
}
FORMATS = {
    "eu-tr-xml": "eu-tr",
    "de-lobbyregister-json": "de-lobbyregister",
    "ep-meetings-csv": "ep-meetings",
    "ec-meetings-json": "ec-meetings",
    "uk-orcl-csv": "uk-orcl",
}
# Formats whose file can be the whole register: an entry absent from a newer full export was removed from it.
# (A Lobbyregister export is full only when it states a total equal to its entries.)
FULL_EXPORT_FORMATS = frozenset({"eu-tr-xml", "de-lobbyregister-json"})
# Reference schemes and the jurisdiction whose dossiers they can name.
REFERENCE_SCHEMES = {
    "eu-procedure": "EU",
    "celex": "EU",
    "eli": "EU",
    "de-drucksache": "DE",
    "de-regulatory-project": "DE",
}
IDENTIFIER_SCHEMES = (
    "lei",
    "gb-coh",
    "de-register",
    "eu-tr",
    "de-lobbyregister",
    "website",
)
SPEND_KINDS = ("costs", "revenue", "expenditure", "client-revenue", "grant")
MAX_ENTRIES = 50_000
REVIEW_BOUNDARY = (
    "Register declarations only, as filed: no influence, corruption or undeclared-lobbying claim, and no spend "
    "range is collapsed to a point estimate, total or average."
)

# T01 access decisions. Terms and endpoints below are recorded from the
# providers' published documentation as known without network access; every
# item marked ``verify`` must be checked against the live terms before a
# dated live run is accepted (#2024).
PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "eu-transparency-register": {
        "register": "eu-tr",
        "publisher": "European Parliament and European Commission (Joint Transparency Register Secretariat)",
        "access": "Open-data XML export of all registrants, published on data.europa.eu and linked from the register "
        "site (verify the current export URL and whether it redirects to another host)",
        "format": "eu-tr-xml",
        "authentication": "none",
        "rate_limits": "undocumented; one bounded download per run",
        "pagination": "none (one full file)",
        "cadence": "the export is regenerated daily (verify)",
        "record_kinds": [
            "registrant",
            "client",
            "declared-interest",
            "spend-range",
            "grant",
            "revision",
        ],
        "identifiers": ["identification number (NNNNNNNNNNNN-NN)"],
        "revisions": "each entry carries its registration and last-update dates; historical versions are not "
        "retrievable from the export, so revisions are the successive exports this system acquired",
        "deregistrations": "a removed entry is absent from the next full export (deregistration revision derived)",
        "terms": "Commission reuse policy (Decision 2011/833/EU), attribution (verify the register's notice)",
        "access_decision": "unverified-live",
        "reason": "fixture-verified parser; no dated live run yet; XML element names must be verified against the "
        "published schema",
    },
    "de-lobbyregister": {
        "register": "de-lobbyregister",
        "publisher": "Deutscher Bundestag (Lobbyregister)",
        "access": "JSON export of register entries from the public search (sucheJson); an API with keys is "
        "announced (verify endpoint, pagination and whether a key is needed)",
        "format": "de-lobbyregister-json",
        "authentication": "none for the export (verify)",
        "rate_limits": "undocumented; one bounded download per run",
        "pagination": "export of the selected result set (verify page limits)",
        "cadence": "entries are updated by registrants; the register confirms annually",
        "record_kinds": [
            "registrant",
            "client",
            "declared-interest",
            "spend-range",
            "position-paper",
            "revision",
        ],
        "identifiers": ["register number (R000000)", "register entry id and version"],
        "revisions": "entries carry a version number and validity date; earlier versions are published on the "
        "entry page (verify machine-readable access); only exports this system acquired are revisions here",
        "deregistrations": "inactive entries are flagged (activeLobbyist=false) or removed",
        "documents": "statements (Stellungnahmen) and position papers are published as PDF links; text is not "
        "retained unless the source's retention is set to text after the terms are verified (link-only default)",
        "terms": "Bundestag open-data terms, attribution (verify)",
        "access_decision": "unverified-live",
        "reason": "fixture-verified parser; JSON field names and the export endpoint must be verified",
    },
    "ep-meetings": {
        "register": "ep-meetings",
        "publisher": "European Parliament (MEP meeting declarations)",
        "access": "Meeting declarations published on MEP pages and the meetings search, with a CSV export (verify)",
        "format": "ep-meetings-csv",
        "authentication": "none",
        "rate_limits": "undocumented; one bounded download per run",
        "pagination": "none for the export (verify row caps)",
        "cadence": "published as MEPs declare (rapporteurs, shadows and committee chairs must publish)",
        "record_kinds": ["meeting"],
        "identifiers": [
            "MEP identifier",
            "procedure reference where declared",
            "EU TR number where given",
        ],
        "revisions": "no revision history; a changed declaration is observed as a changed row",
        "terms": "European Parliament legal notice, reuse with attribution (verify)",
        "access_decision": "unverified-live",
        "reason": "fixture-verified parser; CSV column names must be verified",
    },
    "ec-meetings": {
        "register": "ec-meetings",
        "publisher": "European Commission (meetings of Commissioners, cabinet members and Directors-General)",
        "access": "Meeting lists published per Commissioner and in the Transparency Register (verify a machine-"
        "readable export; HTML pages are never scraped)",
        "format": "ec-meetings-json",
        "authentication": "none",
        "rate_limits": "undocumented; one bounded download per run",
        "pagination": "verify",
        "cadence": "published within two weeks of a meeting (verify)",
        "record_kinds": ["meeting"],
        "identifiers": ["official name and role", "EU TR number of each organisation"],
        "revisions": "no revision history",
        "terms": "Commission reuse policy (Decision 2011/833/EU) (verify)",
        "access_decision": "unverified-live",
        "reason": "fixture-verified parser for a documented JSON shape; the live export itself must be verified; "
        "without one the source stays unavailable rather than scraped",
    },
    "uk-orcl": {
        "register": "uk-orcl",
        "publisher": "Office of the Registrar of Consultant Lobbyists (UK)",
        "access": "Register download (CSV) of registrants and their quarterly client returns (verify URL)",
        "format": "uk-orcl-csv",
        "authentication": "none",
        "rate_limits": "undocumented; one bounded download per run",
        "pagination": "none (one file)",
        "cadence": "quarterly returns",
        "record_kinds": ["registrant", "client", "revision"],
        "identifiers": [
            "registrant name and ORCL reference",
            "Companies House number where declared",
        ],
        "revisions": "each quarterly return is its own revision",
        "terms": "Open Government Licence v3.0 (verify)",
        "access_decision": "unverified-live",
        "reason": "fixture-verified parser; column names and the download URL must be verified",
    },
    "bundestag-party-financing": {
        "register": None,
        "publisher": "Deutscher Bundestag (Parteienfinanzierung: Rechenschaftsberichte, Großspenden)",
        "access": "Published as PDF reports (Drucksachen) and an HTML list of large donations; no documented "
        "machine-readable export was found",
        "record_kinds": [],
        "access_decision": "not-implemented",
        "reason": "party financing is not a lobbying declaration and has no verified machine-readable interface; "
        "pages are never scraped. It may be added as a separate source once a documented export exists",
    },
    "integrity-watch-eu": {
        "register": None,
        "publisher": "Transparency International EU (Integrity Watch), an aggregator",
        "access": "Web application over re-published register and meeting data",
        "record_kinds": [],
        "access_decision": "not-implemented",
        "reason": "aggregator terms of reuse are not verified, so it is not used even as a fixture-only cross-check; "
        "if its terms are verified it may only cross-check primary-register records and keeps its own source "
        "identity, never replacing a primary register",
    },
}


class LobbyingFormatError(ValueError):
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


def _day(value: Any) -> str | None:
    text = str(value or "").strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}", text):
        try:
            return date.fromisoformat(text[:10]).isoformat()
        except ValueError:
            return None
    match = re.match(r"^(\d{2})[./](\d{2})[./](\d{4})$", text)
    if match:
        try:
            return date(int(match[3]), int(match[2]), int(match[1])).isoformat()
        except ValueError:
            return None
    return None


# ------------------------------------------------------------------ references


_PROCEDURE = re.compile(r"(?<!\d)(\d{4})\s*/\s*(\d{4})\s*\(\s*([A-Za-z]{3})\s*\)")
_CELEX = re.compile(
    r"(?<![0-9A-Za-z])([1-9CE]\d{4}[A-Z]{1,2}\d{4}(?:\(\d{1,2}\))?)(?![0-9A-Za-z])"
)


def reference(
    scheme: str, value: Any, *, source_field: str, extracted_from_text: bool = False
) -> dict[str, Any] | None:
    """A legislative reference exactly as a register field states it, with its canonical key; None if malformed."""
    if scheme not in REFERENCE_SCHEMES:
        return None
    raw = _clean(value)
    if raw is None:
        return None
    key = reference_key(scheme, raw)
    if key is None:
        return None
    return {
        "scheme": scheme,
        "value": raw,
        "key": key,
        "jurisdiction": REFERENCE_SCHEMES[scheme],
        "source_field": source_field,
        "extracted_from_text": extracted_from_text,
    }


def reference_key(scheme: str, value: Any) -> str | None:
    """Canonical key of one reference; the same function normalizes dossier identifiers (no fuzzy matching)."""
    text = " ".join(str(value or "").split())
    if scheme == "eu-procedure":
        match = re.fullmatch(
            r"(\d{4})\s*[/_.-]\s*(\d{4})\s*[(_.-]?\s*([A-Za-z]{3})\s*\)?", text
        )
        return (
            f"eu-procedure:{match[1]}/{match[2]}({match[3].lower()})" if match else None
        )
    if scheme == "celex":
        match = re.fullmatch(
            r"([1-9CEce]\d{4}[A-Za-z]{1,2}\d{4})(?:\(\d{1,2}\))?", text
        )
        return f"celex:{match[1].upper()}" if match else None
    if scheme == "eli":
        match = re.fullmatch(
            r"(?:https?://data\.europa\.eu)?/?eli/([a-z0-9_/.-]+?)/?", text.lower()
        )
        return f"eli:{match[1]}" if match else None
    if scheme == "de-drucksache":
        match = re.fullmatch(
            r"(?:(?:bt-?)?(?:drucksache|drs?)\.?[\s_.-]*)?(\d{1,2})\s*[/_.-]\s*(\d{1,6})",
            text.lower(),
        )
        return f"de-drucksache:{int(match[1])}/{int(match[2])}" if match else None
    if scheme == "de-regulatory-project":
        match = re.fullmatch(r"(RV\d{1,10})", text.upper())
        return f"de-regulatory-project:{match[1]}" if match else None
    return None


def references_in_text(text: Any, *, source_field: str) -> list[dict[str, Any]]:
    """Exact procedure-reference and CELEX patterns inside the field that names legislative files."""
    found = []
    for match in _PROCEDURE.finditer(str(text or "")):
        ref = reference(
            "eu-procedure",
            match.group(0),
            source_field=source_field,
            extracted_from_text=True,
        )
        if ref:
            found.append(ref)
    for match in _CELEX.finditer(str(text or "")):
        ref = reference(
            "celex", match.group(1), source_field=source_field, extracted_from_text=True
        )
        if ref:
            found.append(ref)
    unique = {}
    for ref in found:
        unique.setdefault(ref["key"], ref)
    return list(unique.values())


# ------------------------------------------------------------------ spend ranges


def _amount(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = re.sub(r"[\s  ,]", "", str(value)).replace("€", "").replace("£", "")
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    if number < 0:
        return None
    return format(number.normalize(), "f")


def spend_range(
    kind: str,
    lower: Any,
    upper: Any,
    currency: Any,
    period_start: Any,
    period_end: Any,
    *,
    as_filed: Any,
    party: str | None = None,
) -> dict[str, Any] | None:
    """A declared range as filed: lower/upper (either may be open), currency and period. Never a point value."""
    currency_code = str(currency or "").strip().upper()
    low, high = _amount(lower), _amount(upper)
    if (
        kind not in SPEND_KINDS
        or (low is None and high is None)
        or not re.fullmatch(r"[A-Z]{3}", currency_code)
    ):
        return None
    if low is not None and high is not None and Decimal(low) > Decimal(high):
        raise LobbyingFormatError(
            "schema_drift", "declared range has a lower bound above its upper bound"
        )
    return {
        "kind": kind,
        "lower": low,
        "upper": high,
        "lower_open": low is None,
        "upper_open": high is None,
        "currency": currency_code,
        "period": {"start": _day(period_start), "end": _day(period_end)},
        "as_filed": _clean(as_filed),
        "party": party,
    }


def parse_range_text(text: Any) -> tuple[str | None, str | None]:
    """Bounds from a range written as text: "100 000 - 199 999", "< 10 000", ">= 10 000 000", "0"."""
    raw = str(text or "").replace(" ", " ").replace(" ", " ")
    numbers = [
        n
        for n in (_amount(m) for m in re.findall(r"\d[\d\s.,]*", raw))
        if n is not None
    ]
    if re.search(r"(<|less than|under|bis)", raw, re.I) and len(numbers) == 1:
        return None, numbers[0]
    if re.search(r"(>|more than|over|ab|at least)", raw, re.I) and len(numbers) == 1:
        return numbers[0], None
    if len(numbers) == 2:
        return numbers[0], numbers[1]
    if len(numbers) == 1:
        return numbers[0], numbers[0]
    return None, None


# ------------------------------------------------------------------ parsers


def _xml(raw: bytes):
    import defusedxml.ElementTree as ET

    if not isinstance(raw, bytes) or not 0 < len(raw) <= 100_000_000:
        raise LobbyingFormatError("input_limit", "export file is missing or oversized")
    try:
        return ET.fromstring(raw, forbid_entities=True, forbid_external=True)
    except Exception as exc:  # noqa: BLE001 - any XML failure is provider drift
        raise LobbyingFormatError(
            "schema_drift", "export is not well-formed XML"
        ) from exc


def _tag(node) -> str:
    return node.tag.rsplit("}", 1)[-1]


def _children(node, name: str) -> list:
    return [child for child in node if _tag(child) == name]


def _child(node, name: str):
    found = _children(node, name)
    return found[0] if found else None


def _text(node, name: str | None = None) -> str | None:
    target = node if name is None else _child(node, name)
    if target is None:
        return None
    return _clean("".join(target.itertext()))


def _json(raw: bytes) -> Any:
    if not isinstance(raw, bytes) or not 0 < len(raw) <= 100_000_000:
        raise LobbyingFormatError("input_limit", "export file is missing or oversized")
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LobbyingFormatError(
            "schema_drift", "export is not valid UTF-8 JSON"
        ) from exc


def _csv(raw: bytes, required: Sequence[str]) -> list[dict[str, str]]:
    if not isinstance(raw, bytes) or not 0 < len(raw) <= 100_000_000:
        raise LobbyingFormatError("input_limit", "export file is missing or oversized")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise LobbyingFormatError("schema_drift", "export is not UTF-8 CSV") from exc
    reader = csv.DictReader(io.StringIO(text))
    header = [h.strip() for h in reader.fieldnames or []]
    missing = [column for column in required if column not in header]
    if missing:
        raise LobbyingFormatError("schema_drift", f"CSV export lacks columns {missing}")
    return [
        {(k or "").strip(): (v or "").strip() for k, v in row.items()} for row in reader
    ]


def _entry(register: str, native_id: Any, **fields: Any) -> dict[str, Any]:
    native = _clean(native_id)
    if not native:
        raise LobbyingFormatError(
            "schema_drift", f"{register} entry has no native identifier"
        )
    return {
        "register": register,
        "native_id": native,
        "entry_kind": REGISTERS[register]["kind"],
        **fields,
    }


def _identifier(
    scheme: str, value: Any, *, country: Any = None
) -> dict[str, Any] | None:
    text = _clean(value)
    if not text or scheme not in IDENTIFIER_SCHEMES:
        return None
    return {"scheme": scheme, "value": text, "country": (_clean(country) or None)}


_TR_ID = re.compile(r"(?<!\d)(\d{9,12}-\d{2})(?!\d)")


def parse_eu_tr(raw: bytes) -> dict[str, Any]:
    root = _xml(raw)
    if _tag(root) != "ListOfIRPublicDetail":
        raise LobbyingFormatError(
            "schema_drift", "not an EU Transparency Register export"
        )
    published = _day(root.get("generationDate"))
    if published is None:
        raise LobbyingFormatError("schema_drift", "export has no generation date")
    entries = []
    for node in _children(root, "interestRepresentative"):
        native = _text(node, "identificationCode")
        name_node = _child(node, "name")
        name = _text(name_node, "originalName") if name_node is not None else None
        office = _child(node, "headOffice")
        country = _text(office, "country") if office is not None else None
        identifiers = [
            i
            for i in (
                _identifier("eu-tr", native),
                _identifier("website", _text(node, "webSiteURL")),
            )
            if i
        ]
        holder = _child(node, "identifiers")
        for ident in _children(holder, "identifier") if holder is not None else []:
            item = _identifier(
                str(ident.get("scheme") or ""),
                _text(ident),
                country=ident.get("country") or country,
            )
            if item:
                identifiers.append(item)
        interests = []
        for interest in (
            _children(_child(node, "interests"), "interest")
            if _child(node, "interests") is not None
            else []
        ):
            text = _text(interest, "name") or _text(interest)
            if text:
                interests.append(
                    {"kind": "field_of_interest", "text": text, "references": []}
                )
        files = _child(node, "EULegislativeProposals")
        if files is not None:
            proposals = _children(files, "proposal") or [files]
            for proposal in proposals:
                text = _text(proposal)
                refs = []
                for scheme, attribute in (
                    ("eu-procedure", "procedureReference"),
                    ("celex", "celex"),
                    ("eli", "eli"),
                ):
                    ref = reference(
                        scheme,
                        proposal.get(attribute),
                        source_field=f"EULegislativeProposals@{attribute}",
                    )
                    if ref:
                        refs.append(ref)
                known = {r["key"] for r in refs}
                refs += [
                    r
                    for r in references_in_text(
                        text, source_field="EULegislativeProposals"
                    )
                    if r["key"] not in known
                ]
                if text or refs:
                    interests.append(
                        {"kind": "legislative_file", "text": text, "references": refs}
                    )
        financial = _child(node, "financialData")
        spend, clients, grants = [], [], []
        if financial is not None:
            year = _child(financial, "closedYear")
            start, end = (
                (year.get("start"), year.get("end"))
                if year is not None
                else (None, None)
            )
            for element, kind in (
                ("closedYearCosts", "costs"),
                ("closedYearRevenue", "revenue"),
            ):
                item = _child(financial, element)
                if item is None:
                    continue
                low, high = item.get("min"), item.get("max")
                if low is None and high is None:
                    low, high = parse_range_text(_text(item))
                declared = spend_range(
                    kind,
                    low,
                    high,
                    item.get("currency"),
                    start,
                    end,
                    as_filed=_text(item),
                )
                if declared:
                    spend.append(declared)
            holder = _child(financial, "clients")
            for client in _children(holder, "client") if holder is not None else []:
                revenue = _child(client, "revenue")
                declared = None
                if revenue is not None:
                    low, high = revenue.get("min"), revenue.get("max")
                    if low is None and high is None:
                        low, high = parse_range_text(_text(revenue))
                    declared = spend_range(
                        "client-revenue",
                        low,
                        high,
                        revenue.get("currency"),
                        start,
                        end,
                        as_filed=_text(revenue),
                        party=_clean(client.get("name")),
                    )
                clients.append(
                    {
                        "name": _clean(client.get("name")) or _text(client),
                        "native_ids": [
                            i
                            for i in (
                                _identifier("eu-tr", client.get("identificationCode")),
                            )
                            if i
                        ],
                        "role": "client",
                        "spend": declared,
                    }
                )
                if declared:
                    spend.append(declared)
            holder = _child(financial, "grants")
            for grant in _children(holder, "grant") if holder is not None else []:
                low, high = (
                    grant.get("min") or grant.get("amount"),
                    grant.get("max") or grant.get("amount"),
                )
                grants.append(
                    {
                        "source_text": _clean(grant.get("source")) or _text(grant),
                        "programme": _clean(grant.get("programme")),
                        "amount": spend_range(
                            "grant",
                            low,
                            high,
                            grant.get("currency"),
                            start,
                            end,
                            as_filed=_text(grant) or grant.get("amount"),
                        ),
                        "year": _clean(grant.get("year")),
                        "assertion": "declared by the registrant; not confirmed funding",
                    }
                )
        status = str(_text(node, "status") or "ACTIVE").upper()
        entries.append(
            _entry(
                "eu-tr",
                native,
                name=name,
                legal_form=_text(node, "legalStatus"),
                category=_text(node, "registrationCategory"),
                section=_text(node, "section"),
                country=country,
                address=None
                if office is None
                else {
                    "text": ", ".join(
                        p
                        for p in (
                            _text(office, "address"),
                            _text(office, "postCode"),
                            _text(office, "city"),
                        )
                        if p
                    )
                    or None,
                    "country": country,
                },
                identifiers=identifiers,
                lifecycle="deregistered"
                if status in {"REMOVED", "DEREGISTERED", "SUSPENDED"}
                else "active",
                registered_on=_day(_text(node, "registrationDate")),
                declared_on=_day(_text(node, "lastUpdateDate")),
                native_version=None,
                clients=clients,
                interests=interests,
                spend=spend,
                grants=grants,
                documents=[],
                period=None,
            )
        )
    return {
        "register": "eu-tr",
        "format": "eu-tr-xml",
        "publication_date": published,
        "entries": entries,
    }


def parse_de_lobbyregister(
    raw: bytes, *, documents: str = "link-only"
) -> dict[str, Any]:
    payload = _json(raw)
    if not isinstance(payload, Mapping) or not isinstance(payload.get("results"), list):
        raise LobbyingFormatError("schema_drift", "not a Lobbyregister JSON export")
    published = _day(payload.get("sourceDate"))
    if published is None:
        raise LobbyingFormatError("schema_drift", "export has no source date")
    entries = []
    for item in payload["results"]:
        if not isinstance(item, Mapping):
            raise LobbyingFormatError("schema_drift", "export result is not an object")
        details = dict(item.get("registerEntryDetails") or {})
        identity = dict(item.get("lobbyistIdentity") or {})
        address = dict(identity.get("address") or {})
        country = dict(address.get("country") or {}).get("code")
        legal_form = dict(identity.get("legalForm") or {})
        identifiers = [
            i
            for i in (
                _identifier("de-lobbyregister", item.get("registerNumber")),
                _identifier("eu-tr", identity.get("euTransparencyRegisterId")),
                _identifier("lei", identity.get("lei")),
            )
            if i
        ]
        for register_entry in identity.get("commercialRegisterEntries") or []:
            value = f"{register_entry.get('court') or ''} {register_entry.get('number') or ''}".strip()
            ident = _identifier("de-register", value, country=country or "DE")
            if ident:
                identifiers.append(ident)
        for website in identity.get("websites") or []:
            ident = _identifier("website", website)
            if ident:
                identifiers.append(ident)
        interests = [
            {
                "kind": "field_of_interest",
                "text": _clean(f.get("de") or f.get("en") or f.get("code")),
                "code": f.get("code"),
                "references": [],
            }
            for f in dict(item.get("activitiesAndInterests") or {}).get(
                "fieldsOfInterest"
            )
            or []
            if isinstance(f, Mapping)
        ]
        for project in (
            dict(item.get("regulatoryProjects") or {}).get("regulatoryProjects") or []
        ):
            refs = [
                r
                for r in (
                    reference(
                        "de-regulatory-project",
                        project.get("regulatoryProjectNumber"),
                        source_field="regulatoryProjects.regulatoryProjectNumber",
                    ),
                )
                if r
            ]
            for printed in project.get("printedMatters") or []:
                ref = reference(
                    "de-drucksache",
                    printed.get("printingNumber"),
                    source_field="regulatoryProjects.printedMatters.printingNumber",
                )
                if ref:
                    refs.append({**ref, "title": _clean(printed.get("title"))})
            interests.append(
                {
                    "kind": "regulatory_project",
                    "text": _clean(project.get("title")),
                    "code": project.get("regulatoryProjectNumber"),
                    "references": refs,
                }
            )
        expenses = dict(item.get("financialExpenses") or {})
        euro = dict(expenses.get("financialExpensesEuro") or {})
        spend = [
            s
            for s in (
                spend_range(
                    "expenditure",
                    euro.get("from"),
                    euro.get("to"),
                    "EUR",
                    expenses.get("relatedFiscalYearStart"),
                    expenses.get("relatedFiscalYearEnd"),
                    as_filed=f"{euro.get('from')} - {euro.get('to')} EUR"
                    if euro
                    else None,
                ),
            )
            if s
        ]
        clients = []
        client_identity = dict(item.get("clientIdentity") or {})
        for role, key in (
            ("client", "clientOrganizations"),
            ("principal", "principalOrganizations"),
        ):
            for client in client_identity.get(key) or []:
                clients.append(
                    {
                        "name": _clean(client.get("name")),
                        "native_ids": [
                            i
                            for i in (
                                _identifier(
                                    "de-lobbyregister", client.get("registerNumber")
                                ),
                            )
                            if i
                        ],
                        "role": role,
                        "spend": None,
                    }
                )
        for person in client_identity.get("clientPersons") or []:
            clients.append(
                {
                    "name": _clean(
                        " ".join(
                            p
                            for p in (person.get("firstName"), person.get("lastName"))
                            if p
                        )
                    ),
                    "native_ids": [],
                    "role": "client-person",
                    "spend": None,
                }
            )
        papers = []
        for statement in dict(item.get("statements") or {}).get("statements") or []:
            url = _clean(statement.get("pdfUrl"))
            if url and not url.startswith("https://"):
                url = None
            papers.append(
                {
                    "kind": "position-paper",
                    "number": _clean(statement.get("statementNumber")),
                    "title": _clean(statement.get("title")),
                    "regulatory_project": _clean(
                        statement.get("regulatoryProjectNumber")
                    ),
                    "url": url,
                    "retention": documents,
                    "text": _clean(statement.get("text"))
                    if documents == "text"
                    else None,
                }
            )
        active = details.get("activeLobbyist")
        entries.append(
            _entry(
                "de-lobbyregister",
                item.get("registerNumber"),
                name=_clean(identity.get("name")),
                legal_form=_clean(legal_form.get("de") or legal_form.get("code")),
                category=_clean(
                    dict(item.get("activitiesAndInterests") or {}).get("activity")
                ),
                section=None,
                country=country,
                address={
                    "text": _clean(
                        " ".join(
                            str(address.get(k) or "")
                            for k in ("street", "zipCode", "city")
                        )
                    ),
                    "country": country,
                }
                if address
                else None,
                identifiers=identifiers,
                lifecycle="deregistered" if active is False else "active",
                registered_on=_day(details.get("firstPublicationDate")),
                declared_on=_day(details.get("validFromDate")),
                native_version=None
                if details.get("version") is None
                else str(details.get("version")),
                clients=clients,
                interests=interests,
                spend=spend,
                grants=[],
                documents=papers,
                period=None,
            )
        )
    total = payload.get("totalResultCount")
    # Only an export that states it carries every entry is a full register export; a capped result set is not,
    # so absence from it can never read as a deregistration.
    complete = (
        isinstance(total, int) and not isinstance(total, bool) and total == len(entries)
    )
    return {
        "register": "de-lobbyregister",
        "format": "de-lobbyregister-json",
        "publication_date": published,
        "entries": entries,
        "complete": complete,
    }


def _organisations(text: Any) -> list[dict[str, Any]]:
    """Organisation strings as declared; a TR number written beside one is kept, the string is never dropped."""
    found = []
    for part in [p for p in re.split(r"\s*;\s*", str(text or "")) if p.strip()]:
        match = _TR_ID.search(part)
        name = _clean(
            re.sub(r"\((?:[^()]*?)\b\d{9,12}-\d{2}\b[^()]*\)", "", part)
        ) or _clean(part)
        found.append(
            {
                "name": name,
                "as_declared": _clean(part),
                "register_ids": [{"scheme": "eu-tr", "value": match[1]}]
                if match
                else [],
            }
        )
    return found


def parse_ep_meetings(raw: bytes) -> dict[str, Any]:
    rows = _csv(
        raw,
        [
            "mep_id",
            "mep_name",
            "meeting_date",
            "place",
            "capacity",
            "title",
            "meeting_with",
            "procedure_reference",
            "published_on",
        ],
    )
    entries = []
    published = max(
        (d for d in (_day(r["published_on"]) for r in rows) if d), default=None
    )
    for row in rows:
        when = _day(row["meeting_date"])
        if when is None:
            raise LobbyingFormatError("schema_drift", "meeting row has no full date")
        refs = [
            r
            for r in (
                reference(
                    "eu-procedure",
                    row["procedure_reference"],
                    source_field="procedure_reference",
                ),
            )
            if r
        ]
        native = (
            "ep:"
            + _digest([row["mep_id"], when, row["title"], row["meeting_with"]])[:20]
        )
        entries.append(
            _entry(
                "ep-meetings",
                native,
                official={
                    "id": f"ep-mep:{row['mep_id']}",
                    "name": _clean(row["mep_name"]),
                    "role": _clean(row["capacity"]),
                    "institution": "European Parliament",
                    "committee": _clean(row.get("committee")),
                },
                date=when,
                place=_clean(row["place"]),
                subject=_clean(row["title"]),
                organisations=_organisations(row["meeting_with"]),
                references=refs,
                published_on=_day(row["published_on"]),
                declared_on=_day(row["published_on"]),
                lifecycle="active",
                native_version=None,
            )
        )
    return {
        "register": "ep-meetings",
        "format": "ep-meetings-csv",
        "publication_date": published,
        "entries": entries,
    }


def parse_ec_meetings(raw: bytes) -> dict[str, Any]:
    payload = _json(raw)
    if not isinstance(payload, Mapping) or not isinstance(
        payload.get("meetings"), list
    ):
        raise LobbyingFormatError("schema_drift", "not a Commission meetings export")
    published = _day(payload.get("publicationDate"))
    entries = []
    for meeting in payload["meetings"]:
        official = dict(meeting.get("official") or {})
        when = _day(meeting.get("date"))
        if when is None or not official.get("id"):
            raise LobbyingFormatError(
                "schema_drift", "meeting has no date or official identifier"
            )
        organisations = []
        for org in meeting.get("organisations") or []:
            tr = _clean(org.get("transparencyRegisterId"))
            organisations.append(
                {
                    "name": _clean(org.get("name")),
                    "as_declared": _clean(org.get("name")),
                    "register_ids": [{"scheme": "eu-tr", "value": tr}] if tr else [],
                }
            )
        refs = []
        for item in meeting.get("legislativeFiles") or []:
            ref = reference(
                str(item.get("scheme") or ""),
                item.get("value"),
                source_field="legislativeFiles",
            )
            if ref:
                refs.append(ref)
        entries.append(
            _entry(
                "ec-meetings",
                meeting.get("meetingId"),
                official={
                    "id": f"ec-official:{official['id']}",
                    "name": _clean(official.get("name")),
                    "role": _clean(official.get("role")),
                    "institution": "European Commission",
                    "portfolio": _clean(official.get("portfolio")),
                },
                date=when,
                place=_clean(meeting.get("location")),
                subject=_clean(meeting.get("subject")),
                organisations=organisations,
                references=refs,
                published_on=published,
                declared_on=published,
                lifecycle="active",
                native_version=None,
            )
        )
    return {
        "register": "ec-meetings",
        "format": "ec-meetings-json",
        "publication_date": published,
        "entries": entries,
    }


def _quarter(value: Any) -> tuple[str, str, str] | None:
    match = re.fullmatch(r"(\d{4})\s*-?\s*Q([1-4])", str(value or "").strip(), re.I)
    if not match:
        return None
    year, q = int(match[1]), int(match[2])
    start = date(year, 3 * q - 2, 1)
    end = date(year + 1, 1, 1) if q == 4 else date(year, 3 * q + 1, 1)
    from datetime import timedelta

    return f"{year}-Q{q}", start.isoformat(), (end - timedelta(days=1)).isoformat()


def parse_uk_orcl(raw: bytes) -> dict[str, Any]:
    rows = _csv(
        raw,
        [
            "registrant_id",
            "registrant_name",
            "company_number",
            "address",
            "quarter",
            "client_name",
            "return_submitted_on",
        ],
    )
    returns: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        quarter = _quarter(row["quarter"])
        if quarter is None:
            raise LobbyingFormatError(
                "schema_drift", f"unreadable quarterly return period {row['quarter']!r}"
            )
        key = (row["registrant_id"], quarter[0])
        entry = returns.get(key)
        if entry is None:
            entry = returns[key] = _entry(
                "uk-orcl",
                row["registrant_id"],
                name=_clean(row["registrant_name"]),
                legal_form=None,
                category="consultant lobbyist",
                section=None,
                country="GB",
                address={"text": _clean(row["address"]), "country": "GB"},
                identifiers=[
                    i
                    for i in (
                        _identifier("gb-coh", row["company_number"], country="GB"),
                    )
                    if i
                ],
                lifecycle="active",
                registered_on=None,
                declared_on=_day(row["return_submitted_on"]),
                native_version=quarter[0],
                clients=[],
                interests=[],
                spend=[],
                grants=[],
                documents=[],
                period={"return": quarter[0], "start": quarter[1], "end": quarter[2]},
            )
        client = _clean(row["client_name"])
        if client and client.casefold() not in {"no clients", "none", "nil"}:
            entry["clients"].append(
                {"name": client, "native_ids": [], "role": "client", "spend": None}
            )
    entries = sorted(
        returns.values(), key=lambda e: (e["native_id"], e["native_version"])
    )
    for entry in entries:
        entry["clients"].sort(key=lambda c: c["name"].casefold())
    published = max(
        (e["declared_on"] for e in entries if e["declared_on"]), default=None
    )
    return {
        "register": "uk-orcl",
        "format": "uk-orcl-csv",
        "publication_date": published,
        "entries": entries,
    }


def parse_export(
    format_id: str, raw: bytes, *, documents: str = "link-only"
) -> dict[str, Any]:
    if format_id not in FORMATS:
        raise LobbyingFormatError(
            "schema_drift", f"unknown export format {format_id!r}"
        )
    parsers: dict[str, Callable[[bytes], dict[str, Any]]] = {
        "eu-tr-xml": parse_eu_tr,
        "de-lobbyregister-json": lambda data: parse_de_lobbyregister(
            data, documents=documents
        ),
        "ep-meetings-csv": parse_ep_meetings,
        "ec-meetings-json": parse_ec_meetings,
        "uk-orcl-csv": parse_uk_orcl,
    }
    export = parsers[format_id](raw)
    if export["publication_date"] is None:
        raise LobbyingFormatError("schema_drift", "export states no publication date")
    if len(export["entries"]) > MAX_ENTRIES:
        raise LobbyingFormatError(
            "input_limit", "export has more entries than the parser accepts"
        )
    keys = [(e["native_id"], e.get("native_version")) for e in export["entries"]]
    if len(set(keys)) != len(keys):
        raise LobbyingFormatError("schema_drift", "export repeats a native identifier")
    export.update(
        {
            "contract": EXPORT_CONTRACT,
            "file_sha256": hashlib.sha256(raw).hexdigest(),
            "entries_sha256": _digest(export["entries"]),
            "entry_count": len(export["entries"]),
            "full_export": format_id in FULL_EXPORT_FORMATS
            and export.pop("complete", True),
        }
    )
    return export


def lobbying_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    declared = dict(source.get("lobbying") or {})
    if declared.get("format") not in FORMATS or FORMATS[
        declared["format"]
    ] != declared.get("register"):
        raise SourcePackError(
            "invalid_manifest",
            "lobbying sources declare a matching register and format",
        )
    selection = declared.get("selection")
    if selection is not None:
        ids = list(dict(selection).get("native_ids") or [])
        if not 1 <= len(ids) <= 500 or any(not str(i).strip() for i in ids):
            raise SourcePackError(
                "invalid_manifest",
                "a lobbying selection names 1-500 native identifiers",
            )
    if declared.get("documents", "link-only") not in {"link-only", "text"}:
        raise SourcePackError(
            "invalid_manifest", "document retention is link-only or text"
        )
    return declared


class LobbyingRegisterAdapter:
    """Fetch one register export or meeting list from the declared endpoint and emit every (selected) entry."""

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
        self.declared = lobbying_declaration(self.source)
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
            "lobbying": {
                "register": self.declared["register"],
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
                "lobbying runs fetch the declared export, not ad-hoc queries",
            )

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage, _retry_after_ms

        self._check(request)
        if cursor is not None:
            raise SourcePackError(
                "cursor_drift", "a register export is one file; there is no next page"
            )
        url = self.source["endpoint"]
        host = (urlsplit(url).hostname or "").casefold()
        response = self.transport(
            url=url,
            params={},
            headers={"Accept": "application/xml, application/json, text/csv"},
            timeout=int(self.definition["limits"]["timeout_ms"]) / 1000,
        )
        final_host = (
            urlsplit(str(response.get("final_url") or url)).hostname or ""
        ).casefold()
        if final_host != host:
            raise SourcePackError(
                "network_policy", "register export was served from another host"
            )
        status = int(response.get("status", 200))
        headers = {
            str(k).casefold(): v for k, v in dict(response.get("headers") or {}).items()
        }
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError("response_too_large", "export exceeds its byte limit")
        if status == 429:
            raise SourcePackError(
                "rate_limited",
                "provider quota is temporarily exhausted",
                retry_after_ms=_retry_after_ms(headers.get("retry-after")),
            )
        if status in {401, 403}:
            raise SourcePackError(
                "authentication_failed", f"export download refused (HTTP {status})"
            )
        if status >= 500:
            raise SourcePackError(
                "source_unavailable", f"register provider returned HTTP {status}"
            )
        if status >= 400:
            raise SourcePackError(
                "schema_drift", f"export download returned HTTP {status}"
            )
        try:
            export = parse_export(
                self.declared["format"],
                raw,
                documents=self.declared.get("documents", "link-only"),
            )
        except LobbyingFormatError as exc:
            raise SourcePackError(
                "response_too_large" if exc.code == "input_limit" else "schema_drift",
                f"{exc.code}: {exc}",
            ) from exc
        selection = self.declared.get("selection")
        selected_ids = (
            sorted({str(i) for i in dict(selection or {}).get("native_ids") or []})
            or None
        )
        entries = [
            e
            for e in export["entries"]
            if selected_ids is None or e["native_id"] in selected_ids
        ]
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        if len(entries) > limit:
            # Never a truncated export: missing entries of a full export would read as deregistrations.
            raise SourcePackError(
                "budget_exhausted",
                "export has more entries than the run's result budget",
            )
        header = {
            "contract": EXPORT_CONTRACT,
            "register": export["register"],
            "format": export["format"],
            "publication_date": export["publication_date"],
            "file_sha256": export["file_sha256"],
            "entries_sha256": _digest(entries),
            "entry_count": len(entries),
            "full_export": export["full_export"],
            "selection": selected_ids,
            # Only a fixture transport says so; the runtime's HTTPS transport is live evidence.
            "evidence_origin": "fixture"
            if response.get("origin") == "fixture"
            else "live",
            "url": url,
        }
        records = []
        for entry in entries:
            label = entry.get("name") or entry.get("subject") or "unnamed entry"
            version = (
                f"@{entry['native_version']}" if entry.get("native_version") else ""
            )
            records.append(
                {
                    "id": f"{entry['register']}:{entry['native_id']}{version}",
                    "title": f"{REGISTERS[entry['register']]['label']} {entry['native_id']}{version}: {label}",
                    "url": url,
                    "language": "en",
                    "published_at": export["publication_date"],
                    "content": json.dumps(entry, sort_keys=True, ensure_ascii=False),
                    "lobbying_export": header,
                    "lobbying_entry": entry,
                }
            )
        receipt = {
            "status": status,
            "register": export["register"],
            "publication_date": export["publication_date"],
            "file_sha256": export["file_sha256"],
            "entries": len(entries),
            "evidence_origin": header["evidence_origin"],
            "final_page": True,
        }
        return RuntimePage(tuple(records), None, len(raw), receipt=receipt)


FIXTURE_SECRET = None
ADAPTERS = {CONNECTOR: LobbyingRegisterAdapter}


def fixture_transport(
    pages: Sequence[Mapping[str, Any]],
) -> Callable[..., Mapping[str, Any]]:
    """Replay authored export files keyed by URL path (+ query); responses are marked as fixture evidence."""
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


def replay_native_fixture(
    source: Mapping[str, Any], fixture: Mapping[str, Any]
) -> list[dict[str, Any]]:
    adapter = LobbyingRegisterAdapter(
        source, transport=fixture_transport(list(fixture["native_pages"]))
    )
    page = adapter.fetch_page(
        {
            "operation": min(source["operations"]),
            "parameters": {},
            "limit": int(source["budgets"]["max_results"]),
        },
        cursor=None,
    )
    return [dict(item) for item in page.records]
