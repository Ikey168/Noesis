"""Answers: wealth distribution figures for a place as of a release, and a series' history (#2882).

:meth:`WealthQueries.indicator_for_place` takes a place as a published area code (ISO 3166-1 alpha-2, the scheme every
implemented source uses), an optional indicator (``wealth_share``, ``wealth_median``, ...), optional wealth groups as
published and a date, and returns **one row per series**: each source's values as released by that date (the vintage
whose release clock is on or before the date) with the definition, wealth concept, group, unit, population, method
and the cited vintage. WID, DWA and DFA rows are **never combined** (no average, no blended series, no share, ratio
or top share of our own). Every pair of rows from different sources states its comparability: ``different_indicator``
when the indicators differ, else the source-stated or reviewer-recorded notes that relate the two, else
``comparability_unknown``.

:meth:`WealthQueries.series_history` returns every vintage of a series with its release date and label, the periods
each release added, revised or dropped (values, statuses and flags before and after as published), method and
definition changes, model estimates the source later replaced and removals by the source, and for each consecutive
pair the notes that apply. A pair without any note is ``comparability_unknown``. Every vintage is cited.

:func:`place_profile` gathers the indicators for a place; :func:`export_profile` renders it as a
``noesis-evidence-bundle-v1`` in which every item is cited with source, record revision (vintage) and as-of time.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.ingestion.wealth_distribution_sources import (
    AREA_SCHEMES,
    INDICATOR_MEANINGS,
    INDICATORS,
    KEY_FIELDS,
    NEVER_SENTENCE,
    PROVIDERS,
)
from src.kb.wealth_distribution_records import (
    ANSWER_CONTRACT,
    EXCLUSIONS,
    MINIMISATION,
    READ_SCOPE,
    WealthError,
    authorize,
    comparability_basis,
    cutoff_ms,
    digest,
    iso,
)
from src.kb.wealth_distribution_store import WealthStore, citation

HISTORY_CONTRACT = "noesis-wealth-distribution-history-v1"
PROFILE_CONTRACT = "noesis-wealth-distribution-profile-v1"
NEVER_COMBINED = ("WID, ECB DWA and Fed DFA rows are separate series with their own wealth concepts, groups, "
                  "population units and methods and are never combined, averaged or re-harmonised")
MODEL_STATUSES = {"provisional", "estimated", "extrapolated", "projected"}


class WealthQueries:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = WealthStore(conn, initialize=False, now=self.now)

    def indicator_for_place(self, namespace: str, *, scopes: Iterable[str], area: Mapping[str, Any],
                            as_of: Any = None, indicator: str | None = None, groups: Iterable[str] | None = None,
                            reference_period: str | None = None, providers: Iterable[str] | None = None,
                            enabled_providers: Iterable[str] | None = None) -> dict[str, Any]:
        """Each source's values for a place as released by a date, side by side with definitions; never combined."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if indicator is not None and indicator not in INDICATORS:
            raise WealthError("invalid_request", f"indicator is one of {INDICATORS}")
        if not area or area.get("scheme") not in AREA_SCHEMES or not area.get("code"):
            raise WealthError("invalid_request", f"name an area with scheme ({AREA_SCHEMES}) and code")
        cutoff = cutoff_ms(as_of)
        code = (str(area["scheme"]), str(area["code"]).upper())
        wanted = set(providers or PROVIDERS)
        enabled = set(enabled_providers if enabled_providers is not None else PROVIDERS)
        series = self.store.find_series(namespace, indicator=indicator, area_codes=[code],
                                        groups=list(groups) if groups is not None else None)
        rows, unavailable = [], []
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
            if reference_period:
                observations = [o for o in observations if o["period"] == str(reference_period)]
            rows.append({
                "series_id": item["series_id"], "provider": item["provider"], "dataset": item["dataset"],
                "native_key": item["native_key"], **{field: item[field] for field in KEY_FIELDS},
                "indicator_meaning": INDICATOR_MEANINGS[item["indicator"]["concept"]],
                "area": item["area"], "frequency": item["frequency"], "status": answer["status"],
                "definition": answer.get("definition"), "observations": observations,
                "model_based": str(item["method"].get("code", "")).startswith("model-based"),
                "vintage": {k: answer["vintage"][k] for k in ("vintage_id", "release_label", "edition", "release_at",
                                                             "retrieved_at", "definition_id", "status")},
                "citation": answer["citation"],
                "comparability_notes": self.store.notes(namespace, item["series_id"]),
            })
        rows.sort(key=lambda r: (r["indicator"]["concept"], r["provider"], r["native_key"]))
        indicator_groups: dict[str, dict[str, Any]] = {}
        for row in rows:
            concept = row["indicator"]["concept"]
            indicator_groups.setdefault(concept, {"meaning": INDICATOR_MEANINGS[concept], "series_ids": []})[
                "series_ids"].append(row["series_id"])
        present = {r["provider"] for r in rows} | {u["provider"] for u in unavailable}
        none_on_record = [{"provider": p, "indicator": indicator, "area": dict(area)}
                          for p in sorted(wanted & enabled) if p not in present]
        return {
            "contract": ANSWER_CONTRACT, "namespace": namespace,
            "query": {"area": dict(area), "indicator": indicator, "groups": list(groups) if groups else None,
                      "as_of": as_of, "reference_period": reference_period},
            "as_of": iso(cutoff) if cutoff is not None else None, "results": rows,
            "indicator_groups": indicator_groups, "pairs": self._pairs(rows), "unavailable_by_as_of": unavailable,
            "none_on_record": none_on_record,
            "features_disabled": [{"provider": p, "reason": "optional feature not selected"}
                                  for p in sorted(wanted - enabled)],
            "nothing_combined": True, "never_combined": NEVER_COMBINED, "statement": NEVER_SENTENCE,
            "exclusions": list(EXCLUSIONS), "minimisation": MINIMISATION["decision"],
        }

    @staticmethod
    def _pairs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Per pair of rows from different sources: different indicators, else the notes relating them, else unknown."""
        pairs = []
        keys = ("provider", "area", "frequency", *KEY_FIELDS)
        for index, left in enumerate(rows):
            for right in rows[index + 1:]:
                if left["provider"] == right["provider"]:
                    continue
                entry = {"series_ids": [left["series_id"], right["series_id"]],
                         "providers": [left["provider"], right["provider"]],
                         "differences": comparability_basis({k: left.get(k) for k in keys},
                                                            {k: right.get(k) for k in keys})}
                if left["indicator"]["concept"] != right["indicator"]["concept"]:
                    pairs.append({**entry, "comparability": "different_indicator", "notes": [],
                                  "statement": "different indicators are never presented as measuring the same "
                                               "thing"})
                    continue
                notes = [{"note_id": n["note_id"], "relation": n["relation"], "statement": n["statement"],
                          "origin": n["origin"], "cited": n["cited"]}
                         for row, other in ((left, right), (right, left)) for n in row["comparability_notes"]
                         if n["relation"] == "scope_difference"
                         and any(c.get("relates_to") == other["provider"] for c in n["cited"])]
                notes += [{"note_id": n["note_id"], "relation": n["relation"], "statement": n["statement"],
                           "origin": n["origin"], "state": n["state"], "cited": n["cited"]}
                          for n in left["comparability_notes"]
                          if {n.get("series_id"), n.get("other_series_id")} == {left["series_id"], right["series_id"]}]
                pairs.append({**entry, "comparability": "noted" if notes else "comparability_unknown",
                              "notes": notes})
        return pairs

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
            revisions = changes.get("revised", [])
            entry = {
                "vintage_id": vintage["vintage_id"], "sequence": vintage["sequence"], "status": vintage["status"],
                "release_label": vintage["release_label"], "edition": vintage["edition"],
                "release_at": vintage["release_at"], "release_basis": vintage["release_basis"],
                "retrieved_at": vintage["retrieved_at"], "definition_id": vintage["definition_id"],
                "new_periods": changes.get("new_periods", []),
                "changed_periods": [r["period"] for r in revisions], "revisions": revisions,
                "dropped_periods": changes.get("dropped_periods", []),
                "estimates_replaced": [r["period"] for r in revisions
                                       if r["before"].get("publication_status") in MODEL_STATUSES
                                       and r["after"].get("publication_status") not in MODEL_STATUSES],
                "definition_change": changes.get("definition_change"),
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
        change = after["definition_change"]
        if change:
            if change.get("method_statement_before") != change.get("method_statement_after"):
                stated.append({"kind": "method_change",
                               "statement": "the source's method statement changed from "
                                            f"{change.get('method_statement_before')!r} to "
                                            f"{change.get('method_statement_after')!r}", "detail": change})
            else:
                stated.append({"kind": "definition_change", "statement": "the source's definition changed",
                               "detail": change})
        if after["estimates_replaced"]:
            stated.append({"kind": "estimate_replaced",
                           "statement": "the source replaced its estimated, provisional or extrapolated values for "
                                        f"{', '.join(after['estimates_replaced'])}",
                           "periods": after["estimates_replaced"]})
        if after["removed_by_source"]:
            stated.append({"kind": "removed_by_source", "statement": after["removed_by_source"]["statement"]})
        touched = set(after["changed_periods"]) | set(after["new_periods"])
        for note in notes:
            if note["other_series_id"]:
                continue
            if note["relation"] == "break_in_series" and (not note["periods"] or touched & set(note["periods"])):
                stated.append({"kind": note["relation"], "note_id": note["note_id"], "statement": note["statement"],
                               "periods": note["periods"], "origin": note["origin"]})
            elif note["origin"] == "recorded" and note["state"] == "accepted":
                stated.append({"kind": note["relation"], "note_id": note["note_id"], "statement": note["statement"]})
        return {"from_vintage_id": before["vintage_id"], "to_vintage_id": after["vintage_id"],
                "from_release_at": before["release_at"], "to_release_at": after["release_at"],
                "changed_periods": after["changed_periods"], "new_periods": after["new_periods"],
                "comparability": "noted" if stated else "comparability_unknown", "notes": stated}


def place_profile(conn: Any, namespace: str, *, scopes: Iterable[str], area: Mapping[str, Any], as_of: Any = None,
                  enabled_providers: Iterable[str] | None = None) -> dict[str, Any]:
    """A place's wealth figures from each source side by side as of a date, grouped by indicator."""
    from src.kb.wealth_distribution_records import enabled_providers as selected

    providers = set(enabled_providers) if enabled_providers is not None else selected(conn)
    answer = WealthQueries(conn).indicator_for_place(namespace, scopes=set(scopes), area=area, as_of=as_of,
                                                     enabled_providers=providers)
    result = {"contract": PROFILE_CONTRACT, "namespace": namespace, "query": {"area": dict(area), "as_of": as_of},
              "as_of": answer["as_of"], "answer": answer, "none_on_record": answer["none_on_record"],
              "features_disabled": answer["features_disabled"], "never_combined": NEVER_COMBINED,
              "exclusions": list(EXCLUSIONS), "minimisation": MINIMISATION["decision"]}
    result["profile_hash"] = digest(result)
    return result


