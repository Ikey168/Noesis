"""Append-only hazard records, their publisher revisions and their geospatial projection (NH02, #2308).

One store owns ``noesis-hazard-record-v1`` (one owner per record type):

* **Records** (hazard events, advisories, alerts, impact estimates) are keyed
  by ``namespace + record type + provider + native id``.
* **Revisions are appended, never overwritten.** Each distinct published
  content (which includes the publisher's ``revision_key`` and
  ``published_at``) is a new immutable revision. Re-acquiring identical
  content is a no-op. A revision the publisher dated *earlier* than the
  current one (a late or replayed older version) is kept as history and does
  not become current.
* **Two clocks.** ``published_at_ms`` is the issuing body's update/issue time
  (``published_basis = publisher``; the acquisition time is used and labelled
  ``acquisition_fallback`` when the publisher states none) and
  ``observed_at_ms`` is when Noesis acquired it. As-of reads choose either
  basis and say which one and which revision they used.
* **Geometry stays with the geospatial owner.** Each record is a
  ``geospatial_places`` entry (``place_type`` ``hazard-*``) and each revision's
  geometry is stored through :class:`~src.kb.geospatial.GeospatialStore`; no
  spatial table is added here.
* **Validity is as issued.** Alerts and advisories keep their published
  issue time and validity window; :meth:`HazardStore.valid_at` never extends
  or infers a window.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from src.kb import hazards_records as hr
from src.kb.hazards_records import READ_SCOPE, WRITE_SCOPE, canonical, digest

GEO_SCOPES = {"knowledge:geospatial:read", "knowledge:geospatial:write", "knowledge:geospatial:calculate"}
PRODUCER = {"name": "noesis-natural-hazards-pack", "version": "1.0.0"}
DEFAULT_NAMESPACE = "hazards"
STALE_AFTER_MS = 3 * 86_400_000
BASES = ("publisher", "acquisition")
PLACE_TYPES = {"hazard_event": "hazard-event", "advisory": "hazard-advisory-position", "alert": "hazard-alert-area",
               "impact_estimate": "hazard-impact-estimate"}

_DDL = """
CREATE TABLE IF NOT EXISTS hazard_records(
 record_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, record_type TEXT NOT NULL, provider TEXT NOT NULL,
 native_id TEXT NOT NULL, hazard_type TEXT NOT NULL, place_id TEXT, created_at_ms BIGINT NOT NULL,
 UNIQUE(namespace, record_type, provider, native_id));
CREATE TABLE IF NOT EXISTS hazard_record_revisions(
 revision_id TEXT PRIMARY KEY, record_id TEXT NOT NULL, namespace TEXT NOT NULL, revision BIGINT NOT NULL,
 revision_key TEXT NOT NULL, content_hash TEXT NOT NULL, content_json TEXT NOT NULL, published_at_ms BIGINT NOT NULL,
 published_basis TEXT NOT NULL, observed_at_ms BIGINT NOT NULL, geometry_id TEXT, run_id TEXT NOT NULL,
 evidence_json TEXT NOT NULL, principal_id TEXT NOT NULL, UNIQUE(record_id, revision));
CREATE TABLE IF NOT EXISTS hazard_provider_state(
 namespace TEXT NOT NULL, provider TEXT NOT NULL, last_success_ms BIGINT, last_failure_ms BIGINT,
 last_failure_code TEXT, last_execution TEXT, last_run_id TEXT, PRIMARY KEY(namespace, provider));
