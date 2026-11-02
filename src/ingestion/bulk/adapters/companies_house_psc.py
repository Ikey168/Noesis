"""Companies House People with significant control snapshot (#2885).

The daily PSC snapshot is published at
https://download.companieshouse.gov.uk/en_pscdata.html as one ZIP or as split
parts (``psc-snapshot-YYYY-MM-DD_NofM.zip``; single file
``persons-with-significant-control-snapshot-YYYY-MM-DD.zip``), each holding
JSON lines ``{"company_number": ..., "data": {...}}``. The file layout is taken
from the product page and secondary descriptions and is unverified from this
runtime (_verify_). "Provided free of charge and not supported"; reuse terms
and the personal-data assessment are in
``docs/development/ownership-register-audit.md``.

Parameters (a selection is required; the whole register is never kept):
``company_numbers`` (list of eight-character numbers, at most 10,000) and/or
``company_prefix`` (two letters, e.g. ``OE`` for the Register of Overseas
Entities or ``SC`` for Scotland); optional ``kinds`` drawn from ``psc``,
``statement``, ``exemptions``.

Rows go to the ``ownership_records`` table: each row is one
``noesis-ownership-record-v1`` record produced by the same parsers as the API
path (:mod:`src.ingestion.ownership_psc_snapshot`). The ownership sink
(``--sink ownership``) applies them to the ownership store, where an unchanged
entry is not a new revision and a ceased or changed entry is.
"""
from __future__ import annotations

import io
import json
import re
import zipfile
from collections.abc import Iterator, Mapping
from typing import Any, ClassVar

from src.ingestion.bulk.base import (
    BulkAdapter,
    FileSource,
    Release,
    ReleaseFile,
    TableSpec,
)

PAGE = "https://download.companieshouse.gov.uk/en_pscdata.html"
BASE = "https://download.companieshouse.gov.uk/"
_PART = re.compile(r"psc-snapshot-(\d{4}-\d{2}-\d{2})_(\d+)of(\d+)\.zip")
_WHOLE = re.compile(r"persons-with-significant-control-snapshot-(\d{4}-\d{2}-\d{2})\.zip")
_NUMBER = re.compile(r"[A-Z0-9]{8}")
KINDS = ("psc", "statement", "exemptions")
MAX_NUMBERS = 10_000
LICENCE = {"id": "companies-house-psc-snapshot", "terms_url": PAGE,
           "note": "provided free of charge and not supported; reuse under the Companies House public task and "
                   "Crown copyright statement (OGL status conflicting, verify); personal data published under the "
                   "Companies Act 2006, reuse subject to UK GDPR; see docs/development/ownership-register-audit.md"}


class CompaniesHousePscSnapshot(BulkAdapter):
    name = "companies-house-psc-snapshot"
    publisher = "Companies House"
    title = "Companies House People with significant control snapshot"
    description = "Daily PSC snapshot, filtered to declared company numbers or a company-number prefix, as ownership records."
    allowed_hosts = ("download.companieshouse.gov.uk",)
    output = "rows"
    incremental = "release"
    tables: ClassVar[dict[str, TableSpec]] = {
        "ownership_records": TableSpec("ownership_records", [
            {"name": "record_key", "type": "string"}, {"name": "kind", "type": "string"},
            {"name": "company_number", "type": "string"}, {"name": "line_kind", "type": "string"},
            {"name": "record", "type": "json"}], primary_key=("record_key",)),
        "unrecognised_lines": TableSpec("unrecognised_lines", [
            {"name": "company_number", "type": "string"}, {"name": "line_kind", "type": "string"},
            {"name": "error", "type": "string"}]),
    }

    def validate_params(self, params: Mapping[str, Any]) -> dict[str, Any]:
        numbers = sorted({str(n).strip().upper() for n in params.get("company_numbers") or [] if str(n).strip()})
        prefix = str(params.get("company_prefix") or "").strip().upper()
        kinds = sorted(set(params.get("kinds") or KINDS))
        if any(not _NUMBER.fullmatch(n) for n in numbers) or len(numbers) > MAX_NUMBERS:
            raise ValueError(f"company_numbers are eight-character company numbers (at most {MAX_NUMBERS})")
        if prefix and not re.fullmatch(r"[A-Z]{2}", prefix):
            raise ValueError("company_prefix is two letters (e.g. OE, SC, NI)")
        if set(kinds) - set(KINDS):
            raise ValueError(f"kinds are drawn from {KINDS}")
        if not numbers and not prefix:
            raise ValueError("select company_numbers or a company_prefix; the whole PSC register is never kept")
        return {"company_numbers": numbers, "company_prefix": prefix, "kinds": kinds}

    def list_release(self, http, params) -> Release:
        page = http.get_text(PAGE)
        parts = sorted({m.groups() for m in _PART.finditer(page)}, key=lambda g: (g[0], int(g[1])))
        wholes = sorted({m.group(1) for m in _WHOLE.finditer(page)})
        latest = max([p[0] for p in parts] + wholes, default=None)
        if latest is None:
            raise ValueError("no PSC snapshot files found on the product page")
        names = ([f"psc-snapshot-{d}_{n}of{t}.zip" for d, n, t in parts if d == latest]
                 or [f"persons-with-significant-control-snapshot-{latest}.zip"])
        files = []
        for name in names:
            head = http.head(BASE + name)
            files.append(ReleaseFile(name=name, url=BASE + name, mode="download",
                                     size=int(head["content-length"]) if head.get("content-length") else None,
                                     etag=head.get("etag"), last_modified=head.get("last-modified")))
        return Release(provider="companies-house", release_id=latest, files=files, published_at=latest,
                       licence=LICENCE, metadata={"page": PAGE})

    def _selected(self, company: str, params: Mapping[str, Any]) -> bool:
        if params["company_numbers"] and company in params["company_numbers"]:
            return True
        return bool(params["company_prefix"]) and company.startswith(params["company_prefix"])

    def process(self, source: FileSource, params) -> Iterator[Any]:
        from src.ingestion.ownership_psc_snapshot import line_kind, parse_snapshot_line
        from src.ingestion.source_packs import SourcePackError

        with zipfile.ZipFile(source.path) as archive:
            for member in archive.namelist():
                if member.endswith("/"):
                    continue
                with archive.open(member) as handle:
                    for raw in io.TextIOWrapper(handle, encoding="utf-8", errors="strict"):
                        if not raw.strip():
                            continue
                        try:
                            line = json.loads(raw)
                        except ValueError:
                            yield "unrecognised_lines", {"company_number": None, "line_kind": "malformed",
                                                         "error": "not JSON"}
                            continue
                        company = str(line.get("company_number") or "").strip().upper()
                        kind = line_kind(line)
                        if kind == "totals" or not self._selected(company, params):
                            continue
                        if kind in KINDS and kind not in params["kinds"]:
                            continue
                        try:
                            records = parse_snapshot_line(line)
                        except SourcePackError as exc:
                            yield "unrecognised_lines", {"company_number": company, "line_kind": kind,
                                                         "error": f"{exc.code}: {exc}"[:300]}
                            continue
                        for record in records:
                            yield "ownership_records", {"record_key": record["record_key"], "kind": record["kind"],
                                                        "company_number": company, "line_kind": kind,
                                                        "record": record}
