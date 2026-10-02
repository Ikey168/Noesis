"""Treaty, treaty-text reference, participant, treaty-action and statement records with revisions (#2581, TR02).

Acquired ``noesis-treaty-record-v2`` records (from
:mod:`src.ingestion.treaties_sources`) are kept per source and record key with
an append-only revision log, following the :mod:`src.kb.entity_history`
pattern of never rewriting what was recorded:

* every record carries its **source** (source id, provider, locator), its
  **record revision** (revision id and number, the depositary's own revision
  stamp such as UNTC "Status as at" or the Council of Europe "Status as of")
  and its **as-of time** (the observation time of the run that stored it);
* a **depositary correction** (a changed date, a corrected action, a new
  footnote) is a new revision that names its predecessor; nothing is
  overwritten;
* a record the source **no longer publishes** in a complete treaty page (a row
  the depositary removed) becomes a ``removed-by-source`` revision carrying
  ``publication_status: no-longer-published``; nothing is deleted, and a later
  page that shows it again is ``republished``;
* a replayed older page (an older depositary stamp) is logged as
  ``older-observation`` and never becomes current; a replayed identical page
  adds nothing.

**Minimisation (TR01) is enforced at write time.** A record with a signatory,
representative or contact field, a participant that is not a state, an
international organisation or the EU, or a statement still carrying contact
details is refused with ``minimisation_violation`` before anything is written.

Reads (:meth:`TreatiesStore.records`, :meth:`~TreatiesStore.history`,
:meth:`~TreatiesStore.as_known_at`, :meth:`~TreatiesStore.as_of_depositary`)
never give legal advice, infer obligations or interpret reservations.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from src.ingestion.treaties_sources import (
    KEY_SEGMENT,
    MINIMISATION,
    MINIMISATION_POLICY,
    PARTICIPANT_TYPES,
    PROVIDERS,
    RECORD_CONTRACT,
    RECORD_KINDS,
    REVIEW_BOUNDARY,
    minimisation_violations,
)

CONTRACT = RECORD_CONTRACT
READ_SCOPE = "knowledge:legal:read"
WRITE_SCOPE = "knowledge:legal:write"
REVIEW_SCOPE = "knowledge:legal:review"
DEFAULT_NAMESPACE = "global"
BUNDLE = "legal"
FEATURES = {"untc": "treaties-untc", "eu-cellar": "treaties-eu", "coe-treaty-office": "treaties-coe"}
CHANGES = ("new", "revised", "unchanged", "older-observation", "removed-by-source", "republished")
EXCLUSIONS = ("legal advice", "inference of obligations or compliance",
              "interpretation of the legal effect of reservations",
              "treaty-text redistribution beyond what each source licenses")
# Keys no answer may carry (#2581 exclusions).
FORBIDDEN_ANSWER_KEYS = frozenset({
    "legal_advice", "advice", "obligation", "obligations", "binding_obligations", "compliance", "compliant",
    "in_compliance", "legal_effect", "reservation_effect", "reservation_valid", "valid_reservation",
    "permissible", "interpretation", "risk_score", "score", "verdict",
})
_DDL = """
CREATE TABLE IF NOT EXISTS treaty_records (
  namespace TEXT NOT NULL, source_id TEXT NOT NULL, record_key TEXT NOT NULL, provider TEXT NOT NULL,
  record_kind TEXT NOT NULL, treaty_key TEXT NOT NULL, participant_key TEXT, current_revision_id TEXT NOT NULL,
  revision_count INTEGER NOT NULL, first_run_id TEXT NOT NULL, first_observed_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, source_id, record_key)
);
CREATE TABLE IF NOT EXISTS treaty_revisions (
  namespace TEXT NOT NULL, revision_id TEXT NOT NULL, source_id TEXT NOT NULL, record_key TEXT NOT NULL,
  revision_no INTEGER NOT NULL, previous_revision_id TEXT, change TEXT NOT NULL, content_hash TEXT NOT NULL,
  native_revision TEXT, revision_order TEXT NOT NULL, publication_status TEXT NOT NULL, record_json TEXT NOT NULL,
  evidence_origin TEXT NOT NULL, run_id TEXT NOT NULL, receipt_id TEXT, observed_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, revision_id)
);
CREATE TABLE IF NOT EXISTS treaty_receipts (
  namespace TEXT NOT NULL, receipt_id TEXT NOT NULL, run_id TEXT NOT NULL, source_id TEXT NOT NULL,
  receipt_json TEXT NOT NULL, outcome_json TEXT NOT NULL, recorded_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, receipt_id)
);
"""
_REVISION_COLUMNS = ("revision_id", "source_id", "record_key", "revision_no", "previous_revision_id", "change",
                     "content_hash", "native_revision", "revision_order", "publication_status", "record_json",
                     "evidence_origin", "run_id", "receipt_id", "observed_at_ms")
_HEAD_COLUMNS = ("provider", "record_kind", "treaty_key", "participant_key", "revision_count", "first_observed_at_ms")


class TreatiesError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def authorize(namespace: str, scopes: Iterable[str], required: str, *, write: bool = False) -> None:
    scopes = set(scopes)
    if "operator" in scopes:
        return
    needed = ({f"namespace:{namespace}:write"} if write
              else {f"namespace:{namespace}:read", f"namespace:{namespace}:write"})
    if required not in scopes or not needed & scopes:
        raise TreatiesError("unauthorized", f"{required} and namespace access are required")


def table_exists(conn: Any, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name=?", [name]).fetchone())


def feature_enabled(conn: Any, feature: str) -> bool:
    """Whether the Legal bundle's optional ``treaties-untc`` / ``treaties-eu`` / ``treaties-coe`` feature is selected.

    Reads the active composition plan only; defaults to off.
    """
    try:
        if not all(table_exists(conn, t) for t in ("composition_authority", "composition_active",
                                                    "composition_generations", "composition_plans")):
            return False
        managed = conn.execute("SELECT authority FROM composition_authority WHERE bundle=?", [BUNDLE]).fetchone()
        if not managed or managed[0] != "composition":
            return False
        row = conn.execute(
            "SELECT p.plan_json FROM composition_active a JOIN composition_generations g "
            "ON g.generation_id=a.generation_id JOIN composition_plans p ON p.digest=g.plan_digest WHERE a.slot=1"
        ).fetchone()
        plan = json.loads(row[0]) if row else {}
    except Exception:  # noqa: BLE001 - an unreadable plan never enables a feature
        return False
    return feature in ((plan.get("features") or {}).get(BUNDLE) or [])


def validate(record: Mapping[str, Any]) -> dict[str, Any]:
    """Structural checks plus the TR01 minimisation guard; returns a canonical copy."""
    record = json.loads(canonical(record))
    if record.get("contract") != CONTRACT or record.get("record_kind") not in RECORD_KINDS:
        raise TreatiesError("invalid_record", "not a treaty record")
    if record.get("provider") not in PROVIDERS:
        raise TreatiesError("invalid_record", "unknown treaty provider")
    key, segment = str(record.get("record_key") or ""), KEY_SEGMENT[record["provider"]]
    if not key.startswith(f"treaties:{segment}:") or not str(record.get("treaty_key") or "").startswith(
            f"treaties:{segment}:treaty:"):
        raise TreatiesError("invalid_record", "record and treaty keys are treaties:<provider>:* keys")
    if not str(record.get("locator") or "").startswith("https://"):
        raise TreatiesError("invalid_record", "every record cites an HTTPS locator")
    if record.get("publication_status") not in {"published", "no-longer-published"}:
        raise TreatiesError("invalid_record", "publication_status is published or no-longer-published")
    if record.get("minimisation") != MINIMISATION_POLICY:
        raise TreatiesError("minimisation_violation", "records declare the TR01 minimisation policy")
    fields = record.get("fields")
    if not isinstance(fields, dict):
        raise TreatiesError("invalid_record", "fields must be an object")
    kind = record["record_kind"]
    if kind in {"treaty-action", "treaty-statement", "participant"} and kind != "treaty-statement" \
            and not record.get("participant_key"):
        raise TreatiesError("invalid_record", f"a {kind} names its participant")
    if kind == "treaty-action" and not fields.get("action_type"):
        raise TreatiesError("invalid_record", "an action states its type as published")
    if kind == "treaty-statement" and not fields.get("text_verbatim"):
        raise TreatiesError("invalid_record", "a statement keeps its text verbatim")
    if kind == "participant" and fields.get("participant_type") not in PARTICIPANT_TYPES:
        raise TreatiesError("minimisation_violation", "participants are states, international organisations or "
                                                      "the EU (TR01)")
    violations = minimisation_violations(record)
    if violations:
        raise TreatiesError("minimisation_violation", "personal fields may not be stored (TR01)", paths=violations)
    for key_ in FORBIDDEN_ANSWER_KEYS:
        if key_ in fields:
            raise TreatiesError("assessment_forbidden", "records hold what a depositary published; no advice, "
                                                        "obligation, compliance or legal-effect field is stored")
    return record


_STAMP_KEYS = frozenset({"evidence_origin", "native_revision", "revision_order"})


def content_hash(record: Mapping[str, Any]) -> str:
    """The digest of what a record states, without the page stamp: an unchanged action is not a new revision just
    because the depositary's status stamp moved on."""
    return digest({k: v for k, v in record.items() if k not in _STAMP_KEYS})


