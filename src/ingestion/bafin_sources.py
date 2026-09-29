"""BaFin and Bundesanzeiger notice acquisition for the Market ``bafin-notices`` feature (#2106, BF03-BF06).

One native connector, ``bafin-notices``, fetches the declared documents of a
source one page at a time and parses them into ``noesis-bafin-notice-v1``
records (:mod:`src.domains.market.bafin_notices`). Five documented shapes are
supported. The four CSV exports are read through a **declared column mapping**,
so a changed header is a declaration change, not a code change:

* ``bafin-voting-rights-csv``: the voting-rights database (AnteileInfo) export, one
  row per notification, with the chain of controlled undertakings in the
  notification form's own table layout (``Name | voting rights % | instruments % |
  total %`` per line, top controlling person first);
* ``bafin-dealings-csv``: the managers'-transactions database (DealingsInfo)
  export, with trade rows sharing a notification identifier grouped into one
  notice that keeps each trade and the aggregate as published;
* ``bundesanzeiger-short-positions-csv``: published net short positions;
* ``bafin-company-csv``: the company database (InstInfo), with one row per
  licence grouped by BaFin ID;
* ``bafin-notices-rss``: warnings and measures as RSS 2.0 items.

Bounded coverage (BF01): only notices for the declared issuers (ISINs, or
issuer names when a row states no ISIN), declared BaFin IDs and the declared
publication window are emitted. Every other row is counted in the page receipt
(``out_of_scope``, ``outside_window``) and never dropped silently. A row that
cannot be parsed becomes a ``rejection`` record, which the runtime quarantines.
Rows never inherit values from the row before them. Only trade rows that share
a stated notification identifier are one notice.

Each page's receipt says whether the document is a complete listing for its
scope. Only a complete listing lets the store mark a notice as no longer
listed. RSS feeds are never complete.

``PROVIDER_CONTRACTS`` records the BF01 access decisions
(docs/development/bafin-notices-evidence/source-audit.md); every implemented
source is ``unverified-live`` until a dated live run.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

from src.domains.market.bafin_notices import (
    CONTRACT,
    LEGAL_BASIS,
    PROVIDERS,
    RETENTION_POLICIES,
    SHORT_PUBLICATION_THRESHOLD,
    BafinError,
    decimal_text,
    digest,
    isin_valid,
    iso_day,
    lei_valid,
    normalize_bafin_id,
    normalize_isin,
    normalize_lei,
    party_key,
)
from src.ingestion.source_packs import SourcePackError

ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
CONNECTOR = "bafin-notices"
FORMATS = {
    "bafin-voting-rights-csv": "bafin-voting-rights",
    "bafin-dealings-csv": "bafin-managers-transactions",
    "bundesanzeiger-short-positions-csv": "bundesanzeiger-short-positions",
    "bafin-company-csv": "bafin-company-database",
    "bafin-notices-rss": "bafin-warnings-measures",
}
REQUIRED_COLUMNS = {
    "bafin-voting-rights-csv": ("notifier",),
    "bafin-dealings-csv": ("person", "nature"),
    "bundesanzeiger-short-positions-csv": (
        "holder",
        "isin",
        "position",
        "position_date",
    ),
    "bafin-company-csv": ("bafin_id", "name"),
}
LISTINGS = ("complete", "current", "history", "partial")
MAX_ROWS = 20_000

PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "bafin-voting-rights": {
        "publisher": "Bundesanstalt für Finanzdienstleistungsaufsicht (BaFin), Stimmrechtsmitteilungen (AnteileInfo)",
        "endpoint": "https://portal.mvp.bafin.de/database/AnteileInfo/",
        "format": "bafin-voting-rights-csv",
        "access": "public search with a result-list CSV export (verify parameters, encoding and columns)",
        "identifiers": "notification identifier where exported (verify); otherwise issuer ISIN + notifier + event and "
        "publication dates + stated percentages",
        "corrections": "correction reference or flag with the corrected publication date; supersedes",
        "access_decision": "unverified-live",
        "reason": "fixture-verified parser; no dated live run yet",
    },
    "bafin-managers-transactions": {
        "publisher": "BaFin, Directors' Dealings (DealingsInfo)",
        "endpoint": "https://portal.mvp.bafin.de/database/DealingsInfo/",
        "format": "bafin-dealings-csv",
        "access": "public search with a result-list CSV export (verify)",
        "retention": "listing period limited (verify); retention after removal not confirmed, so the production "
        "declaration withdraws person data when a transaction leaves the listing",
        "access_decision": "unverified-live",
        "reason": "fixture-verified parser; no dated live run yet",
    },
    "bundesanzeiger-short-positions": {
        "publisher": "Bundesanzeiger Verlag, Netto-Leerverkaufspositionen",
        "endpoint": "https://www.bundesanzeiger.de/pub/de/nlp",
        "format": "bundesanzeiger-short-positions-csv",
        "access": "CSV download of current and historical positions; the link may be session-bound (verify); the "
        "runtime keeps no cookies and refuses cross-host redirects, so a live run may fail with schema_drift",
        "access_decision": "unverified-live",
        "reason": "fixture-verified parser; session-bound download not verified",
    },
    "bafin-company-database": {
        "publisher": "BaFin, Unternehmensdatenbank (InstInfo)",
        "endpoint": "https://portal.mvp.bafin.de/database/InstInfo/",
        "format": "bafin-company-csv",
        "access": "public search with a result-list export (verify)",
        "access_decision": "unverified-live",
        "reason": "fixture-verified parser; no dated live run yet",
    },
    "bafin-warnings-measures": {
        "publisher": "BaFin, warnings on unauthorised business and published measures",
        "endpoint": "https://www.bafin.de/",
        "format": "bafin-notices-rss",
        "access": "RSS 2.0 feeds (verify feed URLs); never a complete listing, so no removal is inferred",
        "access_decision": "unverified-live",
        "reason": "fixture-verified parser; no dated live run yet",
    },
    "unternehmensregister": {
        "publisher": "Unternehmensregister (issuer publications)",
        "endpoint": "https://www.unternehmensregister.de/",
        "access_decision": "not-implemented",
        "reason": "no documented machine interface and terms restricting automated retrieval (verify); link-only, "
        "and officially obtained documents are recorded with record_ownership_register_document",
    },
    "news-wires": {
        "publisher": "EQS, dpa-AFX distributions of the same notices",
        "access_decision": "not-implemented",
        "reason": "licensed redistributions are never mirrored (tracker exclusion)",
    },
}
LIVE_VERIFICATION = {
    provider: {
        "status": "unverified-live"
        if contract["access_decision"] == "unverified-live"
        else "not-implemented",
        "note": "no dated live run from this runtime; offline fixtures only"
        if contract["access_decision"] == "unverified-live"
        else contract["reason"],
    }
    for provider, contract in PROVIDER_CONTRACTS.items()
}


class BafinFormatError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _header(value: Any) -> str:
    return " ".join(str(value or "").replace("﻿", "").split()).casefold()


def _cell(value: Any) -> str | None:
    text = " ".join(str(value or "").replace(" ", " ").split(" ")).strip()
    return None if text.casefold() in {"", "-", "–", "n/a", "k.a."} else text


def _citations(texts: Sequence[str]) -> list[dict[str, Any]]:
    """Statutory and EU-act citations through the shared parsers (src.kb.legal_citations)."""
    from src.kb.legal_citations import parse_citations, parse_eu_act_references

    found: list[dict[str, Any]] = []
    for text in texts:
        acts = parse_eu_act_references(text)
        for citation in parse_citations(text):
            if citation["status"] == "no_statute" and acts:
                continue  # "Art. 19" of an EU act is cited with the act below, not as a bare provision
            for provision in citation["provisions"] or [{"path": None}]:
                found.append(
                    {
                        "raw": citation["raw"],
                        "statute": citation["statute"],
                        "path": provision.get("path"),
                        "status": citation["status"],
                    }
                )
        for act in acts:
            article = re.search(r"Art\.\s*(\d+[a-z]?)", text[: act["start"]][-40:])
            found.append(
                {
                    "raw": (f"Art. {article.group(1)} " if article else "")
                    + act["raw"],
                    "celex": act["celex"],
                    "path": f"art{article.group(1)}" if article else None,
                    "status": "resolved",
                }
            )
    unique = {json.dumps(f, sort_keys=True): f for f in found}
    return [unique[k] for k in sorted(unique)]


def _party_kind(value: Any) -> str:
    text = str(value or "").casefold()
    if "natürlich" in text or "natural" in text or text in {"person", "np"}:
        return "natural_person"
    if (
        "juristisch" in text
        or "legal" in text
        or "unternehmen" in text
        or text in {"jp", "company"}
    ):
        return "legal_person"
    return "unknown"


def _flag(value: Any) -> bool | None:
    text = str(value or "").strip().casefold()
    if not text or text in {"-", "n/a"}:
        return None
    return text in {
        "ja",
        "yes",
        "true",
        "1",
        "x",
        "korrektur",
        "correction",
        "amendment",
        "änderung",
    }


# ------------------------------------------------------------------ CSV


def read_csv(
    raw: bytes, declared: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Rows keyed by declared field names (with their 1-based row numbers) and the declared columns absent."""
    if not isinstance(raw, bytes) or not 0 < len(raw) <= 50_000_000:
        raise BafinFormatError("input_limit", "export is missing or oversized")
    encoding = str(declared.get("encoding") or "utf-8")
    try:
        text = raw.decode(encoding)
    except (UnicodeDecodeError, LookupError) as exc:
        raise BafinFormatError(
            "schema_drift", f"export is not {encoding} text"
        ) from exc
    if text.lstrip().startswith("<"):
        raise BafinFormatError(
            "schema_drift", "an HTML or XML page was returned instead of the CSV export"
        )
    reader = csv.reader(
        io.StringIO(text.lstrip("﻿")), delimiter=str(declared.get("delimiter") or ";")
    )
    try:
        header = next(reader)
    except StopIteration as exc:
        raise BafinFormatError("schema_drift", "export has no header row") from exc
    positions = {_header(name): index for index, name in enumerate(header)}
    columns = dict(declared.get("columns") or {})
    mapping = {field: positions.get(_header(name)) for field, name in columns.items()}
    missing = sorted(field for field, index in mapping.items() if index is None)
    rows = []
    for number, cells in enumerate(reader, start=1):
        if not any(c.strip() for c in cells):
            continue
        if number > MAX_ROWS:
            raise BafinFormatError("input_limit", "export exceeds the row bound")
        row: dict[str, Any] = {"_row": number}
        for field, index in mapping.items():
            row[field] = (
                _cell(cells[index])
                if index is not None and index < len(cells)
                else None
            )
        # Multi-line cells (the chain table) keep their line breaks.
        if mapping.get("chain") is not None and mapping["chain"] < len(cells):
            row["chain"] = cells[mapping["chain"]].strip() or None
        rows.append(row)
    return rows, missing


