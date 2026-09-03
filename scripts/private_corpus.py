#!/usr/bin/env python3
"""Ingest and query a network-free personal corpus."""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DEFAULT_CONFIG = ROOT / "config" / "private-corpus.yml"

BOOK_EXTENSIONS = {".epub"}
MEDIA_EXTENSIONS = {
    ".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac", ".opus",
    ".mp4", ".mkv", ".webm", ".mov", ".avi",
}


def _connector_name(path: Path) -> str:
    if path.suffix.lower() in BOOK_EXTENSIONS:
        return "book"
    if path.suffix.lower() in MEDIA_EXTENSIONS:
        return "transcript"
    return "note"


def _files(paths: list[str], recursive: bool) -> list[Path]:
    found: list[Path] = []
    for raw in paths:
        path = Path(raw).expanduser().resolve()
        if path.is_file():
            found.append(path)
        elif path.is_dir():
            iterator = path.rglob("*") if recursive else path.glob("*")
            found.extend(candidate for candidate in iterator if candidate.is_file())
        else:
            raise FileNotFoundError(path)
    return sorted(set(found))


def ingest(paths: list[str], *, recursive: bool, conn) -> dict:
    from src.ingestion.connectors import get_connector
    from src.ingestion.document_store import DocumentStore
    from src.ingestion.argument_mining import mine_unprocessed_documents
    from src.kb.membership import run_membership_pass
    from src.kb.registry import load_registry

    documents = []
    failures = []
    for path in _files(paths, recursive):
        try:
            connector = get_connector(_connector_name(path))
            for document in connector.harvest([str(path)]):
                document.metadata = {**(document.metadata or {}), "tags": ["private"]}
                documents.append(document)
        except Exception as exc:  # one unsupported/corrupt file must not lose the batch
            failures.append({"path": str(path), "error": str(exc)})
    summary = DocumentStore(conn).upsert(documents)
    registry = load_registry(DEFAULT_CONFIG)
    membership = run_membership_pass(conn, registry)
    mining = mine_unprocessed_documents(conn=conn)
    return {
        "documents": summary.as_dict(),
        "failures": failures,
        "membership": membership["domains"].get("private", {}),
        "argument_mining": mining,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/private/noesis.duckdb"))
    parser.add_argument("--config", type=Path, help="JSON file with paths/recursive")
    sub = parser.add_subparsers(dest="command", required=True)
    ingest_parser = sub.add_parser("ingest")
    ingest_parser.add_argument("paths", nargs="*")
    ingest_parser.add_argument("--no-recursive", action="store_true")
    query_parser = sub.add_parser("query")
    query_parser.add_argument("text")
    query_parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()

    payload = {}
    if args.config:
        payload = json.loads(args.config.read_text())
    args.db.parent.mkdir(parents=True, exist_ok=True)
    import duckdb

    conn = duckdb.connect(str(args.db))
    try:
        if args.command == "ingest":
            paths = args.paths or payload.get("paths", [])
            if not paths:
                parser.error("ingest requires paths or --config")
            result = ingest(
                paths,
                recursive=not args.no_recursive and bool(payload.get("recursive", True)),
                conn=conn,
            )
        else:
            from src.kb.contract import kb_search
            result = kb_search(
                "private", args.text, limit=args.limit, conn=conn,
                config_path=DEFAULT_CONFIG,
            )
    finally:
        conn.close()
    os.chmod(args.db, stat.S_IRUSR | stat.S_IWUSR)
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
