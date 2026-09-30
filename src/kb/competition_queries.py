"""Company-to-cases, case stage history and state aid for a beneficiary, as of a date (#2217, CS09, CS10).

* :func:`cases_for_company` - the competition cases naming a company (and,
  optionally, its group as of the same date), each with the party role as
  published, the case stage in force at the date and the accepted identity
  match that connected it. Group expansion uses the ownership graph
  (:class:`src.kb.ownership_graph.OwnershipGraph`) as of the date: the stated
  control-chain tops of the company and their stated subsidiaries, each case
  labelled with the group member and the ownership path used. A company with
  no matched party gets ``no_case_on_record`` - never a clean bill - and,
  when asked, the unmatched parties of a similar name as unknowns.
* :func:`case_history` - every stage of a case in date order with its source
  and revision (superseded revisions on request), decision documents,
  parties as published and the exact citation links.
* :func:`awards_for_beneficiary` - the state-aid awards reached through
  accepted identity matches, with granting authority, measure reference,
  amount and currency as published and the SA case where cited. Totals are a
  **computed view** listing their inputs, never stored; awards with only a
  range or no amount, unresolved SA references and unmatched beneficiaries are
  explicit unknowns.

Cases and awards of different authorities stay side by side; nothing is
merged across authorities. Nothing predicts outcomes, assesses market power or
aid compatibility, or gives legal advice.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from src.kb.competition import READ_SCOPE, CompetitionError, CompetitionStore, authorize

CASES_CONTRACT = "noesis-competition-cases-answer-v1"
HISTORY_CONTRACT = "noesis-competition-case-history-v1"
AWARDS_CONTRACT = "noesis-state-aid-awards-answer-v1"
NOTICE = ("Cases and awards are what each authority published, side by side per authority. No outcome is "
          "predicted, no market power or aid compatibility is assessed and nothing here is legal advice.")
MAX_GROUP_DEPTH = 5


def _graph(conn: Any, ownership_namespace: str, scopes: set[str], principal_id: str | None,
           known_at_ms: int | None):
    from src.kb.ownership_graph import OwnershipGraph

    return OwnershipGraph(conn, ownership_namespace, principal_id=principal_id, scopes=scopes,
                          known_at_ms=known_at_ms)


def _resolve(graph, entity: str) -> str:
    from src.kb.ownership_store import OwnershipError

    try:
        return graph.resolve(entity)
    except OwnershipError as exc:
        # A canonical entity id names the same record as its record key.
        for key in graph.entities:
            if any(v["record"].get("canonical_entity_id") == entity for v in graph.entities[key]):
                return graph.cluster(key)
        raise CompetitionError("not_found", "the company is not an entity of the ownership namespace") from exc


def group_members(graph, root: str, as_of: str | None, *, max_depth: int = MAX_GROUP_DEPTH) -> list[dict[str, Any]]:
    """The company, its stated control-chain tops and their stated subsidiaries as of a date, with paths."""
    chain = graph._chain(root, as_of, max_depth)
    members: dict[str, dict[str, Any]] = {root: {"entity": root, "relation": "self", "path": []}}
    for path in chain["paths"]:
        for hop in path["edges"]:
            members.setdefault(hop["to"], {"entity": hop["to"], "relation": "parent-chain",
                                           "path": [h for h in path["edges"][:path["edges"].index(hop) + 1]]})
    queue = [(top, [], 0) for top in (chain["tops"] or [root])]
    seen = set()
    while queue:
        node, path, depth = queue.pop(0)
        if node in seen or depth > max_depth:
            continue
        seen.add(node)
        if node not in members:
            members[node] = {"entity": node, "relation": "group-member", "path": path}
        for item in graph.subsidiaries(node, as_of)["subsidiaries"]:
            hop = {"from": node, "to": item["subject"], "assertion_ids": [a["record_id"] for a in item["assertions"]],
                   "kinds": sorted({a["assertion_kind"] for a in item["assertions"]}), "direction": "holder-to-subject"}
            queue.append((item["subject"], path + [hop], depth + 1))
    return [members[k] for k in sorted(members)]


def _by_subject(links: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One link per subject (the first accepted match); the other accepted matches to the same entity are listed."""
    out: dict[str, dict[str, Any]] = {}
    for link in sorted(links, key=lambda item: (item["subject_key"], item["low_evidence"], item["candidate_id"])):
        if link["subject_key"] in out:
            out[link["subject_key"]]["also_matched_through"].append(link["candidate_id"])
        else:
            out[link["subject_key"]] = {**link, "also_matched_through": []}
    return list(out.values())