def parse_chain(text: Any, decimal: str | None = ",") -> list[dict[str, Any]]:
    """The notification form's chain table, one member per line, in the stated order."""
    members = []
    for line in str(text or "").replace("\r", "").split("\n"):
        line = line.strip()
        if line in {"", "-", "–"}:
            continue
        parts = [p.strip() for p in line.split("|")]
        name = parts[0]
        if not name:
            raise BafinFormatError("invalid_row", "a chain line names no undertaking")
        values = [
            decimal_text(p, decimal=decimal) if p else None for p in parts[1:4]
        ] + [None, None, None]
        members.append(
            {
                "position": len(members) + 1,
                "name": name,
                "voting_rights_pct": values[0],
                "instruments_pct": values[1],
                "total_pct": values[2],
            }
        )
    return members


class _Scope:
    """The declared bounded issuer set: ISINs, and issuer names for rows that state no valid ISIN."""

    def __init__(self, declared: Mapping[str, Any]) -> None:
        self.issuers = {}
        self.names = {}
        for item in declared.get("issuers") or []:
            isin = normalize_isin(item.get("isin"))
            if not isin_valid(isin):
                raise SourcePackError(
                    "invalid_mapping",
                    f"declared issuer ISIN {isin!r} has invalid check digits",
                )
            self.issuers[isin] = item
            for name in [item.get("name"), *(item.get("aliases") or [])]:
                if name:
                    self.names[party_key(name)] = isin
        # Declared (possibly empty) BaFin IDs bound the company database; undeclared means no bound.
        self.bafin_ids = (
            None
            if "bafin_ids" not in declared
            else {normalize_bafin_id(b) for b in declared.get("bafin_ids") or []}
        )
        self.window_from = iso_day((declared.get("window") or {}).get("from"))

    def issuer(
        self, name: Any, isin_text: Any, lei_text: Any = None
    ) -> tuple[dict[str, Any], str | None, str]:
        """(issuer, bounded ISIN or None, basis)."""
        stated = normalize_isin(isin_text) if isin_text else None
        valid = stated if stated and isin_valid(stated) else None
        issuer: dict[str, Any] = {"name": name}
        if valid:
            issuer["isin"] = valid
        elif isin_text:
            issuer["isin_stated"] = str(isin_text)
        lei = normalize_lei(lei_text) if lei_text else None
        if lei and lei_valid(lei):
            issuer["lei"] = lei
        if valid and valid in self.issuers:
            return issuer, valid, "stated-isin"
        if not valid and name and party_key(name) in self.names:
            isin = self.names[party_key(name)]
            issuer["isin"] = isin
            return issuer, isin, "bounded-set-name"
        return issuer, None, "out-of-scope"

    def listing_scope(self, document: Mapping[str, Any]) -> dict[str, Any]:
        """What a complete document lists: its declared issuers, else every declared issuer."""
        declared = [normalize_isin(i) for i in document.get("issuers") or []]
        return {"issuers": sorted(declared or self.issuers)}

    def in_window(self, day: str | None) -> bool | None:
        if self.window_from is None:
            return True
        if day is None:
            return None
        return day >= self.window_from


