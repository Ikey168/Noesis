"""Logistics indicator mappings and vintaged observations over the Economics series storage (#2229, SL02-SL06).

Owns the ``logistics_*`` mapping tables and **reuses the existing series store**: every vintage's numeric values
are registered through :func:`src.domains.economic.model.register_series` (``dataset_series`` /
``dataset_observations`` keyed by ``as_of`` = the release clock, ``economic_vintages`` with release and retrieval
clocks, basis labels and ``revision_of``, and the bitemporal ledger). The tables here keep what the shared contract
has no place for:

* ``logistics_releases`` - one acquired publication (a UN/LOCODE release, a UNCTADstat report, a Eurostat cube, a
  BLS answer) with its release clock and basis, digests, evidence origin and live-verification status;
* ``logistics_series`` - the indicator mapping: provider, source series id, dataset, concept, measure, unit,
  frequency, geography kind (``port``, ``country`` or a published ``route``) with the source's own codes (and a
  UN/LOCODE only where the source publishes one), definition reference, licence and, for a freight index, the
  licence decision of SL01;
* ``logistics_vintages`` / ``logistics_values`` - each release of a series (only when its values changed; an
  unchanged release adds nothing), with value text, status, flags and footnotes verbatim and the licence on every
  freight-index observation;
* ``logistics_breaks`` - series breaks from published methodology notes.

A publication that changes values under an unchanged release clock is refused (``vintage_conflict``). Port records
are applied by :class:`src.kb.logistics_ports.LogisticsPorts`. Nothing forecasts, rebases or interpolates.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from src.kb.logistics_records import (
    CONTRACT,
    DEFAULT_NAMESPACE,
    ECONOMIC_DOMAIN,
    READ_SCOPE,
    STATUSES,
    WRITE_SCOPE,
    LogisticsError,
    authorize,
    canonical,
    digest,
    forbidden_keys,
    iso_from_ms,
    load,
    release_ms,
    table_exists,
)

_DDL = """
CREATE SEQUENCE IF NOT EXISTS logistics_seq;
CREATE TABLE IF NOT EXISTS logistics_releases (
  namespace TEXT NOT NULL, release_id TEXT NOT NULL, provider TEXT NOT NULL, source_id TEXT, run_id TEXT,
  document_label TEXT NOT NULL, release_key TEXT NOT NULL, published_on TEXT, release_label TEXT,
  release_version TEXT, release_at_ms BIGINT NOT NULL, release_basis TEXT NOT NULL, retrieved_at_ms BIGINT NOT NULL,
  file_sha256 TEXT NOT NULL, content_sha256 TEXT NOT NULL, evidence_origin TEXT NOT NULL,
  live_verification TEXT NOT NULL, url TEXT, header_json TEXT NOT NULL, sequence BIGINT NOT NULL,
  PRIMARY KEY(namespace, release_id)
);
CREATE TABLE IF NOT EXISTS logistics_series (
  namespace TEXT NOT NULL, series_id TEXT NOT NULL, provider TEXT NOT NULL, source_series_id TEXT NOT NULL,
  dataset TEXT, concept TEXT NOT NULL, measure_label TEXT NOT NULL, unit_json TEXT, frequency TEXT NOT NULL,
  geo_kind TEXT NOT NULL, geo_scheme TEXT NOT NULL, geo_code TEXT NOT NULL, geo_label TEXT, geo_unlocode TEXT,
  partner_scheme TEXT, partner_code TEXT, partner_label TEXT, partner_unlocode TEXT, dimensions_json TEXT NOT NULL,
  definition_json TEXT NOT NULL, licence_json TEXT NOT NULL, freight_index_json TEXT, first_release_id TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, series_id)
);
CREATE TABLE IF NOT EXISTS logistics_vintages (
  namespace TEXT NOT NULL, vintage_id TEXT NOT NULL, series_id TEXT NOT NULL, release_id TEXT NOT NULL,
  release_at_ms BIGINT NOT NULL, release_basis TEXT NOT NULL, retrieved_at_ms BIGINT NOT NULL,
  content_hash TEXT NOT NULL, economic_vintage_id TEXT, sequence BIGINT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, vintage_id)
);
CREATE TABLE IF NOT EXISTS logistics_values (
  namespace TEXT NOT NULL, vintage_id TEXT NOT NULL, period TEXT NOT NULL, period_published TEXT,
  value_text TEXT, value TEXT, status TEXT NOT NULL, flags_json TEXT NOT NULL, footnotes_json TEXT NOT NULL,
  licence_json TEXT, PRIMARY KEY(namespace, vintage_id, period)
);
CREATE TABLE IF NOT EXISTS logistics_breaks (
  namespace TEXT NOT NULL, break_id TEXT NOT NULL, series_id TEXT NOT NULL, release_id TEXT NOT NULL,
  period TEXT NOT NULL, note TEXT NOT NULL, source_text TEXT, sequence BIGINT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, break_id)
);
"""
TABLES = ("logistics_releases", "logistics_series", "logistics_vintages", "logistics_values", "logistics_breaks")
_FREQUENCY = {"annual": "annual", "quarterly": "quarterly", "monthly": "monthly", "semiannual": "irregular"}
_SERIES_COLUMNS = (
    "series_id, provider, source_series_id, dataset, concept, measure_label, unit_json, frequency, geo_kind, "
    "geo_scheme, geo_code, geo_label, geo_unlocode, partner_scheme, partner_code, partner_label, partner_unlocode, "
    "dimensions_json, definition_json, licence_json, freight_index_json, first_release_id"
)


def _check_item(item: Mapping[str, Any]) -> None:
    if forbidden_keys(dict(item)):
        raise LogisticsError("invalid_release", "published records carry no forecast, derived index or merged value")
    if item.get("kind") != "series":
        raise LogisticsError("invalid_release", "a series release carries series items")
    for key in ("provider", "source_series_id", "concept", "measure_label", "frequency", "geography", "definition",
                "licence"):
        if not item.get(key):
            raise LogisticsError("invalid_release", f"a logistics series states its {key}")
    geography = dict(item["geography"])
    if geography.get("kind") not in ("port", "country", "route"):
        raise LogisticsError("invalid_release", "geography kind is port, country or route")
    if geography["kind"] == "route" and not item.get("partner"):
        raise LogisticsError("invalid_release", "a route series states both published ends")
    if geography["kind"] != "route" and item.get("partner"):
        raise LogisticsError("invalid_release", "only a published route series has a partner")
    for obs in item.get("observations") or []:
        if obs.get("status") not in STATUSES:
            raise LogisticsError("invalid_release", "each observation states its status")
        if obs["status"] != "reported" and obs.get("value") is not None:
            raise LogisticsError("invalid_release", "a confidential or unpublished value carries no number")


def _unit_label(unit: Any) -> str | None:
    if isinstance(unit, Mapping):
        return str(unit.get("label") or unit.get("code") or "") or None
    return None if unit is None else str(unit)


class LogisticsStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            for sql in _DDL.split(";"):
                if sql.strip():
                    conn.execute(sql)

    def ready(self) -> bool:
        return table_exists(self.conn, "logistics_vintages")

    def require_ready(self) -> None:
        if not self.ready():
            raise LogisticsError("not_ready", "no logistics record has been acquired yet")

    def _seq(self) -> int:
        return int(self.conn.execute("SELECT nextval('logistics_seq')").fetchone()[0])

    # ------------------------------------------------------------------ writes

    def apply_release(self, namespace: str, header: Mapping[str, Any], items: Sequence[Mapping[str, Any]], *,
                      run_id: str | None = None, source_id: str | None = None) -> dict[str, Any]:
        """Apply one acquired publication in one transaction: replays add nothing, conflicts roll back."""
        from src.kb.logistics_ports import LogisticsPorts

        retrieved = self.now()
        basis = str(header["release_basis"])
        clock = release_ms(header.get("published_on"), header.get("published_at"), retrieved)
        document = dict(header.get("document") or {})
        key = header.get("published_at") or header.get("published_on") or f"retrieved:{retrieved}"
        release_id = "logistics-release:" + digest([namespace, header["provider"], document.get("label"),
                                                    header.get("release_version"), key])[:24]
        existing = self.conn.execute(
            "SELECT content_sha256 FROM logistics_releases WHERE namespace=? AND release_id=?",
            [namespace, release_id]).fetchone()
        if existing is not None:
            if existing[0] == header["content_sha256"]:
                return {"release_id": release_id, "status": "unchanged", "vintages": 0, "ports": {}}
            raise LogisticsError("vintage_conflict", "the publisher republished other content under an unchanged "
                                 "release clock; it is refused rather than written over", release_id=release_id)
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT INTO logistics_releases VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [namespace, release_id, header["provider"], source_id, run_id, str(document.get("label") or ""),
                 str(key), header.get("published_on"), header.get("release_label"), header.get("release_version"),
                 clock, basis, retrieved, header["file_sha256"], header["content_sha256"],
                 str(header.get("evidence_origin") or "live"), str(header.get("live_verification") or ""),
                 header.get("url"), canonical(dict(header)), self._seq()])
            ports: dict[str, int] = {}
            vintages = 0
            if header["kind"] == "ports":
                ports = LogisticsPorts(self.conn, now=self.now).apply_ports(namespace, release_id, header, items)
            else:
                for item in items:
                    vintages += self._series_vintage(namespace, release_id, dict(item), clock, basis, retrieved,
                                                     header)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {"release_id": release_id, "status": "applied", "vintages": vintages, "ports": ports}

    def _series_vintage(self, namespace, release_id, item, clock, basis, retrieved, header) -> int:
        _check_item(item)
        series_id = "logistics-series:" + digest([namespace, item["provider"], item["source_series_id"]])[:24]
        geography, partner = dict(item["geography"]), dict(item.get("partner") or {})
        self.conn.execute(
            "INSERT OR IGNORE INTO logistics_series VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, series_id, item["provider"], item["source_series_id"], item.get("dataset"), item["concept"],
             item["measure_label"], canonical(item.get("unit")), item["frequency"], geography["kind"],
             geography["scheme"], geography["code"], geography.get("label"), geography.get("unlocode"),
             partner.get("scheme"), partner.get("code"), partner.get("label"), partner.get("unlocode"),
             canonical(item.get("dimensions") or {}), canonical(item["definition"]), canonical(item["licence"]),
             canonical(item["freight_index"]) if item.get("freight_index") else None, release_id, self.now()])
        for declared in item.get("breaks") or []:
            break_id = "logistics-break:" + digest([namespace, series_id, declared["period"], declared["note"]])[:24]
            self.conn.execute(
                "INSERT OR IGNORE INTO logistics_breaks VALUES (?,?,?,?,?,?,?,?,?)",
                [namespace, break_id, series_id, release_id, str(declared["period"]), str(declared["note"]),
                 declared.get("source_text"), self._seq(), self.now()])
        observations = [dict(o) for o in item.get("observations") or []]
        content = [{k: o.get(k) for k in ("period", "value_text", "value", "status", "flags", "footnotes")}
                   for o in observations]
        content_hash = digest(content)
        latest = self.vintage_rows(namespace, series_id)
        if latest and latest[-1]["content_hash"] == content_hash:
            return 0  # an unchanged release of this series adds no vintage
        if any(v["release_at_ms"] == clock for v in latest):
            raise LogisticsError("vintage_conflict", "another release of this series has the same release clock "
                                 "with other values; refused rather than written over", series_id=series_id)
        vintage_id = "logistics-vintage:" + digest([namespace, series_id, release_id])[:24]
        economic = self._register(series_id, item, observations, clock, basis, retrieved, vintage_id, release_id,
                                  header)
        self.conn.execute(
            "INSERT INTO logistics_vintages VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, vintage_id, series_id, release_id, clock, basis, retrieved, content_hash,
             economic["vintage"]["vintage_id"], self._seq(), self.now()])
        licence = item["licence"] if item.get("freight_index") else None
        for obs in observations:
            self.conn.execute(
                "INSERT INTO logistics_values VALUES (?,?,?,?,?,?,?,?,?,?)",
                [namespace, vintage_id, obs["period"], obs.get("period_published"), obs.get("value_text"),
                 obs.get("value"), obs["status"], canonical(obs.get("flags") or {}),
                 canonical(obs.get("footnotes") or []), canonical(obs.get("licence") or licence)
                 if (obs.get("licence") or licence) else None])
        return 1

    def _register(self, series_id, item, observations, clock, basis, retrieved, vintage_id, release_id, header):
        """The vintage's numbers into the shared Economics series storage (dataset tables and economic_vintages)."""
        from services.ingest.common.series_model import Observation, SeriesRecord
        from src.domains.economic.model import EconomicModelError, register_series

        geography = dict(item["geography"])
        unit = _unit_label(item.get("unit"))
        record = SeriesRecord(
            series_id=series_id, provider=f"logistics:{item['provider']}",
            title=f"{item['measure_label']} - {geography.get('label') or geography['code']}"
                  + (f" to {item['partner'].get('label') or item['partner']['code']}" if item.get("partner") else ""),
            frequency=_FREQUENCY.get(item["frequency"], "irregular"), as_of=int(clock),
            observations=[Observation(period=o["period"], value=None if o.get("value") is None else float(o["value"]))
                          for o in observations],
            unit=unit, geography=f"{geography['scheme']}:{geography['code']}",
            license=str(dict(item["licence"]).get("id") or ""), source_url=header.get("url"),
            metadata={"pack": "economics", "feature": "logistics", "vintage_id": vintage_id,
                      "vintage_basis": basis, "acquired_at_ms": int(retrieved),
                      **({"provider_release_at_ms": int(clock)} if basis != "retrieval_time" else {}),
                      "provider_release_time_status": f"release clock basis: {basis}",
                      "source_document_id": release_id,
                      "flags_and_text_in": "logistics_values (vintage_id)"})
        semantics = {
            "concept": f"logistics {item['concept']}",
            "canonical_name": item["measure_label"],
            "definition": str(dict(item["definition"]).get("text") or item["measure_label"]),
            "provider_code": item["source_series_id"],
            "provider_definition": str(dict(item["definition"]).get("reference")),
            "unit": unit,
            "geography": f"{geography['scheme']}:{geography['code']}",
            "price_basis": "index" if (unit or "").casefold() == "index" else "not_applicable",
            "seasonal_adjustment": "unknown",
        }
        try:
            return register_series(self.conn, record, semantics=semantics, domain=ECONOMIC_DOMAIN)
        except EconomicModelError as exc:
            raise LogisticsError(exc.code, str(exc), series_id=series_id) from exc

    # ------------------------------------------------------------------ reads

    def release(self, namespace: str, release_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT provider, source_id, document_label, published_on, release_label, release_version, "
            "release_at_ms, release_basis, retrieved_at_ms, file_sha256, evidence_origin, live_verification, url, "
            "header_json FROM logistics_releases WHERE namespace=? AND release_id=?",
            [namespace, release_id]).fetchone()
        if row is None:
            raise LogisticsError("not_found", "release is not visible in this namespace")
        keys = ("provider", "source_id", "document", "published_on", "release_label", "release_version",
                "release_at_ms", "release_basis", "retrieved_at_ms", "file_sha256", "evidence_origin",
                "live_verification", "url")
        out = {"release_id": release_id, **dict(zip(keys, row[:-1]))}
        out["release_at"], out["retrieved_at"] = iso_from_ms(out["release_at_ms"]), iso_from_ms(out["retrieved_at_ms"])
        return out

    def source_revision(self, namespace: str, release_id: str) -> dict[str, Any]:
        """The citation of one release: publisher, document, release clock and basis, digest and evidence origin."""
        from src.ingestion.logistics_sources import PROVIDER_CONTRACTS

        release = self.release(namespace, release_id)
        return {
            "release_id": release_id,
            "provider": release["provider"],
            "source_id": release["source_id"],
            "document": release["document"],
            "url": release["url"],
            "published_on": release["published_on"],
            "release_label": release["release_label"],
            "release_version": release["release_version"],
            "release_at": release["release_at"],
            "release_basis": release["release_basis"],
            "retrieved_at": release["retrieved_at"],
            "file_sha256": release["file_sha256"],
            "evidence_origin": release["evidence_origin"],
            "live_verification": release["live_verification"],
            "attribution": PROVIDER_CONTRACTS.get(release["provider"], {}).get("licence", {}).get("attribution"),
        }

    def releases(self, namespace: str, *, provider: str | None = None) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "logistics_releases"):
            return []
        rows = self.conn.execute(
            "SELECT release_id FROM logistics_releases WHERE namespace=? AND (? IS NULL OR provider=?) "
            "ORDER BY release_at_ms, sequence", [namespace, provider, provider]).fetchall()
        return [self.release(namespace, r[0]) for r in rows]

    def _view(self, namespace: str, row: Sequence[Any]) -> dict[str, Any]:
        (series_id, provider, source_series_id, dataset, concept, measure_label, unit, frequency, geo_kind,
         geo_scheme, geo_code, geo_label, geo_unlocode, partner_scheme, partner_code, partner_label,
         partner_unlocode, dimensions, definition, licence, freight_index, first_release) = row
        geography = {"kind": geo_kind, "scheme": geo_scheme, "code": geo_code}
        if geo_label:
            geography["label"] = geo_label
        if geo_unlocode:
            geography["unlocode"] = geo_unlocode
        partner = None
        if partner_code:
            partner = {"scheme": partner_scheme, "code": partner_code}
            if partner_label:
                partner["label"] = partner_label
            if partner_unlocode:
                partner["unlocode"] = partner_unlocode
        vintages = self.vintage_rows(namespace, series_id)
        return {
            "contract": CONTRACT, "record_type": "series", "namespace": namespace, "series_id": series_id,
            "provider": provider, "source_series_id": source_series_id, "dataset": dataset, "concept": concept,
            "measure_label": measure_label, "unit": load(unit), "frequency": frequency, "geography": geography,
            "partner": partner, "dimensions": load(dimensions, {}), "definition": load(definition, {}),
            "licence": load(licence, {}), "freight_index": load(freight_index),
            "first_release_id": first_release, "vintage_count": len(vintages),
            "current_vintage_id": vintages[-1]["vintage_id"] if vintages else None,
            "economic_series": {"domain": ECONOMIC_DOMAIN, "series_id": series_id,
                                "store": "dataset_series/dataset_observations + economic_vintages"},
        }

    def series(self, namespace: str, series_id: str) -> dict[str, Any]:
        row = self.conn.execute(f"SELECT {_SERIES_COLUMNS} FROM logistics_series WHERE namespace=? AND series_id=?",
                                [namespace, series_id]).fetchone()
        if row is None:
            raise LogisticsError("not_found", "series is not visible in this namespace")
        return self._view(namespace, row)

    def find_series(self, namespace: str, *, provider: str | None = None, geo_kind: str | None = None,
                    codes: Sequence[tuple[str | None, str]] | None = None,
                    partner_codes: Sequence[tuple[str | None, str]] | None = None,
                    concept: str | None = None) -> list[dict[str, Any]]:
        """Series by provider, geography kind, concept and published codes ((scheme or None, code) pairs); a route
        matches only on its own published ends."""
        if not self.ready():
            return []
        rows = self.conn.execute(
            f"SELECT {_SERIES_COLUMNS} FROM logistics_series WHERE namespace=? AND (? IS NULL OR provider=?) AND "
            "(? IS NULL OR geo_kind=?) AND (? IS NULL OR concept=?) ORDER BY provider, dataset, geo_code, "
            "partner_code, series_id",
            [namespace, provider, provider, geo_kind, geo_kind, concept, concept]).fetchall()

        def hit(wanted, scheme, code, unlocode):
            for want_scheme, want_code in wanted:
                if want_scheme == "unlocode" and unlocode and unlocode == want_code:
                    return True
                if (want_scheme is None or want_scheme == scheme) and want_code == code:
                    return True
            return False

        out = []
        for row in rows:
            if codes is not None and not hit(codes, row[9], row[10], row[12]):
                continue
            if partner_codes is not None and not hit(partner_codes, row[13], row[14], row[16]):
                continue
            out.append(self._view(namespace, row))
        return out

    def vintage_rows(self, namespace: str, series_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT vintage_id, release_id, release_at_ms, release_basis, retrieved_at_ms, content_hash, "
            "economic_vintage_id, sequence FROM logistics_vintages WHERE namespace=? AND series_id=? "
            "ORDER BY release_at_ms, sequence", [namespace, series_id]).fetchall()
        out, previous = [], None
        for r in rows:
            out.append({"vintage_id": r[0], "release_id": r[1], "release_at_ms": int(r[2]),
                        "release_at": iso_from_ms(r[2]), "release_basis": r[3], "retrieved_at_ms": int(r[4]),
                        "retrieved_at": iso_from_ms(r[4]), "content_hash": r[5], "economic_vintage_id": r[6],
                        "sequence": int(r[7]), "revision_of": previous})
            previous = r[0]
        return out

    def select_vintage(self, namespace: str, series_id: str, as_of_ms: int | None) -> dict[str, Any] | None:
        rows = self.vintage_rows(namespace, series_id)
        if as_of_ms is not None:
            rows = [v for v in rows if v["release_at_ms"] <= int(as_of_ms)]
        return rows[-1] if rows else None

    def values(self, namespace: str, vintage_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT period, period_published, value_text, value, status, flags_json, footnotes_json, licence_json "
            "FROM logistics_values WHERE namespace=? AND vintage_id=? ORDER BY period",
            [namespace, vintage_id]).fetchall()
        out = []
        for r in rows:
            item = {"period": r[0], "period_published": r[1], "value_text": r[2], "value": r[3], "status": r[4],
                    "flags": load(r[5], {}), "footnotes": load(r[6], [])}
            if r[7]:
                item["licence"] = load(r[7])
            out.append(item)
        return out

    def breaks(self, namespace: str, series_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT break_id, release_id, period, note, source_text FROM logistics_breaks WHERE namespace=? AND "
            "series_id=? ORDER BY period, sequence", [namespace, series_id]).fetchall()
        return [{"break_id": r[0], "release_id": r[1], "period": r[2], "note": r[3], "source_text": r[4],
                 "handling": "marked; no chaining, rebasing or splicing"} for r in rows]

    def latest_release_ms(self, namespace: str) -> int | None:
        if not table_exists(self.conn, "logistics_releases"):
            return None
        row = self.conn.execute("SELECT max(release_at_ms) FROM logistics_releases WHERE namespace=?",
                                [namespace]).fetchone()
        return None if row is None or row[0] is None else int(row[0])


