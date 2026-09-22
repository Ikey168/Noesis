"""Bounded Crossref update notices, preserving target evidence and typed status."""

import hashlib
import json
import re
import time
from datetime import date

from src.ingestion.document_store import DocumentStore
from src.ingestion.snapshots import SnapshotStore
from src.ingestion.source_pack_runtime import HTTPSPageAdapter

VERSION = "crossref-rest-v1/noesis-notices-v1"
KINDS = {
    "retraction": "retraction",
    "correction": "correction",
    "erratum": "correction",
    "corrigendum": "correction",
    "expression-of-concern": "expression_of_concern",
    "withdrawal": "withdrawal",
}


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _doi(value):
    value = str(value or "").removeprefix("https://doi.org/").lower().strip()
    return value if re.fullmatch(r"10\.\d{4,9}/\S+", value) else None


def normalize_notices(work):
    """A work is the notice; update-to DOI identifies the affected work."""
    notices = []
    updates = work.get("update-to") or []
    if len(updates) > 100:
        raise ValueError("too many Crossref update relations")
    for update in updates:
        value = {
            "notice_doi": _doi(work.get("DOI")),
            "target_doi": _doi(update.get("DOI")),
            "notice_type": KINDS.get(update.get("type"), "unsupported"),
            "raw_type": update.get("type"),
            "provider": update.get("source"),
            "record_id": update.get("record-id"),
            "notice_date": update.get("updated"),
            "indexed": work.get("indexed"),
            "title": work.get("title") or [],
            "relation": update,
        }
        value["notice_id"] = "crossref-notice:" + _hash(value)
        value["status"] = (
            "supported"
            if value["notice_doi"]
            and value["target_doi"]
            and value["notice_type"] != "unsupported"
            and value["provider"] in {"publisher", "retraction-watch"}
            else "unresolved"
        )
        notices.append(value)
    return notices


