"""Extractives records with revisions and as-of lookup, and commodity series over the Economics storage (EX02).

Owns the ``extractives_*`` tables, following the append-only revision pattern of :mod:`src.kb.entity_history`
(nothing is updated or deleted; a later state is a new revision that points at the one before):

* ``extractives_releases`` - one acquired publication (an EITI report version, a USGS MCS annual release table, a
  BGS World Mineral Statistics publication) with its release clock (declared publication date), retrieval time,
  digests, evidence origin and live-verification status;
* ``extractives_records`` - EITI records (report, government agencies, revenue streams, companies, projects with
  licences, company payments) as immutable revisions per record key: a report version that changes a record adds a
  revision, one that no longer states it adds a ``removed`` revision;
* ``extractives_series`` / ``extractives_vintages`` / ``extractives_values`` - commodity series keyed by source,
  commodity, statistic, unit and country; each publication that changes values is an appended vintage with value
  text, status (reported, withheld, not_available, qualitative), estimated and revised markers and notes verbatim.
  The numbers of every vintage are registered through :func:`src.domains.economic.model.register_series` (the
  Economics series storage); no second numeric store.

Each record carries its source, record revision and as-of time. A publication that changes content under an
unchanged release identity is refused (``vintage_conflict``); an older report version arriving after a newer one
is refused (``stale_version``). Personal fields are refused at write time (:func:`check_minimised`).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from src.kb.extractives_records import (
    CONTRACT,
    DEFAULT_NAMESPACE,
    ECONOMIC_DOMAIN,
    EITI_RECORD_TYPES,
    READ_SCOPE,
    ExtractivesError,
    authorize,
    canonical,
    check_minimised,
    digest,
    forbidden_keys,
    iso_from_ms,
    load,
    release_ms,
    table_exists,
)

_DDL = """
CREATE SEQUENCE IF NOT EXISTS extractives_seq;
CREATE TABLE IF NOT EXISTS extractives_releases (
  namespace TEXT NOT NULL, release_id TEXT NOT NULL, provider TEXT NOT NULL, source_id TEXT, run_id TEXT,
  document_label TEXT NOT NULL, report_key TEXT, release_version TEXT, published_on TEXT, release_label TEXT,
  release_at_ms BIGINT NOT NULL, release_basis TEXT NOT NULL, retrieved_at_ms BIGINT NOT NULL,
  file_sha256 TEXT NOT NULL, content_sha256 TEXT NOT NULL, evidence_origin TEXT NOT NULL,
  live_verification TEXT NOT NULL, url TEXT, header_json TEXT NOT NULL, sequence BIGINT NOT NULL,
  PRIMARY KEY(namespace, release_id)
);
CREATE TABLE IF NOT EXISTS extractives_records (
  namespace TEXT NOT NULL, record_key TEXT NOT NULL, revision INTEGER NOT NULL, revision_id TEXT NOT NULL,
  record_type TEXT NOT NULL, report_key TEXT NOT NULL, release_id TEXT NOT NULL, release_version TEXT,
  release_at_ms BIGINT NOT NULL, observed_at_ms BIGINT NOT NULL, state TEXT NOT NULL, content_hash TEXT NOT NULL,
  record_json TEXT NOT NULL, revision_of TEXT, sequence BIGINT NOT NULL, PRIMARY KEY(namespace, record_key, revision)
);
CREATE TABLE IF NOT EXISTS extractives_series (
  namespace TEXT NOT NULL, series_id TEXT NOT NULL, provider TEXT NOT NULL, source_series_id TEXT NOT NULL,
  commodity_code TEXT NOT NULL, commodity_json TEXT NOT NULL, statistic TEXT NOT NULL, unit TEXT NOT NULL,
  frequency TEXT NOT NULL, country_iso2 TEXT, country_json TEXT NOT NULL, licence_json TEXT NOT NULL,
  table_label TEXT, first_release_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, series_id)
);
CREATE TABLE IF NOT EXISTS extractives_vintages (
  namespace TEXT NOT NULL, vintage_id TEXT NOT NULL, series_id TEXT NOT NULL, release_id TEXT NOT NULL,
  release_at_ms BIGINT NOT NULL, retrieved_at_ms BIGINT NOT NULL, content_hash TEXT NOT NULL,
  economic_vintage_id TEXT, sequence BIGINT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, vintage_id)
);
CREATE TABLE IF NOT EXISTS extractives_values (
  namespace TEXT NOT NULL, vintage_id TEXT NOT NULL, period TEXT NOT NULL, column_name TEXT, value_text TEXT,
  value TEXT, status TEXT NOT NULL, estimated BOOLEAN NOT NULL, revised BOOLEAN NOT NULL, reference TEXT,
  notes_json TEXT NOT NULL, row_number INTEGER, PRIMARY KEY(namespace, vintage_id, period)
);
"""
TABLES = ("extractives_releases", "extractives_records", "extractives_series", "extractives_vintages",
          "extractives_values")
SERIES_STATUSES = ("reported", "withheld", "not_available", "qualitative")
_SERIES_COLUMNS = ("series_id, provider, source_series_id, commodity_code, commodity_json, statistic, unit, "
                   "frequency, country_iso2, country_json, licence_json, table_label, first_release_id")
_RECORD_COLUMNS = ("record_key, revision, revision_id, record_type, report_key, release_id, release_version, "
                   "release_at_ms, observed_at_ms, state, content_hash, record_json, revision_of")


def _check_series(item: Mapping[str, Any]) -> None:
    if forbidden_keys(dict(item)):
        raise ExtractivesError("invalid_release", "published series carry no own estimate, filled value or forecast")
    for key in ("provider", "source_series_id", "commodity", "statistic", "unit", "country", "licence"):
        if not item.get(key):
            raise ExtractivesError("invalid_release", f"a commodity series states its {key}")
    for obs in item.get("observations") or []:
        if obs.get("status") not in SERIES_STATUSES:
            raise ExtractivesError("invalid_release", "each value states its status")
        if obs["status"] != "reported" and obs.get("value") is not None:
            raise ExtractivesError("invalid_release", "a withheld or unavailable value carries no number (never "
                                   "filled)")


class ExtractivesStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            for sql in _DDL.split(";"):
                if sql.strip():
                    conn.execute(sql)

    def ready(self) -> bool:
        return table_exists(self.conn, "extractives_releases")

    def require_ready(self) -> None:
        if not self.ready():
            raise ExtractivesError("not_ready", "no extractives record has been acquired yet")

    def _seq(self) -> int:
        return int(self.conn.execute("SELECT nextval('extractives_seq')").fetchone()[0])

    # ------------------------------------------------------------------ writes

    def apply_release(self, namespace: str, header: Mapping[str, Any], items: Sequence[Mapping[str, Any]], *,
                      run_id: str | None = None, source_id: str | None = None) -> dict[str, Any]:
        """Apply one acquired publication in one transaction: replays add nothing, conflicts roll back."""
        if int(header.get("item_count", -1)) != len(items):
            raise ExtractivesError("incomplete_release", "a release carries every item it states")
        retrieved = self.now()
        clock = release_ms(header.get("published_on"), header.get("published_at"), retrieved)
        document = dict(header.get("document") or {})
        release_id = "extractives-release:" + digest([namespace, header["provider"], document.get("label"),
                                                      header.get("report_key"), header.get("release_version")])[:24]
        existing = self.conn.execute(
            "SELECT content_sha256 FROM extractives_releases WHERE namespace=? AND release_id=?",
            [namespace, release_id]).fetchone()
        if existing is not None:
            if existing[0] == header["content_sha256"]:
                return {"release_id": release_id, "status": "unchanged", "revisions": 0, "vintages": 0}
            raise ExtractivesError("vintage_conflict", "the publisher republished other content under an unchanged "
                                   "release version; it is refused rather than written over", release_id=release_id)
        if header["kind"] == "eiti":
            later = self.conn.execute(
                "SELECT release_version FROM extractives_releases WHERE namespace=? AND report_key=? AND "
                "release_at_ms>? LIMIT 1", [namespace, header["report_key"], clock]).fetchone()
            if later is not None:
                raise ExtractivesError("stale_version", "a later version of this report is already recorded; an older "
                                       "version is not applied over it", later_version=later[0])
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT INTO extractives_releases VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [namespace, release_id, header["provider"], source_id, run_id, str(document.get("label") or ""),
                 header.get("report_key"), header.get("release_version"), header.get("published_on"),
                 header.get("release_label"), clock, str(header["release_basis"]), retrieved, header["file_sha256"],
                 header["content_sha256"], str(header.get("evidence_origin") or "live"),
                 str(header.get("live_verification") or ""), header.get("url"), canonical(dict(header)), self._seq()])
            counts = {"created": 0, "revised": 0, "unchanged": 0, "removed": 0}
            vintages = 0
            if header["kind"] == "eiti":
                counts = self._apply_eiti(namespace, release_id, header, items, clock, retrieved)
            else:
                for item in items:
                    vintages += self._series_vintage(namespace, release_id, dict(item), clock, retrieved, header)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {"release_id": release_id, "status": "applied", "revisions": counts, "vintages": vintages}

    # EITI records -------------------------------------------------------

    def _latest(self, namespace: str, record_key: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            f"SELECT {_RECORD_COLUMNS} FROM extractives_records WHERE namespace=? AND record_key=? "
            "ORDER BY revision DESC LIMIT 1", [namespace, record_key]).fetchone()
        return None if row is None else self._revision_view(namespace, row)

    def _insert(self, namespace, key, record_type, report_key, release_id, header, clock, retrieved, state, record,
                previous) -> None:
        revision = 1 if previous is None else previous["revision"] + 1
        revision_id = "extractives-revision:" + digest([namespace, key, revision, release_id])[:24]
        self.conn.execute(
            "INSERT INTO extractives_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, key, revision, revision_id, record_type, report_key, release_id, header.get("release_version"),
             clock, retrieved, state, digest(record), canonical(record),
             previous["revision_id"] if previous else None, self._seq()])

    def _apply_eiti(self, namespace, release_id, header, items, clock, retrieved) -> dict[str, int]:
        counts = {"created": 0, "revised": 0, "unchanged": 0, "removed": 0}
        report_key = str(header["report_key"])
        seen = set()
        for item in items:
            record = {k: v for k, v in dict(item).items() if k not in {"kind", "provider"}}
            if record.get("record_type") not in EITI_RECORD_TYPES:
                raise ExtractivesError("invalid_release", "unknown EITI record type")
            check_minimised(record)
            key = str(record["record_key"])
            seen.add(key)
            previous = self._latest(namespace, key)
            if previous is not None and previous["state"] == "active" and previous["content_hash"] == digest(record):
                counts["unchanged"] += 1
                continue
            self._insert(namespace, key, record["record_type"], report_key, release_id, header, clock, retrieved,
                         "active", record, previous)
            counts["created" if previous is None else "revised"] += 1
        rows = self.conn.execute(
            "SELECT DISTINCT record_key FROM extractives_records WHERE namespace=? AND report_key=?",
            [namespace, report_key]).fetchall()
        for (key,) in rows:
            if key in seen:
                continue
            previous = self._latest(namespace, key)
            if previous["state"] == "removed":
                continue
            self._insert(namespace, key, previous["record_type"], report_key, release_id, header, clock, retrieved,
                         "removed", previous["record"], previous)
            counts["removed"] += 1
        return counts

    # Commodity series ---------------------------------------------------

    def _series_vintage(self, namespace, release_id, item, clock, retrieved, header) -> int:
        _check_series(item)
        series_id = "extractives-series:" + digest([namespace, item["provider"], item["source_series_id"]])[:24]
        country = dict(item["country"])
        self.conn.execute(
            "INSERT OR IGNORE INTO extractives_series VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, series_id, item["provider"], item["source_series_id"], item["commodity"]["code"],
             canonical(item["commodity"]), item["statistic"], item["unit"], item.get("frequency") or "annual",
             country.get("iso2"), canonical(country), canonical(item["licence"]), item.get("table"), release_id,
             self.now()])
        observations = [dict(o) for o in item.get("observations") or []]
        content = [{k: o.get(k) for k in ("period", "value_text", "value", "status", "estimated", "revised", "notes")}
                   for o in observations]
        content_hash = digest(content)
        latest = self.vintage_rows(namespace, series_id)
        if latest and latest[-1]["content_hash"] == content_hash:
            return 0  # an unchanged publication of this series adds no vintage
        if any(v["release_at_ms"] == clock for v in latest):
            raise ExtractivesError("vintage_conflict", "another publication of this series has the same release clock "
                                   "with other values; refused rather than written over", series_id=series_id)
        vintage_id = "extractives-vintage:" + digest([namespace, series_id, release_id])[:24]
        economic = self._register(series_id, item, observations, clock, retrieved, vintage_id, release_id, header)
        self.conn.execute(
            "INSERT INTO extractives_vintages VALUES (?,?,?,?,?,?,?,?,?,?)",
            [namespace, vintage_id, series_id, release_id, clock, retrieved, content_hash,
             economic["vintage"]["vintage_id"], self._seq(), self.now()])
        for obs in observations:
            self.conn.execute(
                "INSERT INTO extractives_values VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [namespace, vintage_id, obs["period"], obs.get("column"), obs.get("value_text"), obs.get("value"),
                 obs["status"], bool(obs.get("estimated")), bool(obs.get("revised")), obs.get("reference"),
                 canonical(obs.get("notes") or []), obs.get("row")])
        return 1

    def _register(self, series_id, item, observations, clock, retrieved, vintage_id, release_id, header):
        """The vintage's numbers into the shared Economics series storage (dataset tables and economic_vintages)."""
        from services.ingest.common.series_model import Observation, SeriesRecord
        from src.domains.economic.model import EconomicModelError, register_series

        country = dict(item["country"])
        geography = f"iso2:{country['iso2']}" if country.get("iso2") else f"aggregate:{country['name']}"
        commodity = dict(item["commodity"])
        record = SeriesRecord(
            series_id=series_id, provider=f"extractives:{item['provider']}",
            title=f"{commodity['label']} {item['statistic']} - {country['name']} ({item['provider']})",
            frequency="annual", as_of=int(clock),
            observations=[Observation(period=o["period"], value=None if o.get("value") is None else float(o["value"]))
                          for o in observations],
            unit=item["unit"], geography=geography, license=str(dict(item["licence"]).get("id") or ""),
            source_url=header.get("url"),
            metadata={"pack": "economics", "provider": "economics.extractives", "vintage_id": vintage_id,
                      "vintage_basis": header.get("release_basis"), "acquired_at_ms": int(retrieved),
                      "provider_release_at_ms": int(clock), "source_document_id": release_id,
                      "markers_and_text_in": "extractives_values (vintage_id)"})
        semantics = {
            "concept": f"extractives {commodity['code']} {item['statistic']}",
            "canonical_name": f"{commodity['label']} {item['statistic']}",
            "definition": str(dict(commodity["definition"]).get("text") or commodity["label"]),
            "provider_code": item["source_series_id"],
            "provider_definition": str(dict(commodity["definition"]).get("reference")),
            "unit": item["unit"],
            "geography": geography,
            "price_basis": "not_applicable",
            "seasonal_adjustment": "not_applicable",
        }
        try:
            return register_series(self.conn, record, semantics=semantics, domain=ECONOMIC_DOMAIN)
        except EconomicModelError as exc:
            raise ExtractivesError(exc.code, str(exc), series_id=series_id) from exc

    # ------------------------------------------------------------------ reads: releases

    def release(self, namespace: str, release_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT provider, source_id, document_label, report_key, release_version, published_on, release_label, "
            "release_at_ms, release_basis, retrieved_at_ms, file_sha256, evidence_origin, live_verification, url "
            "FROM extractives_releases WHERE namespace=? AND release_id=?", [namespace, release_id]).fetchone()
        if row is None:
            raise ExtractivesError("not_found", "release is not visible in this namespace")
        keys = ("provider", "source_id", "document", "report_key", "release_version", "published_on",
                "release_label", "release_at_ms", "release_basis", "retrieved_at_ms", "file_sha256",
                "evidence_origin", "live_verification", "url")
        out = {"release_id": release_id, **dict(zip(keys, row))}
        out["release_at"], out["retrieved_at"] = iso_from_ms(out["release_at_ms"]), iso_from_ms(out["retrieved_at_ms"])
        return out

    def source_revision(self, namespace: str, release_id: str) -> dict[str, Any]:
        """The citation of one release: publisher, document, version, release clock, digest and evidence origin."""
        from src.ingestion.extractives_sources import PROVIDER_CONTRACTS

        release = self.release(namespace, release_id)
        return {
            "release_id": release_id, "provider": release["provider"], "source_id": release["source_id"],
            "document": release["document"], "url": release["url"], "report_key": release["report_key"],
            "release_version": release["release_version"], "published_on": release["published_on"],
            "release_label": release["release_label"], "release_at": release["release_at"],
            "release_basis": release["release_basis"], "retrieved_at": release["retrieved_at"],
            "file_sha256": release["file_sha256"], "evidence_origin": release["evidence_origin"],
            "live_verification": release["live_verification"],
            "attribution": PROVIDER_CONTRACTS.get(release["provider"], {}).get("licence", {}).get("attribution"),
        }

    def releases(self, namespace: str, *, provider: str | None = None,
                 report_key: str | None = None) -> list[dict[str, Any]]:
        if not self.ready():
            return []
        rows = self.conn.execute(
            "SELECT release_id FROM extractives_releases WHERE namespace=? AND (? IS NULL OR provider=?) AND "
            "(? IS NULL OR report_key=?) ORDER BY release_at_ms, sequence",
            [namespace, provider, provider, report_key, report_key]).fetchall()
        return [self.release(namespace, r[0]) for r in rows]

    def latest_release_ms(self, namespace: str) -> int | None:
        if not self.ready():
            return None
        row = self.conn.execute("SELECT max(release_at_ms) FROM extractives_releases WHERE namespace=?",
                                [namespace]).fetchone()
        return None if row is None or row[0] is None else int(row[0])

    # ------------------------------------------------------------------ reads: EITI records

    def _revision_view(self, namespace: str, row: Sequence[Any]) -> dict[str, Any]:
        (key, revision, revision_id, record_type, report_key, release_id, version, release_at, observed_at, state,
         content_hash, record, revision_of) = row
        return {"contract": CONTRACT, "record_type": record_type, "namespace": namespace, "record_key": key,
                "revision": int(revision), "revision_id": revision_id, "revision_of": revision_of, "state": state,
                "report_key": report_key, "release_id": release_id, "report_version": version,
                "release_at_ms": int(release_at), "as_of": iso_from_ms(release_at),
                "observed_at": iso_from_ms(observed_at), "content_hash": content_hash, "record": load(record, {})}

    def history(self, namespace: str, record_key: str) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "extractives_records"):
            return []
        rows = self.conn.execute(
            f"SELECT {_RECORD_COLUMNS} FROM extractives_records WHERE namespace=? AND record_key=? ORDER BY revision",
            [namespace, record_key]).fetchall()
        return [self._revision_view(namespace, r) for r in rows]

    def record(self, namespace: str, record_key: str, *, as_of_ms: int | None = None,
               include_removed: bool = False) -> dict[str, Any] | None:
        """The revision in force at ``as_of_ms`` (by the report version's publication date); latest by default."""
        rows = [r for r in self.history(namespace, record_key) if as_of_ms is None or r["release_at_ms"] <= as_of_ms]
        if not rows or (rows[-1]["state"] == "removed" and not include_removed):
            return None
        return rows[-1]

    def records(self, namespace: str, *, record_types: Sequence[str] | None = None, report_key: str | None = None,
                as_of_ms: int | None = None, include_removed: bool = False) -> list[dict[str, Any]]:
        """The revisions in force at ``as_of_ms`` of every record key (optionally of one report and type)."""
        if not table_exists(self.conn, "extractives_records"):
            return []
        rows = self.conn.execute(
            f"SELECT {_RECORD_COLUMNS} FROM extractives_records WHERE namespace=? AND (? IS NULL OR report_key=?) "
            "AND (? IS NULL OR release_at_ms<=?) ORDER BY record_key, revision",
            [namespace, report_key, report_key, as_of_ms, as_of_ms]).fetchall()
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            latest[row[0]] = self._revision_view(namespace, row)
        out = []
        for view in latest.values():
            if record_types and view["record_type"] not in record_types:
                continue
            if view["state"] == "removed" and not include_removed:
                continue
            out.append(view)
        return out

    def report_versions(self, namespace: str, report_key: str) -> list[dict[str, Any]]:
        return [{"release_id": r["release_id"], "report_version": r["release_version"],
                 "published_on": r["published_on"], "release_at": r["release_at"],
                 "evidence_origin": r["evidence_origin"]} for r in self.releases(namespace, report_key=report_key)]

    def report_keys(self, namespace: str, *, country: str | None = None) -> list[str]:
        if not self.ready():
            return []
        rows = self.conn.execute(
            "SELECT DISTINCT report_key FROM extractives_releases WHERE namespace=? AND report_key IS NOT NULL "
            "ORDER BY report_key", [namespace]).fetchall()
        keys = [r[0] for r in rows]
        return [k for k in keys if country is None or k.split(":")[3] == country.upper()]

    def cite(self, namespace: str, view: Mapping[str, Any]) -> dict[str, Any]:
        """Source, record revision and as-of time of one record revision."""
        return {"record_key": view["record_key"], "revision_id": view["revision_id"], "revision": view["revision"],
                "report_version": view["report_version"], "as_of": view["as_of"], "observed_at": view["observed_at"],
                "source": self.source_revision(namespace, view["release_id"])}

    # ------------------------------------------------------------------ reads: series

    def _series_view(self, namespace: str, row: Sequence[Any]) -> dict[str, Any]:
        (series_id, provider, source_series_id, _commodity_code, commodity, statistic, unit, frequency, _iso2,
         country, licence, table_label, first_release) = row
        vintages = self.vintage_rows(namespace, series_id)
        return {"contract": CONTRACT, "record_type": "commodity_series", "namespace": namespace,
                "series_id": series_id, "provider": provider, "source_series_id": source_series_id,
                "commodity": load(commodity, {}), "statistic": statistic, "unit": unit, "frequency": frequency,
                "country": load(country, {}), "licence": load(licence, {}), "table": table_label,
                "first_release_id": first_release, "vintage_count": len(vintages),
                "current_vintage_id": vintages[-1]["vintage_id"] if vintages else None,
                "economic_series": {"domain": ECONOMIC_DOMAIN, "series_id": series_id,
                                    "store": "dataset_series/dataset_observations + economic_vintages"}}

    def series(self, namespace: str, series_id: str) -> dict[str, Any]:
        row = self.conn.execute(f"SELECT {_SERIES_COLUMNS} FROM extractives_series WHERE namespace=? AND series_id=?",
                                [namespace, series_id]).fetchone()
        if row is None:
            raise ExtractivesError("not_found", "series is not visible in this namespace")
        return self._series_view(namespace, row)

    def find_series(self, namespace: str, *, provider: str | None = None, commodity: str | None = None,
                    statistic: str | None = None, country: str | None = None) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "extractives_series"):
            return []
        rows = self.conn.execute(
            f"SELECT {_SERIES_COLUMNS} FROM extractives_series WHERE namespace=? AND (? IS NULL OR provider=?) AND "
            "(? IS NULL OR commodity_code=?) AND (? IS NULL OR statistic=?) AND (? IS NULL OR country_iso2=?) "
            "ORDER BY provider, commodity_code, statistic, country_iso2, series_id",
            [namespace, provider, provider, commodity, commodity, statistic, statistic,
             None if country is None else country.upper(), None if country is None else country.upper()]).fetchall()
        return [self._series_view(namespace, r) for r in rows]

    def vintage_rows(self, namespace: str, series_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT vintage_id, release_id, release_at_ms, retrieved_at_ms, content_hash, economic_vintage_id, sequence "
            "FROM extractives_vintages WHERE namespace=? AND series_id=? ORDER BY release_at_ms, sequence",
            [namespace, series_id]).fetchall()
        out, previous = [], None
        for r in rows:
            out.append({"vintage_id": r[0], "release_id": r[1], "release_at_ms": int(r[2]),
                        "release_at": iso_from_ms(r[2]), "retrieved_at_ms": int(r[3]),
                        "retrieved_at": iso_from_ms(r[3]), "content_hash": r[4], "economic_vintage_id": r[5],
                        "sequence": int(r[6]), "revision_of": previous})
            previous = r[0]
        return out

    def select_vintage(self, namespace: str, series_id: str, as_of_ms: int | None) -> dict[str, Any] | None:
        rows = self.vintage_rows(namespace, series_id)
        if as_of_ms is not None:
            rows = [v for v in rows if v["release_at_ms"] <= int(as_of_ms)]
        return rows[-1] if rows else None

    def values(self, namespace: str, vintage_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT period, column_name, value_text, value, status, estimated, revised, reference, notes_json "
            "FROM extractives_values WHERE namespace=? AND vintage_id=? ORDER BY period",
            [namespace, vintage_id]).fetchall()
        return [{"period": r[0], "column": r[1], "value_text": r[2], "value": r[3], "status": r[4],
                 "estimated": bool(r[5]), "revised": bool(r[6]), "reference": r[7], "notes": load(r[8], [])}
                for r in rows]


