"""Append-only store for industry and business statistics series, definitions and vintages (#2738, IB02).

Follows the labour and income series stores. Tables (all ``business_*``, namespace-scoped):

* ``business_releases`` - one row per acquired publication: provider, declared document and its document key, release
  label and clock (``provider_last_update``, ``declared_release`` or ``retrieval_time``), retrieval clock, dataflow
  version (or the CBP NAICS variable), digests, evidence origin, run id;
* ``business_series`` - one row per series key (:func:`src.ingestion.business_statistics_sources.series_key`);
* ``business_definitions`` - definition revisions per definition key; a changed definition is a new revision;
* ``business_vintages`` - one row per release of a series, never overwritten: release and retrieval clocks, definition
  revision, dataflow version, recorded changes and the Economics vintage it lives in;
* ``business_observations`` - values as published per vintage and period, status and flags verbatim;
* ``business_release_members`` - which series each release stated, so a complete release that no longer states a
  series records a ``removed_by_source`` vintage instead of a deletion;
* ``business_comparability`` - source-stated breaks, provisional periods and base-year changes, and reviewer notes;
* ``business_receipts`` - one receipt per applied release or failure (provider state is read from them).

A release cannot be dated after its retrieval. As-of reads select the vintage released on or before the requested
instant (release clock) and say which. Nothing is overwritten, filled, re-based or reconstructed.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from decimal import Decimal
from typing import Any

from src.ingestion.business_statistics_sources import (
    FORMATS,
    LIVE_VERIFICATION,
    series_key,
)
from src.kb.business_statistics_records import (
    ANSWER_CONTRACT,
    CONTRACT,
    ECONOMIC_DOMAIN,
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    BusinessError,
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
CREATE TABLE IF NOT EXISTS business_releases (
  namespace TEXT NOT NULL, release_id TEXT NOT NULL, provider TEXT NOT NULL, source_id TEXT, format TEXT NOT NULL,
  document_key TEXT NOT NULL, document_json TEXT NOT NULL, release_label TEXT, published_on TEXT, published_at TEXT,
  release_basis TEXT NOT NULL, release_at_ms BIGINT NOT NULL, retrieved_at_ms BIGINT NOT NULL,
  dataflow_version TEXT, file_sha256 TEXT NOT NULL, content_sha256 TEXT NOT NULL, item_count INTEGER NOT NULL,
  complete BOOLEAN NOT NULL, evidence_origin TEXT NOT NULL, url TEXT, run_id TEXT NOT NULL, recorded_by TEXT,
  sequence INTEGER NOT NULL, PRIMARY KEY(namespace, release_id)
);
CREATE TABLE IF NOT EXISTS business_series (
  namespace TEXT NOT NULL, series_id TEXT NOT NULL, provider TEXT NOT NULL, dataset TEXT NOT NULL,
  native_key TEXT NOT NULL, key_json TEXT NOT NULL, indicator_json TEXT NOT NULL, concept TEXT NOT NULL,
  classification_json TEXT NOT NULL, size_class_json TEXT NOT NULL, area_scheme TEXT NOT NULL,
  area_code TEXT NOT NULL, area_json TEXT NOT NULL, adjustment TEXT NOT NULL, unit_json TEXT NOT NULL,
  statistical_unit TEXT NOT NULL, frequency TEXT NOT NULL, document_key TEXT NOT NULL,
  economic_indicator_id TEXT NOT NULL, first_release_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, series_id)
);
CREATE TABLE IF NOT EXISTS business_definitions (
  namespace TEXT NOT NULL, definition_id TEXT NOT NULL, definition_key TEXT NOT NULL, revision INTEGER NOT NULL,
  provider TEXT NOT NULL, content_json TEXT NOT NULL, content_hash TEXT NOT NULL, release_id TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, definition_id)
);
CREATE TABLE IF NOT EXISTS business_vintages (
  namespace TEXT NOT NULL, vintage_id TEXT NOT NULL, series_id TEXT NOT NULL, release_id TEXT NOT NULL,
  sequence INTEGER NOT NULL, status TEXT NOT NULL, release_at_ms BIGINT NOT NULL, release_basis TEXT NOT NULL,
  retrieved_at_ms BIGINT NOT NULL, economic_as_of BIGINT, content_hash TEXT NOT NULL, definition_id TEXT,
  dataflow_version TEXT, release_label TEXT, previous_vintage_id TEXT, changes_json TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, vintage_id)
);
CREATE TABLE IF NOT EXISTS business_observations (
  namespace TEXT NOT NULL, vintage_id TEXT NOT NULL, period TEXT NOT NULL, value_text TEXT, value TEXT,
  status TEXT NOT NULL, flags_json TEXT NOT NULL, flag_meanings_json TEXT NOT NULL, attributes_json TEXT NOT NULL,
  PRIMARY KEY(namespace, vintage_id, period)
);
CREATE TABLE IF NOT EXISTS business_release_members (
  namespace TEXT NOT NULL, release_id TEXT NOT NULL, series_id TEXT NOT NULL, vintage_id TEXT NOT NULL,
  status TEXT NOT NULL, PRIMARY KEY(namespace, release_id, series_id)
);
CREATE TABLE IF NOT EXISTS business_comparability (
  namespace TEXT NOT NULL, note_id TEXT NOT NULL, series_id TEXT NOT NULL, other_series_id TEXT,
  relation TEXT NOT NULL, statement TEXT NOT NULL, periods_json TEXT NOT NULL, cited_json TEXT NOT NULL,
  origin TEXT NOT NULL, state TEXT NOT NULL, history_json TEXT NOT NULL, created_by TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, note_id)
);
CREATE TABLE IF NOT EXISTS business_receipts (
  namespace TEXT NOT NULL, receipt_id TEXT NOT NULL, run_id TEXT NOT NULL, source_id TEXT, provider TEXT NOT NULL,
  outcome TEXT NOT NULL, execution TEXT NOT NULL, detail_json TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, receipt_id)
);
"""
RELATIONS = ("break_in_series", "provisional", "base_year_change", "classification_change", "source_note",
             "different_definition", "different_statistical_unit", "not_comparable", "comparable")
