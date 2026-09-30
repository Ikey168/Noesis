"""Immutable treaty and treaty-action revisions with as-of lookup (#2581, TR02).

Every acquired ``noesis-treaty-record-v1`` record is appended to its revision
chain, keyed by record key within a namespace:

* ``treaty_revisions`` - one row per treaty revision (identifiers, title as
  published, depositary, adoption and entry-into-force as published, the
  whole record);
* ``treaty_action_revisions`` - one row per treaty-action revision
  (participant as published, action type, action/deposit/effective dates,
  verbatim text, objected action where the source links it);
* ``treaty_receipts`` - one acquisition receipt per run, source and unit.

A record whose content is unchanged adds nothing (a new "Status as at" stamp
alone is not a change). A changed record is a new revision carrying the
depositary's revision stamp and date. An action the source no longer lists
for a treaty it re-published is a ``removed-by-source`` revision; a later
re-listing is a ``relisted`` revision. Nothing is overwritten or deleted.

As-of lookup selects, for a record, the latest revision the source had
published by a date (its depositary date, else the acquisition day). The
TR01 minimisation decision is enforced at write time
(:func:`src.kb.treaties_records.check_minimisation`).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any

from src.kb.treaties_records import (
    DEFAULT_NAMESPACE,
    READ_SCOPE,
    TreatiesError,
    authorize,
    canonical,
    digest,
    load,
    table_exists,
    validate_record,
)

CHANGES = ("new", "revised", "removed-by-source", "relisted")
_DDL = """
CREATE TABLE IF NOT EXISTS treaty_revisions (
  revision_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, record_key TEXT NOT NULL, provider TEXT NOT NULL,
  source_id TEXT, revision_no INTEGER NOT NULL, change TEXT NOT NULL, depositary_revision TEXT, depositary_date DATE,
  title TEXT, identifiers_json TEXT NOT NULL, content_sha256 TEXT NOT NULL, run_id TEXT NOT NULL,
  observed_at_ms BIGINT NOT NULL, evidence_origin TEXT, locator TEXT NOT NULL, record_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS treaty_action_revisions (
  revision_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, record_key TEXT NOT NULL, treaty_key TEXT NOT NULL,
  provider TEXT NOT NULL, source_id TEXT, revision_no INTEGER NOT NULL, change TEXT NOT NULL,
  participant_key TEXT NOT NULL, participant_name TEXT NOT NULL, participant_kind TEXT NOT NULL,
  action_type TEXT NOT NULL, action_date DATE, deposit_date DATE, effective_date DATE, text TEXT,
  objected_key TEXT, depositary_revision TEXT, depositary_date DATE, content_sha256 TEXT NOT NULL,
  run_id TEXT NOT NULL, observed_at_ms BIGINT NOT NULL, evidence_origin TEXT, locator TEXT NOT NULL,
  record_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS treaty_receipts (
  namespace TEXT NOT NULL, run_id TEXT NOT NULL, source_id TEXT NOT NULL, unit_index INTEGER NOT NULL,
  receipt_json TEXT NOT NULL, PRIMARY KEY(namespace, run_id, source_id, unit_index)
);
"""
_COMMON = ("revision_id", "namespace", "record_key", "provider", "source_id", "revision_no", "change",
           "depositary_revision", "depositary_date", "content_sha256", "run_id", "observed_at_ms", "evidence_origin",
           "locator", "record_json")
TREATY_COLUMNS = _COMMON + ("title", "identifiers_json")
ACTION_COLUMNS = _COMMON + ("treaty_key", "participant_key", "participant_name", "participant_kind", "action_type",
                            "action_date", "deposit_date", "effective_date", "text", "objected_key")
_VOLATILE = ("evidence_origin", "depositary_revision", "depositary_date")


def content_hash(record: Mapping[str, Any]) -> str:
    """The record's content without its acquisition origin and revision stamp (a new stamp alone is no change)."""
    return digest({k: v for k, v in record.items() if k not in _VOLATILE})


def as_of_day(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError as exc:
        raise TreatiesError("invalid_request", "dates are YYYY-MM-DD") from exc


def revision_day(row: Mapping[str, Any]) -> str:
    """The day a revision was on record: the depositary's date, else the acquisition day."""
    return str(row.get("depositary_date") or "")[:10] or datetime.fromtimestamp(
        int(row["observed_at_ms"]) / 1000, tz=UTC).date().isoformat()


def cite(row: Mapping[str, Any]) -> dict[str, Any]:
    """The citation of one record revision: source, record revision and as-of time."""
    return {"record_key": row["record_key"], "revision_id": row["revision_id"], "revision_no": row["revision_no"],
            "change": row["change"], "provider": row["provider"], "source_id": row["source_id"],
            "depositary_revision": row["depositary_revision"],
            "depositary_date": str(row["depositary_date"])[:10] if row["depositary_date"] else None,
            "on_record_from": revision_day(row), "retrieved_at_ms": row["observed_at_ms"],
            "evidence_origin": row["evidence_origin"], "locator": row["locator"]}


class TreatyStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def ready(self) -> bool:
        return table_exists(self.conn, "treaty_revisions")

    # ------------------------------------------------------------------ writes

    def _latest(self, table: str, namespace: str, record_key: str) -> dict[str, Any] | None:
        columns = TREATY_COLUMNS if table == "treaty_revisions" else ACTION_COLUMNS
        row = self.conn.execute(f"SELECT {', '.join(columns)} FROM {table} WHERE namespace=? AND record_key=? "
                                "ORDER BY revision_no DESC LIMIT 1", [namespace, record_key]).fetchone()
        return self._row(columns, row) if row else None

    def _insert(self, table: str, values: Mapping[str, Any]) -> None:
        columns = TREATY_COLUMNS if table == "treaty_revisions" else ACTION_COLUMNS
        self.conn.execute(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                          [values[c] for c in columns])

    def _append(self, namespace: str, record: Mapping[str, Any], *, run_id: str, source_id: str | None,
                observed: int) -> str | None:
        table = "treaty_revisions" if record["record_kind"] == "treaty" else "treaty_action_revisions"
        content = content_hash(record)
        latest = self._latest(table, namespace, record["record_key"])
        if latest and latest["content_sha256"] == content and latest["change"] != "removed-by-source":
            return None  # a replay or an unchanged re-read adds nothing
        change = "new" if latest is None else "relisted" if latest["change"] == "removed-by-source" else "revised"
        revision_no = 1 if latest is None else latest["revision_no"] + 1
        values = {"revision_id": "treaty-revision:" + digest([namespace, record["record_key"], revision_no,
                                                              content])[:24],
                  "namespace": namespace, "record_key": record["record_key"], "provider": record["provider"],
                  "source_id": source_id, "revision_no": revision_no, "change": change,
                  "depositary_revision": record.get("depositary_revision"),
                  "depositary_date": record.get("depositary_date"), "content_sha256": content, "run_id": run_id,
                  "observed_at_ms": observed, "evidence_origin": record.get("evidence_origin"),
                  "locator": record["locator"], "record_json": canonical(record)}
        fields = dict(record["fields"])
        if table == "treaty_revisions":
            values.update(title=fields.get("title_as_published") or record["title"],
                          identifiers_json=canonical(fields.get("identifiers") or []))
        else:
            participant = dict(fields["participant"])
            values.update(treaty_key=record["treaty_key"], participant_key=participant["key"],
                          participant_name=participant["name_as_published"], participant_kind=participant["kind"],
                          action_type=fields["action_type"], action_date=fields.get("action_date"),
                          deposit_date=fields.get("deposit_date"), effective_date=fields.get("effective_date"),
                          text=fields.get("text"),
                          objected_key=dict(fields.get("objected") or {}).get("action_key"))
        self._insert(table, values)
        return change

    def _remove_unlisted(self, namespace: str, treaty: Mapping[str, Any], *, run_id: str, source_id: str | None,
                         observed: int) -> int:
        """Actions of a re-published treaty that the source no longer lists become removed-by-source revisions."""
        listed = set(dict(treaty["fields"]).get("action_keys") or [])
        keys = [r[0] for r in self.conn.execute(
            "SELECT DISTINCT record_key FROM treaty_action_revisions WHERE namespace=? AND treaty_key=? AND provider=? "
            "ORDER BY 1", [namespace, treaty["record_key"], treaty["provider"]]).fetchall()]
        removed = 0
        for key in keys:
            latest = self._latest("treaty_action_revisions", namespace, key)
            if key in listed or latest is None or latest["change"] == "removed-by-source":
                continue
            revision_no = latest["revision_no"] + 1
            content = digest({"removed_by_source": key, "previous": latest["content_sha256"]})
            self._insert("treaty_action_revisions", {
                **{c: latest[c] for c in ACTION_COLUMNS},
                "revision_id": "treaty-revision:" + digest([namespace, key, revision_no, content])[:24],
                "source_id": source_id, "revision_no": revision_no, "change": "removed-by-source",
                "depositary_revision": treaty.get("depositary_revision"),
                "depositary_date": treaty.get("depositary_date"), "content_sha256": content, "run_id": run_id,
                "observed_at_ms": observed, "evidence_origin": treaty.get("evidence_origin"),
                "action_date": latest["action_date"], "deposit_date": latest["deposit_date"],
                "effective_date": latest["effective_date"]})
            removed += 1
        return removed

    def project(self, namespace: str, records: Iterable[Mapping[str, Any]], *, run_id: str, source_id: str | None,
                receipt: Mapping[str, Any] | None = None, observed_at_ms: int | None = None) -> dict[str, int]:
        observed = int(observed_at_ms if observed_at_ms is not None else self.now())
        records = [validate_record(r) for r in records]
        counts = {"treaty_revisions": 0, "action_revisions": 0, "removed_by_source": 0, "unchanged": 0}
        self.conn.execute("BEGIN")
        try:
            for record in records:
                change = self._append(namespace, record, run_id=run_id, source_id=source_id, observed=observed)
                if change is None:
                    counts["unchanged"] += 1
                else:
                    counts["treaty_revisions" if record["record_kind"] == "treaty" else "action_revisions"] += 1
            for record in records:
                if record["record_kind"] == "treaty":
                    counts["removed_by_source"] += self._remove_unlisted(namespace, record, run_id=run_id,
                                                                         source_id=source_id, observed=observed)
            if receipt and source_id:
                self.conn.execute("INSERT OR REPLACE INTO treaty_receipts VALUES (?,?,?,?,?)",
                                  [namespace, run_id, source_id, int(receipt.get("unit_index") or 0),
                                   canonical(dict(receipt))])
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return counts

    # ------------------------------------------------------------------ reads

    @staticmethod
    def _row(columns: Sequence[str], row: Sequence[Any]) -> dict[str, Any]:
        out = dict(zip(columns, row))
        for key in ("depositary_date", "action_date", "deposit_date", "effective_date"):
            if key in out and out[key] is not None:
                out[key] = str(out[key])[:10]
        return out

    def receipts(self, namespace: str, run_id: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        authorize(namespace, scopes, READ_SCOPE)
        if not table_exists(self.conn, "treaty_receipts"):
            return []
        rows = self.conn.execute("SELECT source_id, unit_index, receipt_json FROM treaty_receipts WHERE namespace=? "
                                 "AND run_id=? ORDER BY source_id, unit_index", [namespace, run_id]).fetchall()
        return [{"source_id": r[0], "unit_index": r[1], **load(r[2], {})} for r in rows]

    def revisions(self, namespace: str, record_key: str) -> list[dict[str, Any]]:
        """Every revision of a treaty or treaty-action record, oldest first."""
        table, columns = (("treaty_revisions", TREATY_COLUMNS) if record_key.startswith("treaties:treaty:")
                          else ("treaty_action_revisions", ACTION_COLUMNS))
        if not table_exists(self.conn, table):
            return []
        rows = self.conn.execute(f"SELECT {', '.join(columns)} FROM {table} WHERE namespace=? AND record_key=? "
                                 "ORDER BY revision_no", [namespace, record_key]).fetchall()
        return [self._row(columns, r) for r in rows]

    def as_of(self, namespace: str, record_key: str, known_as_of: str | None = None) -> dict[str, Any] | None:
        """The revision the source had published by ``known_as_of`` (the latest one when not given)."""
        cutoff = as_of_day(known_as_of)
        eligible = [r for r in self.revisions(namespace, record_key) if cutoff is None or revision_day(r) <= cutoff]
        return max(eligible, key=lambda r: r["revision_no"]) if eligible else None

    def treaty_keys(self, namespace: str, *, provider: str | None = None) -> list[str]:
        if not table_exists(self.conn, "treaty_revisions"):
            return []
        return [r[0] for r in self.conn.execute(
            "SELECT DISTINCT record_key FROM treaty_revisions WHERE namespace=? AND (? IS NULL OR provider=?) "
            "ORDER BY 1", [namespace, provider, provider]).fetchall()]

    def action_keys(self, namespace: str, *, treaty_keys: Sequence[str] | None = None,
                    participant_keys: Sequence[str] | None = None, provider: str | None = None) -> list[str]:
        if not table_exists(self.conn, "treaty_action_revisions"):
            return []
        sql = "SELECT DISTINCT record_key FROM treaty_action_revisions WHERE namespace=?"
        params: list[Any] = [namespace]
        for column, values in (("treaty_key", treaty_keys), ("participant_key", participant_keys)):
            if values is not None:
                if not values:
                    return []
                sql += f" AND {column} IN ({', '.join('?' * len(values))})"
                params += list(values)
        if provider:
            sql += " AND provider=?"
            params.append(provider)
        return [r[0] for r in self.conn.execute(sql + " ORDER BY 1", params).fetchall()]

    def participants(self, namespace: str) -> list[dict[str, Any]]:
        """Every participant as published per source, with its treaties and published codes."""
        if not table_exists(self.conn, "treaty_action_revisions"):
            return []
        out: dict[str, dict[str, Any]] = {}
        for key, name, kind, provider, treaty, record_json in self.conn.execute(
                "SELECT participant_key, participant_name, participant_kind, provider, treaty_key, record_json FROM "
                "treaty_action_revisions WHERE namespace=? ORDER BY participant_key, record_key, revision_no",
                [namespace]).fetchall():
            entry = out.setdefault(key, {"participant_key": key, "name_as_published": name, "kind": kind,
                                         "provider": provider, "treaties": set(), "published_codes": []})
            entry["treaties"].add(treaty)
            for code in load(record_json, {})["fields"]["participant"].get("published_codes") or []:
                if code not in entry["published_codes"]:
                    entry["published_codes"].append(code)
        return [{**v, "treaties": sorted(v["treaties"])} for _, v in sorted(out.items())]

    @staticmethod
    def record(row: Mapping[str, Any]) -> dict[str, Any]:
        return load(row["record_json"], {})


class TreatiesProjector:
    """Source-pack runtime projector for ``noesis-treaty-record-v1``."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn
        self.store = TreatyStore(conn)

    @staticmethod
    def _namespace(source: Mapping[str, Any]) -> str:
        return str(dict(source.get("treaties") or {}).get("namespace") or DEFAULT_NAMESPACE)

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        del manifest, documents, principal_id
        counts = self.store.project(self._namespace(source), [r["treaty_record"] for r in records], run_id=run_id,
                                    source_id=source["source_id"], receipt=dict(page_receipt or {}))
        return [{"counts": counts}]

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del manifest, principal_id, run_id
        return {"status": status}


def readiness(conn: Any, *, pack_id: str = "legal-research") -> dict[str, Any]:
    """Whether each treaties feature is selected and what each provider has acquired; declined access is stated."""
    from src.ingestion.treaties_sources import (
        FORMATS,
        LICENCE_DECISIONS,
        LIVE_VERIFICATION,
    )
    from src.kb.treaties_records import FEATURES, feature_enabled

    counts: dict[str, int] = {}
    for table in ("treaty_revisions", "treaty_action_revisions"):
        if table_exists(conn, table):
            for provider, count in conn.execute(f"SELECT provider, count(*) FROM {table} GROUP BY 1").fetchall():
                counts[provider] = counts.get(provider, 0) + int(count)
    providers = {spec["provider"]: {"format": fmt, "feature": spec["feature"],
                                    "revisions_acquired": counts.get(spec["provider"], 0),
                                    "licence_decision": LICENCE_DECISIONS[spec["provider"]],
                                    "live": LIVE_VERIFICATION[spec["provider"]]} for fmt, spec in FORMATS.items()}
    return {"pack_id": pack_id, "features": {f: feature_enabled(conn, f) for f in FEATURES.values()},
            "providers": providers,
            "notice": "declined providers are not acquired; unverified-live providers have fixture evidence only "
                      "(TR13, #2645)"}


__all__ = ["CHANGES", "TreatiesProjector", "TreatyStore", "as_of_day", "cite", "content_hash", "readiness",
           "revision_day"]
