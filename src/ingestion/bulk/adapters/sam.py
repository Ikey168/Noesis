"""SAM.gov Contract Opportunities public extract (FA09).

``https://sam.gov/api/prod/fileextractservices/v1/api/download/Contract%20Opportunities/datagov/ContractOpportunitiesFullCSV.csv?privacy=Public``
redirects (303) to a pre-signed S3 URL on ``falextracts.s3.amazonaws.com`` with
the full active-opportunities CSV (~210 MB, Windows-1252 text), refreshed daily.
No key and no API quota. U.S. government data (public domain).

Parameters (at least one): ``naics_prefixes``, ``agencies`` (substring of
Department/Ind.Agency), ``keywords`` (substring of title), ``set_aside``,
``posted_since`` (YYYY-MM-DD), ``active_only`` (default true). Contact names,
emails and phone numbers are not kept; the description text is not kept (the
``link`` points to it).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterator, Mapping

from src.ingestion.bulk.adapters._common import csv_rows, number, text_set
from src.ingestion.bulk.base import BulkAdapter, FileSource, Release, ReleaseFile, TableSpec

URL = ("https://sam.gov/api/prod/fileextractservices/v1/api/download/Contract%20Opportunities/datagov/"
       "ContractOpportunitiesFullCSV.csv?privacy=Public")


class SamOpportunities(BulkAdapter):
    name = "sam-opportunities-extract"
    publisher = "U.S. General Services Administration (SAM.gov)"
    title = "SAM.gov Contract Opportunities public extract"
    description = "Active federal contract opportunities from the daily public CSV extract."
    allowed_hosts = ("sam.gov", "falextracts.s3.amazonaws.com")
    incremental = "release"
    tables = {"opportunities": TableSpec("opportunities", [
        {"name": "notice_id", "type": "string"}, {"name": "title", "type": "string"},
        {"name": "solicitation_number", "type": "string"}, {"name": "agency", "type": "string"},
        {"name": "sub_tier", "type": "string"}, {"name": "office", "type": "string"},
        {"name": "posted_date", "type": "string"}, {"name": "type", "type": "string"},
        {"name": "base_type", "type": "string"}, {"name": "set_aside", "type": "string"},
        {"name": "response_deadline", "type": "string"}, {"name": "naics_code", "type": "string"},
        {"name": "classification_code", "type": "string"}, {"name": "place_of_performance", "type": "json"},
        {"name": "active", "type": "boolean"}, {"name": "award_number", "type": "string"},
        {"name": "award_date", "type": "string"}, {"name": "award_amount", "type": "number"},
        {"name": "awardee", "type": "string"}, {"name": "link", "type": "string"}], primary_key=("notice_id",))}

    def validate_params(self, params: Mapping[str, Any]) -> Dict[str, Any]:
        out = {"naics_prefixes": text_set(params.get("naics_prefixes")),
               "agencies": [a.casefold() for a in text_set(params.get("agencies"))],
               "keywords": [k.casefold() for k in text_set(params.get("keywords"))],
               "set_aside": text_set(params.get("set_aside")),
               "posted_since": params.get("posted_since"), "active_only": params.get("active_only", True) is not False}
        if not any(out[k] for k in ("naics_prefixes", "agencies", "keywords", "set_aside", "posted_since")):
            raise ValueError("give naics_prefixes, agencies, keywords, set_aside or posted_since")
        return out

    def list_release(self, http, params) -> Release:
        # The extract URL redirects to a pre-signed S3 URL that refuses HEAD; date the release by retrieval day.
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return Release("sam.gov", f"opportunities-{today}", [ReleaseFile("ContractOpportunitiesFullCSV.csv", URL, mode="stream")],
                       published_at=today, licence={"id": "public-domain-us-gov", "attribution": "SAM.gov",
                                                     "terms_url": "https://sam.gov/data-services"})

    def _match(self, row: Mapping[str, str], params) -> bool:
        if params["active_only"] and row.get("Active", "").lower() != "yes":
            return False
        if params["naics_prefixes"] and not any(row.get("NaicsCode", "").startswith(p) for p in params["naics_prefixes"]):
            return False
        if params["agencies"] and not any(a in row.get("Department/Ind.Agency", "").casefold() for a in params["agencies"]):
            return False
        if params["keywords"] and not any(k in row.get("Title", "").casefold() for k in params["keywords"]):
            return False
        if params["set_aside"] and row.get("SetASideCode") not in params["set_aside"]:
            return False
        if params["posted_since"] and row.get("PostedDate", "")[:10] < params["posted_since"]:
            return False
        return True

    def process(self, source: FileSource, params) -> Iterator[Any]:
        with source.open_stream() as raw:
            for row in csv_rows(raw, encoding="cp1252"):
                if not self._match(row, params):
                    continue
                yield "opportunities", {
                    "notice_id": row.get("NoticeId"), "title": row.get("Title"), "solicitation_number": row.get("Sol#") or None,
                    "agency": row.get("Department/Ind.Agency"), "sub_tier": row.get("Sub-Tier") or None,
                    "office": row.get("Office") or None, "posted_date": row.get("PostedDate") or None,
                    "type": row.get("Type") or None, "base_type": row.get("BaseType") or None,
                    "set_aside": row.get("SetASideCode") or None, "response_deadline": row.get("ResponseDeadLine") or None,
                    "naics_code": row.get("NaicsCode") or None, "classification_code": row.get("ClassificationCode") or None,
                    "place_of_performance": {k: row.get(c) for k, c in (("city", "PopCity"), ("state", "PopState"),
                                                                         ("zip", "PopZip"), ("country", "PopCountry")) if row.get(c)},
                    "active": row.get("Active", "").lower() == "yes", "award_number": row.get("AwardNumber") or None,
                    "award_date": row.get("AwardDate") or None, "award_amount": number(row.get("Award$")),
                    "awardee": row.get("Awardee") or None, "link": row.get("Link") or None}
