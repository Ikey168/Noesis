"""Extractive payments per EITI report version and commodity production and reserves side by side (EX08, EX09).

* :meth:`ExtractivesQueries.payments_for_company` - the EITI payments of a company and, on request, of its group
  (the ownership graph as of the same date: stated control-chain tops and their stated subsidiaries), reached only
  through **accepted** company matches. Per report (country and fiscal period) the answer lists every report
  version, the version in force at the as-of date and each payment of that version with the government-reported
  and company-reported figures side by side with EITI's discrepancy as published, in the currency as reported,
  each citing its record revision. Currencies are never converted and nothing is summed across reports; a company
  without a matched payment gets ``no_payment_on_record`` - never a clean bill.
* :meth:`ExtractivesQueries.payments_for_country` - the reports of a country with their versions, revenue streams
  (government-reported totals as published) and payments, companies as reported with their match status.
* :meth:`ExtractivesQueries.production_and_reserves` - a commodity (source commodity code, or an HS heading through
  accepted concordance matches) and a country to production, reserves and trade quantities per source and series,
  the vintage released by the as-of date and its values with status (withheld and estimated values stay marked),
  every vintage on request; USGS and BGS series are listed side by side and never blended.

Nothing reconciles discrepancies beyond the report, estimates reserves, scores risk or forecasts prices.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from src.kb.extractives_records import (
    ANSWER_CONTRACT,
    READ_SCOPE,
    ExtractivesError,
    as_of_ms,
    authorize,
    table_exists,
)
from src.kb.extractives_store import ExtractivesStore

NOTICE = ("Figures as each publisher released them: EITI government- and company-reported amounts side by side "
          "with the discrepancy the report states, in the currency as reported (never converted or summed across "
          "reports); USGS and BGS series side by side, never blended. No reconciliation, own estimate, risk score "
          "or price forecast.")
MAX_GROUP_DEPTH = 5


def _resolve(graph, entity: str) -> str:
    from src.kb.ownership_store import OwnershipError

    try:
        return graph.resolve(entity)
    except OwnershipError as exc:
        for key in graph.entities:
            if any(v["record"].get("canonical_entity_id") == entity for v in graph.entities[key]):
                return graph.cluster(key)
        raise ExtractivesError("not_found", "the company is not an entity of the ownership namespace") from exc


class ExtractivesQueries:
    def __init__(self, conn: Any) -> None:
        from src.kb.extractives_identity import ExtractivesIdentity

        self.conn = conn
        self.store = ExtractivesStore(conn, initialize=False)
        self.identity = ExtractivesIdentity(conn, initialize=False)

    # -------------------------------------------------------------- EITI

    def _version_used(self, namespace: str, report_key: str, as_of: int | None) -> tuple[list[dict], dict | None]:
        versions = self.store.report_versions(namespace, report_key)
        clocks = {r["release_id"]: r["release_at_ms"] for r in self.store.releases(namespace, report_key=report_key)}
        eligible = [v for v in versions if as_of is None or clocks[v["release_id"]] <= as_of]
        for version in versions:
            version["release_at_ms"] = clocks[version["release_id"]]
        return versions, (eligible[-1] if eligible else None)

    def _payment_row(self, namespace: str, view: Mapping[str, Any], clock: int) -> dict[str, Any]:
        body = view["record"]
        stream = self.store.record(namespace, body["stream_key"], as_of_ms=clock)
        company = self.store.record(namespace, body["company_key"], as_of_ms=clock)
        project = self.store.record(namespace, body["project_key"], as_of_ms=clock) if body.get("project_key") \
            else None
        return {
            "record_key": body["record_key"],
            "company": {"record_key": body["company_key"],
                        "name_as_published": (company or {}).get("record", {}).get("name_as_published"),
                        "natural_person": bool((company or {}).get("record", {}).get("natural_person"))},
            "revenue_stream": {"record_key": body["stream_key"],
                               "gfs_code": (stream or {}).get("record", {}).get("gfs_code"),
                               "name_as_published": (stream or {}).get("record", {}).get("name_as_published")},
            "project": None if project is None else {"record_key": body["project_key"],
                                                     "name_as_published": project["record"].get("name_as_published")},
            "government_reported": body.get("government_reported"),
            "company_reported": body.get("company_reported"),
            "discrepancy_as_published": body.get("discrepancy_as_published"),
            "currency_note": "as reported; never converted",
            "citation": self.store.cite(namespace, view),
        }

    def _report_block(self, namespace: str, report_key: str, as_of: int | None, company_keys: set[str] | None,
                      all_versions: bool) -> dict[str, Any] | None:
        versions, used = self._version_used(namespace, report_key, as_of)
        if used is None:
            return None
        report = self.store.record(namespace, report_key, as_of_ms=used["release_at_ms"])

        def payments_at(clock: int) -> list[dict[str, Any]]:
            rows = self.store.records(namespace, record_types=("company_payment",), report_key=report_key,
                                      as_of_ms=clock)
            return [self._payment_row(namespace, v, clock) for v in rows
                    if company_keys is None or v["record"]["company_key"] in company_keys]

        payments = payments_at(used["release_at_ms"])
        body = (report or {}).get("record", {})
        block = {
            "report_key": report_key, "country": body.get("country"), "fiscal_period": body.get("fiscal_period"),
            "title": body.get("title"), "currency_as_reported": body.get("currency"),
            "reconciliation_note": body.get("reconciliation_note"),
            "versions": [{k: v[k] for k in ("release_id", "report_version", "published_on", "evidence_origin")}
                         for v in versions],
            "version_used": {k: used[k] for k in ("release_id", "report_version", "published_on")},
            "later_versions": [v["report_version"] for v in versions if v["release_at_ms"] > used["release_at_ms"]],
            "payments": payments,
            "currencies": sorted({(p.get(side) or {}).get("currency") for p in payments
                                  for side in ("government_reported", "company_reported")} - {None}),
            "citation": self.store.cite(namespace, report) if report else None,
        }
        if all_versions:
            block["per_version"] = [{"report_version": v["report_version"], "release_id": v["release_id"],
                                     "payments": payments_at(v["release_at_ms"])}
                                    for v in versions if as_of is None or v["release_at_ms"] <= as_of]
        return block

    def payments_for_company(self, namespace: str, entity: str, *, ownership_namespace: str, scopes: Iterable[str],
                             as_of: Any = None, group: bool = False, all_versions: bool = False,
                             include_unknowns: bool = False, principal_id: str | None = None) -> dict[str, Any]:
        from src.kb.ownership_graph import OwnershipGraph

        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        clock = as_of_ms(as_of)
        base = {"contract": ANSWER_CONTRACT, "query": "payments_for_company", "namespace": namespace,
                "ownership_namespace": ownership_namespace, "entity": entity, "as_of": as_of, "group": group,
                "notice": NOTICE}
        if not table_exists(self.conn, "ownership_records") or not self.conn.execute(
                "SELECT 1 FROM ownership_records WHERE namespace=? LIMIT 1", [ownership_namespace]).fetchone():
            return {**base, "status": "ownership_absent", "reports": [],
                    "message": "no ownership records are held; company payments are reachable by country only"}
        authorize(ownership_namespace, scopes, "knowledge:ownership:read")
        graph = OwnershipGraph(self.conn, ownership_namespace, principal_id=principal_id, scopes=scopes)
        root = _resolve(graph, entity)
        day = None if clock is None else str(as_of)[:10]
        if group:
            from src.kb.competition_queries import group_members

            members = group_members(graph, root, day, max_depth=MAX_GROUP_DEPTH)
        else:
            members = [{"entity": root, "relation": "self", "path": []}]
        matched: dict[str, dict[str, Any]] = {}
        names = set()
        for member in members:
            keys = graph.members(member["entity"])
            names |= {n["name"] for n in graph.describe(member["entity"])["names"] if n.get("name")}
            for link in self.identity.accepted_company_links(namespace, keys, scopes=scopes):
                matched.setdefault(link["subject_key"], {**link, "group_member": member["entity"],
                                                         "group_relation": member["relation"],
                                                         "ownership_path": member["path"]})
        reports = []
        report_keys = sorted({self.store.record(namespace, k, include_removed=True)["report_key"]
                              for k in matched if self.store.record(namespace, k, include_removed=True)})
        for report_key in report_keys:
            block = self._report_block(namespace, report_key, clock, set(matched), all_versions)
            if block is None:
                continue
            for payment in block["payments"] + [p for v in block.get("per_version", []) for p in v["payments"]]:
                payment["matched_through"] = matched[payment["company"]["record_key"]]
            if block["payments"] or block.get("per_version"):
                reports.append(block)
        answer = {**base, "entity": graph.describe(root),
                  "group_members": [{"entity": m["entity"], "relation": m["relation"], "ownership_path": m["path"]}
                                    for m in members],
                  "matched_companies": sorted(matched.values(), key=lambda m: m["subject_key"]),
                  "status": "answered" if reports else "no_payment_on_record", "reports": reports,
                  "coverage": "only acquired EITI report versions and reviewed company matches are searched; 'no "
                              "payment on record' is not a statement that no payment was made",
                  "totals": "not computed: amounts stay per report, revenue stream and currency as reported"}
        if not reports:
            answer["message"] = "no payment on record for this company's reviewed matches (not a clean bill)"
        if include_unknowns:
            from src.kb.entities import normalize_surface

            wanted = {normalize_surface(n) for n in names}
            answer["unknowns"] = [
                {**u, "unknown": "not matched to this company; a similar name only, never counted"}
                for u in self.identity.unmatched_companies(namespace, scopes=scopes)
                if any(normalize_surface(u["name_as_published"]) == w or
                       (len(w) > 3 and w in normalize_surface(u["name_as_published"])) for w in wanted)]
        return answer

    def payments_for_country(self, namespace: str, country: str, *, scopes: Iterable[str], as_of: Any = None,
                             all_versions: bool = False) -> dict[str, Any]:
        authorize(namespace, scopes, READ_SCOPE)
        clock = as_of_ms(as_of)
        matches = {}
        for view in self.identity.company_candidates(namespace, scopes=scopes):
            if view["state"] == "accepted":
                matches.setdefault(view["subject_key"], []).append(view["candidate_id"])
        reports = []
        for report_key in self.store.report_keys(namespace, country=country):
            block = self._report_block(namespace, report_key, clock, None, all_versions)
            if block is None:
                continue
            used = self.store.releases(namespace, report_key=report_key)
            clock_used = next(r["release_at_ms"] for r in used if r["release_id"] == block["version_used"]["release_id"])
            block["revenue_streams"] = [
                {"record_key": v["record_key"], "gfs_code": v["record"].get("gfs_code"),
                 "name_as_published": v["record"].get("name_as_published"),
                 "government_reported_total": v["record"].get("government_reported"),
                 "citation": self.store.cite(namespace, v)}
                for v in self.store.records(namespace, record_types=("revenue_stream",), report_key=report_key,
                                            as_of_ms=clock_used)]
            for payment in block["payments"]:
                payment["company"]["match_status"] = "matched" if matches.get(payment["company"]["record_key"]) \
                    else "unmatched"
            reports.append(block)
        return {"contract": ANSWER_CONTRACT, "query": "payments_for_country", "namespace": namespace,
                "country": country.upper(), "as_of": as_of,
                "status": "answered" if reports else "no_report_on_record", "reports": reports, "notice": NOTICE,
                "coverage": "only the declared, acquired EITI report versions are searched"}

    def record_history(self, namespace: str, record_key: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        authorize(namespace, scopes, READ_SCOPE)
        history = self.store.history(namespace, record_key)
        return {"contract": ANSWER_CONTRACT, "query": "record_history", "namespace": namespace,
                "record_key": record_key, "status": "answered" if history else "no_record_on_record",
                "revisions": [{**{k: v[k] for k in ("revision", "revision_id", "revision_of", "state",
                                                    "report_version", "as_of", "observed_at", "record")},
                               "citation": self.store.cite(namespace, v)} for v in history],
                "notice": NOTICE}

    # -------------------------------------------------------------- commodities

    def _commodity_codes(self, namespace: str, commodity: str) -> tuple[list[str], dict[str, Any]]:
        if commodity.startswith("hs:"):
            code = commodity.split(":")[-1]
            accepted = [m for m in self.identity.matches(namespace, kind="commodity", state="accepted")
                        if m["target"]["id"].split(":")[-1] == code]
            return sorted({m["subject_key"].split(":", 1)[1] for m in accepted}), {
                "requested": commodity, "resolved_through": [{"match_id": m["match_id"], "method": m["method"],
                                                              "commodity": m["subject_key"]} for m in accepted]}
        return [commodity], {"requested": commodity, "resolved_through": "source commodity code"}

    def production_and_reserves(self, namespace: str, commodity: str, country: str, *, scopes: Iterable[str],
                                as_of: Any = None, all_vintages: bool = False,
                                statistic: str | None = None) -> dict[str, Any]:
        authorize(namespace, scopes, READ_SCOPE)
        clock = as_of_ms(as_of)
        codes, resolution = self._commodity_codes(namespace, commodity)
        sources: dict[str, list[dict[str, Any]]] = {}
        excluded = []
        for code in codes:
            for series in self.store.find_series(namespace, commodity=code, country=country, statistic=statistic):
                vintage = self.store.select_vintage(namespace, series["series_id"], clock)
                if vintage is None:
                    excluded.append({"series_id": series["series_id"], "reason": "no vintage released by the date"})
                    continue
                values = self.store.values(namespace, vintage["vintage_id"])
                restated = {v["period"] for v in values}
                earlier = []
                for older in reversed(self.store.vintage_rows(namespace, series["series_id"])):
                    if older["release_at_ms"] >= vintage["release_at_ms"]:
                        continue
                    for value in self.store.values(namespace, older["vintage_id"]):
                        if value["period"] not in restated:
                            restated.add(value["period"])
                            earlier.append({**value, "vintage_id": older["vintage_id"],
                                            "citation": self.store.source_revision(namespace, older["release_id"])})
                entry = {"series": series, "vintage_used": vintage, "values": values,
                         "periods_not_restated": sorted(earlier, key=lambda v: v["period"]),
                         "periods_not_restated_note": "periods the used vintage does not state, from the same "
                                                      "source's earlier vintage (cited); never from another source",
                         "citation": self.store.source_revision(namespace, vintage["release_id"])}
                if all_vintages:
                    entry["vintages"] = [{**v, "values": self.store.values(namespace, v["vintage_id"]),
                                          "citation": self.store.source_revision(namespace, v["release_id"])}
                                         for v in self.store.vintage_rows(namespace, series["series_id"])
                                         if clock is None or v["release_at_ms"] <= clock]
                sources.setdefault(series["provider"], []).append(entry)
        return {"contract": ANSWER_CONTRACT, "query": "production_and_reserves", "namespace": namespace,
                "commodity": resolution, "country": country.upper(), "as_of": as_of,
                "status": "answered" if sources else "no_series_on_record",
                "sources": [{"provider": p, "series": sources[p]} for p in sorted(sources)],
                "excluded": excluded, "blended": False,
                "side_by_side": "each source's series are listed separately; USGS and BGS figures are never blended "
                                "or averaged", "notice": NOTICE}

    # -------------------------------------------------------------- evidence bundle

    @staticmethod
    def evidence_bundle(answer: Mapping[str, Any]) -> dict[str, Any]:
        """Assertions each citing source, record revision and as-of time of the record behind it."""
        from src.ingestion.extractives_sources import EXCLUSIONS

        bibliography: dict[str, dict[str, Any]] = {}
        assertions = []

        def cite_release(source: Mapping[str, Any]) -> str:
            bibliography.setdefault(source["release_id"], {
                "id": source["release_id"],
                "text": f"{source['provider']} {source.get('document')} (version {source.get('release_version')}, "
                        f"published {source.get('published_on')}, retrieved {source.get('retrieved_at')}, "
                        f"{source.get('evidence_origin')} evidence, {source.get('live_verification')}), "
                        f"{source.get('url')}; {source.get('attribution')}"})
            return source["release_id"]

        def add(identifier: str, text: str, record_id: str, revision: str, as_of: str | None,
                source: Mapping[str, Any]) -> None:
            assertions.append({"id": identifier, "text": text, "kind": "sourced",
                               "dependencies": [{"kind": "source", "namespace": answer.get("namespace"),
                                                 "id": record_id, "revision": revision, "as_of": as_of}],
                               "citations": [cite_release(source)]})

        for report in answer.get("reports") or []:
            groups = [("", report["payments"])] + [(f"v{v['report_version']}-", v["payments"])
                                                    for v in report.get("per_version") or []]
            for prefix, payments in groups:
                for payment in payments:
                    citation = payment["citation"]
                    add(f"{prefix}{payment['record_key']}",
                        f"{payment['company']['name_as_published']} - {payment['revenue_stream']['name_as_published']}"
                        f": government-reported {payment['government_reported']}, company-reported "
                        f"{payment['company_reported']}, discrepancy as published "
                        f"{payment['discrepancy_as_published']} (report version {citation['report_version']})",
                        payment["record_key"], citation["revision_id"], citation["as_of"], citation["source"])
        for source in answer.get("sources") or []:
            for entry in source["series"]:
                series, vintage = entry["series"], entry["vintage_used"]
                add(f"{series['series_id']}:{vintage['vintage_id']}",
                    f"{source['provider']} {series['commodity']['label']} {series['statistic']} "
                    f"({series['unit']}), {series['country']['name']}: "
                    + ", ".join(f"{v['period']}={v['value_text']} [{v['status']}"
                                f"{', estimated' if v['estimated'] else ''}{', revised' if v['revised'] else ''}]"
                                for v in entry["values"]),
                    series["series_id"], vintage["vintage_id"], vintage["release_at"], entry["citation"])
        title = answer.get("query", "answer")
        return {"sections": [{"id": title, "title": f"{title} as of {answer.get('as_of') or 'latest'}",
                              "assertions": assertions}],
                "bibliography": list(bibliography.values()), "exclusions": list(EXCLUSIONS)}


__all__ = ["NOTICE", "ExtractivesQueries"]
