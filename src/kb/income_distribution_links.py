"""Income series linked to methodology documents, Demographics denominators and Labour indicators (#2583, IP07).

Links are made by published identifiers only and record their basis:

* ``citation`` - the source document states the reference or the denominator (provider, series code and
  geography); a methodology reference resolves to an acquired document whose ``url`` or ``canonical_url`` is exactly
  the stated URL;
* ``shared_identifier`` - the named denominator omits the geography and a Demographics series with that provider and
  series code carries exactly the income series' area code; or a Labour series carries the same area scheme and
  code;
* ``accepted_match`` - the income area code and the Labour area code are each accepted as the same Geospatial place
  (the IP06 mapping and :class:`src.kb.labour_identity.LabourIdentity`).

Every link names the record revisions it rests on: the income vintage current when the link was made and the
target's vintage (a Demographics or Labour vintage id). A Demographics or Labour link needs overlapping reference
periods. A missing provider (its store is not held) is recorded as an ``unresolved`` link with the reason
``provider_absent``; an optional link feature the Society bundle has not selected is reported as
``feature_not_selected``; a place without a target is reported under ``missing``. Nothing is dropped silently and
no indicator is derived from a linked series.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from src.kb.income_distribution_records import (
    READ_SCOPE,
    WRITE_SCOPE,
    IncomeError,
    authorize,
    canonical,
    digest,
    feature_state,
    table_exists,
)
from src.kb.income_distribution_store import IncomeStore

CONTRACT = "noesis-income-link-v1"
DEMOGRAPHICS_READ = "knowledge:demographics:read"
LABOUR_READ = "knowledge:labour:read"
BASES = ("citation", "shared_identifier", "accepted_match")
NO_DERIVATION = "A link records what the records state; no indicator is derived and no rate is computed."
_DDL = """
CREATE TABLE IF NOT EXISTS income_links (
  namespace TEXT NOT NULL, link_id TEXT NOT NULL, series_id TEXT NOT NULL, income_vintage_id TEXT,
  kind TEXT NOT NULL, basis TEXT, reference_json TEXT NOT NULL, target_json TEXT, state TEXT NOT NULL,
  evidence_json TEXT NOT NULL, history_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, link_id)
);
"""


def _url(reference: dict[str, Any]) -> str | None:
    for value in (reference.get("url"), reference.get("identifier")):
        if isinstance(value, str) and value.startswith(("https://", "http://")):
            return value
    return None


def _need(scopes: set[str], scope: str) -> None:
    if scope not in scopes and "operator" not in scopes:
        raise IncomeError("unauthorized", f"{scope} is required to resolve these links")


class IncomeLinks:
    def __init__(self, conn: Any, *, now=None, initialize: bool = True) -> None:
        self.conn = conn
        self.store = IncomeStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        if initialize:
            conn.execute(_DDL)

    def _put(self, namespace, series, kind, basis, reference, target, state, evidence, principal_id):
        vintage_id = series["current_vintage_id"]
        link_id = "inc-link:" + digest([namespace, series["series_id"], vintage_id, kind, basis, reference, target,
                                        state])[:24]
        if self.conn.execute("SELECT 1 FROM income_links WHERE namespace=? AND link_id=?",
                             [namespace, link_id]).fetchone():
            return link_id, False
        now = self.now()
        self.conn.execute(
            "INSERT INTO income_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, link_id, series["series_id"], vintage_id, kind, basis, canonical(reference),
             None if target is None else canonical(target), state, canonical({**evidence, "note": NO_DERIVATION}),
             canonical([{"state": state, "by": principal_id, "at_ms": now}]), principal_id, now])
        return link_id, True

    def _periods(self, series: dict[str, Any]) -> list[str]:
        if not series["current_vintage_id"]:
            return []
        return [o["period"] for o in self.store.observations(series["namespace"], series["current_vintage_id"])]

    # ------------------------------------------------------------------ references

    def link_references(self, namespace: str, *, principal_id: str, scopes: Iterable[str]) -> dict[str, Any]:
        """Resolve every stated methodology reference by its exact URL; unresolvable references stay citations."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        linked, unresolved = [], []
        documents = table_exists(self.conn, "documents")
        for series in self.store.find_series(namespace):
            for reference in series["references"]:
                reference = dict(reference)
                url = _url(reference)
                targets = [{"kind": "document", "id": r[0], "title": r[1], "url": r[2]} for r in self.conn.execute(
                    "SELECT document_id, title, url FROM documents WHERE url=? OR canonical_url=? ORDER BY document_id",
                    [url, url]).fetchall()] if url and documents else []
                evidence = {"stated_in": series["dataflow"], "lookup": "exact URL" if url else "no URL identifier"}
                if len(targets) == 1:
                    link_id, new = self._put(namespace, series, "reference", "citation", reference, targets[0],
                                             "linked", evidence, principal_id)
                    (linked if new else []).append(link_id)
                else:
                    evidence["reason"] = ("several documents carry this URL" if targets else
                                          "the cited document is not held; kept as the published citation")
                    link_id, new = self._put(namespace, series, "reference", None, reference, None, "unresolved",
                                             evidence, principal_id)
                    (unresolved if new else []).append(link_id)
        return {"linked": linked, "unresolved": unresolved}

    # ------------------------------------------------------------------ demographics

    def _demographic_vintage(self, namespace: str, series_id: str) -> tuple[str | None, list[str]]:
        row = self.conn.execute(
            "SELECT vintage_id FROM demographic_vintages WHERE namespace=? AND series_id=? "
            "ORDER BY release_at_ms DESC, sequence DESC LIMIT 1", [namespace, series_id]).fetchone()
        if row is None:
            return None, []
        periods = [r[0] for r in self.conn.execute(
            "SELECT period FROM demographic_observations WHERE namespace=? AND vintage_id=? ORDER BY period",
            [namespace, row[0]]).fetchall()] if table_exists(self.conn, "demographic_observations") else []
        return row[0], periods

    def link_demographics(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                          demographic_namespace: str = "global") -> dict[str, Any]:
        """Link series to the Demographics denominator their source names, by exact identifiers only."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        state = feature_state(self.conn, "demographics-links")
        if state == "not_selected":
            return {"status": "feature_not_selected", "feature": "demographics-links", "linked": [],
                    "unresolved": [], "missing": []}
        held = table_exists(self.conn, "demographic_series") and table_exists(self.conn, "demographic_vintages")
        if held:
            _need(scopes, DEMOGRAPHICS_READ)
        linked, unresolved, missing = [], [], []
        for series in self.store.find_series(namespace):
            named = series.get("denominator")
            if not named:
                continue
            geography = named.get("geography_code")
            basis = "citation" if geography else "shared_identifier"
            geography = str(geography or series["area"]["code"])
            evidence = {"named_by_source": named, "geography": geography,
                        "geography_basis": "stated by the source" if basis == "citation"
                        else "the income series' own published area code"}
            if not held:
                evidence["reason"] = "provider_absent: the Economics demographics store is not held"
                link_id, new = self._put(namespace, series, "denominator", None, named, None, "unresolved", evidence,
                                         principal_id)
                (unresolved if new else []).append(link_id)
                continue
            rows = self.conn.execute(
                "SELECT series_id, indicator, geography_code FROM demographic_series WHERE namespace=? AND provider=? "
                "AND series_code=? AND geography_code=? ORDER BY series_id",
                [demographic_namespace, str(named.get("provider")), str(named.get("series_code")), geography]
            ).fetchall()
            if len(rows) != 1:
                evidence["reason"] = "several demographic series match" if rows else "the named series is not held"
                link_id, new = self._put(namespace, series, "denominator", None, named, None, "unresolved", evidence,
                                         principal_id)
                (unresolved if new else []).append(link_id)
                missing.append({"series_id": series["series_id"], "reason": evidence["reason"]})
                continue
            vintage_id, periods = self._demographic_vintage(demographic_namespace, rows[0][0])
            overlap = sorted(set(periods) & set(self._periods(series)))
            target = {"kind": "demographic-series", "namespace": demographic_namespace, "id": rows[0][0],
                      "vintage_id": vintage_id, "indicator": rows[0][1], "geography_code": rows[0][2]}
            evidence["overlapping_periods"] = overlap
            if not overlap:
                evidence["reason"] = "no reference period in common"
                missing.append({"series_id": series["series_id"], "reason": evidence["reason"], "target": target})
                continue
            link_id, new = self._put(namespace, series, "denominator", basis, named, target, "linked", evidence,
                                     principal_id)
            (linked if new else []).append(link_id)
        return {"status": "evaluated", "feature_state": state, "linked": linked, "unresolved": unresolved,
                "missing": missing}

    # ------------------------------------------------------------------ labour

    def link_labour(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                    labour_namespace: str = "global") -> dict[str, Any]:
        """Link series to Labour headline series of the same place and period (shared code or accepted matches)."""
        from src.kb.income_distribution_identity import IncomeIdentity

        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        state = feature_state(self.conn, "labour-links")
        if state == "not_selected":
            return {"status": "feature_not_selected", "feature": "labour-links", "linked": [], "unresolved": [],
                    "missing": []}
        held = table_exists(self.conn, "labour_series") and table_exists(self.conn, "labour_vintages")
        linked, unresolved, missing = [], [], []
        if held:
            _need(scopes, LABOUR_READ)
            from src.kb.labour_identity import LabourIdentity
            from src.kb.labour_statistics import LabourStore

            labour = LabourStore(self.conn, initialize=False)
            labour_identity = LabourIdentity(self.conn, initialize=False) if table_exists(
                self.conn, "labour_identity_assertions") else None
            candidates = [s for s in labour.find_series(labour_namespace) if not s["sector"] and not s["occupation"]
                          and s["current_vintage_id"]]
        identity = IncomeIdentity(self.conn, initialize=False) if table_exists(
            self.conn, "income_identity_assertions") else None
        for series in self.store.find_series(namespace):
            if not series["current_vintage_id"] or series["current_status"] != "published":
                continue
            if not held:
                reference = {"place": series["area"], "provider": "economics.labour"}
                evidence = {"reason": "provider_absent: the Economics labour store is not held"}
                link_id, new = self._put(namespace, series, "labour-indicator", None, reference, None, "unresolved",
                                         evidence, principal_id)
                (unresolved if new else []).append(link_id)
                continue
            accepted = identity.place_for_area(namespace, series["area"]["scheme"], series["area"]["code"]) \
                if identity else None
            place_id = accepted["target"]["place_id"] if accepted and accepted["state"] == "accepted" else None
            periods = set(self._periods(series))
            found = 0
            for other in candidates:
                if (other["area"]["scheme"], str(other["area"]["code"])) == (series["area"]["scheme"],
                                                                             str(series["area"]["code"])):
                    basis, via = "shared_identifier", {"scheme": series["area"]["scheme"],
                                                       "code": series["area"]["code"]}
                elif place_id and labour_identity is not None:
                    theirs = labour_identity.place_for_area(labour_namespace, other["area"]["scheme"],
                                                            other["area"]["code"])
                    if not theirs or theirs["state"] != "accepted" or theirs["target"]["place_id"] != place_id:
                        continue
                    basis, via = "accepted_match", {"place_id": place_id, "income_assertion": accepted["assertion_id"],
                                                    "labour_assertion": theirs["assertion_id"]}
                else:
                    continue
                labour_periods = {o["period"] for o in labour.observations(labour_namespace,
                                                                           other["current_vintage_id"])}
                overlap = sorted(periods & labour_periods)
                if not overlap:
                    continue
                target = {"kind": "labour-series", "namespace": labour_namespace, "id": other["series_id"],
                          "vintage_id": other["current_vintage_id"], "provider": other["provider"],
                          "native_key": other["native_key"], "concept": other["indicator"]["concept"]}
                link_id, new = self._put(namespace, series, "labour-indicator", basis, {"place": series["area"]},
                                         target, "linked", {"via": via, "overlapping_periods": overlap}, principal_id)
                (linked if new else []).append(link_id)
                found += 1
            if not found:
                missing.append({"series_id": series["series_id"], "area": series["area"],
                                "reason": "no Labour series of the same place and period is held"})
        return {"status": "evaluated", "feature_state": state, "linked": linked, "unresolved": unresolved,
                "missing": missing}

    # ------------------------------------------------------------------ reads

    def link(self, namespace: str, link_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT link_id, series_id, income_vintage_id, kind, basis, reference_json, target_json, state, "
            "evidence_json, history_json, created_by, created_at_ms FROM income_links WHERE namespace=? AND link_id=?",
            [namespace, link_id]).fetchone()
        if row is None:
            raise IncomeError("not_found", "link is not visible in this namespace")
        return {"contract": CONTRACT, "namespace": namespace, "link_id": row[0], "series_id": row[1],
                "income_vintage_id": row[2], "kind": row[3], "basis": row[4], "reference": json.loads(row[5]),
                "target": None if row[6] is None else json.loads(row[6]), "state": row[7],
                "evidence": json.loads(row[8]), "history": json.loads(row[9]), "created_by": row[10],
                "created_at_ms": row[11]}

    def links(self, namespace: str, *, scopes: Iterable[str], series_id: str | None = None, kind: str | None = None,
              state: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not table_exists(self.conn, "income_links"):
            return []
        rows = self.conn.execute(
            "SELECT link_id FROM income_links WHERE namespace=? AND (? IS NULL OR series_id=?) AND "
            "(? IS NULL OR kind=?) AND (? IS NULL OR state=?) ORDER BY series_id, kind, link_id",
            [namespace, series_id, series_id, kind, kind, state, state]).fetchall()
        return [self.link(namespace, r[0]) for r in rows]


__all__ = ["BASES", "CONTRACT", "NO_DERIVATION", "IncomeLinks"]
