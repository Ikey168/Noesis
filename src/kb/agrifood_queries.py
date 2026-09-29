"""Answer commodity-and-place series queries as of a date with vintages and flags (#2213, AF09 #2358).

Given a commodity and a place, :meth:`AgrifoodQueries.series_as_of` returns
every published production, yield, area, price and food-balance series per
source, side by side and never blended: each series keeps its publisher,
dataset, program, unit, period type (calendar or marketing year, never
converted), the commodity mapping that reached it (AF07) and its citations.

* **As of** - for each period, the figure comes from the latest vintage
  released at or before the as-of time (a date means the end of that day); a
  figure revised later is marked so. Without an as-of time the latest vintage
  is used.
* **Revisions** - :meth:`AgrifoodQueries.revisions` lists every vintage of a
  figure with its release date, flag and citation, and compares consecutive
  vintages with :func:`src.kb.environment_vintages.diff_values` (reused by call).
* **Missing and withheld values** are returned as such, with their text and
  flag, never imputed; a commodity and place with no series is reported as
  having none on record.

No forecast, projection or score is made; publisher forecasts and projections
carry the publisher's label.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from typing import Any

from src.kb.agrifood_identity import AgrifoodIdentity, parse_code
from src.kb.agrifood_links import AgrifoodLinks
from src.kb.agrifood_records import NEVER, READ_SCOPE, SERIES_CONTRACT, AgrifoodError, authorize
from src.kb.agrifood_store import AgrifoodStore, release_ms, table_exists

ESTIMATE_LABELS = {
    "observation": "published figure",
    "publisher-estimate": "the publisher's estimate",
    "publisher-forecast": "the publisher's forecast (not made by this pack)",
    "publisher-projection": "the publisher's projection (not made by this pack)",
}
DAY_MS = 86_400_000


def as_of_ms(value: Any) -> int | None:
    """An as-of date or timestamp as epoch ms; a bare date means the end of that day (UTC)."""
    if value in (None, ""):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    parsed = release_ms(text)
    if parsed is None:
        raise AgrifoodError("invalid_request", "as_of is an ISO date or timestamp")
    return parsed + DAY_MS - 1 if len(text) == 10 else parsed


class AgrifoodQueries:
    def __init__(self, conn: Any, *, now=None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = AgrifoodStore(conn, initialize=False, now=self.now)
        self.identity = AgrifoodIdentity(conn, initialize=False, now=self.now)

    # ------------------------------------------------------------------ resolution

    def _place(self, namespace: str, place: str, scopes: set[str]) -> dict[str, Any]:
        if table_exists(self.conn, "geospatial_place_revisions"):
            resolved = self.identity.resolve_place(namespace, place, scopes=scopes, save=False)
            if resolved["status"] == "resolved":
                return resolved
        parsed = parse_code(place)
        if parsed is None:
            from src.kb.agrifood_identity import PLACES
            from src.kb.agrifood_records import label_key

            named = [spec for spec in PLACES.values() if any(label_key(n) == label_key(place) for n in spec["names"])]
            if len(named) == 1:
                return {"query": place, "status": "codes-from-pack-place-list", "place_id": None,
                        "codes": [{"scheme": s, "code": c} for s, c in sorted(named[0]["codes"].items())],
                        "note": "places are not registered in geospatial; codes from the pack's place list"}
            return {"query": place, "status": "unresolved", "place_id": None, "codes": []}
        return {"query": place, "status": "code-only", "place_id": None,
                "codes": [{"scheme": parsed[0], "code": parsed[1]}],
                "note": "no geospatial place carries this code; only series published under it are reached"}

    def _cite(self, value: Mapping[str, Any], vintage: Mapping[str, Any]) -> dict[str, Any]:
        source = value["statement"]["source"]
        return {"url": source["url"], "locator": source["locator"], "attribution": source["attribution"],
                "evidence_origin": source["evidence_origin"], "document_id": value["document_id"],
                "release_key": vintage["release_key"], "vintage_id": vintage["vintage_id"]}

    def _figure(self, value: Mapping[str, Any], vintage: Mapping[str, Any]) -> dict[str, Any]:
        return {"period": value["period"]["value"], "period_key": value["period_key"],
                "reference": value["period"].get("reference"), "period_start": value["period"].get("start"),
                "period_end": value["period"].get("end"), "value": value["value"], "value_text": value["value_text"],
                "status": value["status"], "flag": value["flag"], "estimate_type": value["estimate_type"],
                "estimate_label": ESTIMATE_LABELS[value["estimate_type"]],
                "vintage": {"vintage_id": vintage["vintage_id"], "release_key": vintage["release_key"],
                            "released_at": vintage["released_at"], "release_basis": vintage["release_basis"]},
                "citation": self._cite(value, vintage)}

    # ------------------------------------------------------------------ series

    def series_values(self, namespace: str, series_id: str, *, as_of: Any = None) -> dict[str, Any]:
        cutoff = as_of_ms(as_of)
        vintages = self.store.vintages(namespace, series_id)
        eligible = [v for v in vintages if cutoff is None or v["as_of_ms"] <= cutoff]
        later = [v for v in vintages if v not in eligible]
        chosen: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        for vintage in eligible:  # ordered by release time: a later release replaces the period's figure
            for value in self.store.values(namespace, vintage["vintage_id"]):
                chosen[value["period_key"]] = (value, vintage)
        revised_later = {v["period_key"] for vintage in later for v in self.store.values(namespace,
                                                                                          vintage["vintage_id"])}
        figures = []
        for key in sorted(chosen):
            value, vintage = chosen[key]
            figures.append({**self._figure(value, vintage), "revised_after_as_of": key in revised_later})
        return {"observations": figures, "vintages_used": sorted({f["vintage"]["release_key"] for f in figures}),
                "later_vintages": [{"release_key": v["release_key"], "released_at": v["released_at"]} for v in later],
                "vintages_total": len(vintages)}

    def series_as_of(self, namespace: str, *, commodity: str, place: str, scopes: Iterable[str],
                     as_of: Any = None, measures: Iterable[str] | None = None) -> dict[str, Any]:
        """Per-source series for a commodity and place as published at the as-of time; never blended."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        cutoff = as_of_ms(as_of)
        resolved = self.identity.resolve_commodity(namespace, commodity, scopes=scopes) if self.store.ready() else {
            "codes": [], "status": "not_found"}
        place_answer = self._place(namespace, place, scopes)
        codes = {(c["scheme"], c["code"]): c for c in resolved["codes"]}
        place_codes = {(c["scheme"], c["code"]) for c in place_answer["codes"]}
        wanted = set(measures or [])
        series = [] if not codes or not place_codes else [
            s for s in self.store.series_list(namespace, commodities=codes, places=place_codes)
            if not wanted or s["measure_kind"] in wanted]
        base = {"contract": SERIES_CONTRACT, "namespace": namespace, "commodity_query": commodity,
                "place_query": place, "as_of": as_of, "as_of_ms": cutoff,
                "commodity_resolution": {k: resolved.get(k) for k in ("status", "entry_codes", "pending_crosswalks")},
                "commodity_codes": list(codes.values()), "place": place_answer,
                "selection_basis": "per period, the figure of the latest vintage released at or before the as-of "
                                   "time", "blending": "none: each source's series is returned on its own",
                "never": list(NEVER)}
        if not series:
            return {**base, "status": "none_on_record", "series": [],
                    "notice": "no acquired series for this commodity and place in the bounded coverage; this is not "
                              "a statement that the commodity is not produced or traded there"}
        out, empty = [], 0
        for item in series:
            values = self.series_values(namespace, item["series_id"], as_of=as_of)
            empty += not values["observations"]
            match = codes[(item["commodity_scheme"], item["commodity_code"])]
            out.append({
                "series_id": item["series_id"], "provider": item["provider"], "dataset": item["dataset"],
                "program": item["program"], "record_type": item["record_type"],
                "commodity": {"scheme": item["commodity_scheme"], "code": item["commodity_code"],
                              "label": item["commodity_label"], "match": match["match"], "via": match.get("via", []),
                              **({"notice": match["notice"]} if match.get("notice") else {})},
                "place": {"scheme": item["place_scheme"], "code": item["place_code"], "label": item["place_label"],
                          "level": item["place_level"]},
                "market": item["market"],
                "measure": {"kind": item["measure_kind"], "element": item["element"],
                            "element_code": item["element_code"], "label": item["measure_label"]},
                "unit": item["unit"], "period_type": item["period_type"], **values})
        out.sort(key=lambda s: (s["provider"], s["dataset"], s["measure"]["kind"], s["measure"]["element"],
                                s["series_id"]))
        by_source: dict[str, list[str]] = {}
        for item in out:
            by_source.setdefault(item["provider"], []).append(item["series_id"])
        links = AgrifoodLinks(self.conn, initialize=False).links(namespace, scopes=scopes, commodities=codes,
                                                                 places=place_codes)
        return {**base, "status": "no_vintage_as_of" if empty == len(out) else "found", "series": out,
                "sources": by_source, "links": links}

    # ------------------------------------------------------------------ revisions

    def revisions(self, namespace: str, series_id: str, *, scopes: Iterable[str],
                  period: str | None = None) -> dict[str, Any]:
        """Every vintage of a series' figures with release dates, flags and citations; consecutive diffs."""
        from src.kb.environment_vintages import diff_values

        authorize(namespace, set(scopes), READ_SCOPE)
        series = self.store.series(namespace, series_id)
        vintages = self.store.vintages(namespace, series_id)
        history: dict[str, list[dict[str, Any]]] = {}
        per_vintage = []
        for vintage in vintages:
            values = {v["period_key"]: v for v in self.store.values(namespace, vintage["vintage_id"])
                      if period is None or v["period_key"] == period}
            per_vintage.append((vintage, values))
            for key, value in values.items():
                history.setdefault(key, []).append(self._figure(value, vintage))
        comparisons = []
        for (left, old), (right, new) in zip(per_vintage, per_vintage[1:]):
            shared = sorted(old.keys() & new.keys())  # a vintage that omits a period does not withdraw it

            def view(values):
                return {k: {"value": values[k]["value"], "status": values[k]["flag"]["code"] or values[k]["status"],
                            "unit": series["unit"]} for k in shared}

            changes = diff_values(view(old), view(new), left_id=left["vintage_id"], right_id=right["vintage_id"])
            comparisons.append({"left": {"release_key": left["release_key"], "released_at": left["released_at"]},
                                "right": {"release_key": right["release_key"], "released_at": right["released_at"]},
                                "changes": [{"period_key": c.pop("key"), **c} for c in changes]})
        return {"namespace": namespace, "series_id": series_id, "series": series, "period": period,
                "vintages": [{"release_key": v["release_key"], "released_at": v["released_at"],
                              "release_basis": v["release_basis"], "vintage_id": v["vintage_id"]} for v in vintages],
                "figures": {k: history[k] for k in sorted(history)}, "comparisons": comparisons,
                "notice": "every vintage stays as published; differences are listed, no cause is inferred"}


__all__ = ["ESTIMATE_LABELS", "AgrifoodQueries", "as_of_ms"]
