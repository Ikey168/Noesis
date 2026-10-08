"""USDA FAS PSD Online download (FA06): alternative to the FAS PSD API.

``https://apps.fas.usda.gov/psdonline/downloads/psd_alldata_csv.zip`` (~10 MB)
holds ``psd_alldata.csv``: Commodity_Code, Commodity_Description,
Country_Code, Country_Name, Market_Year, Calendar_Year, Month, Attribute_ID,
Attribute_Description, Unit_ID, Unit_Description, Value. ETag/Last-Modified are
sent. USDA data are U.S. government works (public domain).

Parameters: ``commodity_codes`` and/or ``commodities`` (descriptions), or
``all_commodities=true``; ``countries`` (codes, e.g. "US", "BR"); ``attributes``
(descriptions, e.g. "Production"); ``since_market_year``.
"""
from __future__ import annotations

from typing import Any, Dict, Iterator, Mapping

from src.ingestion.bulk.adapters._common import csv_rows, head_file, iso, newest, number, stamp, text_set, zip_members
from src.ingestion.bulk.base import BulkAdapter, FileSource, Release, TableSpec

URL = "https://apps.fas.usda.gov/psdonline/downloads/psd_alldata_csv.zip"


class FasPsd(BulkAdapter):
    name = "fas-psd"
    publisher = "USDA Foreign Agricultural Service"
    title = "Production, Supply and Distribution (PSD Online)"
    description = "PSD balances by commodity, country, market year and attribute."
    allowed_hosts = ("apps.fas.usda.gov",)
    tables = {"balances": TableSpec("balances", [
        {"name": "commodity_code", "type": "string"}, {"name": "commodity", "type": "string"},
        {"name": "country_code", "type": "string"}, {"name": "country", "type": "string"},
        {"name": "market_year", "type": "integer"}, {"name": "calendar_year", "type": "integer"},
        {"name": "month", "type": "integer"}, {"name": "attribute_id", "type": "string"},
        {"name": "attribute", "type": "string"}, {"name": "unit", "type": "string"}, {"name": "value", "type": "number"}],
        primary_key=("commodity_code", "country_code", "market_year", "attribute_id"))}

    def validate_params(self, params: Mapping[str, Any]) -> Dict[str, Any]:
        codes, names = text_set(params.get("commodity_codes")), text_set(params.get("commodities"))
        if not (codes or names or params.get("all_commodities")):
            raise ValueError("give commodity_codes, commodities or all_commodities=true")
        since = params.get("since_market_year")
        return {"commodity_codes": codes, "commodities": names, "all_commodities": bool(params.get("all_commodities")),
                "countries": text_set(params.get("countries"), upper=True), "attributes": text_set(params.get("attributes")),
                "since_market_year": int(since) if since else None}

    def list_release(self, http, params) -> Release:
        files = [head_file(http, "psd_alldata_csv.zip", URL)]
        moment = newest(files)
        return Release("usda-fas", "psd-" + stamp(moment, "unknown"), files, published_at=iso(moment),
                       licence={"id": "public-domain-us-gov", "attribution": "USDA Foreign Agricultural Service, PSD Online",
                                "terms_url": "https://apps.fas.usda.gov/psdonline/app/index.html"})

    def process(self, source: FileSource, params) -> Iterator[Any]:
        for handle in zip_members(source.path, (".csv",)):
            for row in csv_rows(handle):
                if not (params["all_commodities"] or row.get("Commodity_Code") in params["commodity_codes"]
                        or row.get("Commodity_Description") in params["commodities"]):
                    continue
                if params["countries"] and row.get("Country_Code", "").upper() not in params["countries"]:
                    continue
                if params["attributes"] and row.get("Attribute_Description") not in params["attributes"]:
                    continue
                year = int(row.get("Market_Year") or 0)
                if params["since_market_year"] and year < params["since_market_year"]:
                    continue
                yield "balances", {
                    "commodity_code": row.get("Commodity_Code"), "commodity": row.get("Commodity_Description"),
                    "country_code": row.get("Country_Code"), "country": row.get("Country_Name"),
                    "market_year": year, "calendar_year": int(row.get("Calendar_Year") or 0) or None,
                    "month": int(row.get("Month") or 0) or None, "attribute_id": row.get("Attribute_ID"),
                    "attribute": row.get("Attribute_Description"), "unit": row.get("Unit_Description"),
                    "value": number(row.get("Value"))}
