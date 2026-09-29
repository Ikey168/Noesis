"""Sanctions designations and trade-control lists: the ``legal.sanctions`` record owner (#1907, S02/S03/S04/S05/S08).

Records (contract ``noesis-sanctions-record-v1``), each carrying the list it
came from, its source revision and its acquisition time separately from the
dates the list itself reports:

* **snapshot** - one acquired full-list publication (publication date, file
  digest); the source revision of everything derived from it;
* **designation** - one entry of one list, keyed by the per-list identifier
  (EU reference number, UN permanent reference number, OFAC UID, UK unique
  ID). No shared party key is ever created: the same party on two lists is two
  designations, and a cross-list match is a reviewable identity decision
  (:mod:`src.kb.sanctions_identity`);
* **listing revision** - what the list stated about the entry, appended when a
  new snapshot adds (``listed``/``relisted``), changes (``amended``) or drops
  (``delisted``) it, citing both snapshots compared. Nothing is inferred between
  snapshots; a replayed snapshot adds nothing;
* **programme**, **legal basis** (a CELEX/ELI reference resolved to a Legal
  ``legal_works`` row by exact identifier, else kept as the source string with
  status ``unresolved``), **identifier alias** (names, transliterations, dates
  of birth, passports, IMO numbers, addresses - each a source assertion of one
  revision) and **delisting**;
* **control-list entry** - a control code in one edition (a consolidated
  version of Regulation (EU) 2021/821) at its passage locator, projected from
  the Legal store's passages.

Reads answer what a list stated, per list, as of a date. They carry no
screening result, risk score or compliance status.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date
from typing import Any

from src.ingestion.sanctions_sources import LISTS, PARTY_KINDS, REVIEW_BOUNDARY

CONTRACT = "noesis-sanctions-record-v1"
ANSWER_CONTRACT = "noesis-sanctions-statement-v1"
READ_SCOPE = "knowledge:legal:read"
WRITE_SCOPE = "knowledge:legal:write"
DEFAULT_NAMESPACE = "global"
RECORD_TYPES = (
    "snapshot",
    "designation",
    "listing_revision",
    "programme",
    "legal_basis",
    "identifier_alias",
    "delisting",
    "control_list_entry",
)
CHANGES = ("listed", "amended", "relisted", "delisted")
# Control lists composed from Legal works: id -> the base act whose consolidated editions carry the annex.
CONTROL_LISTS = {
    "eu-dual-use": {
        "base_celex": "32021R0821",
        "annex": "i",
        "title": "Regulation (EU) 2021/821, Annex I",
    }
}
FORBIDDEN_ANSWER_KEYS = frozenset(
    {
        "screening",
        "screening_result",
        "risk",
        "risk_score",
        "score",
        "compliance",
        "compliance_status",
        "verdict",
        "match_score",
        "sanctioned",
    }
)

_DDL = """
CREATE TABLE IF NOT EXISTS sanctions_snapshots (
  namespace TEXT NOT NULL, snapshot_id TEXT NOT NULL, list_id TEXT NOT NULL, source_id TEXT, format TEXT NOT NULL,
  publication_date TEXT NOT NULL, file_sha256 TEXT NOT NULL, entries_sha256 TEXT NOT NULL, entry_count INTEGER NOT NULL,
  sequence INTEGER NOT NULL, previous_snapshot_id TEXT, run_id TEXT NOT NULL, acquired_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, snapshot_id)
);
CREATE TABLE IF NOT EXISTS sanctions_designations (
  namespace TEXT NOT NULL, designation_id TEXT NOT NULL, list_id TEXT NOT NULL, list_entry_id TEXT NOT NULL,
  party_kind TEXT NOT NULL, record_key TEXT NOT NULL, canonical_entity_id TEXT NOT NULL,
  first_snapshot_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, designation_id)
);
CREATE TABLE IF NOT EXISTS sanctions_revisions (
  namespace TEXT NOT NULL, revision_id TEXT NOT NULL, designation_id TEXT NOT NULL, list_id TEXT NOT NULL,
  revision_no INTEGER NOT NULL, change TEXT NOT NULL, snapshot_id TEXT NOT NULL, previous_snapshot_id TEXT,
  content_hash TEXT, statement_json TEXT, listed_on TEXT, amended_on TEXT, acquired_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, revision_id)
);
CREATE TABLE IF NOT EXISTS sanctions_snapshot_members (
  namespace TEXT NOT NULL, snapshot_id TEXT NOT NULL, designation_id TEXT NOT NULL, revision_id TEXT NOT NULL,
  PRIMARY KEY(namespace, snapshot_id, designation_id)
);
CREATE TABLE IF NOT EXISTS sanctions_aliases (
  namespace TEXT NOT NULL, alias_id TEXT NOT NULL, revision_id TEXT NOT NULL, designation_id TEXT NOT NULL,
  list_id TEXT NOT NULL, alias_kind TEXT NOT NULL, value TEXT NOT NULL, normalized TEXT NOT NULL,
  detail_json TEXT NOT NULL, PRIMARY KEY(namespace, alias_id)
);
CREATE TABLE IF NOT EXISTS sanctions_programmes (
  namespace TEXT NOT NULL, programme_id TEXT NOT NULL, list_id TEXT NOT NULL, code TEXT NOT NULL, name TEXT,
  first_snapshot_id TEXT NOT NULL, PRIMARY KEY(namespace, programme_id)
);
CREATE TABLE IF NOT EXISTS sanctions_legal_bases (
  namespace TEXT NOT NULL, basis_id TEXT NOT NULL, list_id TEXT NOT NULL, citation TEXT NOT NULL, celex TEXT,
  eli TEXT, url TEXT, status TEXT NOT NULL, work_id TEXT, expression_id TEXT, resolution_json TEXT NOT NULL,
  first_snapshot_id TEXT NOT NULL, PRIMARY KEY(namespace, basis_id)
);
CREATE TABLE IF NOT EXISTS sanctions_control_entries (
  namespace TEXT NOT NULL, entry_id TEXT NOT NULL, control_list TEXT NOT NULL, control_code TEXT NOT NULL,
  work_id TEXT NOT NULL, version_id TEXT NOT NULL, edition TEXT, category TEXT, locator_key TEXT NOT NULL,
  locator_json TEXT NOT NULL, text TEXT NOT NULL, text_sha256 TEXT NOT NULL, PRIMARY KEY(namespace, entry_id)
);
"""


class SanctionsError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _load(value: Any, default: Any) -> Any:
    return default if value in (None, "") else json.loads(value)


def authorize(
    namespace: str, scopes: Iterable[str], required: str, *, write: bool = False
) -> None:
    scopes = set(scopes)
    if "operator" in scopes:
        return
    needed = (
        {f"namespace:{namespace}:write"}
        if write
        else {f"namespace:{namespace}:read", f"namespace:{namespace}:write"}
    )
    if required not in scopes or not needed & scopes:
        raise SanctionsError(
            "unauthorized", f"{required} and namespace access are required"
        )


def record_key(list_id: str, list_entry_id: str) -> str:
    return f"sanctions:{list_id}:{list_entry_id}"


def designation_entity_id(list_id: str, list_entry_id: str) -> str:
    """The identity-decision subject for one designation (per list; never shared between lists)."""
    from src.kb.ownership_store import canonical_entity_id

    return canonical_entity_id(record_key(list_id, list_entry_id))


def normalize_identifier(value: Any) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def _alias_rows(entry: Mapping[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    rows = []
    for name in entry.get("names") or []:
        kind = "transliteration" if name.get("script") else "name"
        rows.append((kind, name["name"], dict(name)))
    for identifier in entry.get("identifiers") or []:
        rows.append((identifier["kind"], identifier["value"], dict(identifier)))
    for value in entry.get("dates_of_birth") or []:
        rows.append(("date_of_birth", value, {"value": value}))
    for value in entry.get("nationalities") or []:
        rows.append(("nationality", value, {"value": value}))
    for address in entry.get("addresses") or []:
        rows.append(
            (
                "address",
                ", ".join(
                    p for p in (address.get("text"), address.get("country")) if p
                ),
                dict(address),
            )
        )
    return rows


def feature_enabled(conn: Any, namespace: str | None = None) -> bool:
    """Whether the Legal bundle's optional ``sanctions`` feature is selected in the active composition plan.

    The feature defaults to off; there is no separate enablement flag. Before
    the Legal bundle is composition-managed nothing selects the feature, so it
    reads as off. Reads only (works on a read-only connection).
    """
    del namespace  # composition selection is deployment-wide
    from src.kb.legal import legal_feature_enabled

    return legal_feature_enabled(conn, "sanctions")


class SanctionsStore:
    def __init__(
        self,
        conn: Any,
        *,
        initialize: bool = True,
        now: Callable[[], int] | None = None,
    ) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def ready(self) -> bool:
        return bool(
            self.conn.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_name='sanctions_designations'"
            ).fetchone()
        )

    # ------------------------------------------------------------------ snapshots

    def apply_snapshot(
        self,
        namespace: str,
        header: Mapping[str, Any],
        entries: Sequence[Mapping[str, Any]],
        *,
        run_id: str,
        source_id: str | None,
        acquired_at_ms: int | None = None,
        legal_namespace: str | None = None,
    ) -> dict[str, Any]:
        """Record one full-list snapshot and derive listing revisions against the previous one.

        Idempotent by ``(list, file digest)``: re-acquiring an unchanged file adds
        nothing, whatever the fetch time. A snapshot older than the latest one
        of its list is refused rather than rewriting the revision chain.
        """
        list_id = str(header.get("list_id") or "")
        if list_id not in LISTS:
            raise SanctionsError("invalid_snapshot", f"unknown list {list_id!r}")
        entries = [dict(e) for e in entries]
        if int(header.get("entry_count", -1)) != len(entries) or any(
            e.get("list_id") != list_id for e in entries
        ):
            raise SanctionsError(
                "incomplete_snapshot",
                "a snapshot must carry every entry of one list; partial "
                "snapshots would read as delistings",
            )
        ids = [str(e["entry_id"]) for e in entries]
        if len(set(ids)) != len(ids):
            raise SanctionsError(
                "invalid_snapshot", "snapshot repeats a per-list identifier"
            )
        published = date.fromisoformat(str(header["publication_date"])).isoformat()
        snapshot_id = (
            "sanctions-snapshot:"
            + digest([namespace, list_id, header["file_sha256"]])[:24]
        )
        if self.conn.execute(
            "SELECT 1 FROM sanctions_snapshots WHERE namespace=? AND snapshot_id=?",
            [namespace, snapshot_id],
        ).fetchone():
            # Unchanged file: no snapshot, revision or alias is added. Acts acquired
            # since may now resolve; that link is not a list statement.
            resolved = self.resolve_legal_bases(
                namespace, legal_namespace=legal_namespace or namespace
            )
            return {
                "snapshot_id": snapshot_id,
                "status": "unchanged",
                "listed": 0,
                "amended": 0,
                "relisted": 0,
                "delisted": 0,
                "legal_bases_resolved": resolved,
            }
        latest = self.conn.execute(
            "SELECT snapshot_id, publication_date, sequence FROM sanctions_snapshots WHERE namespace=? AND list_id=? "
            "ORDER BY sequence DESC LIMIT 1",
            [namespace, list_id],
        ).fetchone()
        if latest and published < latest[1]:
            raise SanctionsError(
                "out_of_order_snapshot",
                "snapshot is older than the latest acquired snapshot of this list",
                latest_publication_date=latest[1],
            )
        acquired = int(acquired_at_ms if acquired_at_ms is not None else self.now())
        previous = latest[0] if latest else None
        counts = {"listed": 0, "amended": 0, "relisted": 0, "delisted": 0}
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT INTO sanctions_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    namespace,
                    snapshot_id,
                    list_id,
                    source_id,
                    header.get("format") or "unknown",
                    published,
                    header["file_sha256"],
                    header.get("entries_sha256") or digest(entries),
                    len(entries),
                    (latest[2] + 1) if latest else 1,
                    previous,
                    run_id,
                    acquired,
                ],
            )
            seen = set()
            for entry in sorted(entries, key=lambda e: e["entry_id"]):
                designation_id = self._designation(
                    namespace, entry, snapshot_id, acquired
                )
                seen.add(designation_id)
                content_hash = digest(entry)
                last = self._latest_revision(namespace, designation_id)
                if last is None:
                    change = "listed"
                elif last["change"] == "delisted":
                    change = "relisted"
                elif last["content_hash"] != content_hash:
                    change = "amended"
                else:
                    change = None
                revision_id = (
                    last["revision_id"]
                    if change is None
                    else self._revision(
                        namespace,
                        designation_id,
                        entry,
                        change,
                        snapshot_id,
                        previous,
                        content_hash,
                        acquired,
                        last,
                    )
                )
                if change:
                    counts[change] += 1
                self.conn.execute(
                    "INSERT INTO sanctions_snapshot_members VALUES (?,?,?,?)",
                    [namespace, snapshot_id, designation_id, revision_id],
                )
                self._context(namespace, entry, snapshot_id)
            for (designation_id,) in self.conn.execute(
                "SELECT designation_id FROM sanctions_designations WHERE namespace=? AND list_id=? "
                "ORDER BY designation_id",
                [namespace, list_id],
            ).fetchall():
                if designation_id in seen:
                    continue
                last = self._latest_revision(namespace, designation_id)
                if last and last["change"] != "delisted":
                    self._revision(
                        namespace,
                        designation_id,
                        None,
                        "delisted",
                        snapshot_id,
                        previous,
                        None,
                        acquired,
                        last,
                    )
                    counts["delisted"] += 1
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        resolved = self.resolve_legal_bases(
            namespace, legal_namespace=legal_namespace or namespace
        )
        return {
            "snapshot_id": snapshot_id,
            "status": "applied",
            "previous_snapshot_id": previous,
            "publication_date": published,
            **counts,
            "legal_bases_resolved": resolved,
        }

    def _designation(
        self, namespace: str, entry: Mapping[str, Any], snapshot_id: str, acquired: int
    ) -> str:
        list_id, entry_id = entry["list_id"], str(entry["entry_id"])
        designation_id = (
            "sanctions-designation:" + digest([namespace, list_id, entry_id])[:24]
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO sanctions_designations VALUES (?,?,?,?,?,?,?,?,?)",
            [
                namespace,
                designation_id,
                list_id,
                entry_id,
                entry.get("party_kind")
                if entry.get("party_kind") in PARTY_KINDS
                else "unknown",
                record_key(list_id, entry_id),
                designation_entity_id(list_id, entry_id),
                snapshot_id,
                acquired,
            ],
        )
        return designation_id

    def _latest_revision(
        self, namespace: str, designation_id: str
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT revision_id, revision_no, change, content_hash FROM sanctions_revisions WHERE namespace=? "
            "AND designation_id=? ORDER BY revision_no DESC LIMIT 1",
            [namespace, designation_id],
        ).fetchone()
        return (
            None
            if row is None
            else dict(
                zip(("revision_id", "revision_no", "change", "content_hash"), row)
            )
        )

    def _revision(
        self,
        namespace,
        designation_id,
        entry,
        change,
        snapshot_id,
        previous,
        content_hash,
        acquired,
        last,
    ) -> str:
        number = 1 if last is None else int(last["revision_no"]) + 1
        revision_id = (
            "sanctions-revision:"
            + digest([namespace, designation_id, number, snapshot_id])[:24]
        )
        list_id = self.conn.execute(
            "SELECT list_id FROM sanctions_designations WHERE namespace=? AND "
            "designation_id=?",
            [namespace, designation_id],
        ).fetchone()[0]
        self.conn.execute(
            "INSERT INTO sanctions_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                namespace,
                revision_id,
                designation_id,
                list_id,
                number,
                change,
                snapshot_id,
                previous,
                content_hash,
                None if entry is None else canonical(entry),
                None if entry is None else entry.get("listed_on"),
                None if entry is None else entry.get("amended_on"),
                acquired,
            ],
        )
        for kind, value, detail in [] if entry is None else _alias_rows(entry):
            alias_id = "sanctions-alias:" + digest([revision_id, kind, detail])[:24]
            self.conn.execute(
                "INSERT OR IGNORE INTO sanctions_aliases VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    namespace,
                    alias_id,
                    revision_id,
                    designation_id,
                    list_id,
                    kind,
                    str(value),
                    normalize_identifier(value),
                    canonical(detail),
                ],
            )
        return revision_id

    def _context(
        self, namespace: str, entry: Mapping[str, Any], snapshot_id: str
    ) -> None:
        list_id = entry["list_id"]
        for programme in entry.get("programmes") or []:
            programme_id = (
                "sanctions-programme:"
                + digest([namespace, list_id, programme["code"]])[:24]
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO sanctions_programmes VALUES (?,?,?,?,?,?)",
                [
                    namespace,
                    programme_id,
                    list_id,
                    programme["code"],
                    programme.get("name"),
                    snapshot_id,
                ],
            )
        for basis in entry.get("legal_basis") or []:
            basis_id = self.basis_id(namespace, list_id, basis)
            self.conn.execute(
                "INSERT OR IGNORE INTO sanctions_legal_bases VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    namespace,
                    basis_id,
                    list_id,
                    basis["citation"],
                    basis.get("celex"),
                    basis.get("eli"),
                    basis.get("url"),
                    "unresolved",
                    None,
                    None,
                    canonical({"reason": "not yet resolved"}),
                    snapshot_id,
                ],
            )

    @staticmethod
    def basis_id(namespace: str, list_id: str, basis: Mapping[str, Any]) -> str:
        return (
            "sanctions-basis:"
            + digest(
                [
                    namespace,
                    list_id,
                    basis.get("citation"),
                    basis.get("celex"),
                    basis.get("eli"),
                ]
            )[:24]
        )

    def resolve_legal_bases(
        self, namespace: str, *, legal_namespace: str | None = None
    ) -> int:
        """Link legal-basis citations to Legal works by exact CELEX/ELI (``lookup_legal_work``); idempotent.

        Resolution is a link beside the list statement, never part of it, so it
        adds no listing revision. An act that is not acquired, or matches more
        than one work, stays ``unresolved`` with the source string.
        """
        legal_namespace = legal_namespace or namespace
        if not self.conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name='legal_works'"
        ).fetchone():
            return 0
        from src.kb.legal import READ_SCOPE as LEGAL_READ
        from src.kb.legal import LegalStore

        legal = LegalStore(self.conn, initialize=False)
        scopes = {LEGAL_READ, f"namespace:{legal_namespace}:read"}
        changed = 0
        rows = self.conn.execute(
            "SELECT basis_id, celex, eli, status, work_id FROM sanctions_legal_bases WHERE namespace=? "
            "ORDER BY basis_id",
            [namespace],
        ).fetchall()
        for basis_id, celex, eli, status, work_id in rows:
            found = None
            for identifier in (celex, eli):
                if not identifier:
                    continue
                result = legal.lookup(
                    legal_namespace, scopes=scopes, identifier=identifier
                )
                if result["status"] == "found":
                    found = (identifier, result["works"][0])
                    break
            if found is None:
                reason = (
                    "no CELEX/ELI in the list citation"
                    if not (celex or eli)
                    else "the act is not acquired in the Legal store (or matches more than one work)"
                )
                if status != "unresolved" or work_id is not None:
                    changed += 1
                self.conn.execute(
                    "UPDATE sanctions_legal_bases SET status='unresolved', work_id=NULL, "
                    "expression_id=NULL, resolution_json=? WHERE namespace=? AND basis_id=?",
                    [canonical({"reason": reason}), namespace, basis_id],
                )
                continue
            identifier, work = found
            expressions = self.conn.execute(
                "SELECT expression_id, language FROM legal_expressions WHERE namespace=? AND work_id=? "
                "ORDER BY CASE WHEN language='en' THEN 0 ELSE 1 END, language, expression_id",
                [legal_namespace, work["work_id"]],
            ).fetchall()
            expression_id = expressions[0][0] if expressions else None
            if status != "resolved" or work_id != work["work_id"]:
                changed += 1
            self.conn.execute(
                "UPDATE sanctions_legal_bases SET status='resolved', work_id=?, expression_id=?, resolution_json=? "
                "WHERE namespace=? AND basis_id=?",
                [
                    work["work_id"],
                    expression_id,
                    canonical(
                        {
                            "basis": "exact identifier via lookup_legal_work",
                            "identifier": identifier,
                            "legal_namespace": legal_namespace,
                            "languages": [e[1] for e in expressions],
                        }
                    ),
                    namespace,
                    basis_id,
                ],
            )
        return changed

    # ------------------------------------------------------------------ control lists (S05)

    def derive_control_entries(
        self,
        namespace: str,
        control_list: str = "eu-dual-use",
        *,
        legal_namespace: str | None = None,
    ) -> list[dict[str, Any]]:
        """Control-list entries of every captured edition, read from the Legal store's located passages."""
        legal_namespace = legal_namespace or namespace
        spec = CONTROL_LISTS[control_list]
        work = self.control_list_work(legal_namespace, control_list)
        if work is None:
            return []
        entries = []
        for version_id, record_json in self.conn.execute(
            "SELECT version_id, record_json FROM legal_versions WHERE namespace=? AND work_id=? AND "
            "content_coverage='captured-text' ORDER BY version_id",
            [legal_namespace, work],
        ).fetchall():
            celex = (_load(record_json, {}).get("fields") or {}).get("celex")
            for locator_key, locator_json, text in self.conn.execute(
                "SELECT locator_key, locator_json, text FROM legal_passages WHERE version_id=? ORDER BY ordinal",
                [version_id],
            ).fetchall():
                locator = _load(locator_json, {})
                if (
                    locator.get("kind") != "control-entry"
                    or locator.get("annex") != spec["annex"]
                ):
                    continue  # the same code in another annex (e.g. Annex IV) is not the control-list entry
                code = str(locator["official_norm_id"])
                entries.append(
                    {
                        "entry_id": "sanctions-control:"
                        + digest([namespace, version_id, code])[:24],
                        "control_list": control_list,
                        "control_code": code,
                        "work_id": work,
                        "version_id": version_id,
                        "edition": celex,
                        "category": locator.get("category"),
                        "locator_key": locator_key,
                        "locator": locator,
                        "text": text,
                        "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                    }
                )
        return entries

    def sync_control_entries(
        self,
        namespace: str,
        control_list: str = "eu-dual-use",
        *,
        legal_namespace: str | None = None,
    ) -> dict[str, Any]:
        """Persist the derived control-list-entry records; idempotent (keyed by edition and code)."""
        entries = self.derive_control_entries(
            namespace, control_list, legal_namespace=legal_namespace
        )
        inserted = 0
        for e in entries:
            inserted += len(
                self.conn.execute(
                    "INSERT OR IGNORE INTO sanctions_control_entries VALUES (?,?,?,?,?,?,?,?,?,?,?,?) RETURNING entry_id",
                    [
                        namespace,
                        e["entry_id"],
                        control_list,
                        e["control_code"],
                        e["work_id"],
                        e["version_id"],
                        e["edition"],
                        e["category"],
                        e["locator_key"],
                        canonical(e["locator"]),
                        e["text"],
                        e["text_sha256"],
                    ],
                ).fetchall()
            )
        return {
            "control_list": control_list,
            "base_celex": CONTROL_LISTS[control_list]["base_celex"],
            "work_id": entries[0]["work_id"]
            if entries
            else self.control_list_work(legal_namespace or namespace, control_list),
            "editions": len({e["version_id"] for e in entries}),
            "entries": len(entries),
            "entries_added": inserted,
        }

    def control_list_work(
        self, legal_namespace: str, control_list: str = "eu-dual-use"
    ) -> str | None:
        if not self.conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name='legal_works'"
        ).fetchone():
            return None
        base = CONTROL_LISTS[control_list]["base_celex"]
        row = self.conn.execute(
            "SELECT work_id FROM legal_works WHERE namespace=? AND native_id=?",
            [legal_namespace, f"consolidated:{base}"],
        ).fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------------ record views

    def snapshot(
        self, namespace: str, snapshot_id: str | None
    ) -> dict[str, Any] | None:
        if snapshot_id is None:
            return None
        row = self.conn.execute(
            "SELECT snapshot_id, list_id, source_id, format, publication_date, file_sha256, entry_count, sequence, "
            "previous_snapshot_id, run_id, acquired_at_ms FROM sanctions_snapshots WHERE namespace=? AND "
            "snapshot_id=?",
            [namespace, snapshot_id],
        ).fetchone()
        if row is None:
            return None
        keys = (
            "snapshot_id",
            "list_id",
            "source_id",
            "format",
            "publication_date",
            "file_sha256",
            "entry_count",
            "sequence",
            "previous_snapshot_id",
            "run_id",
            "acquired_at_ms",
        )
        view = dict(zip(keys, row))
        return {
            "contract": CONTRACT,
            "record_type": "snapshot",
            **view,
            "source_revision": {
                "snapshot_id": view["snapshot_id"],
                "file_sha256": view["file_sha256"],
                "publication_date": view["publication_date"],
            },
        }

    def snapshots(self, namespace: str, list_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT snapshot_id FROM sanctions_snapshots WHERE namespace=? AND list_id=? "
            "ORDER BY sequence",
            [namespace, list_id],
        ).fetchall()
        return [self.snapshot(namespace, r[0]) for r in rows]

    def designation(self, namespace: str, designation_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT designation_id, list_id, list_entry_id, party_kind, record_key, canonical_entity_id, "
            "first_snapshot_id, created_at_ms FROM sanctions_designations WHERE namespace=? AND designation_id=?",
            [namespace, designation_id],
        ).fetchone()
        if row is None:
            raise SanctionsError(
                "not_found", "designation is not visible in this namespace"
            )
        keys = (
            "designation_id",
            "list_id",
            "list_entry_id",
            "party_kind",
            "record_key",
            "canonical_entity_id",
            "first_snapshot_id",
            "acquired_at_ms",
        )
        return {
            "contract": CONTRACT,
            "record_type": "designation",
            **dict(zip(keys, row)),
        }

    def revision(self, namespace: str, revision_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT revision_id, designation_id, list_id, revision_no, change, snapshot_id, previous_snapshot_id, "
            "content_hash, statement_json, listed_on, amended_on, acquired_at_ms FROM sanctions_revisions "
            "WHERE namespace=? AND revision_id=?",
            [namespace, revision_id],
        ).fetchone()
        if row is None:
            raise SanctionsError(
                "not_found", "listing revision is not visible in this namespace"
            )
        keys = (
            "revision_id",
            "designation_id",
            "list_id",
            "revision_no",
            "change",
            "snapshot_id",
            "previous_snapshot_id",
            "content_hash",
            "statement",
            "listed_on",
            "amended_on",
            "acquired_at_ms",
        )
        view = dict(zip(keys, row))
        view["statement"] = _load(view["statement"], None)
        snap = self.snapshot(namespace, view["snapshot_id"])
        prev = self.snapshot(namespace, view["previous_snapshot_id"])
        view["source_revision"] = snap["source_revision"]
        view["compared_snapshots"] = [s["source_revision"] for s in (prev, snap) if s]
        view["source_dates"] = {
            "listed_on": view.pop("listed_on"),
            "amended_on": view.pop("amended_on"),
            "delisted_on": None,
        }
        return {
            "contract": CONTRACT,
            "record_type": "delisting"
            if view["change"] == "delisted"
            else "listing_revision",
            **view,
        }

    def history(self, namespace: str, designation_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT revision_id FROM sanctions_revisions WHERE namespace=? AND designation_id=? "
            "ORDER BY revision_no",
            [namespace, designation_id],
        ).fetchall()
        return [self.revision(namespace, r[0]) for r in rows]

    def aliases(self, namespace: str, revision_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT alias_id, alias_kind, value, detail_json, list_id FROM sanctions_aliases WHERE namespace=? AND "
            "revision_id=? ORDER BY alias_kind, value, alias_id",
            [namespace, revision_id],
        ).fetchall()
        return [
            {
                "contract": CONTRACT,
                "record_type": "identifier_alias",
                "alias_id": r[0],
                "alias_kind": r[1],
                "value": r[2],
                "detail": _load(r[3], {}),
                "list_id": r[4],
                "revision_id": revision_id,
                "assertion": "stated by the list at this revision",
            }
            for r in rows
        ]

    def programme(self, namespace: str, list_id: str, code: str) -> dict[str, Any]:
        programme_id = "sanctions-programme:" + digest([namespace, list_id, code])[:24]
        row = self.conn.execute(
            "SELECT name, first_snapshot_id FROM sanctions_programmes WHERE namespace=? AND "
            "programme_id=?",
            [namespace, programme_id],
        ).fetchone()
        return {
            "contract": CONTRACT,
            "record_type": "programme",
            "programme_id": programme_id,
            "list_id": list_id,
            "code": code,
            "name": row[0] if row else None,
            "first_snapshot_id": row[1] if row else None,
        }

    def legal_basis(
        self, namespace: str, list_id: str, basis: Mapping[str, Any]
    ) -> dict[str, Any]:
        basis_id = self.basis_id(namespace, list_id, basis)
        row = self.conn.execute(
            "SELECT citation, celex, eli, url, status, work_id, expression_id, resolution_json FROM "
            "sanctions_legal_bases WHERE namespace=? AND basis_id=?",
            [namespace, basis_id],
        ).fetchone()
        stored = (
            dict(
                zip(
                    (
                        "citation",
                        "celex",
                        "eli",
                        "url",
                        "status",
                        "work_id",
                        "expression_id",
                        "resolution",
                    ),
                    row,
                )
            )
            if row
            else {"citation": basis.get("citation"), "status": "unresolved"}
        )
        stored["resolution"] = _load(stored.get("resolution"), {}) if row else {}
        return {
            "contract": CONTRACT,
            "record_type": "legal_basis",
            "basis_id": basis_id,
            "list_id": list_id,
            "role": basis.get("role"),
            "source_citation": basis.get("citation"),
            "source_dates": {
                "publication_date": basis.get("publication_date"),
                "entry_into_force": basis.get("entry_into_force"),
            },
            **stored,
        }

    def control_entries(
        self,
        namespace: str,
        control_code: str,
        control_list: str = "eu-dual-use",
        *,
        legal_namespace: str | None = None,
    ) -> list[dict[str, Any]]:
        """Records for one control code in every captured edition (read-only; the same ids ``sync`` persists)."""
        code = str(control_code or "").strip().upper()
        return [
            {
                "contract": CONTRACT,
                "record_type": "control_list_entry",
                **{
                    k: e[k]
                    for k in (
                        "entry_id",
                        "control_list",
                        "control_code",
                        "work_id",
                        "version_id",
                        "edition",
                        "category",
                        "locator",
                        "text",
                        "text_sha256",
                    )
                },
                "source_revision": {
                    "version_id": e["version_id"],
                    "edition": e["edition"],
                },
            }
            for e in sorted(
                self.derive_control_entries(
                    namespace, control_list, legal_namespace=legal_namespace
                ),
                key=lambda e: (e["edition"] or "", e["version_id"]),
            )
            if e["control_code"] == code
        ]


