"""Extractive payments per report version and commodity figures with sources side by side (#2653, EX08, EX09).

* :meth:`ExtractivesQueries.payments_for_company` - given an extractives company (its subject key) or an ownership
  entity (and, optionally, its group through the ownership graph as of a date), the payments of every matched
  reporting company per EITI report revision in force at the as-of time and per revenue stream: the
  government-reported and company-reported figures side by side with the report's own discrepancies. Each figure
  keeps the currency the report states; nothing is converted, summed across reports or reconciled. Every row cites
  its report revision (report, version, release, retrieval time).
* :meth:`ExtractivesQueries.payments_for_country` - the reports of a country as of a date with their government
  revenues by stream, company lines and discrepancies, cited per revision.
* :meth:`ExtractivesQueries.production` - given a commodity (a name as published or an HS code through accepted
  mappings) and a country (a name as published or an ISO alpha-3 code through accepted mappings), production,
  reserves and other statistics per source by vintage: USGS and BGS series side by side and never blended,
  withheld, unavailable and estimated values marked, each figure citing its vintage.
* :meth:`ExtractivesQueries.export_bundle` - a ``noesis-evidence-bundle-v1`` citing every item with source,
  record revision and as-of time.

A subject without records answers ``no_payment_on_record`` or ``none_published`` - never zero. Nothing scores risk,
estimates reserves, forecasts or reconciles.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from src.ingestion.extractives_sources import EXCLUSIONS, NEVER_SENTENCE, STATISTICS
from src.kb.extractives_records import (
    ANSWER_CONTRACT,
    READ_SCOPE,
    SUBJECT_PREFIX,
    ExtractivesError,
    authorize,
    canonical,
    digest,
    iso_from_ms,
    table_exists,
)
from src.kb.extractives_store import ExtractivesStore

NOTICE = ("Figures as each source published them. Government- and company-reported payments and EITI's own "
          "discrepancies are side by side; amounts keep the currency the report states and are never converted or "
          "summed across reports; USGS and BGS series are never blended and withheld values never filled. No risk "
          "score, reserve estimate or forecast.")


def _no_method(value: Any) -> Any:
    """Bundle payloads reserve ``method``/``n``/``assumptions`` for analytic honesty envelopes."""
    if isinstance(value, Mapping):
        return {({"method": "match_method", "n": "count", "assumptions": "stated_assumptions"}.get(k, k)):
                _no_method(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_no_method(v) for v in value]
    return value


class ExtractivesQueries:
    def __init__(self, conn: Any, *, now=None) -> None:
        self.conn = conn
        self.store = ExtractivesStore(conn, initialize=False, now=now)

    def _identity(self):
        from src.kb.extractives_identity import ExtractivesIdentity

        return ExtractivesIdentity(self.conn, initialize=False, now=self.store.now)

    # ------------------------------------------------------------------ citations

    def _report_citation(self, namespace: str, report: Mapping[str, Any], as_of_ms: int | None) -> dict[str, Any]:
        revision = self.store.source_revision(namespace, report["release_id"])
        return {"report_id": report["report_id"], "report_key": report["report_key"], "revision": report["revision"],
                "revision_of": report["revision_of"], "status": report["status"],
                "report": report["report"], "country": report["country"], "fiscal_period": report["fiscal_period"],
                "currency": report["currency"], "release_at": report["release_at"],
                "retrieved_at": report["retrieved_at"], "selected_as_of": iso_from_ms(as_of_ms),
                "source_revision": revision}

    # ------------------------------------------------------------------ subjects

    def _subjects(self, namespace: str, company: str, *, scopes: set[str], ownership_namespace: str | None,
                  group: bool, as_of: str | None, principal_id: str | None) -> dict[str, Any]:
        """The extractives company keys a request reaches, with the accepted match and group member used."""
        if company.startswith(SUBJECT_PREFIX + "company:"):
            return {"entity": {"subject_key": company}, "group_members": [],
                    "subjects": [{"subject_key": company, "group_member": None, "matched_through": None}],
                    "status": "direct"}
        if not ownership_namespace:
            raise ExtractivesError("invalid_query", "an ownership entity needs its ownership_namespace")
        if not table_exists(self.conn, "ownership_records"):
            return {"entity": {"requested": company}, "group_members": [], "subjects": [],
                    "status": "ownership_unavailable",
                    "message": "the Corporate Ownership store is not held; ask by extractives company key instead"}
        from src.kb.competition_queries import _graph, _resolve, group_members
        from src.kb.ownership_records import READ_SCOPE as OWNERSHIP_READ

        authorize(ownership_namespace, scopes, OWNERSHIP_READ)
        graph = _graph(self.conn, ownership_namespace, scopes, principal_id, None)
        try:
            root = _resolve(graph, company)
        except Exception as exc:
            raise ExtractivesError("not_found", "the company is not an entity of the ownership namespace") from exc
        members = group_members(graph, root, as_of) if group else [{"entity": root, "relation": "self", "path": []}]
        identity = self._identity()
        subjects = []
        for member in members:
            keys = graph.members(member["entity"])
            for link in identity.accepted_company_links(namespace, keys, scopes=scopes):
                subjects.append({"subject_key": link["subject_key"], "group_member": member["entity"],
                                 "group_relation": member["relation"], "ownership_path": member["path"],
                                 "matched_through": link})
        seen, unique = set(), []
        for subject in sorted(subjects, key=lambda s: (s["subject_key"], s["matched_through"]["candidate_id"])):
            if subject["subject_key"] not in seen:
                seen.add(subject["subject_key"])
                unique.append(subject)
        return {"entity": graph.describe(root), "group_members": [
            {"entity": m["entity"], "relation": m["relation"], "ownership_path": m["path"]} for m in members],
            "subjects": unique, "status": "resolved"}

    # ------------------------------------------------------------------ payments

    def _stream_rows(self, namespace: str, report: Mapping[str, Any], company_keys: set[str] | None
                     ) -> list[dict[str, Any]]:
        lines = self.store.payments(namespace, report["report_id"])
        discrepancies = self.store.discrepancies(namespace, report["report_id"])
        groups: dict[str, dict[str, Any]] = {}
        for line in lines:
            if company_keys is not None and line["company_key"] not in company_keys:
                continue
            stream = line["revenue_stream"]
            key = canonical([line["company_key"], stream.get("gfs_code"), stream.get("name_as_reported")])
            group = groups.setdefault(key, {
                "company_key": line["company_key"],
                "company": None if not line["company"] else {
                    k: line["company"].get(k) for k in ("name_as_reported", "identifiers", "redacted")},
                "revenue_stream": stream, "government_reported": [], "company_reported": [], "discrepancies": []})
            figure = {k: line[k] for k in ("line_key", "agency", "project", "amount_text", "amount", "currency",
                                           "in_kind")}
            group["government_reported" if line["reported_by"] == "government" else "company_reported"].append(figure)
        for disc in discrepancies:
            if company_keys is not None and disc["company_key"] not in company_keys:
                continue
            stream = disc["revenue_stream"]
            key = canonical([disc["company_key"], stream.get("gfs_code"), stream.get("name_as_reported")])
            group = groups.setdefault(key, {
                "company_key": disc["company_key"], "company": None if not disc["company"] else {
                    k: disc["company"].get(k) for k in ("name_as_reported", "identifiers", "redacted")},
                "revenue_stream": stream, "government_reported": [], "company_reported": [], "discrepancies": []})
            group["discrepancies"].append({k: disc[k] for k in (
                "line", "government_amount_text", "company_amount_text", "discrepancy_text", "currency",
                "explanation", "basis")})
        out = []
        for key in sorted(groups):
            group = groups[key]
            currencies = sorted({f["currency"] for side in ("government_reported", "company_reported")
                                 for f in group[side] if f["currency"]} | {d["currency"] for d in group["discrepancies"]
                                                                          if d["currency"]})
            group["currencies"] = currencies
            group["side_by_side"] = {"government_reported": bool(group["government_reported"]),
                                     "company_reported": bool(group["company_reported"]),
                                     "eiti_discrepancy_published": bool(group["discrepancies"])}
            out.append(group)
        return out

    def payments_for_company(self, namespace: str, company: str, *, scopes: Iterable[str],
                             ownership_namespace: str | None = None, group: bool = False, as_of_ms: int | None = None,
                             as_of: str | None = None, history: bool = False, principal_id: str | None = None
                             ) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        resolved = self._subjects(namespace, company, scopes=scopes, ownership_namespace=ownership_namespace,
                                  group=group, as_of=as_of, principal_id=principal_id)
        by_subject = {s["subject_key"]: s for s in resolved["subjects"]}
        reports, unavailable = [], []
        if by_subject and self.store.ready():
            keys = set(by_subject)
            for report_key in self.store.report_keys(namespace):
                revisions = self.store.report_revisions(namespace, report_key)
                if not any(p["company_key"] in keys for r in revisions
                           for p in self.store.payments(namespace, r["report_id"])):
                    continue
                report, reason = self.store.report_as_of(namespace, report_key, as_of_ms=as_of_ms)
                if report is None:
                    unavailable.append({"report_key": report_key, "reason": reason})
                    continue
                streams = self._stream_rows(namespace, report, keys)
                for stream in streams:
                    subject = by_subject.get(stream["company_key"]) or {}
                    stream["group_member"] = subject.get("group_member")
                    stream["matched_through"] = subject.get("matched_through")
                entry = {"citation": self._report_citation(namespace, report, as_of_ms), "streams": streams,
                         "note": "figures of this report revision only; never summed with another report"}
                if not streams:
                    entry["note"] = "the company's lines are absent from the revision in force (e.g. withdrawn)"
                if history:
                    entry["revision_history"] = [{k: r[k] for k in ("report_id", "revision", "revision_of", "status",
                                                                    "release_at", "changes")} for r in revisions]
                reports.append(entry)
        reports.sort(key=lambda r: (r["citation"]["country"]["code"], r["citation"]["fiscal_period"]["start"]))
        answer = {
            "contract": ANSWER_CONTRACT, "namespace": namespace, "question": "payments-for-company",
            "query": {"company": company, "ownership_namespace": ownership_namespace, "group": group,
                      "as_of_ms": as_of_ms, "as_of": as_of},
            "as_of_ms": as_of_ms, "entity": resolved["entity"], "group_members": resolved["group_members"],
            "matched_companies": [{k: s.get(k) for k in ("subject_key", "group_member", "matched_through")}
                                  for s in resolved["subjects"]],
            "status": ("ownership_unavailable" if resolved["status"] == "ownership_unavailable" else
                       "reported" if any(r["streams"] for r in reports) else "no_payment_on_record"),
            "reports": reports, "unavailable_by_as_of": unavailable,
            "coverage": "only the declared, acquired EITI summaries and reviewed company matches are searched; "
                        "'no payment on record' is not a statement that no payment was made",
            "exclusions": list(EXCLUSIONS), "never": NEVER_SENTENCE, "notice": NOTICE,
        }
        if resolved.get("message"):
            answer["message"] = resolved["message"]
        answer["receipt"] = {"digest": digest([answer["query"], [r["citation"]["report_id"] for r in reports]])}
        return answer

    def payments_for_country(self, namespace: str, country_code: str, *, scopes: Iterable[str],
                             as_of_ms: int | None = None, history: bool = False) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        reports, unavailable = [], []
        for report_key in self.store.report_keys(namespace, country_code=str(country_code).upper()):
            report, reason = self.store.report_as_of(namespace, report_key, as_of_ms=as_of_ms)
            if report is None:
                unavailable.append({"report_key": report_key, "reason": reason})
                continue
            entry = {"citation": self._report_citation(namespace, report, as_of_ms),
                     "government_revenues": [s for s in self._stream_rows(namespace, report, None)
                                             if s["company_key"] is None],
                     "companies": [s for s in self._stream_rows(namespace, report, None) if s["company_key"]],
                     "note": "figures of this report revision only; never summed with another report"}
            if history:
                entry["revision_history"] = [{k: r[k] for k in ("report_id", "revision", "revision_of", "status",
                                                                "release_at", "changes")}
                                             for r in self.store.report_revisions(namespace, report_key)]
            reports.append(entry)
        answer = {"contract": ANSWER_CONTRACT, "namespace": namespace, "question": "payments-for-country",
                  "query": {"country": str(country_code).upper(), "as_of_ms": as_of_ms}, "as_of_ms": as_of_ms,
                  "status": "reported" if reports else "no_report_on_record", "reports": reports,
                  "unavailable_by_as_of": unavailable, "exclusions": list(EXCLUSIONS), "never": NEVER_SENTENCE,
                  "notice": NOTICE}
        answer["receipt"] = {"digest": digest([answer["query"], [r["citation"]["report_id"] for r in reports]])}
        return answer

    # ------------------------------------------------------------------ production and reserves

    def production(self, namespace: str, *, commodity: Any, country: Any, scopes: Iterable[str],
                   statistic: str | None = None, as_of_ms: int | None = None, history: bool = False
                   ) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if statistic is not None and statistic not in STATISTICS:
            raise ExtractivesError("invalid_query", f"statistic is one of {STATISTICS}")
        identity = self._identity()
        commodities = identity.commodity_keys_for(namespace, commodity)
        countries = identity.country_names_for(namespace, country)
        keys = {k["commodity_key"] for k in commodities["keys"]}
        names = {n["name"] for n in countries["names"]}
        results, unavailable = [], []
        for series in self.store.find_series(namespace, statistic=statistic, commodity_keys=keys, country_names=names):
            if countries.get("code") and not series["country"].get("code") and not any(
                    n.get("provider") in (None, series["provider"]) and n["name"] == series["country"].get("name")
                    for n in countries["names"]):
                continue
            vintage, reason = self.store.select_vintage(namespace, series["series_id"], as_of_ms=as_of_ms)
            if vintage is None:
                unavailable.append({"series_id": series["series_id"], "provider": series["provider"],
                                    "reason": reason})
                continue
            revision = self.store.source_revision(namespace, vintage["release_id"])
            cite = {"provider": series["provider"], "source_id": revision["source_id"],
                    "vintage_id": vintage["vintage_id"], "publication": vintage["publication"],
                    "release_at": vintage["release_at"], "retrieved_at": vintage["retrieved_at"],
                    "file_sha256": revision["file_sha256"], "licence": revision["licence"]}
            observations = self.store.observations(namespace, vintage["vintage_id"])
            match = next((k for k in commodities["keys"] if k["commodity_key"] == series["commodity_key"]), None)
            results.append({
                "series_id": series["series_id"], "provider": series["provider"], "commodity": series["commodity"],
                "statistic": series["statistic"], "unit": series["unit"], "country": series["country"],
                "commodity_matched_by": match,
                "vintage": {**vintage, "source_revision": revision, "selected_as_of": iso_from_ms(as_of_ms)},
                "values": [{**o, "citation": cite} for o in observations],
                "marked": {"withheld": [o["period"] for o in observations if o["status"] == "withheld"],
                           "not_available": [o["period"] for o in observations if o["status"] == "not_available"],
                           "estimated": [o["period"] for o in observations if o["estimated"]],
                           "revised": [o["period"] for o in observations if o["revised"]],
                           "note": "withheld and unavailable values are not filled; estimates are the publisher's"},
                **({"vintage_history": [
                    {**{k: v[k] for k in ("vintage_id", "publication", "release_at", "retrieved_at", "revision_of",
                                          "changes")},
                     "values": self.store.observations(namespace, v["vintage_id"])}
                    for v in self.store.vintage_rows(namespace, series["series_id"])]} if history else {}),
            })
        by_source: dict[str, list[str]] = {}
        for result in results:
            by_source.setdefault(result["provider"], []).append(result["series_id"])
        answer = {
            "contract": ANSWER_CONTRACT, "namespace": namespace, "question": "commodity-production",
            "query": {"commodity": commodity, "country": country, "statistic": statistic, "as_of_ms": as_of_ms},
            "as_of_ms": as_of_ms, "commodity": commodities, "country": countries,
            "status": "reported" if results else "none_published", "results": results, "by_source": by_source,
            "unavailable_by_as_of": unavailable, "side_by_side": True, "never_blended": True,
            "exclusions": list(EXCLUSIONS), "never": NEVER_SENTENCE, "notice": NOTICE,
        }
        answer["receipt"] = {"digest": digest([answer["query"], [r["vintage"]["vintage_id"] for r in results]])}
        return answer

    def series_history(self, namespace: str, series_id: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        authorize(namespace, set(scopes), READ_SCOPE)
        series = self.store.series(namespace, series_id)
        return {"contract": ANSWER_CONTRACT, "series": series, "vintages": [
            {**v, "source_revision": self.store.source_revision(namespace, v["release_id"]),
             "observations": self.store.observations(namespace, v["vintage_id"])}
            for v in self.store.vintage_rows(namespace, series_id)],
            "note": "every retained vintage; earlier values are never overwritten"}

    def report_history(self, namespace: str, report_key: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        authorize(namespace, set(scopes), READ_SCOPE)
        revisions = self.store.report_revisions(namespace, report_key)
        if not revisions:
            raise ExtractivesError("not_found", "no report with that key")
        return {"contract": ANSWER_CONTRACT, "report_key": report_key, "revisions": [
            {**r, "source_revision": self.store.source_revision(namespace, r["release_id"]),
             "payments": self.store.payments(namespace, r["report_id"]),
             "discrepancies": self.store.discrepancies(namespace, r["report_id"])} for r in revisions],
            "note": "every retained revision; a withdrawal or correction is a revision, never a deletion"}

    # ------------------------------------------------------------------ evidence bundle

    def export_bundle(self, answer: Mapping[str, Any], *, created_at_ms: int | None = None) -> dict[str, Any]:
        """A noesis-evidence-bundle-v1 citing every item with source, record revision and as-of time."""
        from src.evidence_bundle.builder import EvidenceBundleBuilder

        builder = EvidenceBundleBuilder("answer", {"operation": answer["question"], "query": _no_method(
            answer["query"])}, created_at_ms=created_at_ms, as_of_ms=answer.get("as_of_ms"))
        refs, statements = [], []
        for report in answer.get("reports") or []:
            citation = report["citation"]
            source = {k: citation["source_revision"].get(k) for k in (
                "provider", "source_id", "url", "file_sha256", "published_on", "evidence_origin",
                "live_verification", "licence")}
            if citation["source_revision"].get("url"):
                builder.add_external_reference(f"release:{citation['source_revision']['release_id']}",
                                               citation["source_revision"]["url"], required=False)
            streams = list(report.get("streams") or []) + list(report.get("government_revenues") or []) + list(
                report.get("companies") or [])
            for stream in streams:
                object_id = "ex-payment:" + digest([citation["report_id"], stream["company_key"],
                                                   stream["revenue_stream"]])[:24]
                builder.add_object("evidence", _no_method({
                    "kind": "extractive-payment",
                    "locator": {"cited": True, "document_id": citation["source_revision"]["release_id"],
                                "report_id": citation["report_id"]},
                    "company": stream["company"], "revenue_stream": stream["revenue_stream"],
                    "government_reported": stream["government_reported"],
                    "company_reported": stream["company_reported"], "eiti_discrepancies": stream["discrepancies"],
                    "currencies": stream["currencies"],
                    "report_revision": {k: citation[k] for k in ("report_id", "revision", "revision_of", "status",
                                                                "report", "fiscal_period")},
                    "as_of": citation["selected_as_of"], "release_at": citation["release_at"],
                    "retrieved_at": citation["retrieved_at"], "source": source,
                    "matched_through": stream.get("matched_through"),
                }), object_id=object_id)
                refs.append(object_id)
                if len(stream["currencies"]) > 1:
                    builder.add_omission(f"{citation['report_id']}: several currencies stated; not converted or "
                                         "summed", object_id=object_id)
                statements.append({"statement": f"{citation['report']['label']} (revision {citation['revision']}): "
                                                f"{stream['revenue_stream'].get('name_as_reported')} figures as "
                                                "reported", "status": "cited", "evidence_refs": [object_id]})
        for result in answer.get("results") or []:
            vintage = result["vintage"]
            object_id = f"ex-figure:{result['series_id']}@{vintage['vintage_id']}"
            builder.add_object("evidence", _no_method({
                "kind": "commodity-figures",
                "locator": {"cited": True, "document_id": vintage["release_id"], "series_id": result["series_id"]},
                "provider": result["provider"], "commodity": result["commodity"], "statistic": result["statistic"],
                "unit": result["unit"], "country": result["country"],
                "values": [{k: v[k] for k in ("period", "value_text", "value", "status", "estimated", "revised")}
                           for v in result["values"]],
                "vintage": {k: vintage[k] for k in ("vintage_id", "release_id", "publication", "release_at",
                                                    "revision_of")},
                "as_of": vintage["selected_as_of"], "retrieved_at": vintage["retrieved_at"],
                "source": {k: vintage["source_revision"].get(k) for k in (
                    "provider", "source_id", "url", "file_sha256", "published_on", "evidence_origin",
                    "live_verification", "licence")},
            }), object_id=object_id)
            refs.append(object_id)
            for period in result["marked"]["withheld"] + result["marked"]["not_available"]:
                builder.add_omission(f"{result['provider']} {result['series_id']} {period}: no value published "
                                     "(withheld or not available); not filled", object_id=object_id)
            statements.append({"statement": f"{result['provider']} {result['commodity'].get('name')} "
                                            f"{result['statistic']} ({result['country'].get('name')}) as published "
                                            f"in {vintage['publication'].get('label')}", "status": "cited",
                               "evidence_refs": [object_id]})
        if answer.get("status") in {"no_payment_on_record", "none_published", "no_report_on_record"}:
            builder.add_omission(f"{answer['question']}: {answer['status']} (not zero)")
            statements.append({"statement": f"{answer['question']}: {answer['status']}", "status": "not_found",
                               "evidence_refs": []})
        root = {k: answer.get(k) for k in ("contract", "question", "as_of_ms", "status", "receipt", "exclusions",
                                           "never")}
        builder.add_object("answer", {"kind": "extractives", **_no_method(root), "query": _no_method(answer["query"]),
                                      "statements": statements},
                           object_id=f"ex-answer:{answer['receipt']['digest'][:24]}", references=sorted(set(refs)),
                           root=True)
        return builder.build()


__all__ = ["NOTICE", "ExtractivesQueries"]
