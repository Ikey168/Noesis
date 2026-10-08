"""Ember electricity data CSVs (FA05): keyless alternative to the Ember API.

Ember publishes its generation datasets as CSV under CC BY 4.0 at
``files.ember-energy.org/public-downloads/generation/outputs/`` —
``release_generation_monthly_global.csv`` (~29 MB) and
``release_generation_yearly_global.csv`` (~16 MB), one row per area, period and
electricity source with generation, shares, capacity (yearly), emissions and
intensity. Updated about twice a month; ETag/Last-Modified are sent.

Parameters: ``frequency`` ("monthly" | "yearly", default monthly), and
``iso3`` (list) and/or ``areas`` (names), or ``all_areas=true``; ``sources``
(e.g. ["Solar", "Wind"]); ``since`` (YYYY or YYYY-MM-DD).
"""
from __future__ import annotations

from typing import Any, Dict, Iterator, Mapping

from src.ingestion.bulk.adapters._common import csv_rows, head_file, iso, newest, number, stamp, text_set
from src.ingestion.bulk.base import BulkAdapter, FileSource, Release, TableSpec

BASE = "https://files.ember-energy.org/public-downloads/generation/outputs/"
FILES = {"monthly": "release_generation_monthly_global.csv", "yearly": "release_generation_yearly_global.csv"}
METRICS = {"Generation (TWh)": "generation_twh", "Share of generation (%)": "share_of_generation_pct",
           "Capacity (GW)": "capacity_gw", "Emissions (MtCO2e)": "emissions_mtco2e",
           "Emissions intensity (gCO2e/kWh)": "emissions_intensity_gco2e_per_kwh"}


class EmberGeneration(BulkAdapter):
    name = "ember-generation"
    publisher = "Ember"
    title = "Ember electricity generation data"
    description = "Ember monthly or yearly generation, capacity and emissions by area and source (CC BY 4.0)."
    allowed_hosts = ("files.ember-energy.org",)
    tables = {"generation": TableSpec("generation", [
        {"name": "area", "type": "string"}, {"name": "iso3", "type": "string"}, {"name": "area_type", "type": "string"},
        {"name": "period", "type": "string"}, {"name": "frequency", "type": "string"},
        {"name": "source", "type": "string"}, {"name": "is_aggregate", "type": "boolean"},
        *({"name": col, "type": "number"} for col in METRICS.values())],
        primary_key=("area", "period", "source"))}

    def validate_params(self, params: Mapping[str, Any]) -> Dict[str, Any]:
        frequency = params.get("frequency", "monthly")
        if frequency not in FILES:
            raise ValueError("frequency must be monthly or yearly")
        iso3, areas = text_set(params.get("iso3"), upper=True), text_set(params.get("areas"))
        if not (iso3 or areas or params.get("all_areas")):
            raise ValueError("give iso3, areas or all_areas=true")
        return {"frequency": frequency, "iso3": iso3, "areas": areas, "all_areas": bool(params.get("all_areas")),
                "sources": text_set(params.get("sources")), "since": str(params.get("since") or "")}

    def list_release(self, http, params) -> Release:
        name = FILES[params["frequency"]]
        files = [head_file(http, name, BASE + name, mode="stream")]
        moment = newest(files)
        return Release("ember", f"ember-{params['frequency']}-{stamp(moment, 'unknown')}", files, published_at=iso(moment),
                       licence={"id": "CC-BY-4.0", "attribution": "Ember",
                                "terms_url": "https://ember-energy.org/data/monthly-electricity-data/"})

    def process(self, source: FileSource, params) -> Iterator[Any]:
        period_col = "Date" if params["frequency"] == "monthly" else "Year"
        with source.open_stream() as raw:
            for row in csv_rows(raw):
                if not (params["all_areas"] or row.get("ISO 3 code", "").upper() in params["iso3"]
                        or row.get("Area", "") in params["areas"]):
                    continue
                if params["sources"] and row.get("Electricity source") not in params["sources"]:
                    continue
                period = row.get(period_col, "")
                if params["since"] and period < params["since"]:
                    continue
                yield "generation", {
                    "area": row.get("Area"), "iso3": row.get("ISO 3 code") or None, "area_type": row.get("Area type"),
                    "period": period, "frequency": params["frequency"], "source": row.get("Electricity source"),
                    "is_aggregate": row.get("Is aggregated source") == "True",
                    **{col: number(row.get(src)) for src, col in METRICS.items()}}
