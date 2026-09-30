"""Places and indicator definitions across WHO, OECD and Eurostat, aligned by reviewable records (#2215, HS06).

* **Place resolution.** Every series geography code (ISO 3166-1 alpha-3 from WHO and the OECD, Eurostat country
  codes with the EU conventions ``EL`` and ``UK``, NUTS codes) resolves through the Geospatial place store
  (:mod:`src.kb.geospatial`) by the published code carried in a place's source identifiers - never by name or
  nearest match. A code matching no place, or several, stays unresolved with the reason. WHO regions, the WHO global
  code and Eurostat, ECDC and OECD aggregates are recorded as ``aggregate`` and **never resolved to or treated as a
  country**. Resolutions are reviewable (accept, reject, revert); a rejected one is not used. A later evaluation
  that resolves a code differently is a new evaluation; earlier ones stay.
* **Indicator mappings.** Equivalent indicators across sources are aligned by a mapping of kind ``equivalent``,
  ``broader`` or ``narrower`` (the left indicator relative to the right) with evidence, citing both published
  definitions; proposed by a writer, accepted or rejected by a reviewer and revertible.
* **Comparability notes** follow :mod:`src.kb.demographics_comparability`: a typed relation between two indicators
  (``same_concept_different_definition``, ``different_unit_or_denominator``, ``different_coverage``,
  ``not_comparable``) that cites both definitions and the source revisions they were declared with; reviewable and
  revertible, and a reverted note never reactivates an earlier one.

No harmonised, adjusted or combined value is produced and no mapping changes a stored value.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from src.kb.health_capacity import AGGREGATE_SYSTEMS, SCHEME, HealthCapacityError, HealthCapacityStore
from src.kb.surveillance import (
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    authorize,
    canonical,
    digest,
    require_scope,
    table_exists,
)

CONTRACT = "noesis-health-capacity-comparability-v1"
GEO_READ = "knowledge:geospatial:read"
MAPPING_KINDS = ("equivalent", "broader", "narrower")
RELATIONS = (
    "same_concept_different_definition",
    "different_unit_or_denominator",
    "different_coverage",
    "not_comparable",
)
DECISIONS = ("accept", "reject")
ACTIVE_STATES = ("proposed", "accepted")
# Geospatial source-identifier keys a published code is looked up under, per series code system.
PLACE_KEYS = {
    "iso3166-1-alpha3": ("iso3166-1-alpha3",),
    "iso3166-1-alpha2": ("iso3166-1-alpha2",),
    "eu-country": ("eu-country", "iso3166-1-alpha2"),
    "nuts": ("nuts",),
}
# Eurostat's EU country-code conventions against ISO 3166-1 alpha-2 (applied only for the ISO key).
EU_TO_ISO2 = {"EL": "GR", "UK": "GB"}
_DDL = """
CREATE TABLE IF NOT EXISTS health_capacity_place_resolutions (
  namespace TEXT NOT NULL, resolution_id TEXT NOT NULL, geography_system TEXT NOT NULL, geography_code TEXT NOT NULL,
  geo_namespace TEXT NOT NULL, state TEXT NOT NULL, reason TEXT, place_id TEXT, place_revision_id TEXT,
  place_name TEXT, review_state TEXT NOT NULL, history_json TEXT NOT NULL, evaluation_no INTEGER NOT NULL,
  created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, resolution_id)
);
CREATE TABLE IF NOT EXISTS health_capacity_mappings (
  namespace TEXT NOT NULL, mapping_id TEXT NOT NULL, pair_key TEXT NOT NULL, left_json TEXT NOT NULL,
  right_json TEXT NOT NULL, kind TEXT NOT NULL, evidence TEXT NOT NULL, cited_json TEXT NOT NULL, state TEXT NOT NULL,
  history_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, mapping_id)
);
CREATE TABLE IF NOT EXISTS health_capacity_notes (
  namespace TEXT NOT NULL, note_id TEXT NOT NULL, pair_key TEXT NOT NULL, left_json TEXT NOT NULL,
  right_json TEXT NOT NULL, relation TEXT NOT NULL, statement TEXT NOT NULL, cited_json TEXT NOT NULL,
  state TEXT NOT NULL, history_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, note_id)
);
"""


def _ref(value: Mapping[str, Any]) -> dict[str, str]:
    value = dict(value or {})
    provider, code = str(value.get("provider") or "").strip(), str(value.get("source_code") or "").strip()
    if not provider or not code:
        raise HealthCapacityError("invalid_request", "each side names an indicator by provider and source_code")
    return {"provider": provider, "source_code": code}


def pair_key(left: Mapping[str, str], right: Mapping[str, str]) -> str:
    """Order-insensitive: a record on (a, b) is a record on (b, a)."""
    return canonical(sorted([f"{left['provider']}|{left['source_code']}", f"{right['provider']}|{right['source_code']}"]))


def _normalise(code: Any) -> str:
    return str(code if code is not None else "").strip().upper()


class HealthCapacityComparability:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        self.conn = conn
        self.capacity = HealthCapacityStore(conn, initialize=initialize, now=now)
        self.store = self.capacity.series_store
        self.now = self.store.now
        if initialize:
            conn.execute(_DDL)

    # ------------------------------------------------------------------ places

    def _places(self, geo_namespace: str, system: str, code: str) -> list[tuple[str, str, str]]:
        if not table_exists(self.conn, "geospatial_place_revisions"):
            return []
        rows = self.conn.execute(
            "SELECT p.place_id, r.revision_id, r.canonical_name, r.source_ids_json FROM geospatial_places p JOIN "
            "geospatial_place_current c ON c.place_id=p.place_id JOIN geospatial_place_revisions r ON "
            "r.revision_id=c.revision_id WHERE p.namespace IN (?, 'global') ORDER BY p.place_id",
            [geo_namespace],
        ).fetchall()
        found = []
        for place_id, revision_id, name, source_ids in rows:
            ids = {k: _normalise(v) for k, v in json.loads(source_ids or "{}").items()}
            for key in PLACE_KEYS.get(system, ()):
                wanted = EU_TO_ISO2.get(code, code) if (system, key) == ("eu-country", "iso3166-1-alpha2") else code
                if ids.get(key) == wanted:
                    found.append((place_id, revision_id, name))
                    break
        return found

    def _pairs(self, namespace: str) -> list[tuple[str, str]]:
        """Every (code system, code) stated by a series in the namespace (capacity and surveillance alike)."""
        if not table_exists(self.conn, "surveillance_series"):
            return []
        return [tuple(r) for r in self.conn.execute(
            "SELECT DISTINCT geography_system, geography_code FROM surveillance_series WHERE namespace=? "
            "ORDER BY 1, 2", [namespace]).fetchall()]

    _RESOLUTION_KEYS = ("resolution_id", "geography_system", "geography_code", "geo_namespace", "state", "reason",
                        "place_id", "place_revision_id", "place_name", "review_state", "history", "evaluation_no",
                        "created_by", "created_at_ms")

    def _resolution_row(self, namespace: str, where: str, params: list[Any]) -> dict[str, Any] | None:
        row = self.conn.execute(
            f"SELECT {', '.join(k if k != 'history' else 'history_json' for k in self._RESOLUTION_KEYS)} FROM "
            f"health_capacity_place_resolutions WHERE namespace=? AND {where}", [namespace, *params]).fetchone()
        if row is None:
            return None
        view = dict(zip(self._RESOLUTION_KEYS, row))
        view["history"] = json.loads(view["history"])
        view["used"] = view["state"] == "matched" and view["review_state"] != "rejected"
        view["aggregate"] = view["state"] == "aggregate"
        return {"contract": CONTRACT, "record_type": "place-resolution", "namespace": namespace, **view}

    def _latest(self, namespace: str, system: str, code: str) -> dict[str, Any] | None:
        return self._resolution_row(
            namespace, "geography_system=? AND geography_code=? ORDER BY evaluation_no DESC LIMIT 1", [system, code])

    def resolve_places(
        self, namespace: str, *, principal_id: str, scopes: Iterable[str], geo_namespace: str = "global"
    ) -> dict[str, Any]:
        """Resolve every series geography code to a Geospatial place by its published code; idempotent."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        require_scope(scopes, GEO_READ)
        out: dict[str, list[str]] = {"matched": [], "unresolved": [], "aggregate": []}
        for system, code in self._pairs(namespace):
            wanted = _normalise(code)
            if system in AGGREGATE_SYSTEMS:
                places, state = [], "aggregate"
                reason = "an aggregate of countries or a region is never resolved to or treated as a country"
            else:
                places = self._places(geo_namespace, system, wanted)
                state = "matched" if len(places) == 1 else "unresolved"
                reason = (None if len(places) == 1
                          else "the code system has no place identifier key" if system not in PLACE_KEYS
                          else "no place carries this code" if not places
                          else "more than one place carries this code")
            place = places[0] if state == "matched" else (None, None, None)
            outcome = [state, reason, place[0], place[1]]
            current = self._latest(namespace, system, code)
            if current is not None and [current[k] for k in ("state", "reason", "place_id", "place_revision_id")] \
                    == outcome:
                out[state].append(current["resolution_id"])
                continue
            number = 1 + (current["evaluation_no"] if current else 0)
            resolution_id = "hc-place:" + digest([namespace, system, code, geo_namespace, number, outcome])[:24]
            now = self.now()
            review = "proposed" if state == "matched" else "not-applicable"
            self.conn.execute(
                "INSERT INTO health_capacity_place_resolutions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [namespace, resolution_id, system, code, geo_namespace, state, reason, place[0], place[1], place[2],
                 review, canonical([{"state": review, "by": principal_id, "at_ms": now}]), number, principal_id,
                 now])
            out[state].append(resolution_id)
        return {"contract": CONTRACT, **out, "geo_namespace": geo_namespace,
                "note": "codes resolved by the published code only; aggregates stay aggregates"}

    def resolution(self, namespace: str, resolution_id: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        authorize(namespace, set(scopes), READ_SCOPE)
        view = self._resolution_row(namespace, "resolution_id=?", [resolution_id]) if table_exists(
            self.conn, "health_capacity_place_resolutions") else None
        if view is None:
            raise HealthCapacityError("not_found", "place resolution is not visible in this namespace")
        return view

    def resolutions(self, namespace: str, *, scopes: Iterable[str], current_only: bool = True) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not table_exists(self.conn, "health_capacity_place_resolutions"):
            return []
        rows = self.conn.execute(
            "SELECT resolution_id, geography_system, geography_code, evaluation_no FROM "
            "health_capacity_place_resolutions WHERE namespace=? ORDER BY geography_system, geography_code, "
            "evaluation_no", [namespace]).fetchall()
        latest = {}
        for resolution_id, system, code, _ in rows:
            latest[(system, code)] = resolution_id
        wanted = set(latest.values()) if current_only else {r[0] for r in rows}
        return [self._resolution_row(namespace, "resolution_id=?", [r[0]]) for r in rows if r[0] in wanted]

    def review_place(self, namespace, resolution_id, decision, reason, *, principal_id, scopes) -> dict[str, Any]:
        """Accept or reject a matched resolution, or ``revert`` the latest decision (it becomes proposed again)."""
        authorize(namespace, set(scopes), REVIEW_SCOPE, write=True)
        if decision not in (*DECISIONS, "revert") or not str(reason or "").strip():
            raise HealthCapacityError("invalid_decision", "accept, reject or revert with a reason")
        view = self.resolution(namespace, resolution_id, scopes={"operator"})
        if view["state"] != "matched":
            raise HealthCapacityError("invalid_state", "only a matched resolution is reviewed")
        if decision == "revert":
            if view["review_state"] not in ("accepted", "rejected"):
                raise HealthCapacityError("invalid_state", "only an accepted or rejected resolution is reverted")
            state = "proposed"
        elif view["review_state"] != "proposed":
            raise HealthCapacityError("invalid_state", f"resolution is {view['review_state']}; revert it first")
        else:
            state = "accepted" if decision == "accept" else "rejected"
        history = view["history"] + [{"state": state, "decision": decision, "by": principal_id,
                                      "reason": reason.strip(), "at_ms": self.now()}]
        self.conn.execute(
            "UPDATE health_capacity_place_resolutions SET review_state=?, history_json=? WHERE namespace=? AND "
            "resolution_id=?", [state, canonical(history), namespace, resolution_id])
        return self.resolution(namespace, resolution_id, scopes={"operator"})

    def place_codes(self, namespace: str, place_id: str) -> list[dict[str, Any]]:
        """The (system, code) pairs whose current resolution to ``place_id`` is used (matched, not rejected)."""
        if not table_exists(self.conn, "health_capacity_place_resolutions"):
            return []
        return [r for r in self.resolutions(namespace, scopes={"operator"}) if r["used"] and r["place_id"] == place_id]

    def places_for_code(self, namespace: str, code: str) -> list[dict[str, Any]]:
        """Current resolutions of a published code under any code system (used to answer a code as a place)."""
        wanted = _normalise(code)
        if not table_exists(self.conn, "health_capacity_place_resolutions"):
            return []
        return [r for r in self.resolutions(namespace, scopes={"operator"}) if _normalise(r["geography_code"]) == wanted]

    # ------------------------------------------------------------------ definitions cited by mappings and notes

    def _cite(self, namespace: str, ref: Mapping[str, str]) -> dict[str, Any]:
        rows = self.conn.execute(
            "SELECT series_id FROM surveillance_series WHERE namespace=? AND provider=? AND indicator_code=? AND "
            "condition_scheme=? ORDER BY series_id", [namespace, ref["provider"], ref["source_code"], SCHEME],
        ).fetchall() if table_exists(self.conn, "surveillance_series") else []
        if not rows:
            raise HealthCapacityError("not_found", f"no capacity indicator {ref['provider']}:{ref['source_code']}")
        indicator = self.capacity.indicator(namespace, rows[0][0])
        definition = indicator["definition"]
        return {
            **dict(ref),
            "domain": indicator["domain"],
            "unit": indicator["unit"]["label"],
            "label": indicator["indicator"].get("label"),
            "definition": None if definition is None else {
                k: definition[k] for k in ("revision_id", "version", "valid_from", "text", "locator", "declared_on")},
            "source_revision": None if definition is None else definition["source_revision"],
        }

    def _record(self, table, id_prefix, namespace, left, right, fields, *, principal_id, scopes):
        authorize(namespace, set(scopes), WRITE_SCOPE, write=True)
        a, b = _ref(left), _ref(right)
        if a == b:
            raise HealthCapacityError("invalid_request", "a record links two different indicators")
        cited = [self._cite(namespace, a), self._cite(namespace, b)]
        key = pair_key(a, b)
        record_id = id_prefix + digest([namespace, table, key, a, fields,
                                        [c["definition"] and c["definition"]["revision_id"] for c in cited]])[:24]
        id_column = "mapping_id" if table == "health_capacity_mappings" else "note_id"
        if not self.conn.execute(f"SELECT 1 FROM {table} WHERE namespace=? AND {id_column}=?",
                                 [namespace, record_id]).fetchone():
            now = self.now()
            self.conn.execute(
                f"INSERT INTO {table} VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [namespace, record_id, key, canonical(a), canonical(b), *fields, canonical(cited), "proposed",
                 canonical([{"state": "proposed", "by": principal_id, "at_ms": now}]), principal_id, now])
        return record_id

    def _view(self, table, namespace, record_id):
        id_column, names = (("mapping_id", ("kind", "evidence")) if table == "health_capacity_mappings"
                            else ("note_id", ("relation", "statement")))
        row = self.conn.execute(
            f"SELECT {id_column}, pair_key, left_json, right_json, {names[0]}, {names[1]}, cited_json, state, "
            f"history_json, created_by, created_at_ms FROM {table} WHERE namespace=? AND {id_column}=?",
            [namespace, record_id]).fetchone() if table_exists(self.conn, table) else None
        if row is None:
            raise HealthCapacityError("not_found", "record is not visible in this namespace")
        return {
            "contract": CONTRACT,
            "record_type": "indicator-mapping" if table == "health_capacity_mappings" else "comparability-note",
            "namespace": namespace,
            id_column: row[0], "pair_key": row[1], "left": json.loads(row[2]), "right": json.loads(row[3]),
            names[0]: row[4], names[1]: row[5], "cited": json.loads(row[6]), "state": row[7],
            "history": json.loads(row[8]), "created_by": row[9], "created_at_ms": row[10],
        }

    def _transition(self, table, namespace, record_id, decision, reason, *, principal_id, scopes):
        authorize(namespace, set(scopes), REVIEW_SCOPE, write=True)
        if decision not in (*DECISIONS, "revert") or not str(reason or "").strip():
            raise HealthCapacityError("invalid_decision", "accept, reject or revert with a reason")
        view = self._view(table, namespace, record_id)
        if decision == "revert":
            if view["state"] not in ("accepted", "rejected"):
                raise HealthCapacityError("invalid_state", "only an accepted or rejected record is reverted")
            state = "reverted"
        else:
            if view["state"] != "proposed":
                raise HealthCapacityError("invalid_state", f"record is {view['state']}; only a proposed one is "
                                                           "reviewed")
            state = "accepted" if decision == "accept" else "rejected"
        history = view["history"] + [{"state": state, "by": principal_id, "reason": reason.strip(),
                                      "at_ms": self.now()}]
        id_column = "mapping_id" if table == "health_capacity_mappings" else "note_id"
        self.conn.execute(f"UPDATE {table} SET state=?, history_json=? WHERE namespace=? AND {id_column}=?",
                          [state, canonical(history), namespace, record_id])
        return self._view(table, namespace, record_id)

    def _list(self, table, namespace, *, scopes, ref=None, active_only=False):
        authorize(namespace, set(scopes), READ_SCOPE)
        if not table_exists(self.conn, table):
            return []
        id_column = "mapping_id" if table == "health_capacity_mappings" else "note_id"
        needle = None if ref is None else f"{ref['provider']}|{ref['source_code']}"
        rows = self.conn.execute(
            f"SELECT {id_column} FROM {table} WHERE namespace=? AND (? IS NULL OR strpos(pair_key, ?)>0) "
            "ORDER BY created_at_ms, 1", [namespace, needle, needle]).fetchall()
        out = [self._view(table, namespace, r[0]) for r in rows]
        return [v for v in out if not active_only or v["state"] in ACTIVE_STATES]

    # ------------------------------------------------------------------ mappings

    def propose_mapping(self, namespace, left, right, kind, evidence, *, principal_id, scopes) -> dict[str, Any]:
        """Propose that ``left`` is equivalent to, broader or narrower than ``right``; idempotent."""
        if kind not in MAPPING_KINDS or not str(evidence or "").strip():
            raise HealthCapacityError("invalid_mapping", f"a mapping has one of {MAPPING_KINDS} and its evidence")
        mapping_id = self._record("health_capacity_mappings", "hc-mapping:", namespace, left, right,
                                  [kind, str(evidence).strip()], principal_id=principal_id, scopes=scopes)
        return self._view("health_capacity_mappings", namespace, mapping_id)

    def review_mapping(self, namespace, mapping_id, decision, reason, *, principal_id, scopes) -> dict[str, Any]:
        return self._transition("health_capacity_mappings", namespace, mapping_id, decision, reason,
                                principal_id=principal_id, scopes=scopes)

    def mappings(self, namespace, *, scopes, ref=None, active_only=False) -> list[dict[str, Any]]:
        return self._list("health_capacity_mappings", namespace, scopes=scopes,
                          ref=None if ref is None else _ref(ref), active_only=active_only)

    # ------------------------------------------------------------------ comparability notes

    def record_note(self, namespace, left, right, relation, statement, *, principal_id, scopes) -> dict[str, Any]:
        """Propose a comparability note citing both definitions; idempotent for the same pair, relation, statement
        and cited definition revisions."""
        if relation not in RELATIONS or not str(statement or "").strip():
            raise HealthCapacityError("invalid_note", f"a note has one of {RELATIONS} and a statement")
        note_id = self._record("health_capacity_notes", "hc-note:", namespace, left, right,
                               [relation, str(statement).strip()], principal_id=principal_id, scopes=scopes)
        return self._view("health_capacity_notes", namespace, note_id)

    def review_note(self, namespace, note_id, decision, reason, *, principal_id, scopes) -> dict[str, Any]:
        return self._transition("health_capacity_notes", namespace, note_id, decision, reason,
                                principal_id=principal_id, scopes=scopes)

    def notes(self, namespace, *, scopes, ref=None, active_only=False) -> list[dict[str, Any]]:
        return self._list("health_capacity_notes", namespace, scopes=scopes,
                          ref=None if ref is None else _ref(ref), active_only=active_only)

    def overview(self, namespace: str, *, scopes: Iterable[str], ref: Mapping[str, Any] | None = None) -> dict:
        """Every mapping and note (optionally for one indicator) and every current place resolution."""
        return {
            "contract": CONTRACT,
            "mappings": self.mappings(namespace, scopes=scopes, ref=ref),
            "notes": self.notes(namespace, scopes=scopes, ref=ref),
            "places": self.resolutions(namespace, scopes=scopes),
            "note": "reviewable alignment records; no harmonised, adjusted or combined value is produced",
        }


__all__ = [
    "CONTRACT", "DECISIONS", "HealthCapacityComparability", "MAPPING_KINDS", "PLACE_KEYS", "RELATIONS", "pair_key",
]
