"""Energy series, release vintages and acquisition receipts: the ``energy.core`` record owner (EN02-EN07).

One store owns ``noesis-energy-record-v1`` (namespace-scoped, revision
addressable, one owner):

* **Series** are keyed by ``namespace + provider + dataset + native_id``; the
  subject (zone, country, balancing area, plant/unit), counterpart, facets and
  unit are recorded once.
* **Vintages** are every distinct published state of a series. A record whose
  content matches an existing vintage is a no-op; anything else is a new
  vintage with the next sequence, the release key and date the provider states
  (ENTSO-E ``createdDateTime`` + ``revisionNumber``, Eurostat ``LAST UPDATE``,
  a declared EIA/Ember release, else the retrieval time labelled as such), the
  publication status and ``revision_of`` pointing at the previous vintage.
  Provisional and revised figures are therefore distinct rows; nothing is
  overwritten or deleted.
* **Values** are stored per vintage as published decimal text with flags.
* **Receipts** record every bounded acquisition step (request without
  credentials, response digest and size, status, execution ``injected`` or
  ``network``) and every failure; a failure never changes stored values.

Day-ahead prices are also written through Market time-series storage
(:mod:`src.kb.energy_market`); places and plant identities stay with the
geospatial and entity owners (:mod:`src.kb.energy_identity`). No scheduler,
spatial table or entity table is added here.
"""

from __future__ import annotations

import json
import time
from datetime import datetime

from src.kb import energy_records as enr
from src.kb.energy_records import READ_SCOPE, WRITE_SCOPE, canonical, digest

PRODUCER = {"name": "noesis-energy-pack", "version": "1.0.0"}
STALE_AFTER_MS = 3 * 86_400_000
TABLES = ("energy_series", "energy_vintages", "energy_values", "energy_receipts", "energy_provider_state")
_DDL = """
CREATE TABLE IF NOT EXISTS energy_series(
 series_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, record_type TEXT NOT NULL, provider TEXT NOT NULL,
 dataset TEXT NOT NULL, native_id TEXT NOT NULL, subject_kind TEXT NOT NULL, subject_scheme TEXT NOT NULL,
 subject_code TEXT NOT NULL, counterpart_code TEXT, facets_json TEXT NOT NULL, unit TEXT NOT NULL,
 created_at_ms BIGINT NOT NULL, UNIQUE(namespace, provider, dataset, native_id));
CREATE TABLE IF NOT EXISTS energy_vintages(
 vintage_id TEXT PRIMARY KEY, series_id TEXT NOT NULL, namespace TEXT NOT NULL, sequence BIGINT NOT NULL,
 release_key TEXT NOT NULL, release_basis TEXT NOT NULL, released_at_ms BIGINT, retrieved_at_ms BIGINT NOT NULL,
 published_at_ms BIGINT NOT NULL, status TEXT NOT NULL, content_hash TEXT NOT NULL, values_hash TEXT NOT NULL,
 record_json TEXT NOT NULL, receipt_id TEXT, revision_of TEXT, created_at_ms BIGINT NOT NULL,
 UNIQUE(series_id, sequence));
CREATE TABLE IF NOT EXISTS energy_values(
 vintage_id TEXT NOT NULL, start_ms BIGINT NOT NULL, period_start TEXT NOT NULL, period_end TEXT, value TEXT,
 flags_json TEXT NOT NULL, PRIMARY KEY(vintage_id, start_ms));
CREATE TABLE IF NOT EXISTS energy_receipts(
 receipt_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, provider TEXT NOT NULL, source TEXT NOT NULL,
 request_json TEXT NOT NULL, response_sha256 TEXT, bytes BIGINT, status TEXT NOT NULL, failure_code TEXT,
 execution TEXT NOT NULL, run_id TEXT NOT NULL, records BIGINT NOT NULL, coverage_json TEXT NOT NULL,
 retrieved_at_ms BIGINT NOT NULL, principal_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS energy_provider_state(
 namespace TEXT NOT NULL, provider TEXT NOT NULL, last_success_ms BIGINT, last_failure_ms BIGINT,
 last_failure_code TEXT, last_execution TEXT, last_run_id TEXT, PRIMARY KEY(namespace, provider));
"""


