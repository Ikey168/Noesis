"""Append-only store for income, poverty and inequality series, definitions and vintages (IP02, #2592).

Follows the :mod:`src.kb.entity_history` pattern (immutable rows, append-only history) and the labour and
demographics series stores. Tables (all ``income_*``, namespace-scoped):

* ``income_releases`` - one row per acquired publication: provider, declared document and its document key, release
  label and clock (``pip_release_version``, ``provider_last_update``, ``declared_release`` or ``retrieval_time``),
  retrieval clock, PPP round and revision, dataflow version, digests, evidence origin, run id;
* ``income_series`` - one row per series key (see :func:`src.ingestion.income_distribution_sources.series_key`);
* ``income_definitions`` - definition revisions per definition key; a changed definition is a new revision;
* ``income_vintages`` - one row per release of a series, never overwritten: release and retrieval clocks, definition
  revision, PPP round, dataflow version, recorded changes and the Economics vintage it lives in;
* ``income_observations`` - values as published per vintage and reference year (flags, estimation type, survey and
  income reference years);
* ``income_release_members`` - which series (and vintage) each release stated, so a complete release that no longer
  states a series records a ``removed_by_source`` vintage instead of a deletion;
* ``income_comparability`` - source-stated breaks and recorded comparability notes, cited;
* ``income_receipts`` - one receipt per applied release or failure (provider state is read from them).

As-of reads select the vintage released on or before the requested instant (release clock), and say which.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from decimal import Decimal
from typing import Any

from src.ingestion.income_distribution_sources import LIVE_VERIFICATION, series_key
from src.kb.income_distribution_records import (
    ANSWER_CONTRACT,
    ECONOMIC_DOMAIN,
    READ_SCOPE,
    WRITE_SCOPE,
    IncomeError,
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
CREATE TABLE IF NOT EXISTS income_releases (
  namespace TEXT NOT NULL, release_id TEXT NOT NULL, provider TEXT NOT NULL, source_id TEXT, format TEXT NOT NULL,
  document_key TEXT NOT NULL, document_json TEXT NOT NULL, release_label TEXT, published_on TEXT, published_at TEXT,
  release_basis TEXT NOT NULL, release_at_ms BIGINT NOT NULL, retrieved_at_ms BIGINT NOT NULL, ppp_json TEXT,
  dataflow_version TEXT, file_sha256 TEXT NOT NULL, content_sha256 TEXT NOT NULL, item_count INTEGER NOT NULL,
  complete BOOLEAN NOT NULL, evidence_origin TEXT NOT NULL, url TEXT, run_id TEXT NOT NULL, recorded_by TEXT,
  sequence INTEGER NOT NULL, PRIMARY KEY(namespace, release_id)
);
CREATE TABLE IF NOT EXISTS income_series (
  namespace TEXT NOT NULL, series_id TEXT NOT NULL, provider TEXT NOT NULL, native_key TEXT NOT NULL,
  key_json TEXT NOT NULL, indicator_json TEXT NOT NULL, area_json TEXT NOT NULL, unit_json TEXT NOT NULL,
  frequency TEXT NOT NULL, document_key TEXT NOT NULL, economic_indicator_id TEXT NOT NULL,
  first_release_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, series_id)
);
CREATE TABLE IF NOT EXISTS income_definitions (
  namespace TEXT NOT NULL, definition_id TEXT NOT NULL, definition_key TEXT NOT NULL, revision INTEGER NOT NULL,
  provider TEXT NOT NULL, content_json TEXT NOT NULL, content_hash TEXT NOT NULL, release_id TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, definition_id)
);
CREATE TABLE IF NOT EXISTS income_vintages (
  namespace TEXT NOT NULL, vintage_id TEXT NOT NULL, series_id TEXT NOT NULL, release_id TEXT NOT NULL,
  sequence INTEGER NOT NULL, status TEXT NOT NULL, release_at_ms BIGINT NOT NULL, release_basis TEXT NOT NULL,
  retrieved_at_ms BIGINT NOT NULL, economic_as_of BIGINT, content_hash TEXT NOT NULL, definition_id TEXT,
  ppp_json TEXT, dataflow_version TEXT, release_label TEXT, previous_vintage_id TEXT, changes_json TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, vintage_id)
);
CREATE TABLE IF NOT EXISTS income_observations (
  namespace TEXT NOT NULL, vintage_id TEXT NOT NULL, period TEXT NOT NULL, value_text TEXT, value TEXT,
  status TEXT NOT NULL, estimation_type TEXT NOT NULL, survey_year TEXT, income_reference_year TEXT,
  welfare_type TEXT, flags_json TEXT NOT NULL, attributes_json TEXT NOT NULL, PRIMARY KEY(namespace, vintage_id, period)
);
CREATE TABLE IF NOT EXISTS income_release_members (
  namespace TEXT NOT NULL, release_id TEXT NOT NULL, series_id TEXT NOT NULL, vintage_id TEXT NOT NULL,
  status TEXT NOT NULL, PRIMARY KEY(namespace, release_id, series_id)
);
CREATE TABLE IF NOT EXISTS income_comparability (
  namespace TEXT NOT NULL, note_id TEXT NOT NULL, series_id TEXT NOT NULL, other_series_id TEXT,
  relation TEXT NOT NULL, statement TEXT NOT NULL, periods_json TEXT NOT NULL, cited_json TEXT NOT NULL,
  origin TEXT NOT NULL, state TEXT NOT NULL, history_json TEXT NOT NULL, created_by TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, note_id)
);
CREATE TABLE IF NOT EXISTS income_receipts (
  namespace TEXT NOT NULL, receipt_id TEXT NOT NULL, run_id TEXT NOT NULL, source_id TEXT, provider TEXT NOT NULL,
  outcome TEXT NOT NULL, execution TEXT NOT NULL, detail_json TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, receipt_id)
);
"""
RELATIONS = ("break_in_series", "source_note", "different_definition", "not_comparable", "comparable")
NOTE_STATES = ("source-stated", "proposed", "accepted", "rejected", "reverted")
_SERIES_COLUMNS = ("series_id, provider, native_key, key_json, indicator_json, area_json, unit_json, frequency, "
                   "document_key, economic_indicator_id, first_release_id")
