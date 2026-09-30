"""Real-estate transaction, parcel, parcel-revision and price-index observation records (#2228, RE02 #2466).

Owns ``noesis-real-estate-record-v1`` for the Geospatial ``real-estate``
feature. It extends the housing record owner (:mod:`src.kb.housing`): the same
scopes (``knowledge:housing:*``), the same namespace model and the same rule
that geometries live in the Geospatial stores, which a parcel revision
*references* (feature, feature revision and geometry ids, source CRS and the
recorded coordinate transform) and never copies. Tables are
``real_estate_*`` in the same connection; there is no separate store.

Record types:

* **transaction** - source, source transaction id, price exactly as published
  (text, decimal, currency), transfer date, property type and tenure or DVF
  mutation nature as published, address or parcel references, vintage and
  receipt. No owner, buyer, seller or party field exists; one is refused.
* **parcel** and its **revisions** - cadastral identifier (INSPIRE localId and
  namespace, nationalCadastralReference), the Geospatial geometry reference,
  source CRS and valid-from (beginLifespanVersion) as published. A changed
  geometry or reference adds a revision.
* **price_index_observation** - index id, geography, period, value, unit,
  base period and edition as published, with the release vintage.

Revisions never overwrite: an entity is its provider, record type and native
key; a differing reading is a new immutable revision (``supersedes`` the one
before) and an identical reading adds nothing, whatever the vintage. Publisher
corrections and deletions are revisions: a PPD ``C`` row is a *changed*
revision, a ``D`` row a *withdrawn* revision (history kept), and a DVF mutation
absent from the same commune-year file of a later release a dated *removed*
revision. Every revision is *known from* its release's publication date (or its
retrieval day when a source publishes none), which is what as-of answers use.
Every release read is kept as a vintage row, even when nothing changed.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from src.ingestion.real_estate_sources import (
    EXCLUSIONS,
    LIVE_VERIFICATION,
    PARTY_COLUMNS,
    PROVIDER_CONTRACTS,
    REUSE_CONDITIONS,
    TARGET_SCHEMA,
)

CONTRACT = TARGET_SCHEMA
READ_SCOPE = "knowledge:housing:read"
WRITE_SCOPE = "knowledge:housing:write"
REVIEW_SCOPE = "knowledge:housing:review"
DEFAULT_NAMESPACE = "global"
RECORD_TYPES = ("transaction", "parcel", "price_index_observation")
EVENTS = ("added", "changed", "withdrawn", "removed", "observed")
# Keys that would carry a valuation, an estimate, a derived price or a person.
FORBIDDEN_KEYS = frozenset({
    "estimated_value", "market_value", "valuation", "price_estimate", "price_per_m2", "price_per_sqm",
    "interpolated_value", "average_value", "apportioned_value", "investment_score", "recommendation",
    "owner_name", "buyer_name", "seller_name", "party_name",
}) | PARTY_COLUMNS
TABLES = ("real_estate_records", "real_estate_revisions", "real_estate_vintages", "real_estate_snapshots",
          "real_estate_source_runs", "real_estate_projection_outcomes")
_DDL = """
CREATE SEQUENCE IF NOT EXISTS real_estate_seq;
CREATE TABLE IF NOT EXISTS real_estate_records (
  namespace TEXT NOT NULL, record_id TEXT NOT NULL, record_type TEXT NOT NULL, provider TEXT NOT NULL,
  record_key TEXT NOT NULL, selection_key TEXT, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, record_id)
);
CREATE TABLE IF NOT EXISTS real_estate_revisions (
  namespace TEXT NOT NULL, revision_id TEXT NOT NULL, record_id TEXT NOT NULL, seq BIGINT NOT NULL,
  revision_no INTEGER NOT NULL, content_sha TEXT NOT NULL, statement_json TEXT NOT NULL, event TEXT NOT NULL,
  release TEXT, published_on TEXT, known_from TEXT NOT NULL, supersedes TEXT, observed_at_ms BIGINT NOT NULL,
  run_id TEXT, source_id TEXT, evidence_origin TEXT, PRIMARY KEY(namespace, revision_id)
);
CREATE TABLE IF NOT EXISTS real_estate_vintages (
  namespace TEXT NOT NULL, vintage_id TEXT NOT NULL, source_id TEXT NOT NULL, provider TEXT NOT NULL,
  release TEXT NOT NULL, published_on TEXT, basis TEXT, record_count INTEGER NOT NULL, changed INTEGER NOT NULL,
  file_sha256 TEXT, evidence_origin TEXT, run_id TEXT, seq BIGINT NOT NULL, observed_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, vintage_id)
);
CREATE TABLE IF NOT EXISTS real_estate_snapshots (
  namespace TEXT NOT NULL, snapshot_id TEXT NOT NULL, selection_key TEXT NOT NULL, release TEXT NOT NULL,
  members_json TEXT NOT NULL, removed INTEGER NOT NULL, run_id TEXT, observed_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, snapshot_id)
);
CREATE TABLE IF NOT EXISTS real_estate_source_runs (
  namespace TEXT NOT NULL, run_id TEXT NOT NULL, source_id TEXT NOT NULL, provider TEXT, status TEXT NOT NULL,
  outcomes_json TEXT NOT NULL, cutoff_seq BIGINT NOT NULL, finished_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, run_id, source_id)
);
CREATE TABLE IF NOT EXISTS real_estate_projection_outcomes (
  namespace TEXT NOT NULL, run_id TEXT NOT NULL, source_id TEXT NOT NULL, item_key TEXT NOT NULL,
  outcome TEXT NOT NULL, reason TEXT NOT NULL, detail_json TEXT NOT NULL, recorded_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, run_id, source_id, item_key, reason)
);
"""


class RealEstateError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def table_exists(conn: Any, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name=?", [name]).fetchone())


def day(ms: int | None) -> str | None:
    return None if ms is None else datetime.fromtimestamp(ms / 1000, tz=UTC).date().isoformat()


def authorize(namespace: str, scopes: Iterable[str], required: str, *, write: bool = False) -> None:
    """Housing scope plus namespace access (operator bypasses), as for every housing record."""
    scopes = set(scopes or ())
    if "operator" in scopes:
        return
    needed = ({f"namespace:{namespace}:write"} if write
              else {f"namespace:{namespace}:read", f"namespace:{namespace}:write"})
    if required not in scopes or not needed & scopes:
        raise RealEstateError("unauthorized", f"{required} and namespace access are required")


def forbidden_keys(value: Any, path: str = "$") -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).casefold() in FORBIDDEN_KEYS:
                found.append(f"{path}.{key}")
            found += forbidden_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found += forbidden_keys(item, f"{path}[{index}]")
    return found


def validate_statement(statement: Mapping[str, Any]) -> dict[str, Any]:
    value = json.loads(canonical(statement))
    if value.get("contract") != CONTRACT:
        raise RealEstateError("invalid_record", f"statement is not {CONTRACT}")
    if value.get("record_type") not in RECORD_TYPES or value.get("event") not in EVENTS:
        raise RealEstateError("invalid_record", "unknown record type or event")
    if not value.get("provider") or not value.get("record_key"):
        raise RealEstateError("invalid_record", "a statement names its provider and native key")
    published = dict(value.get("as_published") or {})
    if value["record_type"] == "transaction":
        if not published.get("source_transaction_id") or "price" not in published:
            raise RealEstateError("invalid_record", "a transaction keeps its source id and published price")
        if published["price"].get("currency") is None:
            raise RealEstateError("invalid_record", "a published price keeps its currency")
    elif value["record_type"] == "parcel":
        if not published.get("local_id") or "geometry" not in published:
            raise RealEstateError("invalid_record", "a parcel keeps its identifier and geometry reference")
    else:
        for key in ("index_id", "geography", "period", "value", "unit"):
            if key not in published:
                raise RealEstateError("invalid_record", f"an index observation keeps {key}")
    bad = forbidden_keys(value)
    if bad:
        raise RealEstateError("forbidden_field", "valuations, derived prices and party names are never stored",
                              paths=bad)
    if not dict(value.get("source") or {}).get("url"):
        raise RealEstateError("invalid_record", "a statement cites its source URL")
    return value


def record_id_for(namespace: str, provider: str, record_type: str, record_key: str) -> str:
    return "real-estate-record:" + digest([namespace, provider, record_type, record_key])[:24]


def _content(value: Mapping[str, Any]) -> dict[str, Any]:
    """What makes a revision distinct: published content, event and references - never the vintage or fetch time."""
    return {"as_published": value["as_published"], "event": value["event"], "place_refs": value["place_refs"],
            "parcel_refs": value["parcel_refs"]}


def feature_enabled(conn: Any) -> bool:
    """Whether the Geospatial bundle's optional ``real-estate`` feature is selected in the active plan."""
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name IN ('composition_authority', "
            "'composition_active', 'composition_generations', 'composition_plans')").fetchall()}
        if len(tables) < 4:
            return False
        row = conn.execute(
            "SELECT p.plan_json FROM composition_active a JOIN composition_generations g ON g.generation_id="
            "a.generation_id JOIN composition_plans p ON p.digest=g.plan_digest WHERE a.slot=1").fetchone()
        plan = json.loads(row[0]) if row else {}
    except Exception:  # noqa: BLE001 - an unreadable plan never enables a feature
        return False
    return "real-estate" in ((plan.get("features") or {}).get("geospatial") or [])


class RealEstateStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            for sql in _DDL.split(";"):
                if sql.strip():
                    conn.execute(sql)

    def ready(self) -> bool:
        return table_exists(self.conn, "real_estate_revisions")

    def require_ready(self) -> None:
        if not self.ready():
            raise RealEstateError("not_ready", "no real-estate record has been acquired yet")

    def _seq(self) -> int:
        return int(self.conn.execute("SELECT nextval('real_estate_seq')").fetchone()[0])

    # ------------------------------------------------------------------ writes

    def _apply(self, namespace: str, statement: Mapping[str, Any], *, run_id: str | None, source_id: str | None,
               observed_at_ms: int) -> dict[str, Any]:
        value = validate_statement(statement)
        record_id = record_id_for(namespace, value["provider"], value["record_type"], value["record_key"])
        self.conn.execute("INSERT OR IGNORE INTO real_estate_records VALUES (?,?,?,?,?,?,?)",
                          [namespace, record_id, value["record_type"], value["provider"], value["record_key"],
                           value.get("selection_key"), observed_at_ms])
        content_sha = digest(_content(value))
        latest = self.conn.execute(
            "SELECT content_sha, revision_no, revision_id FROM real_estate_revisions WHERE namespace=? AND "
            "record_id=? ORDER BY revision_no DESC LIMIT 1", [namespace, record_id]).fetchone()
        if latest is not None and latest[0] == content_sha:
            return {"record_id": record_id, "revision_id": latest[2], "status": "unchanged"}
        number = (int(latest[1]) if latest else 0) + 1
        revision_id = "real-estate-revision:" + digest([record_id, content_sha, number])[:24]
        vintage = dict(value.get("vintage") or {})
        known_from = vintage.get("published_on") or day(observed_at_ms)
        self.conn.execute(
            "INSERT INTO real_estate_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, revision_id, record_id, self._seq(), number, content_sha, canonical(value), value["event"],
             vintage.get("release"), vintage.get("published_on"), known_from, latest[2] if latest else None,
             observed_at_ms, run_id, source_id, dict(value.get("source") or {}).get("evidence_origin")])
        return {"record_id": record_id, "revision_id": revision_id, "revision_no": number,
                "status": "created" if number == 1 else "revised"}

    def observe(self, namespace: str, statements: Sequence[Mapping[str, Any]], *, run_id: str | None = None,
                source_id: str | None = None, observed_at_ms: int | None = None,
                transaction: bool = True) -> dict[str, Any]:
        """Keep a batch of statements (one transaction unless the caller holds one); replays add nothing."""
        observed = observed_at_ms if observed_at_ms is not None else self.now()
        counts = {"created": 0, "revised": 0, "unchanged": 0}
        results = []
        if transaction:
            self.conn.execute("BEGIN")
        try:
            for item in statements:
                result = self._apply(namespace, item, run_id=run_id, source_id=source_id, observed_at_ms=observed)
                counts[result["status"]] += 1
                results.append(result)
            if transaction:
                self.conn.execute("COMMIT")
        except Exception:
            if transaction:
                self.conn.execute("ROLLBACK")
            raise
        return {"counts": counts, "results": results}

    def record_vintage(self, namespace: str, *, source_id: str, provider: str, release: Mapping[str, Any],
                       record_count: int, changed: int, file_sha256: str | None, evidence_origin: str | None,
                       run_id: str | None) -> dict[str, Any]:
        """One row per release read (a new vintage even when nothing in it changed); a re-read adds nothing."""
        vintage_id = "real-estate-vintage:" + digest([namespace, source_id, release.get("release"),
                                                      file_sha256])[:24]
        if self.conn.execute("SELECT 1 FROM real_estate_vintages WHERE namespace=? AND vintage_id=?",
                             [namespace, vintage_id]).fetchone():
            return {"vintage_id": vintage_id, "status": "unchanged"}
        self.conn.execute(
            "INSERT INTO real_estate_vintages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, vintage_id, source_id, provider, str(release.get("release")), release.get("published_on"),
             release.get("basis"), int(record_count), int(changed), file_sha256, evidence_origin, run_id, self._seq(),
             self.now()])
        return {"vintage_id": vintage_id, "status": "recorded"}

    def close_snapshot(self, namespace: str, *, selection_key: str, release: Mapping[str, Any],
                       present_record_ids: Iterable[str], run_id: str | None, source_id: str | None) -> dict[str, Any]:
        """A DVF commune-year file of one release: mutations of an earlier release now absent get a removal."""
        present = sorted(set(present_record_ids))
        label = str(release.get("release"))
        rows = self.conn.execute(
            "SELECT release, members_json FROM real_estate_snapshots WHERE namespace=? AND selection_key=? "
            "ORDER BY release DESC", [namespace, selection_key]).fetchall()
        if any(r[0] == label for r in rows):
            return {"status": "unchanged", "removed": []}
        earlier = [r for r in rows if r[0] < label]
        removed = []
        if earlier:
            observed = self.now()
            for record_id in sorted(set(json.loads(earlier[0][1])) - set(present)):
                latest = self.revisions(namespace, record_id)[-1]
                if latest["event"] == "removed":
                    continue
                tombstone = {**latest["statement"], "event": "removed", "vintage": dict(release)}
                tombstone["as_published"] = {**latest["statement"]["as_published"],
                                             "removal": f"absent from the {selection_key} file of release {label}; "
                                                        "earlier revisions are kept"}
                self._apply(namespace, tombstone, run_id=run_id, source_id=source_id, observed_at_ms=observed)
                removed.append(record_id)
        snapshot_id = "real-estate-snapshot:" + digest([namespace, selection_key, label])[:24]
        self.conn.execute("INSERT INTO real_estate_snapshots VALUES (?,?,?,?,?,?,?,?)",
                          [namespace, snapshot_id, selection_key, label, canonical(present), len(removed), run_id,
                           self.now()])
        return {"status": "recorded", "snapshot_id": snapshot_id, "removed": removed}

    def record_outcome(self, namespace, run_id, source_id, item_key, outcome, reason, detail) -> None:
        self.conn.execute("INSERT OR IGNORE INTO real_estate_projection_outcomes VALUES (?,?,?,?,?,?,?,?)",
                          [namespace, run_id, source_id, item_key, outcome, reason, canonical(detail), self.now()])

    def record_run(self, namespace: str, run_id: str, source_id: str, *, provider: str | None, status: str,
                   outcomes: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        cutoff = self.generation(namespace)
        self.conn.execute("INSERT OR REPLACE INTO real_estate_source_runs VALUES (?,?,?,?,?,?,?,?)",
                          [namespace, run_id, source_id, provider, status, canonical(list(outcomes)), cutoff,
                           self.now()])
        return {"namespace": namespace, "run_id": run_id, "source_id": source_id, "status": status,
                "cutoff_seq": cutoff, "outcomes": list(outcomes)}

    # ------------------------------------------------------------------ reads

    _COLUMNS = ("revision_id, record_id, seq, revision_no, content_sha, statement_json, event, release, "
                "published_on, known_from, supersedes, observed_at_ms, run_id, source_id, evidence_origin")

    @staticmethod
    def _revision(row: Sequence[Any]) -> dict[str, Any]:
        return {"revision_id": row[0], "record_id": row[1], "seq": int(row[2]), "revision_no": int(row[3]),
                "content_sha": row[4], "statement": json.loads(row[5]), "event": row[6], "release": row[7],
                "published_on": row[8], "known_from": row[9], "supersedes": row[10],
                "observed_at_ms": int(row[11]), "retrieved_on": day(int(row[11])), "run_id": row[12],
                "source_id": row[13], "evidence_origin": row[14]}

    def revisions(self, namespace: str, record_id: str, *, as_of: str | None = None,
                  cutoff_seq: int | None = None) -> list[dict[str, Any]]:
        """Revisions in order; ``as_of`` (YYYY-MM-DD) keeps those known by then (release publication date)."""
        if not self.ready():
            return []
        rows = self.conn.execute(
            f"SELECT {self._COLUMNS} FROM real_estate_revisions WHERE namespace=? AND record_id=? AND "
            "(? IS NULL OR known_from<=?) AND (? IS NULL OR seq<=?) ORDER BY revision_no",
            [namespace, record_id, as_of, as_of, cutoff_seq, cutoff_seq]).fetchall()
        return [self._revision(r) for r in rows]

    def current(self, namespace: str, record_id: str, *, as_of: str | None = None,
                cutoff_seq: int | None = None) -> dict[str, Any] | None:
        revisions = self.revisions(namespace, record_id, as_of=as_of, cutoff_seq=cutoff_seq)
        return revisions[-1] if revisions else None

    def revision(self, namespace: str, revision_id: str) -> dict[str, Any]:
        row = self.conn.execute(f"SELECT {self._COLUMNS} FROM real_estate_revisions WHERE namespace=? AND "
                                "revision_id=?", [namespace, revision_id]).fetchone() if self.ready() else None
        if row is None:
            raise RealEstateError("not_found", "real-estate revision is not visible in this namespace")
        return self._revision(row)

    def records(self, namespace: str, *, record_type: str | None = None,
                provider: str | None = None) -> list[dict[str, Any]]:
        if not self.ready():
            return []
        rows = self.conn.execute(
            "SELECT record_id, record_type, provider, record_key, selection_key FROM real_estate_records WHERE "
            "namespace=? AND (? IS NULL OR record_type=?) AND (? IS NULL OR provider=?) ORDER BY record_type, "
            "provider, record_key", [namespace, record_type, record_type, provider, provider]).fetchall()
        return [dict(zip(("record_id", "record_type", "provider", "record_key", "selection_key"), r)) for r in rows]

    def record(self, namespace: str, record_id: str) -> dict[str, Any]:
        found = next((r for r in self.records(namespace) if r["record_id"] == record_id), None)
        if found is None:
            raise RealEstateError("not_found", "real-estate record is not visible in this namespace")
        return {**found, "revisions": self.revisions(namespace, record_id)}

    def find(self, namespace: str, record_type: str, provider: str, record_key: str) -> dict[str, Any] | None:
        record_id = record_id_for(namespace, provider, record_type, record_key)
        row = self.conn.execute("SELECT 1 FROM real_estate_records WHERE namespace=? AND record_id=?",
                                [namespace, record_id]).fetchone() if self.ready() else None
        return None if row is None else {"record_id": record_id, "record_type": record_type, "provider": provider,
                                         "record_key": record_key}

    def vintages(self, namespace: str, *, source_id: str | None = None,
                 cutoff_seq: int | None = None) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "real_estate_vintages"):
            return []
        rows = self.conn.execute(
            "SELECT vintage_id, source_id, provider, release, published_on, basis, record_count, changed, "
            "file_sha256, evidence_origin, run_id, seq FROM real_estate_vintages WHERE namespace=? AND "
            "(? IS NULL OR source_id=?) AND (? IS NULL OR seq<=?) ORDER BY provider, release, seq",
            [namespace, source_id, source_id, cutoff_seq, cutoff_seq]).fetchall()
        return [dict(zip(("vintage_id", "source_id", "provider", "release", "published_on", "basis", "record_count",
                          "changed", "file_sha256", "evidence_origin", "run_id", "seq"), r)) for r in rows]

    def outcomes(self, namespace: str, *, run_id: str | None = None) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "real_estate_projection_outcomes"):
            return []
        rows = self.conn.execute(
            "SELECT run_id, source_id, item_key, outcome, reason, detail_json FROM real_estate_projection_outcomes "
            "WHERE namespace=? AND (? IS NULL OR run_id=?) ORDER BY run_id, source_id, item_key",
            [namespace, run_id, run_id]).fetchall()
        return [{"run_id": r[0], "source_id": r[1], "item_key": r[2], "outcome": r[3], "reason": r[4],
                 "detail": json.loads(r[5])} for r in rows]

    def generation(self, namespace: str) -> int:
        if not self.ready():
            return 0
        return int(max(self.conn.execute(f"SELECT coalesce(max(seq), 0) FROM {t} WHERE namespace=?",
                                         [namespace]).fetchone()[0]
                       for t in ("real_estate_revisions", "real_estate_vintages")))

    def runs(self, namespace: str, run_id: str | None = None) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "real_estate_source_runs"):
            return []
        rows = self.conn.execute(
            "SELECT run_id, source_id, provider, status, outcomes_json, cutoff_seq, finished_at_ms FROM "
            "real_estate_source_runs WHERE namespace=? AND (? IS NULL OR run_id=?) ORDER BY finished_at_ms, "
            "source_id", [namespace, run_id, run_id]).fetchall()
        return [{"run_id": r[0], "source_id": r[1], "provider": r[2], "status": r[3], "outcomes": json.loads(r[4]),
                 "cutoff_seq": int(r[5]), "finished_at_ms": int(r[6])} for r in rows]