class EnergyStoreError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def authorize(namespace, scopes, required, *, write=False):
    """Energy scope plus namespace access (operator bypasses)."""

    scopes = set(scopes or ())
    if "operator" in scopes:
        return
    needed = {f"namespace:{namespace}:write"} if write else {f"namespace:{namespace}:read", f"namespace:{namespace}:write"}
    if required not in scopes or not needed & scopes:
        raise EnergyStoreError("unauthorized", f"{required} and namespace access are required")


def ms(value):
    """ISO year/month/date/instant -> epoch ms (UTC; partial dates are their first instant)."""

    if value is None:
        return None
    text = str(value).replace("Z", "+00:00")
    if len(text) == 4:
        text += "-01-01"
    if len(text) == 7:
        text += "-01"
    if len(text) == 10:
        text += "T00:00:00+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        from datetime import UTC

        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def iso(value_ms):
    from datetime import UTC

    return None if value_ms is None else datetime.fromtimestamp(value_ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def series_id(namespace, provider, dataset, native_id):
    return "energy-series:" + digest([namespace, provider, dataset, native_id])[:24]


def table_exists(conn, name):
    return bool(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name=?", [name]).fetchone())


class EnergyStore:
    def __init__(self, conn, *, initialize=True, now=None):
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    # ----------------------------------------------------------------- writes

    def receipt(self, namespace, *, provider, source, request, response_sha256, size, status, execution, run_id,
                records, principal_id, coverage=None, failure_code=None, retrieved_at_ms=None):
        """Record one bounded acquisition step (credentials are never part of ``request``)."""

        retrieved = int(retrieved_at_ms or self.now())
        body = {"namespace": namespace, "provider": provider, "source": source, "request": request,
                "response_sha256": response_sha256, "status": status, "run_id": run_id,
                # A replayed page (same run, request and response) is the same receipt.
                "retrieved": retrieved if response_sha256 is None else None}
        receipt_id = "energy-receipt:" + digest(body)[:24]
        self.conn.execute("INSERT INTO energy_receipts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                          [receipt_id, namespace, provider, source, canonical(request), response_sha256, size, status,
                           failure_code, execution, run_id, int(records), canonical(coverage or {}), retrieved,
                           principal_id])
        return receipt_id

    def apply(self, namespace, records, *, receipt_id=None, run_id, principal_id, scopes, execution="injected"):
        """Store validated records; unchanged content is a no-op, anything else a new vintage."""

        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        if namespace == "global":
            raise EnergyStoreError("namespace_forbidden", "energy records are written to a caller namespace")
        counts = {"series": 0, "vintages": 0, "unchanged": 0}
        providers = set()
        for item in records:
            value = enr.validate(dict(item))
            providers.add(value["provider"])
            outcome = self._apply_one(namespace, value, receipt_id=receipt_id)
            for key, amount in outcome.items():
                counts[key] += amount
        stamp = self.now()
        for provider in sorted(providers):
            self.conn.execute(
                "INSERT INTO energy_provider_state VALUES (?,?,?,NULL,NULL,?,?) ON CONFLICT (namespace, provider) DO UPDATE "
                "SET last_success_ms=excluded.last_success_ms, last_execution=excluded.last_execution, "
                "last_run_id=excluded.last_run_id", [namespace, provider, stamp, execution, run_id])
        return counts

    def _apply_one(self, namespace, value, *, receipt_id):
        sid = series_id(namespace, value["provider"], value["dataset"], value["native_id"])
        counts = {"series": 0, "vintages": 0, "unchanged": 0}
        row = self.conn.execute("SELECT record_type, subject_code, unit FROM energy_series WHERE series_id=?", [sid]).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO energy_series VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [sid, namespace, value["record_type"], value["provider"], value["dataset"], value["native_id"],
                 value["subject"]["kind"], value["subject"]["scheme"], value["subject"]["code"],
                 (value.get("counterpart") or {}).get("code"), canonical(value["facets"]), value["unit"], self.now()])
            counts["series"] += 1
        elif (row[0], row[1], row[2]) != (value["record_type"], value["subject"]["code"], value["unit"]):
            raise EnergyStoreError("series_conflict", "a series never changes record type, subject or unit")
        content = {k: v for k, v in value.items() if k != "retrieved_at"}
        if value["release"]["basis"] == "retrieval_time":
            # Undated re-publications differ only when the published content differs.
            content["release"] = {k: v for k, v in value["release"].items() if k != "key"}
        content_hash = digest(content)
        if self.conn.execute("SELECT 1 FROM energy_vintages WHERE series_id=? AND content_hash=?",
                             [sid, content_hash]).fetchone():
            counts["unchanged"] += 1
            return counts
        previous = self.conn.execute("SELECT vintage_id, sequence FROM energy_vintages WHERE series_id=? "
                                     "ORDER BY sequence DESC LIMIT 1", [sid]).fetchone()
        sequence = int(previous[1]) + 1 if previous else 1
        release = value["release"]
        released = ms(release["released_at"])
        retrieved = ms(value["retrieved_at"])
        vintage_id = "energy-vintage:" + digest([sid, sequence, content_hash])[:24]
        self.conn.execute(
            "INSERT INTO energy_vintages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [vintage_id, sid, namespace, sequence, release["key"], release["basis"], released, retrieved,
             released if released is not None else retrieved, value["status"], content_hash, enr.values_digest(value),
             canonical(value), receipt_id, previous[0] if previous else None, self.now()])
        for point in value["values"]:
            self.conn.execute("INSERT INTO energy_values VALUES (?,?,?,?,?,?)",
                              [vintage_id, ms(point["start"]), point["start"], point["end"], point["value"],
                               canonical(point["flags"])])
        counts["vintages"] += 1
        return counts

    def fail(self, namespace, provider, error, *, source, request, run_id, principal_id, execution="injected"):
        """Record a failed step; stored vintages stay as they are (the provider reads as stale)."""

        code = getattr(error, "code", None) or type(error).__name__
        receipt_id = self.receipt(namespace, provider=provider, source=source, request=request, response_sha256=None,
                                  size=None, status="failed", execution=execution, run_id=run_id, records=0,
                                  principal_id=principal_id, failure_code=str(code),
                                  coverage={"message": str(error)[:300]})
        stamp = self.now()
        self.conn.execute(
            "INSERT INTO energy_provider_state VALUES (?,?,NULL,?,?,?,?) ON CONFLICT (namespace, provider) DO UPDATE "
            "SET last_failure_ms=excluded.last_failure_ms, last_failure_code=excluded.last_failure_code, "
            "last_run_id=excluded.last_run_id", [namespace, provider, stamp, str(code), execution, run_id])
        return {"receipt_id": receipt_id, "provider": provider, "failure_code": str(code),
                "effect": "no stored value changed; dependent answers report the provider as stale"}

    # ------------------------------------------------------------------ reads

    def provider_state(self, namespace, provider):
        row = self.conn.execute("SELECT last_success_ms, last_failure_ms, last_failure_code, last_execution, last_run_id "
                                "FROM energy_provider_state WHERE namespace=? AND provider=?",
                                [namespace, provider]).fetchone() if table_exists(self.conn, "energy_provider_state") else None
        if row is None:
            return {"provider": provider, "last_success_ms": None, "stale": True, "reason": "never acquired"}
        success, failure = row[0], row[1]
        stale = success is None or (failure is not None and failure > success) or self.now() - success > STALE_AFTER_MS
        return {"provider": provider, "last_success_ms": success, "last_failure_ms": failure,
                "last_failure_code": row[2], "last_execution": row[3], "last_run_id": row[4], "stale": bool(stale)}

    def series(self, namespace, *, scopes, record_type=None, provider=None, subject_codes=None, series_ids=None):
        authorize(namespace, scopes, READ_SCOPE)
        clauses, params = ["namespace=?"], [namespace]
        for column, value in (("record_type", record_type), ("provider", provider)):
            if value is not None:
                clauses.append(f"{column}=?")
                params.append(value)
        rows = self.conn.execute(
            "SELECT series_id, record_type, provider, dataset, native_id, subject_kind, subject_scheme, subject_code, "
            "counterpart_code, facets_json, unit FROM energy_series WHERE " + " AND ".join(clauses)
            + " ORDER BY provider, dataset, native_id", params).fetchall()
        result = []
        for row in rows:
            if subject_codes is not None and row[7] not in subject_codes:
                continue
            if series_ids is not None and row[0] not in series_ids:
                continue
            result.append({"series_id": row[0], "record_type": row[1], "provider": row[2], "dataset": row[3],
                           "native_id": row[4], "subject": {"kind": row[5], "scheme": row[6], "code": row[7]},
                           "counterpart_code": row[8], "facets": json.loads(row[9]), "unit": row[10]})
        return result

    def subjects(self, namespace, *, scopes):
        """Distinct subjects with the providers publishing them and the name the latest release gives."""

        authorize(namespace, scopes, READ_SCOPE)
        result = []
        for kind, scheme, code, providers in self.conn.execute(
                "SELECT subject_kind, subject_scheme, subject_code, list(provider) FROM energy_series "
                "WHERE namespace=? GROUP BY 1,2,3 ORDER BY 2,3", [namespace]).fetchall():
            row = self.conn.execute(
                "SELECT v.record_json FROM energy_vintages v JOIN energy_series s USING(series_id) WHERE s.namespace=? "
                "AND s.subject_scheme=? AND s.subject_code=? ORDER BY v.created_at_ms DESC, v.vintage_id LIMIT 1",
                [namespace, scheme, code]).fetchone()
            name = json.loads(row[0])["subject"].get("name") if row else None
            result.append({"kind": kind, "scheme": scheme, "code": code, "name": name,
                           "providers": sorted(set(providers))})
        return result

    def _vintage_row(self, row):
        record = json.loads(row[11])
        return {"vintage_id": row[0], "series_id": row[1], "sequence": int(row[2]), "release_key": row[3],
                "release_basis": row[4], "released_at_ms": row[5], "retrieved_at_ms": row[6], "published_at_ms": row[7],
                "status": row[8], "content_hash": row[9], "values_hash": row[10], "record": record,
                "receipt_id": row[12], "revision_of": row[13]}

    _VINTAGE_COLUMNS = ("vintage_id, series_id, sequence, release_key, release_basis, released_at_ms, retrieved_at_ms, "
                        "published_at_ms, status, content_hash, values_hash, record_json, receipt_id, revision_of")

    def vintages(self, namespace, sid, *, scopes):
        authorize(namespace, scopes, READ_SCOPE)
        return [self._vintage_row(r) for r in self.conn.execute(
            f"SELECT {self._VINTAGE_COLUMNS} FROM energy_vintages WHERE namespace=? AND series_id=? ORDER BY sequence",
            [namespace, sid]).fetchall()]

    def vintage(self, namespace, vintage_id, *, scopes):
        authorize(namespace, scopes, READ_SCOPE)
        row = self.conn.execute(f"SELECT {self._VINTAGE_COLUMNS} FROM energy_vintages WHERE namespace=? AND vintage_id=?",
                                [namespace, vintage_id]).fetchone()
        if row is None:
            raise EnergyStoreError("not_found", "vintage is unavailable")
        return self._vintage_row(row)

    def select_vintage(self, namespace, sid, *, scopes, as_of_ms=None):
        """The vintage published at or before ``as_of_ms`` (latest when omitted), or ``None``.

        Publication time is the provider's release date when stated, else the
        retrieval time (labelled by ``release_basis``). Among vintages published
        by the cutoff the latest publication wins, then the latest sequence.
        """

        candidates = [v for v in self.vintages(namespace, sid, scopes=scopes)
                      if as_of_ms is None or v["published_at_ms"] <= as_of_ms]
        if not candidates:
            return None
        return max(candidates, key=lambda v: (v["published_at_ms"], v["sequence"]))

    def values(self, vintage_id, *, start_ms=None, end_ms=None):
        clauses, params = ["vintage_id=?"], [vintage_id]
        if start_ms is not None:
            clauses.append("start_ms>=?")
            params.append(start_ms)
        if end_ms is not None:
            clauses.append("start_ms<?")
            params.append(end_ms)
        return [{"start": r[0], "end": r[1], "value": r[2], "flags": json.loads(r[3])}
                for r in self.conn.execute("SELECT period_start, period_end, value, flags_json FROM energy_values WHERE "
                                           + " AND ".join(clauses) + " ORDER BY start_ms", params).fetchall()]

    def receipt_row(self, receipt_id):
        row = self.conn.execute("SELECT receipt_id, provider, source, request_json, response_sha256, status, execution, "
                                "run_id, retrieved_at_ms FROM energy_receipts WHERE receipt_id=?", [receipt_id]).fetchone()
        if row is None:
            return None
        return {"receipt_id": row[0], "provider": row[1], "source": row[2], "request": json.loads(row[3]),
                "response_sha256": row[4], "status": row[5], "execution": row[6], "run_id": row[7],
                "retrieved_at_ms": row[8]}

    def receipts(self, namespace, *, scopes, provider=None):
        authorize(namespace, scopes, READ_SCOPE)
        clauses, params = ["namespace=?"], [namespace]
        if provider:
            clauses.append("provider=?")
            params.append(provider)
        return [{"receipt_id": r[0], "provider": r[1], "source": r[2], "status": r[3], "failure_code": r[4],
                 "execution": r[5], "records": r[6], "retrieved_at_ms": r[7]}
                for r in self.conn.execute("SELECT receipt_id, provider, source, status, failure_code, execution, records, "
                                           "retrieved_at_ms FROM energy_receipts WHERE " + " AND ".join(clauses)
                                           + " ORDER BY retrieved_at_ms, receipt_id", params).fetchall()]

    def citation(self, vintage):
        """What a reader needs to check a figure: source, release vintage, status, as-of and receipt."""

        record = vintage["record"]
        receipt = self.receipt_row(vintage["receipt_id"]) if vintage.get("receipt_id") else None
        return {"provider": record["provider"], "dataset": record["dataset"], "source_url": record["source_url"],
                "attribution": record["attribution"], "licence": record["licence"],
                "series_id": vintage["series_id"], "vintage_id": vintage["vintage_id"], "sequence": vintage["sequence"],
                "release": record["release"], "status": vintage["status"], "status_basis": record.get("status_basis"),
                "retrieved_at": record["retrieved_at"], "locator": record.get("locator") or {},
                "derived_from": record.get("derived_from"),
                "receipt": None if receipt is None else {k: receipt[k] for k in ("receipt_id", "response_sha256",
                                                                                 "execution", "retrieved_at_ms")}}