NOTE_STATES = ("source-stated", "proposed", "accepted", "rejected", "reverted")
ACTIVE_STATES = ("source-stated", "proposed", "accepted")
# Economics model vocabulary for each published adjustment (calendar adjustment alone is not seasonal adjustment).
ECONOMIC_SEASONAL = {"NSA": "not_adjusted", "CA": "not_adjusted", "SCA": "adjusted", "SA": "adjusted",
                     "not_applicable": "not_applicable"}
_SERIES_COLUMNS = ("series_id, provider, dataset, native_key, key_json, indicator_json, classification_json, "
                   "size_class_json, area_json, adjustment, unit_json, statistical_unit, frequency, document_key, "
                   "economic_indicator_id, first_release_id")
_VINTAGE_COLUMNS = ("vintage_id, series_id, release_id, sequence, status, release_at_ms, release_basis, "
                    "retrieved_at_ms, economic_as_of, content_hash, definition_id, dataflow_version, release_label, "
                    "previous_vintage_id, changes_json")
_VINTAGE_KEYS = ("vintage_id", "series_id", "release_id", "sequence", "status", "release_at_ms", "release_basis",
                 "retrieved_at_ms", "economic_as_of", "content_hash", "definition_id", "dataflow_version",
                 "release_label", "previous_vintage_id", "changes")