class SanctionsProjector:
    """Source-pack runtime projector for ``noesis-sanctions-record-v1`` pages (one full snapshot per page)."""

    def __init__(self, conn: Any) -> None:
        self.store = SanctionsStore(conn)

    @staticmethod
    def _namespace(source: Mapping[str, Any]) -> str:
        return str(
            dict(source.get("sanctions") or {}).get("namespace") or DEFAULT_NAMESPACE
        )

    def project_page(
        self,
        *,
        run_id,
        manifest,
        source,
        records,
        documents,
        page_receipt,
        principal_id,
    ):
        del manifest, documents, page_receipt, principal_id
        groups: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
        for record in records:
            header, entry = (
                dict(record.get("sanctions_snapshot") or {}),
                record.get("sanctions_entry"),
            )
            if not header or not isinstance(entry, Mapping):
                raise SanctionsError(
                    "invalid_record", "page record is not a sanctions list entry"
                )
            groups.setdefault(header["file_sha256"], (header, []))[1].append(
                dict(entry)
            )
        return [
            self.store.apply_snapshot(
                self._namespace(source),
                header,
                entries,
                run_id=run_id,
                source_id=source["source_id"],
                legal_namespace=str(
                    dict(source.get("sanctions") or {}).get("legal_namespace")
                    or self._namespace(source)
                ),
            )
            for header, entries in groups.values()
        ]

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del run_id, manifest, principal_id
        namespace = self._namespace(source)
        row = self.store.conn.execute(
            "SELECT snapshot_id, publication_date FROM sanctions_snapshots WHERE namespace=? AND source_id=? "
            "ORDER BY sequence DESC LIMIT 1",
            [namespace, source["source_id"]],
        ).fetchone()
        return {
            "status": status,
            "latest_snapshot_id": row[0] if row else None,
            "latest_publication_date": row[1] if row else None,
        }


def readiness(conn: Any) -> dict[str, Any]:
    from src.ingestion.sanctions_sources import PROVIDER_CONTRACTS

    store = SanctionsStore(conn, initialize=False)
    lists = {}
    for provider, contract in PROVIDER_CONTRACTS.items():
        snapshots = 0
        if store.ready():
            snapshots = int(
                conn.execute(
                    "SELECT count(*) FROM sanctions_snapshots WHERE list_id=?",
                    [contract["list_id"]],
                ).fetchone()[0]
            )
        lists[provider] = {
            "list_id": contract["list_id"],
            "access_decision": contract["access_decision"],
            "reason": contract["reason"],
            "snapshots": snapshots,
            "live": "not-implemented"
            if contract["access_decision"] == "not-implemented"
            else "outstanding",
        }
    return {
        "feature": "legal.sanctions",
        "enabled": feature_enabled(conn),
        "store_ready": store.ready(),
        "providers": lists,
        "review_boundary": REVIEW_BOUNDARY,
    }