def _notice(
    kind: str,
    provider: str,
    source_id: str,
    basis: str,
    *,
    source_url: str | None,
    document: str,
    row: Any,
    file_sha256: str,
    **fields: Any,
) -> dict[str, Any]:
    return {
        "contract": CONTRACT,
        "kind": kind,
        "source": {
            "provider": provider,
            "source_id": source_id,
            "source_id_basis": basis,
            "url": source_url,
            "locator": {"document": document, "row": row},
            "file_sha256": file_sha256,
            "document": document,
        },
        **fields,
    }


def _legal(kind: str) -> list[dict[str, Any]]:
    return _citations(LEGAL_BASIS.get(kind, []))


def parse_voting_rights(
    raw: bytes, declared: Mapping[str, Any], document: Mapping[str, Any]
) -> dict[str, Any]:
    rows, missing = read_csv(raw, declared)
    scope, decimal, sha = _Scope(declared), declared.get("decimal", ","), _sha(raw)
    notices, rejected, seen, counts = (
        [],
        [],
        [],
        {"rows": len(rows), "out_of_scope": 0, "outside_window": 0},
    )
    for row in rows:
        try:
            if not row.get("notifier"):
                raise BafinFormatError("invalid_row", "row names no notifier")
            issuer, isin, basis = scope.issuer(
                row.get("issuer_name"), row.get("issuer_isin"), row.get("issuer_lei")
            )
            if isin is None:
                counts["out_of_scope"] += 1
                continue
            publication = iso_day(row.get("publication_date"))
            event = iso_day(row.get("event_date"))
            percentages = {
                key: decimal_text(row.get(key), decimal=decimal)
                for key in ("s33", "s38_1_1", "s38_1_2", "s38", "s39")
            }
            stated_id = row.get("source_id")
            source_id = (
                stated_id
                or "derived:"
                + digest(
                    [isin, party_key(row["notifier"]), event, publication, percentages]
                )[:24]
            )
            seen.append(source_id)
            if scope.in_window(publication) is False:
                counts["outside_window"] += 1
                continue
            correction = None
            if row.get("correction_of"):
                correction = {"source_id": row["correction_of"]}
            elif _flag(row.get("correction_flag")) or row.get("correction_date"):
                correction = {
                    "publication_date": iso_day(row.get("correction_date")),
                    "stated": row.get("correction_flag") or row.get("correction_date"),
                }
            previous = (
                {"s39": decimal_text(row.get("previous_s39"), decimal=decimal)}
                if row.get("previous_s39")
                else None
            )
            withdrawn = None
            if "withdrawn" in (declared.get("columns") or {}):
                withdrawn = bool(
                    re.search(
                        r"zurückgezogen|withdrawn|widerruf",
                        str(row.get("withdrawn") or ""),
                        re.I,
                    )
                )
            thresholds = re.findall(
                r"\d+(?:[.,]\d+)?", str(row.get("thresholds") or "")
            )
            notices.append(
                _notice(
                    "voting_rights_notification",
                    "bafin-voting-rights",
                    source_id,
                    "stated" if stated_id else "derived",
                    source_url=document.get("url"),
                    document=document["label"],
                    row=row["_row"],
                    file_sha256=sha,
                    issuer=issuer,
                    notifier={
                        "name": row["notifier"],
                        "kind": _party_kind(row.get("notifier_kind")),
                        "seat": row.get("seat"),
                        "country": row.get("country"),
                    },
                    chain=parse_chain(row.get("chain"), decimal),
                    thresholds=[t.replace(",", ".") for t in thresholds],
                    percentages=percentages,
                    previous_percentages=previous,
                    reason=row.get("reason"),
                    event_date=event,
                    notification_date=iso_day(row.get("notification_date")),
                    publication_date=publication,
                    correction_of=correction,
                    withdrawn=withdrawn,
                    legal_basis=_legal("voting_rights_notification"),
                    native={"issuer_isin_basis": basis},
                )
            )
        except (BafinFormatError, BafinError) as exc:
            rejected.append(
                {
                    "row": row["_row"],
                    "source_id": row.get("source_id"),
                    "code": exc.code,
                    "reason": str(exc)[:200],
                }
            )
    return {
        "notices": notices,
        "rejected": rejected,
        "seen": seen,
        "missing_columns": missing,
        "counts": counts,
        "file_sha256": sha,
        "scope": scope.listing_scope(document),
    }


