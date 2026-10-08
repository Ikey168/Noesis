"""Companies House free company data product (FA07).

The monthly snapshot of live companies is published as ZIP files with one CSV
each at https://download.companieshouse.gov.uk/en_output.html
(``BasicCompanyData-YYYY-MM-DD-partN_M.zip``, about 70 MB each), "provided free
of charge" and updated within five working days of month end. Header names
carry stray spaces and dates are dd/mm/yyyy; both are normalised here.

Parameters (at least one filter, or ``all_companies=true``):
``company_numbers`` (list), ``postcode_prefix``, ``sic_prefix`` (e.g. "62"),
``status`` (e.g. "Active"), ``incorporated_since`` (YYYY-MM-DD).
Licence: not stated on the product page (_verify_); Companies House data are
generally published under the Open Government Licence.
"""
from __future__ import annotations

import csv
import io
import re
import zipfile
from datetime import datetime
from typing import Any, Dict, Iterator, List, Mapping, Optional

from src.ingestion.bulk.base import BulkAdapter, FileSource, Release, ReleaseFile, TableSpec

PAGE = "https://download.companieshouse.gov.uk/en_output.html"
BASE = "https://download.companieshouse.gov.uk/"
_PART = re.compile(r"BasicCompanyData-(\d{4}-\d{2}-\d{2})-part(\d+)_(\d+)\.zip")


def _date(value: str) -> Optional[str]:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%d/%m/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return None


class CompaniesHouseSnapshot(BulkAdapter):
    name = "companies-house-snapshot"
    publisher = "Companies House"
    title = "Companies House free company data product"
    description = "Monthly snapshot of live UK companies, filtered to declared companies or criteria."
    allowed_hosts = ("download.companieshouse.gov.uk",)
    output = "rows"
    incremental = "release"
    tables = {
        "companies": TableSpec("companies", [
            {"name": "company_number", "type": "string"}, {"name": "company_name", "type": "string"},
            {"name": "company_status", "type": "string"}, {"name": "company_category", "type": "string"},
            {"name": "country_of_origin", "type": "string"}, {"name": "incorporation_date", "type": "date"},
            {"name": "dissolution_date", "type": "date"}, {"name": "post_town", "type": "string"},
            {"name": "postcode", "type": "string"}, {"name": "country", "type": "string"},
            {"name": "sic_codes", "type": "json"}, {"name": "accounts_category", "type": "string"},
            {"name": "previous_names", "type": "json"}, {"name": "uri", "type": "string"}],
            primary_key=("company_number",)),
    }

    def validate_params(self, params: Mapping[str, Any]) -> Dict[str, Any]:
        numbers = sorted({str(n).strip().upper() for n in params.get("company_numbers") or [] if str(n).strip()})
        out = {"company_numbers": numbers,
               "postcode_prefix": str(params.get("postcode_prefix") or "").replace(" ", "").upper(),
               "sic_prefix": str(params.get("sic_prefix") or "").strip(),
               "status": str(params.get("status") or "").strip(),
               "incorporated_since": params.get("incorporated_since"),
               "all_companies": bool(params.get("all_companies"))}
        if out["incorporated_since"]:
            datetime.strptime(out["incorporated_since"], "%Y-%m-%d")
        if not (numbers or out["postcode_prefix"] or out["sic_prefix"] or out["status"]
                or out["incorporated_since"] or out["all_companies"]):
            raise ValueError("give at least one filter (company_numbers, postcode_prefix, sic_prefix, "
                             "status, incorporated_since) or all_companies=true")
        return out

    def list_release(self, http, params) -> Release:
        page = http.get_text(PAGE)
        parts = sorted({m.groups() for m in _PART.finditer(page)}, key=lambda g: (g[0], int(g[1])))
        if not parts:
            raise ValueError("no BasicCompanyData parts found on the product page")
        latest = max(p[0] for p in parts)
        files = []
        for date, part, total in (p for p in parts if p[0] == latest):
            name = f"BasicCompanyData-{date}-part{part}_{total}.zip"
            head = http.head(BASE + name)
            files.append(ReleaseFile(name=name, url=BASE + name, mode="download",
                                     size=int(head["content-length"]) if head.get("content-length") else None,
                                     etag=head.get("etag"), last_modified=head.get("last-modified")))
        return Release(provider="companies-house", release_id=latest, files=files, published_at=latest,
                       licence={"id": "companies-house-free-data-product", "terms_url": PAGE,
                                "note": "provided free of charge; licence not stated on the product page (verify; "
                                        "Companies House data are generally under the Open Government Licence)"})

    def _match(self, row: Mapping[str, str], params) -> bool:
        if params["all_companies"]:
            return True
        if params["company_numbers"] and row.get("CompanyNumber", "").upper() not in params["company_numbers"]:
            return False
        if params["postcode_prefix"] and not row.get("RegAddress.PostCode", "").replace(" ", "").upper().startswith(params["postcode_prefix"]):
            return False
        if params["status"] and row.get("CompanyStatus", "") != params["status"]:
            return False
        if params["sic_prefix"]:
            codes = [row.get(f"SICCode.SicText_{i}", "") for i in range(1, 5)]
            if not any(c.startswith(params["sic_prefix"]) for c in codes if c):
                return False
        if params["incorporated_since"]:
            inc = _date(row.get("IncorporationDate", ""))
            if not inc or inc < params["incorporated_since"]:
                return False
        return True

    def process(self, source: FileSource, params) -> Iterator[Any]:
        with zipfile.ZipFile(source.path) as archive:
            for member in archive.namelist():
                if not member.lower().endswith(".csv"):
                    continue
                with archive.open(member) as handle:
                    reader = csv.reader(io.TextIOWrapper(handle, encoding="utf-8", errors="replace", newline=""))
                    header = [h.strip() for h in next(reader)]
                    for values in reader:
                        row = dict(zip(header, values))
                        if not self._match(row, params):
                            continue
                        sic = [row.get(f"SICCode.SicText_{i}", "").strip() for i in range(1, 5)]
                        previous: List[Dict[str, Optional[str]]] = []
                        for i in range(1, 11):
                            prev = row.get(f"PreviousName_{i}.CompanyName", "").strip()
                            if prev:
                                previous.append({"name": prev, "changed": _date(row.get(f"PreviousName_{i}.CONDATE", ""))})
                        yield "companies", {
                            "company_number": row.get("CompanyNumber", "").strip(),
                            "company_name": row.get("CompanyName", "").strip(),
                            "company_status": row.get("CompanyStatus") or None,
                            "company_category": row.get("CompanyCategory") or None,
                            "country_of_origin": row.get("CountryOfOrigin") or None,
                            "incorporation_date": _date(row.get("IncorporationDate", "")),
                            "dissolution_date": _date(row.get("DissolutionDate", "")),
                            "post_town": row.get("RegAddress.PostTown") or None,
                            "postcode": row.get("RegAddress.PostCode") or None,
                            "country": row.get("RegAddress.Country") or None,
                            "sic_codes": [c for c in sic if c],
                            "accounts_category": row.get("Accounts.AccountCategory") or None,
                            "previous_names": previous,
                            "uri": row.get("URI") or None,
                        }
