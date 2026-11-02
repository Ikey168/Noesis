"""Holders of an issuer and holdings of a filer as of a date, from parsed SEC filings (#2883).

Answers the ``holders`` and ``holdings`` queries of :func:`src.kb.ownership_graph.query`
from ``noesis-ownership-record-v2`` assertions that carry the ``filing`` they
come from (``src/ingestion/ownership_sec_holdings.py``). Every line is what one
filing states and cites its accession number, record revision and locator.

Which statement is in force as of a date is decided per amendment chain and
never by rewriting a record:

* **Form 13F** (``reported_holding``): per filing manager (the CIK the filings
  state), the latest report period on or before the date answers. Within that
  period the original and its amendments apply in amendment order: a
  ``restatement`` replaces the rows before it, a ``new_holdings`` amendment
  adds rows, an amendment of unstated type adds rows and is flagged. A
  manager whose latest report omits the issuer is not a holder as of the date.
* **Schedule 13D/13G** (``shareholding``): per chain (issuer CIK and reporting
  person as stated), the statement with the latest event date on or before the
  date answers; earlier ones on the chain are returned as ``superseded``.

Lines are grouped into entities only through accepted identity decisions, as
in :class:`~src.kb.ownership_graph.OwnershipGraph`; proposed candidates that
would widen the answer are listed under ``identity.pending`` so partial
coverage stays visible. 13F positions and Schedule 13D/13G beneficial
ownership are different statements and are never summed; group members on one
schedule report overlapping figures. Owner-scoped natural-person lines appear
to other principals only as withheld, still cited to their accession number.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.kb.ownership_records import CONTRACT_V2

NOTICE = ("Each line is what one SEC filing states as of its report period or event date, cited to its accession "
          "number. 13F positions and Schedule 13D/13G beneficial ownership are different statements and are never "
          "summed; group members' figures overlap. Amendments are applied along their chain, never by overwriting. "
          "This is not a beneficial-ownership, sanctions or AML determination.")


def _low(value: str | None) -> str | None:
    if not value:
        return None
    parts = value.split("-")
    return "-".join(parts + ["01"] * (3 - len(parts)))


def _filing(body: Mapping[str, Any]) -> dict[str, Any]:
    return dict(body.get("filing") or {})


def _reference_date(body: Mapping[str, Any]) -> str | None:
    filing = _filing(body)
    if body["assertion_kind"] == "reported_holding":
        return filing.get("period_of_report") or body.get("statement_date")
    return filing.get("event_date") or body.get("statement_date") or filing.get("filing_date")


def _order(body: Mapping[str, Any]) -> tuple:
    filing = _filing(body)
    amendment = filing.get("amendment") or {}
    return (int(amendment.get("number") or 0) if amendment.get("is_amendment") else -1,
            filing.get("filing_date") or "", filing.get("accession_number") or "")


def _on_or_before(reference: str | None, as_of: str | None) -> bool:
    return as_of is None or (reference is not None and _low(reference) <= as_of)


class _Lines:
    """In-force, superseded, withheld and undated SEC filing lines of one graph, as of one date."""

    def __init__(self, graph: Any, as_of: str | None) -> None:
        self.graph, self.as_of = graph, as_of
        self.in_force: list[tuple[dict[str, Any], dict[str, Any]]] = []  # (view, chain info)
        self.superseded: list[dict[str, Any]] = []
        self.withheld: list[dict[str, Any]] = []
        self.undated: list[dict[str, Any]] = []
        self.after_as_of: list[dict[str, Any]] = []
        thirteen_f: dict[str, list[dict[str, Any]]] = {}
        schedules: dict[str, list[dict[str, Any]]] = {}
        for view in graph.assertions:
            body = view["record"]
            if body.get("contract") != CONTRACT_V2 or not body.get("filing"):
                continue
            if view.get("redacted"):
                self.withheld.append(view)
                continue
            chain = (_filing(body).get("amendment") or {}).get("chain_key") or f"unchained:{view['record_id']}"
            reference = _reference_date(body)
            if reference is None:
                self.undated.append(view)
            elif not _on_or_before(reference, as_of):
                self.after_as_of.append(view)
            elif body["assertion_kind"] == "reported_holding":
                thirteen_f.setdefault(chain.split(":period:")[0], []).append(view)
            else:
                schedules.setdefault(chain, []).append(view)
        for views in thirteen_f.values():
            self._apply_13f(views)
        for views in schedules.values():
            self._apply_chain(views)

    def _apply_13f(self, views: list[dict[str, Any]]) -> None:
        period = max(_low(_reference_date(v["record"])) for v in views)
        by_filing: dict[str, list[dict[str, Any]]] = {}
        for view in views:
            if _low(_reference_date(view["record"])) == period:
                by_filing.setdefault(_filing(view["record"])["accession_number"], []).append(view)
        current: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for accession in sorted(by_filing, key=lambda a: _order(by_filing[a][0]["record"])):
            rows = by_filing[accession]
            amendment = _filing(rows[0]["record"]).get("amendment") or {}
            applied = ("original" if not amendment.get("is_amendment") else amendment.get("type") or "type_not_stated")
            if applied in {"original", "restatement"}:
                reason = "restated by amendment" if applied == "restatement" else "replaced by a later original report"
                self.superseded.extend(self._superseded(v, accession, reason) for v, _ in current)
                current = []
            current.extend((v, {"applied_as": applied}) for v in rows)
        self.in_force.extend(current)

    def _apply_chain(self, views: list[dict[str, Any]]) -> None:
        ordered = sorted(views, key=lambda v: (_low(_reference_date(v["record"])), _order(v["record"])))
        latest = ordered[-1]
        accession = _filing(latest["record"])["accession_number"]
        self.superseded.extend(self._superseded(v, accession, "later statement on the amendment chain")
                               for v in ordered[:-1])
        self.in_force.append((latest, {"applied_as": "latest on chain", "chain_length": len(ordered)}))

    def _superseded(self, view: Mapping[str, Any], by: str, reason: str) -> dict[str, Any]:
        body = view["record"]
        return {"record_id": view["record_id"], "revision": view["revision"], "subject_key": body["subject_key"],
                "holder_key": (body.get("holder") or {}).get("key"), "accession_number": _filing(body)["accession_number"],
                "form_type": _filing(body).get("form_type"), "reference_date": _reference_date(body),
                "superseded_by": by, "reason": reason}

    def line(self, view: Mapping[str, Any], applied: Mapping[str, Any]) -> dict[str, Any]:
        body, graph = view["record"], self.graph
        filing = _filing(body)
        amendment = filing.get("amendment") or {}
        holder = dict(body.get("holder") or {})
        return {
            "statement": ("institutional-13f-position" if body["assertion_kind"] == "reported_holding"
                          else "beneficial-ownership-schedule"),
            "assertion_kind": body["assertion_kind"], "subject": graph.cluster(body["subject_key"]),
            "subject_key": body["subject_key"],
            "holder": {**holder, "entity": graph.cluster(holder["key"]) if holder.get("key") else None},
            "reference_date": _reference_date(body), "share": body.get("share"), "position": body.get("position"),
            "powers": body.get("powers"), "group": body.get("group"), "basis": body.get("basis"),
            "citation": {"accession_number": filing["accession_number"], "form_type": filing.get("form_type"),
                         "filing_date": filing.get("filing_date"), "document_url": body["source"].get("url"),
                         "locator": body["source"].get("locator"), "provider": body["source"]["provider"],
                         "record_id": view["record_id"], "revision": view["revision"],
                         "revision_id": view["revision_id"]},
            "chain": {"chain_key": amendment.get("chain_key"), "is_amendment": amendment.get("is_amendment"),
                      "amendment_number": amendment.get("number"), "amendment_type": amendment.get("type"),
                      "amends_accession": amendment.get("amends_accession"), **dict(applied)},
            "unknowns": body.get("unknowns") or [],
        }

    @staticmethod
    def brief(view: Mapping[str, Any]) -> dict[str, Any]:
        body = view["record"]
        filing = _filing(body)
        return {"record_id": view["record_id"], "revision": view["revision"], "assertion_kind": body["assertion_kind"],
                "holder_kind": (body.get("holder") or {}).get("kind"), "accession_number": filing.get("accession_number"),
                "form_type": filing.get("form_type"), "reference_date": _reference_date(body) if not view.get(
                    "redacted") else body.get("statement_date") or filing.get("period_of_report") or filing.get("event_date")}


def _pending(graph: Any, cluster: str) -> list[dict[str, Any]]:
    from src.kb.ownership_identity import OwnershipIdentityService

    members = set(graph.members(cluster))
    service = OwnershipIdentityService(graph.store.conn, initialize=False)
    return [{"candidate_id": c["candidate_id"], "left_key": c["left_key"], "right_key": c["right_key"],
             "basis": c["basis"], "confidence": c["confidence"]}
            for c in service.candidates(graph.namespace, scopes=graph.scopes, state="proposed")
            if c["left_key"] in members or c["right_key"] in members]


def _answer(graph: Any, query: str, cluster: str, as_of: str | None, lines: _Lines, keep) -> dict[str, Any]:
    selected = [lines.line(v, applied) for v, applied in lines.in_force if keep(v["record"])]
    selected.sort(key=lambda x: (x["statement"], str(x["holder"]["entity"]), x["subject"],
                                 x["citation"]["accession_number"], x["citation"]["record_id"]))
    record_ids = {v["record_id"] for v, _ in lines.in_force if keep(v["record"])}
    superseded = [s for s in lines.superseded
                  if keep({"subject_key": s["subject_key"], "holder": {"key": s["holder_key"]}})]
    side = "subject_key" if query == "holders" else None
    withheld = [lines.brief(v) for v in lines.withheld
                if side and graph.cluster(v["record"]["subject_key"]) == cluster]
    body = {
        "as_of_basis": "13F: latest report period on or before the date per filing manager; schedules: latest event "
                       "date on or before the date per amendment chain",
        "institutional_holdings": [x for x in selected if x["statement"] == "institutional-13f-position"],
        "beneficial_ownership": [x for x in selected if x["statement"] == "beneficial-ownership-schedule"],
        "superseded": sorted(superseded, key=lambda s: (s["accession_number"], s["record_id"])),
        "withheld": withheld,
        "undated": [lines.brief(v) for v in lines.undated if keep(v["record"])],
        "stated_after_as_of": len([v for v in lines.after_as_of if keep(v["record"])]),
        "identity": {"pending": _pending(graph, cluster),
                     "note": "lines group into entities only through accepted identity decisions; pending candidates "
                             "would widen this answer once reviewed"},
        "coverage": {"filings": sorted({x["citation"]["accession_number"] for x in selected}),
                     "lines": len(record_ids),
                     "note": "only filings explicitly selected for acquisition and parsed are covered"},
        "filing_notice": NOTICE,  # the graph result adds its own "notice"
    }
    return graph._result(query, cluster, as_of, body)


def holders(graph: Any, entity: str, as_of: str | None = None) -> dict[str, Any]:
    """Who the parsed filings say holds the issuer as of a date."""
    issuer = graph.resolve(entity)
    lines = _Lines(graph, as_of)
    return _answer(graph, "holders", issuer, as_of, lines,
                   lambda body: graph.cluster(body["subject_key"]) == issuer)


def holdings(graph: Any, entity: str, as_of: str | None = None) -> dict[str, Any]:
    """What the parsed filings say a filer holds across issuers as of a date."""
    filer = graph.resolve(entity)
    lines = _Lines(graph, as_of)
    return _answer(graph, "holdings", filer, as_of, lines,
                   lambda body: bool((body.get("holder") or {}).get("key"))
                   and graph.cluster(body["holder"]["key"]) == filer)
