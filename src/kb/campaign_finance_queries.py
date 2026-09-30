"""Campaign-finance answers: reported totals per filing version, affiliate donations and contest filings (#2209,
CF09, CF10).

* :meth:`CampaignFinanceQueries.reported_totals` - a committee's (or regulated
  entity's) reported receipts, disbursements and cash on hand per report and
  period as of a date, each figure from the filing version available on that
  date (named, with its amendment chain and the differences between versions
  shown). Figures are the totals as reported; a cross-period sum is only given
  on request, labelled ``derived`` and listing the filing versions it used.
  Cash on hand is a stock and is never summed. A committee with no filing on
  record is ``none_on_record``.
* :meth:`CampaignFinanceQueries.affiliate_donations` - contributions reported
  from an organisation and its affiliated entities, reached only through
  accepted CF07 identity matches, cited Corporate Ownership relations
  (:class:`src.kb.ownership_graph.OwnershipGraph`) and connected organisations
  stated on committee registrations; each result lists the path used.
* :meth:`CampaignFinanceQueries.contest_filings` - candidate-committee filings,
  party spending returns and independent expenditures (support/oppose as
  reported) linked to a contest (CF08).

Every item is cited with its source, record revision, filing revision and the
time it was observed. Individual line items are returned only as CF01 allows:
minimised, and only to principals holding the individual-items scope;
otherwise they are counted. No ranking, influence score or "dark money"
inference.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from src.ingestion.campaign_finance_sources import committee_key, ukec_entity_key
from src.kb.campaign_finance_identity import CampaignFinanceIdentity, donor_key
from src.kb.campaign_finance_records import (
    EXCLUSIONS,
    INDIVIDUAL_SCOPE,
    READ_SCOPE,
    CampaignFinanceStore,
    authorize,
    table_exists,
)

ANSWER_CONTRACT = "noesis-campaign-finance-answer-v1"
FLOW_TOTALS = ("total_receipts", "total_disbursements", "total_individual_contributions")
STOCK_TOTALS = ("cash_on_hand_beginning_period", "cash_on_hand_end_period", "debts_owed_by_committee",
                "debts_owed_to_committee")
PARTY_COMMITTEE_TYPES = frozenset({"X", "Y", "Z"})
INDIVIDUAL_NOTICE = ("individual contributions are minimised under CF01 (no name, address, employer or occupation) "
                     "and returned only with the individual-items scope, otherwise counted; natural-person payees "
                     "are reduced to their kind")


def resolve_committee(value: str) -> str:
    text = str(value or "").strip()
    if text.startswith("campaign-finance:"):
        return text
    if re.fullmatch(r"[Cc]\d{8}", text):
        return committee_key(text)
    if re.fullmatch(r"\d{1,8}", text):
        return ukec_entity_key(text)
    return text


def observed_at(ms: int | None) -> str | None:
    return None if ms is None else datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def cite(row: Mapping[str, Any]) -> dict[str, Any]:
    citation = dict(row["citation"])
    citation["observed_at"] = observed_at(citation.get("observed_at_ms"))
    return citation


def _differences(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[dict[str, Any]]:
    out = []
    old, new = before.get("totals_as_reported") or {}, after.get("totals_as_reported") or {}
    for field in sorted(set(old) | set(new)):
        if old.get(field) != new.get(field):
            out.append({"field": field, "from_file": before["file_number"], "to_file": after["file_number"],
                        "before": old.get(field), "after": new.get(field)})
    return out


class CampaignFinanceQueries:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.store = CampaignFinanceStore(conn, initialize=False, now=now)
        self.identity = CampaignFinanceIdentity(conn, now=now, initialize=False)

    # ------------------------------------------------------------------ CF09 reported totals

    def reported_totals(self, namespace: str, committee: str, *, scopes: Iterable[str], as_of: str | None = None,
                        period_start: str | None = None, period_end: str | None = None,
                        derive: bool = False) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        key = resolve_committee(committee)
        day = str(as_of)[:10] if as_of else None
        answer: dict[str, Any] = {"contract": ANSWER_CONTRACT, "query": "reported_totals", "namespace": namespace,
                                  "committee_key": key, "as_of": day, "exclusions": list(EXCLUSIONS)}
        filings = self.store.records(namespace, scopes=scopes, kinds=["filing"], committee_key=key)
        registration = self.store.records(namespace, scopes=scopes, kinds=["committee", "regulated-entity"],
                                          record_keys=[key])
        answer["committee"] = ({"name": registration[0]["record"]["fields"].get("name"),
                                "citation": cite(registration[0])} if registration else None)
        if not filings:
            return {**answer, "status": "none_on_record", "reports": [],
                    "note": "no filing of this committee is on record in the acquired coverage"}
        reports, pending = [], []
        for group in sorted({f["filing_group"] for f in filings}):
            chain = self.store.filing_chain(namespace, group, scopes=scopes, as_of=day)
            selected = chain["selected"]
            if selected is None:
                pending.append({"filing_group": group, "note": "no version had been received by the as-of date"})
                continue
            coverage = selected["coverage"]
            if period_start and coverage["end"] and coverage["end"] < period_start:
                continue
            if period_end and coverage["start"] and coverage["start"] > period_end:
                continue
            versions = chain["versions"]
            differences = []
            for before, after in zip(versions, versions[1:]):
                differences += _differences(before, after)
            first = selected["citation"]
            report = {
                "filing_group": group, "form_type": selected["form_type"], "report_type": selected["report_type"],
                "coverage": coverage,
                "version_used": {"filing_key": selected["filing_key"], "file_number": selected["file_number"],
                                 "receipt_date": selected["receipt_date"],
                                 "amendment_indicator": selected["amendment_indicator"],
                                 "most_recent_as_published": selected["most_recent_as_published"],
                                 "revision_id": selected["revision_id"], "selection_basis": chain["selection_basis"]},
                "totals_as_reported": selected["totals_as_reported"],
                "amendment_chain": [{"filing_key": v["filing_key"], "file_number": v["file_number"],
                                     "receipt_date": v["receipt_date"], "amendment_indicator": v["amendment_indicator"],
                                     "most_recent_as_published": v["most_recent_as_published"],
                                     "available_as_of": v["filing_key"] not in chain["later_versions"],
                                     "totals_as_reported": v["totals_as_reported"], "revision_id": v["revision_id"]}
                                    for v in versions],
                "differences_between_versions": differences,
                "citation": {**first, "observed_at": observed_at(first.get("observed_at_ms"))},
            }
            if selected["totals_as_reported"] is None:
                report["totals_note"] = "the regulator's export publishes items, not return totals; none reported"
                if derive:
                    report["derived"] = self._derived_items(namespace, selected, scopes)
            reports.append(report)
        answer.update(status="answered" if reports else "none_on_record", reports=reports,
                      not_yet_filed_as_of=pending)
        if derive:
            answer["derived"] = self._derived(reports)
        return answer

    @staticmethod
    def _derived(reports: list[dict[str, Any]]) -> dict[str, Any]:
        used = [r for r in reports if r["totals_as_reported"]]
        sums: dict[str, Decimal] = {}
        for report in used:
            for field in FLOW_TOTALS:
                value = report["totals_as_reported"].get(field)
                if value is not None:
                    sums[field] = sums.get(field, Decimal(0)) + Decimal(str(value))
        return {"label": "derived", "basis": "sum of the reported flow totals of the filing versions listed; not a "
                                             "figure any regulator reported",
                "sums": {k: float(v) for k, v in sorted(sums.items())},
                "filing_versions_used": [{"filing_key": r["version_used"]["filing_key"],
                                          "revision_id": r["version_used"]["revision_id"],
                                          "coverage": r["coverage"]} for r in used],
                "not_summed": list(STOCK_TOTALS)}

    def _derived_items(self, namespace: str, selected: Mapping[str, Any], scopes: set[str]) -> dict[str, Any]:
        items = self.store.records(namespace, scopes=scopes, filing_key=selected["filing_key"],
                                   kinds=["contribution", "expenditure"])
        total = sum((Decimal(str(i["record"]["fields"].get("amount_as_reported") or 0)) for i in items), Decimal(0))
        return {"label": "derived", "basis": "sum of the item amounts on record for this return; not a figure the "
                                             "regulator reported", "sum": float(total), "currency": "GBP",
                "items_used": [{"record_key": i["record_key"], "revision_id": i["revision_id"]} for i in items]}

    # ------------------------------------------------------------------ items

    def item_view(self, row: Mapping[str, Any], scopes: set[str]) -> dict[str, Any] | None:
        """One line item as CF01 allows.

        An individual's *contribution* is ``None`` without the individual-items scope. An expenditure or independent
        expenditure paid to a natural person is returned with the payee reduced to its kind (no name): the spending
        committee's report, not the payee, is its subject.
        """
        if (row["individual"] and row["record_kind"] == "contribution" and INDIVIDUAL_SCOPE not in scopes
                and "operator" not in scopes):
            return None
        fields = row["record"]["fields"]
        keep = ("schedule", "file_number", "sub_id", "transaction_id", "ec_ref", "amount_as_reported", "currency",
                "date_as_reported", "accepted_date", "reported_date", "expenditure_date", "dissemination_date",
                "date_incurred", "memo", "receipt_type", "purpose_as_reported", "category_as_reported",
                "support_oppose_indicator", "candidate_id", "candidate_name", "source_assertion", "donation_type",
                "reporting_period", "election_name", "amendment_indicator", "is_aggregation")
        return {"record_key": row["record_key"], "record_kind": row["record_kind"],
                "committee_key": row["committee_key"], "filing_key": row["record"]["filing_key"],
                **{k: fields[k] for k in keep if k in fields},
                "counterparty": fields.get("counterparty"), "late_observation": row["late_observation"],
                "citation": cite(row)}

    def filing_items(self, namespace: str, filing: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        """Line items reported in one filing version, as reported; individual items per CF01."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        filing_key = filing if filing.startswith("campaign-finance:") else f"campaign-finance:fec:filing:{filing}"
        head = self.store.records(namespace, scopes=scopes, kinds=["filing"], record_keys=[filing_key])
        if not head:
            return {"contract": ANSWER_CONTRACT, "status": "none_on_record", "filing_key": filing_key, "items": [],
                    "exclusions": list(EXCLUSIONS)}
        rows = self.store.records(namespace, scopes=scopes, filing_key=filing_key,
                                  kinds=["contribution", "expenditure", "independent-expenditure"])
        items = [v for v in (self.item_view(r, scopes) for r in rows) if v is not None]
        return {"contract": ANSWER_CONTRACT, "status": "answered", "filing_key": filing_key,
                "filing": {"totals_as_reported": head[0]["record"]["fields"].get("totals_as_reported"),
                           "citation": cite(head[0])},
                "items": items, "individual_items_withheld": len(rows) - len(items),
                "individual_items_notice": INDIVIDUAL_NOTICE, "exclusions": list(EXCLUSIONS)}

    # ------------------------------------------------------------------ CF10 affiliate donations

    def affiliate_donations(self, namespace: str, organisation: str, *, principal_id: str, scopes: Iterable[str],
                            ownership_namespace: str | None = None, as_of: str | None = None) -> dict[str, Any]:
        """Contributions from an organisation and its affiliates, each with the path of accepted decisions used."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        day = str(as_of)[:10] if as_of else None
        answer: dict[str, Any] = {"contract": ANSWER_CONTRACT, "query": "affiliate_donations",
                                  "namespace": namespace, "organisation": organisation, "as_of": day,
                                  "exclusions": list(EXCLUSIONS), "unavailable": []}
        # the organisation and its affiliated entities, each with the ownership path that reached it
        reached: dict[str, list[dict[str, Any]]] = {organisation: [{"step": "organisation", "key": organisation}]}
        if ownership_namespace and table_exists(self.conn, "ownership_records"):
            from src.kb.ownership_graph import OwnershipGraph
            from src.kb.ownership_store import OwnershipError

            try:
                graph = OwnershipGraph(self.conn, ownership_namespace, principal_id=principal_id, scopes=scopes)
                root = graph.resolve(organisation)
                for member in graph.members(root):
                    reached.setdefault(member, [{"step": "organisation", "key": member,
                                                 "via": "accepted ownership identity cluster"}])
                for sub in graph.subsidiaries(root, day)["subsidiaries"]:
                    edge = sub["assertions"][0]
                    step = {"step": "ownership", "relation": edge["assertion_kind"],
                            "control_basis": edge.get("control_basis"), "from": root, "to": sub["subject"],
                            "assertion_record_id": edge["record_id"], "assertion_revision": edge["revision"],
                            "source": edge["source"], "as_of_status": edge["as_of_status"],
                            "ownership_namespace": ownership_namespace}
                    for member in graph.members(sub["subject"]):
                        reached.setdefault(member, [{"step": "organisation", "key": organisation}, step])
            except OwnershipError as exc:
                answer["unavailable"].append({"provider": "ownership.core", "reason": exc.code})
        else:
            answer["unavailable"].append({"provider": "ownership.core",
                                          "reason": "no ownership namespace or store; only direct matches are used"})
        items_by_donor: dict[str, list[dict[str, Any]]] = {}
        for row in self.store.records(namespace, scopes=scopes, kinds=["contribution"]):
            key = donor_key(row["record"])
            if key:  # natural persons have no donor key and are never expanded (CF01)
                items_by_donor.setdefault(key, []).append(row)
        subjects = {s["record_key"]: s for s in self.identity.subjects(namespace, scopes=scopes)}
        results, seen = [], set()
        for subject in subjects.values():
            if subject["kind"] not in {"donor-organisation", "connected-organisation"}:
                continue
            for match in self.identity.accepted(namespace, subject["record_key"], scopes=scopes):
                if match["record_key"] not in reached:
                    continue
                path = reached[match["record_key"]] + [{"step": "identity", "decision": match["candidate_id"],
                                                        "method": match["method"], "from": match["record_key"],
                                                        "to": subject["record_key"]}]
                donors = [(subject["record_key"], path)]
                if subject["kind"] == "connected-organisation":
                    committee = subject["stated_by"]
                    (registration,) = self.store.records(namespace, scopes=scopes, kinds=["committee"],
                                                         record_keys=[committee])[:1] or [None]
                    fec_id = committee.rsplit(":", 1)[-1]
                    hop = {"step": "connected-organisation", "committee": committee,
                           "stated_on_registration": cite(registration) if registration else None,
                           "affiliated_committee_name_as_published": (registration or {}).get("record", {}).get(
                               "fields", {}).get("affiliated_committee_name")}
                    donor = f"campaign-finance:fec:donor-committee:{fec_id}"
                    via = [a for a in self.identity.accepted(namespace, donor, scopes=scopes)
                           if a["record_key"] == committee]
                    if not via:
                        continue  # the committee as a donor is not an accepted official-id match
                    donors = [(donor, path + [hop, {"step": "identity", "decision": via[0]["candidate_id"],
                                                    "method": via[0]["method"], "from": committee, "to": donor}])]
                for key, steps in donors:
                    for row in items_by_donor.get(key, []):
                        if row["record_key"] in seen:
                            continue
                        item = self._dated_item(namespace, row, day, scopes)
                        if item is None:
                            continue
                        seen.add(row["record_key"])
                        results.append({**item, "donor": key, "path": steps})
        results.sort(key=lambda r: (r["filing_key"] or "", r["record_key"]))
        answer.update(status="answered" if results else "none_on_record", contributions=results,
                      affiliates_reached=sorted(reached),
                      notice="affiliates are reached only through accepted identity decisions, cited ownership "
                             "relations and connected organisations stated on registrations; individual donors are "
                             "never expanded")
        return answer

    def _dated_item(self, namespace: str, row: Mapping[str, Any], day: str | None, scopes: set[str]
                    ) -> dict[str, Any] | None:
        """An item with the filing version it was reported in; ``None`` if that version was filed after the date."""
        item = self.item_view(row, scopes)
        if item is None:
            return None
        filing = self.store.records(namespace, scopes=scopes, kinds=["filing"],
                                    record_keys=[row["record"]["filing_key"]])
        version = None
        if filing:
            fields = filing[0]["record"]["fields"]
            if day and fields.get("receipt_date") and fields["receipt_date"] > day:
                return None
            chain = self.store.filing_chain(namespace, filing[0]["filing_group"], scopes=scopes, as_of=day)
            selected = (chain["selected"] or {}).get("filing_key")
            version = {"filing_key": filing[0]["record_key"], "file_number": fields.get("file_number"),
                       "amendment_indicator": fields.get("amendment_indicator"),
                       "receipt_date": fields.get("receipt_date"), "revision_id": filing[0]["revision_id"],
                       "selected_as_of": selected == filing[0]["record_key"],
                       "superseded_by": None if selected in (None, filing[0]["record_key"]) else selected}
        return {**item, "reported_in": version}

    # ------------------------------------------------------------------ CF10 contest filings

    def contest_filings(self, namespace: str, contest_id: str, *, scopes: Iterable[str],
                        elections_namespace: str | None = None) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        from src.kb.campaign_finance_links import CampaignFinanceLinks

        answer: dict[str, Any] = {"contract": ANSWER_CONTRACT, "query": "contest_filings", "namespace": namespace,
                                  "contest_id": contest_id, "exclusions": list(EXCLUSIONS)}
        if table_exists(self.conn, "election_contests"):
            from src.kb.elections import ElectionError, ElectionStore

            try:
                contest = ElectionStore(self.conn, initialize=False).contest(elections_namespace or namespace,
                                                                             contest_id)
                answer["contest"] = {k: contest[k] for k in ("election_id", "unit_scheme", "unit_native_id",
                                                             "unit_name", "ballot")}
            except ElectionError:
                answer["contest"] = None
        links = CampaignFinanceLinks(self.conn, initialize=False).links(namespace, scopes=scopes, kind="contest",
                                                                        target_key=contest_id)
        committees = {c["record_key"]: c for c in self.store.records(namespace, scopes=scopes, kinds=["committee"])}
        groups: dict[str, list[dict[str, Any]]] = {"candidate_committee_filings": [], "party_spending_returns": [],
                                                   "independent_expenditures": [], "party_independent_expenditures": []}
        for link in links:
            (row,) = self.store.records(namespace, scopes=scopes, record_keys=[link["record_key"]])[:1] or [None]
            if row is None:
                continue
            basis = {k: link["basis"].get(k) for k in ("identity_candidate_id", "method", "election_id",
                                                       "via_committee", "election_name_as_published")
                     if link["basis"].get(k) is not None}
            if row["record_kind"] == "filing":
                fields = row["record"]["fields"]
                entry = {"record_key": row["record_key"], "committee_key": row["committee_key"],
                         "form_type": fields.get("form_type"), "report_type": fields.get("report_type"),
                         "file_number": fields.get("file_number"), "receipt_date": fields.get("receipt_date"),
                         "amendment_indicator": fields.get("amendment_indicator"),
                         "totals_as_reported": fields.get("totals_as_reported"), "link_basis": basis,
                         "citation": cite(row)}
                key = "party_spending_returns" if fields.get("return_kind") == "spending" else \
                    "candidate_committee_filings"
                groups[key].append(entry)
            else:
                item = self.item_view(row, scopes)
                if item is None:
                    continue
                spender = committees.get(row["committee_key"])
                spender_type = spender["record"]["fields"].get("committee_type") if spender else None
                key = "party_independent_expenditures" if spender_type in PARTY_COMMITTEE_TYPES else \
                    "independent_expenditures"
                groups[key].append({**item, "spender_committee_type": spender_type, "link_basis": basis})
        found = any(groups.values())
        answer.update(status="answered" if found else "none_on_record", **groups,
                      note=None if found else "no filing is linked to this contest (no accepted identity match, or "
                                               "no filing on record)")
        return answer

    # ------------------------------------------------------------------ evidence bundles

    @staticmethod
    def evidence_bundle(answer: Mapping[str, Any]) -> dict[str, Any]:
        """Assertions each citing the record revision behind it (source, filing revision and as-of time)."""
        bibliography: dict[str, dict[str, Any]] = {}
        assertions = []

        def add(identifier: str, text: str, citation: Mapping[str, Any] | None) -> None:
            if not citation:
                return
            bibliography.setdefault(citation["revision_id"], {
                "id": citation["revision_id"],
                "text": f"{citation['provider']} {citation['record_key']} (source {citation['source_id']}, revision "
                        f"{citation['revision_no']}, filing revision {citation.get('filing_revision_id') or 'n/a'}, "
                        f"observed {citation.get('observed_at') or citation.get('observed_at_ms')}, "
                        f"{citation['evidence_origin']} evidence), {citation['locator']}"})
            assertions.append({"id": identifier, "text": text, "kind": "sourced",
                               "dependencies": [{"kind": "source", "namespace": answer.get("namespace"),
                                                 "id": citation["record_key"], "revision": citation["revision_id"],
                                                 "locator": {"section": citation.get("filing_key") or
                                                             citation["record_key"]}}],
                               "citations": [citation["revision_id"]]})

        for report in answer.get("reports") or []:
            version = report["version_used"]
            add(f"totals-{version['filing_key']}", f"{report['form_type']} {report['report_type']} "
                f"({report['coverage']['start']} to {report['coverage']['end']}), file {version['file_number']} "
                f"({version['amendment_indicator']}): totals as reported {report['totals_as_reported']}",
                report["citation"])
        for item in answer.get("contributions") or []:
            add(f"item-{item['record_key']}", f"{item['record_kind']} of {item.get('amount_as_reported')} reported in "
                f"{item['filing_key']}", item["citation"])
        for group in ("candidate_committee_filings", "party_spending_returns", "independent_expenditures",
                      "party_independent_expenditures"):
            for entry in answer.get(group) or []:
                add(f"{group}-{entry['record_key']}", f"{group.replace('_', ' ')}: {entry['record_key']}",
                    entry["citation"])
        title = answer.get("committee_key") or answer.get("organisation") or answer.get("contest_id")
        return {"sections": [{"id": answer.get("query", "answer"), "title": f"{title} as of {answer.get('as_of')}",
                              "assertions": assertions}],
                "bibliography": list(bibliography.values()), "exclusions": list(EXCLUSIONS)}
