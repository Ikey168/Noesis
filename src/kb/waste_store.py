"""Append-only waste release bookkeeping over the existing series and environment stores (#2740, WC02).

No new value store is introduced. Values go where the platform already keeps them:

* **Statistical series** (Eurostat waste, Eurostat circular economy, OECD municipal waste) are written to the Economics
  series storage (``economic_indicators``, ``economic_series_map``, ``economic_vintages``, ``dataset_observations``)
  through :func:`src.domains.economic.model.register_series`;
* **Facility transfer rows** (EEA Industrial Reporting) are ``observation_series`` records of
  :class:`src.kb.environment_store.EnvironmentStore` (``environment_records``, ``environment_vintages``,
  ``environment_values``) located at the ``environment.core`` facility record ``eea-industry:<INSPIRE id>``; their
  vintages are compared with :mod:`src.kb.environment_vintages`. No facility record is ever written here.

The ``waste_*`` tables are the provider's index and bookkeeping (all namespace-scoped):

* ``waste_releases`` - one row per acquired publication: provider, declared document and its key, release label and
  clock (``provider_last_update``, ``provider_dataset_version``, ``declared_release`` or ``retrieval_time``),
  retrieval clock, dataflow or dataset version, digests, completeness (an EEA page that fills ``nrOfHits`` is
  ``truncated``), evidence origin and run id;
* ``waste_series`` - one row per statistical series key (:func:`src.ingestion.waste_sources.series_key`);
* ``waste_definitions`` - definition revisions; a changed definition is a new revision;
* ``waste_vintages`` / ``waste_observations`` - one row per release of a series and its values exactly as published
  with status and flags verbatim (the numeric copy lives in ``dataset_observations``);
* ``waste_transfer_rows`` - the transfer row keys and the environment record each one lives in;
* ``waste_release_members`` - which series or rows each release stated, so a complete later release that no longer
  states one records a ``removed_by_source`` vintage instead of a deletion (a truncated EEA page never removes);
* ``waste_notes`` - source-stated breaks and provisional periods;
* ``waste_receipts`` - one receipt per applied release or failure (provider state is read from them).

A release cannot be dated after its retrieval. Nothing is overwritten, filled, blended, summed or derived.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from decimal import Decimal
from typing import Any

from src.ingestion.waste_sources import (
    FORMATS,
    LIVE_VERIFICATION,
    TRANSFER_PROVIDER,
    series_key,
    transfer_key,
    transfer_native_id,
)
from src.kb.waste_records import (
    ANSWER_CONTRACT,
    CONTRACT,
    ECONOMIC_DOMAIN,
    ENVIRONMENT_READ,
    ENVIRONMENT_WRITE,
    READ_SCOPE,
    WRITE_SCOPE,
    WasteError,
    authorize,
    canonical,
    check_item,
    digest,
    iso,
    load,
    table_exists,
    to_ms,
)

_DDL = """
CREATE TABLE IF NOT EXISTS waste_releases (
  namespace TEXT NOT NULL, release_id TEXT NOT NULL, provider TEXT NOT NULL, source_id TEXT, format TEXT NOT NULL,
  document_key TEXT NOT NULL, document_json TEXT NOT NULL, release_label TEXT, published_on TEXT, published_at TEXT,
  release_basis TEXT NOT NULL, release_at_ms BIGINT NOT NULL, retrieved_at_ms BIGINT NOT NULL,
  dataflow_version TEXT, file_sha256 TEXT NOT NULL, content_sha256 TEXT NOT NULL, item_count INTEGER NOT NULL,
  complete BOOLEAN NOT NULL, truncated BOOLEAN NOT NULL, dataset_version_json TEXT, evidence_origin TEXT NOT NULL,
  url TEXT, run_id TEXT NOT NULL, recorded_by TEXT, sequence INTEGER NOT NULL, PRIMARY KEY(namespace, release_id)
);
CREATE TABLE IF NOT EXISTS waste_series (
  namespace TEXT NOT NULL, series_id TEXT NOT NULL, provider TEXT NOT NULL, dataset TEXT NOT NULL,
  native_key TEXT NOT NULL, key_json TEXT NOT NULL, indicator_json TEXT NOT NULL, concept TEXT NOT NULL,
  parts_json TEXT NOT NULL, area_scheme TEXT NOT NULL, area_code TEXT NOT NULL, area_json TEXT NOT NULL,
  periodicity TEXT NOT NULL, frequency TEXT NOT NULL, document_key TEXT NOT NULL, economic_indicator_id TEXT NOT NULL,
  first_release_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, series_id)
);
CREATE TABLE IF NOT EXISTS waste_definitions (
  namespace TEXT NOT NULL, definition_id TEXT NOT NULL, definition_key TEXT NOT NULL, revision INTEGER NOT NULL,
  provider TEXT NOT NULL, content_json TEXT NOT NULL, content_hash TEXT NOT NULL, release_id TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, definition_id)
);
CREATE TABLE IF NOT EXISTS waste_vintages (
  namespace TEXT NOT NULL, vintage_id TEXT NOT NULL, series_id TEXT NOT NULL, release_id TEXT NOT NULL,
  sequence INTEGER NOT NULL, status TEXT NOT NULL, release_at_ms BIGINT NOT NULL, release_basis TEXT NOT NULL,
  retrieved_at_ms BIGINT NOT NULL, economic_as_of BIGINT, content_hash TEXT NOT NULL, definition_id TEXT,
  dataflow_version TEXT, release_label TEXT, previous_vintage_id TEXT, changes_json TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, vintage_id)
);
CREATE TABLE IF NOT EXISTS waste_observations (
  namespace TEXT NOT NULL, vintage_id TEXT NOT NULL, period TEXT NOT NULL, value_text TEXT, value TEXT,
  status TEXT NOT NULL, flags_json TEXT NOT NULL, flag_meanings_json TEXT NOT NULL, attributes_json TEXT NOT NULL,
  PRIMARY KEY(namespace, vintage_id, period)
);
CREATE TABLE IF NOT EXISTS waste_transfer_rows (
  namespace TEXT NOT NULL, row_id TEXT NOT NULL, environment_record_id TEXT NOT NULL, native_id TEXT NOT NULL,
  inspire_id TEXT NOT NULL, reporting_year INTEGER NOT NULL, hazardous TEXT NOT NULL, treatment TEXT NOT NULL,
  destination TEXT NOT NULL, document_key TEXT NOT NULL, first_release_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, row_id)
);
CREATE TABLE IF NOT EXISTS waste_release_members (
  namespace TEXT NOT NULL, release_id TEXT NOT NULL, subject_kind TEXT NOT NULL, subject_id TEXT NOT NULL,
  vintage_id TEXT, status TEXT NOT NULL, PRIMARY KEY(namespace, release_id, subject_id)
);
CREATE TABLE IF NOT EXISTS waste_notes (
  namespace TEXT NOT NULL, note_id TEXT NOT NULL, series_id TEXT NOT NULL, relation TEXT NOT NULL,
  statement TEXT NOT NULL, periods_json TEXT NOT NULL, cited_json TEXT NOT NULL, origin TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, note_id)
);
CREATE TABLE IF NOT EXISTS waste_receipts (
  namespace TEXT NOT NULL, receipt_id TEXT NOT NULL, run_id TEXT NOT NULL, source_id TEXT, provider TEXT NOT NULL,
  outcome TEXT NOT NULL, execution TEXT NOT NULL, detail_json TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, receipt_id)
);
"""
TABLES = ("waste_releases", "waste_series", "waste_definitions", "waste_vintages", "waste_observations",
          "waste_transfer_rows", "waste_release_members", "waste_notes", "waste_receipts")
FACILITY_PROVIDER = "eea-industry"
TRANSFER_TITLE = "Off-site waste transfer"
_SERIES_COLUMNS = ("series_id, provider, dataset, native_key, key_json, indicator_json, parts_json, area_json, "
                   "periodicity, frequency, document_key, economic_indicator_id, first_release_id")
_VINTAGE_COLUMNS = ("vintage_id, series_id, release_id, sequence, status, release_at_ms, release_basis, "
                    "retrieved_at_ms, economic_as_of, content_hash, definition_id, dataflow_version, release_label, "
                    "previous_vintage_id, changes_json")
_VINTAGE_KEYS = ("vintage_id", "series_id", "release_id", "sequence", "status", "release_at_ms", "release_basis",
                 "retrieved_at_ms", "economic_as_of", "content_hash", "definition_id", "dataflow_version",
                 "release_label", "previous_vintage_id", "changes")
_RELEASE_KEYS = ("release_id", "provider", "source_id", "format", "document_key", "document", "release_label",
                 "published_on", "published_at", "release_basis", "release_at_ms", "retrieved_at_ms",
                 "dataflow_version", "file_sha256", "content_sha256", "item_count", "complete", "truncated",
                 "dataset_version", "evidence_origin", "url", "run_id", "sequence")
PARTS = ("waste_category", "hazard", "activity", "operation", "unit")


def _content(observations: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    keys = ("period", "value_text", "value", "status", "flags")
    return [{k: o.get(k) for k in keys} for o in sorted(observations, key=lambda o: o["period"])]


def environment_scopes(namespace: str) -> set[str]:
    """What the waste store presents to the environment.core store once the caller's waste scope is checked."""
    return {ENVIRONMENT_WRITE, ENVIRONMENT_READ, f"namespace:{namespace}:write", f"namespace:{namespace}:read"}


