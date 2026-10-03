"""Answers: a waste or circularity indicator for a place as of a release, and a facility's waste transfers (WC08, WC09).

Track #2740.

:meth:`WasteQueries.indicator_for_place` takes a place (a Geospatial place id, read through accepted WC06 matches only,
or a published area code) and an optional indicator concept and date. It returns **one row per series and source**:
the values as released by that date (the vintage whose release clock is on or before the date) with the definition
revision, the series key (waste category, hazardousness, activity, treatment operation, unit as published), flags
verbatim, source-stated notes and the cited vintage. Eurostat and OECD rows stand **side by side** and are never
blended, reconciled or averaged; a biennial dataset's odd years are reported **absent** (not collected), never filled.

:meth:`WasteQueries.facility_transfers` takes a facility known to ``environment.core`` (its INSPIRE id or its
environment record id) and returns its published waste transfers per reporting year: hazardousness, recovery or
disposal, domestic or transboundary, the quantity in tonnes as published and the method code, each with every vintage
(corrections and removals) cited, the ``environment.core`` facility record cited by id and revision (no operator
field is copied), truncated acquisitions and the reporting-threshold note (absence is not zero). Transfers are never
summed into national totals or set against Eurostat aggregates as one figure.

:meth:`WasteQueries.export_bundle` returns an evidence bundle in which every item cites its source, record revision
and as-of time.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.ingestion.waste_sources import NEVER_SENTENCE, TRANSFER_PROVIDER
from src.kb.waste_records import (
    ANSWER_CONTRACT,
    ENVIRONMENT_READ,
    EXCLUSIONS,
    MINIMISATION,
    READ_SCOPE,
    WasteError,
    authorize,
    iso,
    require_scope,
    table_exists,
    to_ms,
)
from src.kb.waste_store import (
    FACILITY_PROVIDER,
    WasteStore,
    citation,
    environment_scopes,
)

BUNDLE_CONTRACT = "noesis-waste-evidence-bundle-v1"
HISTORY_CONTRACT = "noesis-waste-history-v1"
THRESHOLD_NOTE = ("Facilities report waste transfers only above the E-PRTR reporting thresholds (verify): a transfer "
                  "that is not reported is absent, never zero.")
NEVER_SUMMED = ("Facility transfers are listed per facility and row; they are never summed into national totals and "
                "never compared with Eurostat or OECD aggregates as one figure.")
SIDE_BY_SIDE = ("Each source's figure is shown with its own definition and vintage; Eurostat, OECD and EEA figures are "
                "never blended, averaged or reconciled, and a difference is shown, not explained away.")


def cutoff_ms(as_of: Any) -> int | None:
    """A date means the end of that day (released *by* the date); a time or epoch ms is used as given."""
    if as_of in (None, ""):
        return None
    if isinstance(as_of, (int, float)):
        return int(as_of)
    raw = str(as_of).strip()
    return to_ms(raw) + 86_399_999 if len(raw) == 10 else to_ms(raw)


def absent_years(periods: Iterable[str], periodicity: str) -> dict[str, list[str]]:
    """Years between the first and last stated year a vintage does not state: biennial gaps or missing years."""
    stated = sorted({int(p) for p in periods if str(p).isdigit()})
    if len(stated) < 2:
        return {"biennial_not_collected": [], "not_published": []}
    gaps = [str(y) for y in range(stated[0], stated[-1]) if y not in stated]
    if periodicity == "biennial":
        biennial = [y for y in gaps if (int(y) - stated[0]) % 2 == 1]
        return {"biennial_not_collected": biennial, "not_published": [y for y in gaps if y not in biennial]}
    return {"biennial_not_collected": [], "not_published": gaps}


class WasteQueries:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = WasteStore(conn, initialize=False, now=self.now)

    def _identity(self):
        from src.kb.waste_identity import WasteIdentity

        return WasteIdentity(self.conn, initialize=False, now=self.now)

    def _area_codes(self, namespace: str, place: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        identity = self._identity()
        ready = table_exists(self.conn, "waste_identity_assertions")
        if isinstance(place, str):
            codes = [{**c, "basis": "accepted-match"} for c in identity.area_codes_for_place(namespace, place)] \
                if ready else []
            return codes, {"place_id": place}
        area = dict(place or {})
        if not area.get("scheme") or not area.get("code"):
            raise WasteError("invalid_request", "name a place id or an area with scheme and code")
        codes = [{"scheme": area["scheme"], "code": str(area["code"]), "basis": "published-code"}]
        accepted = identity.place_for_area(namespace, area["scheme"], str(area["code"])) if ready else None
        if accepted is not None:
            codes += [{**c, "basis": "accepted-match"} for c in identity.area_codes_for_place(
                namespace, accepted["place_id"]) if (c["scheme"], c["code"]) != (area["scheme"], str(area["code"]))]
        return codes, {"area": {"scheme": area["scheme"], "code": str(area["code"])},
                       "place_id": None if accepted is None else accepted["place_id"]}

    def _row(self, namespace: str, series: Mapping[str, Any], cutoff: int | None, basis: str) -> dict[str, Any]:
        answer = self.store.values(namespace, series["series_id"], as_of_ms=cutoff)
        observations = answer.get("observations") or []
        gaps = absent_years([o["period"] for o in observations], series["periodicity"])
        related = []
        if table_exists(self.conn, "waste_identity_assertions"):
            related = self._identity().related(namespace, series["series_id"])
        definition = answer.get("definition")
        return {
            "series_id": series["series_id"], "provider": series["provider"], "dataset": series["dataset"],
            "native_key": series["native_key"], "indicator": series["indicator"],
            "waste_category": series["waste_category"], "hazard": series["hazard"], "activity": series["activity"],
            "operation": series["operation"], "unit": series["unit"], "area": series["area"],
            "periodicity": series["periodicity"], "matched_by": basis, "status": answer["status"],
            "reason": answer.get("reason"),
            "definition": None if definition is None else {
                "definition_id": definition["definition_id"], "revision": definition["revision"],
                "source_text": definition["content"].get("source_text"), "scope": definition["content"].get("scope"),
                "methodology_notes": definition["content"].get("methodology_notes"),
                "references": definition["content"].get("references")},
            "observations": [{k: o[k] for k in ("period", "value_text", "value", "status", "flags", "flag_meanings")}
                             for o in observations],
            "absent_years": gaps,
            "absence_note": ("biennial dataset: odd years are not collected and stay absent, never filled"
                             if series["periodicity"] == "biennial" else
                             "years the source did not state stay absent, never filled"),
            "notes": self.store.notes(namespace, series["series_id"]),
            "related_indicators": related,
            "vintage": None if answer.get("vintage") is None else {
                k: answer["vintage"][k] for k in ("vintage_id", "sequence", "status", "release_at", "release_basis",
                                                  "retrieved_at", "revision_of")},
            "citation": answer.get("citation"),
            "live_verification": series["live_verification"],
        }

    def indicator_for_place(self, namespace: str, *, scopes: Iterable[str], place: Any, concept: str | None = None,
                            as_of: Any = None) -> dict[str, Any]:
        """Each source's figures for a place as released by the date, side by side with definitions and vintages."""
        authorize(namespace, scopes, READ_SCOPE)
        cutoff = cutoff_ms(as_of)
        codes, subject = self._area_codes(namespace, place)
        rows = []
        for code in codes:
            for series in self.store.find_series(namespace, concept=concept,
                                                 area_codes=[(code["scheme"], code["code"])]):
                rows.append(self._row(namespace, series, cutoff, code["basis"]))
        rows.sort(key=lambda r: (r["indicator"]["concept"], r["provider"], r["dataset"], r["native_key"]))
        pairs = []
        by_id = {r["series_id"]: r for r in rows}
        for row in rows:
            for rel in row["related_indicators"]:
                other = by_id.get(rel["series_id"])
                if other is not None and row["series_id"] < other["series_id"]:
                    pairs.append({"series": [row["series_id"], other["series_id"]], "relation": rel["relation"],
                                  "statement": rel["statement"], "providers": [row["provider"], other["provider"]],
                                  "definitions": [row["definition"] and row["definition"]["source_text"],
                                                  other["definition"] and other["definition"]["source_text"]],
                                  "reconciled": False})
        available = [r for r in rows if r["status"] == "available"]
        status = "reported" if available else ("not_released_by_as_of" if rows else "no_records")
        return {
            "contract": ANSWER_CONTRACT, "namespace": namespace, "subject": subject, "concept": concept,
            "as_of": iso(cutoff), "status": status, "area_codes": codes, "results": rows,
            "providers": sorted({r["provider"] for r in rows}), "related_pairs": pairs, "side_by_side": True,
            "never_blended": True, "statement": SIDE_BY_SIDE,
            "no_records_note": None if rows else "no waste or circularity series is held for this place; nothing is "
                                                 "estimated or filled",
            "exclusions": list(EXCLUSIONS), "never": NEVER_SENTENCE,
        }

    # ------------------------------------------------------------------ facility transfers

    def _facility(self, namespace: str, facility: str) -> tuple[str, dict[str, Any] | None]:
        """The INSPIRE id and the environment.core facility record citation (id and revision only)."""
        if not table_exists(self.conn, "environment_records"):
            return facility, None
        from src.kb.environment_store import EnvironmentStore

        env = EnvironmentStore(self.conn, initialize=False)
        row = self.conn.execute("SELECT native_id FROM environment_records WHERE record_id=? AND namespace=? AND "
                                "record_type='facility'", [facility, namespace]).fetchone()
        inspire_id = row[0] if row else facility
        rid = env.find(namespace, "facility", FACILITY_PROVIDER, inspire_id)
        if rid is None:
            return inspire_id, None
        record = env.record(namespace, rid, scopes=environment_scopes(namespace))
        return inspire_id, {"provider_id": "environment.core", "provider": FACILITY_PROVIDER,
                            "record_id": rid, "revision_id": record["revision_id"], "revision": record["revision"],
                            "native_id": inspire_id, "observed_at": iso(record["observed_at_ms"]),
                            "note": "the facility record stays with environment.core; operator and address fields "
                                    "are not copied into waste answers"}

    def facility_transfers(self, namespace: str, *, scopes: Iterable[str], facility: str,
                           as_of: Any = None) -> dict[str, Any]:
        """A facility's published waste transfers per reporting year with every vintage cited."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        require_scope(scopes, ENVIRONMENT_READ)
        cutoff = cutoff_ms(as_of)
        inspire_id, record = self._facility(namespace, facility)
        identity = None
        if table_exists(self.conn, "waste_identity_assertions"):
            identity = self._identity().facility_for(namespace, inspire_id)
        years: dict[int, list[dict[str, Any]]] = {}
        releases: dict[str, dict[str, Any]] = {}
        for row in self.store.transfer_rows(namespace, inspire_id=inspire_id):
            vintages = self.store.transfer_vintages(namespace, row["row_id"])
            cited = []
            for vintage in vintages:
                release = releases.get(vintage["release_id"])
                if release is None and vintage["release_id"]:
                    release = releases[vintage["release_id"]] = self.store.release(namespace, vintage["release_id"])
                cited.append({**{k: vintage[k] for k in ("vintage_id", "sequence", "revision_of", "status", "quantity",
                                                         "unit", "method", "release_at", "release_at_basis",
                                                         "retrieved_at")},
                              "citation": self._transfer_citation(row, vintage, release)})
            in_force = [v for v in cited if cutoff is None or (
                (v["release_at"] is not None and to_ms(v["release_at"]) <= cutoff)
                and to_ms(v["retrieved_at"]) <= cutoff)]
            current = in_force[-1] if in_force else None
            years.setdefault(row["reporting_year"], []).append({
                "row_id": row["row_id"], "hazardous": {"code": row["hazardous"], "label": row["hazardous_label"]},
                "treatment": {"code": row["treatment"], "label": row["treatment_label"]},
                "destination": {"code": row["destination"], "label": row["destination_label"]},
                "status": "not_released_by_as_of" if current is None else current["status"],
                "quantity": None if current is None else current["quantity"], "unit": "t",
                "method": None if current is None else {"code": current["method"],
                                                        "label": {"M": "measured", "C": "calculated",
                                                                  "E": "estimated"}.get(current["method"] or "")},
                "current_vintage_id": None if current is None else current["vintage_id"],
                "vintages": cited, "environment_record_id": row["environment_record_id"]})
        truncated = [{"release_id": r["release_id"], "release_label": r["release_label"],
                      "retrieved_at": r["retrieved_at"], "dataset_version": r["dataset_version"],
                      "statement": "the acquisition filled its nrOfHits page and is truncated (eea_truncated): rows "
                                   "beyond the page may exist; it never removed a row"}
                     for r in self.store.releases(namespace, provider=TRANSFER_PROVIDER) if r["truncated"]]
        return {
            "contract": ANSWER_CONTRACT, "namespace": namespace, "inspire_id": inspire_id, "as_of": iso(cutoff),
            "facility_record": record,
            "facility_identity": identity or {"state": "not_reviewed" if record else "unmatched",
                                              "inspire_id": inspire_id},
            "status": "reported" if years else ("unknown_facility" if record is None else "no_transfers_reported"),
            "reporting_years": [{"reporting_year": year, "rows": sorted(rows, key=lambda r: (
                r["hazardous"]["code"], r["treatment"]["code"], r["destination"]["code"]))}
                for year, rows in sorted(years.items())],
            "truncated_acquisitions": truncated, "threshold_note": THRESHOLD_NOTE, "never_summed": NEVER_SUMMED,
            "totals": None, "exclusions": list(EXCLUSIONS), "never": NEVER_SENTENCE,
        }

    @staticmethod
    def _transfer_citation(row, vintage, release) -> dict[str, Any]:
        release = release or {}
        return {"provider": TRANSFER_PROVIDER, "source_id": release.get("source_id"), "row_id": row["row_id"],
                "environment_record_id": row["environment_record_id"], "vintage_id": vintage["vintage_id"],
                "release_id": vintage["release_id"], "release_label": release.get("release_label"),
                "dataset_version": release.get("dataset_version"), "release_basis": release.get("release_basis"),
                "as_of": vintage["release_at"], "retrieved_at": vintage["retrieved_at"], "url": release.get("url"),
                "file_sha256": release.get("file_sha256"), "truncated": release.get("truncated"),
                "evidence_origin": release.get("evidence_origin"),
                "live_verification": release.get("live_verification", "unverified-live")}

    # ------------------------------------------------------------------ history and evidence bundles

    def series_history(self, namespace: str, series_id: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        authorize(namespace, scopes, READ_SCOPE)
        series = self.store.series(namespace, series_id)
        vintages = []
        for vintage in self.store.vintage_rows(namespace, series_id):
            release = self.store.release(namespace, vintage["release_id"])
            vintages.append({**{k: vintage[k] for k in ("vintage_id", "sequence", "status", "release_at",
                                                         "release_basis", "retrieved_at", "revision_of",
                                                         "definition_id", "changes")},
                             "citation": citation(series, vintage, release)})
        return {"contract": HISTORY_CONTRACT, "series": series, "vintages": vintages,
                "notes": self.store.notes(namespace, series_id), "exclusions": list(EXCLUSIONS)}

    def export_bundle(self, namespace: str, *, scopes: Iterable[str], place: Any = None,
                      facility: str | None = None, as_of: Any = None) -> dict[str, Any]:
        """An evidence bundle; every item cites source, record revision and as-of time."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        items = []
        if place is not None:
            answer = self.indicator_for_place(namespace, scopes=scopes, place=place, as_of=as_of)
            for row in answer["results"]:
                if row["citation"] is None:
                    continue
                items.append({"kind": "series", "series_id": row["series_id"],
                              "source": {"provider": row["provider"], "dataset": row["dataset"],
                                         "url": row["citation"]["url"]},
                              "record_revision": row["citation"]["vintage_id"], "as_of": row["citation"]["as_of"],
                              "retrieved_at": row["citation"]["retrieved_at"], "status": row["status"],
                              "definition_id": row["definition"] and row["definition"]["definition_id"],
                              "observations": row["observations"], "citation": row["citation"]})
        if facility is not None:
            answer = self.facility_transfers(namespace, scopes=scopes, facility=facility, as_of=as_of)
            for year in answer["reporting_years"]:
                for row in year["rows"]:
                    current = next((v for v in row["vintages"] if v["vintage_id"] == row["current_vintage_id"]), None)
                    if current is None:
                        continue
                    items.append({"kind": "transfer_row", "row_id": row["row_id"],
                                  "reporting_year": year["reporting_year"],
                                  "source": {"provider": TRANSFER_PROVIDER, "url": current["citation"]["url"]},
                                  "record_revision": current["vintage_id"], "as_of": current["release_at"],
                                  "retrieved_at": current["retrieved_at"], "status": row["status"],
                                  "quantity": row["quantity"], "unit": "t", "method": row["method"],
                                  "facility_record": answer["facility_record"], "citation": current["citation"]})
        uncited = [i for i in items if not (i["source"] and i["record_revision"] and i["as_of"])]
        if uncited:
            raise WasteError("uncited_item", "every bundle item cites its source, record revision and as-of time")
        return {"contract": BUNDLE_CONTRACT, "namespace": namespace, "as_of": iso(cutoff_ms(as_of)),
                "items": items, "minimisation": MINIMISATION["decision"], "exclusions": list(EXCLUSIONS),
                "statement": SIDE_BY_SIDE, "never_summed": NEVER_SUMMED, "threshold_note": THRESHOLD_NOTE}


__all__ = ["BUNDLE_CONTRACT", "NEVER_SUMMED", "SIDE_BY_SIDE", "THRESHOLD_NOTE", "WasteQueries", "absent_years",
           "cutoff_ms"]
