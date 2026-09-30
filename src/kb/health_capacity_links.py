"""Capacity indicators beside surveillance series and cited Economics denominators (#2215, HS07).

* **Beside surveillance.** For a Geospatial place, the capacity indicators and the surveillance series already in
  the pack whose geography codes resolve to that place (HS06 resolutions that are used: matched and not rejected)
  are returned side by side. No combined metric (per-bed rates, cases per doctor ...) is computed. Codes whose
  resolution to the place was rejected are listed as excluded, never shown.
* **Economics by citation.** A capacity series links to an Economics series only where its publisher cites the
  denominator series (the document's ``denominator.cites``: provider, series code and locator). The Economics
  series is read from its own map (``economic_series_map``, read-only; Economics is never modified). A citation
  whose series is not held is recorded as ``cited-not-held``; a series without a citation has no link.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from src.kb.health_capacity import SCHEME, NEVER_SENTENCE, HealthCapacityError
from src.kb.health_capacity_comparability import HealthCapacityComparability
from src.kb.surveillance import READ_SCOPE, WRITE_SCOPE, authorize, canonical, digest, table_exists

CONTRACT = "noesis-health-capacity-link-v1"
_DDL = """
CREATE TABLE IF NOT EXISTS health_capacity_economic_links (
  namespace TEXT NOT NULL, link_id TEXT NOT NULL, series_id TEXT NOT NULL, vintage_id TEXT NOT NULL,
  state TEXT NOT NULL, citation_json TEXT NOT NULL, locator TEXT, economic_series_json TEXT NOT NULL,
  created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, link_id)
);
"""


def _summary(series: dict[str, Any]) -> dict[str, Any]:
    return {
        "series_id": series["series_id"],
        "provider": series["provider"],
        "condition": series["condition"],
        "indicator": series["indicator"],
        "place": series["geography"],
        "unit": series["unit"]["label"],
        "interval": series["interval"],
        "kind": series["kind"],
        "current_vintage_id": series["current_vintage_id"],
        "citations": series["citations"],
    }


class HealthCapacityLinks:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        self.conn = conn
        self.comparability = HealthCapacityComparability(conn, initialize=initialize, now=now)
        self.store = self.comparability.store
        self.now = self.store.now
        if initialize:
            conn.execute(_DDL)

    def beside_surveillance(self, namespace: str, place_id: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        authorize(namespace, set(scopes), READ_SCOPE)
        resolutions = [r for r in self.comparability.resolutions(namespace, scopes={"operator"})
                       if r["place_id"] == place_id]
        used = [r for r in resolutions if r["used"]]
        capacity, surveillance = [], []
        for resolution in used:
            for series in self.store.find_series(namespace, geography_system=resolution["geography_system"],
                                                 geography_code=resolution["geography_code"]):
                target = capacity if series["condition"].get("scheme") == SCHEME else surveillance
                target.append({**_summary(series), "resolution_id": resolution["resolution_id"]})
        return {
            "contract": CONTRACT,
            "place_id": place_id,
            "status": "resolved" if used else "no_resolved_codes",
            "capacity": capacity,
            "surveillance": surveillance,
            "excluded": [{"resolution_id": r["resolution_id"], "geography_system": r["geography_system"],
                          "geography_code": r["geography_code"], "review_state": r["review_state"]}
                         for r in resolutions if not r["used"]],
            "boundary": NEVER_SENTENCE,
            "note": "shown side by side for the same place; no combined metric is computed",
        }

    def _economic_series(self, provider: str, code: str) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "economic_series_map"):
            return []
        rows = self.conn.execute(
            "SELECT domain, series_id, indicator_id, provider, provider_code, source_url FROM economic_series_map "
            "WHERE lower(provider)=lower(?) AND provider_code=? ORDER BY domain, series_id", [provider, code],
        ).fetchall()
        return [dict(zip(("domain", "series_id", "indicator_id", "provider", "provider_code", "source_url"), r))
                for r in rows]

    def link_economics(self, namespace: str, *, principal_id: str, scopes: Iterable[str]) -> dict[str, Any]:
        """Link every capacity series whose publisher cites its denominator series; idempotent."""
        authorize(namespace, set(scopes), WRITE_SCOPE, write=True)
        linked, not_held, without = [], [], []
        for series in self.store.find_series(namespace, condition_scheme=SCHEME):
            vintages = self.store.vintage_rows(namespace, series["series_id"])
            if not vintages:
                continue
            current = vintages[-1]
            denominator = current["metadata"].get("denominator")
            cites = denominator.get("cites") if isinstance(denominator, dict) else None
            if not isinstance(cites, dict) or not cites.get("provider") or not cites.get("provider_code"):
                without.append(series["series_id"])
                continue
            targets = self._economic_series(str(cites["provider"]), str(cites["provider_code"]))
            state = "linked" if targets else "cited-not-held"
            link_id = "hc-econ:" + digest([namespace, series["series_id"], cites, [t["series_id"] for t in targets]])[:24]
            if not self.conn.execute("SELECT 1 FROM health_capacity_economic_links WHERE namespace=? AND link_id=?",
                                     [namespace, link_id]).fetchone():
                self.conn.execute(
                    "INSERT INTO health_capacity_economic_links VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [namespace, link_id, series["series_id"], current["vintage_id"], state,
                     canonical({"denominator": denominator.get("text"), **cites}), cites.get("locator"),
                     canonical(targets), principal_id, self.now()])
            (linked if targets else not_held).append(link_id)
        return {"contract": CONTRACT, "linked": linked, "cited_not_held": not_held,
                "without_citation": without,
                "note": "links only where the publisher cites the denominator series; Economics is read, never "
                "modified"}

    def links(self, namespace: str, *, scopes: Iterable[str], series_id: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not table_exists(self.conn, "health_capacity_economic_links"):
            return []
        rows = self.conn.execute(
            "SELECT link_id, series_id, vintage_id, state, citation_json, locator, economic_series_json, created_by, "
            "created_at_ms FROM health_capacity_economic_links WHERE namespace=? AND (? IS NULL OR series_id=?) "
            "ORDER BY series_id, created_at_ms", [namespace, series_id, series_id]).fetchall()
        return [{"contract": CONTRACT, "record_type": "economic-denominator-link",
                 **dict(zip(("link_id", "series_id", "vintage_id", "state"), r[:4])),
                 "citation": json.loads(r[4]), "locator": r[5], "economic_series": json.loads(r[6]),
                 "created_by": r[7], "created_at_ms": r[8]} for r in rows]

    def series_links(self, namespace: str, series_id: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        """The cited Economics denominator of one capacity series: linked, cited-not-held, or none cited."""
        authorize(namespace, set(scopes), READ_SCOPE)
        series = self.store.series(namespace, series_id)
        if series["condition"].get("scheme") != SCHEME:
            raise HealthCapacityError("not_found", "series is not a health-capacity indicator")
        found = self.links(namespace, scopes=scopes, series_id=series_id)
        return {"series_id": series_id, "status": found[-1]["state"] if found else "no_denominator_citation",
                "links": found}


__all__ = ["CONTRACT", "HealthCapacityLinks"]
