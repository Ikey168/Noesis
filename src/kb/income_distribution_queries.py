"""Answers: an income or poverty indicator for a place as of a release, and a series' history (IP08, IP09).

Delivery issues #2623 and #2628.

:meth:`IncomeQueries.indicator_for_place` takes a place (a Geospatial place id, read through accepted IP06 matches
only, or a published area code), an indicator concept and a date, and returns **one row per series**: each source's
value as released by that date (the vintage whose release clock is on or before the date), with its definition,
poverty line and PPP base year, welfare concept, equivalence scale and the cited vintage. Rows are grouped by the
fields that make figures incomparable (source, welfare concept, equivalence scale, poverty line, PPP round,
income definition, methodology) and are **never combined**: there is no average, no blended series and no
re-harmonised figure. Series released only after the date are listed as ``unavailable_by_as_of``; a source with no
series for the place is listed under ``none_on_record``.

:meth:`IncomeQueries.series_history` returns every vintage of one series with its release date and label, the
periods each release added, revised or dropped (values before and after as published), PPP revisions, definition
changes and removals by the source, and for each consecutive pair the comparability notes that apply (PPP revision,
definition change, source-stated breaks, recorded notes). A pair without any note is ``comparability_unknown``.
Every vintage is cited.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.ingestion.income_distribution_sources import (
    CONCEPTS,
    NEVER_SENTENCE,
    PROVIDERS,
)
from src.kb.income_distribution_records import (
    ANSWER_CONTRACT,
    EXCLUSIONS,
    MINIMISATION,
    READ_SCOPE,
    IncomeError,
    authorize,
    iso,
    to_ms,
)
from src.kb.income_distribution_store import IncomeDistributionStore, citation

HISTORY_CONTRACT = "noesis-income-distribution-history-v1"
GROUP_FIELDS = ("provider", "welfare_concept", "equivalence_scale", "poverty_line", "ppp_base_year",
                "income_definition", "methodology")


def cutoff_ms(as_of: Any) -> int | None:
    """A date means the end of that day (released *by* the date); a time is used as given."""
    if as_of in (None, ""):
        return None
    if isinstance(as_of, (int, float)):
        return int(as_of)
    text = str(as_of).strip()
    return to_ms(text) + 86_399_999 if len(text) == 10 else to_ms(text)


def comparability_group(key: Mapping[str, Any]) -> str:
    line = key.get("poverty_line") or {}
    parts = [str(key.get("provider")), str(key.get("welfare_concept")), str(key.get("equivalence_scale")),
             f"line={line.get('basis')}:{line.get('amount')}" if line else "line=none",
             f"ppp={key.get('ppp_base_year')}", f"income={key.get('income_definition')}",
             f"method={key.get('methodology')}"]
    return " | ".join(parts)


class IncomeQueries:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = IncomeDistributionStore(conn, initialize=False, now=self.now)

    def _identity(self):
        from src.kb.income_distribution_identity import IncomeIdentity

        return IncomeIdentity(self.conn, initialize=False, now=self.now)

    def _area_codes(self, namespace: str, place_id: str | None, area: Mapping[str, Any] | None
                    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        identity = self._identity()
        if place_id:
            codes = [{**c, "basis": "accepted-match"} for c in identity.areas_for_place(namespace, place_id)]
            return codes, {"place_id": place_id}
        if not area or not area.get("scheme") or not area.get("code"):
            raise IncomeError("invalid_request", "name a place_id or an area with scheme and code")
        codes = [{"scheme": area["scheme"], "code": str(area["code"]), "basis": "published-code"}]
        accepted = identity.place_for_area(namespace, area["scheme"], str(area["code"]))
        if accepted:
            for other in identity.areas_for_place(namespace, accepted["place_id"]):
                if (other["scheme"], other["code"]) != (area["scheme"], str(area["code"])):
                    codes.append({**other, "basis": "accepted-match"})
        return codes, {"area": dict(area), "place_id": accepted["place_id"] if accepted else None}

    def indicator_for_place(self, namespace: str, *, scopes: Iterable[str], concept: str, as_of: Any = None,
                            place_id: str | None = None, area: Mapping[str, Any] | None = None,
                            reference_year: str | None = None, providers: Iterable[str] | None = None,
                            enabled_providers: Iterable[str] | None = None) -> dict[str, Any]:
        """Each source's figure for a place as released by a date, side by side with definitions; never combined."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if concept not in CONCEPTS:
            raise IncomeError("invalid_request", f"concept is one of {CONCEPTS}")
        cutoff = cutoff_ms(as_of)
        codes, subject = self._area_codes(namespace, place_id, area)
        wanted = set(providers or PROVIDERS)
        enabled = set(enabled_providers if enabled_providers is not None else PROVIDERS)
        series = self.store.find_series(namespace, concept=concept,
                                        area_codes=[(c["scheme"], c["code"]) for c in codes]) if codes else []
        bases = {(c["scheme"], c["code"]): c["basis"] for c in codes}
        identity = self._identity()
        rows, unavailable, groups = [], [], {}
        for item in series:
            if item["provider"] not in wanted or item["provider"] not in enabled:
                continue
            answer = self.store.values(namespace, item["series_id"], as_of_ms=cutoff)
            if answer["status"] == "unavailable":
                first = self.store.vintage_rows(namespace, item["series_id"])
                unavailable.append({"series_id": item["series_id"], "provider": item["provider"],
                                    "reason": answer["reason"],
                                    "first_release_at": first[0]["release_at"] if first else None})
                continue
            observations = answer["observations"]
            if reference_year:
                observations = [o for o in observations if o["period"] == str(reference_year)]
            group = comparability_group(item["key"])
            groups.setdefault(group, []).append(item["series_id"])
            rows.append({
                "series_id": item["series_id"], "provider": item["provider"], "native_key": item["native_key"],
                "indicator": item["indicator"], "area": item["area"],
                "area_basis": bases.get((item["area"]["scheme"], str(item["area"]["code"]))),
                "welfare_concept": item["key"]["welfare_concept"],
                "equivalence_scale": item["key"]["equivalence_scale"],
                "poverty_line": item["key"]["poverty_line"], "ppp_base_year": item["key"]["ppp_base_year"],
                "survey": item["key"]["survey"], "income_definition": item["key"]["income_definition"],
                "methodology": item["key"]["methodology"], "coverage": item["key"]["coverage"],
                "unit": item["unit"], "comparability_group": group, "status": answer["status"],
                "definition": answer.get("definition"), "observations": observations,
                "vintage": {k: answer["vintage"][k] for k in ("vintage_id", "release_label", "release_at",
                                                             "retrieved_at", "ppp", "dataflow_version", "status")},
                "citation": answer["citation"],
                "comparability_notes": self.store.notes(namespace, item["series_id"]),
                "related_series": [a["subject"]["series_ids"] for a in identity.related(namespace, item["series_id"])],
            })
        rows.sort(key=lambda r: (r["provider"], r["comparability_group"], r["native_key"]))
        present = {r["provider"] for r in rows} | {u["provider"] for u in unavailable}
        none_on_record = [{"provider": p, "concept": concept, "codes": [(c["scheme"], c["code"]) for c in codes]}
                          for p in sorted(wanted & enabled) if p not in present]
        disabled = sorted(wanted - enabled)
        return {
            "contract": ANSWER_CONTRACT, "namespace": namespace, "query": {**subject, "concept": concept,
                                                                          "as_of": as_of,
                                                                          "reference_year": reference_year},
            "as_of": iso(cutoff) if cutoff is not None else None, "area_codes": codes, "results": rows,
            "comparability_groups": groups, "unavailable_by_as_of": unavailable, "none_on_record": none_on_record,
            "features_disabled": [{"provider": p, "reason": "optional feature not selected"} for p in disabled],
            "combined_value": None,
            "never_combined": "rows with different sources, poverty lines, PPP rounds, welfare concepts, equivalence "
                              "scales or methodologies are separate series and are never combined",
            "unmatched_place": not codes, "statement": NEVER_SENTENCE, "exclusions": list(EXCLUSIONS),
            "minimisation": MINIMISATION["decision"],
        }

    def series_history(self, namespace: str, series_id: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        """Every vintage of a series, what each release changed, and comparability per consecutive pair."""
        authorize(namespace, scopes, READ_SCOPE)
        series = self.store.series(namespace, series_id)
        notes = self.store.notes(namespace, series_id)
        vintages, pairs = [], []
        previous = None
        for vintage in self.store.vintage_rows(namespace, series_id):
            release = self.store.release(namespace, vintage["release_id"])
            changes = vintage["changes"]
            entry = {
                "vintage_id": vintage["vintage_id"], "sequence": vintage["sequence"], "status": vintage["status"],
                "release_label": vintage["release_label"], "release_at": vintage["release_at"],
                "release_basis": vintage["release_basis"], "retrieved_at": vintage["retrieved_at"],
                "ppp": vintage["ppp"], "dataflow_version": vintage["dataflow_version"],
                "definition_id": vintage["definition_id"], "new_periods": changes.get("new_periods", []),
                "changed_periods": [r["period"] for r in changes.get("revised", [])],
                "revisions": changes.get("revised", []), "dropped_periods": changes.get("dropped_periods", []),
                "ppp_revision": changes.get("ppp_revision"), "definition_change": changes.get("definition_change"),
                "removed_by_source": changes.get("removed_by_source"),
                "citation": citation(series, vintage, release),
            }
            vintages.append(entry)
            if previous is not None:
                pairs.append(self._pair(previous, entry, notes))
            previous = entry
        return {"contract": HISTORY_CONTRACT, "namespace": namespace, "series": series, "vintages": vintages,
                "pairs": pairs, "series_notes": notes, "statement": NEVER_SENTENCE, "exclusions": list(EXCLUSIONS)}

    @staticmethod
    def _pair(before: Mapping[str, Any], after: Mapping[str, Any], notes: list[dict[str, Any]]) -> dict[str, Any]:
        stated = []
        if after["ppp_revision"]:
            stated.append({"kind": "ppp_revision", "statement": "the release restates values under PPP revision "
                           f"{after['ppp_revision']['after'].get('revision')} (was "
                           f"{after['ppp_revision']['before'].get('revision')}) of the "
                           f"{after['ppp_revision']['after'].get('base_year')} PPP round"})
        if after["definition_change"]:
            stated.append({"kind": "definition_change", "statement": "the definition or dataflow version changed",
                           "detail": after["definition_change"]})
        if after["removed_by_source"]:
            stated.append({"kind": "removed_by_source", "statement": after["removed_by_source"]["statement"]})
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


__all__ = ["HISTORY_CONTRACT", "IncomeQueries", "comparability_group", "cutoff_ms"]
