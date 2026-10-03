"""Answers: a tourism indicator for a place as of a release, and a series' history with comparability notes.

Track #2739 (TO07, TO08).

:meth:`TourismQueries.indicator_for_place` takes a place (a Geospatial place id, read through accepted TO05 matches
only, or a published place key ``{scheme, code, nuts_version}``), an optional indicator concept, frequency, residence
and period, and a date. It returns **one row per series**: the values from the vintage released by that date (the
vintage whose release clock is on or before the date) with the definition (including the national establishment-size
threshold), flags verbatim, confidential cells as their status, source-stated notes and the cited vintage. Monthly and
annual series are separate rows, grouped by frequency, and never combined: an annual period is never answered from
months, and a monthly period is never answered from a year. A place key under another NUTS version is reached only
through an **accepted** NUTS correspondence link, and such rows say so. UN Tourism is reported ``not-implemented``,
never as an empty answer.

:meth:`TourismQueries.series_history` returns every vintage of one series with its release date and basis, the periods
each release added, revised or dropped (values and flags before and after as published, provisional-to-revised months
included), definition changes and removals by the source, and for each consecutive pair the source-stated
comparability notes that apply (breaks ``b``, definition differences ``d``, national establishment-threshold
deviations, NUTS version changes through the published correspondence); a pair without notes is
``comparability_unknown``. Every vintage is cited.

:meth:`TourismQueries.evidence_bundle` turns an answer into assertions that each cite source, record revision and
as-of time.

Nothing here nowcasts, fills a month or region, seasonally adjusts, blends sources, or computes an occupancy rate,
average, per-capita or per-bed figure.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from src.ingestion.tourism_sources import (
    CONCEPTS,
    FREQUENCIES,
    NEVER_SENTENCE,
    NOT_IMPLEMENTED,
    NOT_IMPLEMENTED_ANSWER,
    PROVIDERS,
    RESIDENCES,
    period_matches,
)
from src.kb.tourism_records import (
    ANSWER_CONTRACT,
    EXCLUSIONS,
    MINIMISATION,
    READ_SCOPE,
    TourismError,
    authorize,
    comparability_basis,
    iso,
    table_exists,
    to_ms,
)
from src.kb.tourism_store import TourismStore, citation

HISTORY_CONTRACT = "noesis-tourism-statistics-history-v1"
BUNDLE_CONTRACT = "noesis-tourism-evidence-bundle-v1"
NEVER_COMBINED = ("monthly and annual series, residence groups, accommodation types, units and NUTS versions are "
                  "separate series and are never combined; an annual total is never computed from months")


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


class TourismQueries:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = TourismStore(conn, initialize=False, now=self.now)

    def _identity(self):
        from src.kb.tourism_identity import TourismIdentity

        return TourismIdentity(self.conn, initialize=False, now=self.now)

    def _place_keys(self, namespace: str, place: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        identity = self._identity()
        held = table_exists(self.conn, "tourism_identity_assertions")
        if isinstance(place, str):
            keys = [{**k, "basis": "accepted-match"} for k in identity.area_keys_for_place(namespace, place)] \
                if held else []
            return keys, {"place_id": place}
        area = dict(place or {})
        if area.get("scheme") != "eurostat-geo" or not area.get("code"):
            raise TourismError("invalid_request", "name a place id or a place key {scheme: eurostat-geo, code, "
                                                  "nuts_version}")
        if not area.get("nuts_version"):
            # Without a version every stated version of the code answers, each as its own key and its own rows.
            versions = sorted({s["area"]["nuts_version"] for s in self.store.find_series(
                namespace, area_codes=[("eurostat-geo", str(area["code"]), None)])})
            return ([{"scheme": "eurostat-geo", "code": str(area["code"]), "nuts_version": v,
                      "basis": "published-code (NUTS version not requested; each version is its own key)"}
                     for v in versions], {"area": area})
        key = {"scheme": "eurostat-geo", "code": str(area["code"]), "nuts_version": str(area["nuts_version"])}
        keys = [{**key, "basis": "published-code"}]
        accepted = identity.place_for_area(namespace, key) if held else None
        if held:
            for other in identity.corresponding_keys(namespace, key):
                keys.append({k: other[k] for k in ("scheme", "code", "nuts_version")} | {
                    "basis": f"accepted NUTS correspondence ({other['relation']})",
                    "assertion_id": other["assertion_id"]})
        return keys, {"area": area, "place_id": accepted["place_id"] if accepted else None}

    def indicator_for_place(self, namespace: str, *, scopes: Iterable[str], place: Any,
                            concept: str | None = None, frequency: str | None = None, residence: str | None = None,
                            period: str | None = None, as_of: Any = None, period_from: str | None = None,
                            period_to: str | None = None, providers: Iterable[str] | None = None,
                            enabled_providers: Iterable[str] | None = None) -> dict[str, Any]:
        """A place's tourism figures as released by a date, one row per series, monthly and annual kept apart."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if concept is not None and concept not in CONCEPTS:
            raise TourismError("invalid_request", f"concept is one of {CONCEPTS}")
        if frequency is not None and frequency not in FREQUENCIES:
            raise TourismError("invalid_request", f"frequency is one of {FREQUENCIES}")
        if residence is not None and residence not in RESIDENCES:
            raise TourismError("invalid_request", f"residence is one of {sorted(RESIDENCES)}")
        wanted = set(providers or (*PROVIDERS, *NOT_IMPLEMENTED))
        not_implemented = [dict(NOT_IMPLEMENTED_ANSWER) for p in NOT_IMPLEMENTED if p in wanted]
        unknown = wanted - set(PROVIDERS) - set(NOT_IMPLEMENTED)
        if unknown:
            raise TourismError("invalid_request", f"providers are among {PROVIDERS + NOT_IMPLEMENTED}")
        cutoff = cutoff_ms(as_of)
        keys, subject = self._place_keys(namespace, place)
        enabled = set(enabled_providers if enabled_providers is not None else PROVIDERS)
        bases = {(k["code"], k["nuts_version"]): k["basis"] for k in keys}
        candidates = self.store.find_series(namespace, concept=concept, frequency=frequency, area_codes=[
            (k["scheme"], k["code"], k["nuts_version"]) for k in keys]) if keys else []
        rows, unavailable, other_frequency = [], [], []
        by_frequency: dict[str, list[str]] = {f: [] for f in FREQUENCIES}
        for series in candidates:
            if series["provider"] not in wanted or series["provider"] not in enabled:
                continue
            if residence is not None and series["residence"]["code"] != residence:
                continue
            if period is not None and not period_matches(period, series["frequency"]):
                other_frequency.append({
                    "series_id": series["series_id"], "frequency": series["frequency"],
                    "reason": f"the series states {series['frequency']} periods; {period!r} is never computed from "
                              "or split into them"})
                continue
            answer = self.store.values(namespace, series["series_id"], as_of_ms=cutoff)
            if answer["status"] == "unavailable":
                first = self.store.vintage_rows(namespace, series["series_id"])
                unavailable.append({"series_id": series["series_id"], "provider": series["provider"],
                                    "native_key": series["native_key"], "reason": answer["reason"],
                                    "first_release_at": first[0]["release_at"] if first else None})
                continue
            stated = [o["period"] for o in answer["observations"]]
            observations = [o for o in answer["observations"]
                            if (period is None or o["period"] == period)
                            and (not period_from or o["period"] >= period_from)
                            and (not period_to or o["period"][:len(period_to)] <= period_to)]
            by_frequency[series["frequency"]].append(series["series_id"])
            definition = answer.get("definition")
            rows.append({
                "series_id": series["series_id"], "provider": series["provider"], "dataset": series["dataset"],
                "native_key": series["native_key"], "indicator": series["indicator"],
                "residence": series["residence"], "accommodation": series["accommodation"], "area": series["area"],
                "area_basis": bases.get((series["area"]["code"], series["area"]["nuts_version"])),
                "unit": series["unit"], "frequency": series["frequency"], "status": answer["status"],
                "definition": definition,
                "coverage_threshold": (definition or {}).get("content", {}).get("coverage_threshold"),
                "values": [{**o, "citation_vintage_id": answer["vintage"]["vintage_id"]} for o in observations],
                "period_stated": None if period is None else bool(observations),
                "gaps": {"missing_periods": missing_periods(stated, series["frequency"]) if stated else [],
                         "confidential": [{"period": o["period"], "status": o["status"], "flags": o["flags"]}
                                          for o in answer["observations"] if o["status"] != "reported"],
                         "latest_published_period": max(stated) if stated else None,
                         "note": "missing and confidential periods are not filled, nowcast or reconstructed"},
                "vintage": {k: answer["vintage"][k] for k in ("vintage_id", "status", "release_label", "release_at",
                                                              "release_basis", "retrieved_at", "dataflow_version",
                                                              "revision_of")},
                "citation": answer["citation"],
                "notes": [{k: n[k] for k in ("note_id", "relation", "statement", "periods", "state", "origin",
                                              "other_series_id")} for n in self.store.notes(namespace,
                                                                                             series["series_id"])],
            })
        rows.sort(key=lambda r: (r["frequency"], r["indicator"]["concept"], r["dataset"], r["area"]["nuts_version"],
                                 r["native_key"]))
        present = {r["provider"] for r in rows} | {u["provider"] for u in unavailable}
        status = "reported" if rows else "none_published"
        if not rows and not unavailable and wanted <= set(NOT_IMPLEMENTED):
            status = "not-implemented"
        return {
            "contract": ANSWER_CONTRACT, "namespace": namespace,
            "query": {**subject, "concept": concept, "frequency": frequency, "residence": residence,
                      "period": period, "as_of": as_of, "period_from": period_from, "period_to": period_to},
            "as_of": iso(cutoff) if cutoff is not None else None, "place_keys": keys, "status": status,
            "results": rows, "by_frequency": {f: ids for f, ids in by_frequency.items() if ids},
            "other_frequency": other_frequency, "comparability": self._pairs(rows),
            "unavailable_by_as_of": unavailable,
            "none_on_record": [{"provider": p, "place_keys": [(k["code"], k["nuts_version"]) for k in keys]}
                               for p in sorted((wanted & enabled) - set(NOT_IMPLEMENTED)) if p not in present],
            "not_implemented": not_implemented,
            "features_disabled": [{"provider": p, "reason": "optional feature not selected"}
                                  for p in sorted((wanted - enabled) - set(NOT_IMPLEMENTED))],
            "unmatched_place": not keys, "never_combined": NEVER_COMBINED, "statement": NEVER_SENTENCE,
            "exclusions": list(EXCLUSIONS), "minimisation": MINIMISATION["decision"],
        }

    @staticmethod
    def _pairs(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        pairs = []
        for i, left in enumerate(rows):
            for right in rows[i + 1:]:
                if left["indicator"]["concept"] != right["indicator"]["concept"]:
                    continue
                differences = comparability_basis(left, right)
                pairs.append({"series": [left["series_id"], right["series_id"]],
                              "recorded_differences": differences,
                              "status": "noted" if differences else "comparability_unknown",
                              "combined": False})
        return pairs

    def series_history(self, namespace: str, series_id: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        """Every vintage of a series, what each release changed, and comparability per consecutive pair."""
        authorize(namespace, scopes, READ_SCOPE)
        series = self.store.series(namespace, series_id)
        notes = self.store.notes(namespace, series_id)
        nuts_links = []
        if table_exists(self.conn, "tourism_identity_assertions"):
            identity = self._identity()
            for state in ("accepted", "proposed"):
                for link in identity.corresponding_keys(namespace, series["area"], state=state):
                    successors = [s["series_id"] for s in self.store.find_series(
                        namespace, area_codes=[(link["scheme"], link["code"], link["nuts_version"])])
                        if s["dataset"] == series["dataset"] and s["indicator"]["code"] == series["indicator"]["code"]
                        and s["residence"]["code"] == series["residence"]["code"]
                        and s["accommodation"]["code"] == series["accommodation"]["code"]]
                    nuts_links.append({
                        "kind": "nuts_version_change", "assertion_id": link["assertion_id"], "state": link["state"],
                        "relation": link["relation"], "other_key": {k: link[k] for k in ("scheme", "nuts_version",
                                                                                         "code")},
                        "other_series_ids": successors, "citation": link["citation"],
                        "statement": f"NUTS {series['area']['nuts_version']} {series['area']['code']} and NUTS "
                                     f"{link['nuts_version']} {link['code']} are different place keys, linked only "
                                     f"through Eurostat's published correspondence ({link['relation']}); the series "
                                     "stay separate"})
        vintages, pairs, previous = [], [], None
        for vintage in self.store.vintage_rows(namespace, series_id):
            release = self.store.release(namespace, vintage["release_id"])
            changes = vintage["changes"]
            definition = self.store.definition(namespace, vintage["definition_id"]) or {"content": {}}
            entry = {
                "vintage_id": vintage["vintage_id"], "sequence": vintage["sequence"], "status": vintage["status"],
                "release_label": vintage["release_label"], "release_at": vintage["release_at"],
                "release_basis": vintage["release_basis"], "retrieved_at": vintage["retrieved_at"],
                "revision_of": vintage["revision_of"], "dataflow_version": vintage["dataflow_version"],
                "definition_id": vintage["definition_id"],
                "coverage_threshold": definition["content"].get("coverage_threshold"),
                "new_periods": changes.get("new_periods", []),
                "changed_periods": [r["period"] for r in changes.get("revised", [])],
                "revisions": changes.get("revised", []), "dropped_periods": changes.get("dropped_periods", []),
                "definition_change": changes.get("definition_change"),
                "removed_by_source": changes.get("removed_by_source"),
                "observations": self.store.observations(namespace, vintage["vintage_id"]),
                "citation": citation(series, vintage, release),
            }
            vintages.append(entry)
            if previous is not None:
                pairs.append(self._history_pair(previous, entry, notes, nuts_links))
            previous = entry
        return {"contract": HISTORY_CONTRACT, "namespace": namespace, "series": series, "vintages": vintages,
                "pairs": pairs, "series_notes": notes, "nuts_version_links": nuts_links,
                "statement": NEVER_SENTENCE, "exclusions": list(EXCLUSIONS),
                "note": "every retained vintage; earlier values are never overwritten, adjusted or filled"}

    @staticmethod
    def _history_pair(before: Mapping[str, Any], after: Mapping[str, Any], notes: list[dict[str, Any]],
                      nuts_links: list[dict[str, Any]]) -> dict[str, Any]:
        stated = []
        if after["definition_change"]:
            stated.append({"kind": "definition_change", "statement": "the definition or dataflow version changed",
                           "detail": after["definition_change"]})
            if "coverage_threshold" in after["definition_change"].get("changed_fields", []) or \
                    "coverage_thresholds" in after["definition_change"].get("changed_fields", []):
                stated.append({"kind": "threshold_deviation", "before": before["coverage_threshold"],
                               "after": after["coverage_threshold"],
                               "statement": "the establishment-size threshold the source states changed"})
        if after["removed_by_source"]:
            stated.append({"kind": "removed_by_source", "statement": after["removed_by_source"]["statement"]})
            stated += [{k: link[k] for k in ("kind", "assertion_id", "state", "relation", "statement",
                                             "other_series_ids")} for link in nuts_links]
        confirmed = [r["period"] for r in after["revisions"]
                     if "p" in str(dict(r["before"].get("flags") or {}).get("OBS_FLAG", ""))
                     and "p" not in str(dict(r["after"].get("flags") or {}).get("OBS_FLAG", ""))]
        if confirmed:
            stated.append({"kind": "provisional_revised", "periods": confirmed,
                           "statement": "values the earlier release marked provisional (p) are restated without the "
                                        "flag"})
        touched = set(after["changed_periods"]) | set(after["new_periods"])
        for note in notes:
            if note["relation"] in {"break_in_series", "definition_differs"} and (
                    not note["periods"] or touched & set(note["periods"])):
                stated.append({"kind": note["relation"], "note_id": note["note_id"], "statement": note["statement"],
                               "periods": note["periods"], "origin": note["origin"]})
            elif note["origin"] == "recorded" and note["state"] == "accepted" and not note["other_series_id"]:
                stated.append({"kind": note["relation"], "note_id": note["note_id"], "statement": note["statement"]})
        return {"from_vintage_id": before["vintage_id"], "to_vintage_id": after["vintage_id"],
                "from_release_at": before["release_at"], "to_release_at": after["release_at"],
                "changed_periods": after["changed_periods"], "new_periods": after["new_periods"],
                "citations": [before["citation"], after["citation"]],
                "comparability": "noted" if stated else "comparability_unknown", "notes": stated}

    @staticmethod
    def evidence_bundle(answer: Mapping[str, Any]) -> dict[str, Any]:
        """Assertions each citing source, record revision (vintage) and as-of time of the release behind it."""
        bibliography: dict[str, dict[str, Any]] = {}
        assertions = []
        for row in answer.get("results") or []:
            cited = row["citation"]
            bibliography.setdefault(cited["release_id"], {
                "id": cited["release_id"],
                "text": f"{cited['provider']} {cited['dataset']} ({cited.get('release_label')}, released "
                        f"{cited['as_of']}, retrieved {cited['retrieved_at']}, {cited['evidence_origin']} evidence, "
                        f"{cited['live_verification']}), {cited.get('url')}; Eurostat, reuse with acknowledgement"})
            for value in row["values"]:
                shown = value["value_text"] if value["status"] == "reported" else f"[{value['status']}]"
                flags = value["flags"].get("OBS_FLAG")
                assertions.append({
                    "id": f"{row['series_id']}:{row['vintage']['vintage_id']}:{value['period']}",
                    "text": f"{row['indicator']['label']} ({row['residence']['label']}, "
                            f"{row['accommodation']['code']}, {row['unit']['label']}), {row['area'].get('label') or ''}"
                            f" {row['area']['code']} (NUTS {row['area']['nuts_version']}), {row['frequency']} "
                            f"{value['period']}: {shown}" + (f" [OBS_FLAG {flags}]" if flags else ""),
                    "kind": "sourced",
                    "dependencies": [{"kind": "source", "namespace": answer.get("namespace"),
                                      "id": row["series_id"], "revision": row["vintage"]["vintage_id"],
                                      "as_of": row["vintage"]["release_at"]}],
                    "citations": [cited["release_id"]],
                    "source": cited["provider"],
                })
        title = "tourism indicator for place"
        return {"contract": BUNDLE_CONTRACT,
                "sections": [{"id": title, "title": f"{title} as of {answer.get('as_of') or 'latest'}",
                              "assertions": assertions}],
                "bibliography": list(bibliography.values()), "not_implemented": answer.get("not_implemented", []),
                "exclusions": list(EXCLUSIONS)}


__all__ = ["BUNDLE_CONTRACT", "HISTORY_CONTRACT", "NEVER_COMBINED", "TourismQueries", "cutoff_ms",
           "missing_periods"]