def parse_dealings(
    raw: bytes, declared: Mapping[str, Any], document: Mapping[str, Any]
) -> dict[str, Any]:
    rows, missing = read_csv(raw, declared)
    scope, decimal, sha = _Scope(declared), declared.get("decimal", ","), _sha(raw)
    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    rejected = []
    for row in rows:
        stated = row.get("source_id")
        key = (
            f"stated:{stated}" if stated else f"row:{row['_row']}"
        )  # never merge rows without a stated id
        if key not in groups:
            order.append(key)
        groups.setdefault(key, []).append(row)
    notices, seen, counts = (
        [],
        [],
        {"rows": len(rows), "out_of_scope": 0, "outside_window": 0},
    )
    for key in order:
        group = groups[key]
        first = group[0]
        try:
            for row in group:
                if not row.get("person") or not row.get("nature"):
                    raise BafinFormatError(
                        "invalid_row", "a trade row names no person or no nature"
                    )
            persons = {party_key(r["person"], kind="natural_person") for r in group}
            issuers = {
                (
                    r.get("issuer_isin")
                    or r.get("instrument_isin")
                    or r.get("issuer_name")
                )
                for r in group
            }
            if len(persons) > 1 or len(issuers) > 1:
                raise BafinFormatError(
                    "invalid_row",
                    "rows sharing a notification id name different persons or issuers",
                )
            issuer, isin, basis = scope.issuer(
                first.get("issuer_name"),
                first.get("issuer_isin"),
                first.get("issuer_lei"),
            )
            if isin is None and first.get("instrument_isin"):
                candidate = normalize_isin(first["instrument_isin"])
                if candidate in scope.issuers:
                    issuer, isin, basis = (
                        {**issuer, "isin": candidate},
                        candidate,
                        "instrument-isin",
                    )
            if isin is None:
                counts["out_of_scope"] += 1
                continue
            trade_dates = sorted(
                {d for d in (iso_day(r.get("transaction_date")) for r in group) if d}
            )
            publication = iso_day(first.get("publication_date"))
            trades = [
                {
                    "price": decimal_text(r.get("price"), decimal=decimal),
                    "volume": decimal_text(r.get("volume"), decimal=decimal),
                    "currency": r.get("currency"),
                    "date": iso_day(r.get("transaction_date")),
                    "venue": r.get("venue"),
                    "row": r["_row"],
                }
                for r in group
            ]
            aggregate = None
            if first.get("aggregate_price") or first.get("aggregate_volume"):
                aggregate = {
                    "price": decimal_text(
                        first.get("aggregate_price"), decimal=decimal
                    ),
                    "volume": decimal_text(
                        first.get("aggregate_volume"), decimal=decimal
                    ),
                    "currency": first.get("currency"),
                }
            stated_id = first.get("source_id")
            source_id = (
                stated_id
                or "derived:"
                + digest(
                    [
                        isin,
                        party_key(first["person"], kind="natural_person"),
                        first.get("instrument_isin"),
                        first["nature"],
                        trade_dates,
                        [(t["price"], t["volume"]) for t in trades],
                        publication,
                    ]
                )[:24]
            )
            seen.append(source_id)
            if (
                scope.in_window(
                    publication or (trade_dates[0] if trade_dates else None)
                )
                is False
            ):
                counts["outside_window"] += 1
                continue
            instrument_text = first.get("instrument_isin")
            instrument = {"type": first.get("instrument_type")}
            if instrument_text and isin_valid(instrument_text):
                instrument["isin"] = normalize_isin(instrument_text)
            elif instrument_text:
                instrument["isin_stated"] = instrument_text
            amendment = _flag(first.get("amendment"))
            correction = (
                {"source_id": first["correction_of"]}
                if first.get("correction_of")
                else None
            )
            closely = _flag(first.get("closely_associated"))
            notices.append(
                _notice(
                    "managers_transaction",
                    "bafin-managers-transactions",
                    source_id,
                    "stated" if stated_id else "derived",
                    source_url=document.get("url"),
                    document=document["label"],
                    row=[r["_row"] for r in group],
                    file_sha256=sha,
                    issuer=issuer,
                    person={
                        "name": first["person"],
                        "kind": "legal_person"
                        if _party_kind(first.get("person_kind")) == "legal_person"
                        else "natural_person",
                        "role": first.get("role"),
                        "closely_associated": closely,
                    },
                    instrument=instrument,
                    nature=first["nature"],
                    trades=trades,
                    aggregate=aggregate,
                    venue=first.get("venue")
                    if len({r.get("venue") for r in group}) == 1
                    else None,
                    transaction_date=trade_dates[0] if len(trade_dates) == 1 else None,
                    notification_date=iso_day(first.get("notification_date")),
                    publication_date=publication,
                    amendment=amendment,
                    correction_of=correction,
                    legal_basis=_legal("managers_transaction"),
                    native={"issuer_isin_basis": basis, "trade_dates": trade_dates},
                )
            )
        except (BafinFormatError, BafinError) as exc:
            rejected.append(
                {
                    "row": [r["_row"] for r in group],
                    "source_id": first.get("source_id"),
                    "code": exc.code,
                    "reason": str(exc)[:200],
                }
            )
    return {
        "notices": notices,
        "rejected": rejected,
        "seen": seen,
        "missing_columns": missing,
        "counts": counts,
        "file_sha256": sha,
        "scope": scope.listing_scope(document),
    }


