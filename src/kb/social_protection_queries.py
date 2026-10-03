"""Answers: a social protection indicator for a place as of a release, and a series' history (#2741, SS08, SS09).

:meth:`SocialProtectionQueries.indicator_for_place` takes a place (a Geospatial place id, read through accepted SS06
matches only, or a published area code), an optional measure (``expenditure``, ``beneficiaries``, ``coverage``), an
optional function in its publisher's own scheme and a date, and returns **one row per series**: each source's values
as released by that date (the vintage whose release clock is on or before the date) with the definition, the
publisher's classification, financing, cash or in kind, basis, sex, unit as published, population denominator and the
cited vintage. Another publisher's function is reached only through an accepted SS06 relation, and each row says how
it matched. Rows are grouped by **measure**: expenditure, beneficiary counts and coverage are never presented as
measuring the same thing. ESSPROS, SOCX and ILO rows are **never combined** (no average, no blended series, no
per-capita or share figure of our own); COFOG social-protection expenditure is listed only as cited links, never as
a value beside them. Pairs of rows state their source-stated scope notes, else ``comparability_unknown``.

:meth:`SocialProtectionQueries.series_history` returns every vintage of a series with its release date, label and
edition, the periods each release added, revised or dropped (values, statuses and flags before and after as
published), definition changes (including ESSPROS manual edition changes), report-edition restatements, OECD
estimates replaced by later figures and removals by the source, and for each consecutive pair the comparability notes
that apply. A pair without any note is ``comparability_unknown``. Every vintage is cited.

:func:`place_profile` gathers the three measures for a place with its links; :func:`export_profile` renders it as a
``noesis-evidence-bundle-v1`` in which every item is cited with source, record revision (vintage) and as-of time.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.ingestion.social_protection_sources import (
    KEY_FIELDS,
    MEASURE_MEANINGS,
    MEASURES,
    NEVER_SENTENCE,
    PROVIDERS,
)
from src.kb.social_protection_records import (
    ANSWER_CONTRACT,
    EXCLUSIONS,
    MINIMISATION,
    READ_SCOPE,
    SocialProtectionError,
    authorize,
    cutoff_ms,
    digest,
    iso,
    table_exists,
)
from src.kb.social_protection_store import SocialProtectionStore, citation

HISTORY_CONTRACT = "noesis-social-protection-history-v1"
PROFILE_CONTRACT = "noesis-social-protection-profile-v1"
NEVER_EQUATED = ("expenditure, beneficiary counts and coverage are different measures and are never presented as "
                 "measuring the same thing")
NEVER_COMBINED = ("ESSPROS, SOCX and ILO rows are separate series with their own definitions and classifications and "
                  "are never combined; COFOG social-protection expenditure is a distinct concept, shown only as "
                  "cited links")
PROVISIONAL_STATUSES = {"provisional", "estimated", "projected"}


class SocialProtectionQueries:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = SocialProtectionStore(conn, initialize=False, now=self.now)

    def _identity(self):
        from src.kb.social_protection_identity import SocialProtectionIdentity

        return SocialProtectionIdentity(self.conn, initialize=False, now=self.now)

    def _area_codes(self, namespace: str, place_id: str | None, area: Mapping[str, Any] | None
                    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        identity = self._identity()
        if place_id:
            codes = [{**c, "basis": "accepted-match"} for c in identity.areas_for_place(namespace, place_id)]
            return codes, {"place_id": place_id}
        if not area or not area.get("scheme") or not area.get("code"):
            raise SocialProtectionError("invalid_request", "name a place_id or an area with scheme and code")
        codes = [{"scheme": area["scheme"], "code": str(area["code"]), "basis": "published-code"}]
        accepted = identity.place_for_area(namespace, area["scheme"], str(area["code"]))
        if accepted:
            for other in identity.areas_for_place(namespace, accepted["place_id"]):
                if (other["scheme"], other["code"]) != (area["scheme"], str(area["code"])):
                    codes.append({**other, "basis": "accepted-match"})
        return codes, {"area": dict(area), "place_id": accepted["place_id"] if accepted else None}

    def _functions(self, namespace: str, function: Mapping[str, Any] | None) -> dict[tuple[str, str], dict] | None:
        if not function:
            return None
        scheme, code = str(function.get("scheme") or ""), str(function.get("code") or "")
        if scheme == "cofog":
            raise SocialProtectionError("distinct_concept", "COFOG functions are answered by economics.public-finance; "
                                                            "they are never a social protection function")
        wanted = {(scheme, code): {"basis": "native"}}
        for related in self._identity().related_functions(namespace, scheme, code):
            wanted.setdefault((related["scheme"], related["code"]),
                              {"basis": "accepted-related-function", "assertion_id": related["assertion_id"],
                               "relation": "related, not equal"})
        return wanted

    def indicator_for_place(self, namespace: str, *, scopes: Iterable[str], as_of: Any = None,
                            place_id: str | None = None, area: Mapping[str, Any] | None = None,
                            measure: str | None = None, function: Mapping[str, Any] | None = None,
                            reference_year: str | None = None, providers: Iterable[str] | None = None,
                            enabled_providers: Iterable[str] | None = None) -> dict[str, Any]:
        """Each source's values for a place as released by a date, side by side with definitions; never combined."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if measure is not None and measure not in MEASURES:
            raise SocialProtectionError("invalid_request", f"measure is one of {MEASURES}")
        cutoff = cutoff_ms(as_of)
        codes, subject = self._area_codes(namespace, place_id, area)
        functions = self._functions(namespace, function)
        wanted = set(providers or PROVIDERS)
        enabled = set(enabled_providers if enabled_providers is not None else PROVIDERS)
        series = self.store.find_series(namespace, measure=measure,
                                        area_codes=[(c["scheme"], c["code"]) for c in codes],
                                        functions=list(functions) if functions is not None else None) if codes else []
        bases = {(c["scheme"], c["code"]): c for c in codes}
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
            if reference_year:
                observations = [o for o in observations if o["period"] == str(reference_year)]
            function_key = (item["function"]["scheme"], str(item["function"]["code"]))
            rows.append({
                "series_id": item["series_id"], "provider": item["provider"], "dataset": item["dataset"],
                "native_key": item["native_key"], "measure": item["measure"],
                "measure_meaning": MEASURE_MEANINGS[item["measure"]["concept"]],
                **{field: item[field] for field in KEY_FIELDS},
                "function_basis": (functions or {}).get(function_key, {"basis": "native"}),
                "area": item["area"], "area_basis": bases.get((item["area"]["scheme"], str(item["area"]["code"]))),
                "population": item["population"], "status": answer["status"],
                "definition": answer.get("definition"), "observations": observations,
                "vintage": {k: answer["vintage"][k] for k in ("vintage_id", "release_label", "edition", "release_at",
                                                             "retrieved_at", "dataflow_version", "definition_id",
                                                             "status")},
                "citation": answer["citation"],
                "comparability_notes": self.store.notes(namespace, item["series_id"]),
            })
        rows.sort(key=lambda r: (r["measure"]["concept"], r["provider"], r["native_key"]))
        groups: dict[str, dict[str, Any]] = {}
        for row in rows:
            group = groups.setdefault(row["measure"]["concept"], {"meaning": MEASURE_MEANINGS[row["measure"]["concept"]],
                                                                 "series_ids": []})
            group["series_ids"].append(row["series_id"])
        present = {r["provider"] for r in rows} | {u["provider"] for u in unavailable}
        none_on_record = [{"provider": p, "measure": measure, "function": dict(function) if function else None,
                           "codes": [(c["scheme"], c["code"]) for c in codes]}
                          for p in sorted(wanted & enabled) if p not in present]
        return {
            "contract": ANSWER_CONTRACT, "namespace": namespace,
            "query": {**subject, "measure": measure, "function": dict(function) if function else None,
                      "as_of": as_of, "reference_year": reference_year},
            "as_of": iso(cutoff) if cutoff is not None else None, "area_codes": codes, "results": rows,
            "measure_groups": groups, "pairs": self._pairs(rows), "unavailable_by_as_of": unavailable,
            "none_on_record": none_on_record, "cofog_links": self._cofog_links(namespace, rows),
            "features_disabled": [{"provider": p, "reason": "optional feature not selected"}
                                  for p in sorted(wanted - enabled)],
            "nothing_combined": True, "never_equated": NEVER_EQUATED, "never_combined": NEVER_COMBINED,
            "unmatched_place": not codes, "statement": NEVER_SENTENCE, "exclusions": list(EXCLUSIONS),
            "minimisation": MINIMISATION["decision"],
        }

    @staticmethod
    def _pairs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Per pair of rows from different sources: different measures, else the stated scope notes, else unknown."""
        from src.kb.social_protection_records import comparability_basis

        pairs = []
        for index, left in enumerate(rows):
            for right in rows[index + 1:]:
                if left["provider"] == right["provider"]:
                    continue
                entry = {"series_ids": [left["series_id"], right["series_id"]],
                         "providers": [left["provider"], right["provider"]],
                         "differences": comparability_basis(
                             {k: left.get(k) for k in ("provider", "area", *KEY_FIELDS)} | {"measure": left["measure"]},
                             {k: right.get(k) for k in ("provider", "area", *KEY_FIELDS)} | {"measure": right["measure"]})}
                if left["measure"]["concept"] != right["measure"]["concept"]:
                    pairs.append({**entry, "comparability": "different_measure", "notes": [],
                                  "statement": NEVER_EQUATED})
                    continue
                notes = [{"note_id": n["note_id"], "relation": n["relation"], "statement": n["statement"],
                          "origin": n["origin"], "cited": n["cited"]}
                         for row, other in ((left, right), (right, left)) for n in row["comparability_notes"]
                         if n["relation"] == "scope_difference"
                         and any(c.get("relates_to") == other["provider"] for c in n["cited"])]
                notes += [{"note_id": n["note_id"], "relation": n["relation"], "statement": n["statement"],
                           "origin": n["origin"], "cited": n["cited"]}
                          for n in left["comparability_notes"] if n.get("other_series_id") == right["series_id"]]
                pairs.append({**entry, "comparability": "noted" if notes else "comparability_unknown",
                              "notes": notes})
        return pairs

    def _cofog_links(self, namespace: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not rows or not table_exists(self.conn, "social_protection_links"):
            return []
        from src.kb.social_protection_links import COFOG_NOTE, SocialProtectionLinks

        ids = {r["series_id"] for r in rows}
        return [{"link_id": link["link_id"], "series_id": link["series_id"], "state": link["state"],
                 "basis": link["basis"], "target": link["target"], "note": COFOG_NOTE}
                for link in SocialProtectionLinks(self.conn, initialize=False).links(namespace, scopes={"operator"},
                                                                                     kind="cofog")
                if link["series_id"] in ids]

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
                "retrieved_at": vintage["retrieved_at"], "dataflow_version": vintage["dataflow_version"],
                "definition_id": vintage["definition_id"], "new_periods": changes.get("new_periods", []),
                "changed_periods": [r["period"] for r in revisions], "revisions": revisions,
                "dropped_periods": changes.get("dropped_periods", []),
                "estimates_replaced": [r["period"] for r in revisions
                                       if r["before"].get("publication_status") in PROVISIONAL_STATUSES
                                       and r["after"].get("publication_status") not in PROVISIONAL_STATUSES],
                "definition_change": changes.get("definition_change"),
                "edition_restatement": changes.get("edition_restatement"),
                "removed_by_source": changes.get("removed_by_source"),
                "citation": citation(series, vintage, release),
            }
            vintages.append(entry)
            if previous is not None:
                pairs.append(self._pair(previous, entry, notes))
            previous = entry
        return {"contract": HISTORY_CONTRACT, "namespace": namespace, "series": series, "vintages": vintages,
                "pairs": pairs, "series_notes": notes,
                "scope_notes": [n for n in notes if n["relation"] == "scope_difference"],
                "statement": NEVER_SENTENCE, "exclusions": list(EXCLUSIONS)}

    @staticmethod
    def _pair(before: Mapping[str, Any], after: Mapping[str, Any], notes: list[dict[str, Any]]) -> dict[str, Any]:
        stated = []
        change = after["definition_change"]
        if change:
            if change.get("manual_edition_before") != change.get("manual_edition_after"):
                stated.append({"kind": "manual_edition_change",
                               "statement": f"the ESSPROS manual edition changed from "
                                            f"{change.get('manual_edition_before')!r} to "
                                            f"{change.get('manual_edition_after')!r}", "detail": change})
            else:
                stated.append({"kind": "definition_change", "statement": "the definition or dataflow version changed",
                               "detail": change})
        if after["edition_restatement"]:
            restated = after["edition_restatement"]
            stated.append({"kind": "edition_restatement",
                           "statement": f"{restated['edition_after']} restates periods "
                                        f"{', '.join(restated['restated_periods'])} published in "
                                        f"{restated['edition_before']}", "periods": restated["restated_periods"]})
        if after["estimates_replaced"]:
            stated.append({"kind": "estimate_replaced",
                           "statement": "the source replaced its estimated, provisional or projected values for "
                                        f"{', '.join(after['estimates_replaced'])} with later figures",
                           "periods": after["estimates_replaced"]})
        if after["removed_by_source"]:
            stated.append({"kind": "removed_by_source", "statement": after["removed_by_source"]["statement"]})
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
                "comparability": "noted" if stated else "comparability_unknown", "notes": stated}


def place_profile(conn: Any, namespace: str, *, scopes: Iterable[str], as_of: Any = None,
                  place_id: str | None = None, area: Mapping[str, Any] | None = None,
                  enabled_providers: Iterable[str] | None = None) -> dict[str, Any]:
    """A place's expenditure, beneficiary and coverage figures from each source side by side as of a date, with links."""
    from src.kb.social_protection_links import SocialProtectionLinks
    from src.kb.social_protection_records import enabled_providers as selected

    scopes = set(scopes)
    queries = SocialProtectionQueries(conn)
    providers = set(enabled_providers) if enabled_providers is not None else selected(conn)
    answers = {measure: queries.indicator_for_place(namespace, scopes=scopes, as_of=as_of, place_id=place_id,
                                                    area=area, measure=measure, enabled_providers=providers)
               for measure in MEASURES}
    series_ids = {r["series_id"] for a in answers.values() for r in a["results"]}
    links = [link for link in SocialProtectionLinks(conn, initialize=False).links(namespace, scopes=scopes)
             if link["series_id"] in series_ids] if table_exists(conn, "social_protection_links") else []
    first = next(iter(answers.values()))
    result = {
        "contract": PROFILE_CONTRACT, "namespace": namespace,
        "query": {"place_id": place_id, "area": dict(area) if area else None, "as_of": as_of},
        "as_of": first["as_of"], "answers": answers, "links": links,
        "none_on_record": [n for a in answers.values() for n in a["none_on_record"]],
        "features_disabled": first["features_disabled"], "never_equated": NEVER_EQUATED,
        "never_combined": NEVER_COMBINED, "exclusions": list(EXCLUSIONS), "minimisation": MINIMISATION["decision"],
    }
    result["profile_hash"] = digest(result)
    return result


