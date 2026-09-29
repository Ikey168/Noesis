"""Lobbying and transparency-register declarations: the ``political.lobbying`` record owner (#1911, T02-T05).

Records (contract ``noesis-lobbying-record-v1``), each carrying its register,
native register identifier, register revision, source, observation time and
namespace:

* **export** - one acquired register export or meeting list (publication date,
  file digest, evidence origin); the source revision of everything derived from
  it;
* **registrant** / **meeting** entry - one entry of one register, keyed by the
  register and its native identifier (EU TR identification number,
  Lobbyregister number, UK registrant reference, meeting id). No shared key is
  ever created: one organisation in three registers is three registrants, and
  a cross-register or entity match is a reviewable identity decision
  (:mod:`src.kb.lobbying_identity`);
* **register revision** - what the register stated about the entry, appended
  (never rewritten) when an export adds (``registered``), changes
  (``amended``), re-adds (``reregistered``) or drops or marks it
  (``deregistered``) - a deregistration is a lifecycle revision, not a
  deletion. Each revision references its predecessor and the export it came
  from. A replayed export, or an entry whose statement did not change, adds
  nothing whatever the fetch time; an older export arriving later adds its
  distinct statements as dated observations without deregistering anything;
* **client**, **declared interest**, **spend declaration** (the declared range:
  lower and upper bound as filed, either may be open, currency and period -
  never a midpoint, total or estimate), **declared grant** (a registrant's
  assertion, cross-referenced to funding records only as candidates) and
  **document** (position papers, link-only unless retention allows text) are
  views of one revision.

Conflicting declarations from different registers are separate records with
their own sources. Nothing here states influence, corruption or undeclared
lobbying.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date
from typing import Any

from src.ingestion.lobbying_sources import REGISTERS, REVIEW_BOUNDARY

CONTRACT = "noesis-lobbying-record-v1"
ANSWER_CONTRACT = "noesis-lobbying-answer-v1"
READ_SCOPE = "knowledge:political:lobbying:read"
WRITE_SCOPE = "knowledge:political:lobbying:write"
REVIEW_SCOPE = "knowledge:political:lobbying:review"
DEFAULT_NAMESPACE = "global"
RECORD_TYPES = (
    "export",
    "registrant",
    "meeting",
    "register_revision",
    "client",
    "declared_interest",
    "spend_declaration",
    "declared_grant",
    "document",
)
CHANGES = ("registered", "amended", "reregistered", "deregistered")
LIFECYCLES = ("active", "deregistered")
# Keys that would carry an inference or a collapsed range; no answer may contain them.
FORBIDDEN_ANSWER_KEYS = frozenset(
    {
        "midpoint",
        "total",
        "total_spend",
        "sum",
        "average",
        "mean",
        "estimate",
        "point_estimate",
        "influence",
        "influence_score",
        "corruption",
        "undeclared",
        "risk",
        "risk_score",
        "score",
        "verdict",
    }
)

_DDL = """
CREATE TABLE IF NOT EXISTS lobbying_exports (
  namespace TEXT NOT NULL, export_id TEXT NOT NULL, register TEXT NOT NULL, source_id TEXT, format TEXT NOT NULL,
  publication_date TEXT NOT NULL, file_sha256 TEXT NOT NULL, entries_sha256 TEXT NOT NULL, entry_count INTEGER NOT NULL,
  full_export BOOLEAN NOT NULL, selection_json TEXT, evidence_origin TEXT NOT NULL, url TEXT, sequence INTEGER NOT NULL,
  run_id TEXT NOT NULL, acquired_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, export_id)
);
CREATE TABLE IF NOT EXISTS lobbying_entries (
  namespace TEXT NOT NULL, entry_id TEXT NOT NULL, register TEXT NOT NULL, native_id TEXT NOT NULL,
  entry_kind TEXT NOT NULL, record_key TEXT NOT NULL, entity_id TEXT NOT NULL, first_export_id TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, entry_id)
);
CREATE TABLE IF NOT EXISTS lobbying_revisions (
  namespace TEXT NOT NULL, revision_id TEXT NOT NULL, entry_id TEXT NOT NULL, register TEXT NOT NULL,
  revision_no INTEGER NOT NULL, previous_revision_id TEXT, change TEXT NOT NULL, lifecycle TEXT NOT NULL,
  change_basis TEXT NOT NULL, content_hash TEXT, statement_json TEXT, effective_on TEXT NOT NULL, declared_on TEXT,
  native_version TEXT, export_id TEXT NOT NULL, source_id TEXT, evidence_origin TEXT NOT NULL,
  observed_at_ms BIGINT NOT NULL, export_date TEXT NOT NULL, PRIMARY KEY(namespace, revision_id)
);
CREATE TABLE IF NOT EXISTS lobbying_export_members (
  namespace TEXT NOT NULL, export_id TEXT NOT NULL, entry_id TEXT NOT NULL, revision_id TEXT NOT NULL,
  PRIMARY KEY(namespace, export_id, entry_id, revision_id)
);
"""


# The register's own order of an entry's revisions: register date, then the date of the export that stated it,
# then arrival. A late-arriving older export therefore never becomes the current state.
REGISTER_ORDER = "effective_on DESC, export_date DESC, revision_no DESC"
REGISTER_ORDER_ASC = "effective_on, export_date, revision_no"


class LobbyingError(ValueError):
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
        raise LobbyingError(
            "unauthorized", f"{required} and namespace access are required"
        )


def normalize_name(value: Any) -> str:
    return " ".join(re.sub(r"[^0-9a-z]+", " ", str(value or "").casefold()).split())


def record_key(register: str, native_id: str) -> str:
    return f"lobbying:{register}:{native_id}"


def client_key(register: str, native_id: str, name: Any) -> str:
    return f"lobbying:{register}:{native_id}:client:{normalize_name(name).replace(' ', '-')}"


def entry_entity_id(key: str) -> str:
    """The identity-decision subject for one register record (never shared between registers)."""
    from src.kb.ownership_store import canonical_entity_id

    return canonical_entity_id(key)


def feature_enabled(conn: Any, namespace: str | None = None) -> bool:
    """Whether the Political bundle's optional ``lobbying`` feature is selected in the active composition plan.

    Defaults to off; there is no separate enablement flag. Reads only.
    """
    del namespace  # composition selection is deployment-wide
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_name IN "
                "('composition_authority', 'composition_active', 'composition_generations', 'composition_plans')"
            ).fetchall()
        }
        if len(tables) < 4:
            return False
        managed = conn.execute(
            "SELECT authority FROM composition_authority WHERE bundle='political'"
        ).fetchone()
        if not managed or managed[0] != "composition":
            return False
        row = conn.execute(
            "SELECT p.plan_json FROM composition_active a JOIN composition_generations g "
            "ON g.generation_id=a.generation_id JOIN composition_plans p ON p.digest=g.plan_digest WHERE a.slot=1"
        ).fetchone()
        plan = json.loads(row[0]) if row else {}
    except Exception:  # noqa: BLE001 - an unreadable plan never enables a feature
        return False
    return "lobbying" in ((plan.get("features") or {}).get("political") or [])


def _validate_entry(entry: Mapping[str, Any], register: str) -> dict[str, Any]:
    entry = json.loads(canonical(entry))
    if (
        entry.get("register") != register
        or not str(entry.get("native_id") or "").strip()
    ):
        raise LobbyingError(
            "invalid_export",
            "every entry names the export's register and a native identifier",
        )
    if entry.get("entry_kind") != REGISTERS[register]["kind"]:
        raise LobbyingError("invalid_export", "entry kind does not match its register")
    if entry.get("lifecycle", "active") not in LIFECYCLES:
        raise LobbyingError("invalid_export", "unknown lifecycle state")
    for spend in list(entry.get("spend") or []) + [
        c["spend"] for c in entry.get("clients") or [] if c.get("spend")
    ]:
        if set(spend) & {"midpoint", "total", "estimate", "value"}:
            raise LobbyingError(
                "invalid_export",
                "a spend declaration is a declared range, never a point value",
            )
        if spend.get("lower") is None and spend.get("upper") is None:
            raise LobbyingError(
                "invalid_export", "a spend declaration states at least one bound"
            )
        if not re.fullmatch(r"[A-Z]{3}", str(spend.get("currency") or "")):
            raise LobbyingError(
                "invalid_export", "a spend declaration states its currency"
            )
    return entry


def _effective(entry: Mapping[str, Any], publication_date: str) -> str:
    period = entry.get("period") or {}
    return (
        entry.get("declared_on")
        or entry.get("date")
        or period.get("start")
        or publication_date
    )


class LobbyingStore:
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
                "SELECT 1 FROM information_schema.tables WHERE table_name='lobbying_revisions'"
            ).fetchone()
        )

    # ------------------------------------------------------------------ exports

    def apply_export(
        self,
        namespace: str,
        header: Mapping[str, Any],
        entries: Sequence[Mapping[str, Any]],
        *,
        run_id: str,
        source_id: str | None,
        acquired_at_ms: int | None = None,
    ) -> dict[str, Any]:
        """Record one export and append register revisions for what changed.

        Idempotent by ``(register, file digest, selection)``: re-acquiring an
        unchanged file adds nothing, whatever the fetch time; an unchanged entry
        in a new file adds no revision. An export older than one already
        acquired adds its distinct statements as dated observations but never
        deregisters an entry by absence.
        """
        register = str(header.get("register") or "")
        if register not in REGISTERS:
            raise LobbyingError("invalid_export", f"unknown register {register!r}")
        entries = [_validate_entry(e, register) for e in entries]
        if int(header.get("entry_count", -1)) != len(entries):
            raise LobbyingError(
                "incomplete_export",
                "an export must carry every selected entry; a partial export "
                "would read as deregistrations",
            )
        keys = [(e["native_id"], e.get("native_version")) for e in entries]
        if len(set(keys)) != len(keys):
            raise LobbyingError("invalid_export", "export repeats a native identifier")
        published = date.fromisoformat(str(header["publication_date"])).isoformat()
        selection = sorted(header.get("selection") or []) or None
        origin = "fixture" if header.get("evidence_origin") == "fixture" else "live"
        export_id = (
            "lobbying-export:"
            + digest([namespace, register, header["file_sha256"], selection])[:24]
        )
        if self.conn.execute(
            "SELECT 1 FROM lobbying_exports WHERE namespace=? AND export_id=?",
            [namespace, export_id],
        ).fetchone():
            return {
                "export_id": export_id,
                "status": "unchanged",
                **dict.fromkeys(CHANGES, 0),
            }
        latest = self.conn.execute(
            "SELECT max(publication_date), max(sequence) FROM lobbying_exports WHERE namespace=? AND register=?",
            [namespace, register],
        ).fetchone()
        newest = latest[0] is None or published >= latest[0]
        acquired = int(acquired_at_ms if acquired_at_ms is not None else self.now())
        counts = dict.fromkeys(CHANGES, 0)
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT INTO lobbying_exports VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    namespace,
                    export_id,
                    register,
                    source_id,
                    header.get("format") or "unknown",
                    published,
                    header["file_sha256"],
                    header.get("entries_sha256") or digest(entries),
                    len(entries),
                    bool(header.get("full_export")),
                    None if selection is None else canonical(selection),
                    origin,
                    header.get("url"),
                    int(latest[1] or 0) + 1,
                    run_id,
                    acquired,
                ],
            )
            seen = set()
            for entry in sorted(
                entries,
                key=lambda e: (
                    e["native_id"],
                    str(e.get("native_version") or ""),
                    _effective(e, published),
                ),
            ):
                entry_id = self._entry(namespace, entry, export_id, acquired)
                seen.add(entry_id)
                content_hash = digest(entry)
                revision_id, change = self._observe(
                    namespace,
                    entry_id,
                    entry,
                    content_hash,
                    published,
                    export_id,
                    source_id,
                    origin,
                    acquired,
                    newest=newest,
                )
                if change:
                    counts[change] += 1
                self.conn.execute(
                    "INSERT OR IGNORE INTO lobbying_export_members VALUES (?,?,?,?)",
                    [namespace, export_id, entry_id, revision_id],
                )
            if header.get("full_export") and newest:
                for entry_id, native_id in self.conn.execute(
                    "SELECT entry_id, native_id FROM lobbying_entries WHERE namespace=? AND register=? "
                    "ORDER BY entry_id",
                    [namespace, register],
                ).fetchall():
                    if entry_id in seen or (
                        selection is not None and native_id not in selection
                    ):
                        continue
                    last = self._latest(namespace, entry_id)
                    if last and last["lifecycle"] != "deregistered":
                        self._append(
                            namespace,
                            entry_id,
                            None,
                            None,
                            "deregistered",
                            "deregistered",
                            "absent from a newer full register export",
                            published,
                            None,
                            None,
                            export_id,
                            source_id,
                            origin,
                            acquired,
                            last,
                            published,
                        )
                        counts["deregistered"] += 1
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {
            "export_id": export_id,
            "status": "applied",
            "publication_date": published,
            "evidence_origin": origin,
            "newest": newest,
            **counts,
        }

    def _entry(
        self, namespace: str, entry: Mapping[str, Any], export_id: str, acquired: int
    ) -> str:
        register, native = entry["register"], str(entry["native_id"])
        entry_id = "lobbying-entry:" + digest([namespace, register, native])[:24]
        key = record_key(register, native)
        self.conn.execute(
            "INSERT OR IGNORE INTO lobbying_entries VALUES (?,?,?,?,?,?,?,?,?)",
            [
                namespace,
                entry_id,
                register,
                native,
                entry["entry_kind"],
                key,
                entry_entity_id(key),
                export_id,
                acquired,
            ],
        )
        return entry_id

    def _latest(self, namespace: str, entry_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT revision_id, revision_no, lifecycle, content_hash, effective_on FROM lobbying_revisions "
            f"WHERE namespace=? AND entry_id=? ORDER BY {REGISTER_ORDER} LIMIT 1",
            [namespace, entry_id],
        ).fetchone()
        return (
            None
            if row is None
            else dict(
                zip(
                    (
                        "revision_id",
                        "revision_no",
                        "lifecycle",
                        "content_hash",
                        "effective_on",
                    ),
                    row,
                )
            )
        )

    def _observe(
        self,
        namespace,
        entry_id,
        entry,
        content_hash,
        published,
        export_id,
        source_id,
        origin,
        acquired,
        *,
        newest: bool,
    ):
        last = self._latest(namespace, entry_id)
        # A dated statement already recorded (in any order of arrival) is the same observation; an undated one is
        # compared with the latest statement only, so a change back to earlier content stays visible.
        dated = bool(
            entry.get("declared_on") or entry.get("native_version") or entry.get("date")
        )
        same = self.conn.execute(
            "SELECT revision_id FROM lobbying_revisions WHERE namespace=? AND entry_id=? AND content_hash=? "
            "ORDER BY revision_no DESC LIMIT 1",
            [namespace, entry_id, content_hash],
        ).fetchone()
        lifecycle = entry.get("lifecycle") or "active"
        # An entry that reappears in a newer export after a deregistration is re-registered even when the
        # register repeats its earlier statement unchanged.
        reappears = (
            newest
            and last is not None
            and last["lifecycle"] == "deregistered"
            and lifecycle == "active"
        )
        if (
            same
            and not reappears
            and (dated or (last and last["content_hash"] == content_hash))
        ):
            return same[0], None
        if not newest:
            # A distinct statement from an export older than one already acquired: kept as a dated observation.
            change = "registered" if last is None else "amended"
            if lifecycle == "deregistered":
                change = "deregistered"
            basis = "stated in an export older than one already acquired (dated observation)"
        elif lifecycle == "deregistered":
            change, basis = "deregistered", "register marks the entry deregistered"
        else:
            change = (
                "registered"
                if last is None
                else "reregistered"
                if reappears
                else "amended"
            )
            basis = "stated in the export"
        effective = _effective(entry, published)
        previous = last
        if not newest and last is not None:
            # History only: never dated after the current state, and chained to the revision it follows by date.
            effective = min(effective, last["effective_on"])
            previous = self._before(namespace, entry_id, effective, published)
        if reappears:
            effective = published  # re-listed as of this export, whatever date the repeated statement carries
        if newest and last is not None and not entry.get("period"):
            # A statement from the newest export is never dated before the revision it follows, so it is the
            # one in force from now on even when the register repeats an older date. (A corrected return for an
            # earlier reporting period keeps its own period date.)
            latest_effective = self.conn.execute(
                "SELECT max(effective_on) FROM lobbying_revisions WHERE namespace=? AND entry_id=?",
                [namespace, entry_id],
            ).fetchone()[0]
            effective = max(effective, latest_effective)
        revision_id = self._append(
            namespace,
            entry_id,
            entry,
            content_hash,
            change,
            lifecycle,
            basis,
            effective,
            entry.get("declared_on"),
            entry.get("native_version"),
            export_id,
            source_id,
            origin,
            acquired,
            last,
            published,
            previous=previous,
        )
        return revision_id, change

    def _before(
        self, namespace, entry_id, effective, export_date
    ) -> dict[str, Any] | None:
        """The revision a dated observation follows in the register's own order (None when it is the earliest)."""
        row = self.conn.execute(
            "SELECT revision_id FROM lobbying_revisions WHERE namespace=? AND entry_id=? AND "
            "(effective_on<? OR (effective_on=? AND export_date<=?)) "
            f"ORDER BY {REGISTER_ORDER} LIMIT 1",
            [namespace, entry_id, effective, effective, export_date],
        ).fetchone()
        return None if row is None else {"revision_id": row[0]}

    def _append(
        self,
        namespace,
        entry_id,
        entry,
        content_hash,
        change,
        lifecycle,
        basis,
        effective,
        declared_on,
        native_version,
        export_id,
        source_id,
        origin,
        acquired,
        last,
        export_date,
        *,
        previous: Mapping[str, Any] | None | bool = True,
    ) -> str:
        if previous is True:
            previous = last
        number = 1 + int(
            self.conn.execute(
                "SELECT coalesce(max(revision_no), 0) FROM lobbying_revisions WHERE namespace=? AND entry_id=?",
                [namespace, entry_id],
            ).fetchone()[0]
        )
        revision_id = (
            "lobbying-revision:" + digest([namespace, entry_id, number, export_id])[:24]
        )
        register = self.conn.execute(
            "SELECT register FROM lobbying_entries WHERE namespace=? AND entry_id=?",
            [namespace, entry_id],
        ).fetchone()[0]
        self.conn.execute(
            "INSERT INTO lobbying_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                namespace,
                revision_id,
                entry_id,
                register,
                number,
                None if not previous else previous["revision_id"],
                change,
                lifecycle,
                basis,
                content_hash,
                None if entry is None else canonical(entry),
                effective,
                declared_on,
                native_version,
                export_id,
                source_id,
                origin,
                acquired,
                export_date,
            ],
        )
        return revision_id

    # ------------------------------------------------------------------ record views

    def export(self, namespace: str, export_id: str | None) -> dict[str, Any] | None:
        if export_id is None:
            return None
        row = self.conn.execute(
            "SELECT export_id, register, source_id, format, publication_date, file_sha256, entry_count, full_export, "
            "selection_json, evidence_origin, url, sequence, run_id, acquired_at_ms FROM lobbying_exports "
            "WHERE namespace=? AND export_id=?",
            [namespace, export_id],
        ).fetchone()
        if row is None:
            return None
        keys = (
            "export_id",
            "register",
            "source_id",
            "format",
            "publication_date",
            "file_sha256",
            "entry_count",
            "full_export",
            "selection",
            "evidence_origin",
            "url",
            "sequence",
            "run_id",
            "acquired_at_ms",
        )
        view = dict(zip(keys, row))
        view["selection"] = _load(view["selection"], None)
        return {
            "contract": CONTRACT,
            "record_type": "export",
            "namespace": namespace,
            **view,
        }

    def entry(self, namespace: str, entry_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT entry_id, register, native_id, entry_kind, record_key, entity_id, first_export_id, created_at_ms "
            "FROM lobbying_entries WHERE namespace=? AND entry_id=?",
            [namespace, entry_id],
        ).fetchone()
        if row is None:
            raise LobbyingError(
                "not_found", "register entry is not visible in this namespace"
            )
        view = dict(
            zip(
                (
                    "entry_id",
                    "register",
                    "native_id",
                    "entry_kind",
                    "record_key",
                    "entity_id",
                    "first_export_id",
                    "acquired_at_ms",
                ),
                row,
            )
        )
        return {
            "contract": CONTRACT,
            "record_type": view["entry_kind"],
            "namespace": namespace,
            "register_label": REGISTERS[view["register"]]["label"],
            **view,
        }

    def find_entry(
        self, namespace: str, register: str, native_id: str
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT entry_id FROM lobbying_entries WHERE namespace=? AND register=? AND "
            "native_id=?",
            [namespace, register, native_id],
        ).fetchone()
        return None if row is None else self.entry(namespace, row[0])

    def revision(self, namespace: str, revision_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT revision_id, entry_id, register, revision_no, previous_revision_id, change, lifecycle, "
            "change_basis, content_hash, statement_json, effective_on, declared_on, native_version, export_id, "
            "source_id, evidence_origin, observed_at_ms FROM lobbying_revisions WHERE namespace=? AND revision_id=?",
            [namespace, revision_id],
        ).fetchone()
        if row is None:
            raise LobbyingError(
                "not_found", "register revision is not visible in this namespace"
            )
        keys = (
            "revision_id",
            "entry_id",
            "register",
            "revision_no",
            "previous_revision_id",
            "change",
            "lifecycle",
            "change_basis",
            "content_hash",
            "statement",
            "effective_on",
            "declared_on",
            "native_version",
            "export_id",
            "source_id",
            "evidence_origin",
            "observed_at_ms",
        )
        view = dict(zip(keys, row))
        view["statement"] = _load(view["statement"], None)
        source = self.export(namespace, view["export_id"])
        entry = self.entry(namespace, view["entry_id"])
        view["native_id"] = entry["native_id"]
        view["record_key"] = entry["record_key"]
        view["source_revision"] = {
            "export_id": source["export_id"],
            "file_sha256": source["file_sha256"],
            "publication_date": source["publication_date"],
            "url": source["url"],
            "evidence_origin": source["evidence_origin"],
        }
        return {
            "contract": CONTRACT,
            "record_type": "register_revision",
            "namespace": namespace,
            **view,
        }

    def history(self, namespace: str, entry_id: str) -> list[dict[str, Any]]:
        """Every revision of an entry, in the register's own date order (arrival order breaks ties)."""
        rows = self.conn.execute(
            "SELECT revision_id FROM lobbying_revisions WHERE namespace=? AND entry_id=? "
            f"ORDER BY {REGISTER_ORDER_ASC}",
            [namespace, entry_id],
        ).fetchall()
        return [self.revision(namespace, r[0]) for r in rows]

    def in_force(
        self, namespace: str, entry_id: str, as_of: str | None = None
    ) -> dict[str, Any] | None:
        """The revision in force on a date: the latest by the register's date on or before it (None before the first)."""
        if as_of is not None:
            as_of = date.fromisoformat(str(as_of)[:10]).isoformat()
        row = self.conn.execute(
            "SELECT revision_id FROM lobbying_revisions WHERE namespace=? AND entry_id=? AND (? IS NULL OR "
            f"effective_on<=?) ORDER BY {REGISTER_ORDER} LIMIT 1",
            [namespace, entry_id, as_of, as_of],
        ).fetchone()
        return None if row is None else self.revision(namespace, row[0])

    def entries(
        self, namespace: str, *, register: str | None = None, kind: str | None = None
    ) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT entry_id FROM lobbying_entries WHERE namespace=? AND (? IS NULL OR register=?) AND "
            "(? IS NULL OR entry_kind=?) ORDER BY register, native_id",
            [namespace, register, register, kind, kind],
        ).fetchall()
        return [self.entry(namespace, r[0]) for r in rows]

    # ------------------------------------------------------------------ views of one revision

    @staticmethod
    def _cite(revision: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "register": revision["register"],
            "native_id": revision["native_id"],
            "revision_id": revision["revision_id"],
            "revision_no": revision["revision_no"],
            "native_version": revision["native_version"],
            "effective_on": revision["effective_on"],
            "source_id": revision["source_id"],
            "source_revision": revision["source_revision"],
        }

    def clients(self, revision: Mapping[str, Any]) -> list[dict[str, Any]]:
        statement = revision.get("statement") or {}
        return [
            {
                "contract": CONTRACT,
                "record_type": "client",
                "record_key": client_key(
                    revision["register"], revision["native_id"], client.get("name")
                ),
                "name": client.get("name"),
                "role": client.get("role"),
                "native_ids": client.get("native_ids") or [],
                "declared_range": client.get("spend"),
                "citation": self._cite(revision),
            }
            for client in statement.get("clients") or []
        ]

    def interests(self, revision: Mapping[str, Any]) -> list[dict[str, Any]]:
        statement = revision.get("statement") or {}
        rows = []
        for index, interest in enumerate(statement.get("interests") or []):
            rows.append(
                {
                    "contract": CONTRACT,
                    "record_type": "declared_interest",
                    "interest_key": f"{revision['revision_id']}#interest-{index}",
                    "kind": interest.get("kind"),
                    "text": interest.get("text"),
                    "code": interest.get("code"),
                    "references": interest.get("references") or [],
                    "citation": self._cite(revision),
                }
            )
        if statement.get("entry_kind") == "meeting":
            rows.append(
                {
                    "contract": CONTRACT,
                    "record_type": "declared_interest",
                    "interest_key": f"{revision['revision_id']}#subject",
                    "kind": "meeting_subject",
                    "text": statement.get("subject"),
                    "code": None,
                    "references": statement.get("references") or [],
                    "citation": self._cite(revision),
                }
            )
        return rows

    def spend(self, revision: Mapping[str, Any]) -> list[dict[str, Any]]:
        statement = revision.get("statement") or {}
        return [
            {
                "contract": CONTRACT,
                "record_type": "spend_declaration",
                **item,
                "note": "the declared range as filed; not a point value",
                "citation": self._cite(revision),
            }
            for item in statement.get("spend") or []
        ]

    def grants(self, revision: Mapping[str, Any]) -> list[dict[str, Any]]:
        statement = revision.get("statement") or {}
        return [
            {
                "contract": CONTRACT,
                "record_type": "declared_grant",
                **grant,
                "citation": self._cite(revision),
            }
            for grant in statement.get("grants") or []
        ]

    def documents(self, revision: Mapping[str, Any]) -> list[dict[str, Any]]:
        statement = revision.get("statement") or {}
        return [
            {
                "contract": CONTRACT,
                "record_type": "document",
                **item,
                "citation": self._cite(revision),
            }
            for item in statement.get("documents") or []
        ]

    def meeting(self, revision: Mapping[str, Any]) -> dict[str, Any] | None:
        statement = revision.get("statement") or {}
        if statement.get("entry_kind") != "meeting":
            return None
        return {
            "contract": CONTRACT,
            "record_type": "meeting",
            "official": statement.get("official"),
            "date": statement.get("date"),
            "place": statement.get("place"),
            "subject": statement.get("subject"),
            "organisations": statement.get("organisations") or [],
            "references": statement.get("references") or [],
            "published_on": statement.get("published_on"),
            "lifecycle": revision["lifecycle"],
            "citation": self._cite(revision),
        }

    def grant_candidates(
        self,
        namespace: str,
        revision_id: str,
        *,
        funding_namespace: str,
        scopes: Iterable[str],
    ) -> list[dict[str, Any]]:
        """Funding programme records whose normalized title equals a declared grant's programme: candidates only."""
        scopes = set(scopes)
        revision = self.revision(namespace, revision_id)
        if not self.conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name='funding_opportunities'"
        ).fetchone():
            return []
        from src.kb.funding_opportunities import FundingOpportunityStore

        opportunities = FundingOpportunityStore(self.conn, initialize=False).list(
            funding_namespace, scopes=scopes, kinds=["programme"]
        )
        found = []
        for grant in self.grants(revision):
            wanted = normalize_name(grant.get("programme") or grant.get("source_text"))
            for item in opportunities:
                record = item["record"]
                names = {
                    normalize_name(record.get("title")),
                    normalize_name(record.get("programme")),
                } - {""}
                if wanted and wanted in names:
                    found.append(
                        {
                            "state": "candidate",
                            "confirmed": False,
                            "declared_grant": grant,
                            "funding_record": {
                                "opportunity_id": item.get("opportunity_id"),
                                "revision": item.get("revision"),
                                "title": record.get("title"),
                                "funding_namespace": funding_namespace,
                            },
                            "basis": "equal normalized programme name",
                            "note": "a cross-reference candidate; the registrant's declaration is not confirmed funding",
                        }
                    )
        return found


