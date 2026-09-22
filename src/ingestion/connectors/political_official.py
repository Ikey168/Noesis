"""Fail-closed connector for declarative official political sources."""

from __future__ import annotations

import json
import os
import time
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from services.ingest.common.document_model import Document
from src.ingestion.connectors.base import (
    Connector,
    PermanentFetchError,
    RawDocument,
    SourceRef,
)
from src.ingestion.connectors.registry import register_connector
from src.kb.temporal import TemporalError, parse_source_time

SOURCE_CONTRACT = "noesis-political-source-v1"
RECORD_CONTRACT = "political-records-v1"
LIVE_ENV = "NOESIS_POLITICAL_LIVE"
DEFAULT_CATALOG = Path(__file__).resolve().parents[3] / "config/political_sources.json"
SOURCE_CLASSES = {"executive", "regulatory", "electoral", "parliamentary"}


def validate_source_catalog(payload: Any) -> list[str]:
    """Validate the safety-critical fields without optional JSON Schema code."""

    if not isinstance(payload, Mapping):
        return ["catalog must be an object"]
    errors: list[str] = []
    if payload.get("contract") != SOURCE_CONTRACT:
        errors.append(f"contract must be {SOURCE_CONTRACT}")
    sources = payload.get("sources")
    if not isinstance(sources, list) or not sources:
        return [*errors, "sources must be a non-empty list"]
    seen: set[str] = set()
    required = (
        "source_id",
        "source_class",
        "jurisdiction",
        "issuing_institution",
        "document_types",
        "identifier_fields",
        "license",
        "update_cadence",
        "canonical_url",
        "parser",
        "fixture_path",
        "live",
    )
    for index, source in enumerate(sources):
        where = f"sources[{index}]"
        if not isinstance(source, Mapping):
            errors.append(f"{where} must be an object")
            continue
        missing = [key for key in required if not source.get(key)]
        if missing:
            errors.append(f"{where} missing {', '.join(missing)}")
        source_id = str(source.get("source_id") or "")
        if source_id in seen:
            errors.append(f"duplicate source_id {source_id!r}")
        seen.add(source_id)
        if source.get("source_class") not in SOURCE_CLASSES:
            errors.append(f"{where}.source_class is unsupported")
        if not str(source.get("canonical_url") or "").startswith("https://"):
            errors.append(f"{where}.canonical_url must use HTTPS")
        if source.get("parser") != RECORD_CONTRACT:
            errors.append(f"{where}.parser is unsupported")
        if not isinstance(source.get("document_types"), list):
            errors.append(f"{where}.document_types must be a list")
        live = source.get("live")
        if not isinstance(live, Mapping) or not isinstance(live.get("enabled"), bool):
            errors.append(f"{where}.live must declare enabled explicitly")
        elif live.get("requires_api_key") and not str(live.get("api_key_env") or "").startswith(
            "NOESIS_"
        ):
            errors.append(f"{where}.live.api_key_env is required for keyed sources")
    represented = {
        str(source.get("source_class"))
        for source in sources
        if isinstance(source, Mapping)
    }
    if represented != SOURCE_CLASSES:
        errors.append("catalog must represent executive, regulatory, electoral, and parliamentary sources")
    return errors


def load_source_catalog(path: Path | str = DEFAULT_CATALOG) -> dict[str, dict[str, Any]]:
    catalog_path = Path(path)
    try:
        payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read political source catalog: {exc}") from exc
    errors = validate_source_catalog(payload)
    if errors:
        raise ValueError("invalid political source catalog: " + "; ".join(errors))
    return {str(source["source_id"]): dict(source) for source in payload["sources"]}


