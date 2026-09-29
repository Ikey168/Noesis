"""Justice-statistic indicators, definitions, observations and vintages (#2218, CJ02, CJ04-CJ06, CJ10).

A statistics release from the FBI Crime Data Explorer, data.police.uk or
Eurostat is one **vintage** of one dataset selection: its observations (place,
period, value, unit, flags as published, suppression, reporting coverage),
the **definitions** they are counted under (UCR/NIBRS, ICCS or the publisher's
category, each with its text, source and validity; a changed definition is a
new definition revision) and the source's **coverage notes** (reporting
coverage, location anonymisation, national-definition footnotes, ESMS
comparability sections) attached to the places they cover. Re-reading an
unchanged release adds nothing; a re-released or revised selection is a new
vintage and earlier vintages stay queryable.

Answers follow :mod:`src.kb.demographics_comparability`: each source's series
side by side with definition, unit, flags, coverage and vintage; gaps and
suppressed values are explicit. **Comparability notes** (reviewable,
reversible) and source-captured notes are the only basis on which series of
different jurisdictions are aligned; without a note for a pair, the
comparison is refused. Nothing is merged, averaged, ranked, rated or scored.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timezone
from itertools import combinations
from typing import Any

from src.kb.courts_justice import (
    READ_SCOPE,
    REVIEW_SCOPE,
    STATISTICS_ANSWER_CONTRACT,
    WRITE_SCOPE,
    CourtsJusticeError,
    authorize,
    canonical,
    day,
    digest,
    load,
    table_exists,
)

NOTE_CONTRACT = "noesis-justice-comparability-note-v1"
RELATIONS = ("same_concept_different_definition", "different_reporting_coverage", "classification_mapping",
             "not_comparable")
ACTIVE_STATES = ("proposed", "accepted")
NOTICE = ("Official statistics as each publisher released them, side by side; no ranking, safety rating, risk "
          "score or merged value is produced.")
_DDL = """
CREATE TABLE IF NOT EXISTS justice_definitions (
  definition_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, provider TEXT NOT NULL, classification TEXT NOT NULL,
  code TEXT NOT NULL, label TEXT, text TEXT NOT NULL, source_url TEXT NOT NULL, in_force_from DATE,
  in_force_to DATE, content_sha256 TEXT NOT NULL, revision_no INTEGER NOT NULL, first_vintage_id TEXT NOT NULL,
  observed_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS justice_vintages (
  vintage_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, provider TEXT NOT NULL, source_id TEXT,
  record_key TEXT NOT NULL, dataset_key TEXT NOT NULL, classification TEXT, release_label TEXT NOT NULL,
  released_at DATE, content_sha256 TEXT NOT NULL, vintage_no INTEGER NOT NULL, run_id TEXT NOT NULL,
  observed_at_ms BIGINT NOT NULL, evidence_origin TEXT, locator TEXT NOT NULL, request_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS justice_observations (
  vintage_id TEXT NOT NULL, ordinal INTEGER NOT NULL, series_key TEXT NOT NULL, indicator TEXT NOT NULL,
  measure TEXT, place_scheme TEXT NOT NULL, place_code TEXT NOT NULL, place_label TEXT, period TEXT NOT NULL,
  value DOUBLE, unit TEXT, flags_json TEXT NOT NULL, suppressed BOOLEAN NOT NULL, coverage_json TEXT NOT NULL,
  definition_id TEXT, extra_json TEXT NOT NULL, PRIMARY KEY(vintage_id, ordinal)
);
CREATE TABLE IF NOT EXISTS justice_coverage_notes (
  vintage_id TEXT NOT NULL, ordinal INTEGER NOT NULL, kind TEXT NOT NULL, place_codes_json TEXT NOT NULL,
  text TEXT NOT NULL, section TEXT, source_url TEXT, content_sha256 TEXT, PRIMARY KEY(vintage_id, ordinal)
);
CREATE TABLE IF NOT EXISTS justice_comparability_notes (
  namespace TEXT NOT NULL, note_id TEXT NOT NULL, pair_key TEXT NOT NULL, left_json TEXT NOT NULL,
  right_json TEXT NOT NULL, relation TEXT NOT NULL, statement TEXT NOT NULL, cited_json TEXT NOT NULL,
  state TEXT NOT NULL, history_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, note_id)
);
CREATE TABLE IF NOT EXISTS justice_receipts (
  namespace TEXT NOT NULL, run_id TEXT NOT NULL, source_id TEXT NOT NULL, unit_index INTEGER NOT NULL,
  receipt_json TEXT NOT NULL, PRIMARY KEY(namespace, run_id, source_id, unit_index)
);
"""


def place_key(scheme: str, code: str) -> str:
    return f"{scheme}:{code}"


def split_place(value: str) -> tuple[str, str]:
    scheme, _, code = str(value or "").partition(":")
    if not scheme or not code:
        raise CourtsJusticeError("invalid_request", "a place is '<scheme>:<code>' (us-state:CA, fbi-ori:..., "
                                                    "police-uk-neighbourhood:force/id, eurostat-geo:DE) or a place_id")
    return scheme, code


def pair_key(left: Mapping[str, str], right: Mapping[str, str]) -> str:
    a, b = sorted([canonical({"series": left["series"]}), canonical({"series": right["series"]})])
    return digest([a, b])[:32]


class JusticeStatisticsStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    # ------------------------------------------------------------------ projection

    def _definition(self, namespace: str, provider: str, item: Mapping[str, Any], vintage_id: str,
                    observed: int) -> str:
        body = {k: item.get(k) for k in ("label", "text", "source_url", "in_force_from", "in_force_to")}
        content = digest(body)
        rows = self.conn.execute("SELECT definition_id, content_sha256, revision_no FROM justice_definitions WHERE "
                                 "namespace=? AND provider=? AND classification=? AND code=? ORDER BY revision_no",
                                 [namespace, provider, item["classification"], item["code"]]).fetchall()
        for definition_id, sha, _ in rows:
            if sha == content:
                return definition_id
        definition_id = "justice-definition:" + digest([namespace, provider, item["classification"], item["code"],
                                                        content])[:24]
        self.conn.execute("INSERT INTO justice_definitions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          [definition_id, namespace, provider, item["classification"], item["code"],
                           item.get("label"), item["text"], item["source_url"], item.get("in_force_from"),
                           item.get("in_force_to"), content, (rows[-1][2] + 1) if rows else 1, vintage_id, observed])
        return definition_id

    def _release(self, namespace: str, record: Mapping[str, Any], *, run_id: str, source_id: str | None,
                 observed: int) -> bool:
        fields = dict(record["fields"])
        content = digest({k: fields.get(k) for k in ("definitions", "observations", "coverage_notes")})
        rows = self.conn.execute("SELECT content_sha256, vintage_no FROM justice_vintages WHERE namespace=? AND "
                                 "record_key=? ORDER BY vintage_no", [namespace, record["record_key"]]).fetchall()
        if any(r[0] == content for r in rows):
            return False
        vintage_id = "justice-vintage:" + digest([namespace, record["record_key"], content])[:24]
        release = dict(fields.get("release") or {})
        self.conn.execute("INSERT INTO justice_vintages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          [vintage_id, namespace, record["provider"], source_id, record["record_key"],
                           fields["dataset_key"], fields.get("classification"), str(release.get("label")),
                           day(release.get("released_at")), content, (rows[-1][1] + 1) if rows else 1, run_id,
                           observed, record.get("evidence_origin"), record["locator"],
                           canonical(fields.get("request") or {})])
        definitions = {d["code"]: self._definition(namespace, record["provider"], d, vintage_id, observed)
                       for d in fields.get("definitions") or []}
        for ordinal, obs in enumerate(fields.get("observations") or []):
            if obs["suppressed"] != (obs["value"] is None):
                raise CourtsJusticeError("invalid_record", "a suppressed observation is one without a value")
            extra = {k: v for k, v in obs.items() if k not in {
                "series_key", "indicator", "measure", "place", "period", "value", "unit", "flags", "suppressed",
                "coverage", "definition_code"}}
            self.conn.execute("INSERT INTO justice_observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                              [vintage_id, ordinal, obs["series_key"], obs["indicator"], obs.get("measure"),
                               obs["place"]["scheme"], obs["place"]["code"], obs["place"].get("label"),
                               str(obs["period"]), obs["value"], obs.get("unit"), canonical(obs.get("flags") or []),
                               bool(obs["suppressed"]), canonical(obs.get("coverage") or {}),
                               definitions.get(obs.get("definition_code")), canonical(extra)])
        for ordinal, note in enumerate(fields.get("coverage_notes") or []):
            self.conn.execute("INSERT INTO justice_coverage_notes VALUES (?,?,?,?,?,?,?,?)",
                              [vintage_id, ordinal, note["kind"], canonical(sorted(note.get("covers") or [])),
                               note["text"], note.get("section"), note.get("source_url"), note.get("content_sha256")])
        return True

    def project(self, namespace: str, records: Iterable[Mapping[str, Any]], *, run_id: str, source_id: str | None,
                receipt: Mapping[str, Any] | None = None, observed_at_ms: int | None = None) -> dict[str, int]:
        observed = int(observed_at_ms if observed_at_ms is not None else self.now())
        counts = {"vintages": 0, "unchanged": 0}
        self.conn.execute("BEGIN")
        try:
            for record in records:
                if record["record_kind"] != "statistics-release":
                    raise CourtsJusticeError("invalid_record", "not a statistics-release record")
                changed = self._release(namespace, record, run_id=run_id, source_id=source_id, observed=observed)
                counts["vintages" if changed else "unchanged"] += 1
            if receipt and source_id:
                self.conn.execute("INSERT OR REPLACE INTO justice_receipts VALUES (?,?,?,?,?)",
                                  [namespace, run_id, source_id, int(receipt.get("unit_index") or 0),
                                   canonical(dict(receipt))])
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return counts

    # ------------------------------------------------------------------ reads

    def receipts(self, namespace: str, run_id: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        authorize(namespace, scopes, READ_SCOPE)
        rows = self.conn.execute("SELECT source_id, unit_index, receipt_json FROM justice_receipts WHERE "
                                 "namespace=? AND run_id=? ORDER BY source_id, unit_index",
                                 [namespace, run_id]).fetchall()
        return [{"source_id": r[0], "unit_index": r[1], **load(r[2], {})} for r in rows]

    _VINTAGE = ("vintage_id", "provider", "source_id", "record_key", "dataset_key", "classification",
                "release_label", "released_at", "vintage_no", "run_id", "observed_at_ms", "evidence_origin",
                "locator")

    def vintages(self, namespace: str, *, record_key: str | None = None) -> list[dict[str, Any]]:
        rows = self.conn.execute(f"SELECT {', '.join(self._VINTAGE)} FROM justice_vintages WHERE namespace=? AND "
                                 "(? IS NULL OR record_key=?) ORDER BY record_key, vintage_no",
                                 [namespace, record_key, record_key]).fetchall()
        return [{**dict(zip(self._VINTAGE, r)), "released_at": r[7] and str(r[7])} for r in rows]

    def definition(self, definition_id: str | None) -> dict[str, Any] | None:
        if not definition_id:
            return None
        row = self.conn.execute("SELECT definition_id, provider, classification, code, label, text, source_url, "
                                "in_force_from, in_force_to, revision_no FROM justice_definitions WHERE "
                                "definition_id=?", [definition_id]).fetchone()
        if row is None:
            return None
        return {"definition_id": row[0], "provider": row[1], "classification": row[2], "code": row[3],
                "label": row[4], "text": row[5], "source_url": row[6], "in_force_from": row[7] and str(row[7]),
                "in_force_to": row[8] and str(row[8]), "revision_no": row[9]}

    def definition_revisions(self, namespace: str, provider: str, classification: str, code: str
                             ) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT definition_id FROM justice_definitions WHERE namespace=? AND provider=? "
                                 "AND classification=? AND code=? ORDER BY revision_no",
                                 [namespace, provider, classification, code]).fetchall()
        return [self.definition(r[0]) for r in rows]

    @staticmethod
    def _vintage_day(vintage: Mapping[str, Any]) -> str:
        return vintage["released_at"] or datetime.fromtimestamp(vintage["observed_at_ms"] / 1000,
                                                                tz=timezone.utc).date().isoformat()

    def vintage_as_of(self, namespace: str, record_key: str, as_of: str | None) -> dict[str, Any] | None:
        """The vintage released on or before ``as_of`` (the latest vintage without a date)."""
        vintages = self.vintages(namespace, record_key=record_key)
        if as_of:
            vintages = [v for v in vintages if self._vintage_day(v) <= str(as_of)[:10]]
        return max(vintages, key=lambda v: v["vintage_no"]) if vintages else None

    def place_codes(self, namespace: str) -> list[tuple[str, str, str | None]]:
        rows = self.conn.execute(
            "SELECT DISTINCT o.place_scheme, o.place_code, o.place_label FROM justice_observations o JOIN "
            "justice_vintages v USING(vintage_id) WHERE v.namespace=? ORDER BY 1, 2", [namespace]).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

    def _notes_for(self, vintage_id: str, code: str) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT kind, place_codes_json, text, section, source_url, content_sha256 FROM "
                                 "justice_coverage_notes WHERE vintage_id=? ORDER BY ordinal", [vintage_id]).fetchall()
        return [{"kind": r[0], "covers": load(r[1], []), "text": r[2], "section": r[3], "source_url": r[4],
                 "content_sha256": r[5], "basis": "source-captured, quoted verbatim"}
                for r in rows if code in load(r[1], [])]

    def _series(self, namespace: str, scheme: str, code: str, *, as_of: str | None, indicator: str | None,
                period_from: str | None, period_to: str | None) -> list[dict[str, Any]]:
        keys = [r[0] for r in self.conn.execute(
            "SELECT DISTINCT v.record_key FROM justice_vintages v JOIN justice_observations o USING(vintage_id) "
            "WHERE v.namespace=? AND o.place_scheme=? AND o.place_code=? ORDER BY 1",
            [namespace, scheme, code]).fetchall()]
        columns = []
        for record_key in keys:
            vintage = self.vintage_as_of(namespace, record_key, as_of)
            later = [v for v in self.vintages(namespace, record_key=record_key)
                     if vintage and v["vintage_no"] > vintage["vintage_no"]]
            if vintage is None:
                columns.append({"record_key": record_key, "status": "no_vintage_released_by_as_of",
                                "observations": []})
                continue
            rows = self.conn.execute(
                "SELECT series_key, indicator, measure, period, value, unit, flags_json, suppressed, coverage_json, "
                "definition_id, extra_json FROM justice_observations WHERE vintage_id=? AND place_scheme=? AND "
                "place_code=? ORDER BY series_key, period", [vintage["vintage_id"], scheme, code]).fetchall()
            all_periods = sorted({r[3] for r in rows})
            by_series: dict[str, dict[str, Any]] = {}
            for r in rows:
                if indicator and indicator.casefold() not in r[1].casefold():
                    continue
                if (period_from and r[3] < period_from) or (period_to and r[3] > period_to):
                    continue
                series = by_series.setdefault(r[0], {
                    "series_key": r[0], "indicator": r[1], "measure": r[2], "unit": r[5],
                    "definition": self.definition(r[9]), "observations": []})
                series["observations"].append({
                    "period": r[3], "value": r[4], "flags": load(r[6], []), "suppressed": bool(r[7]),
                    "coverage": load(r[8], {}), **load(r[10], {}),
                    **({"note": "value suppressed or not published; no value is imputed"} if r[7] else {})})
            for series in by_series.values():
                have = {o["period"] for o in series["observations"]}
                wanted = [p for p in all_periods if (not period_from or p >= period_from)
                          and (not period_to or p <= period_to)]
                series["gaps"] = [p for p in wanted if p not in have]
            columns.append({
                "record_key": record_key, "provider": vintage["provider"], "dataset_key": vintage["dataset_key"],
                "classification": vintage["classification"], "status": "answered" if by_series else "no_matching_series",
                "vintage": {"vintage_id": vintage["vintage_id"], "vintage_no": vintage["vintage_no"],
                            "release_label": vintage["release_label"], "released_at": vintage["released_at"],
                            "source_id": vintage["source_id"], "retrieved_at_ms": vintage["observed_at_ms"],
                            "evidence_origin": vintage["evidence_origin"], "locator": vintage["locator"],
                            "later_vintages": [v["vintage_no"] for v in later]},
                "coverage_notes": self._notes_for(vintage["vintage_id"], code),
                "series": [by_series[k] for k in sorted(by_series)]})
        return columns

    def statistics_for_place(self, namespace: str, place: str, *, scopes: Iterable[str], as_of: str | None = None,
                             indicator: str | None = None, period_from: str | None = None,
                             period_to: str | None = None, geo_namespace: str | None = None) -> dict[str, Any]:
        """Each source's official statistics for one place with definitions, vintages, flags and coverage notes.

        ``place`` is a published code (``eurostat-geo:DE``) or a geospatial ``place_id`` reached through a resolved
        or accepted place mapping (CJ07)."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        codes, mapping = self._codes_for(namespace, place, scopes, geo_namespace)
        columns = []
        for scheme, code in codes:
            for column in self._series(namespace, scheme, code, as_of=as_of, indicator=indicator,
                                       period_from=period_from, period_to=period_to):
                columns.append({"place": {"scheme": scheme, "code": code}, **column})
        answered = [c for c in columns if c["status"] == "answered"]
        return {"contract": STATISTICS_ANSWER_CONTRACT, "namespace": namespace, "place": place,
                "place_mapping": mapping, "as_of": as_of, "indicator": indicator,
                "period": {"from": period_from, "to": period_to},
                "status": "answered" if answered else "no_statistics_on_record", "sources": columns,
                "notice": NOTICE}

    def _codes_for(self, namespace, place, scopes, geo_namespace) -> tuple[list[tuple[str, str]], dict[str, Any]]:
        if str(place).startswith("place:") or str(place).startswith("geospatial-place:") or ":" not in str(place):
            from src.kb.courts_justice_identity import JusticePlaces

            resolutions = JusticePlaces(self.conn, initialize=False, now=self.now).codes_for_place(
                namespace, str(place), scopes=scopes)
            return [(r["scheme"], r["code"]) for r in resolutions], {
                "basis": "resolved or accepted place mappings (published code to place)", "mappings": resolutions}
        scheme, code = split_place(place)
        return [(scheme, code)], {"basis": "published code as stated by the source"}

    # ------------------------------------------------------------------ comparability (CJ10)

    def record_note(self, namespace: str, left_series: str, right_series: str, relation: str, statement: str,
                    cited: Sequence[Mapping[str, Any]], *, principal_id: str, scopes: Iterable[str]) -> dict[str, Any]:
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        if relation not in RELATIONS:
            raise CourtsJusticeError("invalid_relation", f"relation is one of {RELATIONS}")
        if not str(statement or "").strip() or not cited or any(not str(dict(c).get("source_url") or dict(c).get(
                "definition_id") or "").strip() for c in cited):
            raise CourtsJusticeError("invalid_request", "a comparability note states its reason and cites the "
                                                        "definitions or source texts it rests on")
        left, right = {"series": left_series}, {"series": right_series}
        key = pair_key(left, right)
        note_id = "justice-comparability:" + digest([namespace, key, relation, statement.strip()])[:24]
        now = self.now()
        self.conn.execute("INSERT OR IGNORE INTO justice_comparability_notes VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                          [namespace, note_id, key, canonical(left), canonical(right), relation, statement.strip(),
                           canonical([dict(c) for c in cited]), "proposed",
                           canonical([{"state": "proposed", "by": principal_id, "at_ms": now}]), principal_id, now])
        return self.note(namespace, note_id)

    def note(self, namespace: str, note_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT note_id, pair_key, left_json, right_json, relation, statement, cited_json, "
                                "state, history_json, created_by FROM justice_comparability_notes WHERE namespace=? "
                                "AND note_id=?", [namespace, note_id]).fetchone()
        if row is None:
            raise CourtsJusticeError("not_found", "no comparability note with that id")
        return {"contract": NOTE_CONTRACT, "note_id": row[0], "pair_key": row[1], "left": load(row[2], {}),
                "right": load(row[3], {}), "relation": row[4], "statement": row[5], "cited": load(row[6], []),
                "state": row[7], "history": load(row[8], []), "created_by": row[9]}

    def review_note(self, namespace: str, note_id: str, decision: str, reason: str, *, principal_id: str,
                    scopes: Iterable[str]) -> dict[str, Any]:
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        note = self.note(namespace, note_id)
        if decision not in {"accept", "reject"} or not str(reason or "").strip():
            raise CourtsJusticeError("invalid_decision", "accept or reject with a reason")
        if note["state"] != "proposed":
            raise CourtsJusticeError("invalid_state", f"note is {note['state']}")
        if principal_id == note["created_by"]:
            raise CourtsJusticeError("self_review", "a comparability note is reviewed by someone other than its author")
        return self._transition(namespace, note, "accepted" if decision == "accept" else "rejected", principal_id,
                                reason)

    def revert_note(self, namespace: str, note_id: str, reason: str, *, principal_id: str,
                    scopes: Iterable[str]) -> dict[str, Any]:
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        note = self.note(namespace, note_id)
        if note["state"] not in {"accepted", "rejected"} or not str(reason or "").strip():
            raise CourtsJusticeError("invalid_state", "only a reviewed note is reverted, with a reason")
        return self._transition(namespace, note, "reverted", principal_id, reason)

    def _transition(self, namespace, note, state, principal_id, reason):
        history = note["history"] + [{"state": state, "by": principal_id, "reason": reason.strip(),
                                      "at_ms": self.now()}]
        self.conn.execute("UPDATE justice_comparability_notes SET state=?, history_json=? WHERE namespace=? AND "
                          "note_id=?", [state, canonical(history), namespace, note["note_id"]])
        return self.note(namespace, note["note_id"])

    def _pair_notes(self, namespace: str, left: Mapping[str, Any], right: Mapping[str, Any]) -> list[dict[str, Any]]:
        notes = []
        if table_exists(self.conn, "justice_comparability_notes"):
            key = pair_key({"series": left["series_key"]}, {"series": right["series_key"]})
            for (note_id,) in self.conn.execute("SELECT note_id FROM justice_comparability_notes WHERE namespace=? "
                                                "AND pair_key=? ORDER BY created_at_ms, note_id",
                                                [namespace, key]).fetchall():
                note = self.note(namespace, note_id)
                if note["state"] in ACTIVE_STATES:
                    notes.append({k: note[k] for k in ("note_id", "relation", "statement", "state", "cited")}
                                 | {"basis": "reviewable comparability note"})
        # A note the source itself published (ESMS comparability) that covers both places qualifies the pair.
        for column_note in left["coverage_notes"]:
            if column_note["kind"] == "esms_comparability" and right["place"]["code"] in column_note["covers"] \
                    and left["provider"] == right["provider"]:
                notes.append({"relation": "source_comparability_note", "statement": column_note["text"],
                              "cited": [{"source_url": column_note["source_url"], "section": column_note["section"]}],
                              "basis": column_note["basis"]})
        return notes

    def compare_places(self, namespace: str, places: Sequence[str], *, scopes: Iterable[str],
                       indicator: str | None = None, as_of: str | None = None, period_from: str | None = None,
                       period_to: str | None = None) -> dict[str, Any]:
        """Series of several jurisdictions, each separately, with the comparability notes of every pair.

        An aligned view is produced only when every cross-jurisdiction pair has a comparability note; otherwise
        the comparison is refused and each series is still returned on its own. Nothing is ranked."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if len(places) < 2:
            raise CourtsJusticeError("invalid_request", "compare at least two places")
        answers = [self.statistics_for_place(namespace, p, scopes=scopes, as_of=as_of, indicator=indicator,
                                             period_from=period_from, period_to=period_to) for p in places]
        series = []
        for answer in answers:
            for column in answer["sources"]:
                for item in column.get("series") or []:
                    series.append({"place": column["place"], "provider": column["provider"],
                                   "series_key": item["series_key"], "unit": item["unit"],
                                   "definition": item["definition"], "vintage": column["vintage"],
                                   "coverage_notes": column["coverage_notes"], "observations": item["observations"],
                                   "gaps": item["gaps"]})
        pairs, missing = [], []
        for left, right in combinations(series, 2):
            if left["place"] == right["place"]:
                continue
            notes = self._pair_notes(namespace, left, right) or self._pair_notes(namespace, right, left)
            entry = {"left": left["series_key"], "right": right["series_key"],
                     "comparability": notes[-1]["relation"] if notes else "comparability_unknown", "notes": notes}
            if left["unit"] != right["unit"]:
                entry["unit_difference"] = [left["unit"], right["unit"]]
            if (left["definition"] or {}).get("definition_id") != (right["definition"] or {}).get("definition_id"):
                entry["definition_difference"] = [(left["definition"] or {}).get("code"),
                                                  (right["definition"] or {}).get("code")]
            pairs.append(entry)
            if not notes:
                missing.append([left["series_key"], right["series_key"]])
        if not pairs:
            comparison = {"status": "no_cross_jurisdiction_pairs"}
        elif missing:
            comparison = {"status": "refused_no_comparability_note", "missing_pairs": missing,
                          "reason": "series of different jurisdictions are not compared without a stated "
                                    "comparability note; each series is returned separately"}
        else:
            periods = sorted({o["period"] for s in series for o in s["observations"]})
            comparison = {"status": "qualified_by_notes", "aligned_by_period": [
                {"period": p, "values": [{"series_key": s["series_key"], "place": s["place"], "unit": s["unit"],
                                          "value": next((o["value"] for o in s["observations"] if o["period"] == p),
                                                        None)} for s in series]} for p in periods],
                "note": "values side by side in request order; no ranking, ratio or merged figure"}
        return {"contract": STATISTICS_ANSWER_CONTRACT, "namespace": namespace, "places": list(places),
                "as_of": as_of, "series": series, "pairs": pairs, "comparison": comparison, "notice": NOTICE}
