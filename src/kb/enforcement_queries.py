"""Enforcement actions for an entity or its group as of a date, and by authority and legal basis (EN09, EN10).

* :func:`actions_for_entity` - the actions naming a company (and, optionally,
  its group as of the same date) through **accepted** EN07 identity matches
  only. The company is an ownership entity; its cluster comes from the
  accepted ownership identity decisions and its group from the stated
  ownership/control assertions of the ownership graph as of the date
  (:func:`src.kb.competition_queries.group_members`); the answer states which
  matches and which ownership paths were used. Each action carries the
  outcome **as published** at the date (a settlement keeps the published
  admission wording, never a finding), its penalties as published, its appeal
  history with dates and the revisions it cites. "No action on record" is
  never a clean bill.
* :func:`actions_by_authority` - the actions of an authority and/or citing a
  legal basis over a period, each with outcome and penalties as published.
  Penalties are **never summed** across currencies or authorities (nor within
  them): figures are listed per authority and currency with their inputs, and
  undisclosed penalties stay explicit.
* :func:`action_history` - an action's revision chain (corrections and
  removals included), its notices with their versions, respondents, penalties,
  appeals and links.

Authorities stay side by side. Nothing scores risk or compliance, infers
wrongdoing from an initiated action, merges a settled outcome into a finding or
profiles an individual; answers carry the regulators' own wording.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from src.kb.enforcement import (
    EXCLUSIONS,
    READ_SCOPE,
    EnforcementError,
    EnforcementStore,
    authorize,
)

ENTITY_CONTRACT = "noesis-enforcement-actions-answer-v1"
AUTHORITY_CONTRACT = "noesis-enforcement-actions-by-authority-v1"
HISTORY_CONTRACT = "noesis-enforcement-action-history-v1"
NOTICE = ("Actions, outcomes, penalties and appeals are what each regulator published, side by side per authority. "
          f"Exclusions: {EXCLUSIONS}. An initiated action is not a finding; a settlement is shown with its published "
          "admission wording only.")
DATE_FIELDS = ("decided_on", "published_on", "initiated_on")


def _graph(conn: Any, ownership_namespace: str, scopes: set[str], principal_id: str | None, known_at_ms: int | None):
    from src.kb.ownership_graph import OwnershipGraph

    return OwnershipGraph(conn, ownership_namespace, principal_id=principal_id, scopes=scopes,
                          known_at_ms=known_at_ms)


def _resolve(graph, entity: str) -> str:
    from src.kb.competition_queries import CompetitionError
    from src.kb.competition_queries import _resolve as resolve_entity

    try:
        return resolve_entity(graph, entity)
    except CompetitionError as exc:
        raise EnforcementError("not_found", "the company is not an entity of the ownership namespace") from exc


def _cite(view: Mapping[str, Any]) -> dict[str, Any]:
    body = view["record"]
    return {"record_key": body["record_key"], "revision": view["revision"], "revision_id": view["revision_id"],
            "observed_at_ms": view["observed_at_ms"], "provider": body["source"]["provider"],
            "url": body["source"].get("url"), "source_revision": body["source"].get("revision"),
            "evidence_origin": body["source"].get("evidence_origin")}


def _penalty(view: Mapping[str, Any]) -> dict[str, Any]:
    body = view["record"]
    return {"penalty_type": body["penalty_type"], "penalty_type_as_published": body["penalty_type_as_published"],
            "amount_as_published": body.get("amount_as_published"), "currency": body.get("currency"),
            "amount_status": body["amount_status"], "imposed_on": body.get("imposed_on"), "note": body.get("note"),
            "cite": _cite(view)}


def _appeal(view: Mapping[str, Any], as_of: str | None) -> dict[str, Any]:
    body = view["record"]
    decided = body.get("decided_on")
    return {"forum_as_published": body["forum_as_published"], "reference": body.get("reference"),
            "status_as_published": body.get("status_as_published"), "lodged_on": body.get("lodged_on"),
            "decided_on": decided, "court_docket": body.get("court_docket"),
            "status_at_date": (None if as_of is None else
                               "decided by the date" if decided and decided <= as_of else
                               "no published appeal decision by the date" if decided or body.get("lodged_on")
                               else "appeal dates not published"),
            "cite": _cite(view)}


def _action_date(body: Mapping[str, Any]) -> tuple[str | None, str | None]:
    for field in DATE_FIELDS:
        if body.get(field):
            return body[field], field
    return None, None


def action_row(store: EnforcementStore, namespace: str, action: Mapping[str, Any], as_of: str | None,
               known_at_ms: int | None = None) -> dict[str, Any]:
    """One action as published (at the date when given), with penalties, appeals, notices and cited revisions."""
    body = action["record"]
    children = store.action_children(namespace, body["record_key"], known_at_ms=known_at_ms)
    decided = body.get("decided_on")
    if as_of is None:
        outcome_at = "as currently published"
    elif decided and decided <= as_of:
        outcome_at = "decided by the date"
    elif decided:
        outcome_at = "no published decision by the date (decided later)"
    else:
        outcome_at = "decision date not published"
    return {
        "action_key": body["record_key"], "authority": body["authority"], "action_number": body["action_number"],
        "action_type": body["action_type"], "action_type_as_published": body["action_type_as_published"],
        "title": body.get("title"), "status_as_published": body.get("status_as_published"),
        "source_status": body["source_status"], "initiated_on": body.get("initiated_on"), "decided_on": decided,
        "published_on": body.get("published_on"), "legal_bases": body.get("legal_bases") or [],
        "charges_as_published": body.get("charges_as_published") or [],
        "outcome_as_published": body.get("outcome_as_published") if outcome_at != "no published decision by the "
                                                                                  "date (decided later)" else None,
        "outcome_at_date": outcome_at, "settlement_as_published": body.get("settlement"),
        "lead_authority": body.get("lead_authority"), "concerned_authorities": body.get("concerned_authorities") or [],
        "corrective_measures_as_published": body.get("corrective_measures_as_published") or [],
        "facilities": body.get("facilities") or [], "court_cases": body.get("court_cases") or [],
        "natural_person_respondents": body.get("natural_person_respondents"),
        "penalties": [_penalty(v) for v in children["penalty"]],
        "appeals": sorted((_appeal(v, as_of) for v in children["appeal"]),
                          key=lambda a: (a["lodged_on"] or a["decided_on"] or "", a["cite"]["record_key"])),
        "notices": [{"document_type_as_published": v["record"]["document_type_as_published"],
                     "document_date": v["record"].get("document_date"), "url": v["record"]["url"],
                     "amended_on": v["record"].get("amended_on"), "source_status": v["record"]["source_status"],
                     "cite": _cite(v)} for v in children["enforcement_decision"]],
        "unknowns": body.get("unknowns") or [],
        "action_revision": _cite(action),
    }


def _similar_unknowns(identity, namespace: str, names: set[str], scopes) -> list[dict[str, Any]]:
    from src.kb.entities import normalize_surface

    wanted = {normalize_surface(n) for n in names if n}
    out = []
    for item in identity.unmatched(namespace, scopes=scopes):
        name = normalize_surface(item["name_as_published"])
        if any(name == w or (len(w) > 3 and (w in name or name in w)) for w in wanted):
            out.append({**item, "unknown": "not matched to this company; a similar name only, never counted"})
    return out


def actions_for_entity(conn: Any, namespace: str, entity: str, *, ownership_namespace: str, scopes: Iterable[str],
                       as_of: str | None = None, group: bool = False, include_unknowns: bool = False,
                       include_removed: bool = False, principal_id: str | None = None,
                       known_at_ms: int | None = None, authority: str | None = None) -> dict[str, Any]:
    from src.kb.competition_queries import group_members
    from src.kb.enforcement_identity import EnforcementIdentity

    scopes = set(scopes)
    authorize(namespace, scopes, READ_SCOPE)
    graph = _graph(conn, ownership_namespace, scopes, principal_id, known_at_ms)
    root = _resolve(graph, entity)
    members = group_members(graph, root, as_of) if group else [{"entity": root, "relation": "self", "path": []}]
    store = EnforcementStore(conn, initialize=False)
    identity = EnforcementIdentity(conn, initialize=False)
    respondents = {v["record"]["record_key"]: v for v in store.views(namespace, ("respondent",),
                                                                     known_at_ms=known_at_ms)}
    rows, excluded, removed, names, matches_used = [], [], [], set(), []
    seen: set[tuple[str, str]] = set()
    for member in members:
        names |= {n["name"] for n in graph.describe(member["entity"])["names"] if n.get("name")}
        for link in identity.accepted_links(namespace, graph.members(member["entity"]), scopes=scopes):
            respondent = respondents.get(link["subject_key"])
            if respondent is None:
                continue
            matches_used.append({**link, "group_member": member["entity"]})
            action = store.by_key(namespace, respondent["record"]["action_key"], known_at_ms=known_at_ms)
            if action is None or (authority and action["record"]["authority"] != authority):
                continue
            key = (action["record"]["record_key"], respondent["record"]["record_key"])
            if key in seen:
                continue
            seen.add(key)
            body = action["record"]
            if body["source_status"] == "removed_by_source" and not include_removed:
                removed.append({"action_key": body["record_key"], "cite": _cite(action),
                                "reason": "the regulator no longer publishes this action (a removed_by_source "
                                          "revision); history on request"})
                continue
            started = body.get("initiated_on") or body.get("published_on") or body.get("decided_on")
            if as_of and started and started > as_of:
                excluded.append({"action_key": body["record_key"], "reason": "not initiated or published by the "
                                                                             "as-of date"})
                continue
            row = action_row(store, namespace, action, as_of, known_at_ms)
            row.update({
                "respondent": {"name_as_published": respondent["record"]["name_as_published"],
                               "role_as_published": respondent["record"]["role_as_published"],
                               "identifiers": respondent["record"].get("identifiers") or [],
                               "cite": _cite(respondent)},
                "matched_through": {k: link[k] for k in ("candidate_id", "method", "low_evidence", "reviewer",
                                                         "reviewed_at_ms", "decision_id", "ownership_key")},
                "group_member": member["entity"], "group_relation": member["relation"],
                "ownership_path": member["path"]})
            row["cites"] = {"action_revision_id": action["revision_id"],
                            "respondent_revision_id": respondent["revision_id"],
                            "penalty_revision_ids": [p["cite"]["revision_id"] for p in row["penalties"]],
                            "appeal_revision_ids": [a["cite"]["revision_id"] for a in row["appeals"]],
                            "notice_revision_ids": [n["cite"]["revision_id"] for n in row["notices"]],
                            "identity_candidate": link["candidate_id"], "identity_decision": link["decision_id"]}
            rows.append(row)
    rows.sort(key=lambda r: (r["authority"], r["action_number"], r["respondent"]["cite"]["record_key"]))
    by_authority: dict[str, list[str]] = {}
    for row in rows:
        by_authority.setdefault(row["authority"], []).append(row["action_key"])
    answer = {
        "contract": ENTITY_CONTRACT, "namespace": namespace, "ownership_namespace": ownership_namespace,
        "entity": graph.describe(root), "as_of": as_of, "group": group,
        "group_basis": ("entity clusters from accepted ownership identity decisions; group members from stated "
                        "ownership and control assertions as of the date; actions reached through accepted "
                        "enforcement identity matches only (listed in matches_used)"),
        "group_members": [{"entity": m["entity"], "relation": m["relation"], "ownership_path": m["path"]}
                          for m in members],
        "matches_used": matches_used,
        "status": "answered" if rows else "no_action_on_record", "actions": rows, "by_authority": by_authority,
        "excluded": excluded, "removed_by_source": removed,
        "coverage": "only the declared, acquired enforcement selections and reviewed identity matches are searched; "
                    "'no action on record' is not a statement that no action exists",
        "notice": NOTICE,
    }
    if not rows:
        answer["message"] = "no enforcement action on record for this company's reviewed identity matches (not a clean " \
                            "bill)"
    if include_unknowns:
        answer["unknowns"] = _similar_unknowns(identity, namespace, names, scopes)
    return answer


def _basis_keys(text: str) -> set[str]:
    from src.kb.enforcement_links import parse_legal_bases

    # A whole act (celex:32016R0679) matches its provisions (celex:32016R0679:art32) by prefix; a provision matches
    # only itself.
    return {c["key"] for c in parse_legal_bases(text, context="gdpr")}


def actions_by_authority(conn: Any, namespace: str, *, scopes: Iterable[str], authority: str | None = None,
                         legal_basis: str | None = None, date_from: str | None = None, date_to: str | None = None,
                         include_removed: bool = False, known_at_ms: int | None = None) -> dict[str, Any]:
    from src.kb.enforcement_links import EnforcementLinks

    authorize(namespace, scopes, READ_SCOPE)
    if not authority and not legal_basis:
        raise EnforcementError("invalid_request", "name an authority, a legal basis or both")
    store = EnforcementStore(conn, initialize=False)
    wanted = _basis_keys(legal_basis) if legal_basis else set()
    links = EnforcementLinks(conn, initialize=False)
    rows, undated, removed = [], [], []
    for view in store.views(namespace, ("enforcement_action",), known_at_ms=known_at_ms):
        body = view["record"]
        lead = (body.get("lead_authority") or {}).get("code")
        if authority and authority not in {body["authority"], lead}:
            continue
        match_basis = None
        if legal_basis:
            own = set()
            for text in (body.get("legal_bases") or []) + (body.get("charges_as_published") or []):
                own |= _basis_keys(text)
            works = {link["target_record"] for link in links.links(namespace, citing_record_key=body["record_key"])
                     if link["status"] == "resolved" and link["target_pack"] == "legal.works"}
            if wanted and (wanted & own or any(k.startswith(w + ":") for w in wanted for k in own)):
                match_basis = "exact citation"
            elif legal_basis in works:
                match_basis = "linked Legal work"
            elif not wanted and any(legal_basis.casefold() in b.casefold() for b in body.get("legal_bases") or []):
                match_basis = "the published wording contains the query text (not a parsed citation)"
            if match_basis is None:
                continue
        if body["source_status"] == "removed_by_source" and not include_removed:
            removed.append({"action_key": body["record_key"], "cite": _cite(view)})
            continue
        day, field = _action_date(body)
        if day is None:
            undated.append({"action_key": body["record_key"], "unknown": "no initiated, decided or published date",
                            "cite": _cite(view)})
            continue
        if (date_from and day < date_from) or (date_to and day > date_to[:len(day)]):
            continue
        row = action_row(store, namespace, view, None, known_at_ms)
        row.update({"period_date": day, "period_date_field": field, "legal_basis_match": match_basis})
        rows.append(row)
    rows.sort(key=lambda r: (r["authority"], r["period_date"], r["action_number"]))
    figures: dict[str, dict[str, list[dict[str, Any]]]] = {}
    undisclosed = []
    for row in rows:
        for penalty in row["penalties"]:
            if penalty["amount_status"] != "stated":
                undisclosed.append({"action_key": row["action_key"], "penalty_type": penalty["penalty_type"],
                                    "unknown": penalty["note"] or "amount not published",
                                    "revision_id": penalty["cite"]["revision_id"]})
                continue
            figures.setdefault(row["authority"], {}).setdefault(penalty["currency"] or "currency not published",
                                                                []).append(
                {"action_key": row["action_key"], "penalty_type": penalty["penalty_type"],
                 "amount_as_published": penalty["amount_as_published"], "revision_id": penalty["cite"]["revision_id"]})
    return {
        "contract": AUTHORITY_CONTRACT, "namespace": namespace, "authority": authority, "legal_basis": legal_basis,
        "legal_basis_keys": sorted(wanted), "period": {"from": date_from, "to": date_to,
                                                       "date_used": "decided_on, else published_on, else initiated_on "
                                                                    "(named per action)"},
        "status": "answered" if rows else "no_action_on_record", "actions": rows,
        "penalty_figures": {"by_authority_and_currency": figures,
                            "note": "figures are listed as published; they are never summed or converted, within or "
                                    "across currencies and authorities"},
        "undisclosed_penalties": undisclosed, "undated": undated, "removed_by_source": removed,
        "cites": [r["action_revision"]["revision_id"] for r in rows], "notice": NOTICE,
    }


def action_history(conn: Any, namespace: str, action_key: str, *, scopes: Iterable[str]) -> dict[str, Any]:
    """An action's full revision chain with its notices, respondents, penalties, appeals and links."""
    from src.kb.enforcement_links import EnforcementLinks

    authorize(namespace, scopes, READ_SCOPE)
    store = EnforcementStore(conn, initialize=False)
    current = store.by_key(namespace, action_key)
    if current is None:
        return {"contract": HISTORY_CONTRACT, "namespace": namespace, "action_key": action_key,
                "status": "no_action_on_record", "notice": NOTICE}
    children = store.action_children(namespace, action_key)

    def chain(key: str) -> list[dict[str, Any]]:
        return [{"revision": v["revision"], "revision_id": v["revision_id"], "observed_at_ms": v["observed_at_ms"],
                 "run_id": v["run_id"], "source_status": v["record"].get("source_status"),
                 "source_revision": v["record"]["source"].get("revision"), "record": v["record"]}
                for v in store.history(namespace, key)]

    links = EnforcementLinks(conn, initialize=False)
    keys = [action_key] + [v["record"]["record_key"] for group in children.values() for v in group]
    return {
        "contract": HISTORY_CONTRACT, "namespace": namespace, "action_key": action_key, "status": "answered",
        "current": action_row(store, namespace, current, None), "action_revisions": chain(action_key),
        "notice_revisions": {v["record"]["record_key"]: chain(v["record"]["record_key"])
                             for v in children["enforcement_decision"]},
        "appeal_revisions": {v["record"]["record_key"]: chain(v["record"]["record_key"]) for v in children["appeal"]},
        "respondents": [{"name_as_published": v["record"]["name_as_published"],
                         "role_as_published": v["record"]["role_as_published"],
                         "identifiers": v["record"].get("identifiers") or [], "cite": _cite(v)}
                        for v in children["respondent"]],
        "links": [link for key in keys for link in links.links(namespace, citing_record_key=key)],
        "notice": NOTICE,
    }
