"""IATI Bulk Data Service (FA10): alternative to the capped IATI Datastore API.

``https://bulk-data.iatistandard.org/datasets-minimal`` (JSON, ~15 MB) indexes
every registered dataset with its publisher (``reporting_org_short_name``),
licence and a cached copy (``last_known_good_dataset.cached_dataset_url_xml``)
plus its SHA-1 ``hash``. This adapter downloads only the declared publishers'
files, verifies the hash, parses activities with defusedxml, and skips files
whose hash is unchanged since an earlier run. The all-in-one ZIP (~820 MB) is
not used.

Parameters: ``publishers`` (required; registry short names, e.g.
["theglobalfund"]); optional ``recipient_countries`` (ISO2), ``statuses``
(activity-status codes), ``identifiers``. Licence per dataset as published.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterator, List, Mapping, Optional

from defusedxml.ElementTree import iterparse

from src.ingestion.bulk.adapters._common import number, text_set
from src.ingestion.bulk.base import BulkAdapter, FileSource, Release, ReleaseFile, TableSpec

INDEX = "https://bulk-data.iatistandard.org/datasets-minimal"
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
DATE_TYPES = {"1": "planned_start", "2": "actual_start", "3": "planned_end", "4": "actual_end"}
TX_TYPES = {"2": "commitment", "3": "disbursement", "4": "expenditure"}


def _narrative(element) -> Optional[str]:
    if element is None:
        return None
    texts = [(n.get(XML_LANG), (n.text or "").strip()) for n in element.findall("narrative")]
    english = [t for lang, t in texts if lang in (None, "en") and t]
    return (english or [t for _, t in texts if t] or [None])[0]


class IatiActivities(BulkAdapter):
    name = "iati-activities"
    publisher = "IATI Secretariat (Bulk Data Service)"
    title = "IATI activities by publisher"
    description = "IATI activity records from the Bulk Data Service, for declared publishers."
    allowed_hosts = ("bulk-data.iatistandard.org",)
    tables = {"activities": TableSpec("activities", [
        {"name": "iati_identifier", "type": "string"}, {"name": "publisher", "type": "string"},
        {"name": "dataset", "type": "string"}, {"name": "reporting_org_ref", "type": "string"},
        {"name": "reporting_org", "type": "string"}, {"name": "title", "type": "string"},
        {"name": "activity_status", "type": "string"}, {"name": "dates", "type": "json"},
        {"name": "recipient_countries", "type": "json"}, {"name": "sectors", "type": "json"},
        {"name": "default_currency", "type": "string"}, {"name": "budget_total", "type": "number"},
        {"name": "transaction_totals", "type": "json"}, {"name": "last_updated", "type": "string"},
        {"name": "licence", "type": "string"}], primary_key=("iati_identifier", "dataset"))}

    def validate_params(self, params: Mapping[str, Any]) -> Dict[str, Any]:
        publishers = text_set(params.get("publishers"))
        if not publishers:
            raise ValueError("publishers is required (IATI Registry short names)")
        return {"publishers": publishers, "recipient_countries": text_set(params.get("recipient_countries"), upper=True),
                "statuses": text_set(params.get("statuses")), "identifiers": text_set(params.get("identifiers"))}

    def list_release(self, http, params) -> Release:
        index = json.loads(http.get_text(INDEX, limit=200 * 1024 * 1024))
        files: List[ReleaseFile] = []
        for entry in index["datasets"]:
            if entry.get("reporting_org_short_name") not in params["publishers"]:
                continue
            good = entry.get("last_known_good_dataset") or {}
            url = good.get("cached_dataset_url_xml")
            if not url:
                continue
            files.append(ReleaseFile(name=f"{entry['short_name']}.xml", url=url, mode="download",
                                     checksum=("sha1", good["hash"]) if good.get("hash") else None,
                                     metadata={"publisher": entry["reporting_org_short_name"],
                                               "dataset": entry["short_name"], "licence": entry.get("licence_id")}))
        if not files:
            raise ValueError(f"no datasets for publishers {params['publishers']}")
        created = str(index.get("index_created", ""))[:19].replace(" ", "T")
        return Release("iati", "iati-" + created.replace(":", "").replace("-", ""), sorted(files, key=lambda f: f.name),
                       published_at=created + "Z" if created else None,
                       licence={"id": "per-dataset", "note": "each IATI dataset carries its own licence (see records)",
                                "terms_url": "https://iatistandard.org/en/iati-tools-and-resources/bulk-data-service/"})

    def process(self, source: FileSource, params) -> Iterator[Any]:
        meta = source.file.metadata
        for _, element in iterparse(str(source.path), events=("end",)):
            if element.tag != "iati-activity":
                continue
            ident = (element.findtext("iati-identifier") or "").strip()
            countries = [{"code": c.get("code"), "percentage": number(c.get("percentage"))}
                         for c in element.findall("recipient-country")]
            status = (element.find("activity-status").get("code") if element.find("activity-status") is not None else None)
            keep = True
            if params["identifiers"] and ident not in params["identifiers"]:
                keep = False
            if params["recipient_countries"] and not {(c["code"] or "").upper() for c in countries} & set(params["recipient_countries"]):
                keep = False
            if params["statuses"] and status not in params["statuses"]:
                keep = False
            if keep:
                totals: Dict[str, float] = {}
                for tx in element.findall("transaction"):
                    kind = TX_TYPES.get((tx.find("transaction-type").get("code") if tx.find("transaction-type") is not None else ""))
                    value = number(tx.findtext("value"))
                    if kind and value is not None:
                        totals[kind] = totals.get(kind, 0.0) + value
                budgets = [number(b.findtext("value")) for b in element.findall("budget")]
                org = element.find("reporting-org")
                yield "activities", {
                    "iati_identifier": ident, "publisher": meta.get("publisher"), "dataset": meta.get("dataset"),
                    "reporting_org_ref": org.get("ref") if org is not None else None, "reporting_org": _narrative(org),
                    "title": _narrative(element.find("title")), "activity_status": status,
                    "dates": {DATE_TYPES[d.get("type")]: d.get("iso-date") for d in element.findall("activity-date")
                              if d.get("type") in DATE_TYPES},
                    "recipient_countries": countries,
                    "sectors": [{"code": s.get("code"), "vocabulary": s.get("vocabulary"),
                                 "percentage": number(s.get("percentage"))} for s in element.findall("sector")],
                    "default_currency": element.get("default-currency"),
                    "budget_total": sum(b for b in budgets if b is not None) if budgets else None,
                    "transaction_totals": totals, "last_updated": element.get("last-updated-datetime"),
                    "licence": meta.get("licence")}
            element.clear()
