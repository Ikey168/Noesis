"""Comparability across publishers and definition revisions, without merging series (#1914, M06).

A **comparability note** links two series (or two definition revisions of one
series) with a typed relation - ``same_concept_different_definition``,
``same_definition_different_geography_level``,
``same_definition_different_reference_date`` or ``not_comparable`` - and cites
the definition texts and the source revisions it rests on. Notes are proposed
by a writer, accepted or rejected by a reviewer and reversible; a reverted
note never reactivates an earlier one.

A side-by-side query returns every publisher's series for one concept,
geography and period with its definition, vintage, unit, level, breaks and
coverage notes. It never produces a merged, averaged or netted value; a pair
without a note is reported as ``comparability_unknown``. Unit conversions
between series (persons and thousand persons) go through the pint evaluation
with a calculation receipt and never create a stored series; a count and a
rate are never converted into each other (no denominator is inferred).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from decimal import ROUND_HALF_EVEN, Decimal
from itertools import combinations
from typing import Any

from src.ingestion.demographic_sources import UNITS
from src.kb.demographics import (
    ANSWER_CONTRACT,
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    DemographicError,
    DemographicStore,
    authorize,
    canonical,
    digest,
    table_exists,
)

CONTRACT = "noesis-demographic-comparability-v1"
RELATIONS = (
    "same_concept_different_definition",
    "same_definition_different_geography_level",
    "same_definition_different_reference_date",
    "not_comparable",
)
ACTIVE_STATES = ("proposed", "accepted")
_DDL = """
CREATE TABLE IF NOT EXISTS demographic_comparability (
  namespace TEXT NOT NULL, note_id TEXT NOT NULL, pair_key TEXT NOT NULL, left_json TEXT NOT NULL,
  right_json TEXT NOT NULL, relation TEXT NOT NULL, statement TEXT NOT NULL, cited_json TEXT NOT NULL,
  state TEXT NOT NULL, history_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, note_id)
);
"""


def _side(value: Mapping[str, Any]) -> dict[str, str]:
    value = dict(value or {})
    if bool(value.get("series_id")) == bool(value.get("definition_id")):
        raise DemographicError(
            "invalid_note", "each side names one series_id or one definition_id"
        )
    return (
        {"kind": "series", "id": str(value["series_id"])}
        if value.get("series_id")
        else {"kind": "definition", "id": str(value["definition_id"])}
    )


def pair_key(left: Mapping[str, str], right: Mapping[str, str]) -> str:
    """Order-insensitive: a note on (a, b) is a note on (b, a)."""
    return canonical(
        sorted([f"{left['kind']}:{left['id']}", f"{right['kind']}:{right['id']}"])
    )


class DemographicComparability:
    def __init__(self, conn: Any, *, now=None, initialize: bool = True) -> None:
        self.conn = conn
        self.store = DemographicStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        if initialize:
            conn.execute(_DDL)

    def _cite(self, namespace: str, side: Mapping[str, str]) -> dict[str, Any]:
        if side["kind"] == "series":
            series = self.store.series(namespace, side["id"])
            definition = series["definition"]
            if definition is None:
                raise DemographicError("not_found", "the series has no vintage yet")
        else:
            definition = self.store.definition(namespace, side["id"])
            series = None
        return {
            **dict(side),
            "provider": definition["provider"],
            "definition_id": definition["definition_id"],
            "definition_text": definition["content"].get("source_text"),
            "definition": {
                k: definition["content"].get(k)
                for k in (
                    "concept",
                    "measure",
                    "breakdown",
                    "procedure",
                    "reference",
                    "population_base",
                )
            },
            "geography_level": None if series is None else series["geography_level"],
            "source_revision": definition["source_revision"],
        }

    def record(
        self,
        namespace: str,
        left: Mapping[str, Any],
        right: Mapping[str, Any],
        relation: str,
        statement: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        """Propose a note; idempotent for the same pair, relation, statement and cited definitions."""
        authorize(namespace, set(scopes), WRITE_SCOPE, write=True)
        if relation not in RELATIONS or not str(statement or "").strip():
            raise DemographicError(
                "invalid_note", f"a note has one of {RELATIONS} and a statement"
            )
        a, b = _side(left), _side(right)
        if a == b:
            raise DemographicError("invalid_note", "a note links two different records")
        cited = sorted(
            [self._cite(namespace, a), self._cite(namespace, b)],
            key=lambda c: (c["kind"], c["id"]),
        )
        key = pair_key(a, b)
        note_id = (
            "dm-comparability:"
            + digest(
                [
                    namespace,
                    key,
                    relation,
                    statement.strip(),
                    [c["definition_id"] for c in cited],
                ]
            )[:24]
        )
        if not self.conn.execute(
            "SELECT 1 FROM demographic_comparability WHERE namespace=? AND note_id=?",
            [namespace, note_id],
        ).fetchone():
            now = self.now()
            self.conn.execute(
                "INSERT INTO demographic_comparability VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    namespace,
                    note_id,
                    key,
                    canonical(a),
                    canonical(b),
                    relation,
                    statement.strip(),
                    canonical(cited),
                    "proposed",
                    canonical(
                        [{"state": "proposed", "by": principal_id, "at_ms": now}]
                    ),
                    principal_id,
                    now,
                ],
            )
        return self.note(namespace, note_id, scopes={"operator"})

    def _transition(self, namespace, note, state, principal_id, reason):
        history = note["history"] + [
            {"state": state, "by": principal_id, "reason": reason, "at_ms": self.now()}
        ]
        self.conn.execute(
            "UPDATE demographic_comparability SET state=?, history_json=? WHERE namespace=? AND note_id=?",
            [state, canonical(history), namespace, note["note_id"]],
        )
        return self.note(namespace, note["note_id"], scopes={"operator"})

    def review(self, namespace, note_id, decision, reason, *, principal_id, scopes):
        authorize(namespace, set(scopes), REVIEW_SCOPE, write=True)
        if decision not in {"accept", "reject"} or not str(reason or "").strip():
            raise DemographicError("invalid_decision", "accept or reject with a reason")
        note = self.note(namespace, note_id, scopes={"operator"})
        if note["state"] != "proposed":
            raise DemographicError(
                "invalid_state",
                f"note is {note['state']}; only a proposed note is reviewed",
            )
        return self._transition(
            namespace,
            note,
            "accepted" if decision == "accept" else "rejected",
            principal_id,
            reason.strip(),
        )

    def revert(self, namespace, note_id, reason, *, principal_id, scopes):
        """Withdraw a reviewed note; no earlier note is reactivated by it."""
        authorize(namespace, set(scopes), REVIEW_SCOPE, write=True)
        if not str(reason or "").strip():
            raise DemographicError("invalid_decision", "a revert needs a reason")
        note = self.note(namespace, note_id, scopes={"operator"})
        if note["state"] not in {"accepted", "rejected"}:
            raise DemographicError(
                "invalid_state", "only an accepted or rejected note can be reverted"
            )
        return self._transition(
            namespace, note, "reverted", principal_id, reason.strip()
        )

    def note(
        self, namespace: str, note_id: str, *, scopes: Iterable[str]
    ) -> dict[str, Any]:
        authorize(namespace, set(scopes), READ_SCOPE)
        row = self.conn.execute(
            "SELECT note_id, pair_key, left_json, right_json, relation, statement, cited_json, state, history_json, "
            "created_by, created_at_ms FROM demographic_comparability WHERE namespace=? AND note_id=?",
            [namespace, note_id],
        ).fetchone()
        if row is None:
            raise DemographicError(
                "not_found", "comparability note is not visible in this namespace"
            )
        return {
            "contract": CONTRACT,
            "namespace": namespace,
            "note_id": row[0],
            "pair_key": row[1],
            "left": json.loads(row[2]),
            "right": json.loads(row[3]),
            "relation": row[4],
            "statement": row[5],
            "cited": json.loads(row[6]),
            "state": row[7],
            "history": json.loads(row[8]),
            "created_by": row[9],
            "created_at_ms": row[10],
        }

    def notes(
        self,
        namespace: str,
        *,
        scopes: Iterable[str],
        record_id: str | None = None,
        active_only: bool = False,
    ) -> list[dict[str, Any]]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if not table_exists(self.conn, "demographic_comparability"):
            return []
        rows = self.conn.execute(
            "SELECT note_id FROM demographic_comparability WHERE namespace=? AND (? IS NULL OR strpos(pair_key, ?)>0) "
            "ORDER BY created_at_ms, note_id",
            [namespace, record_id, record_id],
        ).fetchall()
        out = [self.note(namespace, r[0], scopes=scopes) for r in rows]
        return [n for n in out if not active_only or n["state"] in ACTIVE_STATES]

    def _pair(
        self, namespace, a: Mapping[str, str], b: Mapping[str, str]
    ) -> dict[str, Any]:
        key = pair_key(a, b)
        found = []
        if table_exists(self.conn, "demographic_comparability"):
            found = [
                self.note(namespace, r[0], scopes={"operator"})
                for r in self.conn.execute(
                    "SELECT note_id FROM demographic_comparability WHERE namespace=? AND pair_key=? "
                    "ORDER BY created_at_ms, note_id",
                    [namespace, key],
                ).fetchall()
            ]
        active = [n for n in found if n["state"] in ACTIVE_STATES]
        return {
            "left": dict(a),
            "right": dict(b),
            "comparability": active[-1]["relation"]
            if active
            else "comparability_unknown",
            "notes": [
                {
                    k: n[k]
                    for k in ("note_id", "relation", "statement", "state", "cited")
                }
                for n in active
            ],
        }

    def side_by_side(
        self,
        namespace: str,
        concept: str,
        *,
        scopes: Iterable[str],
        geography_codes: Sequence[str] | None = None,
        period_from: str | None = None,
        period_to: str | None = None,
        as_of_ms: int | None = None,
    ) -> dict[str, Any]:
        """Each publisher's series for one concept, geography and period, side by side; nothing merged."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        wanted = {str(c).upper() for c in geography_codes or []}
        columns = []
        for series in self.store.find_series(namespace, concept=concept):
            if wanted and series["geography_code"].upper() not in wanted:
                continue
            # The vintage in force is selected first; the period filter applies to its values.
            values = self.store.values(
                namespace,
                series["series_id"],
                as_of_ms=as_of_ms,
                period_from=period_from,
                period_to=period_to,
            )
            columns.append(
                {
                    "series_id": series["series_id"],
                    "provider": series["provider"],
                    "series_code": series["series_code"],
                    "indicator": series["indicator"],
                    "dimensions": series["dimensions"],
                    "geography": {
                        "code": series["geography_code"],
                        "label": series["geography_label"],
                    },
                    "geography_level": series["geography_level"],
                    "unit": series["unit"],
                    "status": values["status"],
                    "reason": values.get("reason"),
                    "definition": values.get("definition"),
                    "vintage": values.get("vintage"),
                    "coverage_notes": values.get("coverage_notes", []),
                    "breaks": series["breaks"],
                    "observations": values["observations"],
                }
            )
        pairs = [
            self._pair(
                namespace,
                {"kind": "series", "id": x["series_id"]},
                {"kind": "series", "id": y["series_id"]},
            )
            for x, y in combinations(columns, 2)
        ]
        for column in columns:
            for item in column["breaks"]:
                if item["from_definition_id"] and item["to_definition_id"]:
                    pairs.append(
                        self._pair(
                            namespace,
                            {"kind": "definition", "id": item["from_definition_id"]},
                            {"kind": "definition", "id": item["to_definition_id"]},
                        )
                    )
        return {
            "contract": ANSWER_CONTRACT,
            "concept": concept,
            "geography_codes": sorted(wanted),
            "period": {"from": period_from, "to": period_to},
            "as_of_ms": as_of_ms,
            "series": columns,
            "pairs": pairs,
            "note": "each publisher's series as published, side by side; no merged, averaged or netted value is "
            "produced, and a pair without a note is comparability_unknown",
        }

    def convert(
        self,
        namespace: str,
        series_id: str,
        target_unit: str,
        *,
        scopes: Iterable[str],
        vintage_id: str | None = None,
    ) -> dict[str, Any]:
        """A series' values in another count unit with a calculation receipt; nothing is stored."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        values = self.store.values(namespace, series_id, vintage_id=vintage_id)
        source_unit = values["series"]["unit"]["label"]
        if target_unit not in UNITS:
            raise DemographicError(
                "invalid_unit", f"{target_unit!r} is not a declared unit"
            )
        source_pint, source_scale, source_kind = UNITS[source_unit]
        target_pint, target_scale, target_kind = UNITS[target_unit]
        if source_kind != "count" or target_kind != "count":
            raise DemographicError(
                "incompatible_units",
                "a count and a rate are not converted into each other (that needs a denominator, never inferred)",
            )
        converted, receipts = [], []
        for obs in values["observations"]:
            if obs["value"] is None:
                converted.append({**obs, "converted": None})
                continue
            try:
                from src.integrations.units import convert_physical

                receipt = convert_physical(
                    obs["value"], source_pint, target_pint, precision=3
                )
                value, method = receipt["result"]["value"], "pint convert_physical"
                receipts.append(receipt.get("sha256"))
            except ModuleNotFoundError:
                value = str(
                    (Decimal(obs["value"]) * source_scale / target_scale).quantize(
                        Decimal("0.001"), rounding=ROUND_HALF_EVEN
                    )
                )
                method = "exact decimal scale (pint not installed)"
            converted.append(
                {
                    **obs,
                    "converted": {
                        "value": value,
                        "unit": target_unit,
                        "method": method,
                    },
                }
            )
        request = {
            "series_id": series_id,
            "vintage_id": (values.get("vintage") or {}).get("vintage_id"),
            "to": target_unit,
        }
        return {
            "contract": ANSWER_CONTRACT,
            "series_id": series_id,
            "from_unit": source_unit,
            "to_unit": target_unit,
            "observations": converted,
            "calculation_receipt": {
                "request": request,
                "pint_receipts": receipts,
                "digest": digest([request, [o.get("converted") for o in converted]]),
            },
            "stored": False,
            "note": "a calculation over one series; it never creates a stored series",
        }
