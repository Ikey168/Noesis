"""Query Context7 and archive one explicitly selected original documentation URL."""

import argparse
import json
import time
from pathlib import Path

import duckdb

from src.ingestion.document_store import DocumentStore
from src.integrations.documentation import Context7Research
from src.integrations.mcp import federation_adapter


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--library-id", required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--allowed-host", required=True)
    parser.add_argument("--language", required=True)
    parser.add_argument("--requested-version")
    parser.add_argument("--anonymous", action="store_true")
    args = parser.parse_args()
    adapter = federation_adapter(
        "context7", secret_resolver=(lambda _: None) if args.anonymous else None
    )
    conn = duckdb.connect(args.database)
    try:
        client = Context7Research(adapter)
        started = time.monotonic()
        snippets = client.query(
            args.library_id,
            args.query,
            requested_version=args.requested_version,
            scopes={"operator"},
        )
        query_ms = (time.monotonic() - started) * 1000
        captured = client.capture(
            snippets,
            args.source_url,
            DocumentStore(conn),
            allowed_hosts=[args.allowed_host],
            language=args.language,
        )
        result = {
            "query_ms": query_ms,
            "server_version": adapter.client.version,
            "library_id": snippets["library_id"],
            "requested_version": snippets["requested_version"],
            "resolved_version": snippets["resolved_version"],
            "version_status": snippets["version_status"],
            "source_links": snippets["source_links"],
            "capture": captured,
        }
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2) + "\n")
    finally:
        adapter.client.close()
        conn.close()


if __name__ == "__main__":
    main()
