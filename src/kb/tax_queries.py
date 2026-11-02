"""Answers: tax figures for a jurisdiction as of an edition, a series' edition history, public CbCR references for an
ownership entity, and evidence bundles (#2889).

:meth:`TaxQueries.indicator_for_jurisdiction` takes an ISO 3166 alpha-3 jurisdiction code, an optional concept, the
providers and a date. It returns **one row per series** from the edition published on or before that date (the vintage
whose release clock is on or before the date) and says which edition it is. Published statistics and model estimates
are returned in **separate sections** and never combined: a model-estimate row carries its method and edition, its
cells have status ``estimated`` and it says it is not an observation. CbCR caveats travel with every CbCR row.
Tax Justice Network index questions return ``not-implemented``, never an empty answer.

:meth:`TaxQueries.series_history` lists every edition of one series: release date and basis, the periods each edition
added, revised or dropped (values and status letters before and after as published), method and definition changes and
removals by the source. Every edition is cited.

:meth:`TaxQueries.company_reports_for_entity` returns the public CbCR references of an ownership entity reached through
**accepted** identity decisions only; pending candidates and unmatched references are reported, never used.

Nothing here computes an effective tax rate, a ratio, a share, a rank or a score, blends sources, fills a jurisdiction
or year, converts a currency or forecasts.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from src.ingestion.tax_sources import (
    CONCEPTS,
    NEVER_SENTENCE,
    NOT_IMPLEMENTED,
    NOT_IMPLEMENTED_ANSWER,
    NOT_IMPLEMENTED_CONCEPTS,
    PROVIDERS,
)
from src.kb.tax_records import (
    ANSWER_CONTRACT,
    EXCLUSIONS,
    READ_SCOPE,
    TaxError,
    authorize,
    to_ms,
)
from src.kb.tax_store import TaxStore, citation

HISTORY_CONTRACT = "noesis-tax-transparency-history-v1"
BUNDLE_CONTRACT = "noesis-tax-evidence-bundle-v1"
ESTIMATE_NOTICE = ("model estimate: computed by its publisher with the stated method for this edition; not an "
                   "observation, never blended with published statistics")


def cutoff_ms(as_of: Any) -> int | None:
    """A date means the end of that day (published *by* the date); a time or epoch ms is used as given."""
    if as_of in (None, ""):
        return None
    if isinstance(as_of, (int, float)):
        return int(as_of)
    raw = str(as_of).strip()
    return to_ms(raw) + 86_399_999 if len(raw) == 10 else to_ms(raw)


def _jurisdiction(value: Any) -> str:
    code = dict(value).get("code") if isinstance(value, Mapping) else value
    code = str(code or "").strip().upper()
    if len(code) != 3 or not code.isalpha():
        raise TaxError("invalid_query", "a jurisdiction is an ISO 3166 alpha-3 code")
    return code


class TaxQueries:
    def __init__(self, conn: Any) -> None:
        self.conn = conn
        self.store = TaxStore(conn, initialize=False)

    def _row(self, namespace: str, series: Mapping[str, Any], cutoff: int | None, period: str | None
             ) -> dict[str, Any] | None:
        answer = self.store.values(namespace, series["series_id"], as_of_ms=cutoff)
        if answer["status"] == "unavailable":
            return None
        vintage = answer["vintage"]
        estimate = series["nature"] == "model_estimate"
        values = [{**o, "kind": series["nature"]} for o in answer["observations"]
                  if period is None or o["period"] == str(period)]
        row = {
            "series_id": series["series_id"], "provider": series["provider"], "dataset": series["dataset"],
            "nature": series["nature"], "indicator": series["indicator"], "jurisdiction": series["jurisdiction"],
            "counterpart": series["counterpart"], "unit": series["unit"], "frequency": series["frequency"],
            "edition": vintage["edition"], "status": answer["status"], "values": values,
            "definition": vintage["definition"], "notes": vintage["source_notes"],
            "vintage": {k: vintage[k] for k in ("vintage_id", "sequence", "edition", "release_at", "release_basis",
                                                "retrieved_at", "revision_of", "economic_vintage_id")},
            "citation": answer["citation"],
        }
        if estimate:
            row.update({"method": vintage["method"], "presented_as": ESTIMATE_NOTICE, "observation": False})
        else:
            row.update({"presented_as": "published statistic as released in this edition", "observation": True})
        if answer["status"] == "removed_by_source":
            row["note"] = answer["note"]
        return row

    def indicator_for_jurisdiction(self, namespace: str, *, scopes: Iterable[str], jurisdiction: Any,
                                   concept: str | None = None, providers: Iterable[str] | None = None,
                                   period: str | None = None, as_of: Any = None,
                                   enabled_providers: Iterable[str] | None = None) -> dict[str, Any]:
        authorize(namespace, scopes, READ_SCOPE)
        wanted = list(providers) if providers else None
        base = {"contract": ANSWER_CONTRACT, "namespace": namespace, "as_of": as_of, "concept": concept,
                "not_implemented": [NOT_IMPLEMENTED_ANSWER], "exclusions": list(EXCLUSIONS), "never": NEVER_SENTENCE}
        if concept in NOT_IMPLEMENTED_CONCEPTS or (wanted and set(wanted) <= set(NOT_IMPLEMENTED)):
            return {**base, "status": "not-implemented", "jurisdiction": jurisdiction,
                    "provider": NOT_IMPLEMENTED_CONCEPTS.get(str(concept)) or wanted,
                    "reason": NOT_IMPLEMENTED_ANSWER["reason"], "published_statistics": [], "model_estimates": []}
        if concept is not None and concept not in CONCEPTS:
            raise TaxError("invalid_query", f"concept is one of {CONCEPTS} (or a not-implemented index concept)")
        code = _jurisdiction(jurisdiction)
        enabled = set(enabled_providers) if enabled_providers is not None else set(PROVIDERS)
        selected = [p for p in (wanted or PROVIDERS) if p in PROVIDERS]
        cutoff = cutoff_ms(as_of)
        statistics, estimates = [], []
        for provider in selected:
            if provider not in enabled:
                continue
            for series in self.store.find_series(namespace, provider=provider, concept=concept, jurisdiction=code):
                row = self._row(namespace, series, cutoff, period)
                if row is None:
                    continue
                (estimates if row["nature"] == "model_estimate" else statistics).append(row)
        status = "reported" if statistics or estimates else "no_records"
        return {**base, "status": status, "jurisdiction": {"scheme": "iso3166-alpha3", "code": code},
                "published_statistics": statistics, "model_estimates": estimates,
                "not_selected": sorted(set(selected) - enabled),
                "note": "published statistics and model estimates are separate sections and never combined; each row "
                        "is the edition published by the date and cites it"}

    def series_history(self, namespace: str, series_id: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        authorize(namespace, scopes, READ_SCOPE)
        series = self.store.series(namespace, series_id)
        editions = []
        for vintage in self.store.vintage_rows(namespace, series_id):
            release = self.store.release(namespace, vintage["release_id"])
            editions.append({
                "edition": vintage["edition"], "vintage_id": vintage["vintage_id"], "status": vintage["status"],
                "release_at": vintage["release_at"], "release_basis": vintage["release_basis"],
                "retrieved_at": vintage["retrieved_at"], "revision_of": vintage["revision_of"],
                "changes": vintage["changes"], "method": vintage["method"], "notes": vintage["source_notes"],
                "observations": self.store.observations(namespace, vintage["vintage_id"]),
                "citation": citation(series, vintage, release),
            })
        return {"contract": HISTORY_CONTRACT, "namespace": namespace, "series": series, "editions": editions,
                "presented_as": ESTIMATE_NOTICE if series["nature"] == "model_estimate" else
                "published statistic as released in each edition",
                "note": "every retained edition, oldest first; a revised value is a new edition's vintage, never an "
                        "overwrite", "exclusions": list(EXCLUSIONS)}

    def company_reports_for_entity(self, namespace: str, *, scopes: Iterable[str], ownership_key: str
                                   ) -> dict[str, Any]:
        """Public CbCR references linked to an ownership record or entity through accepted decisions only."""
        from src.kb.tax_identity import TaxIdentity

        authorize(namespace, scopes, READ_SCOPE)
        identity = TaxIdentity(self.conn, initialize=False)
        links = identity.accepted_links(namespace, [ownership_key], scopes=scopes)
        reports = []
        for link in links:
            for report in self.store.company_reports(namespace, reference_key=link["reference_key"]):
                reports.append({**report, "identity": link})
        pending = [c for c in identity.candidates(namespace, scopes=scopes)
                   if c["state"] == "proposed" and ownership_key in (c["ownership_key"], c["ownership_entity"])]
        return {"contract": ANSWER_CONTRACT, "namespace": namespace, "ownership_key": ownership_key,
                "status": "reported" if reports else "no_accepted_match", "reports": reports,
                "pending_candidates": [{"candidate_id": c["candidate_id"], "reference_key": c["reference_key"],
                                        "method": c["method"], "low_evidence": c["low_evidence"]} for c in pending],
                "note": "link-only references reached through accepted identity decisions; no company-level figure "
                        "is stored", "exclusions": list(EXCLUSIONS)}

    @staticmethod
    def evidence_bundle(answer: Mapping[str, Any]) -> dict[str, Any]:
        """Assertions each citing source, edition, record revision (vintage) and as-of time."""
        bibliography: dict[str, dict[str, Any]] = {}
        sections = []
        for key, title in (("published_statistics", "published statistics"), ("model_estimates", "model estimates")):
            assertions = []
            for row in answer.get(key) or []:
                cited = row["citation"]
                bibliography.setdefault(cited["release_id"], {
                    "id": cited["release_id"],
                    "text": f"{cited['provider']} {cited['dataset']} ({cited['edition']}, released {cited['as_of']}, "
                            f"retrieved {cited['retrieved_at']}, {cited['evidence_origin']} evidence, "
                            f"{cited['live_verification']}), {cited.get('url')}"})
                for value in row["values"]:
                    shown = value["value_text"] if value["status"] in {"reported", "estimated"} else \
                        f"[{value['status']}]"
                    label = f"{row['indicator']['label']} ({row['unit']['label']}, x10^{row['unit']['multiplier']})"
                    partner = row["counterpart"]["code"]
                    text = (f"{label}, {row['jurisdiction']['code']}"
                            + (f" / {partner}" if partner != "not_applicable" else "")
                            + f", {value['period']}: {shown}")
                    if row["nature"] == "model_estimate":
                        text += f" [model estimate; method: {row['method']['label']}; edition: {row['edition']}]"
                    assertions.append({
                        "id": f"{row['series_id']}:{row['vintage']['vintage_id']}:{value['period']}",
                        "text": text, "kind": "sourced",
                        "dependencies": [{"kind": "source", "namespace": answer.get("namespace"),
                                          "id": row["series_id"], "revision": row["vintage"]["vintage_id"],
                                          "as_of": row["vintage"]["release_at"]}],
                        "citations": [cited["release_id"]], "source": cited["provider"], "nature": row["nature"],
                    })
            sections.append({"id": key, "title": f"{title} as of {answer.get('as_of') or 'latest'}",
                             "assertions": assertions})
        return {"contract": BUNDLE_CONTRACT, "sections": sections, "bibliography": list(bibliography.values()),
                "limitations": ["offline fixture evidence is not live coverage",
                                "model estimates keep their method and edition and are not observations",
                                "Tax Justice Network indices are not implemented (licence)"],
                "exclusions": list(EXCLUSIONS)}


__all__ = ["BUNDLE_CONTRACT", "ESTIMATE_NOTICE", "HISTORY_CONTRACT", "TaxQueries", "cutoff_ms"]