def parse_short_positions(
    raw: bytes, declared: Mapping[str, Any], document: Mapping[str, Any]
) -> dict[str, Any]:
    rows, missing = read_csv(raw, declared)
    scope, decimal, sha = _Scope(declared), declared.get("decimal", ","), _sha(raw)
    notices, rejected, seen = [], [], []
    counts = {"rows": len(rows), "out_of_scope": 0, "outside_window": 0}
    for row in rows:
        try:
            if not (
                row.get("holder") and row.get("position_date") and row.get("position")
            ):
                raise BafinFormatError(
                    "invalid_row", "row states no holder, position or position date"
                )
            issuer, isin, basis = scope.issuer(row.get("issuer_name"), row.get("isin"))
            if isin is None:
                counts["out_of_scope"] += 1
                continue
            position_date = iso_day(row["position_date"])
            position = decimal_text(row["position"], decimal=decimal)
            holder_key = party_key(row["holder"])
            seen.append(f"{holder_key}|{isin}")
            publication = iso_day(row.get("publication_date"))
            if scope.in_window(publication or position_date) is False:
                counts["outside_window"] += 1
                continue
            notices.append(
                _notice(
                    "net_short_position",
                    "bundesanzeiger-short-positions",
                    f"{holder_key}|{isin}|{position_date}",
                    "derived",
                    source_url=document.get("url"),
                    document=document["label"],
                    row=row["_row"],
                    file_sha256=sha,
                    issuer=issuer,
                    holder={
                        "name": row["holder"],
                        "kind": _party_kind(row.get("holder_kind")),
                    },
                    position_pct=position,
                    position_date=position_date,
                    publication_date=publication,
                    publication_ended=Decimal(position) < SHORT_PUBLICATION_THRESHOLD,
                    legal_basis=_legal("net_short_position"),
                    native={"issuer_isin_basis": basis},
                )
            )
        except (BafinFormatError, BafinError) as exc:
            rejected.append(
                {"row": row["_row"], "code": exc.code, "reason": str(exc)[:200]}
            )
    return {
        "notices": notices,
        "rejected": rejected,
        "seen": seen,
        "missing_columns": missing,
        "counts": counts,
        "file_sha256": sha,
        "scope": scope.listing_scope(document),
    }