def read_store(conn: Any, namespace: str, scopes) -> LogisticsStore:
    authorize(namespace, scopes, READ_SCOPE)
    store = LogisticsStore(conn, initialize=False)
    store.require_ready()
    return store


class LogisticsProjector:
    """Source-pack runtime projector for ``noesis-logistics-record-v1`` pages (one release per page)."""

    def __init__(self, conn: Any) -> None:
        self.store = LogisticsStore(conn)

    @staticmethod
    def _namespace(source: Mapping[str, Any]) -> str:
        return str(dict(source.get("logistics") or {}).get("namespace") or DEFAULT_NAMESPACE)

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        del manifest, documents, page_receipt, principal_id
        groups: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
        for item in records:
            header, body = dict(item.get("logistics_release") or {}), item.get("logistics_item")
            if not header or not isinstance(body, Mapping):
                raise LogisticsError("invalid_record", "page record is not a logistics release item")
            groups.setdefault(header["file_sha256"] + canonical(header.get("document")), (header, []))[1].append(
                dict(body))
        namespace = self._namespace(source)
        return [self.store.apply_release(namespace, header, items, run_id=run_id, source_id=source["source_id"])
                for header, items in groups.values()]

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del run_id, manifest, principal_id
        row = self.store.conn.execute(
            "SELECT release_id, published_on FROM logistics_releases WHERE namespace=? AND source_id=? "
            "ORDER BY release_at_ms DESC, sequence DESC LIMIT 1", [self._namespace(source), source["source_id"]],
        ).fetchone()
        return {"status": status, "latest_release_id": row[0] if row else None,
                "latest_published_on": row[1] if row else None}