_VINTAGE_COLUMNS = ("vintage_id, series_id, release_id, sequence, status, release_at_ms, release_basis, retrieved_at_ms, "
                    "economic_as_of, content_hash, definition_id, ppp_json, dataflow_version, release_label, "
                    "previous_vintage_id, changes_json")


def _content(observations: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    keys = ("period", "value_text", "value", "status", "estimation_type", "survey_year", "income_reference_year",
            "welfare_type", "flags")
    return [{k: o.get(k) for k in keys} for o in sorted(observations, key=lambda o: o["period"])]


def citation(series: Mapping[str, Any], vintage: Mapping[str, Any], release: Mapping[str, Any]) -> dict[str, Any]:
    """What every answer cites for one value set: source, record revision (vintage) and as-of time."""
    return {
        "provider": series["provider"], "source_id": release.get("source_id"), "series_id": series["series_id"],
        "native_key": series["native_key"], "vintage_id": vintage["vintage_id"], "release_id": release["release_id"],
        "release_label": release.get("release_label"), "release_basis": release["release_basis"],
        "as_of": vintage["release_at"], "retrieved_at": vintage["retrieved_at"], "url": release.get("url"),
        "evidence_origin": release["evidence_origin"],
        "live_verification": LIVE_VERIFICATION[series["provider"]]["status"],
    }


class IncomeDistributionStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def ready(self) -> bool:
        return table_exists(self.conn, "income_vintages")

    # ------------------------------------------------------------------ writes

    def apply_release(self, namespace: str, header: Mapping[str, Any], items: Sequence[Mapping[str, Any]], *,
                      source_id: str | None, run_id: str, principal_id: str, scopes: Iterable[str],
                      retrieved_at_ms: int | None = None) -> dict[str, Any]:
        """Append one release: series, definition revisions, vintages and removals; never an overwrite."""
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        retrieved = int(retrieved_at_ms if retrieved_at_ms is not None else self.now())
        valid, rejected = [], []
        for index, item in enumerate(items):
            try:
                check_item(item)
            except IncomeError as exc:
                rejected.append({"index": index, "native_key": dict(item).get("native_key"), **exc.as_dict()})
                continue
            valid.append(dict(item))
        clock, basis = self._clock(header, retrieved)
        release_id = "inc-release:" + digest([namespace, header["document_key"], header.get("release_label"),
                                              header["file_sha256"]])[:24]
        if self.conn.execute("SELECT 1 FROM income_releases WHERE namespace=? AND release_id=?",
                             [namespace, release_id]).fetchone():
            self._receipt(namespace, run_id, source_id, header["provider"], "unchanged", header, retrieved,
                          {"release_id": release_id})
            return {"release_id": release_id, "status": "unchanged", "vintages": 0, "rejected": rejected}
        self.conn.execute("BEGIN")
        try:
            sequence = 1 + int(self.conn.execute(
                "SELECT count(*) FROM income_releases WHERE namespace=? AND document_key=?",
                [namespace, header["document_key"]]).fetchone()[0])
            previous = self.conn.execute(
                "SELECT release_id FROM income_releases WHERE namespace=? AND document_key=? AND complete "
                "ORDER BY sequence DESC LIMIT 1", [namespace, header["document_key"]]).fetchone()
            self.conn.execute(
                "INSERT INTO income_releases VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [namespace, release_id, header["provider"], source_id, header["format"], header["document_key"],
                 canonical(header.get("document") or {}), header.get("release_label"), header.get("published_on"),
                 header.get("published_at"), basis, clock, retrieved, canonical(header.get("ppp")),
                 header.get("dataflow_version"), header["file_sha256"], header["content_sha256"], len(items),
                 bool(header.get("complete", True)) and not rejected, header.get("evidence_origin") or "fixture",
                 header.get("url"), run_id, principal_id, sequence])
            counts = {"new": 0, "unchanged": 0, "removed": 0}
            stated = set()
            for item in valid:
                series_id = self._series(namespace, item, header, release_id)
                stated.add(series_id)
                definition_id = self._definition(namespace, item, release_id)
                vintage_id, status = self._vintage(namespace, series_id, item, header, release_id, clock, basis,
                                                   retrieved, definition_id)
                counts[status] += 1
                self.conn.execute("INSERT INTO income_release_members VALUES (?,?,?,?,?)",
                                  [namespace, release_id, series_id, vintage_id, status])
                self._source_notes(namespace, series_id, item, vintage_id)
            if previous and header.get("complete", True) and not rejected:
                for (series_id,) in self.conn.execute(
                        "SELECT series_id FROM income_release_members WHERE namespace=? AND release_id=? "
                        "AND status <> 'removed' ORDER BY series_id", [namespace, previous[0]]).fetchall():
                    if series_id in stated:
                        continue
                    vintage_id = self._removal(namespace, series_id, header, release_id, clock, basis, retrieved)
                    if vintage_id:
                        counts["removed"] += 1
                        self.conn.execute("INSERT INTO income_release_members VALUES (?,?,?,?,?)",
                                          [namespace, release_id, series_id, vintage_id, "removed"])
            self._receipt(namespace, run_id, source_id, header["provider"], "applied", header, retrieved,
                          {"release_id": release_id, **counts, "rejected": len(rejected)})
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {"release_id": release_id, "status": "applied", "vintages": counts["new"],
                "unchanged_series": counts["unchanged"], "removed_series": counts["removed"], "rejected": rejected}

    @staticmethod
    def _clock(header: Mapping[str, Any], retrieved: int) -> tuple[int, str]:
        if header.get("published_at"):
            return to_ms(header["published_at"]), header.get("release_basis") or "provider_last_update"
        if header.get("published_on"):
            return to_ms(header["published_on"]), header.get("release_basis") or "declared_release"
        return retrieved, "retrieval_time"

    def _series(self, namespace, item, header, release_id) -> str:
        key = series_key(item)
        series_id = f"income:{item['provider']}:" + digest(key)[:20]
        if not self.conn.execute("SELECT 1 FROM income_series WHERE namespace=? AND series_id=?",
                                 [namespace, series_id]).fetchone():
            self.conn.execute("INSERT INTO income_series VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                              [namespace, series_id, item["provider"], item["native_key"], canonical(key),
                               canonical(item["indicator"]), canonical(item["area"]), canonical(item.get("unit") or {}),
                               item.get("frequency") or "annual", header["document_key"],
                               "income-indicator:" + series_id.split(":", 1)[1], release_id, self.now()])
        return series_id

    @staticmethod
    def definition_key(item: Mapping[str, Any]) -> str:
        definition = dict(item["definition"])
        return f"{item['provider']}:" + digest([item["indicator"]["concept"], item["indicator"]["measure"],
                                                definition.get("welfare_concept"), definition.get("equivalence_scale"),
                                                item.get("income_definition"), item.get("methodology"),
                                                item.get("poverty_line"), item.get("ppp_base_year")])[:16]

    def _definition(self, namespace, item, release_id) -> str:
        key = self.definition_key(item)
        content = dict(item["definition"])
        content_hash = digest(content)
        latest = self.conn.execute(
            "SELECT definition_id, content_hash, revision FROM income_definitions WHERE namespace=? AND "
            "definition_key=? ORDER BY revision DESC LIMIT 1", [namespace, key]).fetchone()
        if latest and latest[1] == content_hash:
            return latest[0]
        revision = 1 + (int(latest[2]) if latest else 0)
        definition_id = f"inc-def:{key}:r{revision}"
        self.conn.execute("INSERT INTO income_definitions VALUES (?,?,?,?,?,?,?,?,?)",
                          [namespace, definition_id, key, revision, item["provider"], canonical(content), content_hash,
                           release_id, self.now()])
        return definition_id

    def _changes(self, prior, observations, definition_id, header) -> dict[str, Any]:
        if prior is None:
            return {"new_series": True, "new_periods": sorted(o["period"] for o in observations)}
        before = {o["period"]: o for o in self.observations(prior["namespace"], prior["vintage_id"])}
        after = {o["period"]: o for o in _content(observations)}
        compared = ("value_text", "value", "status", "estimation_type", "flags")
        revised = [{"period": p, "before": {k: before[p].get(k) for k in compared},
                    "after": {k: after[p].get(k) for k in compared}}
                   for p in sorted(set(before) & set(after))
                   if any(before[p].get(k) != after[p].get(k) for k in compared)]
        changes: dict[str, Any] = {
            "new_periods": sorted(set(after) - set(before)),
            "dropped_periods": sorted(set(before) - set(after)),
            "revised": revised,
        }
        ppp_before, ppp_after = prior.get("ppp"), header.get("ppp")
        if ppp_before and ppp_after and ppp_before != ppp_after:
            changes["ppp_revision"] = {"before": ppp_before, "after": ppp_after}
        if prior.get("definition_id") != definition_id or prior.get("dataflow_version") != header.get("dataflow_version"):
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
                prior["definition_id"] == definition_id and prior["ppp"] == header.get("ppp") and \
                prior["dataflow_version"] == header.get("dataflow_version"):
            return prior["vintage_id"], "unchanged"
        same_clock = [v for v in previous if v["release_at_ms"] == clock]
        if same_clock:
            raise IncomeError("vintage_conflict", "the publication changed values without a new release time; the "
                                                  "stored vintage is kept", series_id=series_id)
        if prior is not None and clock < prior["release_at_ms"]:
            raise IncomeError("stale_release", "a release dated before the series' latest vintage is not appended",
                              series_id=series_id)
        changes = self._changes(prior, observations, definition_id, header)
        record = SeriesRecord(
            series_id=series_id, provider=item["provider"], title=str(item["indicator"].get("label")
                                                                      or item["native_key"]),
            frequency=item.get("frequency") or "annual", as_of=int(clock),
            observations=[{"period": o["period"], "value": None if o.get("value") is None
                           else float(Decimal(o["value"]))} for o in sorted(observations, key=lambda o: o["period"])],
            unit=dict(item.get("unit") or {}).get("label") or None, geography=str(item["area"]["code"]),
            source_url=header.get("url"),
            metadata={"provider_release_at_ms": int(clock), "provider_release_time_status": f"income release ({basis})",
                      "acquired_at_ms": max(int(retrieved), int(clock)), "vintage_basis": basis,
                      "source_document_id": release_id},
        )
        semantics = {
            "indicator_id": "income-indicator:" + series_id.split(":", 1)[1],
            "canonical_name": str(item["indicator"].get("label") or item["indicator"]["concept"]),
            "concept": f"income distribution {item['indicator']['concept']} {item['indicator']['measure']} "
                       f"{series_id}",
            "definition": dict(item["definition"]).get("source_text") or item["indicator"]["concept"],
            "seasonal_adjustment": "not_applicable", "price_basis": "not_applicable",
            "provider_code": str(item["native_key"]),
            "provider_definition": dict(item["definition"]).get("source_text") or item["indicator"]["concept"],
            "attributes": {"income_series_id": series_id, "welfare_concept": item["welfare_concept"],
                           "equivalence_scale": item["equivalence_scale"], "poverty_line": item.get("poverty_line"),
                           "contract": "noesis-income-distribution-record-v2"},
        }
        try:
            economic = register_series(self.conn, record, semantics=semantics, domain=ECONOMIC_DOMAIN)
        except EconomicModelError as exc:
            raise IncomeError(exc.code, str(exc)) from exc
        vintage_id = "inc-vintage:" + digest([namespace, series_id, release_id, content_hash])[:24]
        self.conn.execute(
            "INSERT INTO income_vintages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, vintage_id, series_id, release_id, 1 + len(previous), "published", clock, basis, retrieved,
             economic["vintage"]["as_of"], content_hash, definition_id, canonical(header.get("ppp")),
             header.get("dataflow_version"), header.get("release_label"),
             prior["vintage_id"] if prior else None, canonical(changes), self.now()])
        for obs in observations:
            self.conn.execute(
                "INSERT INTO income_observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [namespace, vintage_id, obs["period"], obs.get("value_text"), obs.get("value"), obs["status"],
                 obs["estimation_type"], obs.get("survey_year"), obs.get("income_reference_year"),
                 obs.get("welfare_type"), canonical(dict(obs.get("flags") or {})),
                 canonical({**dict(obs.get("attributes") or {}),
                            **({"flag_meanings": obs["flag_meanings"]} if obs.get("flag_meanings") else {})})])
        return vintage_id, "new"

    def _removal(self, namespace, series_id, header, release_id, clock, basis, retrieved) -> str | None:
        previous = self.vintage_rows(namespace, series_id)
        prior = previous[-1] if previous else None
        if prior is None or prior["status"] == "removed" or clock <= prior["release_at_ms"]:
            return None
        vintage_id = "inc-vintage:" + digest([namespace, series_id, release_id, "removed"])[:24]
        changes = {"removed_by_source": {"release_label": header.get("release_label"),
                                         "statement": "the source's complete release of this document no longer "
                                                      "states the series; earlier vintages stay queryable"}}
        self.conn.execute(
            "INSERT INTO income_vintages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, vintage_id, series_id, release_id, 1 + len(previous), "removed", clock, basis, retrieved, None,
             digest("removed"), prior["definition_id"], canonical(header.get("ppp")), header.get("dataflow_version"),
             header.get("release_label"), prior["vintage_id"], canonical(changes), self.now()])
        return vintage_id

    def _source_notes(self, namespace, series_id, item, vintage_id) -> None:
        for note in item.get("source_notes") or []:
            relation = "break_in_series" if note.get("kind") == "break" else "source_note"
            note_id = "inc-note:" + digest([namespace, series_id, relation, note.get("value"),
                                            sorted(note.get("periods") or [])])[:24]
            self.conn.execute(
                "INSERT INTO income_comparability VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                [namespace, note_id, series_id, None, relation, str(note.get("statement") or note.get("value")),
                 canonical(sorted(note.get("periods") or [])),
                 canonical([{"vintage_id": vintage_id, "attribute": note.get("attribute")}]), "source-stated",
                 "source-stated", canonical([]), "source", self.now()])

    def record_note(self, namespace: str, series_id: str, relation: str, statement: str, *, other_series_id: str | None,
                    periods: Sequence[str] = (), cited: Sequence[Mapping[str, Any]], principal_id: str,
                    scopes: Iterable[str]) -> dict[str, Any]:
        """A reviewer-proposed comparability note between series (cited; nothing is re-harmonised)."""
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        if relation not in RELATIONS:
            raise IncomeError("invalid_note", f"relation is one of {RELATIONS}")
        if not cited:
            raise IncomeError("invalid_note", "a comparability note cites the methodology or release it rests on")
        self.series(namespace, series_id)
        if other_series_id:
            self.series(namespace, other_series_id)
        note_id = "inc-note:" + digest([namespace, series_id, other_series_id, relation, statement])[:24]
        self.conn.execute(
            "INSERT INTO income_comparability VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
            [namespace, note_id, series_id, other_series_id, relation, statement, canonical(sorted(periods)),
             canonical([dict(c) for c in cited]), "recorded", "proposed",
             canonical([{"state": "proposed", "by": principal_id, "at": iso(self.now())}]), principal_id,
             self.now()])
        return self.note(namespace, note_id)

    def review_note(self, namespace: str, note_id: str, decision: str, reason: str, *, principal_id: str,
                    scopes: Iterable[str]) -> dict[str, Any]:
        from src.kb.income_distribution_records import REVIEW_SCOPE

        authorize(namespace, scopes, REVIEW_SCOPE)
        note = self.note(namespace, note_id)
        transitions = {"accept": ("proposed", "accepted"), "reject": ("proposed", "rejected"),
                       "revert": ("accepted", "reverted")}
        if decision not in transitions or note["state"] != transitions[decision][0]:
            raise IncomeError("invalid_transition", f"cannot {decision} a {note['state']} note")
        if decision != "revert" and note["created_by"] == principal_id:
            raise IncomeError("self_review", "a note is reviewed by another principal")
        history = note["history"] + [{"state": transitions[decision][1], "by": principal_id, "reason": reason,
                                      "at": iso(self.now())}]
        self.conn.execute("UPDATE income_comparability SET state=?, history_json=? WHERE namespace=? AND note_id=?",
                          [transitions[decision][1], canonical(history), namespace, note_id])
        return self.note(namespace, note_id)

    def note(self, namespace: str, note_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT note_id, series_id, other_series_id, relation, statement, periods_json, cited_json, origin, state, "
            "history_json, created_by FROM income_comparability WHERE namespace=? AND note_id=?",
            [namespace, note_id]).fetchone()
        if row is None:
            raise IncomeError("not_found", "no such comparability note")
        return {"note_id": row[0], "series_id": row[1], "other_series_id": row[2], "relation": row[3],
                "statement": row[4], "periods": load(row[5], []), "cited": load(row[6], []), "origin": row[7],
                "state": row[8], "history": load(row[9], []), "created_by": row[10]}

    def notes(self, namespace: str, series_id: str) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "income_comparability"):
            return []
        rows = self.conn.execute(
            "SELECT note_id FROM income_comparability WHERE namespace=? AND (series_id=? OR other_series_id=?) "
            "AND state IN ('source-stated', 'accepted', 'proposed') ORDER BY note_id",
            [namespace, series_id, series_id]).fetchall()
        return [self.note(namespace, r[0]) for r in rows]

    def _receipt(self, namespace, run_id, source_id, provider, outcome, header, retrieved, detail):
        body = {"run_id": run_id, "source_id": source_id, "provider": provider, "outcome": outcome,
                "release_label": (header or {}).get("release_label"),
                "file_sha256": (header or {}).get("file_sha256"), "retrieved_at": iso(retrieved), **detail}
        receipt_id = "inc-receipt:" + digest([namespace, body])[:24]
        self.conn.execute("INSERT INTO income_receipts VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                          [namespace, receipt_id, run_id, source_id, provider, outcome,
                           (header or {}).get("evidence_origin") or "fixture", canonical(body), self.now()])
        return receipt_id

    def record_failure(self, namespace: str, provider: str, *, code: str, run_id: str, source_id: str | None,
                       scopes: Iterable[str]) -> dict[str, Any]:
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        self._receipt(namespace, run_id, source_id, provider, "failed", None, self.now(), {"failure_code": code})
        return {"provider": provider, "failure_code": code,
                "effect": "stored vintages unchanged; the source reads as stale, nothing is marked removed"}

    # ------------------------------------------------------------------ reads

    def provider_state(self, namespace: str, provider: str) -> dict[str, Any]:
        if not table_exists(self.conn, "income_receipts"):
            return {"provider": provider, "last_success_ms": None, "stale": True, "reason": "never acquired"}
        rows = self.conn.execute("SELECT outcome, execution, created_at_ms, run_id FROM income_receipts WHERE "
                                 "namespace=? AND provider=? ORDER BY created_at_ms, receipt_id",
                                 [namespace, provider]).fetchall()
        successes = [r for r in rows if r[0] in {"applied", "unchanged"}]
        if not successes:
            return {"provider": provider, "last_success_ms": None, "stale": True, "reason": "never acquired"}
        return {"provider": provider, "last_success_ms": int(successes[-1][2]), "last_execution": successes[-1][1],
                "last_run_id": rows[-1][3], "stale": rows[-1][0] == "failed"}

    def release(self, namespace: str, release_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT release_id, provider, source_id, format, document_key, release_label, published_on, release_basis, "
            "release_at_ms, retrieved_at_ms, ppp_json, dataflow_version, file_sha256, evidence_origin, url, run_id, "
            "complete FROM income_releases WHERE namespace=? AND release_id=?", [namespace, release_id]).fetchone()
        if row is None:
            raise IncomeError("not_found", "no such release")
        keys = ("release_id", "provider", "source_id", "format", "document_key", "release_label", "published_on",
                "release_basis", "release_at_ms", "retrieved_at_ms", "ppp", "dataflow_version", "file_sha256",
                "evidence_origin", "url", "run_id", "complete")
        view = dict(zip(keys, row))
        view["ppp"] = load(view["ppp"], None)
        view["release_at"], view["retrieved_at"] = iso(view["release_at_ms"]), iso(view["retrieved_at_ms"])
        return view

    def releases(self, namespace: str, *, provider: str | None = None) -> list[dict[str, Any]]:
        if not self.ready():
            return []
        rows = self.conn.execute("SELECT release_id FROM income_releases WHERE namespace=? AND (? IS NULL OR "
                                 "provider=?) ORDER BY release_at_ms, release_id",
                                 [namespace, provider, provider]).fetchall()
        return [self.release(namespace, r[0]) for r in rows]

    def _series_view(self, namespace: str, row: Sequence[Any]) -> dict[str, Any]:
        (series_id, provider, native_key, key, indicator, area, unit, frequency, document_key, economic_indicator,
         first_release) = row
        return {"record_type": "series", "series_id": series_id, "provider": provider, "native_key": native_key,
                "key": load(key, {}), "indicator": load(indicator, {}), "area": load(area, {}),
                "unit": load(unit, {}), "frequency": frequency, "document_key": document_key,
                "first_release_id": first_release,
                "economic_series": {"domain": ECONOMIC_DOMAIN, "series_id": series_id,
                                    "indicator_id": economic_indicator}}

    def series(self, namespace: str, series_id: str) -> dict[str, Any]:
        row = self.conn.execute(f"SELECT {_SERIES_COLUMNS} FROM income_series WHERE namespace=? AND series_id=?",
                                [namespace, series_id]).fetchone() if self.ready() else None
        if row is None:
            raise IncomeError("not_found", "no such income series")
        return self._series_view(namespace, row)

    def find_series(self, namespace: str, *, provider: str | None = None, concept: str | None = None,
                    area_codes: Iterable[tuple[str, str]] | None = None) -> list[dict[str, Any]]:
        if not self.ready():
            return []
        rows = self.conn.execute(f"SELECT {_SERIES_COLUMNS} FROM income_series WHERE namespace=? AND (? IS NULL OR "
                                 "provider=?) ORDER BY series_id", [namespace, provider, provider]).fetchall()
        wanted = None if area_codes is None else {(s, str(c)) for s, c in area_codes}
        out = []
        for row in rows:
            view = self._series_view(namespace, row)
            if concept and view["indicator"].get("concept") != concept:
                continue
            if wanted is not None and (view["area"]["scheme"], str(view["area"]["code"])) not in wanted:
                continue
            out.append(view)
        return out

    def vintage_rows(self, namespace: str, series_id: str) -> list[dict[str, Any]]:
        if not self.ready():
            return []
        rows = self.conn.execute(f"SELECT {_VINTAGE_COLUMNS} FROM income_vintages WHERE namespace=? AND series_id=? "
                                 "ORDER BY sequence", [namespace, series_id]).fetchall()
        out = []
        for row in rows:
            keys = ("vintage_id", "series_id", "release_id", "sequence", "status", "release_at_ms", "release_basis",
                    "retrieved_at_ms", "economic_as_of", "content_hash", "definition_id", "ppp", "dataflow_version",
                    "release_label", "previous_vintage_id", "changes")
            view = dict(zip(keys, row))
            view["namespace"] = namespace
            view["ppp"], view["changes"] = load(view["ppp"], None), load(view["changes"], {})
            view["release_at"], view["retrieved_at"] = iso(view["release_at_ms"]), iso(view["retrieved_at_ms"])
            view["economic_vintage_id"] = (f"{series_id}@{view['economic_as_of']}"
                                           if view["economic_as_of"] is not None else None)
            out.append(view)
        return out

    def vintage(self, namespace: str, vintage_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT series_id FROM income_vintages WHERE namespace=? AND vintage_id=?",
                                [namespace, vintage_id]).fetchone()
        if row is None:
            raise IncomeError("not_found", "no such vintage")
        return next(v for v in self.vintage_rows(namespace, row[0]) if v["vintage_id"] == vintage_id)

    def observations(self, namespace: str, vintage_id: str) -> list[dict[str, Any]]:
        vintage = self.conn.execute("SELECT series_id, economic_as_of FROM income_vintages WHERE namespace=? AND "
                                    "vintage_id=?", [namespace, vintage_id]).fetchone()
        if vintage is None:
            return []
        numeric = {r[0]: r[1] for r in self.conn.execute(
            "SELECT period, value FROM dataset_observations WHERE series_id=? AND as_of=?",
            [vintage[0], vintage[1]]).fetchall()} if vintage[1] is not None and \
            table_exists(self.conn, "dataset_observations") else {}
        rows = self.conn.execute(
            "SELECT period, value_text, value, status, estimation_type, survey_year, income_reference_year, "
            "welfare_type, flags_json, attributes_json FROM income_observations WHERE namespace=? AND vintage_id=? "
            "ORDER BY period", [namespace, vintage_id]).fetchall()
        return [{"period": r[0], "value_text": r[1], "value": r[2], "numeric_value": numeric.get(r[0]),
                 "status": r[3], "estimation_type": r[4], "survey_year": r[5], "income_reference_year": r[6],
                 "welfare_type": r[7], "flags": load(r[8], {}), "attributes": load(r[9], {})} for r in rows]

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
                                "FROM income_definitions WHERE namespace=? AND definition_id=?",
                                [namespace, definition_id]).fetchone()
        if row is None:
            return None
        return {"record_type": "definition", "definition_id": row[0], "definition_key": row[1], "revision": row[2],
                "provider": row[3], "content": load(row[4], {}), "release_id": row[5]}

    def values(self, namespace: str, series_id: str, *, as_of_ms: int | None = None,
               vintage_id: str | None = None) -> dict[str, Any]:
        series = self.series(namespace, series_id)
        reason = None
        if vintage_id:
            vintage = next((v for v in self.vintage_rows(namespace, series_id) if v["vintage_id"] == vintage_id), None)
            if vintage is None:
                raise IncomeError("not_found", "vintage does not belong to this series")
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
                "note": "values as published in this vintage; flags, estimation type and survey and income reference "
                        "years as the source states them"}

    def latest_release_ms(self, namespace: str) -> int | None:
        if not self.ready():
            return None
        row = self.conn.execute("SELECT max(release_at_ms) FROM income_releases WHERE namespace=?",
                                [namespace]).fetchone()
        return None if row is None or row[0] is None else int(row[0])

    def receipts(self, namespace: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        authorize(namespace, scopes, READ_SCOPE)
        if not table_exists(self.conn, "income_receipts"):
            return []
        rows = self.conn.execute("SELECT receipt_id, run_id, source_id, provider, outcome, execution, detail_json FROM "
                                 "income_receipts WHERE namespace=? ORDER BY created_at_ms, receipt_id",
                                 [namespace]).fetchall()
        return [{"receipt_id": r[0], "run_id": r[1], "source_id": r[2], "provider": r[3], "outcome": r[4],
                 "execution": r[5], "detail": load(r[6], {})} for r in rows]


class IncomeDistributionProjector:
    """Source-pack runtime projector for ``noesis-income-distribution-record-v2`` pages (one release per page)."""

    @staticmethod
    def scopes_for(namespace: str) -> set[str]:
        return {WRITE_SCOPE, READ_SCOPE, f"namespace:{namespace}:write", f"namespace:{namespace}:read"}

    def __init__(self, conn: Any) -> None:
        self.store = IncomeDistributionStore(conn)

    @staticmethod
    def _namespace(source: Mapping[str, Any]) -> str:
        return str(dict(source.get("income_distribution") or {}).get("namespace") or "global")

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        del manifest, page_receipt
        namespace = self._namespace(source)
        releases: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
        for record in records:
            header = record.get("income_release")
            if not header:
                continue
            releases.setdefault(header["file_sha256"], (dict(header), []))[1].append(dict(record["income_item"]))
        retrieved = max((int(d["ingested_at"]) for d in documents or [] if d.get("ingested_at") is not None),
                        default=None)
        return [self.store.apply_release(namespace, header, items, source_id=source.get("source_id"), run_id=run_id,
                                         principal_id=principal_id, scopes=self.scopes_for(namespace),
                                         retrieved_at_ms=retrieved)
                for header, items in releases.values()]

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del manifest, principal_id
        namespace = self._namespace(source)
        provider = str(dict(source.get("income_distribution") or {}).get("provider"))
        if status != "complete":
            self.store.record_failure(namespace, provider, code="source_run_" + status, run_id=run_id,
                                      source_id=source.get("source_id"), scopes=self.scopes_for(namespace))
        return {"status": status, "provider": provider, "namespace": namespace,
                "provider_state": self.store.provider_state(namespace, provider)}


__all__ = ["RELATIONS", "IncomeDistributionProjector", "IncomeDistributionStore", "citation"]
