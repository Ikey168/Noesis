"""Standards/specification ingestion with exact structural locators."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from services.ingest.common.document_model import Document
from src.domains.technical.model import record_object, record_relation
from src.ingestion.connectors.base import Connector, RawDocument, SourceRef
from src.ingestion.connectors.registry import register_connector

STATUS_VALUES = frozenset({"draft", "final", "obsolete", "amended"})


class SpecificationError(ValueError):
    pass


def _anchor(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-") or "section"


@register_connector
class SpecificationConnector(Connector):
    """Parse a JSON source manifest or Markdown specification into cited sections."""

    source_type = "web"
    name = "technical-specification"

    def __init__(self, *, opener=urlopen) -> None:
        self.opener = opener

    def discover(self, query: Any = None) -> Iterable[SourceRef]:
        if isinstance(query, str):
            query = {"source": query}
        query = dict(query or {})
        source = str(query.get("source") or query.get("url") or query.get("path") or "").strip()
        if not source:
            raise SpecificationError("specification source is required")
        metadata = {key: value for key, value in query.items() if key not in {"source", "url", "path"}}
        metadata["source_id"] = str(
            query.get("specification_id")
            or "spec:" + hashlib.sha256(source.encode()).hexdigest()[:20]
        )
        yield SourceRef(source, query.get("title"), metadata)

    def fetch(self, ref: SourceRef) -> RawDocument:
        path = Path(ref.locator)
        if path.exists():
            content = path.read_text()
            content_type = "application/json" if path.suffix == ".json" else "text/markdown"
        else:
            if os.getenv("NOESIS_TECHNICAL_LIVE") != "1":
                raise SpecificationError(
                    "live specification access requires NOESIS_TECHNICAL_LIVE=1"
                )
            request = Request(ref.locator, headers={"User-Agent": "Noesis/technical-knowledge"})
            with self.opener(request, timeout=20) as response:
                content = response.read().decode()
                content_type = response.headers.get_content_type()
        return RawDocument(ref=ref, content=content, content_type=content_type)

    def parse(self, raw: RawDocument) -> list[Document]:
        defaults = dict(raw.ref.metadata)
        if raw.content_type == "application/json" or str(raw.content).lstrip().startswith("{"):
            payload = json.loads(raw.content)
            metadata = {**defaults, **payload}
            sections = list(payload.get("sections") or ())
        else:
            metadata = defaults
            sections = _markdown_sections(str(raw.content))
        specification_id = str(metadata.get("specification_id") or metadata.get("source_id"))
        title = str(metadata.get("title") or raw.ref.title or specification_id)
        version = str(metadata.get("version") or "unversioned")
        status = str(metadata.get("status") or "draft").casefold()
        if status not in STATUS_VALUES:
            raise SpecificationError(f"unsupported specification status {status!r}")
        base_url = str(metadata.get("canonical_url") or raw.ref.locator)
        common = {
            "kind": "specification_section",
            "specification_id": specification_id,
            "version": version,
            "status": status,
            "published_at": metadata.get("published_at"),
            "supersedes": list(metadata.get("supersedes") or ()),
            "superseded_by": metadata.get("superseded_by"),
            "normative_references": list(metadata.get("normative_references") or ()),
            "amendments": list(metadata.get("amendments") or ()),
        }
        documents = []
        seen: dict[str, int] = {}
        for index, section in enumerate(sections, 1):
            heading = str(section.get("title") or section.get("heading") or f"Section {index}")
            base_anchor = str(section.get("anchor") or _anchor(heading))
            seen[base_anchor] = seen.get(base_anchor, 0) + 1
            anchor = base_anchor if seen[base_anchor] == 1 else f"{base_anchor}-{seen[base_anchor]}"
            section_url = str(section.get("url") or f"{base_url}#{anchor}")
            locator = {
                "specification_id": specification_id,
                "version": version,
                "section": section.get("number") or str(index),
                "anchor": anchor,
                "url": section_url,
            }
            content = str(section.get("content") or section.get("text") or "")
            document_id = (
                "technical:spec:"
                + hashlib.sha256(
                    f"{specification_id}:{version}:{anchor}".encode()
                ).hexdigest()[:28]
            )
            documents.append(
                Document(
                    document_id=document_id,
                    source_type=self.source_type,
                    language=str(metadata.get("language") or "en"),
                    ingested_at=raw.fetched_at,
                    source_id=specification_id,
                    url=section_url,
                    title=f"{title} {version} — {heading}",
                    content=content,
                    authors=[str(item) for item in metadata.get("editors") or ()],
                    created_at=_millis(metadata.get("published_at")),
                    metadata={
                        **common,
                        "section_title": heading,
                        "locator": locator,
                        "normative": bool(section.get("normative", True)),
                        "normative_references": list(
                            section.get("normative_references")
                            or common["normative_references"]
                        ),
                    },
                )
            )
        if not documents:
            raise SpecificationError("specification contains no structural sections")
        return documents


def _markdown_sections(content: str) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    title, lines = None, []
    for line in content.splitlines():
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if match:
            if title is not None:
                sections.append({"title": title, "content": "\n".join(lines).strip()})
            title, lines = match.group(2), []
        elif title is not None:
            lines.append(line)
    if title is not None:
        sections.append({"title": title, "content": "\n".join(lines).strip()})
    return sections


def _millis(value: Any) -> int | None:
    if value is None:
        return None
    from src.kb.temporal import parse_source_time

    return parse_source_time(value, field="published_at")[0]


def ingest_specification(
    conn: Any,
    documents: list[Document],
    *,
    domain: str = "technology",
) -> dict[str, Any]:
    if not documents:
        raise SpecificationError("documents are required")
    meta = documents[0].metadata
    spec_id = str(meta["specification_id"])
    version = str(meta["version"])
    object_id = f"specification:{spec_id}:{version}"
    obj = record_object(
        conn,
        object_type="specification",
        object_id=object_id,
        canonical_name=documents[0].title.rsplit(" — ", 1)[0],
        version=version,
        immutable_id=f"{spec_id}@{version}",
        status=str(meta["status"]),
        published_at=meta.get("published_at"),
        observed_at=documents[0].ingested_at,
        source_url=documents[0].url.split("#", 1)[0] if documents[0].url else None,
        source_document_id=documents[0].document_id,
        metadata={"section_count": len(documents), "amendments": meta.get("amendments", [])},
        provenance={"section_document_ids": [item.document_id for item in documents]},
        domain=domain,
    )
    for predecessor in meta.get("supersedes") or ():
        record_relation(
            conn, object_id, "supersedes", str(predecessor),
            observed_at=documents[0].ingested_at,
            source_url=documents[0].url, source_document_id=documents[0].document_id,
            domain=domain,
        )
    return {"specification": obj, "sections": [item.metadata["locator"] for item in documents]}


__all__ = [
    "STATUS_VALUES",
    "SpecificationConnector",
    "SpecificationError",
    "ingest_specification",
]