def _view(row: Sequence[Any], head: Mapping[str, Any]) -> dict[str, Any]:
    revision = dict(zip(_REVISION_COLUMNS, row))
    record = json.loads(revision.pop("record_json"))
    return {
        **head, **revision, "record": record,
        "citation": {
            "source_id": revision["source_id"], "provider": record.get("provider"),
            "record_key": revision["record_key"], "revision_id": revision["revision_id"],
            "revision_no": revision["revision_no"], "depositary_revision": revision["native_revision"],
            "publication_status": revision["publication_status"], "locator": record.get("locator"),
            "as_of_ms": revision["observed_at_ms"], "evidence_origin": revision["evidence_origin"],
        },
    }


class TreatiesStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def ready(self) -> bool:
        return table_exists(self.conn, "treaty_revisions")

    # ------------------------------------------------------------------ writes

    def project(self, namespace: str, records: Sequence[Mapping[str, Any]], *, run_id: str, source_id: str,
                receipt: Mapping[str, Any] | None = None, observed_at_ms: int | None = None) -> dict[str, Any]:
        """Append revisions for what changed; idempotent. A complete treaty page also records removals by the source.

        ``receipt['complete_for']`` names the treaties the page states completely: a record of those treaties that
        the page no longer carries gets a ``removed-by-source`` revision (never a deletion).
        """
        checked = [validate(r) for r in records]  # refuse the whole page before writing anything
        keys = [r["record_key"] for r in checked]
        if len(set(keys)) != len(keys):
            raise TreatiesError("invalid_record", "a page repeats a record key")
        observed = int(observed_at_ms if observed_at_ms is not None else self.now())
        receipt = dict(receipt or {})
        receipt_id = "treaty-receipt:" + digest([namespace, source_id, run_id, receipt, keys])[:24]
        counts = dict.fromkeys(CHANGES, 0)
        order = {kind: i for i, kind in enumerate(RECORD_KINDS)}
        complete = {str(t) for t in receipt.get("complete_for") or []}
        self.conn.execute("BEGIN")
        try:
            for record in sorted(checked, key=lambda r: (order[r["record_kind"]], r["record_key"])):
                counts[self._observe(namespace, record, source_id, run_id, receipt_id, observed)] += 1
            page_order = max((str(r.get("revision_order") or "") for r in checked), default="")
            for treaty in sorted(complete):
                counts["removed-by-source"] += self._removals(namespace, source_id, treaty, set(keys), page_order,
                                                              run_id, receipt_id, observed)
            self.conn.execute("INSERT OR IGNORE INTO treaty_receipts VALUES (?,?,?,?,?,?,?)",
                              [namespace, receipt_id, run_id, source_id, canonical(receipt), canonical(counts),
                               observed])
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {"receipt_id": receipt_id, "counts": counts, "records": len(checked)}

    def _head(self, namespace: str, source_id: str, key: str) -> tuple | None:
        return self.conn.execute(
            "SELECT r.current_revision_id, r.revision_count, v.content_hash, v.revision_order, v.publication_status "
            "FROM treaty_records r JOIN treaty_revisions v ON v.namespace=r.namespace AND "
            "v.revision_id=r.current_revision_id WHERE r.namespace=? AND r.source_id=? AND r.record_key=?",
            [namespace, source_id, key]).fetchone()

    def _append(self, namespace, source_id, record, head, change, run_id, receipt_id, observed, origin) -> None:
        key = record["record_key"]
        hashed = content_hash(record)
        revision_no = 1 if head is None else int(head[1]) + 1
        revision_id = "treaty-rev:" + digest([namespace, source_id, key, revision_no, hashed])[:24]
        self.conn.execute(
            "INSERT INTO treaty_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, revision_id, source_id, key, revision_no, None if head is None else head[0], change,
             hashed, record.get("native_revision"), str(record.get("revision_order") or ""),
             record["publication_status"], canonical(record), origin, run_id, receipt_id, observed])
        if head is None:
            self.conn.execute("INSERT INTO treaty_records VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                              [namespace, source_id, key, record["provider"], record["record_kind"],
                               record["treaty_key"], record.get("participant_key"), revision_id, 1, run_id, observed])
        elif change == "older-observation":
            self.conn.execute("UPDATE treaty_records SET revision_count=? WHERE namespace=? AND source_id=? AND "
                              "record_key=?", [revision_no, namespace, source_id, key])
        else:
            self.conn.execute("UPDATE treaty_records SET current_revision_id=?, revision_count=?, participant_key=? "
                              "WHERE namespace=? AND source_id=? AND record_key=?",
                              [revision_id, revision_no, record.get("participant_key"), namespace, source_id, key])

    def _observe(self, namespace, record, source_id, run_id, receipt_id, observed) -> str:
        key = record["record_key"]
        origin = "fixture" if record.get("evidence_origin") == "fixture" else "live"
        hashed = content_hash(record)
        head = self._head(namespace, source_id, key)
        order = str(record.get("revision_order") or "")
        if head is not None:
            if head[2] == hashed:
                return "unchanged"
            if self.conn.execute("SELECT 1 FROM treaty_revisions WHERE namespace=? AND source_id=? AND record_key=? "
                                 "AND content_hash=?", [namespace, source_id, key, hashed]).fetchone() \
                    and order <= str(head[3] or ""):
                return "unchanged"  # a replayed older response: already on record, never re-applied
            if order and head[3] and order < str(head[3]):
                change = "older-observation"
            elif head[4] == "no-longer-published":
                change = "republished"
            else:
                change = "revised"
        else:
            change = "new"
        self._append(namespace, source_id, record, head, change, run_id, receipt_id, observed, origin)
        return change

    def _removals(self, namespace, source_id, treaty, present, page_order, run_id, receipt_id, observed) -> int:
        rows = self.conn.execute(
            "SELECT r.record_key, v.record_json, v.revision_order, v.publication_status FROM treaty_records r JOIN "
            "treaty_revisions v ON v.namespace=r.namespace AND v.revision_id=r.current_revision_id WHERE "
            "r.namespace=? AND r.source_id=? AND r.treaty_key=? ORDER BY r.record_key",
            [namespace, source_id, treaty]).fetchall()
        removed = 0
        for key, record_json, order, status in rows:
            if key in present or status == "no-longer-published" or (page_order and order and page_order < order):
                continue
            record = json.loads(record_json)
            record["publication_status"] = "no-longer-published"
            record["native_revision"] = page_order or record.get("native_revision")
            record["revision_order"] = page_order or record.get("revision_order") or ""
            record["removal"] = {"basis": "absent from a complete page of the same treaty", "run_id": run_id}
            head = self._head(namespace, source_id, key)
            self._append(namespace, source_id, record, head, "removed-by-source", run_id, receipt_id, observed,
                         record.get("evidence_origin") or "live")
            removed += 1
        return removed

    # ------------------------------------------------------------------ reads

    def _heads(self, namespace: str, where: str, params: Sequence[Any]) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT " + ", ".join(f"r.{c}" for c in _HEAD_COLUMNS) + ", " +
            ", ".join(f"v.{c}" for c in _REVISION_COLUMNS) +
            " FROM treaty_records r JOIN treaty_revisions v ON v.namespace=r.namespace AND "
            "v.revision_id=r.current_revision_id WHERE r.namespace=? " + where +
            " ORDER BY r.record_kind, r.record_key, r.source_id", [namespace, *params]).fetchall()
        return [_view(row[len(_HEAD_COLUMNS):], dict(zip(_HEAD_COLUMNS, row[:len(_HEAD_COLUMNS)]))) for row in rows]

    def records(self, namespace: str, *, scopes: Iterable[str], kinds: Iterable[str] | None = None,
                treaty_key: str | None = None, participant_key: str | None = None,
                record_keys: Iterable[str] | None = None, providers: Iterable[str] | None = None,
                include_removed: bool = False) -> list[dict[str, Any]]:
        """Current revision of every matching record (per source); removed-by-source records only on request."""
        authorize(namespace, set(scopes), READ_SCOPE)
        if not self.ready():
            return []
        where, params = [], []
        for column, value in (("r.treaty_key", treaty_key), ("r.participant_key", participant_key)):
            if value is not None:
                where.append(f"AND {column}=?")
                params.append(value)
        for column, values in (("r.record_kind", kinds), ("r.record_key", record_keys), ("r.provider", providers)):
            if values is not None:
                values = sorted(set(values))
                if not values:
                    return []
                where.append(f"AND {column} IN (" + ",".join("?" * len(values)) + ")")
                params += values
        if not include_removed:
            where.append("AND v.publication_status='published'")
        return self._heads(namespace, " ".join(where), params)

    def history(self, namespace: str, record_key: str, *, scopes: Iterable[str], source_id: str | None = None
                ) -> list[dict[str, Any]]:
        """Every revision of a record in arrival order, including older observations and removals by the source."""
        authorize(namespace, set(scopes), READ_SCOPE)
        if not self.ready():
            return []
        rows = self.conn.execute(
            "SELECT " + ", ".join(_REVISION_COLUMNS) + " FROM treaty_revisions WHERE namespace=? AND record_key=? "
            "AND (? IS NULL OR source_id=?) ORDER BY source_id, revision_no",
            [namespace, record_key, source_id, source_id]).fetchall()
        return [_view(row, {}) for row in rows]

    def revision(self, namespace: str, revision_id: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        authorize(namespace, set(scopes), READ_SCOPE)
        row = self.conn.execute("SELECT " + ", ".join(_REVISION_COLUMNS) + " FROM treaty_revisions WHERE "
                                "namespace=? AND revision_id=?", [namespace, revision_id]).fetchone() \
            if self.ready() else None
        if row is None:
            raise TreatiesError("not_found", "revision is not visible in this namespace")
        return _view(row, {})

    def as_known_at(self, namespace: str, record_key: str, known_at_ms: int, *, scopes: Iterable[str]
                    ) -> dict[str, Any] | None:
        """The revision that was current at a record time (the latest non-older revision observed by then)."""
        candidates = [r for r in self.history(namespace, record_key, scopes=scopes)
                      if int(r["observed_at_ms"]) <= int(known_at_ms) and r["change"] != "older-observation"]
        return candidates[-1] if candidates else None

    def as_of_depositary(self, namespace: str, record_key: str, as_of: str, *, scopes: Iterable[str]
                         ) -> dict[str, Any] | None:
        """The revision the depositary had published by a date (its own revision stamp on or before it)."""
        day = str(as_of)[:10]
        candidates = [r for r in self.history(namespace, record_key, scopes=scopes)
                      if r["native_revision"] and str(r["native_revision"])[:10] <= day]
        candidates.sort(key=lambda r: (str(r["revision_order"]), int(r["revision_no"])))
        return candidates[-1] if candidates else None

    def receipts(self, namespace: str, run_id: str | None = None, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not self.ready():
            return []
        rows = self.conn.execute(
            "SELECT receipt_id, run_id, source_id, receipt_json, outcome_json, recorded_at_ms FROM treaty_receipts "
            "WHERE namespace=? AND (? IS NULL OR run_id=?) ORDER BY recorded_at_ms, receipt_id",
            [namespace, run_id, run_id]).fetchall()
        return [{"receipt_id": r[0], "run_id": r[1], "source_id": r[2], "receipt": json.loads(r[3]),
                 "counts": json.loads(r[4]), "recorded_at_ms": r[5]} for r in rows]


class TreatiesProjector:
    """Source-pack runtime projector for ``noesis-treaty-record-v2`` pages (one treaty per page)."""

    def __init__(self, conn: Any) -> None:
        self.store = TreatiesStore(conn)

    @staticmethod
    def _namespace(source: Mapping[str, Any]) -> str:
        return str(dict(source.get("treaties") or {}).get("namespace") or DEFAULT_NAMESPACE)

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        del manifest, principal_id
        from src.ingestion.source_packs import SourcePackError

        items = []
        for item in records:
            record = item.get("treaty_record")
            if not isinstance(record, Mapping):
                raise SourcePackError("mapping_failed", "page record is not a treaty record")
            items.append(dict(record))
        observed = max((int(d["ingested_at"]) for d in documents or [] if d.get("ingested_at") is not None),
                       default=None)
        try:
            return [self.store.project(self._namespace(source), items, run_id=run_id, source_id=source["source_id"],
                                       receipt=dict(page_receipt or {}), observed_at_ms=observed)]
        except TreatiesError as exc:
            raise SourcePackError("mapping_failed", f"{exc.code}: {exc}") from exc

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del manifest, principal_id
        rows = self.store.conn.execute(
            "SELECT count(*) FROM treaty_receipts WHERE namespace=? AND run_id=? AND source_id=?",
            [self._namespace(source), run_id, source["source_id"]]).fetchone()
        return {"status": status, "units": int(rows[0])}


def readiness(conn: Any) -> dict[str, Any]:
    from src.ingestion.treaties_sources import (
        BOUNDED_COVERAGE,
        LIVE_VERIFICATION,
        PROVIDER_CONTRACTS,
    )

    store = TreatiesStore(conn, initialize=False)
    counts: dict[str, int] = {}
    if store.ready():
        counts = {p: int(c) for p, c in conn.execute(
            "SELECT provider, count(*) FROM treaty_records GROUP BY provider").fetchall()}
    return {
        "provider": "legal.treaties",
        "enabled": {provider: feature_enabled(conn, feature) for provider, feature in FEATURES.items()},
        "features": FEATURES, "store_ready": store.ready(),
        "providers": {p: {"access_decision": c["access_decision"], "live": LIVE_VERIFICATION[p]["status"],
                          "records": counts.get(p, 0)} for p, c in PROVIDER_CONTRACTS.items()},
        "bounded_coverage": BOUNDED_COVERAGE, "minimisation": MINIMISATION["policy"],
        "review_boundary": REVIEW_BOUNDARY,
        "notice": "unverified-live providers have authored fixture evidence only; a dated live run is outstanding "
                  "(#2645)",
    }


def forbidden_keys(value: Any, path: str = "$") -> list[str]:
    """Keys anywhere in an answer that would carry advice, an obligation or compliance reading or a legal effect."""
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