class EnergyProjector:
    """Source-pack runtime projector for ``noesis-energy-record-v1`` pages (the ``energy`` connector)."""

    SCOPES_FOR = staticmethod(lambda namespace: {WRITE_SCOPE, READ_SCOPE, f"namespace:{namespace}:write",
                                                 f"namespace:{namespace}:read"})

    def __init__(self, conn):
        self.store = EnergyStore(conn)

    @staticmethod
    def _namespace(source):
        return str(dict(source.get("energy") or {}).get("namespace") or "energy")

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        del documents
        namespace = self._namespace(source)
        payload = [dict(item["energy_record"]) for item in records if item.get("energy_record")]
        provider = str(dict(source.get("energy") or {}).get("provider"))
        receipt_id = self.store.receipt(
            namespace, provider=provider, source=f"{manifest['pack_id']}@{manifest['version']}:{source['source_id']}",
            request=dict(page_receipt.get("request") or {"step": page_receipt.get("step")}),
            response_sha256=page_receipt.get("response_sha256"), size=None, status="ok", execution="source-pack",
            run_id=run_id, records=len(payload), principal_id=principal_id, coverage=page_receipt.get("coverage"))
        return self.store.apply(namespace, payload, receipt_id=receipt_id, run_id=run_id, principal_id=principal_id,
                                scopes=self.SCOPES_FOR(namespace), execution="source-pack")

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del manifest
        namespace = self._namespace(source)
        provider = str(dict(source.get("energy") or {}).get("provider"))
        if status != "complete":
            self.store.fail(namespace, provider, EnergyStoreError("source_run_" + status, "source run did not complete"),
                            source=source["source_id"], request={}, run_id=run_id, principal_id=principal_id,
                            execution="source-pack")
        return {"status": status, "provider": provider, "namespace": namespace,
                "provider_state": self.store.provider_state(namespace, provider)}