def citation(series: Mapping[str, Any], vintage: Mapping[str, Any], release: Mapping[str, Any]) -> dict[str, Any]:
    """What every answer cites for one value set: source, record revision (vintage) and as-of time."""
    return {
        "provider": series["provider"], "source_id": release.get("source_id"), "series_id": series["series_id"],
        "dataset": series["dataset"], "native_key": series["native_key"], "vintage_id": vintage["vintage_id"],
        "release_id": release["release_id"], "release_label": release.get("release_label"),
        "release_basis": release["release_basis"], "as_of": vintage["release_at"],
        "retrieved_at": vintage["retrieved_at"], "url": release.get("url"), "file_sha256": release["file_sha256"],
        "evidence_origin": release["evidence_origin"], "contract": CONTRACT,
        "live_verification": LIVE_VERIFICATION[series["provider"]]["status"],
    }


class WasteStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self._environment = None
        self._initialize = initialize
        if initialize:
            conn.execute(_DDL)

    def ready(self) -> bool:
        return table_exists(self.conn, "waste_vintages")

    def environment(self):
        """The environment.core store the transfer rows live in."""
        if self._environment is None:
            from src.kb.environment_store import EnvironmentStore

            self._environment = EnvironmentStore(self.conn, initialize=self._initialize, now=self.now)
        return self._environment

    # ------------------------------------------------------------------ writes

    def apply_release(self, namespace: str, header: Mapping[str, Any], items: Sequence[Mapping[str, Any]], *,
                      source_id: str | None, run_id: str, principal_id: str, scopes: Iterable[str],
                      retrieved_at_ms: int | None = None) -> dict[str, Any]:
        """Append one release: series or transfer-row vintages and removals; never an overwrite."""
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        provider, fmt = str(header.get("provider") or ""), header.get("format")
        if FORMATS.get(str(fmt), {}).get("provider") != provider:
            raise WasteError("invalid_release", "a release names a known provider and its format")
        if int(header.get("item_count", -1)) != len(items):
            raise WasteError("incomplete_release", "a release carries every item it states")
        for item in items:
            # The whole release is refused when one item breaks the record rules or the minimisation decision.
            check_item(item)
            if item["provider"] != provider:
                raise WasteError("invalid_release", "every item of a release belongs to its provider")
        retrieved = int(retrieved_at_ms if retrieved_at_ms is not None else self.now())
        clock, basis = self._clock(header, retrieved)
        if clock > retrieved:
            raise WasteError("invalid_release", "a release cannot be dated after its retrieval")
        release_id = "waste-release:" + digest([namespace, header["document_key"], header.get("release_label"),
                                                header["file_sha256"]])[:24]
        if self.conn.execute("SELECT 1 FROM waste_releases WHERE namespace=? AND release_id=?",
                             [namespace, release_id]).fetchone():
            self._receipt(namespace, run_id, source_id, provider, "unchanged", header, retrieved,
                          {"release_id": release_id})
            return {"release_id": release_id, "status": "unchanged", "vintages": 0, "unchanged": 0, "removed": 0}
        transfers = provider == TRANSFER_PROVIDER
        if transfers:
            self.environment()  # created outside the transaction
        complete = bool(header.get("complete", True)) and not header.get("truncated")
        self.conn.execute("BEGIN")
        try:
            sequence = 1 + int(self.conn.execute(
                "SELECT count(*) FROM waste_releases WHERE namespace=? AND document_key=?",
                [namespace, header["document_key"]]).fetchone()[0])
            previous = self.conn.execute(
                "SELECT release_id FROM waste_releases WHERE namespace=? AND document_key=? AND complete "
                "ORDER BY sequence DESC LIMIT 1", [namespace, header["document_key"]]).fetchone()
            self.conn.execute(
                "INSERT INTO waste_releases VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [namespace, release_id, provider, source_id, fmt, header["document_key"],
                 canonical(header.get("document") or {}), header.get("release_label"), header.get("published_on"),
                 header.get("published_at"), basis, clock, retrieved, header.get("dataflow_version"),
                 header["file_sha256"], header.get("content_sha256") or "", len(items), complete,
                 bool(header.get("truncated")), canonical(header.get("dataset_version"))
                 if header.get("dataset_version") else None, self._origin(header), header.get("url"), run_id,
                 principal_id, sequence])
            counts = {"new": 0, "unchanged": 0, "removed": 0}
            stated: set[str] = set()
            for item in items:
                if transfers:
                    subject_id, vintage_id, status = self._transfer(namespace, item, header, release_id, clock,
                                                                    basis, retrieved, run_id)
                    kind = "transfer_row"
                else:
                    subject_id = self._series(namespace, item, header, release_id)
                    definition_id = self._definition(namespace, item, release_id)
                    vintage_id, status = self._vintage(namespace, subject_id, item, header, release_id, clock, basis,
                                                       retrieved, definition_id)
                    kind = "series"
                    if status == "new":
                        self._source_notes(namespace, subject_id, item, vintage_id)
                if subject_id in stated:
                    raise WasteError("invalid_release", "a release states the same series or row twice")
                stated.add(subject_id)
                counts[status] += 1
                self.conn.execute("INSERT INTO waste_release_members VALUES (?,?,?,?,?,?)",
                                  [namespace, release_id, kind, subject_id, vintage_id, status])
            if previous and complete:
                for subject_kind, subject_id in self.conn.execute(
                        "SELECT subject_kind, subject_id FROM waste_release_members WHERE namespace=? AND "
                        "release_id=? AND status <> 'removed' ORDER BY subject_id", [namespace, previous[0]]
                ).fetchall():
                    if subject_id in stated:
                        continue
                    if subject_kind == "transfer_row":
                        vintage_id = self._transfer_removal(namespace, subject_id, header, release_id, clock,
                                                            retrieved, run_id)
                    else:
                        vintage_id = self._removal(namespace, subject_id, header, release_id, clock, basis,
                                                   retrieved)
                    if vintage_id:
                        counts["removed"] += 1
                        self.conn.execute("INSERT INTO waste_release_members VALUES (?,?,?,?,?,?)",
                                          [namespace, release_id, subject_kind, subject_id, vintage_id, "removed"])
            self._receipt(namespace, run_id, source_id, provider, "applied", header, retrieved,
                          {"release_id": release_id, **counts, "truncated": bool(header.get("truncated"))})
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {"release_id": release_id, "status": "applied", "published_on": header.get("published_on"),
                "release_basis": basis, "vintages": counts["new"], "unchanged": counts["unchanged"],
                "removed": counts["removed"], "complete": complete, "truncated": bool(header.get("truncated"))}

    @staticmethod
    def _origin(header: Mapping[str, Any]) -> str:
        origin = header.get("evidence_origin")
        return origin if origin in {"fixture", "operator"} else "live"

    @staticmethod
    def _clock(header: Mapping[str, Any], retrieved: int) -> tuple[int, str]:
        if header.get("published_at"):
            return to_ms(header["published_at"]), header.get("release_basis") or "provider_last_update"
        if header.get("published_on"):
            return to_ms(header["published_on"]), header.get("release_basis") or "declared_release"
        return retrieved, "retrieval_time"

    # ------------------------------------------------------------------ statistical series

    def _series(self, namespace, item, header, release_id) -> str:
        key = series_key(item)
        series_id = f"waste:{item['provider']}:" + digest(key)[:20]
        if not self.conn.execute("SELECT 1 FROM waste_series WHERE namespace=? AND series_id=?",
                                 [namespace, series_id]).fetchone():
            parts = {p: dict(item[p]) for p in PARTS}
            self.conn.execute(
                "INSERT INTO waste_series VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [namespace, series_id, item["provider"], item["dataset"], item["native_key"], canonical(key),
                 canonical(item["indicator"]), item["indicator"]["concept"], canonical(parts),
                 item["area"]["scheme"], str(item["area"]["code"]), canonical(item["area"]), item["periodicity"],
                 item.get("frequency") or "annual", header["document_key"],
                 "waste-indicator:" + series_id.split(":", 2)[2], release_id, self.now()])
        return series_id

    @staticmethod
    def definition_key(item: Mapping[str, Any]) -> str:
        return f"{item['provider']}:" + digest([item["dataset"], item["indicator"].get("code"),
                                                item["indicator"]["concept"],
                                                dict(item["unit"]).get("code"), item["periodicity"]])[:16]

    def _definition(self, namespace, item, release_id) -> str:
        key = self.definition_key(item)
        content = dict(item["definition"])
        content_hash = digest(content)
        latest = self.conn.execute(
            "SELECT definition_id, content_hash, revision FROM waste_definitions WHERE namespace=? AND "
            "definition_key=? ORDER BY revision DESC LIMIT 1", [namespace, key]).fetchone()
        if latest and latest[1] == content_hash:
            return latest[0]
        revision = 1 + (int(latest[2]) if latest else 0)
        definition_id = f"waste-def:{key}:r{revision}"
        self.conn.execute("INSERT INTO waste_definitions VALUES (?,?,?,?,?,?,?,?,?)",
                          [namespace, definition_id, key, revision, item["provider"], canonical(content), content_hash,
                           release_id, self.now()])
        return definition_id

    def _changes(self, namespace, prior, observations, definition_id, header) -> dict[str, Any]:
        if prior is None:
            return {"new_series": True, "new_periods": sorted(o["period"] for o in observations)}
        before = {o["period"]: o for o in self.observations(namespace, prior["vintage_id"])}
        after = {o["period"]: o for o in _content(observations)}
        compared = ("value_text", "value", "status", "flags")
        revised = [{"period": p, "before": {k: before[p].get(k) for k in compared},
                    "after": {k: after[p].get(k) for k in compared}}
                   for p in sorted(set(before) & set(after))
                   if any(before[p].get(k) != after[p].get(k) for k in compared)]
        changes: dict[str, Any] = {
            "new_periods": sorted(set(after) - set(before)),
            "dropped_periods": sorted(set(before) - set(after)),
            "revised": revised,
        }
        if prior["status"] == "removed":
            changes["restated_after_removal"] = True
        if prior.get("definition_id") != definition_id or prior.get("dataflow_version") != header.get(
                "dataflow_version"):
            changes["definition_change"] = {"definition_before": prior.get("definition_id"),
                                            "definition_after": definition_id,
                                            "dataflow_version_before": prior.get("dataflow_version"),
                                            "dataflow_version_after": header.get("dataflow_version")}
        return {k: v for k, v in changes.items() if v}

    def _vintage(self, namespace, series_id, item, header, release_id, clock, basis, retrieved, definition_id):
        from services.ingest.common.series_model import SeriesRecord
        from src.domains.economic.model import EconomicModelError, register_series

        observations = list(item.get("observations") or [])
        content_hash = digest(_content(observations))
        previous = self.vintage_rows(namespace, series_id)
        prior = previous[-1] if previous else None
        if prior is not None and prior["status"] == "published" and prior["content_hash"] == content_hash and \
                prior["definition_id"] == definition_id and prior["dataflow_version"] == header.get(
                    "dataflow_version"):
            return prior["vintage_id"], "unchanged"
        if any(v["release_at_ms"] == clock for v in previous):
            raise WasteError("vintage_conflict", "the publication changed values without a new release time; the "
                                                 "stored vintage is kept", series_id=series_id)
        if prior is not None and clock < prior["release_at_ms"]:
            raise WasteError("stale_release", "a release dated before the series' latest vintage is not appended",
                             series_id=series_id)
        changes = self._changes(namespace, prior, observations, definition_id, header)
        record = SeriesRecord(
            series_id=series_id, provider=item["provider"],
            title=str(item["indicator"].get("label") or item["native_key"]),
            frequency=item.get("frequency") or "annual", as_of=int(clock),
            observations=[{"period": o["period"], "value": None if o.get("value") is None
                           else float(Decimal(o["value"]))} for o in sorted(observations, key=lambda o: o["period"])],
            unit=dict(item["unit"]).get("label") or dict(item["unit"]).get("code") or None,
            geography=str(item["area"]["code"]), source_url=header.get("url"),
            metadata={"provider_release_at_ms": int(clock),
                      "provider_release_time_status": f"waste release clock ({basis})",
                      "acquired_at_ms": int(retrieved), "vintage_basis": basis, "source_document_id": release_id},
        )
        semantics = {
            "indicator_id": "waste-indicator:" + series_id.split(":", 2)[2],
            "canonical_name": str(item["indicator"].get("label") or item["indicator"]["concept"]),
            "concept": f"waste {item['indicator']['concept']} {series_id}",
            "definition": dict(item["definition"]).get("source_text") or item["indicator"]["concept"],
            "seasonal_adjustment": "not_applicable",
            "price_basis": "not_applicable",
            "provider_code": str(item["native_key"]),
            "provider_definition": dict(item["definition"]).get("source_text") or item["indicator"]["concept"],
            "attributes": {"waste_series_id": series_id, "periodicity": item["periodicity"],
                           **{p: dict(item[p]).get("code") for p in PARTS}, "contract": CONTRACT},
        }
        try:
            economic = register_series(self.conn, record, semantics=semantics, domain=ECONOMIC_DOMAIN)
        except EconomicModelError as exc:
            raise WasteError(exc.code, str(exc)) from exc
        vintage_id = "waste-vintage:" + digest([namespace, series_id, release_id, content_hash])[:24]
        self.conn.execute(
            "INSERT INTO waste_vintages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, vintage_id, series_id, release_id, 1 + len(previous), "published", clock, basis, retrieved,
             economic["vintage"]["as_of"], content_hash, definition_id, header.get("dataflow_version"),
             header.get("release_label"), prior["vintage_id"] if prior else None, canonical(changes), self.now()])
        for obs in observations:
            self.conn.execute(
                "INSERT INTO waste_observations VALUES (?,?,?,?,?,?,?,?,?)",
                [namespace, vintage_id, obs["period"], obs.get("value_text"), obs.get("value"), obs["status"],
                 canonical(dict(obs.get("flags") or {})), canonical(list(obs.get("flag_meanings") or [])),
                 canonical(dict(obs.get("attributes") or {}))])
        return vintage_id, "new"

    def _removal(self, namespace, series_id, header, release_id, clock, basis, retrieved) -> str | None:
        previous = self.vintage_rows(namespace, series_id)
        prior = previous[-1] if previous else None
        if prior is None or prior["status"] == "removed" or clock <= prior["release_at_ms"]:
            return None
        vintage_id = "waste-vintage:" + digest([namespace, series_id, release_id, "removed"])[:24]
        changes = {"removed_by_source": {"release_label": header.get("release_label"),
                                         "statement": "the source's complete release of this document no longer "
                                                      "states the series; earlier vintages stay queryable"}}
        self.conn.execute(
            "INSERT INTO waste_vintages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, vintage_id, series_id, release_id, 1 + len(previous), "removed", clock, basis, retrieved, None,
             digest("removed"), prior["definition_id"], header.get("dataflow_version"), header.get("release_label"),
             prior["vintage_id"], canonical(changes), self.now()])
        return vintage_id

    def _source_notes(self, namespace, series_id, item, vintage_id) -> None:
        for note in item.get("source_notes") or []:
            relation = {"break": "break_in_series", "provisional": "provisional"}.get(note.get("kind"), "source_note")
            statement = str(note.get("statement") or note.get("value"))
            note_id = "waste-note:" + digest([namespace, series_id, relation, statement,
                                              sorted(note.get("periods") or [])])[:24]
            self.conn.execute(
                "INSERT INTO waste_notes VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                [namespace, note_id, series_id, relation, statement, canonical(sorted(note.get("periods") or [])),
                 canonical([{"vintage_id": vintage_id, "attribute": note.get("attribute"),
                             "value": note.get("value")}]), "source-stated", self.now()])

    # ------------------------------------------------------------------ facility transfer rows

    def _transfer_record(self, item, values, header, clock, basis):
        from src.kb import environment_records as er

        key = transfer_key(item)
        released = header.get("published_on") if basis == "provider_dataset_version" else None
        return er.series(
            FACILITY_PROVIDER, transfer_native_id(item),
            f"{TRANSFER_TITLE} {key['reporting_year']}: {item['hazardous']['label']} waste, "
            f"{item['treatment']['label']}, {item['destination']['label']}",
            source_url="https://industry.eea.europa.eu/",
            location={"kind": "facility", "ref": item["facility_ref"]},
            indicator={"code": "offsite-waste-transfer", "name": "Off-site transfer of waste",
                       "scheme": "E-PRTR / IED Industrial Reporting", "hazardous": key["hazardous"],
                       "treatment": key["treatment"], "destination": key["destination"],
                       "reporting_year": key["reporting_year"], "waste_provider": TRANSFER_PROVIDER},
            unit="t", interval="P1Y", aggregation="total", kind="observation", values=values,
            status_basis="quantity in tonnes as published with the method code (M/C/E); facilities report only above "
                         "the E-PRTR thresholds, so an absent row is not zero",
            release={"released_at": released, "basis": "provider_dataset_version"} if released
            else {"basis": "retrieval_time"},
            unknowns=[] if dict(item["method"]).get("code") else ["method"])

    def _transfer(self, namespace, item, header, release_id, clock, basis, retrieved, run_id):
        key = transfer_key(item)
        native = transfer_native_id(item)
        row_id = "waste-row:" + digest([namespace, native])[:24]
        year = key["reporting_year"]
        values = [{"start": f"{year}-01-01", "end": f"{year + 1}-01-01", "value": item["quantity"],
                   "status": "unknown", "flags": {"method": dict(item["method"]).get("code"),
                                                  "quantity_text": item.get("quantity_text"),
                                                  "release_id": release_id}}]
        record = self._transfer_record(item, values, header, clock, basis)
        env = self.environment()
        before = self._latest_env_vintage(namespace, native)
        # Values carry the release id so the vintage cites it; an unchanged quantity and method keeps its vintage.
        if before is not None:
            stored = env.values(before["vintage_id"])
            same = stored and stored[0]["value"] == item["quantity"] and \
                stored[0]["flags"].get("method") == dict(item["method"]).get("code") and \
                not stored[0]["flags"].get("removed_by_source")
            if same:
                self._row(namespace, row_id, item, native, header, release_id)
                return row_id, before["vintage_id"], "unchanged"
            if clock < before["release_at_ms"]:
                raise WasteError("stale_release", "a release dated before the row's latest vintage is not appended",
                                 row_id=row_id)
        env.apply(namespace, [record], run_id=run_id, principal_id="environment.waste",
                  scopes=environment_scopes(namespace), evidence=self._evidence(header, release_id),
                  execution="waste-transfers", observed_at_ms=retrieved, record_provider_state=False)
        self._row(namespace, row_id, item, native, header, release_id)
        after = self._latest_env_vintage(namespace, native)
        return row_id, after["vintage_id"], "new"

    @staticmethod
    def _evidence(header, release_id):
        return {"waste_release_id": release_id, "file_sha256": header["file_sha256"], "url": header.get("url"),
                "dataset_version": header.get("dataset_version"), "truncated": bool(header.get("truncated")),
                "evidence_origin": header.get("evidence_origin"), "contract": CONTRACT}

    def _row(self, namespace, row_id, item, native, header, release_id) -> None:
        from src.kb.environment_store import record_id

        if self.conn.execute("SELECT 1 FROM waste_transfer_rows WHERE namespace=? AND row_id=?",
                             [namespace, row_id]).fetchone():
            return
        key = transfer_key(item)
        self.conn.execute(
            "INSERT INTO waste_transfer_rows VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, row_id, record_id(namespace, "observation_series", FACILITY_PROVIDER, native), native,
             key["inspire_id"], key["reporting_year"], key["hazardous"], key["treatment"], key["destination"],
             header["document_key"], release_id, self.now()])

    def _latest_env_vintage(self, namespace, native) -> dict[str, Any] | None:
        from src.kb.environment_store import record_id

        if not table_exists(self.conn, "environment_vintages"):
            return None
        rid = record_id(namespace, "observation_series", FACILITY_PROVIDER, native)
        row = self.conn.execute(
            "SELECT vintage_id, sequence, release_at_ms, retrieved_at_ms FROM environment_vintages WHERE "
            "namespace=? AND record_id=? ORDER BY sequence DESC LIMIT 1", [namespace, rid]).fetchone()
        return None if row is None else {"vintage_id": row[0], "sequence": int(row[1]), "release_at_ms": int(row[2]),
                                         "retrieved_at_ms": int(row[3]), "record_id": rid}

    def _transfer_removal(self, namespace, row_id, header, release_id, clock, retrieved, run_id) -> str | None:
        row = self.transfer_row(namespace, row_id)
        before = self._latest_env_vintage(namespace, row["native_id"])
        env = self.environment()
        if before is None:
            return None
        stored = env.values(before["vintage_id"])
        if stored and stored[0]["flags"].get("removed_by_source"):
            return None
        item = {"provider": TRANSFER_PROVIDER, "inspire_id": row["inspire_id"],
                "reporting_year": row["reporting_year"],
                "hazardous": {"code": row["hazardous"], "label": row["hazardous_label"]},
                "treatment": {"code": row["treatment"], "label": row["treatment_label"]},
                "destination": {"code": row["destination"], "label": row["destination_label"]},
                "method": {"code": None}, "facility_ref": f"{FACILITY_PROVIDER}:{row['inspire_id']}"}
        year = row["reporting_year"]
        # A removal is a dated vintage with no value and the removed_by_source flag: never a zero, never a deletion.
        values = [{"start": f"{year}-01-01", "end": f"{year + 1}-01-01", "value": None, "status": "unknown",
                   "flags": {"removed_by_source": True, "release_id": release_id,
                             "statement": "the source's complete response for this selection and reporting year no "
                                          "longer states the row; earlier vintages stay queryable"}}]
        record = self._transfer_record(item, values, header, clock, header.get("release_basis") or "retrieval_time")
        env.apply(namespace, [record], run_id=run_id, principal_id="environment.waste",
                  scopes=environment_scopes(namespace), evidence=self._evidence(header, release_id),
                  execution="waste-transfers", observed_at_ms=retrieved, record_provider_state=False)
        return self._latest_env_vintage(namespace, row["native_id"])["vintage_id"]

    # ------------------------------------------------------------------ receipts and failures

    def _receipt(self, namespace, run_id, source_id, provider, outcome, header, retrieved, detail):
        body = {"run_id": run_id, "source_id": source_id, "provider": provider, "outcome": outcome,
                "release_label": (header or {}).get("release_label"),
                "file_sha256": (header or {}).get("file_sha256"), "retrieved_at": iso(retrieved), **detail}
        receipt_id = "waste-receipt:" + digest([namespace, body])[:24]
        self.conn.execute("INSERT INTO waste_receipts VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                          [namespace, receipt_id, run_id, source_id, provider, outcome,
                           (header or {}).get("evidence_origin") or "none", canonical(body), self.now()])
        return receipt_id

    def record_failure(self, namespace: str, provider: str, *, code: str, run_id: str, source_id: str | None,
                       scopes: Iterable[str]) -> dict[str, Any]:
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        self._receipt(namespace, run_id, source_id, provider, "failed", None, self.now(), {"failure_code": code})
        return {"provider": provider, "failure_code": code,
                "effect": "stored vintages unchanged; the source reads as stale, nothing is marked removed or revised"}

    # ------------------------------------------------------------------ reads

    def provider_state(self, namespace: str, provider: str) -> dict[str, Any]:
        if not table_exists(self.conn, "waste_receipts"):
            return {"provider": provider, "last_success_ms": None, "stale": True, "reason": "never acquired"}
        rows = self.conn.execute("SELECT outcome, execution, created_at_ms, run_id, detail_json FROM waste_receipts "
                                 "WHERE namespace=? AND provider=? ORDER BY created_at_ms, receipt_id",
                                 [namespace, provider]).fetchall()
        successes = [r for r in rows if r[0] in {"applied", "unchanged"}]
        if not successes:
            return {"provider": provider, "last_success_ms": None, "stale": True,
                    "reason": "never acquired" if not rows else load(rows[-1][4], {}).get("failure_code")}
        failed = rows[-1][0] == "failed"
        return {"provider": provider, "last_success_ms": int(successes[-1][2]), "last_execution": successes[-1][1],
                "last_run_id": rows[-1][3], "stale": failed,
                "reason": load(rows[-1][4], {}).get("failure_code") if failed else None}

    def release(self, namespace: str, release_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT release_id, provider, source_id, format, document_key, document_json, release_label, published_on, "
            "published_at, release_basis, release_at_ms, retrieved_at_ms, dataflow_version, file_sha256, "
            "content_sha256, item_count, complete, truncated, dataset_version_json, evidence_origin, url, run_id, "
            "sequence FROM waste_releases WHERE namespace=? AND release_id=?", [namespace, release_id]).fetchone()
        if row is None:
            raise WasteError("not_found", "no such release")
        view = dict(zip(_RELEASE_KEYS, row))
        view["document"] = load(view["document"], {})
        view["dataset_version"] = load(view["dataset_version"], None)
        view["release_at"], view["retrieved_at"] = iso(view["release_at_ms"]), iso(view["retrieved_at_ms"])
        view["live_verification"] = LIVE_VERIFICATION[view["provider"]]["status"]
        return {"contract": CONTRACT, "record_type": "release", "namespace": namespace, **view}

    def releases(self, namespace: str, *, provider: str | None = None) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "waste_releases"):
            return []
        rows = self.conn.execute("SELECT release_id FROM waste_releases WHERE namespace=? AND (? IS NULL OR "
                                 "provider=?) ORDER BY release_at_ms, release_id",
                                 [namespace, provider, provider]).fetchall()
        return [self.release(namespace, r[0]) for r in rows]

    def release_members(self, namespace: str, release_id: str) -> list[dict[str, str]]:
        return [{"subject_kind": r[0], "subject_id": r[1], "vintage_id": r[2], "status": r[3]}
                for r in self.conn.execute(
                    "SELECT subject_kind, subject_id, vintage_id, status FROM waste_release_members WHERE "
                    "namespace=? AND release_id=? ORDER BY subject_id", [namespace, release_id]).fetchall()]

    def _series_view(self, namespace: str, row: Sequence[Any]) -> dict[str, Any]:
        (series_id, provider, dataset, native_key, key, indicator, parts, area, periodicity, frequency, document_key,
         economic_indicator, first_release) = row
        vintages = self.vintage_rows(namespace, series_id)
        current = vintages[-1] if vintages else None
        return {"contract": CONTRACT, "record_type": "series", "namespace": namespace, "series_id": series_id,
                "provider": provider, "dataset": dataset, "native_key": native_key, "key": load(key, {}),
                "indicator": load(indicator, {}), **load(parts, {}), "area": load(area, {}),
                "periodicity": periodicity, "frequency": frequency, "document_key": document_key,
                "first_release_id": first_release,
                "economic_series": {"domain": ECONOMIC_DOMAIN, "series_id": series_id,
                                    "indicator_id": economic_indicator},
                "vintage_count": len(vintages),
                "current_vintage_id": None if current is None else current["vintage_id"],
                "current_status": None if current is None else current["status"],
                "live_verification": LIVE_VERIFICATION[provider]["status"]}

    def series(self, namespace: str, series_id: str) -> dict[str, Any]:
        row = self.conn.execute(f"SELECT {_SERIES_COLUMNS} FROM waste_series WHERE namespace=? AND series_id=?",
                                [namespace, series_id]).fetchone() if self.ready() else None
        if row is None:
            raise WasteError("not_found", "no such waste series")
        return self._series_view(namespace, row)

    def find_series(self, namespace: str, *, provider: str | None = None, concept: str | None = None,
                    area_codes: Iterable[tuple[str, str]] | None = None) -> list[dict[str, Any]]:
        if not self.ready():
            return []
        rows = self.conn.execute(
            f"SELECT {_SERIES_COLUMNS} FROM waste_series WHERE namespace=? AND (? IS NULL OR provider=?) AND "
            "(? IS NULL OR concept=?) ORDER BY provider, concept, area_code, native_key, series_id",
            [namespace, provider, provider, concept, concept]).fetchall()
        wanted = None if area_codes is None else {(s, str(c)) for s, c in area_codes}
        out = []
        for row in rows:
            view = self._series_view(namespace, row)
            if wanted is not None and (view["area"]["scheme"], str(view["area"]["code"])) not in wanted:
                continue
            out.append(view)
        return out

    def vintage_rows(self, namespace: str, series_id: str) -> list[dict[str, Any]]:
        if not self.ready():
            return []
        rows = self.conn.execute(f"SELECT {_VINTAGE_COLUMNS} FROM waste_vintages WHERE namespace=? AND "
                                 "series_id=? ORDER BY sequence", [namespace, series_id]).fetchall()
        out = []
        for row in rows:
            view = dict(zip(_VINTAGE_KEYS, row))
            view["namespace"] = namespace
            view["changes"] = load(view["changes"], {})
            view["release_at"], view["retrieved_at"] = iso(view["release_at_ms"]), iso(view["retrieved_at_ms"])
            view["revision_of"] = view["previous_vintage_id"]
            view["economic_vintage_id"] = (f"{series_id}@{view['economic_as_of']}"
                                           if view["economic_as_of"] is not None else None)
            out.append(view)
        return out

    def observations(self, namespace: str, vintage_id: str) -> list[dict[str, Any]]:
        vintage = self.conn.execute("SELECT series_id, economic_as_of FROM waste_vintages WHERE namespace=? AND "
                                    "vintage_id=?", [namespace, vintage_id]).fetchone()
        if vintage is None:
            return []
        numeric = {r[0]: r[1] for r in self.conn.execute(
            "SELECT period, value FROM dataset_observations WHERE series_id=? AND as_of=?",
            [vintage[0], vintage[1]]).fetchall()} if vintage[1] is not None and \
            table_exists(self.conn, "dataset_observations") else {}
        rows = self.conn.execute(
            "SELECT period, value_text, value, status, flags_json, flag_meanings_json, attributes_json FROM "
            "waste_observations WHERE namespace=? AND vintage_id=? ORDER BY period", [namespace, vintage_id]).fetchall()
        return [{"record_type": "observation", "period": r[0], "value_text": r[1], "value": r[2],
                 "numeric_value": numeric.get(r[0]) if r[3] == "reported" else None, "status": r[3],
                 "flags": load(r[4], {}), "flag_meanings": load(r[5], []), "attributes": load(r[6], {})}
                for r in rows]

    def select_vintage(self, namespace: str, series_id: str, *, as_of_ms: int | None = None
                       ) -> tuple[dict[str, Any] | None, str | None]:
        """The vintage released on or before the cutoff; the reason when there is none."""
        vintages = self.vintage_rows(namespace, series_id)
        cutoff = as_of_ms if as_of_ms is not None else 2**62
        eligible = [v for v in vintages if v["release_at_ms"] <= cutoff]
        if eligible:
            return eligible[-1], None
        return None, "no_release_by_as_of" if vintages else "no_vintage"

    def definition(self, namespace: str, definition_id: str | None) -> dict[str, Any] | None:
        if not definition_id:
            return None
        row = self.conn.execute("SELECT definition_id, definition_key, revision, provider, content_json, release_id "
                                "FROM waste_definitions WHERE namespace=? AND definition_id=?",
                                [namespace, definition_id]).fetchone()
        if row is None:
            return None
        return {"contract": CONTRACT, "record_type": "definition", "definition_id": row[0], "definition_key": row[1],
                "revision": row[2], "provider": row[3], "content": load(row[4], {}), "release_id": row[5]}

    def notes(self, namespace: str, series_id: str) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "waste_notes"):
            return []
        return [{"note_id": r[0], "relation": r[1], "statement": r[2], "periods": load(r[3], []),
                 "cited": load(r[4], []), "origin": r[5]} for r in self.conn.execute(
            "SELECT note_id, relation, statement, periods_json, cited_json, origin FROM waste_notes WHERE namespace=? "
            "AND series_id=? ORDER BY created_at_ms, note_id", [namespace, series_id]).fetchall()]

    def values(self, namespace: str, series_id: str, *, as_of_ms: int | None = None,
               vintage_id: str | None = None) -> dict[str, Any]:
        series = self.series(namespace, series_id)
        reason = None
        if vintage_id:
            vintage = next((v for v in self.vintage_rows(namespace, series_id) if v["vintage_id"] == vintage_id), None)
            if vintage is None:
                raise WasteError("not_found", "vintage does not belong to this series")
        else:
            vintage, reason = self.select_vintage(namespace, series_id, as_of_ms=as_of_ms)
        if vintage is None:
            return {"contract": ANSWER_CONTRACT, "series": series, "status": "unavailable", "reason": reason,
                    "observations": []}
        release = self.release(namespace, vintage["release_id"])
        if vintage["status"] == "removed":
            return {"contract": ANSWER_CONTRACT, "series": series, "status": "removed_by_source",
                    "vintage": vintage, "citation": citation(series, vintage, release), "observations": [],
                    "note": "the source's release in force at this date no longer states the series; the earlier "
                            "vintages remain in its history"}
        return {"contract": ANSWER_CONTRACT, "series": series, "status": "available", "vintage": vintage,
                "release": release, "definition": self.definition(namespace, vintage["definition_id"]),
                "observations": self.observations(namespace, vintage["vintage_id"]),
                "citation": citation(series, vintage, release),
                "note": "values as published in this vintage; flags verbatim; confidential and unpublished cells "
                        "carry no value"}

    # ------------------------------------------------------------------ transfer-row reads

    def transfer_row(self, namespace: str, row_id: str) -> dict[str, Any]:
        from src.ingestion.waste_sources import (
            TRANSFER_DESTINATION,
            TRANSFER_HAZARD,
            TRANSFER_TREATMENT,
        )

        row = self.conn.execute(
            "SELECT row_id, environment_record_id, native_id, inspire_id, reporting_year, hazardous, treatment, "
            "destination, document_key, first_release_id FROM waste_transfer_rows WHERE namespace=? AND row_id=?",
            [namespace, row_id]).fetchone()
        if row is None:
            raise WasteError("not_found", "no such transfer row")
        return {"row_id": row[0], "environment_record_id": row[1], "native_id": row[2], "inspire_id": row[3],
                "reporting_year": int(row[4]), "hazardous": row[5], "hazardous_label": TRANSFER_HAZARD[row[5]],
                "treatment": row[6], "treatment_label": TRANSFER_TREATMENT[row[6]], "destination": row[7],
                "destination_label": TRANSFER_DESTINATION[row[7]], "document_key": row[8],
                "first_release_id": row[9]}

    def transfer_rows(self, namespace: str, *, inspire_id: str | None = None) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "waste_transfer_rows"):
            return []
        rows = self.conn.execute(
            "SELECT row_id FROM waste_transfer_rows WHERE namespace=? AND (? IS NULL OR inspire_id=?) ORDER BY "
            "inspire_id, reporting_year, hazardous, treatment, destination", [namespace, inspire_id, inspire_id]
        ).fetchall()
        return [self.transfer_row(namespace, r[0]) for r in rows]

    def transfer_vintages(self, namespace: str, row_id: str) -> list[dict[str, Any]]:
        """Every vintage of a transfer row (environment_vintages) with its value, method and removal state."""
        row = self.transfer_row(namespace, row_id)
        env = self.environment()
        out = []
        for vintage in env.vintages(namespace, row["environment_record_id"], scopes=environment_scopes(namespace)):
            values = env.values(vintage["vintage_id"])
            value = values[0] if values else {"value": None, "flags": {}}
            flags = dict(value.get("flags") or {})
            release_id = flags.get("release_id") or dict(vintage["evidence"]).get("waste_release_id")
            out.append({"vintage_id": vintage["vintage_id"], "sequence": vintage["sequence"],
                        "revision_of": vintage["revision_of"],
                        "status": "removed_by_source" if flags.get("removed_by_source") else "published",
                        "quantity": value.get("value"), "unit": "t", "quantity_text": flags.get("quantity_text"),
                        "method": flags.get("method"), "release_id": release_id,
                        "release_at": iso(vintage["release_at_ms"]), "release_at_ms": vintage["release_at_ms"],
                        "release_at_basis": vintage["release_at_basis"],
                        "retrieved_at": iso(vintage["retrieved_at_ms"]), "retrieved_at_ms": vintage["retrieved_at_ms"],
                        "release_time_status": vintage["release_time_status"],
                        "environment_record_id": row["environment_record_id"]})
        return out

    def latest_release_ms(self, namespace: str) -> int | None:
        if not table_exists(self.conn, "waste_releases"):
            return None
        row = self.conn.execute("SELECT max(release_at_ms) FROM waste_releases WHERE namespace=?",
                                [namespace]).fetchone()
        return None if row is None or row[0] is None else int(row[0])

    def receipts(self, namespace: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        authorize(namespace, scopes, READ_SCOPE)
        if not table_exists(self.conn, "waste_receipts"):
            return []
        rows = self.conn.execute("SELECT receipt_id, run_id, source_id, provider, outcome, execution, detail_json FROM "
                                 "waste_receipts WHERE namespace=? ORDER BY created_at_ms, receipt_id",
                                 [namespace]).fetchall()
        return [{"receipt_id": r[0], "run_id": r[1], "source_id": r[2], "provider": r[3], "outcome": r[4],
                 "execution": r[5], "detail": load(r[6], {})} for r in rows]


class WasteProjector:
    """Source-pack runtime projector for ``noesis-waste-record-v2`` pages (one release per page).

    Retrieval is stamped at the runtime's document ingestion time (else the store's clock), so a release is never
    dated after it.
    """

    @staticmethod
    def scopes_for(namespace: str) -> set[str]:
        return {WRITE_SCOPE, READ_SCOPE, f"namespace:{namespace}:write", f"namespace:{namespace}:read"}

    def __init__(self, conn: Any) -> None:
        self.store = WasteStore(conn)

    @staticmethod
    def _namespace(source: Mapping[str, Any]) -> str:
        return str(dict(source.get("waste") or {}).get("namespace") or "environment")

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        del manifest, page_receipt
        namespace = self._namespace(source)
        retrieved = max((int(d["ingested_at"]) for d in documents or () if d.get("ingested_at") is not None),
                        default=None)
        releases: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
        for record in records:
            header, body = record.get("waste_release"), record.get("waste_item")
            if not header or (body is not None and not isinstance(body, Mapping)):
                raise WasteError("invalid_record", "page record is not a waste release item")
            entry = releases.setdefault(header["file_sha256"] + canonical(header.get("document")), (dict(header), []))
            if body is not None:
                entry[1].append(dict(body))
        return [self.store.apply_release(namespace, header, items, source_id=source.get("source_id"), run_id=run_id,
                                         principal_id=principal_id or "source-runtime",
                                         scopes=self.scopes_for(namespace), retrieved_at_ms=retrieved)
                for header, items in releases.values()]

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del manifest, principal_id
        namespace = self._namespace(source)
        provider = str(dict(source.get("waste") or {}).get("provider"))
        if status != "complete":
            self.store.record_failure(namespace, provider, code="source_run_" + str(status), run_id=run_id,
                                      source_id=source.get("source_id"), scopes=self.scopes_for(namespace))
        return {"status": status, "provider": provider, "namespace": namespace,
                "provider_state": self.store.provider_state(namespace, provider)}


__all__ = ["TABLES", "WasteProjector", "WasteStore", "citation", "environment_scopes"]
