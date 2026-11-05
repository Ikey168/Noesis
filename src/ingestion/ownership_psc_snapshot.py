"""Companies House PSC snapshot lines as ownership records (#2885).

The daily *People with significant control* snapshot is a set of ZIP files of
JSON lines, each ``{"company_number": ..., "data": {...}}`` where ``data`` is
the same item the public data API returns for a PSC, a PSC statement or a
company's exemptions (a final ``totals#...`` line summarises the file). Each
line is mapped by the *same* parsers as the API path
(:func:`~src.ingestion.ownership_providers.parse_ch_psc`,
``parse_ch_psc_statements``, ``parse_ch_exemptions``), so a snapshot entry and
an API response produce the same statements, natures, bands, validity and
person handling. Only the channel differs:

* record keys of statements and of PSC-specific holders are namespaced
  ``companies-house:psc-snapshot...`` so each channel keeps its own revision
  chain; company identity stays shared (``companies-house:gb-coh:<number>``),
  so corporate PSCs resolve into the ownership graph by company number;
* the source names the snapshot (``source_type`` ``bulk-snapshot``) and the
  SHA-256 of the canonical line, never the file or line position, so an
  unchanged entry in a later snapshot is not a new revision; a ceased or
  changed entry is.

Natural persons follow person-research mode (``NOESIS_PERSON_RESEARCH``): by
default they are owner-scoped, with no date of birth and no address. Entries
absent from a later snapshot are never deleted.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from src.ingestion.ownership_providers import (
    _ch_company,
    parse_ch_exemptions,
    parse_ch_psc,
    parse_ch_psc_statements,
)
from src.ingestion.source_packs import SourcePackError
from src.kb.ownership_records import OwnershipRecordError, canonical, validate_record

SOURCE_TYPE = "bulk-snapshot"
LICENCE = "Companies House public data (PSC snapshot; reuse terms per the audit, verify)"
STATEMENT_KINDS = frozenset({"persons-with-significant-control-statement"})
TOTALS_PREFIX = "totals#"
# API-path key prefix -> snapshot key prefix. Company identity (gb-coh) is shared on purpose.
_PREFIXES = (
    ("companies-house:psc-statement:", "companies-house:psc-snapshot-statement:"),
    ("companies-house:exemption:", "companies-house:psc-snapshot-exemption:"),
    ("companies-house:psc:", "companies-house:psc-snapshot:"),
)


def _rekey(key: str | None) -> str | None:
    if not key:
        return key
    for old, new in _PREFIXES:
        if key.startswith(old):
            return new + key[len(old):]
    return key


def line_kind(line: Mapping[str, Any]) -> str:
    data = line.get("data")
    if not isinstance(data, Mapping):
        return "malformed"
    kind = str(data.get("kind") or "")
    if kind.startswith(TOTALS_PREFIX):
        return "totals"
    if kind in STATEMENT_KINDS or (data.get("statement") and "natures_of_control" not in data):
        return "statement"
    if kind == "exemptions" or isinstance(data.get("exemptions"), Mapping):
        return "exemptions"
    if kind.startswith("super-secure") or "natures_of_control" in data:
        return "psc"
    return "unrecognised"


def parse_snapshot_line(line: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One snapshot line -> ownership records (empty for the totals line)."""
    kind = line_kind(line)
    if kind == "totals":
        return []
    if kind in {"malformed", "unrecognised"}:
        raise SourcePackError("schema_drift", f"PSC snapshot line is {kind}")
    company = _ch_company(str(line.get("company_number") or "").strip().upper())
    data = dict(line["data"])
    raw_sha = hashlib.sha256(canonical(dict(line)).encode()).hexdigest()
    if kind == "statement":
        records = parse_ch_psc_statements({"items": [data]}, company=company, raw_sha=raw_sha)
    elif kind == "exemptions":
        records = parse_ch_exemptions(data, company=company, raw_sha=raw_sha)
    else:
        records = parse_ch_psc({"items": [data]}, company=company, raw_sha=raw_sha)
    return [_as_snapshot(item, company=company, raw_sha=raw_sha) for item in records]


def _as_snapshot(item: dict[str, Any], *, company: str, raw_sha: str) -> dict[str, Any]:
    body = json.loads(json.dumps(item))
    body.pop("unknowns", None)
    body["record_key"] = _rekey(body["record_key"])
    if body.get("holder"):
        body["holder"] = {**body["holder"], "key": _rekey(body["holder"].get("key"))}
    locator = dict(body["source"].get("locator") or {})
    if "json_pointer" in locator:
        pointer = str(locator["json_pointer"])
        for prefix in ("/items/0", "/exemptions"):
            if pointer.startswith(prefix):
                pointer = "/data" + (pointer[len("/items/0"):] if prefix == "/items/0" else pointer)
                break
        locator["json_pointer"] = pointer
    part = str(body["source"]["provider_record_id"]).split(":", 1)[-1]
    body["source"] = {
        "provider": "companies-house", "provider_record_id": f"{company}:{part}-snapshot",
        "url": f"https://find-and-update.company-information.service.gov.uk/company/{company}/"
               "persons-with-significant-control",
        "raw_sha256": raw_sha, "locator": locator, "license": LICENCE, "source_type": SOURCE_TYPE,
        "publisher": "Companies House",
        "note": "Companies House People with significant control snapshot (daily bulk data product)",
    }
    try:
        return validate_record(body)
    except OwnershipRecordError as exc:
        raise SourcePackError("mapping_failed", str(exc)) from exc