def read_store(conn: Any, namespace: str, scopes) -> ExtractivesStore:
    authorize(namespace, scopes, READ_SCOPE)
    store = ExtractivesStore(conn, initialize=False)
    store.require_ready()
    return store


class ExtractivesProjector:
    """Source-pack runtime projector for ``noesis-extractives-record-v1`` pages (one release per page)."""

    def __init__(self, conn: Any) -> None:
        self.store = ExtractivesStore(conn)

    @staticmethod
    def _namespace(source: Mapping[str, Any]) -> str:
        return str(dict(source.get("extractives") or {}).get("namespace") or DEFAULT_NAMESPACE)

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        del manifest, documents, page_receipt, principal_id
        groups: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
        for item in records:
            header, body = dict(item.get("extractives_release") or {}), item.get("extractives_item")
            if not header or not isinstance(body, Mapping):
                raise ExtractivesError("invalid_record", "page record is not an extractives release item")
            groups.setdefault(header["file_sha256"] + canonical(header.get("document")), (header, []))[1].append(
                dict(body))
        namespace = self._namespace(source)
        return [self.store.apply_release(namespace, header, items, run_id=run_id, source_id=source["source_id"])
                for header, items in groups.values()]

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del run_id, manifest, principal_id
        row = self.store.conn.execute(
            "SELECT release_id, release_version FROM extractives_releases WHERE namespace=? AND source_id=? "
            "ORDER BY release_at_ms DESC, sequence DESC LIMIT 1", [self._namespace(source), source["source_id"]],
        ).fetchone()
        return {"status": status, "latest_release_id": row[0] if row else None,
                "latest_release_version": row[1] if row else None}


def register_schemas(conn: Any, *, principal_id: str, scopes: Any) -> list[dict[str, Any]]:
    """Register the extractives record contract as a schema module in the shared registry."""
    import json
    from pathlib import Path

    from src.kb.schema_registry import SchemaRegistry

    path = Path(__file__).resolve().parents[2] / "contracts/schemas/jsonschema" / f"{CONTRACT}.json"
    definition = {
        "contract": "noesis-schema-module-v1", "name": "extractives-record", "kind": "schema",
        "semantic_version": "1.0.0", "content": json.loads(path.read_text()), "owner": "economics.extractives",
        "dependencies": [], "compatibility_policy": "backward",
        "provenance": {"kind": "imported", "source": f"contracts/schemas/jsonschema/{CONTRACT}.json"},
        "actor": {"principal_id": principal_id, "kind": "service"},
    }
    return [SchemaRegistry(conn).register(definition, "extractives-schema:extractives-record:1.0.0",
                                          principal_id=principal_id, scopes=scopes)]


__all__ = ["TABLES", "ExtractivesProjector", "ExtractivesStore", "read_store", "register_schemas"]