def _stage_as_of(stages: list[dict[str, Any]], as_of: str | None) -> tuple[dict[str, Any] | None, list[dict]]:
    dated = sorted((s for s in stages if s["record"].get("stage_date")),
                   key=lambda s: (s["record"]["stage_date"], s["record"]["record_key"]))
    undated = [s for s in stages if not s["record"].get("stage_date")]
    if as_of:
        dated = [s for s in dated if s["record"]["stage_date"] <= as_of]
    return (dated[-1] if dated else None), undated


def _stage_view(view: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if view is None:
        return None
    body = view["record"]
    return {"stage_as_published": body["stage_as_published"], "stage_date": body.get("stage_date"),
            "date_status": body["date_status"], "document": body.get("document"),
            "page_revision": body.get("page_revision"), "record_key": body["record_key"],
            "revision_id": view["revision_id"], "source": body["source"]}


def _case_row(store: CompetitionStore, namespace: str, case_key: str, as_of: str | None,
              known_at_ms: int | None) -> dict[str, Any] | None:
    case = next((v for v in store.views(namespace, ("competition_case",), known_at_ms=known_at_ms)
                 if v["record"]["record_key"] == case_key), None)
    if case is None:
        return None
    body = case["record"]
    stages = store.case_children(namespace, case_key, known_at_ms=known_at_ms)["case_stage"]
    current, undated = _stage_as_of(stages, as_of)
    started = body.get("opened_on") or min((s["record"]["stage_date"] for s in stages if s["record"].get("stage_date")),
                                           default=None)
    return {"case_key": case_key, "authority": body["authority"], "case_number": body["case_number"],
            "instrument": body["instrument"], "instrument_as_published": body["instrument_as_published"],
            "title": body.get("title"), "state_as_published_now": body.get("state_as_published"),
            "opened_on": body.get("opened_on"), "case_url": body.get("case_url"),
            "stage_as_of": _stage_view(current), "undated_stages": [_stage_view(s) for s in undated],
            "not_yet_opened": bool(as_of and started and started > as_of),
            "case_revision": {"record_id": case["record_id"], "revision": case["revision"],
                              "revision_id": case["revision_id"], "source": body["source"]}}


def _similar_unknowns(identity, namespace: str, names: set[str], scopes) -> list[dict[str, Any]]:
    from src.kb.entities import normalize_surface

    wanted = {normalize_surface(n) for n in names if n}
    out = []
    for item in identity.unmatched(namespace, scopes=scopes):
        name = normalize_surface(item["name_as_published"])
        if any(name == w or (len(w) > 3 and (w in name or name in w)) for w in wanted):
            out.append({**item, "unknown": "not matched to this company; a similar name only, never counted"})
    return out


def cases_for_company(conn: Any, namespace: str, entity: str, *, ownership_namespace: str, scopes: Iterable[str],
                      as_of: str | None = None, group: bool = False, include_unknowns: bool = False,
                      principal_id: str | None = None, known_at_ms: int | None = None,
                      authority: str | None = None, instrument: str | None = None) -> dict[str, Any]:
    from src.kb.competition_identity import CompetitionIdentity

    scopes = set(scopes)
    authorize(namespace, scopes, READ_SCOPE)
    authorize(ownership_namespace, scopes, READ_SCOPE)
    graph = _graph(conn, ownership_namespace, scopes, principal_id, known_at_ms)
    root = _resolve(graph, entity)
    members = group_members(graph, root, as_of) if group else [{"entity": root, "relation": "self", "path": []}]
    store = CompetitionStore(conn, initialize=False)
    identity = CompetitionIdentity(conn, initialize=False)
    parties = {v["record"]["record_key"]: v for v in store.views(namespace, ("case_party",), known_at_ms=known_at_ms)}
    rows, awards, excluded, names = [], [], [], set()
    for member in members:
        keys = graph.members(member["entity"])
        names |= {n["name"] for n in graph.describe(member["entity"])["names"] if n.get("name")}
        for link in _by_subject(identity.accepted_links(namespace, keys, scopes=scopes)):
            matched = {k: link[k] for k in ("candidate_id", "method", "low_evidence", "reviewer", "reviewed_at_ms",
                                            "decision_id", "ownership_key", "also_matched_through")}
            if link["subject_key"].startswith("competition:award:"):
                awards.append({"award_key": link["subject_key"], "group_member": member["entity"],
                               "matched_through": matched})
                continue
            party = parties.get(link["subject_key"])
            if party is None:
                continue
            row = _case_row(store, namespace, party["record"]["case_key"], as_of, known_at_ms)
            if row is None or (authority and row["authority"] != authority) or \
                    (instrument and row["instrument"] != instrument):
                continue
            if row["not_yet_opened"]:
                excluded.append({"case_key": row["case_key"], "reason": "opened after the as-of date"})
                continue
            row.update({"party": {"name_as_published": party["record"]["name_as_published"],
                                  "role": party["record"]["role"],
                                  "role_as_published": party["record"]["role_as_published"],
                                  "record_key": party["record"]["record_key"], "revision_id": party["revision_id"]},
                        "matched_through": matched, "group_member": member["entity"],
                        "group_relation": member["relation"], "ownership_path": member["path"]})
            row["cites"] = {"case_revision_id": row["case_revision"]["revision_id"],
                            "stage_revision_id": (row["stage_as_of"] or {}).get("revision_id"),
                            "party_revision_id": party["revision_id"], "identity_candidate": link["candidate_id"],
                            "identity_decision": link["decision_id"]}
            rows.append(row)
    rows.sort(key=lambda r: (r["authority"], r["case_number"], r["party"]["record_key"]))
    by_authority: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_authority.setdefault(row["authority"], []).append(row)
    answer = {
        "contract": CASES_CONTRACT, "namespace": namespace, "ownership_namespace": ownership_namespace,
        "entity": graph.describe(root), "as_of": as_of, "group": group,
        "group_members": [{"entity": m["entity"], "relation": m["relation"], "ownership_path": m["path"]}
                          for m in members],
        "status": "answered" if rows else "no_case_on_record", "cases": rows, "by_authority": by_authority,
        "awards_matched": awards, "excluded": excluded,
        "coverage": "only the declared, acquired case selections and reviewed identity matches are searched; "
                    "'no case on record' is not a statement that no case exists",
        "notice": NOTICE,
    }
    if not rows:
        answer["message"] = "no case on record for this company's reviewed identity matches (not a clean bill)"
    if include_unknowns:
        answer["unknowns"] = _similar_unknowns(identity, namespace, names, scopes)
    return answer


def case_history(conn: Any, namespace: str, case_key: str, *, scopes: Iterable[str],
                 include_superseded: bool = False) -> dict[str, Any]:
    from src.kb.competition_citations import CompetitionCitations

    authorize(namespace, scopes, READ_SCOPE)
    store = CompetitionStore(conn, initialize=False)
    case = store.by_key(namespace, case_key)
    if case is None:
        return {"contract": HISTORY_CONTRACT, "namespace": namespace, "case_key": case_key,
                "status": "no_case_on_record", "notice": NOTICE}
    children = store.case_children(namespace, case_key)
    stages = []
    for view in children["case_stage"]:
        item = _stage_view(view)
        if include_superseded:
            item["revisions"] = [{"revision": v["revision"], "revision_id": v["revision_id"],
                                  "observed_at_ms": v["observed_at_ms"],
                                  "stage_as_published": v["record"]["stage_as_published"]}
                                 for v in store.history(namespace, view["record"]["record_key"])]
        stages.append(item)
    stages.sort(key=lambda s: (s["stage_date"] is None, s["stage_date"] or "", s["record_key"]))
    citations = CompetitionCitations(conn, initialize=False)
    body = case["record"]
    answer = {
        "contract": HISTORY_CONTRACT, "namespace": namespace, "case_key": case_key, "status": "answered",
        "case": {"authority": body["authority"], "case_number": body["case_number"], "title": body.get("title"),
                 "instrument": body["instrument"], "instrument_as_published": body["instrument_as_published"],
                 "state_as_published": body.get("state_as_published"), "opened_on": body.get("opened_on"),
                 "closed_on": body.get("closed_on"), "court_dockets": body.get("court_dockets") or [],
                 "revision_id": case["revision_id"], "revision": case["revision"], "source": body["source"]},
        "stages": stages,
        "documents": sorted(({"document_type_as_published": v["record"]["document_type_as_published"],
                              "document_date": v["record"].get("document_date"),
                              "language": v["record"].get("language"), "url": v["record"]["url"],
                              "citation": v["record"].get("citation") or {}, "revision_id": v["revision_id"],
                              "record_key": v["record"]["record_key"]} for v in children["decision_document"]),
                            key=lambda d: (d["document_date"] or "", d["record_key"])),
        "parties": [{"name_as_published": v["record"]["name_as_published"], "role": v["record"]["role"],
                     "role_as_published": v["record"]["role_as_published"], "record_key": v["record"]["record_key"]}
                    for v in sorted(children["case_party"], key=lambda v: v["record"]["record_key"])],
        "citations": [link for key in [case_key] + [v["record"]["record_key"] for v in children["decision_document"]]
                      for link in citations.links(namespace, citing_record_key=key)],
        "cited_by": citations.links(namespace, target_case_key=case_key),
        "unknowns": [f"stage {s['record_key']} has no published date" for s in stages if s["stage_date"] is None],
        "notice": NOTICE,
    }
    if include_superseded:
        answer["case_revisions"] = [{"revision": v["revision"], "revision_id": v["revision_id"],
                                     "observed_at_ms": v["observed_at_ms"],
                                     "state_as_published": v["record"].get("state_as_published"),
                                     "page_revision": v["record"].get("page_revision")}
                                    for v in store.history(namespace, case_key)]
    return answer


def _amount(text: Any) -> Decimal | None:
    try:
        value = Decimal(str(text))
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() else None


def awards_for_beneficiary(conn: Any, namespace: str, entity: str, *, ownership_namespace: str,
                           scopes: Iterable[str], include_superseded: bool = False, include_unknowns: bool = True,
                           principal_id: str | None = None) -> dict[str, Any]:
    from src.kb.competition_citations import CompetitionCitations
    from src.kb.competition_identity import CompetitionIdentity

    scopes = set(scopes)
    authorize(namespace, scopes, READ_SCOPE)
    authorize(ownership_namespace, scopes, READ_SCOPE)
    graph = _graph(conn, ownership_namespace, scopes, principal_id, None)
    root = _resolve(graph, entity)
    store = CompetitionStore(conn, initialize=False)
    identity = CompetitionIdentity(conn, initialize=False)
    citations = CompetitionCitations(conn, initialize=False)
    rows, unknowns = [], []
    for link in _by_subject(identity.accepted_links(namespace, graph.members(root), scopes=scopes)):
        if not link["subject_key"].startswith("competition:award:"):
            continue
        view = store.by_key(namespace, link["subject_key"])
        if view is None:
            continue
        body = view["record"]
        measure = [c for c in citations.links(namespace, citing_record_key=body["record_key"])
                   if c["field"] == "sa_number" and c["citing_revision_id"] == view["revision_id"]]
        row = {"award_key": body["record_key"], "award_id": body["award_id"], "member_state": body["member_state"],
               "beneficiary_name_as_published": body["beneficiary_name_as_published"],
               "beneficiary_identifiers": body.get("beneficiary_identifiers") or [],
               "granting_authority": body.get("granting_authority"),
               "aid_instrument_as_published": body.get("aid_instrument_as_published"),
               "aid_objective_as_published": body.get("aid_objective_as_published"),
               "sa_number": body.get("sa_number"), "amount_as_published": body.get("amount_as_published"),
               "amount_range_as_published": body.get("amount_range_as_published"), "currency": body.get("currency"),
               "award_date": body.get("award_date"), "status": body["status"],
               "status_as_published": body.get("status_as_published"),
               "measure_case": ({"status": measure[0]["status"], "case_key": measure[0]["target_case_key"],
                                 "link_id": measure[0]["link_id"]} if measure else
                                {"status": "not_linked", "case_key": None,
                                 "note": "run the citation linking to resolve the SA reference"}),
               "revision_id": view["revision_id"], "revision": view["revision"], "source": body["source"],
               "matched_through": {k: link[k] for k in ("candidate_id", "method", "low_evidence", "reviewer",
                                                        "decision_id", "ownership_key", "also_matched_through")}}
        if include_superseded:
            row["revisions"] = [{"revision": v["revision"], "revision_id": v["revision_id"],
                                 "amount_as_published": v["record"].get("amount_as_published"),
                                 "status": v["record"]["status"], "observed_at_ms": v["observed_at_ms"]}
                                for v in store.history(namespace, body["record_key"])]
        if row["amount_as_published"] is None:
            unknowns.append({"award_key": row["award_key"], "unknown": "amount not published as a figure",
                             "amount_range_as_published": row["amount_range_as_published"]})
        if row["measure_case"]["status"] != "resolved":
            unknowns.append({"award_key": row["award_key"], "unknown": "SA measure reference not resolved to an "
                                                                       "acquired case", "sa_number": row["sa_number"]})
        rows.append(row)
    rows.sort(key=lambda r: (r["member_state"], r["award_date"] or "", r["award_id"]))
    totals: dict[str, dict[str, Any]] = {}
    for row in rows:
        value = _amount(row["amount_as_published"])
        if value is None or row["status"] == "withdrawn" or not row["currency"]:
            continue
        entry = totals.setdefault(row["currency"], {"currency": row["currency"], "sum": Decimal(0), "inputs": []})
        entry["sum"] += value
        entry["inputs"].append({"award_key": row["award_key"], "amount_as_published": row["amount_as_published"],
                                "revision_id": row["revision_id"]})
    computed = [{**t, "sum": str(t["sum"])} for t in totals.values()]
    names = {n["name"] for n in graph.describe(root)["names"] if n.get("name")}
    answer = {
        "contract": AWARDS_CONTRACT, "namespace": namespace, "entity": graph.describe(root),
        "status": "answered" if rows else "no_award_on_record", "awards": rows,
        "computed_totals": {"view": "computed at query time from the listed inputs; never stored as a fact",
                            "excludes": "withdrawn awards, ranges and awards without a published figure",
                            "by_currency": computed},
        "unknowns": unknowns, "notice": NOTICE,
    }
    if include_unknowns:
        answer["unmatched_beneficiaries_of_similar_name"] = [
            u for u in _similar_unknowns(identity, namespace, names, scopes) if u["subject"] == "award_beneficiary"]
    return answer