class CrossrefNoticeCollection:
    def __init__(
        self,
        conn,
        collection_id,
        *,
        from_date,
        until_date,
        targets=None,
        rows=20,
        max_pages=10,
        max_bytes=2_000_000,
        transport=None,
    ):
        if (
            date.fromisoformat(from_date) > date.fromisoformat(until_date)
            or not collection_id
            or not 1 <= rows <= 100
            or not 1 <= max_pages <= 100
            or not 1 <= max_bytes <= 20_000_000
        ):
            raise ValueError("invalid Crossref collection bounds")
        targets = targets or {}
        if len(targets) > 1000 or any(
            not _doi(k) or not isinstance(v, str) or not v for k, v in targets.items()
        ):
            raise ValueError(
                "targets must map at most 1000 DOI identifiers to documents"
            )
        self.conn, self.collection_id = conn, collection_id
        self.config = {
            "from_date": from_date,
            "until_date": until_date,
            "targets": {_doi(k): v for k, v in targets.items()},
            "rows": rows,
            "max_pages": max_pages,
            "max_bytes": max_bytes,
            "adapter": VERSION,
        }
        self.transport = transport or HTTPSPageAdapter._request
        self.documents, self.snapshots = DocumentStore(conn), SnapshotStore(conn)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS crossref_notice_collections(collection_id TEXT PRIMARY KEY, config TEXT, state TEXT)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS crossref_notices(notice_id TEXT PRIMARY KEY, document_id TEXT, notice_document_id TEXT, notice_json TEXT, observed_at_ms BIGINT)"
        )
        existing = conn.execute(
            "SELECT config FROM crossref_notice_collections WHERE collection_id=?",
            [collection_id],
        ).fetchone()
        if existing and json.loads(existing[0]) != self.config:
            raise ValueError("Crossref collection configuration changed")
        if not existing:
            conn.execute(
                "INSERT INTO crossref_notice_collections VALUES(?,?,?)",
                [
                    collection_id,
                    json.dumps(self.config),
                    json.dumps(
                        {
                            "cursor": "*",
                            "pages": 0,
                            "requests": 0,
                            "status": "running",
                            "notices": [],
                        }
                    ),
                ],
            )

    def inspect(self):
        return json.loads(
            self.conn.execute(
                "SELECT state FROM crossref_notice_collections WHERE collection_id=?",
                [self.collection_id],
            ).fetchone()[0]
        )

    def _save(self, state):
        self.conn.execute(
            "UPDATE crossref_notice_collections SET state=? WHERE collection_id=?",
            [json.dumps(state), self.collection_id],
        )

    def _target_revision(self, target):
        if not self.documents.get(target):
            raise ValueError("selected notice target document does not exist")
        latest = self.documents.revisions.revision(target, include_retracted=True)
        if latest is None:
            raise ValueError("selected notice target has no committed revision")
        return latest["revision_id"]

    def step(self):
        state = self.inspect()
        if state["status"] != "running":
            return state
        if state["requests"] >= self.config["max_pages"]:
            state["status"] = "bounded"
            self._save(state)
            return state
        state["requests"] += 1
        self._save(state)  # Failed requests also consume the finite network budget.
        endpoint = "https://api.crossref.org/v1/works"
        params = {
            "filter": f"is-update:true,from-index-date:{self.config['from_date']},until-index-date:{self.config['until_date']}",
            "rows": self.config["rows"],
            "cursor": state["cursor"],
        }
        response = self.transport(
            url=endpoint,
            params=params,
            headers={
                "User-Agent": "Noesis/0.1 (https://github.com/Ikey168/Noesis)",
                "Accept": "application/json",
            },
            timeout=15,
            max_bytes=self.config["max_bytes"],
        )
        if int(response.get("status", 200)) != 200:
            raise ValueError(
                f"Crossref HTTP {response.get('status')}; no automatic retry"
            )
        raw = response.get("content", b"")
        raw = raw.encode() if isinstance(raw, str) else bytes(raw)
        if len(raw) > self.config["max_bytes"]:
            raise ValueError("Crossref page exceeds byte limit")
        message = json.loads(raw)["message"]
        works = message["items"]
        if not isinstance(works, list) or len(works) > self.config["rows"]:
            raise ValueError("Crossref page exceeds row limit")
        normalized = [
            (work, notice) for work in works for notice in normalize_notices(work)
        ]
        cursor = message.get("next-cursor")
        if works and (not isinstance(cursor, str) or not cursor or len(cursor) > 20000):
            raise ValueError("Crossref page lacks a bounded cursor")
        now = int(time.time() * 1000)
        self.conn.execute("BEGIN TRANSACTION")
        try:
            snapshot = self.snapshots.snapshot_bytes(
                endpoint, raw, now, content_type="application/json", final_url=endpoint
            )
            for work, notice in normalized:
                target = self.config["targets"].get(notice["target_doi"])
                existing = self.conn.execute(
                    "SELECT document_id,notice_json FROM crossref_notices WHERE notice_id=?",
                    [notice["notice_id"]],
                ).fetchone()
                if existing:
                    if target and notice["status"] == "supported":
                        if existing[0] and existing[0] != target:
                            raise ValueError(
                                "notice already linked to another document"
                            )
                        if not existing[0]:
                            retained = json.loads(existing[1])
                            revision = self._target_revision(target)
                            retained.update(
                                target_before_revision=revision,
                                target_after_revision=revision,
                            )
                            self.conn.execute(
                                "UPDATE crossref_notices SET document_id=?,notice_json=? WHERE notice_id=?",
                                [target, json.dumps(retained), notice["notice_id"]],
                            )
                    state["notices"].append(notice["notice_id"])
                    continue
                notice_document = "crossref-update:" + _hash(work)
                notice_payload = {
                    "document_id": notice_document,
                    "source_type": "web",
                    "source_id": "crossref",
                    "language": work.get("language")
                    if work.get("language") in {"de", "en"}
                    else "en",
                    "ingested_at": now,
                    "url": "https://doi.org/" + notice["notice_doi"]
                    if notice["notice_doi"]
                    else endpoint,
                    "title": " / ".join(work.get("title") or []),
                    "content": json.dumps(work, ensure_ascii=False, sort_keys=True),
                    "authors": [],
                    "metadata": {
                        "crossref_notice": True,
                        "snapshot_json": json.dumps(snapshot),
                        "language_status": "provider"
                        if work.get("language") in {"de", "en"}
                        else "fallback_unverified",
                    },
                }
                if self.documents.upsert([notice_payload]).invalid:
                    raise ValueError("notice failed document validation")
                affected = None
                if target and notice["status"] == "supported":
                    revision = self._target_revision(target)
                    notice["target_before_revision"] = revision
                    notice["target_after_revision"] = revision
                    affected = target
                notice["snapshot"] = snapshot
                self.conn.execute(
                    "INSERT INTO crossref_notices VALUES(?,?,?,?,?)",
                    [
                        notice["notice_id"],
                        affected,
                        notice_document,
                        json.dumps(notice),
                        now,
                    ],
                )
                state["notices"].append(notice["notice_id"])
            state["notices"] = sorted(set(state["notices"]))
            state["pages"] += 1
            if not works or len(works) < self.config["rows"]:
                state["status"] = "complete"
            elif state["requests"] >= self.config["max_pages"]:
                state["status"] = "bounded"
            state["cursor"] = cursor
            self._save(state)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return state
