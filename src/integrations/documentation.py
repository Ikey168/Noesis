"""Context7 discovery/snippet identity and explicit original-document capture."""

import json
import re
import time
from urllib.parse import urlsplit

from src.ingestion.europepmc_api import _Text
from src.ingestion.snapshots import SnapshotStore
from src.ingestion.source_pack_runtime import HTTPSPageAdapter, _validate_redirect

from .common import IntegrationError, digest


def _text(result):
    return "\n".join(
        block["text"]
        for item in result.get("items", [])
        for block in item.get("value", {}).get("content", [])
        if block.get("type") == "text" and isinstance(block.get("text"), str)
    )


class Context7Research:
    def __init__(self, adapter, *, transport=None, now=None, dns_resolver=None):
        if adapter.describe()["source_id"] != "context7-mcp":
            raise ValueError("Context7 federation adapter required")
        self.adapter = adapter
        self.transport = transport or HTTPSPageAdapter._request
        self.now = now or (lambda: int(time.time() * 1000))
        self.dns_resolver = dns_resolver

    def resolve(self, library_name, query, *, scopes):
        result = self.adapter.query(
            {
                "kind": "tool",
                "name": "resolve-library-id",
                "arguments": {"libraryName": library_name, "query": query},
            },
            scopes=scopes,
        )
        candidates = list(
            dict.fromkeys(
                re.findall(
                    r"Context7-compatible library ID:\s*(/[^\s]+)", _text(result)
                )
            )
        )
        return {
            "status": "selection_required"
            if candidates
            else "no_identified_candidates",
            "library_ids": candidates,
            "raw": result,
        }

    def query(self, library_id, query, *, scopes, requested_version=None):
        if not re.fullmatch(r"/[^/\s]+/[^/\s]+(?:/[^/\s]+)?", library_id):
            raise ValueError("Explicit Context7 library ID required")
        segments = library_id.strip("/").split("/")
        if (
            requested_version is not None
            and len(segments) == 3
            and requested_version != segments[2]
        ):
            raise IntegrationError(
                "version_mismatch",
                "Requested version conflicts with selected library ID",
            )
        result = self.adapter.query(
            {
                "kind": "tool",
                "name": "query-docs",
                "arguments": {"libraryId": library_id, "query": query},
            },
            scopes=scopes,
        )
        text = _text(result)
        links = list(
            dict.fromkeys(
                re.findall(r"^Source:\s*(https://[^\s]+)", text, re.MULTILINE)
            )
        )
        return {
            "contract": "noesis-documentation-snippets-v1",
            "library_id": library_id,
            "requested_version": requested_version,
            "resolved_version": None,
            "version_status": "provider_did_not_attest_exact_version",
            "representation": "provider-snippets",
            "source_links": links,
            "source_identity_status": "links_available"
            if links
            else "missing_citations",
            "retrieved_at_ms": result["provenance"]["observed_at_ms"],
            "source_snapshot_verified": False,
            "text": text,
            "raw": result,
        }

    def capture(self, snippets, source_url, store, *, allowed_hosts, language):
        """Fetch one explicitly selected cited original into existing snapshots/store.

        Snippet version claims remain unverified. A captured original establishes
        exact retrieved bytes, not the correctness of its prose or library version.
        """
        if source_url not in snippets["source_links"]:
            raise IntegrationError(
                "uncited_source", "Select an original cited by this result"
            )
        if urlsplit(source_url).hostname not in set(allowed_hosts):
            raise IntegrationError(
                "source_forbidden", "Documentation host is not allowed"
            )
        if not re.fullmatch(r"[a-z]{2}", language):
            raise ValueError("Explicit ISO 639-1 language required")
        _validate_redirect(source_url, source_url, resolver=self.dns_resolver)
        response = self.transport(
            url=source_url,
            params={},
            headers={
                "Accept": "text/html,text/plain",
                "User-Agent": "Noesis/1.0 (+https://github.com/Ikey168/Noesis)",
            },
            timeout=15,
            max_bytes=2_000_000,
        )
        if response.get("status", 200) != 200:
            raise IntegrationError(
                "source_unavailable", "Original documentation is unavailable"
            )
        content = response["content"]
        content = content.encode() if isinstance(content, str) else content
        if len(content) > 2_000_000:
            raise IntegrationError("input_limit", "Documentation exceeds byte budget")
        final_url = response.get("final_url") or source_url
        _validate_redirect(source_url, final_url, resolver=self.dns_resolver)
        headers = {k.lower(): v for k, v in response.get("headers", {}).items()}
        content_type = headers.get("content-type", "").split(";", 1)[0].lower()
        if content_type not in {"text/html", "text/plain", "application/xhtml+xml"}:
            raise IntegrationError(
                "unsupported_content",
                "Original documentation must be HTML or plain text",
            )
        html = content.decode("utf-8")
        parser = _Text()
        if content_type == "text/plain":
            text = html
        else:
            parser.feed(html)
            parser.close()
            text = "".join(parser.parts)
        if not text.strip():
            raise IntegrationError(
                "empty_document", "Original documentation contains no text"
            )
        observed = self.now()
        snapshot_store = SnapshotStore(store.conn)
        store.conn.execute("BEGIN")
        try:
            snapshot = snapshot_store.snapshot_bytes(
                source_url,
                content,
                observed,
                content_type=content_type,
                final_url=final_url,
            )
            snapshot_store.snapshot(source_url, html, observed)
            metadata = {
                "library_id": snippets["library_id"],
                "requested_version": snippets["requested_version"],
                "resolved_version": None,
                "version_status": snippets["version_status"],
                "acquisition_provenance_json": json.dumps(snapshot, sort_keys=True),
                "content_representation": "captured-original-documentation",
                "snippet_query_hash": snippets["raw"]["provenance"]["query_hash"],
            }
            result = store.upsert(
                [
                    {
                        "document_id": "documentation:"
                        + digest([source_url, snapshot["digest"]]),
                        "source_type": "web",
                        "source_id": "context7-original",
                        "language": language,
                        "title": snippets["library_id"] + " documentation",
                        "url": source_url,
                        "content": text,
                        "ingested_at": observed,
                        "metadata": metadata,
                    }
                ]
            )
            if result.invalid:
                raise IntegrationError(
                    "invalid_document",
                    "Captured documentation failed document validation",
                )
            store.conn.execute("COMMIT")
        except BaseException:
            store.conn.execute("ROLLBACK")
            raise
        return {
            "snapshot": snapshot,
            "document_result": result.as_dict(),
            "snippet_version_verified": False,
        }