def export_profile(profile: Mapping[str, Any], *, created_at_ms: int | None = None) -> dict[str, Any]:
    """A ``noesis-evidence-bundle-v1``: every figure cited with source, record revision (vintage) and as-of time."""
    from src.evidence_bundle.builder import EvidenceBundleBuilder

    builder = EvidenceBundleBuilder("receipt", {"operation": PROFILE_CONTRACT, "query": profile["query"],
                                                "as_of": profile["as_of"]},
                                    created_at_ms=created_at_ms or 0, as_of_ms=cutoff_ms(profile["query"]["as_of"]))
    refs: list[str] = []
    for row in profile["answer"]["results"]:
        cite = row["citation"]
        object_id = f"wealth:{cite['series_id']}@{cite['vintage_id']}"
        builder.add_object("evidence", {
            # The bundle verifier reads a "method" key as an analytic output; the publisher's method is named
            # estimation_method here (the value is the source's, unchanged).
            "kind": "wealth-series-vintage",
            **{("estimation_method" if field == "method" else field): row[field] for field in KEY_FIELDS},
            "locator": {"cited": True, "document_id": cite["series_id"], "revision_id": cite["vintage_id"],
                        "url": cite.get("url")},
            "citation": {**cite, "source": cite["provider"], "record_revision": cite["vintage_id"]},
            "definition_id": row["vintage"]["definition_id"], "status": row["status"],
            "observations": [{k: o[k] for k in ("period", "value_text", "status", "publication_status", "flags")}
                             for o in row["observations"]]}, object_id=object_id)
        refs.append(object_id)
        if cite.get("url"):
            builder.add_external_reference(f"source:{cite['series_id']}:{cite['vintage_id']}", cite["url"],
                                           required=False)
    summary = {k: v for k, v in profile.items() if k != "answer"}
    summary["pairs"] = [{k: p[k] for k in ("series_ids", "providers", "comparability")}
                        for p in profile["answer"]["pairs"]]
    builder.add_object("receipt", {"kind": "wealth-profile", **summary},
                       object_id=f"wealth-profile:{profile['profile_hash'][:24]}", references=refs, root=True)
    for gap in profile.get("none_on_record") or []:
        builder.add_omission(f"none on record: {gap['provider']}")
    for disabled in profile.get("features_disabled") or []:
        builder.add_omission(f"source {disabled['provider']}: {disabled['reason']}")
    return builder.build()


__all__ = ["HISTORY_CONTRACT", "NEVER_COMBINED", "PROFILE_CONTRACT", "WealthQueries", "export_profile",
           "place_profile"]