def read_store(conn: Any, namespace: str, scopes: Iterable[str]) -> RealEstateStore:
    authorize(namespace, scopes, READ_SCOPE)
    store = RealEstateStore(conn, initialize=False)
    store.require_ready()
    return store


class RealEstateProjector:
    """Runtime projector for ``noesis-real-estate-record-v1``.

    Native pages (PPD, UK HPI, DVF, Eurostat HPI) are recorded whole with their
    release as a vintage; a DVF page closes its commune-year snapshot. INSPIRE
    parcel pages are first projected into the Geospatial feature store by
    :class:`src.kb.geospatial_features.GeospatialFeatureProjector` (the existing
    WFS path); each parcel revision then references the feature revision and
    geometry it produced.
    """

    def __init__(self, conn: Any) -> None:
        self.conn = conn
        self.store = RealEstateStore(conn)
        self._outcomes: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._features = None

    def _geo(self):
        if self._features is None:
            from src.kb.geospatial_features import GeospatialFeatureProjector

            self._features = GeospatialFeatureProjector(self.conn)
        return self._features

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        if source.get("connector") == "wfs":
            return self._project_parcels(run_id=run_id, manifest=manifest, source=source, records=records,
                                         documents=documents, page_receipt=page_receipt, principal_id=principal_id)
        from src.ingestion.real_estate_sources import declaration

        declared = declaration(source)
        namespace = declared["namespace"]
        statements = [dict(r["real_estate_record"]) for r in records if r.get("real_estate_record")]
        receipt = dict(page_receipt or {})
        release = dict(receipt.get("release") or {})
        self.conn.execute("BEGIN")
        try:
            result = self.store.observe(namespace, statements, run_id=run_id, source_id=source["source_id"],
                                        transaction=False)
            snapshot = dict(receipt.get("snapshot") or {})
            closed = None
            if snapshot.get("selection_key") and snapshot.get("complete"):
                closed = self.store.close_snapshot(
                    namespace, selection_key=snapshot["selection_key"], release=release,
                    present_record_ids=[r["record_id"] for r in result["results"]], run_id=run_id,
                    source_id=source["source_id"])
            changed = result["counts"]["created"] + result["counts"]["revised"] + len((closed or {}).get("removed")
                                                                                     or [])
            vintage = self.store.record_vintage(
                namespace, source_id=source["source_id"], provider=declared["provider"], release=release,
                record_count=len(statements), changed=changed,
                file_sha256=statements[0]["source"].get("file_sha256") if statements else None,
                evidence_origin=receipt.get("evidence_origin"), run_id=run_id)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        self._outcomes.setdefault((run_id, source["source_id"]), []).append(
            {"document": receipt.get("document"), "release": release.get("release"), "records": len(statements),
             "counts": result["counts"], "removed": len((closed or {}).get("removed") or []),
             "vintage": vintage["status"]})
        return result["counts"]

    def _project_parcels(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        from src.ingestion.geojson_features import feature_key
        from src.ingestion.real_estate_sources import parcel_declaration

        declared = parcel_declaration(source)
        wfs = dict(source.get("wfs") or {})
        pinned = [float(v) for v in wfs["bbox"]]
        for record in records:
            scope = dict(dict(record.get("feature_page") or {}).get("scope") or {})
            if [float(v) for v in scope.get("bbox") or []] != pinned:
                raise RealEstateError("unbounded_scope", "parcel pages are acquired only within the declared bbox")
        counts = self._geo().project_page(run_id=run_id, manifest=manifest, source=source, records=records,
                                          documents=documents, page_receipt=page_receipt, principal_id=principal_id)
        namespace = declared["namespace"]
        geo_namespace = str(dict(source.get("geospatial") or {}).get("namespace") or DEFAULT_NAMESPACE)
        attributes = declared["attributes"]
        states: dict[str, int] = {}
        page = dict(records[0].get("feature_page") or {}) if records else {}
        stamp = page.get("provider_timestamp")
        release = {"release": stamp or f"retrieved {day(self.store.now())}",
                   "published_on": str(stamp)[:10] if stamp else None,
                   "basis": "the WFS response timeStamp" if stamp else "no provider timestamp; retrieval day"}
        statements = []
        self.conn.execute("BEGIN")
        try:
            for record in records:
                if record.get("rejection"):
                    rejection = dict(record["rejection"])
                    self.store.record_outcome(namespace, run_id, source["source_id"],
                                              f"{rejection.get('collection')}:{rejection.get('feature_index')}",
                                              "not_projected", f"feature_rejected:{rejection.get('code')}",
                                              {"native_id": rejection.get("native_id")})
                    states["feature_rejected"] = states.get("feature_rejected", 0) + 1
                    continue
                feature = dict(record["feature"])
                feature_id = feature_key(str(feature["provider"]), str(feature["collection"]),
                                         str(feature["native_id"]))
                row = self.conn.execute(
                    "SELECT r.revision_id, r.geometry_id, r.properties_json, r.source_crs, r.transform_json, "
                    "r.source_geometry_json, r.lifecycle FROM geospatial_feature_current c JOIN "
                    "geospatial_feature_revisions r ON r.revision_id=c.revision_id JOIN geospatial_features f ON "
                    "f.feature_id=c.feature_id WHERE c.feature_id=? AND f.namespace=?",
                    [feature_id, geo_namespace]).fetchone()
                if row is None or row[6] != "active":
                    self.store.record_outcome(namespace, run_id, source["source_id"], feature_id, "not_projected",
                                              "geometry_not_projected", {"native_id": feature["native_id"]})
                    states["geometry_not_projected"] = states.get("geometry_not_projected", 0) + 1
                    continue
                properties = json.loads(row[2])
                local_id = properties.get(attributes["local_id"])
                reference = properties.get(attributes["national_reference"])
                if not local_id or not reference:
                    self.store.record_outcome(namespace, run_id, source["source_id"], feature_id, "not_projected",
                                              "missing_identifier", {"native_id": feature["native_id"]})
                    states["missing_identifier"] = states.get("missing_identifier", 0) + 1
                    continue
                inspire_ns = properties.get(attributes["namespace"])
                published = {"local_id": str(local_id), "inspire_namespace": inspire_ns,
                             "inspire_id": f"{inspire_ns}.{local_id}" if inspire_ns else str(local_id),
                             "national_cadastral_reference": str(reference),
                             "reference_scheme": declared["reference_scheme"], "country": declared["country"],
                             **{name: properties.get(attr) for name, attr in attributes.items()
                                if name not in {"local_id", "namespace", "national_reference"}},
                             "geometry": {"feature_id": feature_id, "geometry_id": row[1],
                                          "source_crs": row[3],
                                          "source_geometry_sha256": hashlib.sha256(
                                              (row[5] or "").encode()).hexdigest(),
                                          "coordinate_transform": json.loads(row[4] or "{}"),
                                          "store": "geospatial_feature_revisions / geospatial_geometries"}}
                statements.append({
                    "contract": CONTRACT, "record_type": "parcel", "provider": declared["provider"],
                    "record_key": published["inspire_id"], "event": "observed", "vintage": release,
                    "as_published": published,
                    "place_refs": [], "parcel_refs": [{"scheme": declared["reference_scheme"], "code": str(reference)}],
                    "selection_key": None,
                    "source": {"provider": declared["provider"],
                               "publisher": PROVIDER_CONTRACTS[declared["provider"]]["publisher"],
                               "url": source["endpoint"], "licence": PROVIDER_CONTRACTS[declared["provider"]]["licence"],
                               "attribution": PROVIDER_CONTRACTS[declared["provider"]]["attribution"],
                               "evidence_origin": "live", "file_sha256": page.get("response_sha256"),
                               "feature_revision_id": row[0]}})
            # The shared WFS transport does not attest whether a page was replayed or live, so parcels say so
            # rather than claiming live evidence.
            origin = dict(page_receipt or {}).get("evidence_origin") or "unattested-wfs-transport"
            for item in statements:
                item["source"]["evidence_origin"] = origin
            # The feature revision id is a pointer into the Geospatial store, not published content: a re-read of
            # an unchanged parcel must not add a revision, so it is kept on the source block only.
            result = self.store.observe(namespace, statements, run_id=run_id, source_id=source["source_id"],
                                        transaction=False)
            for key, value in result["counts"].items():
                states[key] = states.get(key, 0) + value
            changed = result["counts"]["created"] + result["counts"]["revised"]
            vintage = self.store.record_vintage(
                namespace, source_id=source["source_id"], provider=declared["provider"], release=release,
                record_count=len(statements), changed=changed, file_sha256=page.get("response_sha256"),
                evidence_origin=origin, run_id=run_id)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        self._outcomes.setdefault((run_id, source["source_id"]), []).append(
            {"parcels": len(statements), "counts": states, "vintage": vintage["status"],
             "properties_dropped": page.get("properties_dropped") or []})
        return {"features": counts, "real_estate": states}

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        snapshot = None
        if source.get("connector") == "wfs":
            from src.ingestion.real_estate_sources import parcel_declaration

            snapshot = self._geo().finish_source(run_id=run_id, manifest=manifest, source=source, status=status,
                                                 principal_id=principal_id)
            declared = parcel_declaration(source)
        else:
            from src.ingestion.real_estate_sources import declaration

            declared = declaration(source)
        receipt = self.store.record_run(declared["namespace"], run_id, source["source_id"],
                                        provider=declared["provider"], status=status,
                                        outcomes=self._outcomes.pop((run_id, source["source_id"]), []))
        return {**receipt, **({"features": snapshot} if snapshot else {})}


def register_schemas(conn: Any, *, principal_id: str, scopes: Any) -> list[dict[str, Any]]:
    """Register the real-estate record contract as a schema module in the shared registry."""
    from pathlib import Path

    from src.kb.schema_registry import SchemaRegistry

    path = Path(__file__).resolve().parents[2] / "contracts/schemas/jsonschema" / f"{CONTRACT}.json"
    definition = {
        "contract": "noesis-schema-module-v1", "name": "real-estate-record", "kind": "schema",
        "semantic_version": "1.0.0", "content": json.loads(path.read_text()), "owner": "geospatial.real-estate",
        "dependencies": [], "compatibility_policy": "backward",
        "provenance": {"kind": "imported", "source": f"contracts/schemas/jsonschema/{CONTRACT}.json"},
        "actor": {"principal_id": principal_id, "kind": "service"},
    }
    return [SchemaRegistry(conn).register(definition, "real-estate-schema:real-estate-record:1.0.0",
                                          principal_id=principal_id, scopes=scopes)]


def readiness(conn: Any) -> dict[str, Any]:
    store = RealEstateStore(conn, initialize=False)
    counts = {t: 0 for t in RECORD_TYPES}
    if store.ready():
        for record_type, count in conn.execute("SELECT record_type, count(*) FROM real_estate_records GROUP BY "
                                               "record_type").fetchall():
            counts[record_type] = int(count)
    return {"feature": "real-estate", "selected": feature_enabled(conn), "stores_ready": store.ready(),
            "records": counts,
            "providers": {p: {k: c.get(k) for k in ("delivers", "access_decision", "reason")}
                          for p, c in PROVIDER_CONTRACTS.items()},
            "live_verification": LIVE_VERIFICATION, "reuse_conditions": REUSE_CONDITIONS, "exclusions": EXCLUSIONS,
            "note": "offline fixture evidence and live evidence are reported per revision (evidence_origin); no "
                    "provider is live until a dated run verifies it (#2519)"}


__all__ = ["CONTRACT", "READ_SCOPE", "RECORD_TYPES", "REVIEW_SCOPE", "TABLES", "WRITE_SCOPE", "RealEstateError",
           "RealEstateProjector", "RealEstateStore", "authorize", "feature_enabled", "read_store", "readiness",
           "record_id_for", "register_schemas", "validate_statement"]
