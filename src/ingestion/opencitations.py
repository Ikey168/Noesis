"""Bounded OpenCitations v2 snapshots and resumable local citation-graph import.

The documented API has no cursor pagination. Resume over an immutable captured
response, never invent upstream limit/offset parameters.
"""

import json
import re
import time
from urllib.parse import quote

from src.ingestion.source_pack_runtime import HTTPSPageAdapter
from src.integrations.common import IntegrationError, digest
from src.knowledge_graph.foundation import (
    EntityType,
    Node,
    Provenance,
    RelationType,
    Triple,
)


def normalize_identifier(value):
    value = str(value).strip().removeprefix("https://doi.org/")
    if value.startswith("10."):
        value = "doi:" + value
    if len(value) > 2048:
        raise IntegrationError("invalid_identifier", "Identifier exceeds length budget")
    prefix, separator, suffix = value.partition(":")
    if (
        not separator
        or prefix not in {"doi", "pmid", "omid"}
        or not suffix
        or any(c.isspace() for c in suffix)
    ):
        raise IntegrationError(
            "invalid_identifier", "An explicit DOI, PMID or OMID is required"
        )
    if prefix == "doi" and (not suffix.startswith("10.") or "/" not in suffix):
        raise IntegrationError("invalid_identifier", "Invalid DOI")
    if prefix == "pmid" and not suffix.isdigit():
        raise IntegrationError("invalid_identifier", "Invalid PMID")
    if prefix == "omid" and not re.fullmatch(r"br/[0-9]+", suffix):
        raise IntegrationError("invalid_identifier", "Invalid OMID")
    return prefix + ":" + (suffix.lower() if prefix == "doi" else suffix)


def _identity(values):
    identifiers = [
        normalize_identifier(v)
        for v in str(values).split()
        if v.startswith(("doi:", "pmid:", "omid:"))
    ]
    if not identifiers:
        raise IntegrationError(
            "missing_identifier", "Citation endpoint lacks a supported identifier"
        )
    return next((v for v in identifiers if v.startswith("doi:")), identifiers[0])


class OpenCitationsClient:
    def __init__(
        self, *, transport=None, token=None, max_bytes=5_000_000, max_edges=10000
    ):
        if not 1 <= max_bytes <= 20_000_000 or not 1 <= max_edges <= 10000:
            raise ValueError("Invalid citation bounds")
        if token is not None and (
            not isinstance(token, str)
            or not token
            or len(token) > 8192
            or any(c.isspace() for c in token)
        ):
            raise IntegrationError(
                "invalid_credential", "Invalid OpenCitations token format"
            )
        self.transport = transport or HTTPSPageAdapter._request
        self.token, self.max_bytes, self.max_edges = token, max_bytes, max_edges

    def snapshot(self, identifier, *, direction="references", observed_at_ms=None):
        identifier = normalize_identifier(identifier)
        if direction not in {"references", "citations"}:
            raise ValueError("Choose references or citations")
        headers = {"Accept": "application/json"}
        if self.token:
            headers["authorization"] = self.token
        url = (
            "https://api.opencitations.net/index/v2/"
            + direction
            + "/"
            + quote(identifier, safe=":")
        )
        response = self.transport(
            url=url, params={}, headers=headers, timeout=20, max_bytes=self.max_bytes
        )
        if response.get("status", 200) != 200:
            raise IntegrationError("source_unavailable", "OpenCitations request failed")
        content = response["content"]
        content = content.encode() if isinstance(content, str) else content
        if len(content) > self.max_bytes:
            raise IntegrationError(
                "input_limit", "Citation snapshot exceeds byte budget"
            )
        records = json.loads(content)
        if not isinstance(records, list) or len(records) > self.max_edges:
            raise IntegrationError(
                "input_limit", "Citation response exceeds edge budget"
            )
        for record in records:
            if not re.fullmatch(r"[0-9]+-[0-9]+", record.get("oci", "")):
                raise IntegrationError("invalid_citation", "Citation lacks an OCI")
            _identity(record.get("citing"))
            _identity(record.get("cited"))
            field = record["citing"] if direction == "references" else record["cited"]
            if identifier not in [
                normalize_identifier(v)
                for v in field.split()
                if v.startswith(("doi:", "pmid:", "omid:"))
            ]:
                raise IntegrationError(
                    "identity_mismatch",
                    "Citation does not involve the requested identifier",
                )
        snapshot = {
            "provider": "opencitations",
            "api_version": "2.2.0",
            "identifier": identifier,
            "direction": direction,
            "url": url,
            "records": records,
            "observed_at_ms": observed_at_ms
            if observed_at_ms is not None
            else int(time.time() * 1000),
            "pagination": "bounded whole upstream response; resume over captured snapshot",
        }
        return {**snapshot, "sha256": digest(snapshot)}

    def ingest_snapshot(self, snapshot, store, *, cursor=None, page_size=100):
        if not 1 <= page_size <= 1000:
            raise ValueError("Invalid import page size")
        expected = digest({k: v for k, v in snapshot.items() if k != "sha256"})
        if expected != snapshot.get("sha256"):
            raise IntegrationError("changed_snapshot", "Citation snapshot was modified")
        start = 0
        if cursor:
            if (
                cursor.get("snapshot_sha256") != expected
                or type(cursor.get("index")) is not int
            ):
                raise IntegrationError(
                    "invalid_cursor", "Cursor belongs to another snapshot"
                )
            start = cursor["index"]
        records = snapshot["records"]
        if not 0 <= start <= len(records):
            raise IntegrationError("invalid_cursor", "Cursor is outside snapshot")
        imported = 0
        for record in records[start : start + page_size]:
            citing, cited = _identity(record["citing"]), _identity(record["cited"])
            for identity in (citing, cited):
                store.add_node(Node(EntityType.DOCUMENT, identity, node_id=identity))
            provenance = Provenance(
                source_doc="https://opencitations.net/oci/" + record["oci"],
                extractor="opencitations-v2",
                chunk_id=expected,
                confidence=1.0,
            )
            triple = Triple(
                citing,
                RelationType.CITES,
                cited,
                provenance=provenance,
                properties={
                    "opencitations": {
                        "oci": record["oci"],
                        "observed_at_ms": snapshot["observed_at_ms"],
                        "snapshot_sha256": expected,
                        "native_record": record,
                    },
                    "corroboration_semantics": "citation relation; mirrored provider records are not independent evidence",
                },
            )
            if not any(
                p.source_doc == provenance.source_doc
                and p.extractor == provenance.extractor
                and p.chunk_id == expected
                for p in store.provenance_for(triple)
            ):
                store.add_triple(triple)
                imported += 1
        next_index = min(len(records), start + page_size)
        return {
            "imported": imported,
            "processed": next_index - start,
            "snapshot_sha256": expected,
            "next_cursor": {"snapshot_sha256": expected, "index": next_index}
            if next_index < len(records)
            else None,
        }