def operator_import(conn: Any, namespace: str, source: Mapping[str, Any], document_index: int, raw: bytes, *,
                    principal_id: str, scopes, now: Callable[[], int] | None = None) -> dict[str, Any]:
    """Record a file an operator obtained for a declared document (an extracted UNCTADstat 7z bulk CSV): parsed with
    the same parser and applied with evidence origin ``operator``."""
    from src.ingestion.logistics_sources import (
        LogisticsFormatError,
        document_url,
        logistics_declaration,
        parse_document,
        release_records,
    )

    authorize(namespace, scopes, WRITE_SCOPE, write=True)
    declared = logistics_declaration(source)
    documents = list(declared["documents"])
    if not 0 <= int(document_index) < len(documents):
        raise LogisticsError("invalid_request", "document_index names no declared document")
    document = dict(documents[int(document_index)])
    url = document_url(declared["format"], document, source["endpoint"])
    try:
        release = parse_document(declared["format"], bytes(raw), document, url=url,
                                 limit=int(source["budgets"]["max_results"]))
    except LogisticsFormatError as exc:
        raise LogisticsError(exc.code, str(exc)) from exc
    records = release_records(release, document, url, "operator")
    store = LogisticsStore(conn, now=now)
    header = dict(records[0]["logistics_release"])
    result = store.apply_release(namespace, header, [r["logistics_item"] for r in records],
                                 run_id=f"operator-import:{principal_id}", source_id=source["source_id"])
    return {**result, "evidence_origin": "operator", "recorded_by": principal_id}


def register_schemas(conn: Any, *, principal_id: str, scopes: Any) -> list[dict[str, Any]]:
    """Register the logistics record contract as a schema module in the shared registry."""
    import json
    from pathlib import Path

    from src.kb.schema_registry import SchemaRegistry

    path = Path(__file__).resolve().parents[2] / "contracts/schemas/jsonschema" / f"{CONTRACT}.json"
    definition = {
        "contract": "noesis-schema-module-v1", "name": "logistics-record", "kind": "schema",
        "semantic_version": "1.0.0", "content": json.loads(path.read_text()), "owner": "economics.logistics",
        "dependencies": [], "compatibility_policy": "backward",
        "provenance": {"kind": "imported", "source": f"contracts/schemas/jsonschema/{CONTRACT}.json"},
        "actor": {"principal_id": principal_id, "kind": "service"},
    }
    return [SchemaRegistry(conn).register(definition, "logistics-schema:logistics-record:1.0.0",
                                          principal_id=principal_id, scopes=scopes)]


__all__ = ["TABLES", "LogisticsProjector", "LogisticsStore", "operator_import", "read_store", "register_schemas"]