def export_profile(profile: Mapping[str, Any], *, created_at_ms: int | None = None) -> dict[str, Any]:
    """A ``noesis-evidence-bundle-v1``: every figure cited with source, record revision (vintage) and as-of time."""
    from src.evidence_bundle.builder import EvidenceBundleBuilder

    builder = EvidenceBundleBuilder("receipt", {"operation": PROFILE_CONTRACT, "query": profile["query"],
                                                "as_of": profile["as_of"]},
                                    created_at_ms=created_at_ms or 0, as_of_ms=cutoff_ms(profile["query"]["as_of"]))
    refs: list[str] = []
    for measure, answer in sorted(profile["answers"].items()):
        for row in answer["results"]:
            cite = row["citation"]
            object_id = f"social-protection:{cite['series_id']}@{cite['vintage_id']}"
            builder.add_object("evidence", {
                "kind": "social-protection-series-vintage", "measure": measure,
                "function": row["function"], "unit": row["unit"], "population": row["population"],
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
    for link in profile.get("links") or []:
        object_id = f"social-protection-link:{link['link_id']}"
        builder.add_object("evidence", {"kind": "social-protection-link", "link_kind": link["kind"],
                                        "basis": link["basis"], "state": link["state"], "locator": {"cited": False},
                                        "record_revision_id": link["vintage_id"], "target": link["target"]},
                           object_id=object_id)
        refs.append(object_id)
    summary = {k: v for k, v in profile.items() if k not in {"answers", "links"}}
    builder.add_object("receipt", {"kind": "social-protection-profile", **summary},
                       object_id=f"social-protection-profile:{profile['profile_hash'][:24]}", references=refs,
                       root=True)
    for gap in profile.get("none_on_record") or []:
        builder.add_omission(f"none on record: {gap['provider']} {gap['measure']}")
    for disabled in profile.get("features_disabled") or []:
        builder.add_omission(f"source {disabled['provider']}: {disabled['reason']}")
    for link in profile.get("links") or []:
        if link["state"] in {"provider_absent", "target_not_held"}:
            builder.add_omission(f"link {link['link_id']} {link['state']}")
    return builder.build()


__all__ = ["HISTORY_CONTRACT", "NEVER_COMBINED", "NEVER_EQUATED", "PROFILE_CONTRACT", "SocialProtectionQueries",
           "export_profile", "place_profile"]