class LobbyingProjector:
    """Source-pack runtime projector for ``noesis-lobbying-record-v1`` pages (one export per page)."""

    def __init__(self, conn: Any) -> None:
        self.store = LobbyingStore(conn)

    @staticmethod
    def _namespace(source: Mapping[str, Any]) -> str:
        return str(
            dict(source.get("lobbying") or {}).get("namespace") or DEFAULT_NAMESPACE
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
        for item in records:
            header, entry = (
                dict(item.get("lobbying_export") or {}),
                item.get("lobbying_entry"),
            )
            if not header or not isinstance(entry, Mapping):
                raise LobbyingError(
                    "invalid_record", "page record is not a register entry"
                )
            groups.setdefault(header["file_sha256"], (header, []))[1].append(
                dict(entry)
            )
        return [
            self.store.apply_export(
                self._namespace(source),
                header,
                entries,
                run_id=run_id,
                source_id=source["source_id"],
            )
            for header, entries in groups.values()
        ]

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del run_id, manifest, principal_id
        row = self.store.conn.execute(
            "SELECT export_id, publication_date FROM lobbying_exports WHERE namespace=? AND source_id=? "
            "ORDER BY sequence DESC LIMIT 1",
            [self._namespace(source), source["source_id"]],
        ).fetchone()
        return {
            "status": status,
            "latest_export_id": row[0] if row else None,
            "latest_publication_date": row[1] if row else None,
        }


def readiness(conn: Any) -> dict[str, Any]:
    from src.ingestion.lobbying_sources import PROVIDER_CONTRACTS

    store = LobbyingStore(conn, initialize=False)
    providers = {}
    for provider, contract in PROVIDER_CONTRACTS.items():
        exports = 0
        if store.ready() and contract.get("register"):
            exports = int(
                conn.execute(
                    "SELECT count(*) FROM lobbying_exports WHERE register=?",
                    [contract["register"]],
                ).fetchone()[0]
            )
        providers[provider] = {
            "register": contract.get("register"),
            "access_decision": contract["access_decision"],
            "reason": contract["reason"],
            "exports": exports,
            "live": "not-implemented"
            if contract["access_decision"] == "not-implemented"
            else "outstanding",
        }
    return {
        "feature": "political.lobbying",
        "enabled": feature_enabled(conn),
        "store_ready": store.ready(),
        "providers": providers,
        "review_boundary": REVIEW_BOUNDARY,
    }


def forbidden_keys(value: Any, path: str = "$") -> list[str]:
    """Keys anywhere in an answer that would carry an inference or a collapsed spend range."""
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() in FORBIDDEN_ANSWER_KEYS:
                found.append(f"{path}.{key}")
            found += forbidden_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found += forbidden_keys(item, f"{path}[{index}]")
    return found