@register_connector
class PoliticalOfficialConnector(Connector):
    """Normalize already-adapted official records; never guess source fields."""

    name = "political-official"
    source_type = "note"

    def __init__(
        self,
        catalog_path: Path | str = DEFAULT_CATALOG,
        *,
        opener: Any = None,
    ) -> None:
        self.catalog_path = Path(catalog_path)
        self.sources = load_source_catalog(self.catalog_path)
        self._opener = opener or urllib.request.urlopen

    def discover(self, query: Any = None):
        offline = isinstance(query, Mapping) and bool(query.get("offline"))
        selected = query.get("source_ids") if isinstance(query, Mapping) else query
        if selected is None:
            source_ids = sorted(self.sources)
        elif isinstance(selected, str):
            source_ids = [selected]
        else:
            source_ids = [str(value) for value in selected]
        for source_id in source_ids:
            if source_id not in self.sources:
                raise ValueError(f"unknown political source {source_id!r}")
            source = dict(self.sources[source_id])
            locator = str(source["canonical_url"])
            if offline:
                fixture = Path(source["fixture_path"])
                if not fixture.is_absolute():
                    fixture = self.catalog_path.parent.parent / fixture
                locator = str(fixture)
                source["fixture"] = True
            yield SourceRef(locator=locator, metadata={**source, "source_id": source_id})

    def fetch(self, ref: SourceRef) -> RawDocument:
        if ref.metadata.get("fixture"):
            path = Path(ref.locator)
            if not path.is_file():
                raise PermanentFetchError(f"political fixture not found: {path}")
            return RawDocument(ref=ref, content=path.read_bytes(), content_type="application/json")
        live = ref.metadata.get("live") or {}
        if os.getenv(LIVE_ENV) != "1" or not live.get("enabled"):
            raise PermanentFetchError(
                f"live political fetch disabled for {ref.source_id}; set {LIVE_ENV}=1 and enable the manifest"
            )
        api_key_env = live.get("api_key_env")
        if live.get("requires_api_key") and (
            not api_key_env or not os.getenv(str(api_key_env))
        ):
            raise PermanentFetchError(
                f"live political fetch for {ref.source_id} requires {api_key_env or 'a declared API key'}"
            )
        request = urllib.request.Request(
            ref.locator, headers={"User-Agent": "Noesis/1.0 political-official"}
        )
        try:
            with self._opener(request, timeout=30) as response:
                content = response.read()
                content_type = response.headers.get_content_type()
        except Exception as exc:
            raise PermanentFetchError(f"could not fetch {ref.source_id}: {exc}") from exc
        return RawDocument(ref=ref, content=content, content_type=content_type)

    def parse(self, raw: RawDocument) -> list[Document]:
        try:
            payload = json.loads(
                raw.content.decode("utf-8-sig")
                if isinstance(raw.content, bytes)
                else raw.content
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("official political payload is not valid UTF-8 JSON") from exc
        if not isinstance(payload, Mapping) or payload.get("contract") != RECORD_CONTRACT:
            raise ValueError(f"official political payload must declare {RECORD_CONTRACT}")
        if payload.get("source_id") != raw.ref.source_id:
            raise ValueError("official political payload source_id does not match its manifest")
        records = payload.get("records")
        if not isinstance(records, list):
            raise ValueError("official political payload records must be a list")  # noqa: TRY004
        documents: list[Document] = []
        allowed_types = set(raw.ref.metadata.get("document_types") or [])
        for index, record in enumerate(records):
            documents.append(self._parse_record(raw, record, index, allowed_types))
        return documents

    @staticmethod
    def _parse_record(
        raw: RawDocument,
        record: Any,
        index: int,
        allowed_types: set[str],
    ) -> Document:
        if not isinstance(record, Mapping):
            raise ValueError(f"record {index} must be an object")  # noqa: TRY004
        required = ("record_id", "document_type", "title", "content", "issued_at", "canonical_url")
        missing = [key for key in required if not record.get(key)]
        if missing:
            raise ValueError(f"record {index} missing {', '.join(missing)}")
        document_type = str(record["document_type"])
        if document_type not in allowed_types:
            raise ValueError(f"record {index} document_type is not allowed by its manifest")
        url = str(record["canonical_url"])
        if not url.startswith("https://"):
            raise ValueError(f"record {index} canonical_url must use HTTPS")
        try:
            issued_at, receipt = parse_source_time(record["issued_at"], field="issued_at")
            if record.get("effective_from") is not None:
                parse_source_time(record["effective_from"], field="effective_from")
        except TemporalError as exc:
            raise ValueError(f"record {index} has malformed time: {exc}") from exc
        source_id = raw.ref.source_id
        identifier = str(record["record_id"])
        return Document(
            document_id=f"political:{source_id}:{identifier}",
            source_type="note",
            language=str(record.get("language") or "en"),
            ingested_at=int(raw.fetched_at or time.time() * 1000),
            created_at=issued_at,
            source_id=source_id,
            url=url,
            title=str(record["title"]),
            content=str(record["content"]),
            metadata={
                "tags": ["political", str(raw.ref.metadata["jurisdiction"]).casefold()],
                "jurisdiction": raw.ref.metadata["jurisdiction"],
                "issuing_institution": raw.ref.metadata["issuing_institution"],
                "document_type": document_type,
                "official_identifier": identifier,
                "license": raw.ref.metadata["license"],
                "update_cadence": raw.ref.metadata["update_cadence"],
                "canonical_url": url,
                "source_manifest_id": source_id,
                "source_time_receipt": json.dumps(receipt, sort_keys=True),
                "effective_from": record.get("effective_from"),
                "political": json.dumps(dict(record.get("political") or {}), sort_keys=True),
            },
        )


def live_smoke(source_id: str, *, catalog_path: Path | str = DEFAULT_CATALOG) -> dict[str, Any]:
    """Perform one explicitly enabled reachability fetch; CI remains offline."""

    connector = PoliticalOfficialConnector(catalog_path)
    ref = next(iter(connector.discover(source_id)))
    raw = connector.fetch(ref)
    return {
        "source_id": source_id,
        "reachable": bool(raw.content),
        "content_type": raw.content_type,
        "fetched_at_ms": raw.fetched_at,
        "parsed": False,
        "note": "reachability only; production source adapters must emit political-records-v1 before ingestion",
    }


__all__ = [
    "DEFAULT_CATALOG",
    "LIVE_ENV",
    "PoliticalOfficialConnector",
    "live_smoke",
    "load_source_catalog",
    "validate_source_catalog",
]
