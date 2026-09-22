"""Revision/configuration receipts for the direct document processing passes."""

from __future__ import annotations

import hashlib
import json
import time


def configuration_hash(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def document_input_hash(alias: str = "d") -> str:
    # Internal SQL identifiers only; callers never supply user input as alias.
    if alias not in {"d", "documents"}:
        raise ValueError("unsupported document alias")
    return f"sha256(to_json(struct_pack(title := {alias}.title, content := {alias}.content)))"


class ProcessingVersions:
    def __init__(self, conn):
        self.conn = conn
        conn.execute("""CREATE TABLE IF NOT EXISTS document_processing_versions (
            document_id TEXT NOT NULL, stage TEXT NOT NULL, input_hash TEXT NOT NULL,
            configuration_hash TEXT NOT NULL, processed_at_ms BIGINT NOT NULL,
            PRIMARY KEY(document_id, stage))""")

    def record(self, document_id: str, stage: str, input_hash: str, config_hash: str):
        """Call in the same transaction that publishes the derived output."""
        self.conn.execute("""INSERT INTO document_processing_versions VALUES (?,?,?,?,?)
            ON CONFLICT (document_id,stage) DO UPDATE SET input_hash=excluded.input_hash,
            configuration_hash=excluded.configuration_hash,processed_at_ms=excluded.processed_at_ms""",
            [document_id, stage, input_hash, config_hash, int(time.time() * 1000)])
