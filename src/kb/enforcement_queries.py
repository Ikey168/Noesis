"""Enforcement actions for an entity (and its group) as of a date, and by authority or legal basis (#2651, EN09, EN10).

* :func:`actions_for_entity` - the actions whose respondents reach an
  ownership entity through **accepted** identity matches (EN07), optionally
  expanded to its group through the ownership graph as of the date
  (:func:`src.kb.competition_queries.group_members`: the stated control-chain
  tops and their stated subsidiaries over the accepted ownership identity
  decisions, which the answer lists). Each action shows what was published by
  the as-of date: the decisions, penalties and appeals dated on or before it,
  the outcome as published (a settlement "without admitting or denying" keeps
  that wording) and the appeal status as published. An entity with no
  matched respondent gets ``no_action_on_record`` - never a clean bill.
* :func:`actions_for_identifier` - the same answer keyed by an identifier the
  regulator itself published for a respondent (CIK, FRN, LEI), for
  deployments without the ownership store; it is a lookup of published text,
  not an identity match.
* :func:`actions_by_authority` - the actions of an authority and/or citing a
  legal basis over a period, with outcomes and penalties as published.
  Penalties are **never summed** - not across currencies, not across
  authorities, not within one; they are listed per authority and currency
  with a count, and undisclosed or unpublished figures stay explicit
  unknowns.

Every answer cites each action revision (and the decision, penalty, appeal and
respondent revisions it shows) and exports as a ``noesis-evidence-bundle-v1``
(:func:`export_bundle`). Nothing scores risk or compliance, infers wrongdoing
from an initiated action, merges a settled outcome into a finding or profiles
a named individual.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any

from src.kb.enforcement import (
    NOTICE,
    READ_SCOPE,
    EnforcementError,
    EnforcementStore,
    authorize,
    table_exists,
)
from src.kb.enforcement_records import digest

ENTITY_CONTRACT = "noesis-enforcement-actions-answer-v1"
AUTHORITY_CONTRACT = "noesis-enforcement-authority-answer-v1"
HISTORY_CONTRACT = "noesis-enforcement-action-history-v1"
OWNERSHIP_READ = "knowledge:ownership:read"
NO_ACTION = ("no action on record for this entity's reviewed identity matches inside the acquired coverage (not a "
             "statement that no action exists)")
SETTLEMENT_NOTE = ("settled as published; the stated admission wording is quoted and is not a finding of the "
                   "allegations")


def _as_of(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError as exc:
        raise EnforcementError("invalid_request", "dates are YYYY-MM-DD") from exc


def _on_or_before(value: str | None, as_of: str | None) -> bool:
    return as_of is None or (value is not None and value[:10] <= as_of)


def _cite(view: Mapping[str, Any]) -> dict[str, Any]:
    body = view["record"]
    return {"record_key": body["record_key"], "revision_id": view["revision_id"], "revision": view["revision"],
            "observed_at_ms": view["observed_at_ms"], "provider": body["source"]["provider"],
            "url": body["source"].get("url"), "source_revision": body["source"].get("revision"),
            "evidence_origin": body["source"].get("evidence_origin")}


def _respondent_view(view: Mapping[str, Any]) -> dict[str, Any]:
    body = view["record"]
    if body["party_type"] == "natural_person":
        return {"party_type": "natural_person", "pseudonym": body["pseudonym"],
                "role_as_published": body["role_as_published"], "cite": _cite(view)}
    return {"party_type": "organisation", "name_as_published": body["name_as_published"],
            "role_as_published": body["role_as_published"], "identifiers": body.get("identifiers") or [],
            "cite": _cite(view)}


def _penalty_view(view: Mapping[str, Any]) -> dict[str, Any]:
    body = view["record"]
    return {"penalty_type": body["penalty_type"], "penalty_type_as_published": body["penalty_type_as_published"],
            "amount_as_published": body.get("amount_as_published"), "amount": body.get("amount"),
            "currency": body.get("currency"), "status": body["status"], "stage": body.get("stage"),
            "discount_as_published": body.get("discount_as_published"), "respondent_key": body.get("respondent_key"),
            "cite": _cite(view)}


def action_row(store: EnforcementStore, namespace: str, action_key: str, *, as_of: str | None = None,
               known_at_ms: int | None = None) -> dict[str, Any] | None:
    """One action as published by the as-of date, with every shown revision cited."""
    action = store.by_key(namespace, action_key, known_at_ms=known_at_ms)
    if action is None:
        return None
    body = action["record"]
    children = store.action_children(namespace, action_key, known_at_ms=known_at_ms)
    first = min((d for d in (body.get("initiated_on"), body.get("published_on"), body.get("decided_on")) if d),
                default=None)
    decisions = [v for v in children["decision"] if _on_or_before(v["record"].get("decided_on"), as_of)
                 or (as_of and v["record"].get("decided_on") is None and _on_or_before(body.get("published_on"),
                                                                                      as_of))]
    shown = {v["record"]["record_key"] for v in decisions}
    penalties = [v for v in children["penalty"] if v["record"].get("decision_key") in shown
                 or (v["record"].get("decision_key") is None and _on_or_before(first, as_of))]
    appeals = [v for v in children["appeal"] if _on_or_before(v["record"].get("stated_on"), as_of)]
    later = [v["record"]["record_key"] for v in children["decision"] + children["appeal"]
             if v not in decisions and v not in appeals]
    unknowns = list(body.get("unknowns") or [])
    unknowns += [f"penalty {p['record']['penalty_type']}: figure not published" for p in penalties
                 if p["record"]["status"] == "not_published"]
    if not [v for v in children["respondent"] if v["record"]["party_type"] == "organisation"]:
        unknowns.append("no organisation respondent published")
    row = {
        "action_key": action_key, "authority": body["authority"],
        "authority_as_published": body.get("authority_as_published"), "native_id": body["native_id"],
        "identifiers": body.get("identifiers") or [], "action_type": body["action_type"],
        "action_type_as_published": body["action_type_as_published"], "title": body.get("title"),
        "legal_bases": body.get("legal_bases") or [], "initiated_on": body.get("initiated_on"),
        "decided_on": body.get("decided_on"), "published_on": body.get("published_on"),
        "publication_status": body.get("publication_status"),
        "outcome": ({"as_published": body.get("outcome_as_published"), "settled": body.get("settled"),
                     "admission_wording": body.get("admission_wording"),
                     "note": SETTLEMENT_NOTE if body.get("settled") else
                     "outcome as published; an initiated action is not a finding"}
                    if decisions or as_of is None else
                    {"as_published": None, "settled": None, "admission_wording": None,
                     "note": "no decision published by the as-of date; an initiated action is not a finding"}),
        "decisions": [{"decision_type_as_published": v["record"]["decision_type_as_published"],
                       "decided_on": v["record"].get("decided_on"),
                       "outcome_as_published": v["record"].get("outcome_as_published"),
                       "settled": v["record"].get("settled"), "admission_wording": v["record"].get("admission_wording"),
                       "corrective_measures": v["record"].get("corrective_measures") or [],
                       "document_url": v["record"].get("document_url"), "cite": _cite(v)} for v in decisions],
        "penalties": [_penalty_view(v) for v in penalties],
        "appeals": [{"forum_as_published": v["record"]["forum_as_published"],
                     "reference": v["record"].get("reference"), "status_as_published": v["record"]["status_as_published"],
                     "stated_on": v["record"].get("stated_on"), "cite": _cite(v)} for v in appeals],
        "appeal_status": ("appeal published" if appeals else "no appeal published by the as-of date"),
        "respondents": [_respondent_view(v) for v in children["respondent"]],
        "court_cases": body.get("court_cases") or [], "facilities": body.get("facilities") or [],
        "concerned_authorities": body.get("concerned_authorities") or [],
        "documents": [{"url": v["record"]["url"], "title": v["record"].get("title"),
                       "document_type_as_published": v["record"].get("document_type_as_published"),
                       "published_on": v["record"].get("published_on"),
                       "content_sha256": v["record"].get("content_sha256"), "cite": _cite(v)}
                      for v in children["notice_document"]],
        "not_yet_published_at_as_of": later if as_of else [],
        "not_yet_initiated": bool(as_of and first and first > as_of),
        "unknowns": sorted(set(unknowns)),
        "cite": _cite(action),
    }
    return row


def _group(conn: Any, ownership_namespace: str, entity: str, *, as_of: str | None, group: bool, scopes: set[str],
           principal_id: str | None, known_at_ms: int | None):
    from src.kb.competition_queries import group_members
    from src.kb.ownership_graph import OwnershipGraph
    from src.kb.ownership_store import OwnershipError

    graph = OwnershipGraph(conn, ownership_namespace, principal_id=principal_id, scopes=scopes,
                           known_at_ms=known_at_ms)
    person = [v for v in graph.by_kind.get("person", []) if v["record"]["record_key"] == entity
              or v["record_id"] == entity]
    if person:
        raise EnforcementError("natural_person_not_a_query_key",
                               "natural persons are never a query key (EN01 minimisation decision)")
    try:
        root = graph.resolve(entity)
    except OwnershipError:
        root = next((graph.cluster(k) for k, views in graph.entities.items()
                     if any(v["record"].get("canonical_entity_id") == entity for v in views)), None)
        if root is None:
            raise EnforcementError("not_found", "the entity is not an entity of the ownership namespace") from None
    members = group_members(graph, root, as_of) if group else [{"entity": root, "relation": "self", "path": []}]
    return graph, root, members


def actions_for_entity(conn: Any, namespace: str, entity: str, *, ownership_namespace: str, scopes: Iterable[str],
                       as_of: str | None = None, group: bool = False, principal_id: str | None = None,
                       known_at_ms: int | None = None, include_unmatched: bool = False) -> dict[str, Any]:
    from src.kb.enforcement_identity import EnforcementIdentity

    scopes = set(scopes)
    authorize(namespace, scopes, READ_SCOPE)
    as_of = _as_of(as_of)
    base = {"contract": ENTITY_CONTRACT, "namespace": namespace, "ownership_namespace": ownership_namespace,
            "as_of": as_of, "group": group, "notice": NOTICE}
    if not table_exists(conn, "ownership_records"):
        return {**base, "status": "ownership_unavailable", "actions": [],
                "message": "the Corporate Ownership store is not installed; use actions_for_identifier with an "
                           "identifier the regulator published"}
    if OWNERSHIP_READ not in scopes and "operator" not in scopes:
        raise EnforcementError("unauthorized", f"{OWNERSHIP_READ} is required to read ownership entities")
    graph, root, members = _group(conn, ownership_namespace, entity, as_of=as_of, group=group, scopes=scopes,
                                  principal_id=principal_id, known_at_ms=known_at_ms)
    store = EnforcementStore(conn, initialize=False)
    identity = EnforcementIdentity(conn, initialize=False)
    respondents = {v["record"]["record_key"]: v for v in store.views(namespace, ("respondent",),
                                                                        known_at_ms=known_at_ms)}
    rows, excluded, seen = [], [], set()
    for member in members:
        for link in identity.accepted_links(namespace, graph.members(member["entity"]), scopes=scopes):
            respondent = respondents.get(link["subject_key"])
            if respondent is None or (respondent["record"]["action_key"], link["subject_key"]) in seen:
                continue
            seen.add((respondent["record"]["action_key"], link["subject_key"]))
            row = action_row(store, namespace, respondent["record"]["action_key"], as_of=as_of,
                             known_at_ms=known_at_ms)
            if row is None:
                continue
            if row["not_yet_initiated"]:
                excluded.append({"action_key": row["action_key"], "reason": "initiated or published after the as-of "
                                                                            "date"})
                continue
            matched = {k: link[k] for k in ("candidate_id", "method", "low_evidence", "reviewer", "reviewed_at_ms",
                                            "decision_id", "ownership_key")}
            row.update({"respondent": _respondent_view(respondent), "matched_through": matched,
                        "group_member": member["entity"], "group_relation": member["relation"],
                        "ownership_path": member["path"]})
            rows.append(row)
    rows.sort(key=lambda r: (r["authority"], r["decided_on"] or r["published_on"] or "", r["action_key"]))
    by_authority: dict[str, list[str]] = {}
    for row in rows:
        by_authority.setdefault(row["authority"], []).append(row["action_key"])
    answer = {**base, "entity": graph.describe(root),
              "group_members": [{"entity": m["entity"], "relation": m["relation"], "ownership_path": m["path"]}
                                for m in members],
              "group_basis": {"accepted_ownership_identity_decisions": list(graph.identity),
                              "note": "group expansion uses the accepted ownership identity decisions listed here and "
                                      "the ownership assertions on each path; nothing else"} if group else None,
              "status": "answered" if rows else "no_action_on_record", "actions": rows,
              "by_authority": by_authority, "excluded": excluded,
              "coverage": "only the declared, acquired enforcement selections and reviewed identity matches are "
                          "searched"}
    if not rows:
        answer["message"] = NO_ACTION
    if include_unmatched:
        from src.kb.entities import normalize_surface

        names = {normalize_surface(n["name"]) for m in members for n in graph.describe(m["entity"])["names"]
                 if n.get("name")}
        answer["unmatched_respondents_of_same_name"] = [
            {**u, "unknown": "not matched to this entity; a name only, never counted"}
            for u in identity.unmatched(namespace, scopes=scopes) if normalize_surface(u["name_as_published"]) in names]
    return answer


def actions_for_identifier(conn: Any, namespace: str, scheme: str, value: str, *, scopes: Iterable[str],
                           as_of: str | None = None) -> dict[str, Any]:
    """Actions whose respondents carry an identifier the regulator published (no identity match involved)."""
    authorize(namespace, scopes, READ_SCOPE)
    as_of = _as_of(as_of)
    wanted = "".join(ch for ch in str(value).upper() if ch.isalnum()).lstrip("0")
    store = EnforcementStore(conn, initialize=False)
    rows = []
    for view in store.views(namespace, ("respondent",)):
        body = view["record"]
        if not any(i["scheme"] == scheme and "".join(ch for ch in str(i["value"]).upper()
                                                     if ch.isalnum()).lstrip("0") == wanted
                   for i in body.get("identifiers") or []):
            continue
        row = action_row(store, namespace, body["action_key"], as_of=as_of)
        if row and not row["not_yet_initiated"]:
            rows.append({**row, "respondent": _respondent_view(view),
                         "matched_through": {"method": "published identifier on the respondent record",
                                             "scheme": scheme, "value": value}})
    return {"contract": ENTITY_CONTRACT, "namespace": namespace, "query": {"scheme": scheme, "value": value},
            "as_of": as_of, "status": "answered" if rows else "no_action_on_record", "actions": rows,
            "note": "a lookup of identifiers as the regulators published them; not an identity match",
            "notice": NOTICE, **({} if rows else {"message": NO_ACTION})}


def actions_by_authority(conn: Any, namespace: str, *, scopes: Iterable[str], authority: str | None = None,
                         legal_basis: str | None = None, date_from: str | None = None, date_to: str | None = None,
                         as_of: str | None = None) -> dict[str, Any]:
    from src.kb.enforcement_links import basis_keys, parse_references

    authorize(namespace, scopes, READ_SCOPE)
    if not authority and not legal_basis:
        raise EnforcementError("invalid_request", "name an authority, a legal basis or both")
    date_from, date_to, as_of = _as_of(date_from), _as_of(date_to), _as_of(as_of)
    wanted_keys = {c["key"] for c in parse_references(legal_basis, authority=authority)} if legal_basis else set()
    store = EnforcementStore(conn, initialize=False)
    rows, undated = [], []
    for view in store.views(namespace, ("enforcement_action",)):
        body = view["record"]
        if authority and body["authority"] != authority:
            continue
        if legal_basis:
            published = {" ".join(b.lower().split()) for b in body.get("legal_bases") or []}
            if not (" ".join(legal_basis.lower().split()) in published or (wanted_keys & basis_keys(body))):
                continue
        reference = body.get("decided_on") or body.get("published_on") or body.get("initiated_on")
        if reference is None:
            undated.append({"action_key": body["record_key"], "unknown": "no published date; outside any period"})
            continue
        if (date_from and reference < date_from) or (date_to and reference > date_to):
            continue
        row = action_row(store, namespace, body["record_key"], as_of=as_of or date_to)
        if row:
            row["period_date"] = {"value": reference, "basis": "decided_on" if body.get("decided_on") else
                                  "published_on" if body.get("published_on") else "initiated_on"}
            rows.append(row)
    rows.sort(key=lambda r: (r["authority"], r["period_date"]["value"], r["action_key"]))
    listed: dict[tuple[str, str], dict[str, Any]] = {}
    unknowns = list(undated)
    for row in rows:
        for penalty in row["penalties"]:
            if penalty["status"] != "stated" or not penalty["currency"]:
                unknowns.append({"action_key": row["action_key"], "penalty_type": penalty["penalty_type"],
                                 "unknown": "figure not published or no currency stated"})
                continue
            entry = listed.setdefault((row["authority"], penalty["currency"]), {
                "authority": row["authority"], "currency": penalty["currency"], "penalties": []})
            entry["penalties"].append({"action_key": row["action_key"], "penalty_type": penalty["penalty_type"],
                                       "amount_as_published": penalty["amount_as_published"],
                                       "stage": penalty["stage"], "revision_id": penalty["cite"]["revision_id"]})
    for row in rows:
        if not row["penalties"]:
            unknowns.append({"action_key": row["action_key"], "unknown": "no penalty published for this action"})
    return {"contract": AUTHORITY_CONTRACT, "namespace": namespace,
            "query": {"authority": authority, "legal_basis": legal_basis, "reference_keys": sorted(wanted_keys),
                      "date_from": date_from, "date_to": date_to},
            "authority_identity": _authority(authority), "status": "answered" if rows else "no_action_on_record",
            "actions": rows,
            "penalties_by_authority_and_currency": [
                {**entry, "count": len(entry["penalties"])} for _, entry in sorted(listed.items())],
            "totals_note": "not computed: penalties are never summed across currencies or authorities, and the "
                           "figures of different stages (before or after a settlement discount) and types are not "
                           "additive",
            "unknowns": unknowns, "notice": NOTICE, **({} if rows else {"message": "no action on record for this "
                                                                                   "authority, basis and period"})}


def _authority(authority: str | None) -> dict[str, Any] | None:
    from src.kb.enforcement_identity import authority_identity

    return authority_identity(authority) if authority else None


def export_bundle(answer: Mapping[str, Any], *, created_at_ms: int = 0) -> dict[str, Any]:
    """A ``noesis-evidence-bundle-v1`` citing every action, decision, penalty, appeal and respondent revision with
    its source, record revision and as-of time."""
    from src.evidence_bundle.builder import EvidenceBundleBuilder

    as_of = answer.get("as_of") or (answer.get("query") or {}).get("date_to")
    as_of_ms = None
    if as_of:
        as_of_ms = int((date.fromisoformat(as_of) - date(1970, 1, 1)).total_seconds() * 1000)
    builder = EvidenceBundleBuilder("receipt", {"operation": "enforcement-actions", "contract": answer["contract"],
                                               "query": answer.get("query"), "as_of": as_of},
                                    created_at_ms=created_at_ms, as_of_ms=as_of_ms)
    refs: list[str] = []

    def add(kind: str, cite: Mapping[str, Any], payload: Mapping[str, Any]) -> None:
        object_id = f"enforcement:{cite['revision_id']}"
        if object_id in refs:
            return
        builder.add_object("evidence", {
            "kind": kind, **payload, "as_of": as_of,
            "locator": {"cited": True, "document_id": cite["record_key"], "revision_id": cite["revision_id"],
                        "url": cite.get("url")},
            "citation": dict(cite)}, object_id=object_id)
        refs.append(object_id)
        if cite.get("url"):
            builder.add_external_reference(f"source:{cite['record_key']}", cite["url"], required=False)

    for row in answer.get("actions") or []:
        add("enforcement-action-revision", row["cite"], {
            "action_key": row["action_key"], "authority": row["authority"], "title": row["title"],
            "outcome": row["outcome"], "legal_bases": row["legal_bases"], "appeal_status": row["appeal_status"]})
        for decision in row["decisions"]:
            add("enforcement-decision-revision", decision["cite"], {k: v for k, v in decision.items() if k != "cite"})
        for penalty in row["penalties"]:
            add("enforcement-penalty-revision", penalty["cite"], {k: v for k, v in penalty.items() if k != "cite"})
        for appeal in row["appeals"]:
            add("enforcement-appeal-revision", appeal["cite"], {k: v for k, v in appeal.items() if k != "cite"})
        if row.get("respondent"):
            add("enforcement-respondent-revision", row["respondent"]["cite"],
                {k: v for k, v in row["respondent"].items() if k != "cite"})
    root = {k: v for k, v in answer.items() if k != "actions"}
    root["action_keys"] = [row["action_key"] for row in answer.get("actions") or []]
    builder.add_object("receipt", root, object_id=f"{answer['contract']}:{digest(refs)[:16]}", references=refs,
                       root=True)
    if not answer.get("actions"):
        builder.add_omission(str(answer.get("message") or answer.get("status") or NO_ACTION))
    for item in answer.get("unknowns") or []:
        if isinstance(item, Mapping):
            builder.add_omission(f"unknown: {item.get('action_key')} ({item.get('unknown')})")
    return builder.build()


def action_history(conn: Any, namespace: str, action_key: str, *, scopes: Iterable[str], as_of: str | None = None,
                   known_at_ms: int | None = None) -> dict[str, Any]:
    """One action as published by a date (or as recorded at a record time), with every revision of it and of its
    decisions, penalties, appeals and notice documents, and its links; corrections and removals are revisions."""
    from src.kb.enforcement_links import EnforcementLinks

    authorize(namespace, scopes, READ_SCOPE)
    as_of = _as_of(as_of)
    store = EnforcementStore(conn, initialize=False)
    row = action_row(store, namespace, action_key, as_of=as_of, known_at_ms=known_at_ms)
    if row is None:
        return {"contract": HISTORY_CONTRACT, "namespace": namespace, "action_key": action_key,
                "status": "no_action_on_record", "notice": NOTICE}
    children = store.action_children(namespace, action_key)
    revisions = {}
    for key in [action_key] + [v["record"]["record_key"] for kind in ("decision", "penalty", "appeal",
                                                                       "notice_document") for v in children[kind]]:
        revisions[key] = [{"revision": v["revision"], "revision_id": v["revision_id"],
                           "observed_at_ms": v["observed_at_ms"], "run_id": v["run_id"],
                           "publication_status": v["record"].get("publication_status"),
                           "source_revision": v["record"]["source"].get("revision"),
                           "content_sha256": v["record"].get("content_sha256"),
                           "amount_as_published": v["record"].get("amount_as_published")}
                          for v in store.history(namespace, key)]
    links = EnforcementLinks(conn, initialize=False)
    return {"contract": HISTORY_CONTRACT, "namespace": namespace, "action_key": action_key, "as_of": as_of,
            "known_at_ms": known_at_ms, "status": "answered", "action": row, "revisions": revisions,
            "links": [link for key in [action_key] + [v["record"]["record_key"] for v in children["respondent"]]
                      for link in links.links(namespace, citing_record_key=key)],
            "notice": NOTICE}