def parse_company(
    raw: bytes, declared: Mapping[str, Any], document: Mapping[str, Any]
) -> dict[str, Any]:
    rows, missing = read_csv(raw, declared)
    scope, sha = _Scope(declared), _sha(raw)
    groups: dict[str, list[dict[str, Any]]] = {}
    rejected = []
    for row in rows:
        bafin_id = normalize_bafin_id(row.get("bafin_id"))
        if not bafin_id or not row.get("name"):
            rejected.append(
                {
                    "row": row["_row"],
                    "code": "invalid_row",
                    "reason": "row states no BaFin ID or name",
                }
            )
            continue
        groups.setdefault(bafin_id, []).append(row)
    notices, seen = [], []
    counts = {"rows": len(rows), "out_of_scope": 0, "outside_window": 0}
    for bafin_id, group in sorted(groups.items()):
        try:
            if scope.bafin_ids is not None and bafin_id not in scope.bafin_ids:
                counts["out_of_scope"] += 1
                continue
            names = {party_key(r["name"]) for r in group}
            if len(names) > 1:
                raise BafinFormatError(
                    "invalid_row", "rows for one BaFin ID state different names"
                )
            first = group[0]
            seen.append(bafin_id)
            licences = []
            for r in group:
                if r.get("licence"):
                    licences.append(
                        {
                            "type": r["licence"],
                            "start": iso_day(r.get("licence_start")),
                            "end": iso_day(r.get("licence_end")),
                            "status": r.get("licence_status"),
                        }
                    )
            lei = normalize_lei(first.get("lei")) if first.get("lei") else None
            notices.append(
                _notice(
                    "authorised_entity",
                    "bafin-company-database",
                    bafin_id,
                    "stated",
                    source_url=document.get("url"),
                    document=document["label"],
                    row=[r["_row"] for r in group],
                    file_sha256=sha,
                    bafin_id=bafin_id,
                    name=first["name"],
                    place=first.get("place"),
                    country=first.get("country"),
                    lei=lei if lei and lei_valid(lei) else None,
                    entity_type=first.get("entity_type"),
                    licences=sorted(
                        licences, key=lambda item: (item["type"], item["start"] or "")
                    ),
                    native={"lei_stated": first.get("lei")}
                    if lei and not lei_valid(lei)
                    else None,
                )
            )
        except (BafinFormatError, BafinError) as exc:
            rejected.append(
                {
                    "row": [r["_row"] for r in group],
                    "source_id": bafin_id,
                    "code": exc.code,
                    "reason": str(exc)[:200],
                }
            )
    return {
        "notices": notices,
        "rejected": rejected,
        "seen": seen,
        "missing_columns": missing,
        "counts": counts,
        "file_sha256": sha,
        "scope": {"source_ids": sorted(scope.bafin_ids)}
        if scope.bafin_ids is not None
        else {"all": True},
    }


def _xml(raw: bytes):
    import defusedxml.ElementTree as ET

    if not isinstance(raw, bytes) or not 0 < len(raw) <= 20_000_000:
        raise BafinFormatError("input_limit", "feed is missing or oversized")
    try:
        return ET.fromstring(raw, forbid_entities=True, forbid_external=True)
    except Exception as exc:  # noqa: BLE001 - any XML failure is provider drift
        raise BafinFormatError("schema_drift", "feed is not well-formed XML") from exc


def parse_rss(
    raw: bytes, declared: Mapping[str, Any], document: Mapping[str, Any]
) -> dict[str, Any]:
    root = _xml(raw)
    channel = root.find("channel")
    if root.tag != "rss" or channel is None:
        raise BafinFormatError("schema_drift", "feed is not RSS 2.0")
    kind = document.get("kind")
    if kind not in {"bafin_warning", "bafin_measure"}:
        raise SourcePackError(
            "invalid_mapping",
            "RSS documents declare kind bafin_warning or bafin_measure",
        )
    patterns = [re.compile(p) for p in declared.get("title_entity_patterns") or []]
    scope, sha, host = (
        _Scope(declared),
        _sha(raw),
        (urlsplit(document["url"]).hostname or "").casefold(),
    )
    notices, rejected, seen = [], [], []
    items = channel.findall("item")
    counts = {"rows": len(items), "out_of_scope": 0, "outside_window": 0}
    for number, item in enumerate(items, start=1):
        try:

            def text(tag: str) -> str | None:
                node = item.find(tag)
                value = (
                    " ".join("".join(node.itertext()).split())
                    if node is not None
                    else ""
                )
                return value or None

            title = text("title")
            if not title:
                raise BafinFormatError("invalid_row", "item has no title")
            published = iso_day(text("pubDate"))
            link = text("link")
            url = None
            if link:
                parts = urlsplit(link)
                if (parts.hostname or "").casefold() == host:
                    url = (
                        "https://"
                        + parts.netloc
                        + parts.path
                        + ("?" + parts.query if parts.query else "")
                    )
            guid = text("guid")
            source_id = guid or link
            basis = "stated" if source_id else "derived"
            source_id = source_id or "derived:" + digest([title, published])[:24]
            seen.append(source_id)
            if scope.in_window(published) is False:
                counts["outside_window"] += 1
                continue
            names = []
            for pattern in patterns:
                match = pattern.search(title)
                if match and match.groupdict().get("entity"):
                    names.append(match.group("entity").strip().strip("„“\"'").strip())
                    break
            summary = text("description")
            notices.append(
                _notice(
                    kind,
                    "bafin-warnings-measures",
                    source_id,
                    basis,
                    source_url=url,
                    document=document["label"],
                    row=number,
                    file_sha256=sha,
                    title=title,
                    named_entities=names,
                    category=document.get("category"),
                    url=url,
                    summary=summary,
                    publication_date=published,
                    legal_basis=_citations([title, summary or ""]),
                    native={"link": link} if link and url is None else None,
                )
            )
        except (BafinFormatError, BafinError) as exc:
            rejected.append({"row": number, "code": exc.code, "reason": str(exc)[:200]})
    return {
        "notices": notices,
        "rejected": rejected,
        "seen": seen,
        "missing_columns": [],
        "counts": counts,
        "file_sha256": sha,
        "scope": {"all": True},
    }


