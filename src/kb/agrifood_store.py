"""Vintaged agri-food series store for the Agriculture and Food Systems pack (#2213, AF02 #2333).

Owns ``noesis-agrifood-record-v1`` and **reuses the Economics series storage**
(:class:`src.ingestion.connectors.dataset.store.ObservationStore`) rather than a
series store of its own:

* ``agrifood_series`` - one row per published series: publisher, dataset,
  commodity, place, market, program, element/measure, unit and period type
  (a series never mixes publishers, units or period types);
* ``agrifood_vintages`` - one row per release of a series (FAOSTAT domain
  update, NASS load time, PSD release month, Eurostat LAST UPDATE, or the
  content of a portal answer that states no release time), with the release
  time, its basis and the first retrieval;
* ``agrifood_values`` - one row per period of a vintage: value text, number,
  status, the flag code and label verbatim with its classes, the publisher's
  estimate type, the statement and its source. A different figure for the same
  period under an unchanged release is refused (``vintage_conflict``), never
  written over;
* the numeric values are mirrored into ``dataset_series`` /
  ``dataset_observations`` at ``series_id`` and ``as_of`` = the vintage's
  release time, so Economics' series readers see each vintage (withheld and
  missing values are ``NULL`` there; their text and flags stay here).

Commodity code lists and release declarations are kept in
``agrifood_commodities`` and ``agrifood_releases``; each source's run outcome is
a receipt row in ``agrifood_source_runs``. Runtime pages arrive through
:class:`AgrifoodProjector` (registered for ``noesis-agrifood-record-v1`` in
``src/ingestion/source_pack_runtime.py``).
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from src.kb.agrifood_records import (
    CONTRACT,
    FIGURE_TYPES,
    READ_SCOPE,
    AgrifoodError,
    authorize,
    canonical,
    digest,
    validate_statement,
)

_DDL = """
CREATE SEQUENCE IF NOT EXISTS agrifood_seq;
CREATE TABLE IF NOT EXISTS agrifood_series (
  namespace TEXT NOT NULL, series_id TEXT NOT NULL, provider TEXT NOT NULL, dataset TEXT NOT NULL,
  record_type TEXT NOT NULL, commodity_scheme TEXT NOT NULL, commodity_code TEXT NOT NULL, commodity_label TEXT,
  place_scheme TEXT NOT NULL, place_code TEXT NOT NULL, place_label TEXT, place_level TEXT, market TEXT, program TEXT,
  measure_kind TEXT NOT NULL, element TEXT NOT NULL, element_code TEXT, measure_label TEXT NOT NULL, unit TEXT,
  period_type TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, series_id)
);
CREATE TABLE IF NOT EXISTS agrifood_vintages (
  namespace TEXT NOT NULL, vintage_id TEXT NOT NULL, series_id TEXT NOT NULL, release_key TEXT NOT NULL,
  released_at TEXT, released_at_ms BIGINT, release_basis TEXT NOT NULL, as_of_ms BIGINT NOT NULL,
  first_observed_at_ms BIGINT NOT NULL, run_id TEXT, source_id TEXT, evidence_origin TEXT,
  PRIMARY KEY(namespace, vintage_id)
);
CREATE TABLE IF NOT EXISTS agrifood_values (
  namespace TEXT NOT NULL, vintage_id TEXT NOT NULL, period_key TEXT NOT NULL, seq BIGINT NOT NULL,
  period_value TEXT NOT NULL, period_reference TEXT, value_text TEXT, value_number TEXT, value_status TEXT NOT NULL,
  flag_code TEXT, flag_label TEXT, flag_classes TEXT NOT NULL, flag_vocabulary TEXT NOT NULL, flag_note TEXT,
  estimate_type TEXT NOT NULL, content_sha TEXT NOT NULL, statement_json TEXT NOT NULL, observed_at_ms BIGINT NOT NULL,
  run_id TEXT, source_id TEXT, document_id TEXT, PRIMARY KEY(namespace, vintage_id, period_key)
);
CREATE TABLE IF NOT EXISTS agrifood_commodities (
  namespace TEXT NOT NULL, provider TEXT NOT NULL, scheme TEXT NOT NULL, code TEXT NOT NULL, label TEXT,
  statement_json TEXT NOT NULL, observed_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, scheme, code, provider)
);
CREATE TABLE IF NOT EXISTS agrifood_releases (
  namespace TEXT NOT NULL, release_id TEXT NOT NULL, provider TEXT NOT NULL, dataset TEXT NOT NULL,
  release_key TEXT NOT NULL, released_at TEXT, basis TEXT NOT NULL, statement_json TEXT NOT NULL,
  observed_at_ms BIGINT NOT NULL, run_id TEXT, PRIMARY KEY(namespace, release_id)
);
CREATE TABLE IF NOT EXISTS agrifood_source_runs (
  namespace TEXT NOT NULL, run_id TEXT NOT NULL, source_id TEXT NOT NULL, provider TEXT, status TEXT NOT NULL,
  outcomes_json TEXT NOT NULL, cutoff_seq BIGINT NOT NULL, evidence_origin TEXT, finished_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, run_id, source_id)
);
"""
TABLES = ("agrifood_series", "agrifood_vintages", "agrifood_values", "agrifood_commodities", "agrifood_releases",
          "agrifood_source_runs")
_FREQUENCY = {"calendar-year": "annual", "month": "monthly", "week": "weekly"}
_SERIES_COLUMNS = ("series_id", "provider", "dataset", "record_type", "commodity_scheme", "commodity_code",
                   "commodity_label", "place_scheme", "place_code", "place_label", "place_level", "market", "program",
                   "measure_kind", "element", "element_code", "measure_label", "unit", "period_type")


def table_exists(conn: Any, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name=?", [name]).fetchone())


def release_ms(value: Any) -> int | None:
    """A published release time (date, month or timestamp) as epoch ms, UTC; None when not stated."""
    text = str(value or "").strip().replace(" ", "T")
    if not text:
        return None
    if len(text) == 7:
        text += "-01"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


class AgrifoodStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            for sql in _DDL.split(";"):
                if sql.strip():
                    conn.execute(sql)

    def ready(self) -> bool:
        return table_exists(self.conn, "agrifood_values")

    def require_ready(self) -> None:
        if not self.ready():
            raise AgrifoodError("not_ready", "no agri-food record has been acquired yet")

    # ------------------------------------------------------------------ writes

    def _series(self, namespace: str, value: Mapping[str, Any], observed: int) -> str:
        series_id = value["series_key"]
        commodity, place, measure = value["commodity"], value["place"], value["measure"]
        self.conn.execute(
            "INSERT OR IGNORE INTO agrifood_series VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, series_id, value["provider"], value["dataset"], value["record_type"], commodity["scheme"],
             commodity["code"], commodity.get("label"), place["scheme"], place["code"], place.get("label"),
             place.get("level"), value.get("market"), value.get("program"), measure["kind"], measure["element"],
             measure.get("element_code"), measure["label"], value.get("unit"), value["period"]["type"], observed])
        return series_id

    def _vintage(self, namespace, series_id, release, observed, run_id, source_id, origin) -> dict[str, Any]:
        vintage_id = "agrifood-vintage:" + digest([namespace, series_id, release["key"]])[:24]
        row = self.conn.execute("SELECT as_of_ms FROM agrifood_vintages WHERE namespace=? AND vintage_id=?",
                                [namespace, vintage_id]).fetchone()
        if row is not None:
            return {"vintage_id": vintage_id, "as_of_ms": int(row[0]), "created": False}
        published = release_ms(release.get("released_at"))
        as_of = published if published is not None else observed
        self.conn.execute(
            "INSERT INTO agrifood_vintages VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, vintage_id, series_id, release["key"], release.get("released_at"), published,
             release["basis"] if published is not None else
             f"{release['basis']}; the publisher states no release time, so the first retrieval dates this vintage",
             as_of, observed, run_id, source_id, origin])
        return {"vintage_id": vintage_id, "as_of_ms": as_of, "created": True}

    def _mirror(self, series: Mapping[str, Any], vintage_id: str, as_of_ms: int) -> None:
        """Numeric values of one vintage into the Economics ObservationStore (dataset_series/_observations)."""
        from services.ingest.common.series_model import Observation, SeriesRecord
        from src.ingestion.connectors.dataset.store import ObservationStore

        observations = ObservationStore(self.conn)
        rows = self.conn.execute(
            "SELECT period_key, value_number FROM agrifood_values WHERE vintage_id=? ORDER BY period_key",
            [vintage_id]).fetchall()
        record = SeriesRecord(
            series_id=series["series_id"], provider=f"agrifood:{series['provider']}",
            title=f"{series['commodity_label'] or series['commodity_code']} - {series['measure_label']} - "
                  f"{series['place_label'] or series['place_code']}",
            frequency=_FREQUENCY.get(series["period_type"], "irregular"), as_of=int(as_of_ms),
            observations=[Observation(period=p, value=None if n is None else float(n)) for p, n in rows],
            unit=series["unit"], geography=f"{series['place_scheme']}:{series['place_code']}",
            metadata={"pack": "agrifood", "period_type": series["period_type"],
                      "flags_and_text_in": "agrifood_values (vintage_id)", "vintage_id": vintage_id})
        header = self.conn.execute("SELECT as_of FROM dataset_series WHERE series_id=?",
                                   [record.series_id]).fetchone()
        if header is None or int(header[0]) <= record.as_of:
            observations.upsert(record)
        else:  # an older vintage arriving late: its rows only, the header keeps the newest release
            for item in record.observations:
                self.conn.execute(
                    "INSERT INTO dataset_observations (series_id, period, as_of, value) VALUES (?,?,?,?) "
                    "ON CONFLICT (series_id, period, as_of) DO UPDATE SET value=excluded.value",
                    [record.series_id, item.period, record.as_of, item.value])

    def _apply(self, namespace: str, statement: Mapping[str, Any], *, run_id, source_id, document_id,
               observed: int, origin, touched: dict[str, tuple[str, int]]) -> str:
        value = validate_statement(statement)
        record_type = value["record_type"]
        if record_type == "commodity":
            commodity = value["commodity"]
            inserted = self.conn.execute(
                "INSERT OR IGNORE INTO agrifood_commodities VALUES (?,?,?,?,?,?,?) RETURNING code",
                [namespace, value["provider"], commodity["scheme"], commodity["code"], commodity.get("label"),
                 canonical(value), observed]).fetchall()
            return "created" if inserted else "unchanged"
        if record_type == "release":
            release = value["release"]
            release_id = "agrifood-release:" + digest([namespace, value["provider"], value["dataset"],
                                                       release["key"]])[:24]
            inserted = self.conn.execute(
                "INSERT OR IGNORE INTO agrifood_releases VALUES (?,?,?,?,?,?,?,?,?,?) RETURNING release_id",
                [namespace, release_id, value["provider"], value["dataset"], release["key"], release.get("released_at"),
                 release["basis"], canonical(value), observed, run_id]).fetchall()
            return "created" if inserted else "unchanged"
        series_id = self._series(namespace, value, observed)
        vintage = self._vintage(namespace, series_id, value["release"], observed, run_id, source_id, origin)
        period = value["period"]
        content = {k: value[k] for k in ("value", "flag", "estimate_type", "period", "unit", "measure")}
        content_sha = digest(content)
        row = self.conn.execute(
            "SELECT content_sha FROM agrifood_values WHERE namespace=? AND vintage_id=? AND period_key=?",
            [namespace, vintage["vintage_id"], period["key"]]).fetchone()
        if row is not None:
            if row[0] == content_sha:
                return "unchanged"
            raise AgrifoodError("vintage_conflict", "the publisher republished another figure under an unchanged "
                                                    "release; it is refused rather than written over",
                                series_id=series_id, period=period["key"], release=value["release"]["key"])
        seq = self.conn.execute("SELECT nextval('agrifood_seq')").fetchone()[0]
        figure, flag = value["value"], value["flag"]
        self.conn.execute(
            "INSERT INTO agrifood_values VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, vintage["vintage_id"], period["key"], seq, period["value"], period.get("reference"),
             figure["text"], figure["number"], figure["status"], flag["code"], flag["label"], canonical(flag["classes"]),
             flag["vocabulary"], flag.get("note"), value["estimate_type"], content_sha, canonical(value), observed,
             run_id, source_id, document_id])
        touched[vintage["vintage_id"]] = (series_id, vintage["as_of_ms"])
        prior = self.conn.execute(
            "SELECT 1 FROM agrifood_values v JOIN agrifood_vintages g ON g.namespace=v.namespace AND "
            "g.vintage_id=v.vintage_id WHERE v.namespace=? AND g.series_id=? AND v.period_key=? AND v.vintage_id<>? "
            "LIMIT 1", [namespace, series_id, period["key"], vintage["vintage_id"]]).fetchone()
        return "revised" if prior else "created"

    def observe(self, namespace: str, statements: Sequence[Mapping[str, Any]], *, run_id: str | None = None,
                source_id: str | None = None, document_ids: Sequence[str | None] | None = None,
                observed_at_ms: int | None = None) -> dict[str, Any]:
        """Keep a batch of statements in one transaction; replays add nothing, conflicts roll back."""
        observed = observed_at_ms if observed_at_ms is not None else self.now()
        counts = {"created": 0, "revised": 0, "unchanged": 0}
        touched: dict[str, tuple[str, int]] = {}
        ids = list(document_ids or [])
        self.conn.execute("BEGIN")
        try:
            for index, item in enumerate(statements):
                origin = dict(item.get("source") or {}).get("evidence_origin")
                status = self._apply(namespace, item, run_id=run_id, source_id=source_id,
                                     document_id=ids[index] if index < len(ids) else None, observed=observed,
                                     origin=origin, touched=touched)
                counts[status] += 1
            for vintage_id, (series_id, as_of_ms) in sorted(touched.items()):
                self._mirror(self.series(namespace, series_id), vintage_id, as_of_ms)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {"counts": counts, "vintages_touched": sorted(touched)}

    def apply(self, namespace: str, statement: Mapping[str, Any], **kwargs: Any) -> dict[str, Any]:
        return self.observe(namespace, [statement], **kwargs)

    def record_run(self, namespace: str, run_id: str, source_id: str, *, provider: str | None, status: str,
                   outcomes: Sequence[Mapping[str, Any]], evidence_origin: str | None) -> dict[str, Any]:
        cutoff = self.conn.execute("SELECT coalesce(max(seq), 0) FROM agrifood_values WHERE namespace=?",
                                   [namespace]).fetchone()[0]
        self.conn.execute(
            "INSERT OR REPLACE INTO agrifood_source_runs VALUES (?,?,?,?,?,?,?,?,?)",
            [namespace, run_id, source_id, provider, status, canonical(list(outcomes)), int(cutoff), evidence_origin,
             self.now()])
        return {"namespace": namespace, "run_id": run_id, "source_id": source_id, "status": status,
                "cutoff_seq": int(cutoff), "outcomes": list(outcomes)}

    # ------------------------------------------------------------------ reads

    def series(self, namespace: str, series_id: str) -> dict[str, Any]:
        row = self.conn.execute(f"SELECT {', '.join(_SERIES_COLUMNS)} FROM agrifood_series WHERE namespace=? AND "
                                "series_id=?", [namespace, series_id]).fetchone()
        if row is None:
            raise AgrifoodError("not_found", "series is not visible in this namespace")
        return dict(zip(_SERIES_COLUMNS, row))

    def series_list(self, namespace: str, *, provider: str | None = None, commodities: Iterable[tuple[str, str]] = (),
                    places: Iterable[tuple[str, str]] = ()) -> list[dict[str, Any]]:
        if not self.ready():
            return []
        rows = self.conn.execute(
            f"SELECT {', '.join(_SERIES_COLUMNS)} FROM agrifood_series WHERE namespace=? AND (? IS NULL OR provider=?) "
            "ORDER BY provider, dataset, commodity_code, place_code, element, unit, series_id",
            [namespace, provider, provider]).fetchall()
        wanted_c, wanted_p = set(commodities), set(places)
        result = []
        for row in rows:
            item = dict(zip(_SERIES_COLUMNS, row))
            if wanted_c and (item["commodity_scheme"], item["commodity_code"]) not in wanted_c:
                continue
            if wanted_p and (item["place_scheme"], item["place_code"]) not in wanted_p:
                continue
            result.append(item)
        return result

    def vintages(self, namespace: str, series_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT vintage_id, release_key, released_at, released_at_ms, release_basis, as_of_ms, first_observed_at_ms, "
            "run_id, source_id, evidence_origin FROM agrifood_vintages WHERE namespace=? AND series_id=? "
            "ORDER BY as_of_ms, release_key", [namespace, series_id]).fetchall()
        keys = ("vintage_id", "release_key", "released_at", "released_at_ms", "release_basis", "as_of_ms",
                "first_observed_at_ms", "run_id", "source_id", "evidence_origin")
        return [dict(zip(keys, r)) for r in rows]

    def values(self, namespace: str, vintage_id: str, *, cutoff_seq: int | None = None) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT period_key, seq, period_value, period_reference, value_text, value_number, value_status, flag_code, "
            "flag_label, flag_classes, flag_vocabulary, flag_note, estimate_type, statement_json, observed_at_ms, "
            "run_id, source_id, document_id FROM agrifood_values WHERE namespace=? AND vintage_id=? "
            "AND (? IS NULL OR seq<=?) ORDER BY period_key", [namespace, vintage_id, cutoff_seq, cutoff_seq]).fetchall()
        result = []
        for r in rows:
            statement = json.loads(r[13])
            result.append({
                "period_key": r[0], "seq": int(r[1]), "period": statement["period"], "value_text": r[4],
                "value": r[5], "status": r[6],
                "flag": {"vocabulary": r[10], "code": r[7], "label": r[8], "classes": json.loads(r[9]), "note": r[11]},
                "estimate_type": r[12], "statement": statement, "observed_at_ms": int(r[14]), "run_id": r[15],
                "source_id": r[16], "document_id": r[17]})
        return result

    def commodities(self, namespace: str) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "agrifood_commodities"):
            return []
        rows = self.conn.execute("SELECT provider, scheme, code, label FROM agrifood_commodities WHERE namespace=? "
                                 "ORDER BY scheme, code, provider", [namespace]).fetchall()
        return [dict(zip(("provider", "scheme", "code", "label"), r)) for r in rows]

    def releases(self, namespace: str, *, provider: str | None = None) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "agrifood_releases"):
            return []
        rows = self.conn.execute(
            "SELECT release_id, provider, dataset, release_key, released_at, basis FROM agrifood_releases "
            "WHERE namespace=? AND (? IS NULL OR provider=?) ORDER BY provider, dataset, released_at, release_key",
            [namespace, provider, provider]).fetchall()
        return [dict(zip(("release_id", "provider", "dataset", "release_key", "released_at", "basis"), r))
                for r in rows]

    def generation(self, namespace: str) -> int:
        if not self.ready():
            return 0
        return int(self.conn.execute("SELECT coalesce(max(seq), 0) FROM agrifood_values WHERE namespace=?",
                                     [namespace]).fetchone()[0])

    def runs(self, namespace: str, run_id: str | None = None) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "agrifood_source_runs"):
            return []
        rows = self.conn.execute(
            "SELECT run_id, source_id, provider, status, outcomes_json, cutoff_seq, evidence_origin, finished_at_ms "
            "FROM agrifood_source_runs WHERE namespace=? AND (? IS NULL OR run_id=?) ORDER BY finished_at_ms, "
            "source_id", [namespace, run_id, run_id]).fetchall()
        return [{"run_id": r[0], "source_id": r[1], "provider": r[2], "status": r[3], "outcomes": json.loads(r[4]),
                 "cutoff_seq": int(r[5]), "evidence_origin": r[6], "finished_at_ms": int(r[7])} for r in rows]

    def provider_state(self, namespace: str, provider: str) -> dict[str, Any]:
        runs = [r for r in self.runs(namespace) if r["provider"] == provider]
        done = [r for r in runs if r["status"] == "complete"]
        return {"runs": len(runs), "last_success_ms": done[-1]["finished_at_ms"] if done else None,
                "last_evidence_origin": done[-1]["evidence_origin"] if done else None,
                "last_status": runs[-1]["status"] if runs else None}


def read_store(conn: Any, namespace: str, scopes: Iterable[str]) -> AgrifoodStore:
    authorize(namespace, scopes, READ_SCOPE)
    store = AgrifoodStore(conn, initialize=False)
    store.require_ready()
    return store


class AgrifoodProjector:
    """Runtime projector for ``noesis-agrifood-record-v1`` pages."""

    def __init__(self, conn: Any) -> None:
        self.store = AgrifoodStore(conn)
        self._outcomes: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._origin: dict[tuple[str, str], str] = {}

    @staticmethod
    def _namespace(source: Mapping[str, Any]) -> str:
        return str(dict(source.get("agrifood") or {}).get("namespace") or "global")

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        del manifest, principal_id
        namespace = self._namespace(source)
        statements = [dict(r["agrifood_record"]) for r in records if r.get("agrifood_record")]
        if any(s.get("contract") != CONTRACT for s in statements):
            raise AgrifoodError("invalid_record", "page record lacks an agri-food statement")
        by_id = {str(dict(d.get("metadata") or {}).get("source_pack_record_id")): d["document_id"] for d in documents}
        result = self.store.observe(namespace, statements, run_id=run_id, source_id=source["source_id"],
                                    document_ids=[by_id.get(str(r.get("id"))) for r in records
                                                  if r.get("agrifood_record")])
        key = (run_id, source["source_id"])
        receipt = dict(page_receipt or {})
        if receipt.get("selection"):
            self._outcomes.setdefault(key, []).append(
                {"selection": receipt["selection"], "outcome": receipt.get("outcome"),
                 "statements": len(statements),
                 "figures": sum(1 for s in statements if s["record_type"] in FIGURE_TYPES),
                 "missing": receipt.get("missing", [])})
        if receipt.get("evidence_origin"):
            self._origin[key] = receipt["evidence_origin"]
        return result["counts"]

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del manifest, principal_id
        key = (run_id, source["source_id"])
        return self.store.record_run(self._namespace(source), run_id, source["source_id"],
                                     provider=dict(source.get("agrifood") or {}).get("provider"), status=status,
                                     outcomes=self._outcomes.pop(key, []), evidence_origin=self._origin.pop(key, None))


__all__ = ["AgrifoodProjector", "AgrifoodStore", "TABLES", "read_store", "release_ms", "table_exists"]
