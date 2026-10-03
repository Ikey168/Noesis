"""Answers: a business indicator for a place as of a release, places side by side, and a series' history (IB08, IB09).

Track #2738.

:meth:`BusinessQueries.indicator_for_place` takes a place (a Geospatial place id, read through accepted IB06 matches
only, or a published area code), an optional indicator concept and classification code, and a date. It returns **one
row per series**: each source's values as released by that date (the vintage whose release clock is on or before the
date) with the definition, statistical unit, classification and its version, size class, adjustment, unit and index
base year, flags verbatim, withheld and missing periods, source-stated notes and the cited vintage. A requested
classification code also reaches codes of other classifications through **accepted** candidate links only, and such
rows say so. Rows are never combined: no average, no blended, re-based, re-adjusted or reconstructed figure.

:meth:`BusinessQueries.compare_places` answers several places at once (Germany through Eurostat, California through
CBP) and lists, per pair of rows of the same concept, the recorded differences (statistical unit, classification,
place, source, adjustment, base year) and the comparability notes; a pair without either is
``comparability_unknown``.

:meth:`BusinessQueries.series_history` returns every vintage of one series with its release date and basis, the periods
each release added, revised or dropped (values and flags before and after as published), definition changes and
removals by the source, and for each consecutive pair the comparability notes that apply (classification breaks
flagged by the source, provisional periods confirmed, base-year changes, NAICS vintages, recorded notes). Every vintage
is cited.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from src.ingestion.business_statistics_sources import (
    CONCEPTS,
    NEVER_SENTENCE,
    PROVIDERS,
)
from src.kb.business_statistics_records import (
    ANSWER_CONTRACT,
    EXCLUSIONS,
    MINIMISATION,
    READ_SCOPE,
    BusinessError,
    authorize,
    comparability_basis,
    iso,
    table_exists,
    to_ms,
)
from src.kb.business_statistics_store import BusinessStatisticsStore, citation

HISTORY_CONTRACT = "noesis-business-statistics-history-v1"
# Concepts that answer a similar question with different statistical units: paired only to state the difference.
RELATED_CONCEPTS = {
    frozenset({"active_enterprises", "establishments"}): (
        "Eurostat counts active enterprises and Census counts establishments (single physical locations); an "
        "enterprise may run several establishments, so the counts are different measures and are never compared as "
        "one"),
}
NEVER_COMBINED = ("rows with different sources, statistical units, classifications or classification vintages, size "
                  "classes, adjustments or index base years are separate series and are never combined")


def cutoff_ms(as_of: Any) -> int | None:
    """A date means the end of that day (released *by* the date); a time or epoch ms is used as given."""
    if as_of in (None, ""):
        return None
    if isinstance(as_of, (int, float)):
        return int(as_of)
    raw = str(as_of).strip()
    return to_ms(raw) + 86_399_999 if len(raw) == 10 else to_ms(raw)


def _next_period(period: str, frequency: str) -> str | None:
    if frequency == "annual" and len(period) == 4:
        return str(int(period) + 1)
    if frequency == "monthly" and len(period) == 7:
        year, month = int(period[:4]), int(period[5:])
        return f"{year + (month == 12)}-{1 if month == 12 else month + 1:02d}"
    return None


def missing_periods(periods: Sequence[str], frequency: str) -> list[str]:
    """Periods between the first and last stated period that a vintage does not state (never filled)."""
    stated = sorted(set(periods))
    out, current = [], _next_period(stated[0], frequency) if len(stated) > 1 else None
    while current is not None and current < stated[-1] and len(out) < 1000:
        if current not in stated:
            out.append(current)
        current = _next_period(current, frequency)
    return out


def comparability_group(series: Mapping[str, Any]) -> str:
    c, u = series["classification"], series["unit"]
    return " | ".join([series["provider"], f"unit={series['statistical_unit']}",
                       f"class={c['scheme']} {c['version']} {c['code']}", f"size={series['size_class'].get('code')}",
                       f"adj={series['adjustment']}", f"measure={u.get('code')}" + (
                           f" (base {u['base_year']})" if u.get("base_year") else "")])


class BusinessQueries:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = BusinessStatisticsStore(conn, initialize=False, now=self.now)

    def _identity(self):
        from src.kb.business_statistics_identity import BusinessIdentity

        return BusinessIdentity(self.conn, initialize=False, now=self.now)

    def _area_codes(self, namespace: str, place: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        identity = self._identity()
        if isinstance(place, str):
            codes = [{**c, "basis": "accepted-match"} for c in identity.area_codes_for_place(namespace, place)]
            return codes, {"place_id": place}
        area = dict(place or {})
        if not area.get("scheme") or not area.get("code"):
            raise BusinessError("invalid_request", "name a place id or an area with scheme and code")
        codes = [{"scheme": area["scheme"], "code": str(area["code"]), "basis": "published-code"}]
        accepted = identity.place_for_area(namespace, area["scheme"], str(area["code"]))
        if accepted:
            for other in identity.area_codes_for_place(namespace, accepted["place_id"]):
                if (other["scheme"], other["code"]) != (area["scheme"], str(area["code"])):
                    codes.append({**other, "basis": "accepted-match"})
        return codes, {"area": area, "place_id": accepted["place_id"] if accepted else None}

    def _classifications(self, namespace: str, wanted: Mapping[str, Any] | None) -> list[dict[str, Any]] | None:
        if not wanted:
            return None
        from src.kb.business_statistics_identity import code_ref

        requested = code_ref(wanted)
        codes = [{**requested, "matched_by": "requested code"}]
        for linked in self._identity().linked_codes(namespace, requested):
            codes.append({k: linked[k] for k in ("scheme", "version", "code")} | {
                "matched_by": f"accepted candidate link ({linked['relation']})",
                "assertion_id": linked["assertion_id"]})
        return codes

    def indicator_for_place(self, namespace: str, *, scopes: Iterable[str], place: Any,
                            concept: str | None = None, classification: Mapping[str, Any] | None = None,
                            as_of: Any = None, period_from: str | None = None, period_to: str | None = None,
                            providers: Iterable[str] | None = None,
                            enabled_providers: Iterable[str] | None = None) -> dict[str, Any]:
        """Each source's figures for a place as released by a date, side by side with definitions; never combined."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if concept is not None and concept not in CONCEPTS:
            raise BusinessError("invalid_request", f"concept is one of {CONCEPTS}")
        cutoff = cutoff_ms(as_of)
        codes, subject = self._area_codes(namespace, place)
        classes = self._classifications(namespace, classification)
        wanted = set(providers or PROVIDERS)
        enabled = set(enabled_providers if enabled_providers is not None else PROVIDERS)
        bases = {(c["scheme"], c["code"]): c["basis"] for c in codes}
        candidates = self.store.find_series(namespace, concept=concept,
                                            area_codes=[(c["scheme"], c["code"]) for c in codes]) if codes else []
        rows, unavailable, groups = [], [], {}
        for series in candidates:
            if series["provider"] not in wanted or series["provider"] not in enabled:
                continue
            matched = None
            if classes is not None:
                ref = {k: series["classification"][k] for k in ("scheme", "version", "code")}
                matched = next((c for c in classes if {k: c[k] for k in ("scheme", "version", "code")} == ref), None)
                if matched is None:
                    continue
            answer = self.store.values(namespace, series["series_id"], as_of_ms=cutoff)
            if answer["status"] == "unavailable":
                first = self.store.vintage_rows(namespace, series["series_id"])
                unavailable.append({"series_id": series["series_id"], "provider": series["provider"],
                                    "native_key": series["native_key"], "reason": answer["reason"],
                                    "first_release_at": first[0]["release_at"] if first else None})
                continue
            observations = [o for o in answer["observations"]
                            if (not period_from or o["period"] >= period_from)
                            and (not period_to or o["period"][:len(period_to)] <= period_to)]
            group = comparability_group(series)
            groups.setdefault(group, []).append(series["series_id"])
            stated = [o["period"] for o in answer["observations"]]
            rows.append({
                "series_id": series["series_id"], "provider": series["provider"], "dataset": series["dataset"],
                "native_key": series["native_key"], "indicator": series["indicator"],
                "classification": series["classification"], "matched_by": matched,
                "size_class": series["size_class"], "area": series["area"],
                "area_basis": bases.get((series["area"]["scheme"], str(series["area"]["code"]))),
                "adjustment": series["adjustment"], "unit": series["unit"],
                "statistical_unit": series["statistical_unit"], "frequency": series["frequency"],
                "comparability_group": group, "status": answer["status"], "definition": answer.get("definition"),
                "values": [{**o, "citation_vintage_id": answer["vintage"]["vintage_id"]} for o in observations],
                "gaps": {"missing_periods": missing_periods(stated, series["frequency"]) if stated else [],
                         "withheld_or_confidential": [{"period": o["period"], "status": o["status"],
                                                       "flags": o["flags"]} for o in answer["observations"]
                                                      if o["status"] not in {"reported"}],
                         "latest_published_period": max(stated) if stated else None,
                         "note": "missing, withheld and confidential periods are not filled, nowcast or "
                                 "reconstructed"},
                "vintage": {k: answer["vintage"][k] for k in ("vintage_id", "status", "release_label", "release_at",
                                                              "release_basis", "retrieved_at", "dataflow_version",
                                                              "revision_of")},
                "citation": answer["citation"],
                "notes": [{k: n[k] for k in ("note_id", "relation", "statement", "periods", "state", "origin",
                                              "other_series_id")} for n in self.store.notes(namespace,
                                                                                             series["series_id"])],
            })
        rows.sort(key=lambda r: (r["provider"], r["indicator"]["concept"], r["comparability_group"], r["native_key"]))
        present = {r["provider"] for r in rows} | {u["provider"] for u in unavailable}
        return {
            "contract": ANSWER_CONTRACT, "namespace": namespace,
            "query": {**subject, "concept": concept, "classification": dict(classification) if classification else None,
                      "as_of": as_of, "period_from": period_from, "period_to": period_to},
            "as_of": iso(cutoff) if cutoff is not None else None, "area_codes": codes,
            "classification_codes": classes, "status": "reported" if rows else "none_published", "results": rows,
            "comparability_groups": groups, "comparability": self._pairs(namespace, rows),
            "unavailable_by_as_of": unavailable,
            "none_on_record": [{"provider": p, "codes": [(c["scheme"], c["code"]) for c in codes]}
                               for p in sorted(wanted & enabled) if p not in present],
            "features_disabled": [{"provider": p, "reason": "optional feature not selected"}
                                  for p in sorted(wanted - enabled)],
            "unmatched_place": not codes, "side_by_side": True,
            "never_combined": NEVER_COMBINED, "statement": NEVER_SENTENCE, "exclusions": list(EXCLUSIONS),
            "minimisation": MINIMISATION["decision"],
        }

    def _notes_between(self, namespace: str, left: str, right: str) -> list[dict[str, Any]]:
        return [{k: n[k] for k in ("note_id", "relation", "statement", "state", "origin")}
                for n in self.store.notes(namespace, left) if n["other_series_id"] == right
                or (n["series_id"] == right and n["other_series_id"] == left)]

    def _pairs(self, namespace: str, rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        identity = self._identity() if table_exists(self.conn, "business_identity_assertions") else None
        pairs = []
        for i, left in enumerate(rows):
            for right in rows[i + 1:]:
                concepts = {left["indicator"]["concept"], right["indicator"]["concept"]}
                if len(concepts) > 1 and not any(concepts <= family for family in RELATED_CONCEPTS):
                    continue
                differences = comparability_basis(left, right)
                if len(concepts) > 1:
                    differences.append({"kind": "different_concept", "field": "indicator",
                                        "left": left["indicator"]["concept"], "right": right["indicator"]["concept"],
                                        "statement": RELATED_CONCEPTS[next(f for f in RELATED_CONCEPTS
                                                                           if concepts <= f)]})
                links = []
                if identity is not None and left["classification"] != right["classification"]:
                    target = {k: right["classification"][k] for k in ("scheme", "version", "code")}
                    links = [{k: link[k] for k in ("relation", "state", "assertion_id")}
                             for state in ("accepted", "proposed")
                             for link in identity.linked_codes(namespace, left["classification"], state=state)
                             if {k: link[k] for k in ("scheme", "version", "code")} == target]
                notes = self._notes_between(namespace, left["series_id"], right["series_id"])
                pairs.append({"series": [left["series_id"], right["series_id"]],
                              "recorded_differences": differences, "classification_links": links, "notes": notes,
                              "status": "noted" if differences or notes else "comparability_unknown"})
        return pairs

    def compare_places(self, namespace: str, *, scopes: Iterable[str], places: Sequence[Any],
                       concept: str | None = None, classification: Mapping[str, Any] | None = None,
                       classifications: Mapping[str, Mapping[str, Any]] | None = None, as_of: Any = None,
                       enabled_providers: Iterable[str] | None = None) -> dict[str, Any]:
        """Several places' figures side by side (each with its own sources and definitions); never blended.

        ``classifications`` may name a code per place key (e.g. NACE ``C`` for Germany, NAICS ``31-33`` for
        California); ``classification`` applies to every place.
        """
        if not places:
            raise BusinessError("invalid_request", "name at least one place")
        answers = []
        for place in places:
            key = place if isinstance(place, str) else f"{dict(place).get('scheme')}:{dict(place).get('code')}"
            wanted = dict((classifications or {}).get(key) or {}) or classification
            answers.append({"place": place, "answer": self.indicator_for_place(
                namespace, scopes=scopes, place=place, concept=concept, classification=wanted, as_of=as_of,
                enabled_providers=enabled_providers)})
        rows = [r for a in answers for r in a["answer"]["results"]]
        return {"contract": ANSWER_CONTRACT, "namespace": namespace, "as_of": answers[0]["answer"]["as_of"],
                "places": answers, "comparability": self._pairs(namespace, rows), "side_by_side": True,
                "never_combined": NEVER_COMBINED, "statement": NEVER_SENTENCE,
                "exclusions": list(EXCLUSIONS)}

    def series_history(self, namespace: str, series_id: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        """Every vintage of a series, what each release changed, and comparability per consecutive pair."""
        authorize(namespace, scopes, READ_SCOPE)
        series = self.store.series(namespace, series_id)
        notes = self.store.notes(namespace, series_id)
        vintages, pairs, previous = [], [], None
        for vintage in self.store.vintage_rows(namespace, series_id):
            release = self.store.release(namespace, vintage["release_id"])
            changes = vintage["changes"]
            entry = {
                "vintage_id": vintage["vintage_id"], "sequence": vintage["sequence"], "status": vintage["status"],
                "release_label": vintage["release_label"], "release_at": vintage["release_at"],
                "release_basis": vintage["release_basis"], "retrieved_at": vintage["retrieved_at"],
                "revision_of": vintage["revision_of"], "dataflow_version": vintage["dataflow_version"],
                "definition_id": vintage["definition_id"], "new_periods": changes.get("new_periods", []),
                "changed_periods": [r["period"] for r in changes.get("revised", [])],
                "revisions": changes.get("revised", []), "dropped_periods": changes.get("dropped_periods", []),
                "definition_change": changes.get("definition_change"),
                "removed_by_source": changes.get("removed_by_source"),
                "observations": self.store.observations(namespace, vintage["vintage_id"]),
                "citation": citation(series, vintage, release),
            }
            vintages.append(entry)
            if previous is not None:
                pairs.append(self._history_pair(previous, entry, notes))
            previous = entry
        related = [{"series_id": n["other_series_id"] if n["series_id"] == series_id else n["series_id"],
                    "relation": n["relation"], "statement": n["statement"], "state": n["state"],
                    "origin": n["origin"]} for n in notes if n["other_series_id"]]
        classification_links = []
        if table_exists(self.conn, "business_identity_assertions"):
            for state in ("accepted", "proposed"):
                for link in self._identity().linked_codes(namespace, series["classification"], state=state):
                    vintage_change = link["scheme"] == series["classification"]["scheme"]
                    classification_links.append({
                        **link, "kind": "classification_vintage" if vintage_change else "other_classification",
                        "statement": (f"{series['classification']['scheme']} {series['classification']['version']} "
                                      f"and {link['version']} are different classification keys; series of each "
                                      "vintage stay separate" if vintage_change else
                                      "a candidate concordance link; the series are never merged")})
        return {"contract": HISTORY_CONTRACT, "namespace": namespace, "series": series, "vintages": vintages,
                "pairs": pairs, "series_notes": notes, "related_series": related,
                "classification_links": classification_links, "statement": NEVER_SENTENCE,
                "exclusions": list(EXCLUSIONS),
                "note": "every retained vintage; earlier values are never overwritten, re-based or filled"}

    @staticmethod
    def _history_pair(before: Mapping[str, Any], after: Mapping[str, Any],
                      notes: list[dict[str, Any]]) -> dict[str, Any]:
        stated = []
        if after["definition_change"]:
            stated.append({"kind": "definition_change", "statement": "the definition or dataflow version changed",
                           "detail": after["definition_change"]})
        if after["removed_by_source"]:
            stated.append({"kind": "removed_by_source", "statement": after["removed_by_source"]["statement"]})
            for note in notes:
                if note["relation"] in {"base_year_change", "classification_change"} and note["other_series_id"]:
                    stated.append({"kind": note["relation"], "note_id": note["note_id"],
                                   "statement": note["statement"], "successor_series_id": note["other_series_id"]})
        confirmed = [r["period"] for r in after["revisions"]
                     if "p" in str(dict(r["before"].get("flags") or {}).get("OBS_FLAG", ""))
                     and "p" not in str(dict(r["after"].get("flags") or {}).get("OBS_FLAG", ""))]
        if confirmed:
            stated.append({"kind": "provisional_confirmed", "periods": confirmed,
                           "statement": "values the earlier release marked provisional (p) are restated without the "
                                        "flag"})
        touched = set(after["changed_periods"]) | set(after["new_periods"])
        for note in notes:
            if note["relation"] == "break_in_series" and (not note["periods"] or touched & set(note["periods"])):
                stated.append({"kind": "break_in_series", "note_id": note["note_id"], "statement": note["statement"],
                               "periods": note["periods"], "origin": note["origin"]})
            elif note["origin"] == "recorded" and note["state"] == "accepted" and not note["other_series_id"]:
                stated.append({"kind": note["relation"], "note_id": note["note_id"], "statement": note["statement"]})
        return {"from_vintage_id": before["vintage_id"], "to_vintage_id": after["vintage_id"],
                "from_release_at": before["release_at"], "to_release_at": after["release_at"],
                "changed_periods": after["changed_periods"], "new_periods": after["new_periods"],
                "comparability": "noted" if stated else "comparability_unknown", "notes": stated}


__all__ = ["HISTORY_CONTRACT", "NEVER_COMBINED", "BusinessQueries", "comparability_group", "cutoff_ms",
           "missing_periods"]