PARSERS: dict[str, Callable[..., dict[str, Any]]] = {
    "bafin-voting-rights-csv": parse_voting_rights,
    "bafin-dealings-csv": parse_dealings,
    "bundesanzeiger-short-positions-csv": parse_short_positions,
    "bafin-company-csv": parse_company,
    "bafin-notices-rss": parse_rss,
}


def _validated(parsed: dict[str, Any]) -> dict[str, Any]:
    """Validate every notice as the store will; a notice that fails becomes a rejection, never a failed page."""
    from src.domains.market.bafin_notices import validate_notice

    kept = []
    for notice in parsed["notices"]:
        probe = json.loads(json.dumps(notice))
        if probe["kind"] == "managers_transaction":
            person = probe.get("person") or {}
            if not person.get("name"):
                parsed["rejected"].append(
                    {
                        "row": notice["source"]["locator"]["row"],
                        "source_id": notice["source"]["source_id"],
                        "code": "invalid_row",
                        "reason": "the transaction names no person",
                    }
                )
                continue
            person["ref"] = "pending"  # the store moves the name to its person table
            person.pop("name")
        try:
            validate_notice(probe)
        except BafinError as exc:
            parsed["rejected"].append(
                {
                    "row": notice["source"]["locator"]["row"],
                    "source_id": notice["source"]["source_id"],
                    "code": exc.code,
                    "reason": str(exc)[:200],
                }
            )
            continue
        kept.append(notice)
    parsed["notices"] = kept
    return parsed


def parse_document(
    format_id: str, raw: bytes, declared: Mapping[str, Any], document: Mapping[str, Any]
) -> dict[str, Any]:
    if format_id not in PARSERS:
        raise BafinFormatError("schema_drift", f"unsupported format {format_id}")
    parsed = _validated(PARSERS[format_id](raw, declared, document))
    required = [
        c for c in REQUIRED_COLUMNS.get(format_id, ()) if c in parsed["missing_columns"]
    ]
    if required:
        raise BafinFormatError(
            "schema_drift", f"export lacks the declared column(s) {', '.join(required)}"
        )
    return parsed


# ------------------------------------------------------------------ runtime adapter


def bafin_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    declared = dict(source.get("bafin") or {})
    fmt = declared.get("format")
    if fmt not in FORMATS or declared.get("provider") != FORMATS[fmt]:
        raise SourcePackError(
            "invalid_mapping",
            "BaFin sources declare a supported format and its provider",
        )
    if declared.get("retention_policy", "retain") not in RETENTION_POLICIES:
        raise SourcePackError(
            "invalid_mapping", f"retention_policy is one of {RETENTION_POLICIES}"
        )
    host = (urlsplit(source["endpoint"]).hostname or "").casefold()
    documents = list(declared.get("documents") or [])
    if not documents:
        raise SourcePackError(
            "invalid_mapping", "BaFin sources declare their documents"
        )
    for document in documents:
        parts = urlsplit(str(document.get("url") or ""))
        if parts.scheme != "https" or (parts.hostname or "").casefold() != host:
            raise SourcePackError(
                "invalid_mapping", "BaFin documents are HTTPS URLs on the endpoint host"
            )
        if document.get("listing", "partial") not in LISTINGS or not document.get(
            "label"
        ):
            raise SourcePackError(
                "invalid_mapping",
                f"documents carry a label and a listing in {LISTINGS}",
            )
        if (
            fmt == "bafin-notices-rss"
            and document.get("listing", "partial") != "partial"
        ):
            raise SourcePackError(
                "invalid_mapping", "an RSS feed is never a complete listing"
            )
    if declared["provider"] not in PROVIDERS:
        raise SourcePackError("invalid_mapping", "unknown BaFin provider")
    _Scope(declared)  # validates the declared ISINs
    return declared


