"""Bounded adapters for the 2026 EU/provider integration roadmap.

These adapters deliberately separate fixture/import conformance from live provider
readiness. Hosted services never run without explicit opt-in, credentials and a
cost ceiling; provider summaries are discovery metadata, not source evidence.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import io
import json
import time
import zipfile
from dataclasses import dataclass
from typing import ClassVar
from urllib.parse import urlsplit

from src.ingestion.connectors.base import SourceRef


def _bytes(response, *, limit):
    raw = response.get("content", b"")
    raw = raw.encode() if isinstance(raw, str) else bytes(raw)
    if response.get("status", 200) != 200 or not raw:
        raise ValueError("provider request failed")
    if len(raw) > limit:
        raise ValueError("provider response exceeds byte budget")
    return raw


def _https(url):
    parsed = urlsplit(str(url))
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("HTTPS provider URL required")
    return str(url)


def parse_tei(data: bytes, *, max_bytes=10_000_000):
    """Parse GROBID-like TEI while retaining section/reference locators."""
    if not data or len(data) > max_bytes:
        raise ValueError("TEI missing or oversized")
    from defusedxml import ElementTree as ET

    root = ET.fromstring(data)
    if not root.tag.endswith("TEI"):
        raise ValueError("expected TEI root")
    sections, references = [], []
    for node in root.iter():
        local = node.tag.rsplit("}", 1)[-1]
        text = " ".join("".join(node.itertext()).split())
        xml_id = node.get("{http://www.w3.org/XML/1998/namespace}id")
        if local in {"p", "head"} and text:
            sections.append(
                {
                    "kind": local,
                    "id": xml_id,
                    "text": text,
                    "coords": node.get("coords"),
                }
            )
        if local == "biblStruct":
            references.append(
                {
                    "id": xml_id,
                    "text": text,
                    "coords": node.get("coords"),
                }
            )
    if not sections and not references:
        raise ValueError("TEI contains no usable structure")
    return {
        "sections": sections,
        "references": references,
        "text": "\n\n".join(x["text"] for x in sections),
    }


class OpenAlexContentAcquirer:
    """Fetch bounded OpenAlex full-text objects with conservative charge replay."""

    def __init__(self, snapshots, *, transport, max_bytes=20_000_000, budget_micros=0):
        self.snapshots = snapshots
        self.transport = transport
        self.max_bytes = min(20_000_000, max(1, int(max_bytes)))
        self.budget_micros = max(0, int(budget_micros))
        self.spent_micros = 0
        self._receipts = {}

    def acquire(self, work, *, price_micros=0):
        work_id = str(work.get("id") or "")
        if not work_id.startswith("https://openalex.org/W"):
            raise ValueError("stable OpenAlex work ID required")
        if work_id in self._receipts:
            return self._receipts[work_id]
        urls = work.get("content_urls") or work.get("content", {}).get("urls") or []
        if isinstance(urls, dict):
            urls = [urls]
        candidates = []
        for item in urls:
            if isinstance(item, str):
                candidates.append({"url": item, "format": None})
            elif isinstance(item, dict) and item.get("url"):
                candidates.append(item)
        if not work.get("has_content") or not candidates:
            return {
                "outcome": "unavailable",
                "work_id": work_id,
                "content_coverage": "abstract-or-metadata-only",
            }
        price = max(0, int(price_micros))
        if price and self.spent_micros + price > self.budget_micros:
            return {
                "outcome": "unavailable",
                "work_id": work_id,
                "failure_code": "priced_download_budget_exceeded",
            }
        item = candidates[0]
        url = _https(item["url"])
        self.spent_micros += price  # reserve conservatively before the request
        try:
            response = self.transport(
                url=url,
                params={},
                headers={"Accept": "application/pdf,application/xml,text/xml"},
                timeout=20,
            )
            raw = _bytes(response, limit=self.max_bytes)
            content_type = next(
                (
                    str(v).split(";", 1)[0]
                    for k, v in response.get("headers", {}).items()
                    if str(k).lower() == "content-type"
                ),
                "application/octet-stream",
            )
            fetched_at = int(response.get("fetched_at_ms") or time.time() * 1000)
            receipt = self.snapshots.snapshot_bytes(
                url,
                raw,
                fetched_at,
                content_type=content_type,
                final_url=response.get("final_url", url),
            )
            result = {
                "outcome": "full-text",
                "work_id": work_id,
                "snapshot": receipt,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "retrieved_at_ms": fetched_at,
                "format": item.get("format") or content_type,
                "license": item.get("license") or work.get("license"),
                "reserved_cost_micros": price,
            }
            if raw.lstrip().startswith(b"<"):
                result.update(parse_tei(raw, max_bytes=self.max_bytes))
            elif raw.startswith(b"%PDF"):
                result.update(
                    content_coverage="full-text-pdf", sections=[], references=[]
                )
            else:
                raise ValueError("unsupported OpenAlex content representation")
        except Exception as exc:  # noqa: BLE001 - preserve explicit provider failure
            result = {
                "outcome": "failed",
                "work_id": work_id,
                "failure_code": type(exc).__name__,
                "reserved_cost_micros": price,
            }
        self._receipts[work_id] = result
        return result


@dataclass(frozen=True)
class RegionalProviderSpec:
    provider: str
    source_url: str
    access: str
    coverage: str
    version: str = "2026-09-contract"


REGIONAL_PROVIDER_SPECS = {
    "ctis": RegionalProviderSpec(
        "ctis",
        "https://euclinicaltrials.eu/",
        "documented-export-import; no public REST API asserted",
        "EU/EEA CTIS public records; field availability varies",
    ),
    "drks": RegionalProviderSpec(
        "drks",
        "https://drks.de/",
        "documented JSON/CSV/RIS export-import",
        "German Clinical Trials Register",
    ),
    "cellar": RegionalProviderSpec(
        "cellar",
        "https://op.europa.eu/web/cellar",
        "public CELLAR dissemination/SPARQL",
        "EU Publications Office legal corpus",
    ),
    "german-courts": RegionalProviderSpec(
        "german-courts",
        "https://www.rechtsprechung-im-internet.de/",
        "official publication/import",
        "published German federal court decisions, not all German case law",
    ),
    "berlin-law": RegionalProviderSpec(
        "berlin-law",
        "https://gesetze.berlin.de/",
        "documented public export/import where available",
        "Berlin official portal; unrestricted API not asserted",
    ),
    "opencorporates": RegionalProviderSpec(
        "opencorporates",
        "https://api.opencorporates.com/",
        "credentialed API; explicit account/cost policy",
        "provider registry coverage; not an official registry substitute",
    ),
    "opensanctions": RegionalProviderSpec(
        "opensanctions",
        "https://api.opensanctions.org/",
        "credentialed API/dataset; review-only matches",
        "provider datasets and list coverage",
    ),
    "ema": RegionalProviderSpec(
        "ema",
        "https://www.ema.europa.eu/",
        "public website JSON/PMS API",
        "centralised EMA material only unless source says otherwise",
    ),
    "bfarm": RegionalProviderSpec(
        "bfarm",
        "https://www.bfarm.de/",
        "public RSS/linked documents",
        "BfArM scope; PEI products excluded unless explicitly sourced",
    ),
}


def normalize_regional_record(provider: str, record: dict):
    """Normalize fixture/export records without erasing provider-native fields."""
    spec = REGIONAL_PROVIDER_SPECS[provider]
    if not isinstance(record, dict):
        raise TypeError("record must be an object")
    stable_id = (
        record.get("id")
        or record.get("identifier")
        or record.get("registry_number")
        or record.get("ecli")
        or record.get("celex")
    )
    if not stable_id:
        raise ValueError("provider record lacks stable identity")
    return {
        "provider": provider,
        "provider_id": str(stable_id),
        "source_url": record.get("source_url") or spec.source_url,
        "observed_at": record.get("observed_at"),
        "published_at": record.get("published_at"),
        "updated_at": record.get("updated_at"),
        "language": record.get("language"),
        "jurisdiction": record.get("jurisdiction"),
        "status": record.get("status"),
        "title": record.get("title") or record.get("name"),
        "locators": record.get("locators") or [],
        "relationships": record.get("relationships") or [],
        "missing_fields": sorted(record.get("missing_fields") or []),
        "review_required": provider == "opensanctions"
        or bool(record.get("review_required")),
        "provider_score": record.get("provider_score")
        if provider == "opensanctions"
        else None,
        "coverage_notice": spec.coverage,
        "native": record,
    }


class HostedDiscovery:
    """Exa/Tavily discovery candidate adapter with explicit usage ceilings."""

    ENDPOINTS: ClassVar[dict[str, str]] = {
        "exa": "https://api.exa.ai/search",
        "tavily": "https://api.tavily.com/search",
    }

    def __init__(
        self,
        provider,
        *,
        api_key,
        transport,
        per_request_cost_micros,
        budget_micros,
        enabled=False,
    ):
        if provider not in self.ENDPOINTS:
            raise ValueError("unsupported discovery provider")
        if not enabled:
            raise ValueError("remote discovery is opt-in")
        if not api_key:
            raise ValueError("provider credential required")
        self.provider, self.api_key, self.transport = provider, api_key, transport
        self.price, self.budget, self.spent = (
            int(per_request_cost_micros),
            int(budget_micros),
            0,
        )
        if min(self.price, self.budget) < 0:
            raise ValueError("invalid cost ceiling")

    def discover(
        self, query, *, domains=None, from_date=None, to_date=None, max_results=10
    ):
        if not str(query).strip() or not 1 <= int(max_results) <= 100:
            raise ValueError("invalid discovery request")
        if self.spent + self.price > self.budget:
            raise ValueError("discovery cost budget exhausted")
        self.spent += self.price
        payload = {"query": query, "max_results": int(max_results)}
        if domains:
            payload["include_domains"] = list(domains)[:20]
        if from_date:
            payload["start_published_date"] = from_date
        if to_date:
            payload["end_published_date"] = to_date
        response = self.transport(
            url=self.ENDPOINTS[self.provider],
            json=payload,
            headers={"Authorization": "Bearer " + self.api_key},
            timeout=20,
        )
        data = json.loads(_bytes(response, limit=5_000_000))
        items = data.get("results") or []
        if not isinstance(items, list) or len(items) > int(max_results):
            raise ValueError("invalid discovery results")
        for item in items:
            url = item.get("url")
            if not url or urlsplit(url).scheme not in {"http", "https"}:
                continue
            yield SourceRef(
                url,
                title=item.get("title"),
                metadata={
                    "discovery_provider": self.provider,
                    "provider_source_id": item.get("id") or item.get("score"),
                    "provider_summary": item.get("text") or item.get("content"),
                    "provider_summary_is_evidence": False,
                    "requires_source_acquisition": True,
                    "reserved_cost_micros": self.price,
                },
            )


class JinaReaderFallback:
    def __init__(self, *, transport, enabled=False, max_bytes=2_000_000):
        if not enabled:
            raise ValueError("remote processing is opt-in")
        self.transport, self.max_bytes = transport, min(5_000_000, int(max_bytes))

    def extract(self, url, *, original_snapshot):
        _https(url)
        if not original_snapshot or not original_snapshot.get("digest"):
            raise ValueError("original source snapshot required")
        response = self.transport(
            url="https://r.jina.ai/" + url, params={}, headers={}, timeout=20
        )
        raw = _bytes(response, limit=self.max_bytes)
        return {
            "origin_url": url,
            "original_snapshot": original_snapshot,
            "transformed_text": raw.decode("utf-8", "replace"),
            "transformed_by": "jina-reader",
            "locator_fidelity": "document-level-only",
            "precise_locators": False,
        }


def optional_document_conversion(
    data: bytes, *, filename, converter=None, max_bytes=10_000_000
):
    """Evaluation-only MarkItDown boundary; original bytes stay authoritative."""
    if not data or len(data) > max_bytes:
        raise ValueError("document missing or oversized")
    digest = hashlib.sha256(data).hexdigest()
    native_converter = converter is None
    embedded_count = 0
    if native_converter:
        extension = filename.rsplit(".", 1)[-1].casefold() if "." in filename else ""
        required_zip_members = {
            "docx": "word/document.xml",
            "xlsx": "xl/workbook.xml",
            "pptx": "ppt/presentation.xml",
        }
        required_member = required_zip_members.get(extension)
        if required_member:
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    entries = archive.infolist()
                    names = {entry.filename for entry in entries}
                    if (
                        len(entries) > 10000
                        or sum(entry.file_size for entry in entries) > max_bytes * 10
                    ):
                        return {
                            "status": "failed",
                            "backend": "markitdown",
                            "original_sha256": digest,
                            "failure_type": "document_expansion_limit",
                        }
                    if len(names) != len(entries):
                        raise ValueError("ambiguous duplicate Office archive members")
                    embedded_count = sum(
                        "/embeddings/" in name or "/media/" in name for name in names
                    )
                if "[Content_Types].xml" not in names or required_member not in names:
                    raise ValueError(f"invalid {extension} package structure")
            except (zipfile.BadZipFile, ValueError):
                return {
                    "status": "failed",
                    "backend": "markitdown",
                    "original_sha256": digest,
                    "failure_type": f"invalid_{extension}_container",
                }
        try:
            from markitdown import MarkItDown

            version = importlib.metadata.version("markitdown")
        except (ImportError, importlib.metadata.PackageNotFoundError):
            return {
                "status": "unavailable",
                "backend": "markitdown",
                "original_sha256": digest,
                "reason": "optional dependency not installed",
            }
        converter = MarkItDown(enable_plugins=False).convert_stream
    else:
        version = "injected-fixture-converter"
    try:
        converted = converter(
            io.BytesIO(data) if native_converter else data,
            file_extension=filename.rsplit(".", 1)[-1],
        )
        text = getattr(converted, "text_content", converted)
        if not isinstance(text, str) or len(text.encode()) > max_bytes:
            raise ValueError("converted output invalid or oversized")
        return {
            "status": "completed",
            "backend": "markitdown",
            "version": version,
            "original_sha256": digest,
            "text": text,
            "locator_fidelity": "approximate/markdown-only",
            "precise_locators": False,
            "embedded_content_count": embedded_count,
            "embedded_content_coverage": "not_evaluated"
            if embedded_count
            else "none_present",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "failed",
            "backend": "markitdown",
            "original_sha256": digest,
            "failure_type": type(exc).__name__,
        }


def normalize_paddleocr(
    pages, *, model_version, config, elapsed_seconds=None, peak_rss_kib=None
):
    """Adapt OCR/layout output to page/bounding-box provenance without inference."""
    output = []
    for page_no, regions in enumerate(pages, 1):
        for region in regions:
            if len(region) < 2:
                continue
            bbox, value = region[0], region[1]
            text = value[0] if isinstance(value, (list, tuple)) else value
            confidence = (
                value[1]
                if isinstance(value, (list, tuple)) and len(value) > 1
                else None
            )
            output.append(
                {
                    "page": page_no,
                    "bbox": bbox,
                    "text": str(text),
                    "confidence": confidence,
                    "uncertain": confidence is not None and float(confidence) < 0.8,
                }
            )
    return {
        "backend": "paddleocr",
        "model_version": model_version,
        "configuration": dict(config),
        "regions": output,
        "missing_regions_preserved": True,
        "elapsed_seconds": elapsed_seconds,
        "peak_rss_kib": peak_rss_kib,
    }