"""


class HazardStoreError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def authorize(namespace, scopes, required, *, write=False):
    """Hazards scope plus current namespace access (operator bypasses)."""

    scopes = set(scopes or ())
    if "operator" in scopes:
        return
    needed = {f"namespace:{namespace}:write"} if write else {f"namespace:{namespace}:read", f"namespace:{namespace}:write"}
    if required not in scopes or not needed & scopes:
        raise HazardStoreError("unauthorized", f"{required} and namespace access are required")


def ms(value):
    """ISO date/instant -> epoch ms (dates are UTC midnight); ``None`` stays ``None``."""

    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).replace("Z", "+00:00")
    if len(text) == 10:
        text += "T00:00:00+00:00"
    return int(datetime.fromisoformat(text).timestamp() * 1000)


def iso(value_ms):
    if value_ms is None:
        return None
    return datetime.fromtimestamp(value_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def record_id(namespace, record_type, provider, native_id):
    return "hazard:" + digest([namespace, record_type, provider, native_id])[:24]


def _table(conn, name):
    return bool(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name=?", [name]).fetchone())


class HazardStore:
    def __init__(self, conn, *, initialize=True, now=None):
        from src.kb.geospatial import GeospatialStore

        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)
        self.geo = GeospatialStore(conn, initialize=initialize, now=self.now)

    # ----------------------------------------------------------------- writes

    def apply(self, namespace, records, *, run_id, principal_id, scopes, evidence=None, execution="injected",
              observed_at_ms=None):
        """Apply validated records: new content is an appended revision, identical content a no-op."""

        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        if namespace == "global":
            raise HazardStoreError("namespace_forbidden", "hazard records are written to a caller namespace")
        observed = int(observed_at_ms or self.now())
        ordered = sorted((hr.validate(dict(r)) for r in records),
                         key=lambda r: (hr.RECORD_TYPES.index(r["record_type"]), r["provider"], r["native_id"],
                                        ms(r.get("published_at")) or 0))
        counts = {"revisions": 0, "unchanged": 0, "late": 0, "places": 0, "applied": []}
        for record in ordered:
            outcome = self._apply_one(namespace, record, run_id=run_id, principal_id=principal_id,
                                      evidence=dict(evidence or {}), observed=observed)
            for key in ("revisions", "unchanged", "late", "places"):
                counts[key] += outcome.get(key, 0)
            if outcome.get("revision_id"):
                counts["applied"].append({"record_id": outcome["record_id"], "revision_id": outcome["revision_id"],
                                          "revision": outcome["revision"], "late": bool(outcome.get("late"))})
        self._provider_state(namespace, {r["provider"] for r in ordered}, success=observed, execution=execution,
                             run_id=run_id)
        return counts

    def _apply_one(self, namespace, record, *, run_id, principal_id, evidence, observed):
        rid = record_id(namespace, record["record_type"], record["provider"], record["native_id"])
        # The locator names the document it was read from; the same published version read through
        # another document (an event list and its detail) is the same revision.
        content_hash = digest({k: v for k, v in record.items() if k != "locator"})
        if self.conn.execute("SELECT 1 FROM hazard_record_revisions WHERE record_id=? AND content_hash=?",
                             [rid, content_hash]).fetchone():
            return {"unchanged": 1, "record_id": rid}
        existing = self.conn.execute("SELECT place_id, hazard_type FROM hazard_records WHERE record_id=?", [rid]).fetchone()
        placed = 0
        if existing is None:
            self.conn.execute("INSERT INTO hazard_records VALUES (?,?,?,?,?,?,NULL,?)",
                              [rid, namespace, record["record_type"], record["provider"], record["native_id"],
                               record["hazard_type"], observed])
            place_id = None
        else:
            if existing[1] != record["hazard_type"]:
                raise HazardStoreError("hazard_type_conflict", "a record never changes hazard type")
            place_id = existing[0]
        geometry_id = None
        if record.get("geometry") is not None:
            if place_id is None:
                place_id, placed = self._place(namespace, record, rid, principal_id=principal_id, observed=observed)
                self.conn.execute("UPDATE hazard_records SET place_id=? WHERE record_id=?", [place_id, rid])
            geometry_id = self.geo.store_geometry(
                namespace, record["geometry"], place_id=place_id, crs="EPSG:4326", precision_m=0.0,
                simplified_from=None, disputed=False, admin_hierarchy=[],
                source={"kind": "hazard-record", "record_id": rid, "provider": record["provider"],
                        "role": record.get("geometry_role") or "published geometry"},
                evidence=[{"kind": "provider-record", "source_url": record["source_url"]}], principal_id=principal_id,
                scopes=GEO_SCOPES, observed_at_ms=observed, producer=PRODUCER,
                policy={"crs": "explicit-v1", "geometry": "published by the issuing body; never recomputed"},
            )["geometry_id"]
        published = ms(record.get("published_at"))
        basis = "publisher" if published is not None else "acquisition_fallback"
        published = published if published is not None else observed
        last = self.conn.execute("SELECT max(revision) FROM hazard_record_revisions WHERE record_id=?", [rid]).fetchone()[0]
        revision = int(last or 0) + 1
        current = self._current_row(rid)
        late = current is not None and published < int(current["published_at_ms"])
        revision_id = "hazard-rev:" + digest([rid, revision, content_hash])[:24]
        self.conn.execute(
            "INSERT INTO hazard_record_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [revision_id, rid, namespace, revision, record["revision_key"], content_hash, canonical(record), published,
             basis, observed, geometry_id, run_id, canonical(evidence), principal_id])
        return {"revisions": 1, "late": int(late), "places": placed, "record_id": rid, "revision_id": revision_id,
                "revision": revision}

    def _place(self, namespace, record, rid, *, principal_id, observed):
        key = f"hazards:{record['provider']}:{record['record_type']}:{record['native_id']}"
        row = self.conn.execute("SELECT place_id FROM geospatial_places WHERE namespace=? AND place_key=?",
                                [namespace, key]).fetchone()
        if row:
            return row[0], 0
        place = self.geo.register_place(
            namespace, record["title"], PLACE_TYPES[record["record_type"]],
            names=[{"value": record["title"], "language": "und", "kind": "canonical"}],
            source_ids={record["provider"]: record["native_id"]}, parent_ids=[], principal_id=principal_id,
            scopes=GEO_SCOPES, place_key=key, observed_at_ms=observed, producer=PRODUCER,
            provenance={"source_url": record["source_url"], "record_id": rid})
        return place["place_id"], 1

    def _provider_state(self, namespace, providers, *, success=None, failure=None, code=None, execution=None, run_id=None):
        for provider in sorted(providers):
            self.conn.execute(
                "INSERT INTO hazard_provider_state VALUES (?,?,?,?,?,?,?) ON CONFLICT (namespace, provider) DO UPDATE SET "
                "last_success_ms=coalesce(excluded.last_success_ms, hazard_provider_state.last_success_ms), "
                "last_failure_ms=coalesce(excluded.last_failure_ms, hazard_provider_state.last_failure_ms), "
                "last_failure_code=coalesce(excluded.last_failure_code, hazard_provider_state.last_failure_code), "
                "last_execution=coalesce(excluded.last_execution, hazard_provider_state.last_execution), "
                "last_run_id=coalesce(excluded.last_run_id, hazard_provider_state.last_run_id)",
                [namespace, provider, success, failure, code, execution, run_id])

    def record_failure(self, namespace, provider, *, code, run_id, scopes):
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        self._provider_state(namespace, {provider}, failure=self.now(), code=code, run_id=run_id)
        return self.provider_state(namespace, provider)

    # ------------------------------------------------------------------ reads

    def provider_state(self, namespace, provider):
        row = self.conn.execute("SELECT last_success_ms, last_failure_ms, last_failure_code, last_execution, last_run_id "
                                "FROM hazard_provider_state WHERE namespace=? AND provider=?", [namespace, provider]).fetchone()
        if row is None:
            return {"provider": provider, "acquired": False, "last_success_ms": None, "stale": True}
        success, failure = row[0], row[1]
        stale = success is None or (failure is not None and failure > success) or self.now() - success > STALE_AFTER_MS
        return {"provider": provider, "acquired": success is not None, "last_success_ms": success,
                "last_failure_ms": failure, "last_failure_code": row[2], "last_execution": row[3], "last_run_id": row[4],
                "stale": bool(stale)}

    def find(self, namespace, provider, record_type, native_id):
        rid = record_id(namespace, record_type, provider, native_id)
        return rid if self.conn.execute("SELECT 1 FROM hazard_records WHERE record_id=?", [rid]).fetchone() else None

    def _rows(self, record_id_):
        rows = self.conn.execute(
            "SELECT revision_id, revision, revision_key, content_json, published_at_ms, published_basis, observed_at_ms, "
            "geometry_id, run_id, evidence_json FROM hazard_record_revisions WHERE record_id=? "
            "ORDER BY published_at_ms, revision", [record_id_]).fetchall()
        return [{"revision_id": r[0], "revision": int(r[1]), "revision_key": r[2], "content": json.loads(r[3]),
                 "published_at_ms": int(r[4]), "published_basis": r[5], "observed_at_ms": int(r[6]),
                 "geometry_id": r[7], "run_id": r[8], "evidence": json.loads(r[9])} for r in rows]

    def _current_row(self, record_id_):
        rows = self._rows(record_id_)
        return rows[-1] if rows else None

    def revision_at(self, record_id_, *, as_of_ms=None, basis="publisher"):
        """The revision in force at ``as_of_ms`` on the chosen clock (latest publisher time by default)."""

        if basis not in BASES:
            raise HazardStoreError("invalid_basis", f"basis is one of {BASES}")
        rows = self._rows(record_id_)
        if as_of_ms is None:
            return rows[-1] if rows else None
        if basis == "publisher":
            known = [r for r in rows if r["published_at_ms"] <= as_of_ms]
        else:
            known = [r for r in rows if r["observed_at_ms"] <= as_of_ms]
        return known[-1] if known else None

    def _header(self, record_id_):
        row = self.conn.execute("SELECT namespace, record_type, provider, native_id, hazard_type, place_id FROM hazard_records "
                                "WHERE record_id=?", [record_id_]).fetchone()
        if row is None:
            return None
        return {"record_id": record_id_, "namespace": row[0], "record_type": row[1], "provider": row[2],
                "native_id": row[3], "hazard_type": row[4], "place_id": row[5]}

    def record(self, namespace, record_id_, *, scopes, as_of_ms=None, basis="publisher"):
        authorize(namespace, scopes, READ_SCOPE)
        header = self._header(record_id_)
        if header is None or header["namespace"] != namespace:
            raise HazardStoreError("not_found", "hazard record is not visible in this namespace")
        row = self.revision_at(record_id_, as_of_ms=as_of_ms, basis=basis)
        if row is None:
            return {**header, "revision": None, "content": None, "as_of_ms": as_of_ms, "basis": basis,
                    "state": "not yet published on this clock"}
        return {**header, **self._view(header, row), "as_of_ms": as_of_ms, "basis": basis}

    def _view(self, header, row):
        return {"revision_id": row["revision_id"], "revision": row["revision"], "revision_key": row["revision_key"],
                "published_at": iso(row["published_at_ms"]), "published_basis": row["published_basis"],
                "observed_at": iso(row["observed_at_ms"]), "geometry_id": row["geometry_id"], "content": row["content"],
                "citation": cite(header, row)}

    def revisions(self, namespace, record_id_, *, scopes):
        """Every revision in publisher order with the parameter changes each one made."""

        authorize(namespace, scopes, READ_SCOPE)
        header = self._header(record_id_)
        if header is None or header["namespace"] != namespace:
            raise HazardStoreError("not_found", "hazard record is not visible in this namespace")
        history, previous = [], None
        for row in self._rows(record_id_):
            history.append({**self._view(header, row),
                            "changes": hr.parameter_changes(previous, row["content"]),
                            "acquisition_order": row["revision"]})
            previous = row["content"]
        return {**header, "revisions": history, "order": "publisher time, then acquisition order"}

    def records(self, namespace, *, scopes, record_type=None, provider=None, hazard_type=None):
        authorize(namespace, scopes, READ_SCOPE)
        rows = self.conn.execute(
            "SELECT record_id FROM hazard_records WHERE namespace=? AND (? IS NULL OR record_type=?) "
            "AND (? IS NULL OR provider=?) AND (? IS NULL OR hazard_type=?) ORDER BY provider, native_id, record_type",
            [namespace, record_type, record_type, provider, provider, hazard_type, hazard_type]).fetchall()
        return [self.record(namespace, rid, scopes=scopes) for (rid,) in rows]

    def record_ids(self, namespace, *, record_type=None, provider=None, hazard_type=None):
        return [r[0] for r in self.conn.execute(
            "SELECT record_id FROM hazard_records WHERE namespace=? AND (? IS NULL OR record_type=?) "
            "AND (? IS NULL OR provider=?) AND (? IS NULL OR hazard_type=?) ORDER BY provider, native_id, record_type",
            [namespace, record_type, record_type, provider, provider, hazard_type, hazard_type]).fetchall()]

    def related(self, namespace, provider, event_native_id, record_type):
        """Advisories, alerts or estimates the publisher issued for one of its events (by its event id)."""

        result = []
        for rid in self.record_ids(namespace, record_type=record_type, provider=provider):
            row = self._current_row(rid)
            if row and row["content"].get("event_native_id") == event_native_id:
                result.append(rid)
        return result


def cite(header, row):
    """A citation for one record revision: source, revision and both clocks."""

    content = row["content"]
    return {"record_id": header["record_id"], "revision_id": row["revision_id"], "revision_key": row["revision_key"],
            "provider": header["provider"], "issuing_body": content["issuing_body"], "source_url": content["source_url"],
            "locator": content.get("locator") or {}, "published_at": iso(row["published_at_ms"]),
            "published_basis": row["published_basis"], "acquired_at": iso(row["observed_at_ms"]),
            "evidence_origin": row["evidence"].get("evidence_origin", "unknown")}


def valid_at(content, at_ms, *, next_issued_ms=None):
    """Whether an alert/advisory's *published* validity covers ``at_ms``.

    The window starts at ``valid_from`` (or the issue time when the publisher
    gives no start) and ends at ``valid_to`` when published. An advisory with no
    published expiry is in force until the issuing body's next advisory for the
    same storm (``next_issued_ms``); without one it is ``open`` (no expiry
    published), never assumed to have lapsed or to continue.
    """

    start = ms(content.get("valid_from")) or ms(content.get("issued_at"))
    end = ms(content.get("valid_to"))
    if start is not None and at_ms < start:
        return {"in_force": False, "reason": "not yet valid", "window": [iso(start), iso(end)]}
    if end is not None:
        return {"in_force": at_ms < end, "reason": "published validity window" if at_ms < end else "expired",
                "window": [iso(start), iso(end)], "basis": "published valid_to"}
    if next_issued_ms is not None:
        return {"in_force": at_ms < next_issued_ms,
                "reason": "superseded by the next issued product" if at_ms >= next_issued_ms else "latest issued at that time",
                "window": [iso(start), iso(next_issued_ms)], "basis": "superseded at next issue time"}
    return {"in_force": True, "reason": "no expiry published; latest issued product", "window": [iso(start), None],
            "basis": "open (no published expiry)"}


class HazardProjector:
    """Source-pack runtime projector for ``noesis-hazard-record-v1`` pages."""

    SCOPES_FOR = staticmethod(lambda namespace: {WRITE_SCOPE, READ_SCOPE, f"namespace:{namespace}:write",
                                                 f"namespace:{namespace}:read"})

    def __init__(self, conn):
        self.store = HazardStore(conn)

    @staticmethod
    def _namespace(source):
        return str(dict(source.get("natural_hazards") or {}).get("namespace") or DEFAULT_NAMESPACE)

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        namespace = self._namespace(source)
        payload = [dict(item["hazard_record"]) for item in records if item.get("hazard_record")]
        evidence = {"run_id": run_id, "source_id": source["source_id"],
                    "pack_id": (manifest or {}).get("pack_id"), "pack_version": (manifest or {}).get("version"),
                    "response_sha256": page_receipt.get("response_sha256"),
                    "evidence_origin": page_receipt.get("evidence_origin", "unknown"),
                    "documents": sorted(str(d["document_id"]) for d in documents or [])}
        observed = max((int(d["ingested_at"]) for d in documents or [] if d.get("ingested_at") is not None), default=None)
        return self.store.apply(namespace, payload, run_id=run_id, principal_id=principal_id,
                                scopes=self.SCOPES_FOR(namespace), evidence=evidence, execution="source-pack",
                                observed_at_ms=observed)

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del manifest, principal_id
        namespace = self._namespace(source)
        provider = str(dict(source.get("natural_hazards") or {}).get("provider"))
        if status != "complete":
            self.store.record_failure(namespace, provider, code="source_run_" + status, run_id=run_id,
                                      scopes=self.SCOPES_FOR(namespace))
        return {"status": status, "provider": provider, "namespace": namespace,
                "provider_state": self.store.provider_state(namespace, provider)}