class BafinNoticeAdapter:
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
        self.declared = bafin_declaration(self.source)
        if transport is None:
            from functools import partial

            # The runtime's default transport: same-host public redirects only, a byte ceiling, the timeout.
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
            "bafin": {
                "provider": self.declared["provider"],
                "format": self.declared["format"],
                "documents": [d["label"] for d in self.declared["documents"]],
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
                "BaFin runs fetch the declared documents, not ad-hoc queries",
            )

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage, _retry_after_ms

        self._check(request)
        documents = self.declared["documents"]
        try:
            index = 0 if cursor is None else int(cursor)
        except ValueError as exc:
            raise SourcePackError(
                "cursor_drift", "cursor is not a document index"
            ) from exc
        if not 0 <= index < len(documents):
            raise SourcePackError(
                "cursor_drift", "cursor is outside the declared documents"
            )
        document = documents[index]
        url = document["url"]
        host = (urlsplit(url).hostname or "").casefold()
        accept = (
            "application/rss+xml, application/xml"
            if self.declared["format"] == "bafin-notices-rss"
            else ("text/csv, text/plain")
        )
        response = self.transport(
            url=url,
            params={},
            headers={"Accept": accept},
            timeout=int(self.definition["limits"]["timeout_ms"]) / 1000,
        )
        final_host = (
            urlsplit(str(response.get("final_url") or url)).hostname or ""
        ).casefold()
        if final_host != host:
            raise SourcePackError(
                "network_policy", "BaFin document was served from another host"
            )
        status = int(response.get("status", 200))
        headers = {
            str(k).casefold(): v for k, v in dict(response.get("headers") or {}).items()
        }
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError(
                "response_too_large", "document exceeds its byte limit"
            )
        if status == 429:
            raise SourcePackError(
                "rate_limited",
                "provider quota is temporarily exhausted",
                retry_after_ms=_retry_after_ms(headers.get("retry-after")),
            )
        if status in {401, 403}:
            raise SourcePackError(
                "authentication_failed", f"document refused (HTTP {status})"
            )
        if status >= 500:
            raise SourcePackError(
                "source_unavailable", f"provider returned HTTP {status}"
            )
        if status >= 400:
            raise SourcePackError("schema_drift", f"document returned HTTP {status}")
        try:
            parsed = parse_document(
                self.declared["format"], raw, self.declared, document
            )
        except BafinFormatError as exc:
            raise SourcePackError(
                "response_too_large" if exc.code == "input_limit" else "schema_drift",
                f"{exc.code}: {exc}",
            ) from exc
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        if len(parsed["notices"]) > limit:
            # A truncated listing would read as removals; the page fails instead.
            raise SourcePackError(
                "budget_exhausted",
                "document has more notices than the run's result budget",
            )
        records = []
        for notice in parsed["notices"]:
            label = (
                notice.get("title")
                or (notice.get("issuer") or {}).get("name")
                or notice.get("name")
            )
            records.append(
                {
                    "id": f"{self.declared['provider']}:{notice['source']['source_id']}",
                    "title": f"{notice['kind']}: {label or notice['source']['source_id']}"[
                        :300
                    ],
                    "url": notice["source"].get("url") or url,
                    "language": "de",
                    "published_at": notice.get("publication_date"),
                    "content": json.dumps(notice, sort_keys=True, ensure_ascii=False),
                    "bafin_notice": notice,
                }
            )
        for item in parsed["rejected"]:
            records.append(
                {
                    "id": f"{self.declared['provider']}:rejected:{document['label']}:{item['row']}",
                    "url": url,
                    "rejection": {
                        "code": item["code"],
                        "reason": item["reason"],
                        "row": item["row"],
                    },
                }
            )
        listing_kind = document.get("listing", "partial")
        unidentified = [r for r in parsed["rejected"] if not r.get("source_id")]
        receipt = {
            "status": status,
            "document": document["label"],
            "file_sha256": parsed["file_sha256"],
            "source_as_of": document.get("source_as_of"),
            "counts": {
                **parsed["counts"],
                "notices": len(parsed["notices"]),
                "rejected": len(parsed["rejected"]),
            },
            "missing_columns": parsed["missing_columns"],
            "listing": {
                # A row rejected without a stated identifier could be any notice, so the listing cannot prove
                # that anything left it; rejected rows that state their identifier still count as listed.
                "complete": listing_kind in {"complete", "current"}
                and not unidentified,
                "kind": listing_kind,
                "scope": parsed["scope"],
                "seen": sorted(
                    set(parsed["seen"])
                    | {r["source_id"] for r in parsed["rejected"] if r.get("source_id")}
                ),
                **(
                    {
                        "incomplete_reason": f"{len(unidentified)} row(s) rejected without a stated identifier"
                    }
                    if unidentified and listing_kind in {"complete", "current"}
                    else {}
                ),
            },
            "final_page": index + 1 >= len(documents),
        }
        next_cursor = str(index + 1) if index + 1 < len(documents) else None
        return RuntimePage(tuple(records), next_cursor, len(raw), receipt=receipt)


FIXTURE_SECRET = None
ADAPTERS = {CONNECTOR: BafinNoticeAdapter}


def fixture_transport(
    pages: Sequence[Mapping[str, Any]],
) -> Callable[..., Mapping[str, Any]]:
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
        if page.get("body_encoding") == "windows-1252":
            content = str(body).encode("windows-1252")
        else:
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
            **({"final_url": page["final_url"]} if page.get("final_url") else {}),
        }

    return transport


def replay_native_fixture(
    source: Mapping[str, Any], fixture: Mapping[str, Any]
) -> list[dict[str, Any]]:
    adapter = BafinNoticeAdapter(
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
        records.extend(dict(item) for item in page.records)
        cursor = page.next_cursor
        if cursor is None:
            return records