def _content(observations: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    keys = ("period", "value_text", "value", "status", "flags")
    return [{k: o.get(k) for k in keys} for o in sorted(observations, key=lambda o: o["period"])]


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


class BusinessStatisticsStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def ready(self) -> bool:
        return table_exists(self.conn, "business_vintages")

    # ------------------------------------------------------------------ writes

    def apply_release(self, namespace: str, header: Mapping[str, Any], items: Sequence[Mapping[str, Any]], *,
                      source_id: str | None, run_id: str, principal_id: str, scopes: Iterable[str],
                      retrieved_at_ms: int | None = None) -> dict[str, Any]:
        """Append one release: series, definition revisions, vintages and removals; never an overwrite."""
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        provider, fmt = str(header.get("provider") or ""), header.get("format")
        if FORMATS.get(str(fmt), {}).get("provider") != provider:
            raise BusinessError("invalid_release", "a release names a known provider and its format")
        if int(header.get("item_count", -1)) != len(items):
            raise BusinessError("incomplete_release", "a release carries every item it states")
        for item in items:
            # The whole release is refused when one item breaks the record rules or the minimisation decision.
            check_item(item)
            if item["provider"] != provider:
                raise BusinessError("invalid_release", "every item of a release belongs to its provider")
        retrieved = int(retrieved_at_ms if retrieved_at_ms is not None else self.now())
        clock, basis = self._clock(header, retrieved)
        if clock > retrieved:
            raise BusinessError("invalid_release", "a release cannot be dated after its retrieval")
        release_id = "bs-release:" + digest([namespace, header["document_key"], header.get("release_label"),
                                             header["file_sha256"]])[:24]
        if self.conn.execute("SELECT 1 FROM business_releases WHERE namespace=? AND release_id=?",
                             [namespace, release_id]).fetchone():
            self._receipt(namespace, run_id, source_id, provider, "unchanged", header, retrieved,
                          {"release_id": release_id})
            return {"release_id": release_id, "status": "unchanged", "vintages": 0, "unchanged_series": 0,
                    "removed_series": 0}
        self.conn.execute("BEGIN")
        try:
            sequence = 1 + int(self.conn.execute(
                "SELECT count(*) FROM business_releases WHERE namespace=? AND document_key=?",
                [namespace, header["document_key"]]).fetchone()[0])
            previous = self.conn.execute(
                "SELECT release_id FROM business_releases WHERE namespace=? AND document_key=? AND complete "
                "ORDER BY sequence DESC LIMIT 1", [namespace, header["document_key"]]).fetchone()
            self.conn.execute(
                "INSERT INTO business_releases VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [namespace, release_id, provider, source_id, fmt, header["document_key"],
                 canonical(header.get("document") or {}), header.get("release_label"), header.get("published_on"),
                 header.get("published_at"), basis, clock, retrieved, header.get("dataflow_version"),
                 header["file_sha256"], header.get("content_sha256") or "", len(items),
                 bool(header.get("complete", True)), self._origin(header), header.get("url"), run_id, principal_id,
                 sequence])
            counts = {"new": 0, "unchanged": 0, "removed": 0}
            stated: dict[str, dict[str, Any]] = {}
            for item in items:
                series_id = self._series(namespace, item, header, release_id)
                if series_id in stated:
                    raise BusinessError("invalid_release", "a release states the same series twice")
                stated[series_id] = dict(item)
                definition_id = self._definition(namespace, item, release_id)
                vintage_id, status = self._vintage(namespace, series_id, item, header, release_id, clock, basis,
                                                   retrieved, definition_id)
                counts[status] += 1
                self.conn.execute("INSERT INTO business_release_members VALUES (?,?,?,?,?)",
                                  [namespace, release_id, series_id, vintage_id, status])
                if status == "new":
                    self._source_notes(namespace, series_id, item, vintage_id)
            if previous and header.get("complete", True):
                for (series_id,) in self.conn.execute(
                        "SELECT series_id FROM business_release_members WHERE namespace=? AND release_id=? "
                        "AND status <> 'removed' ORDER BY series_id", [namespace, previous[0]]).fetchall():
                    if series_id in stated:
                        continue
                    vintage_id = self._removal(namespace, series_id, header, release_id, clock, basis, retrieved)
                    if vintage_id:
                        counts["removed"] += 1
                        self.conn.execute("INSERT INTO business_release_members VALUES (?,?,?,?,?)",
                                          [namespace, release_id, series_id, vintage_id, "removed"])
                        self._successor_notes(namespace, series_id, stated, vintage_id)
            self._receipt(namespace, run_id, source_id, provider, "applied", header, retrieved,
                          {"release_id": release_id, **counts})
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {"release_id": release_id, "status": "applied", "published_on": header.get("published_on"),
                "release_basis": basis, "vintages": counts["new"], "unchanged_series": counts["unchanged"],
                "removed_series": counts["removed"]}

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

    def _series(self, namespace, item, header, release_id) -> str:
        key = series_key(item)
        series_id = f"business:{item['provider']}:" + digest(key)[:20]
        if not self.conn.execute("SELECT 1 FROM business_series WHERE namespace=? AND series_id=?",
                                 [namespace, series_id]).fetchone():
            self.conn.execute(
                "INSERT INTO business_series VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [namespace, series_id, item["provider"], item["dataset"], item["native_key"], canonical(key),
                 canonical(item["indicator"]), item["indicator"]["concept"], canonical(item["classification"]),
                 canonical(item["size_class"]), item["area"]["scheme"], str(item["area"]["code"]),
                 canonical(item["area"]), item["adjustment"], canonical(item["unit"]), item["statistical_unit"],
                 item["frequency"], header["document_key"], "business-indicator:" + series_id.split(":", 2)[2],
                 release_id, self.now()])
        return series_id

    @staticmethod
    def definition_key(item: Mapping[str, Any]) -> str:
        classification = dict(item["classification"])
        return f"{item['provider']}:" + digest([item["dataset"], item["indicator"].get("code"),
                                                item["indicator"]["concept"], item["statistical_unit"],
                                                classification.get("scheme"), classification.get("version"),
                                                item.get("adjustment")])[:16]

    def _definition(self, namespace, item, release_id) -> str:
        key = self.definition_key(item)
        content = {**dict(item["definition"]), "source_notes": [
            {k: n.get(k) for k in ("kind", "attribute", "value")} for n in item.get("source_notes") or []
            if n.get("kind") not in {"break", "provisional"}]}
        content_hash = digest(content)
        latest = self.conn.execute(
            "SELECT definition_id, content_hash, revision FROM business_definitions WHERE namespace=? AND "
            "definition_key=? ORDER BY revision DESC LIMIT 1", [namespace, key]).fetchone()
        if latest and latest[1] == content_hash:
            return latest[0]
        revision = 1 + (int(latest[2]) if latest else 0)
        definition_id = f"bs-def:{key}:r{revision}"
        self.conn.execute("INSERT INTO business_definitions VALUES (?,?,?,?,?,?,?,?,?)",
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
            # An unchanged re-publication adds no vintage: the earlier vintage stays current.
            return prior["vintage_id"], "unchanged"
        if any(v["release_at_ms"] == clock for v in previous):
            raise BusinessError("vintage_conflict", "the publication changed values without a new release time; the "
                                                    "stored vintage is kept", series_id=series_id)
        if prior is not None and clock < prior["release_at_ms"]:
            raise BusinessError("stale_release", "a release dated before the series' latest vintage is not appended",
                                series_id=series_id)
        changes = self._changes(namespace, prior, observations, definition_id, header)
        record = SeriesRecord(
            series_id=series_id, provider=item["provider"],
            title=str(item["indicator"].get("label") or item["native_key"]),
            frequency=item["frequency"], as_of=int(clock),
            observations=[{"period": o["period"], "value": None if o.get("value") is None
                           else float(Decimal(o["value"]))} for o in sorted(observations, key=lambda o: o["period"])],
            unit=dict(item["unit"]).get("label") or None, geography=str(item["area"]["code"]),
            source_url=header.get("url"),
            metadata={"provider_release_at_ms": int(clock),
                      "provider_release_time_status": f"business release clock ({basis})",
                      "acquired_at_ms": int(retrieved), "vintage_basis": basis, "source_document_id": release_id},
        )
        semantics = {
            "indicator_id": "business-indicator:" + series_id.split(":", 2)[2],
            "canonical_name": str(item["indicator"].get("label") or item["indicator"]["concept"]),
            "concept": f"business {item['indicator']['concept']} {series_id}",
            "definition": dict(item["definition"]).get("source_text") or item["indicator"]["concept"],
            "seasonal_adjustment": ECONOMIC_SEASONAL[item["adjustment"]],
            "price_basis": "index" if dict(item["unit"]).get("base_year") else "not_applicable",
            "provider_code": str(item["native_key"]),
            "provider_definition": dict(item["definition"]).get("source_text") or item["indicator"]["concept"],
            "attributes": {"business_series_id": series_id, "adjustment": item["adjustment"],
                           "statistical_unit": item["statistical_unit"], "classification": item["classification"],
                           "contract": CONTRACT},
        }
        try:
            economic = register_series(self.conn, record, semantics=semantics, domain=ECONOMIC_DOMAIN)
        except EconomicModelError as exc:
            raise BusinessError(exc.code, str(exc)) from exc
        vintage_id = "bs-vintage:" + digest([namespace, series_id, release_id, content_hash])[:24]
        self.conn.execute(
            "INSERT INTO business_vintages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, vintage_id, series_id, release_id, 1 + len(previous), "published", clock, basis, retrieved,
             economic["vintage"]["as_of"], content_hash, definition_id, header.get("dataflow_version"),
             header.get("release_label"), prior["vintage_id"] if prior else None, canonical(changes), self.now()])
        for obs in observations:
            self.conn.execute(
                "INSERT INTO business_observations VALUES (?,?,?,?,?,?,?,?,?)",
                [namespace, vintage_id, obs["period"], obs.get("value_text"), obs.get("value"), obs["status"],
                 canonical(dict(obs.get("flags") or {})), canonical(list(obs.get("flag_meanings") or [])),
                 canonical(dict(obs.get("attributes") or {}))])
        return vintage_id, "new"

    def _removal(self, namespace, series_id, header, release_id, clock, basis, retrieved) -> str | None:
        previous = self.vintage_rows(namespace, series_id)
        prior = previous[-1] if previous else None
        if prior is None or prior["status"] == "removed" or clock <= prior["release_at_ms"]:
            return None
        vintage_id = "bs-vintage:" + digest([namespace, series_id, release_id, "removed"])[:24]
        changes = {"removed_by_source": {"release_label": header.get("release_label"),
                                         "statement": "the source's complete release of this document no longer "
                                                      "states the series; earlier vintages stay queryable"}}
        self.conn.execute(
            "INSERT INTO business_vintages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, vintage_id, series_id, release_id, 1 + len(previous), "removed", clock, basis, retrieved, None,
             digest("removed"), prior["definition_id"], header.get("dataflow_version"), header.get("release_label"),
             prior["vintage_id"], canonical(changes), self.now()])
        return vintage_id

    def _note(self, namespace, series_id, other_series_id, relation, statement, periods, cited, origin, state,
              principal_id, history) -> str:
        note_id = "bs-note:" + digest([namespace, series_id, other_series_id, relation, statement,
                                       sorted(periods)])[:24]
        self.conn.execute(
            "INSERT INTO business_comparability VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
            [namespace, note_id, series_id, other_series_id, relation, statement, canonical(sorted(periods)),
             canonical(cited), origin, state, canonical(history), principal_id, self.now()])
        return note_id

    def _source_notes(self, namespace, series_id, item, vintage_id) -> None:
        for note in item.get("source_notes") or []:
            relation = {"break": "break_in_series", "provisional": "provisional"}.get(note.get("kind"), "source_note")
            self._note(namespace, series_id, None, relation, str(note.get("statement") or note.get("value")),
                       list(note.get("periods") or []), [{"vintage_id": vintage_id, "attribute": note.get("attribute")}],
                       "source-stated", "source-stated", "source", [])

    def _successor_notes(self, namespace, removed_id, stated, vintage_id) -> None:
        """A removed series whose key reappears in the same release under another index base year or classification
        version: a source-stated note linking the two series (they stay separate; nothing is re-based or merged)."""
        removed = self.series(namespace, removed_id)
        before = removed["key"]
        for series_id, item in stated.items():
            after = series_key(item)
            if series_id == removed_id:
                continue
            same_but = {k for k in before if before[k] != after.get(k)}
            if same_but == {"unit"} and before["unit"]["code"] != after["unit"]["code"]:
                relation = "base_year_change"
                statement = (f"the source now publishes this series with unit {after['unit']['code']} (base year "
                             f"{after['unit']['base_year']}) instead of {before['unit']['code']} (base year "
                             f"{before['unit']['base_year']}); the two are separate series and nothing is re-based")
            elif same_but == {"classification"} and before["classification"]["scheme"] == \
                    after["classification"]["scheme"]:
                relation = "classification_change"
                statement = (f"the source now classifies this series by {after['classification']['scheme']} "
                             f"{after['classification']['version']} instead of {before['classification']['version']}")
            else:
                continue
            self._note(namespace, removed_id, series_id, relation, statement, [],
                       [{"vintage_id": vintage_id, "basis": "the same complete release removed one and stated the "
                                                             "other"}], "source-stated", "source-stated", "source", [])

    def record_note(self, namespace: str, series_id: str, relation: str, statement: str, *,
                    other_series_id: str | None = None, periods: Sequence[str] = (),
                    cited: Sequence[Mapping[str, Any]], principal_id: str, scopes: Iterable[str]) -> dict[str, Any]:
        """A reviewer-proposed comparability note between series (cited; nothing is harmonised or merged)."""
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        if relation not in RELATIONS or not str(statement or "").strip():
            raise BusinessError("invalid_note", f"a note has one of {RELATIONS} and a statement")
        if not cited:
            raise BusinessError("invalid_note", "a comparability note cites the methodology or release it rests on")
        self.series(namespace, series_id)
        if other_series_id:
            if other_series_id == series_id:
                raise BusinessError("invalid_note", "a note links two different series")
            self.series(namespace, other_series_id)
        note_id = self._note(namespace, series_id, other_series_id, relation, statement.strip(),
                             [str(p) for p in periods], [dict(c) for c in cited], "recorded", "proposed", principal_id,
                             [{"state": "proposed", "by": principal_id, "at": iso(self.now())}])
        return self.note(namespace, note_id)

    def review_note(self, namespace: str, note_id: str, decision: str, reason: str, *, principal_id: str,
                    scopes: Iterable[str]) -> dict[str, Any]:
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if not str(reason or "").strip():
            raise BusinessError("invalid_decision", "a review states its reason")
        note = self.note(namespace, note_id)
        transitions = {"accept": ("proposed", "accepted"), "reject": ("proposed", "rejected"),
                       "revert": ("accepted", "reverted")}
        if decision not in transitions or note["state"] != transitions[decision][0]:
            raise BusinessError("invalid_transition", f"cannot {decision} a {note['state']} note")
        if decision != "revert" and note["created_by"] == principal_id:
            raise BusinessError("self_review", "a note is reviewed by another principal")
        history = note["history"] + [{"state": transitions[decision][1], "by": principal_id, "reason": reason,
                                      "at": iso(self.now())}]
        self.conn.execute("UPDATE business_comparability SET state=?, history_json=? WHERE namespace=? AND note_id=?",
                          [transitions[decision][1], canonical(history), namespace, note_id])
        return self.note(namespace, note_id)

    def note(self, namespace: str, note_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT note_id, series_id, other_series_id, relation, statement, periods_json, cited_json, origin, state, "
            "history_json, created_by FROM business_comparability WHERE namespace=? AND note_id=?",
            [namespace, note_id]).fetchone()
        if row is None:
            raise BusinessError("not_found", "no such comparability note")
        return {"note_id": row[0], "series_id": row[1], "other_series_id": row[2], "relation": row[3],
                "statement": row[4], "periods": load(row[5], []), "cited": load(row[6], []), "origin": row[7],
                "state": row[8], "history": load(row[9], []), "created_by": row[10]}

    def notes(self, namespace: str, series_id: str, *, active_only: bool = True) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "business_comparability"):
            return []
        rows = self.conn.execute(
            "SELECT note_id, state FROM business_comparability WHERE namespace=? AND (series_id=? OR "
            "other_series_id=?) ORDER BY created_at_ms, note_id", [namespace, series_id, series_id]).fetchall()
        return [self.note(namespace, r[0]) for r in rows if not active_only or r[1] in ACTIVE_STATES]

    def _receipt(self, namespace, run_id, source_id, provider, outcome, header, retrieved, detail):
        body = {"run_id": run_id, "source_id": source_id, "provider": provider, "outcome": outcome,
                "release_label": (header or {}).get("release_label"),
                "file_sha256": (header or {}).get("file_sha256"), "retrieved_at": iso(retrieved), **detail}
        receipt_id = "bs-receipt:" + digest([namespace, body])[:24]
        self.conn.execute("INSERT INTO business_receipts VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
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
        if not table_exists(self.conn, "business_receipts"):
            return {"provider": provider, "last_success_ms": None, "stale": True, "reason": "never acquired"}
        rows = self.conn.execute("SELECT outcome, execution, created_at_ms, run_id, detail_json FROM business_receipts "
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

    _RELEASE_KEYS = ("release_id", "provider", "source_id", "format", "document_key", "document", "release_label",
                     "published_on", "published_at", "release_basis", "release_at_ms", "retrieved_at_ms",
                     "dataflow_version", "file_sha256", "content_sha256", "item_count", "complete",
                     "evidence_origin", "url", "run_id", "sequence")

    def release(self, namespace: str, release_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT release_id, provider, source_id, format, document_key, document_json, release_label, published_on, "
            "published_at, release_basis, release_at_ms, retrieved_at_ms, dataflow_version, file_sha256, "
            "content_sha256, item_count, complete, evidence_origin, url, run_id, sequence FROM business_releases "
            "WHERE namespace=? AND release_id=?", [namespace, release_id]).fetchone()
        if row is None:
            raise BusinessError("not_found", "no such release")
        view = dict(zip(self._RELEASE_KEYS, row))
        view["document"] = load(view["document"], {})
        view["release_at"], view["retrieved_at"] = iso(view["release_at_ms"]), iso(view["retrieved_at_ms"])
        view["live_verification"] = LIVE_VERIFICATION[view["provider"]]["status"]
        return {"contract": CONTRACT, "record_type": "release", "namespace": namespace, **view}

    def releases(self, namespace: str, *, provider: str | None = None) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "business_releases"):
            return []
        rows = self.conn.execute("SELECT release_id FROM business_releases WHERE namespace=? AND (? IS NULL OR "
                                 "provider=?) ORDER BY release_at_ms, release_id",
                                 [namespace, provider, provider]).fetchall()
        return [self.release(namespace, r[0]) for r in rows]

    def release_series(self, namespace: str, release_id: str) -> list[dict[str, str]]:
        return [{"series_id": r[0], "vintage_id": r[1], "status": r[2]} for r in self.conn.execute(
            "SELECT series_id, vintage_id, status FROM business_release_members WHERE namespace=? AND release_id=? "
            "ORDER BY series_id", [namespace, release_id]).fetchall()]

    def _series_view(self, namespace: str, row: Sequence[Any]) -> dict[str, Any]:
        (series_id, provider, dataset, native_key, key, indicator, classification, size_class, area, adjustment, unit,
         statistical_unit, frequency, document_key, economic_indicator, first_release) = row
        vintages = self.vintage_rows(namespace, series_id)
        current = vintages[-1] if vintages else None
        return {"contract": CONTRACT, "record_type": "series", "namespace": namespace, "series_id": series_id,
                "provider": provider, "dataset": dataset, "native_key": native_key, "key": load(key, {}),
                "indicator": load(indicator, {}), "classification": load(classification, {}),
                "size_class": load(size_class, {}), "area": load(area, {}), "adjustment": adjustment,
                "unit": load(unit, {}), "statistical_unit": statistical_unit, "frequency": frequency,
                "document_key": document_key, "first_release_id": first_release,
                "economic_series": {"domain": ECONOMIC_DOMAIN, "series_id": series_id,
                                    "indicator_id": economic_indicator},
                "vintage_count": len(vintages),
                "current_vintage_id": None if current is None else current["vintage_id"],
                "current_status": None if current is None else current["status"],
                "live_verification": LIVE_VERIFICATION[provider]["status"]}

    def series(self, namespace: str, series_id: str) -> dict[str, Any]:
        row = self.conn.execute(f"SELECT {_SERIES_COLUMNS} FROM business_series WHERE namespace=? AND series_id=?",
                                [namespace, series_id]).fetchone() if self.ready() else None
        if row is None:
            raise BusinessError("not_found", "no such business series")
        return self._series_view(namespace, row)

    def find_series(self, namespace: str, *, provider: str | None = None, concept: str | None = None,
                    area_codes: Iterable[tuple[str, str]] | None = None,
                    classification: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        if not self.ready() or not table_exists(self.conn, "business_series"):
            return []
        rows = self.conn.execute(
            f"SELECT {_SERIES_COLUMNS} FROM business_series WHERE namespace=? AND (? IS NULL OR provider=?) AND "
            "(? IS NULL OR concept=?) ORDER BY provider, concept, area_code, native_key, series_id",
            [namespace, provider, provider, concept, concept]).fetchall()
        wanted = None if area_codes is None else {(s, str(c)) for s, c in area_codes}
        out = []
        for row in rows:
            view = self._series_view(namespace, row)
            if wanted is not None and (view["area"]["scheme"], str(view["area"]["code"])) not in wanted:
                continue
            if classification and any(str(view["classification"].get(k)) != str(classification[k])
                                      for k in ("scheme", "version", "code") if classification.get(k) is not None):
                continue
            out.append(view)
        return out

    def vintage_rows(self, namespace: str, series_id: str) -> list[dict[str, Any]]:
        if not self.ready():
            return []
        rows = self.conn.execute(f"SELECT {_VINTAGE_COLUMNS} FROM business_vintages WHERE namespace=? AND "
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

    def vintage(self, namespace: str, vintage_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT series_id FROM business_vintages WHERE namespace=? AND vintage_id=?",
                                [namespace, vintage_id]).fetchone()
        if row is None:
            raise BusinessError("not_found", "no such vintage")
        return next(v for v in self.vintage_rows(namespace, row[0]) if v["vintage_id"] == vintage_id)

    def observations(self, namespace: str, vintage_id: str) -> list[dict[str, Any]]:
        vintage = self.conn.execute("SELECT series_id, economic_as_of FROM business_vintages WHERE namespace=? AND "
                                    "vintage_id=?", [namespace, vintage_id]).fetchone()
        if vintage is None:
            return []
        numeric = {r[0]: r[1] for r in self.conn.execute(
            "SELECT period, value FROM dataset_observations WHERE series_id=? AND as_of=?",
            [vintage[0], vintage[1]]).fetchall()} if vintage[1] is not None and \
            table_exists(self.conn, "dataset_observations") else {}
        rows = self.conn.execute(
            "SELECT period, value_text, value, status, flags_json, flag_meanings_json, attributes_json FROM "
            "business_observations WHERE namespace=? AND vintage_id=? ORDER BY period",
            [namespace, vintage_id]).fetchall()
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
                                "FROM business_definitions WHERE namespace=? AND definition_id=?",
                                [namespace, definition_id]).fetchone()
        if row is None:
            return None
        return {"contract": CONTRACT, "record_type": "definition", "definition_id": row[0], "definition_key": row[1],
                "revision": row[2], "provider": row[3], "content": load(row[4], {}), "release_id": row[5]}

    def values(self, namespace: str, series_id: str, *, as_of_ms: int | None = None,
               vintage_id: str | None = None) -> dict[str, Any]:
        series = self.series(namespace, series_id)
        reason = None
        if vintage_id:
            vintage = next((v for v in self.vintage_rows(namespace, series_id) if v["vintage_id"] == vintage_id), None)
            if vintage is None:
                raise BusinessError("not_found", "vintage does not belong to this series")
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
                "note": "values as published in this vintage; flags verbatim; withheld, confidential and unpublished "
                        "cells carry no value"}

    def latest_release_ms(self, namespace: str) -> int | None:
        if not table_exists(self.conn, "business_releases"):
            return None
        row = self.conn.execute("SELECT max(release_at_ms) FROM business_releases WHERE namespace=?",
                                [namespace]).fetchone()
        return None if row is None or row[0] is None else int(row[0])

    def receipts(self, namespace: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        authorize(namespace, scopes, READ_SCOPE)
        if not table_exists(self.conn, "business_receipts"):
            return []
        rows = self.conn.execute("SELECT receipt_id, run_id, source_id, provider, outcome, execution, detail_json FROM "
                                 "business_receipts WHERE namespace=? ORDER BY created_at_ms, receipt_id",
                                 [namespace]).fetchall()
        return [{"receipt_id": r[0], "run_id": r[1], "source_id": r[2], "provider": r[3], "outcome": r[4],
                 "execution": r[5], "detail": load(r[6], {})} for r in rows]


class BusinessStatisticsProjector:
    """Source-pack runtime projector for ``noesis-business-statistics-record-v2`` pages (one release per page).

    Retrieval is stamped at the store's clock (the real clock in the runtime), so a release is never dated after it.
    """

    @staticmethod
    def scopes_for(namespace: str) -> set[str]:
        return {WRITE_SCOPE, READ_SCOPE, f"namespace:{namespace}:write", f"namespace:{namespace}:read"}

    def __init__(self, conn: Any) -> None:
        self.store = BusinessStatisticsStore(conn)

    @staticmethod
    def _namespace(source: Mapping[str, Any]) -> str:
        return str(dict(source.get("business_statistics") or {}).get("namespace") or "global")

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        del manifest, page_receipt, documents
        namespace = self._namespace(source)
        releases: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
        for record in records:
            header, body = record.get("business_release"), record.get("business_item")
            if not header or not isinstance(body, Mapping):
                raise BusinessError("invalid_record", "page record is not a business release item")
            releases.setdefault(header["file_sha256"] + canonical(header.get("document")),
                                (dict(header), []))[1].append(dict(body))
        return [self.store.apply_release(namespace, header, items, source_id=source.get("source_id"), run_id=run_id,
                                         principal_id=principal_id or "source-runtime",
                                         scopes=self.scopes_for(namespace))
                for header, items in releases.values()]

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del manifest, principal_id
        namespace = self._namespace(source)
        provider = str(dict(source.get("business_statistics") or {}).get("provider"))
        if status != "complete":
            self.store.record_failure(namespace, provider, code="source_run_" + str(status), run_id=run_id,
                                      source_id=source.get("source_id"), scopes=self.scopes_for(namespace))
        return {"status": status, "provider": provider, "namespace": namespace,
                "provider_state": self.store.provider_state(namespace, provider)}


__all__ = ["ACTIVE_STATES", "RELATIONS", "BusinessStatisticsProjector", "BusinessStatisticsStore", "citation"]
