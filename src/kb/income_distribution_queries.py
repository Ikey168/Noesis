"""Income, poverty and inequality answers: a place's indicator as of a release, a series' history (#2583, IP08-IP09).

:meth:`IncomeQueries.indicator_for_place` takes a place (a Geospatial place id, resolved through accepted IP06
mappings only, or a published ``{scheme, code}``), an optional indicator concept and a date, and returns every
source's series for that place with the vintage each source had released by the date, side by side. Series are
grouped by the definition that makes values comparable - concept, welfare concept, equivalence scale, poverty line,
PPP base year and reference-year basis - and values in different groups are never combined: a 2.15 PPP$ headcount,
an EU-SILC at-risk-of-poverty rate and an OECD 50 %-of-median poverty rate stay three answers. Every value cites the
vintage (release and retrieval clocks, release version, file digest) and the definition revision.

:meth:`IncomeQueries.history` lists a series' vintages with release dates, the periods each added, revised or
removed, PPP revisions and source-stated breaks; each consecutive pair states its comparability notes or is marked
``comparability_unknown``. :meth:`IncomeQueries.export_bundle` renders an answer as a ``noesis-evidence-bundle-v1``
citing every value with source, record revision and as-of time.

Nothing is nowcast, filled, re-based to another PPP round, converted between income and consumption or averaged.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from itertools import pairwise
from typing import Any

from src.ingestion.income_distribution_sources import (
    CONCEPTS,
    EXCLUSIONS,
    MINIMISATION,
    PROVIDER_CONTRACTS,
)
from src.kb.income_distribution_records import (
    ANSWER_CONTRACT,
    READ_SCOPE,
    IncomeError,
    authorize,
    canonical,
    digest,
    iso_from_ms,
    minimised,
    table_exists,
)
from src.kb.income_distribution_store import (
    IncomeComparability,
    IncomeStore,
    comparability_basis,
)

NOTE = ("each source's published values side by side; different poverty lines, PPP rounds, welfare concepts and "
        "equivalence scales are separate groups and are never combined; no year is filled or nowcast")


def comparable_group(series: Mapping[str, Any]) -> dict[str, Any]:
    """The definition attributes values must share to sit in one group (sources still stay separate rows)."""
    return {"concept": series["indicator"]["concept"], "welfare_concept": series["welfare_concept"],
            "equivalence_scale": series["equivalence_scale"].get("code"), "poverty_line": series["poverty_line"],
            "ppp_base_year": series["ppp_base_year"], "reference_year_basis": series["reference_year_basis"],
            "unit": series["unit"].get("code")}


class IncomeQueries:
    def __init__(self, conn: Any, *, now=None) -> None:
        self.conn = conn
        self.store = IncomeStore(conn, initialize=False, now=now)

    def _identity(self):
        from src.kb.income_distribution_identity import IncomeIdentity

        return IncomeIdentity(self.conn, initialize=False, now=self.store.now) if table_exists(
            self.conn, "income_identity_assertions") else None

    def _place_codes(self, namespace: str, place: Any) -> dict[str, Any]:
        """Area codes of a place: accepted mappings for a place id, else the published code as given."""
        if isinstance(place, Mapping):
            if not place.get("scheme") or not place.get("code"):
                raise IncomeError("invalid_query", "a place code states its scheme and code")
            return {"place": dict(place), "codes": [{"scheme": place["scheme"], "code": str(place["code"]),
                                                    "basis": "published code as requested"}], "unmatched_codes": []}
        identity = self._identity()
        if identity is None:
            return {"place": {"place_id": place}, "codes": [], "unmatched_codes": []}
        codes = [{**c, "basis": "accepted place mapping"} for c in identity.area_codes_for_place(namespace, place)]
        pending = [{"scheme": a["subject"]["scheme"], "code": a["subject"]["code"], "state": a["state"],
                    "reason": a["reason"]}
                   for a in identity.assertions(namespace, scopes={"operator"}, kind="area")
                   if a["state"] in {"proposed", "ambiguous"} and (
                       (a["target"] or {}).get("place_id") == place
                       or any(c["place_id"] == place for c in a["evidence"].get("candidates") or []))]
        return {"place": {"place_id": place}, "codes": codes, "unmatched_codes": pending}

    def _citation(self, namespace: str, series: Mapping[str, Any], vintage: Mapping[str, Any],
                  revision: Mapping[str, Any], as_of_ms: int | None) -> dict[str, Any]:
        return {"provider": series["provider"], "source_id": revision["source_id"], "series_id": series["series_id"],
                "series_key": series["native_key"], "vintage_id": vintage["vintage_id"],
                "definition_id": vintage["definition_id"], "release_at": vintage["release_at"],
                "release_basis": vintage["release_at_basis"], "release_version": vintage["release_version"],
                "retrieved_at": vintage["retrieved_at"], "file_sha256": revision["file_sha256"], "url": revision["url"],
                "evidence_origin": revision["evidence_origin"], "live_verification": revision["live_verification"],
                "as_of": iso_from_ms(as_of_ms) or "latest"}

    def indicator_for_place(self, namespace: str, *, scopes: Iterable[str], place: Any, concept: str | None = None,
                            provider: str | None = None, as_of_ms: int | None = None, period_from: str | None = None,
                            period_to: str | None = None, history: bool = False) -> dict[str, Any]:
        """Published values known at ``as_of_ms`` per source with definitions, vintages and comparability."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if place is None:
            raise IncomeError("invalid_query", "name a place (place id or {scheme, code})")
        if concept is not None and concept not in CONCEPTS:
            raise IncomeError("invalid_query", f"concept is one of {CONCEPTS}")
        if provider is not None and provider not in PROVIDER_CONTRACTS:
            raise IncomeError("invalid_query", f"provider is one of {sorted(PROVIDER_CONTRACTS)}")
        places = self._place_codes(namespace, place)
        areas = [(c["scheme"], c["code"]) for c in places["codes"]]
        comparability = IncomeComparability(self.conn, now=self.store.now, initialize=False) if table_exists(
            self.conn, "income_comparability") else None
        identity = self._identity()
        results, unavailable = [], []
        for series in (self.store.find_series(namespace, provider=provider, concept=concept, areas=areas)
                       if areas else []):
            vintage, reason = self.store.select_vintage(namespace, series["series_id"], as_of_ms=as_of_ms)
            if vintage is None:
                unavailable.append({"series_id": series["series_id"], "provider": series["provider"],
                                    "native_key": series["native_key"], "reason": reason})
                continue
            if vintage["status"] == "withdrawn":
                unavailable.append({"series_id": series["series_id"], "provider": series["provider"],
                                    "native_key": series["native_key"], "reason": "withdrawn_by_source",
                                    "vintage_id": vintage["vintage_id"], "release_at": vintage["release_at"]})
                continue
            revision = self.store.source_revision(namespace, vintage["release_id"])
            cite = self._citation(namespace, series, vintage, revision, as_of_ms)
            observations = self.store.observations(namespace, vintage["vintage_id"], period_from=period_from,
                                                   period_to=period_to)
            results.append({
                "series_id": series["series_id"], "provider": series["provider"], "native_key": series["native_key"],
                "indicator": series["indicator"], "welfare_concept": series["welfare_concept"],
                "equivalence_scale": series["equivalence_scale"], "poverty_line": series["poverty_line"],
                "ppp_base_year": series["ppp_base_year"], "reference_year_basis": series["reference_year_basis"],
                "survey": series["survey"], "coverage": series["coverage"],
                "methodology_version": series["methodology_version"], "unit": series["unit"],
                "unit_multiplier": series["unit_multiplier"], "area": series["area"],
                "group": digest(comparable_group(series))[:16],
                "definition": self.store.definition(namespace, vintage["definition_id"]),
                "vintage": {**vintage, "source_revision": revision},
                "values": [{**o, "citation": cite} for o in observations],
                "withheld_periods": [{"period": o["period"], "status": o["status"], "flags": o["flags"]}
                                     for o in observations if o["status"] != "reported"],
                "source_notes": [] if comparability is None else [
                    {k: n[k] for k in ("relation", "statement", "periods", "state")}
                    for n in comparability.notes(namespace, scopes={"operator"}, series_id=series["series_id"],
                                                 active_only=True) if n["right"] is None],
                "related_series": [] if identity is None else identity.related_series(namespace, series["series_id"]),
                **({"revision_history": [
                    {k: v[k] for k in ("vintage_id", "release_at", "release_at_basis", "release_version",
                                       "retrieved_at", "revision_of", "status", "changes")}
                    for v in self.store.vintage_rows(namespace, series["series_id"])
                    if as_of_ms is None or v["release_at_ms"] <= as_of_ms]} if history else {}),
            })
        groups: dict[str, dict[str, Any]] = {}
        for result in results:
            entry = groups.setdefault(result["group"], {"group": result["group"], "definition": comparable_group({
                **result, "indicator": result["indicator"]}), "series": []})
            entry["series"].append({"series_id": result["series_id"], "provider": result["provider"]})
        pairs = []
        for i, left in enumerate(results):
            for right in results[i + 1:]:
                if left["indicator"]["concept"] != right["indicator"]["concept"]:
                    continue
                differences = comparability_basis(left, right)
                notes = [] if comparability is None else comparability.notes_between(
                    namespace, left["series_id"], right["series_id"])
                pairs.append({"series": [left["series_id"], right["series_id"]],
                              "same_group": left["group"] == right["group"], "recorded_differences": differences,
                              "notes": notes, "status": "noted" if notes or differences else "comparability_unknown",
                              "combined": False})
        answer = {
            "contract": ANSWER_CONTRACT,
            "namespace": namespace,
            "as_of": iso_from_ms(as_of_ms) or "latest",
            "as_of_ms": as_of_ms,
            "subject": {"place": places, "concept": concept, "provider": provider},
            "status": "reported" if results else "none_published",
            "results": results,
            "groups": sorted(groups.values(), key=lambda g: canonical(g["definition"])),
            "unavailable_by_as_of": unavailable,
            "comparability": pairs,
            "side_by_side": True,
            "exclusions": list(EXCLUSIONS),
            "minimisation": MINIMISATION["decision"],
            "note": NOTE,
        }
        if not results:
            answer["reason"] = ("no accepted place mapping or published code for this place" if not areas else
                                "no source has published a series for this place and indicator by the date")
        return minimised(answer)

    def history(self, namespace: str, series_id: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        """Every vintage with release dates, changed periods, PPP revisions and breaks, and pairwise comparability."""
        authorize(namespace, set(scopes), READ_SCOPE)
        series = self.store.series(namespace, series_id)
        comparability = IncomeComparability(self.conn, now=self.store.now, initialize=False) if table_exists(
            self.conn, "income_comparability") else None
        notes = [] if comparability is None else comparability.notes(namespace, scopes={"operator"},
                                                                     series_id=series_id, active_only=True)
        vintages, pairs = [], []
        rows = self.store.vintage_rows(namespace, series_id)
        for vintage in rows:
            revision = self.store.source_revision(namespace, vintage["release_id"])
            changes = vintage["changes"]
            vintages.append({
                **vintage, "source_revision": revision,
                "citation": self._citation(namespace, series, vintage, revision, None),
                "changed_periods": {"new": changes.get("new_periods") or [],
                                    "revised": [r["period"] for r in changes.get("revised") or []],
                                    "removed": changes.get("removed_periods") or []},
                "observations": self.store.observations(namespace, vintage["vintage_id"]),
            })
        for before, after in pairwise(rows):
            changes = after["changes"]
            changed = set(changes.get("new_periods") or []) | {r["period"] for r in changes.get("revised") or []}
            stated = []
            if changes.get("ppp_revision"):
                stated.append({"relation": "ppp_revision", "statement": "; ".join(changes["ppp_revision_basis"]),
                               "periods": changes.get("restated_periods") or [], "origin": "release record"})
            if changes.get("definition_change"):
                stated.append({"relation": "different_methodology", "statement":
                               f"definition {changes['definition']['before']} -> {changes['definition']['after']}; "
                               f"release version {changes['release_version']['before']} -> "
                               f"{changes['release_version']['after']}", "periods": [], "origin": "release record"})
            if changes.get("withdrawn"):
                stated.append({"relation": "source_note", "statement": "the source no longer states this series",
                               "periods": changes.get("removed_periods") or [], "origin": "release record"})
            for note in notes:
                if note["right"] is None and (not note["periods"] or set(note["periods"]) & changed):
                    stated.append({k: note[k] for k in ("relation", "statement", "periods", "origin", "state")})
            pairs.append({"vintages": [before["vintage_id"], after["vintage_id"]],
                          "release_dates": [before["release_at"], after["release_at"]],
                          "notes": stated, "status": "noted" if stated else "comparability_unknown"})
        return minimised({
            "contract": ANSWER_CONTRACT, "series": series, "vintages": vintages, "comparability": pairs,
            "source_notes": [{k: n[k] for k in ("relation", "statement", "periods", "state", "origin")}
                             for n in notes],
            "exclusions": list(EXCLUSIONS),
            "note": "every retained vintage; earlier values are never overwritten; a PPP revision is its own vintage",
        })

    @staticmethod
    def export_bundle(answer: Mapping[str, Any], *, created_at_ms: int | None = None) -> dict[str, Any]:
        """A ``noesis-evidence-bundle-v1`` citing every value with source, record revision and as-of time."""
        from src.evidence_bundle.builder import EvidenceBundleBuilder

        builder = EvidenceBundleBuilder("answer", {"operation": "income-indicator-for-place",
                                                   "subject": answer["subject"], "as_of": answer["as_of"]},
                                        created_at_ms=created_at_ms, as_of_ms=answer.get("as_of_ms"))
        refs, statements = [], []
        for result in answer["results"]:
            label = f"{result['provider']} {result['indicator']['concept']} {result['area']['code']}"
            result_refs = []
            for value in result["values"]:
                citation = value["citation"]
                object_id = f"income-value:{result['series_id']}:{value['period']}@{citation['vintage_id']}"
                builder.add_object("evidence", {
                    "kind": "income-value",
                    "locator": {"cited": True, "document_id": result["vintage"]["release_id"],
                                "series_id": result["series_id"], "period": value["period"]},
                    "provider": result["provider"], "indicator": result["indicator"],
                    "welfare_concept": result["welfare_concept"],
                    "equivalence_scale": result["equivalence_scale"].get("code"),
                    "poverty_line": result["poverty_line"], "ppp_base_year": result["ppp_base_year"],
                    "reference_year_basis": result["reference_year_basis"], "area": result["area"],
                    "period": value["period"], "value_text": value["value_text"], "status": value["status"],
                    "flags": value["flags"], "attributes": value["attributes"], "unit": result["unit"],
                    "record_revision": {"vintage_id": citation["vintage_id"], "definition_id": citation["definition_id"],
                                        "release_version": citation["release_version"],
                                        "revision_of": result["vintage"]["revision_of"]},
                    "as_of": citation["as_of"], "released_at": citation["release_at"],
                    "retrieved_at": citation["retrieved_at"],
                    "source": {k: citation[k] for k in ("provider", "source_id", "url", "file_sha256",
                                                        "evidence_origin", "live_verification")},
                }, object_id=object_id)
                refs.append(object_id)
                result_refs.append(object_id)
                if citation.get("url"):
                    builder.add_external_reference(f"release:{result['vintage']['release_id']}", citation["url"],
                                                   required=False)
                if value["status"] != "reported":
                    builder.add_omission(f"{label} {value['period']}: {value['status']}; no value",
                                         object_id=object_id)
            statements.append({"statement": f"{label} as published in vintage {result['vintage']['vintage_id']} "
                               f"(group {result['group']}; never combined with another group)",
                               "status": "cited", "evidence_refs": sorted(result_refs)})
        for missing in answer["unavailable_by_as_of"]:
            builder.add_omission(f"{missing['provider']} {missing['native_key']}: {missing['reason']}")
        if answer["status"] == "none_published":
            builder.add_omission(f"no published series: {answer.get('reason')}")
        root = {k: answer[k] for k in ("contract", "subject", "as_of", "status", "exclusions", "minimisation")}
        builder.add_object("answer", {"kind": "income-indicator-for-place", **root, "statements": statements,
                                      "groups": answer["groups"]},
                           object_id=f"income-answer:{digest([answer['subject'], answer['as_of']])[:24]}",
                           references=sorted(set(refs)), root=True)
        return builder.build()


__all__ = ["IncomeQueries", "comparable_group"]
