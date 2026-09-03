"""Network-free connector for document-ingest-v1 JSON manifests.

The connector is intentionally generic: fixtures, air-gapped transfers, and
bulk exports can all enter the canonical document store without bespoke SQL.
Only local files are accepted; network retrieval belongs to source-specific
connectors with their own rate and licensing policy.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from services.ingest.common.document_model import Document
from src.ingestion.connectors.base import Connector, RawDocument, SourceRef
from src.ingestion.connectors.registry import register_connector


@register_connector
class ManifestConnector(Connector):
    """Read a JSON object with a ``documents`` array through the connector API."""

    name = "manifest"
    source_type = "note"

    def discover(self, query: Any = None):
        values = [query] if isinstance(query, (str, Path)) else list(query or [])
        for value in values:
            path = Path(value).expanduser().resolve()
            yield SourceRef(
                locator=str(path), metadata={"source_id": f"manifest:{path.name}"}
            )

    def fetch(self, ref: SourceRef) -> RawDocument:
        path = Path(ref.locator)
        if not path.is_file():
            raise FileNotFoundError(path)
        return RawDocument(
            ref=ref, content=path.read_bytes(), content_type="application/json"
        )

    def parse(self, raw: RawDocument) -> list[Document]:
        payload = json.loads(raw.content)
        if not isinstance(payload, dict) or not isinstance(payload.get("documents"), list):
            raise TypeError("manifest must be an object containing a documents array")
        documents = []
        for index, value in enumerate(payload["documents"]):
            if not isinstance(value, dict):
                raise TypeError(f"manifest document {index} must be an object")
            documents.append(Document.from_dict(value))
        return documents
