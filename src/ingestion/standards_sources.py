"""Bounded ISO Open Data catalogue acquisition for the Technical standards capability.

ISO publishes deliverables metadata (reference, edition, stage, ICS codes,
replaces/replaced-by, titles, scope) as open data. This connector reads the
JSONL export within the source byte budget and keeps only records matching the
pinned reference prefixes or ICS codes, up to a record ceiling. Standards text
is protected and is never fetched: records carry only the catalogue page link.

ETSI (deliverables site and IPR database) and product certification registries
are recorded in ``PROVIDER_CONTRACTS`` as not implemented until a documented,
permitted machine interface is validated.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from src.ingestion.source_packs import SourcePackError

ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
RECORD_CONTRACT = "noesis-standard-catalogue-v1"
FIXTURE_SECRET = None
MAX_RECORDS = 500
PROVIDER_CONTRACTS = {
    "iso-open-data": {
        "documentation": "https://www.iso.org/open-data.html",
        "access": "ISO deliverables metadata JSONL export (open data)",
        "authentication": "none",
        "content": "catalogue metadata only; standards text is protected and never fetched",
        "status": "unverified-live",
    },
    "etsi": {"status": "not-implemented",
             "reason": "no documented machine catalogue or IPR-declaration interface validated; the deliverables "
                       "site is a document tree behind a search UI"},
    "certification-registries": {
        "status": "not-implemented",
        "candidates": ["EU NANDO (notified bodies)", "Bluetooth SIG qualification listings",
                       "Wi-Fi Alliance certified products"],
        "reason": "access, terms and record semantics must be validated per registry before an adapter; "
                  "certificates enter through the explicit import path meanwhile",
    },
}


def stage_status(stage: Any) -> str:
    try:
        code = int(str(stage).replace(".", ""))
    except ValueError:
        return "unknown"
    if code >= 9500:
        return "withdrawn"
    if code >= 9060:
        return "under-review" if code in {9060, 9092} else "published"
    if code >= 6060:
        return "published"
    return "under-development"


class IsoOpenDataAdapter:
    accepts_transport = True

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None) -> None:
        del secret
        from functools import partial

        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        self.source = json.loads(json.dumps(source))
        selection = dict(dict(self.source.get("standards") or {}).get("selection") or {})
        self.prefixes = [str(p) for p in selection.get("reference_prefixes") or []]
        self.ics = [str(c) for c in selection.get("ics_codes") or []]
        self.ceiling = int(selection.get("max_records") or 0)
        if not (self.prefixes or self.ics) or not 1 <= self.ceiling <= MAX_RECORDS:
            raise SourcePackError("unbounded_source", "standards sources pin reference prefixes or ICS codes and a "
                                                      f"record ceiling of 1-{MAX_RECORDS}")
        self.transport = transport or partial(HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"]))
        self.definition = {
            "contract": ADAPTER_CONTRACT, "source_id": source["source_id"], "connector": source["connector"],
            "endpoint": source["endpoint"], "operations": list(source["operations"]),
            "source_hash": source["source_hash"], "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"], "limits": source["budgets"],
            "standards": {"reference_prefixes": self.prefixes, "ics_codes": self.ics, "max_records": self.ceiling},
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _selected(self, item: Mapping[str, Any]) -> bool:
        reference = str(item.get("reference") or "")
        codes = [str(c) for c in item.get("icsCode") or []]
        return any(reference.startswith(p) for p in self.prefixes) or any(
            code.startswith(prefix) for code in codes for prefix in self.ics)

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage

        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError("operation_forbidden", "operation is not declared by the source")
        if cursor is not None or dict(request.get("parameters") or {}):
            raise SourcePackError("parameter_forbidden", "the catalogue export is read once per run")
        response = self.transport(url=self.source["endpoint"], params={}, headers={"Accept": "application/x-ndjson"},
                                  timeout=int(self.definition["limits"]["timeout_ms"]) / 1000)
        status = int(response.get("status", 200))
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError("response_too_large", "catalogue export exceeds the source byte budget")
        if status >= 500:
            raise SourcePackError("source_unavailable", f"ISO open data returned HTTP {status}")
        if status >= 400:
            raise SourcePackError("schema_drift", f"ISO open data returned HTTP {status}")
        records, malformed, matched = [], 0, 0
        for line in raw.decode("utf-8").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except ValueError:
                malformed += 1
                continue
            if not isinstance(item, Mapping) or not item.get("id") or not item.get("reference"):
                malformed += 1
                continue
            if not self._selected(item):
                continue
            matched += 1
            if len(records) >= self.ceiling:
                continue
            titles = dict(item.get("title") or {})
            records.append({
                "id": f"iso:{item['id']}", "title": titles.get("en") or item["reference"], "language": "en",
                "url": f"https://www.iso.org/standard/{item['id']}.html",
                "standard_record": {
                    "contract": RECORD_CONTRACT, "provider": "iso-open-data", "native_id": str(item["id"]),
                    "reference": item["reference"], "deliverable_type": item.get("deliverableType"),
                    "supplement_type": item.get("supplementType"), "edition": item.get("edition"),
                    "publication_date": item.get("publicationDate"), "stage": item.get("currentStage"),
                    "status": stage_status(item.get("currentStage")), "ics_codes": list(item.get("icsCode") or []),
                    "committee": item.get("ownerCommittee"), "titles": titles, "scope": dict(item.get("scope") or {}),
                    "replaces": [str(v) for v in item.get("replaces") or []],
                    "replaced_by": [str(v) for v in item.get("replacedBy") or []],
                    "languages": list(item.get("languages") or []),
                    "catalogue_url": f"https://www.iso.org/standard/{item['id']}.html",
                    "content_access": "protected-link-only",
                    "native_sha256": hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest(),
                }})
        if malformed and not records and not matched:
            raise SourcePackError("schema_drift", "catalogue export has no parseable deliverables")
        return RuntimePage(tuple(records), None, len(raw), receipt={
            "status": status, "matched": matched, "kept": len(records), "malformed_lines": malformed,
            "truncated": matched > len(records), "export_sha256": hashlib.sha256(raw).hexdigest()})


ADAPTERS = {"iso-open-data": IsoOpenDataAdapter}


def fixture_transport(pages: Sequence[Mapping[str, Any]]) -> Callable[..., Mapping[str, Any]]:
    page = dict(pages[0])

    def transport(*, url, params, headers, timeout):
        del url, params, headers, timeout
        return {"status": int(page.get("status", 200)), "headers": {}, "content": "\n".join(page["lines"]).encode()}

    return transport


def replay_native_fixture(source: Mapping[str, Any], fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    adapter = IsoOpenDataAdapter(source, transport=fixture_transport(list(fixture["native_pages"])))
    return [dict(item) for item in adapter.fetch_page({"operation": min(source["operations"]), "parameters": {}},
                                                      cursor=None).records]
