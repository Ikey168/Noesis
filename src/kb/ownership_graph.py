"""Ownership-graph queries as of a date, with sources, validity and conflicts visible (O09).

Every answer is assembled from ownership *assertions* (what sources state).
Records are grouped into entities only through accepted, unreverted identity
decisions (:mod:`src.kb.ownership_identity`); nothing is merged.

* Direct parents: accounting-consolidation ``direct_parent`` assertions *and*
  control assertions naming an entity holder (shareholding, voting rights,
  board appointment, significant influence, other control). They stay
  distinct kinds in every edge.
* Ultimate parents: ``ultimate_parent`` assertions and ultimate-level
  reporting exceptions only; a chain top is a *derived path*, reported as
  such, never a stated ultimate parent.
* Assertions naming the same holder are returned together with the reasons
  they differ (``different_source``, ``different_date``, ``different_kind``).
  Assertions naming *different* holders for one slot are a conflict and are
  returned together with those reasons; they are never resolved by rank.
* As-of status per edge: ``valid``, ``undetermined`` (a start or end is
  unknown), ``not_started`` or ``ended``. Only valid and undetermined edges
  answer a query; the others are counted under ``excluded``.
* Traversals have an explicit ``max_depth`` (at most :data:`MAX_DEPTH`) and
  report cycles and truncation. Every result pins the record revisions and
  accepted identity candidates it used, so :func:`replay` recomputes it
  exactly from those revisions.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from src.kb.ownership_records import CONTROL_KINDS, ENTITY_KINDS, digest
from src.kb.ownership_store import OwnershipError, OwnershipStore

MAX_DEPTH = 10
PARENT_SLOT_KINDS = ("direct_parent",) + CONTROL_KINDS
NOTICE = ("Assertions are what each source states. Conflicts are shown, not resolved. This is not a "
          "beneficial-ownership, sanctions or AML determination.")


def _low(value: str | None) -> str | None:
    if not value:
        return None
    parts = value.split("-")
    return "-".join(parts + ["01"] * (3 - len(parts)))


def as_of_status(validity: Mapping[str, Any], as_of: str | None) -> str:
    """valid / undetermined / not_started / ended for an as-of date (half-open [from, to))."""
    if as_of is None:
        return "not_evaluated"
    start, end = _low(validity.get("from")), _low(validity.get("to"))
    if start and as_of < start:
        return "not_started"
    if validity.get("to_status") == "stated" and end and as_of >= end:
        return "ended"
    if validity.get("from_status") == "unknown" or validity.get("to_status") == "unknown":
        return "undetermined"
    return "valid"


def control_basis(edge: Mapping[str, Any]) -> str:
    """Why an entity holder sits in the parent slot, read only from what the source states.

    Consolidation parents and board-appointment / influence / other-control
    statements are control as stated. A shareholding or voting-rights figure
    counts only when its stated lower bound is above half; a smaller stated
    figure is a minority holding, and an unstated figure stays ``share-not-stated``.
    """
    kind = edge["assertion_kind"]
    if kind in {"direct_parent", "ultimate_parent"}:
        return "consolidation-as-stated"
    if kind in {"appoint_directors", "significant_influence", "other_control"}:
        return "control-as-stated"
    share = edge.get("share") or {}
    low = share.get("exact") if "exact" in share else (share.get("band") or {}).get("min")
    if low is None:
        return "share-not-stated"
    return "majority-as-stated" if float(low) > 50 or (float(low) == 50 and "band" in share and share["band"].get(
        "min_inclusive") is False) else "minority-as-stated"


def differences(assertions: Iterable[Mapping[str, Any]]) -> list[str]:
    items = list(assertions)
    reasons = []
    if len({a["source"]["provider"] for a in items}) > 1:
        reasons.append("different_source")
    if len({(a["validity"].get("from"), a.get("statement_date")) for a in items}) > 1:
        reasons.append("different_date")
    if len({a["assertion_kind"] for a in items}) > 1:
        reasons.append("different_kind")
    return reasons


class OwnershipGraph:
    def __init__(self, conn: Any, namespace: str, *, principal_id: str | None, scopes: Iterable[str],
                 known_at_ms: int | None = None, pins: Mapping[str, int] | None = None,
                 identity: Iterable[str] | None = None) -> None:
        from src.kb.ownership_identity import OwnershipIdentityService

        self.namespace, self.principal_id, self.scopes = namespace, principal_id, set(scopes)
        self.store = OwnershipStore(conn, initialize=False)
        self.views = self.store.records(namespace, principal_id=principal_id, scopes=self.scopes,
                                        known_at_ms=known_at_ms, pins=pins)
        service = OwnershipIdentityService(conn, initialize=False)
        self.identity = sorted(identity) if identity is not None else service.accepted_ids(namespace)
        self._clusters = service.clusters(namespace, accepted=self.identity)
        self.pins = {v["record_id"]: v["revision"] for v in self.views}
        self.entities: dict[str, list[dict[str, Any]]] = {}
        self.assertions, self.events, self.by_kind = [], [], {}
        for view in self.views:
            body = view["record"]
            self.by_kind.setdefault(body["kind"], []).append(view)
            if body["kind"] in ENTITY_KINDS:
                self.entities.setdefault(body["record_key"], []).append(view)
            elif body["kind"] == "ownership_assertion":
                self.assertions.append(view)

    # -------------------------------------------------------------- identity

    def cluster(self, key: str) -> str:
        return self._clusters.get(key, key)

    def members(self, key: str) -> list[str]:
        root = self.cluster(key)
        keys = {k for k in self.entities if self.cluster(k) == root} | {k for k, r in self._clusters.items() if r == root}
        return sorted(keys | {key})

    def resolve(self, entity: str) -> str:
        """Accept a record_key or record_id and return its cluster representative."""
        if entity in self.entities or entity in self._clusters:
            return self.cluster(entity)
        for key, views in self.entities.items():
            if any(v["record_id"] == entity for v in views):
                return self.cluster(key)
        raise OwnershipError("not_found", "entity is not visible in this namespace")

    def describe(self, cluster: str) -> dict[str, Any]:
        names = []
        for key in self.members(cluster):
            for view in self.entities.get(key, []):
                body = view["record"]
                names.append({"record_key": key, "record_id": view["record_id"], "revision": view["revision"],
                              "name": body.get("name"), "provider": body["source"]["provider"],
                              "jurisdiction": body.get("jurisdiction"), "redacted": bool(view.get("redacted"))})
        return {"entity": cluster, "members": self.members(cluster), "names": names,
                "acquired": bool(names)}

    # ----------------------------------------------------------------- edges

    def _edge(self, view: Mapping[str, Any], as_of: str | None) -> dict[str, Any]:
        edge = self._edge_body(view, as_of)
        if edge["assertion_kind"] != "reporting_exception":
            edge["control_basis"] = control_basis(edge)
        return edge

    def _edge_body(self, view: Mapping[str, Any], as_of: str | None) -> dict[str, Any]:
        body = view["record"]
        holder = body.get("holder")
        return {
            "record_id": view["record_id"], "revision": view["revision"], "revision_id": view["revision_id"],
            "assertion_kind": body["assertion_kind"], "subject": self.cluster(body["subject_key"]),
            "subject_key": body["subject_key"],
            "holder": None if holder is None else {**holder, "entity": self.cluster(holder["key"]) if holder.get("key") else None},
            "share": body.get("share"), "validity": body["validity"], "as_of_status": as_of_status(body["validity"], as_of),
            "statement_date": body.get("statement_date"), "reporting_exception": body.get("reporting_exception"),
            "relationship_status": body.get("relationship_status"), "basis": body.get("basis"),
            "source": body["source"], "native": body.get("native"), "redacted": bool(view.get("redacted")),
            "observed_at_ms": view["observed_at_ms"],
        }

    def _slot(self, subject: str, kinds: tuple[str, ...], exception_levels: tuple[str, ...], as_of: str | None,
              *, entity_holders_only: bool) -> dict[str, Any]:
        edges, exceptions, excluded, minority, others = [], [], [], [], []
        live = {"valid", "undetermined", "not_evaluated"}
        for view in self.assertions:
            body = view["record"]
            if self.cluster(body["subject_key"]) != subject:
                continue
            edge = self._edge(view, as_of)
            if body["assertion_kind"] == "reporting_exception":
                if body["reporting_exception"]["level"] in exception_levels:
                    (exceptions if edge["as_of_status"] in live else excluded).append(edge)
                continue
            if body["assertion_kind"] not in kinds:
                continue
            if edge["as_of_status"] not in live:
                excluded.append(edge)
            elif entity_holders_only and (body.get("holder") or {}).get("kind") != "entity":
                others.append(edge)
            elif control_basis(edge) == "minority-as-stated":
                minority.append(edge)
            else:
                edges.append(edge)
        result = self._grouped(edges, exceptions, excluded)
        result["other_holdings"] = {
            "minority_as_stated": minority, "person_or_unidentified_holders": others,
            "note": "returned, not hidden: stated interests below a majority are holdings, not parents; person "
                    "holders are listed apart (owner-scoped ones redacted)"}
        return result

    def _grouped(self, edges, exceptions, excluded) -> dict[str, Any]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for edge in edges:
            groups.setdefault(edge["holder"]["entity"] or f"unidentified:{edge['holder'].get('name')}", []).append(edge)
        grouped = [{"holder": holder, "holder_entity": self.describe(holder) if not holder.startswith("unidentified:") else None,
                    "assertions": items, "differences": differences(items)}
                   for holder, items in sorted(groups.items())]
        conflicts = []
        if len(groups) > 1:
            conflicts.append({"holders": sorted(groups), "assertion_ids": sorted(e["record_id"] for e in edges),
                              "reasons": differences(edges) or ["different_holder"],
                              "resolution": "not resolved; each assertion is what its source states"})
        return {"groups": grouped, "reporting_exceptions": exceptions, "conflicts": conflicts,
                "excluded": [{"record_id": e["record_id"], "as_of_status": e["as_of_status"],
                              "assertion_kind": e["assertion_kind"], "provider": e["source"]["provider"]} for e in excluded]}

    def _result(self, query: str, entity: str, as_of: str | None, body: Mapping[str, Any]) -> dict[str, Any]:
        result = {"query": query, "namespace": self.namespace, "entity": self.describe(entity), "as_of": as_of,
                  **body, "pins": {"records": dict(sorted(self.pins.items())), "identity": self.identity},
                  "notice": NOTICE}
        result["result_hash"] = digest({k: v for k, v in result.items() if k != "pins"})
        return result

    # --------------------------------------------------------------- queries

    def direct_parents(self, entity: str, as_of: str | None = None) -> dict[str, Any]:
        subject = self.resolve(entity)
        return self._result("direct_parents", subject, as_of,
                            self._slot(subject, PARENT_SLOT_KINDS, ("direct", "psc", "any"), as_of, entity_holders_only=True))

    def ultimate_parents(self, entity: str, as_of: str | None = None, *, max_depth: int = MAX_DEPTH) -> dict[str, Any]:
        subject = self.resolve(entity)
        stated = self._slot(subject, ("ultimate_parent",), ("ultimate",), as_of, entity_holders_only=True)
        chain = self._chain(subject, as_of, max_depth)
        stated["derived_chain_tops"] = {
            "tops": chain["tops"], "note": "top of the stated direct/control chain; a derived path, not a stated "
                                           "ultimate parent"}
        return self._result("ultimate_parents", subject, as_of, stated)

    def subsidiaries(self, entity: str, as_of: str | None = None) -> dict[str, Any]:
        holder = self.resolve(entity)
        found: dict[str, list[dict[str, Any]]] = {}
        minority: dict[str, list[dict[str, Any]]] = {}
        excluded = []
        for view in self.assertions:
            body = view["record"]
            if body["assertion_kind"] not in PARENT_SLOT_KINDS or not (body.get("holder") or {}).get("key"):
                continue
            if self.cluster(body["holder"]["key"]) != holder:
                continue
            edge = self._edge(view, as_of)
            if edge["as_of_status"] not in {"valid", "undetermined", "not_evaluated"}:
                excluded.append({"record_id": edge["record_id"], "as_of_status": edge["as_of_status"]})
            elif edge["control_basis"] == "minority-as-stated":
                minority.setdefault(edge["subject"], []).append(edge)
            else:
                found.setdefault(edge["subject"], []).append(edge)
        return self._result("subsidiaries", holder, as_of, {
            "subsidiaries": [{"subject": s, "subject_entity": self.describe(s), "assertions": items,
                              "differences": differences(items)} for s, items in sorted(found.items())],
            "minority_holdings_as_stated": [{"subject": s, "assertions": items} for s, items in sorted(minority.items())],
            "excluded": excluded})

    def _chain(self, start: str, as_of: str | None, max_depth: int) -> dict[str, Any]:
        if not 1 <= int(max_depth) <= MAX_DEPTH:
            raise OwnershipError("invalid_request", f"max_depth must be between 1 and {MAX_DEPTH}")
        paths, cycles, truncated, tops = [], [], False, set()

        def walk(node: str, path: list[str], edges: list[dict[str, Any]]) -> None:
            nonlocal truncated
            slot = self._slot(node, PARENT_SLOT_KINDS, ("direct", "psc", "any"), as_of, entity_holders_only=True)
            nexts = [(g["holder"], g["assertions"]) for g in slot["groups"] if not g["holder"].startswith("unidentified:")]
            if not nexts:
                tops.add(node)
                paths.append({"entities": path, "edges": edges, "ends_with": "no stated holder",
                              "reporting_exceptions": [e["record_id"] for e in slot["reporting_exceptions"]]})
                return
            for holder, assertions in nexts:
                hop = {"from": node, "to": holder, "assertion_ids": [a["record_id"] for a in assertions],
                       "kinds": sorted({a["assertion_kind"] for a in assertions}),
                       "providers": sorted({a["source"]["provider"] for a in assertions})}
                if holder in path:
                    cycles.append({"path": path + [holder], "edge": hop})
                    continue
                if len(path) - 1 >= max_depth:
                    truncated = True
                    paths.append({"entities": path, "edges": edges, "ends_with": "depth bound"})
                    continue
                walk(holder, path + [holder], edges + [hop])

        walk(start, [start], [])
        return {"paths": paths, "cycles": cycles, "truncated": truncated, "tops": sorted(tops), "max_depth": max_depth}

    def control_chain(self, entity: str, as_of: str | None = None, *, max_depth: int = MAX_DEPTH) -> dict[str, Any]:
        subject = self.resolve(entity)
        return self._result("control_chain", subject, as_of, self._chain(subject, as_of, max_depth))

    def successor_chain(self, entity: str, *, max_depth: int = MAX_DEPTH) -> dict[str, Any]:
        if not 1 <= int(max_depth) <= MAX_DEPTH:
            raise OwnershipError("invalid_request", f"max_depth must be between 1 and {MAX_DEPTH}")
        start = self.resolve(entity)
        links = []
        for view in self.by_kind.get("corporate_event", []):
            body = view["record"]
            if body["event_type"] in {"succession", "merger", "re_registration"} and body.get("related_entity_key"):
                links.append({"from": self.cluster(body["entity_key"]), "to": self.cluster(body["related_entity_key"]),
                              "record_id": view["record_id"], "revision": view["revision"], "event_type": body["event_type"],
                              "event_date": body.get("event_date"), "date_status": body["date_status"],
                              "source": body["source"], "description": body.get("description")})

        def walk(node: str, direction: str) -> dict[str, Any]:
            chain, seen, cycle, current = [], [node], None, node
            for _ in range(max_depth):
                step = [l for l in links if l["from" if direction == "forward" else "to"] == current]
                if not step:
                    return {"chain": chain, "cycle": cycle, "truncated": False}
                link = step[0]
                chain.append({**link, "alternatives": [s["record_id"] for s in step[1:]]})
                current = link["to" if direction == "forward" else "from"]
                if current in seen:
                    cycle = seen + [current]
                    return {"chain": chain, "cycle": cycle, "truncated": False}
                seen.append(current)
            return {"chain": chain, "cycle": cycle, "truncated": True}

        return self._result("successor_chain", start, None, {
            "successors": walk(start, "forward"), "predecessors": walk(start, "backward"),
            "note": "successors and predecessors are linked by corporate events; entities are never collapsed"})


def query(conn: Any, namespace: str, name: str, entity: str, *, principal_id: str | None, scopes: Iterable[str],
          as_of: str | None = None, max_depth: int = MAX_DEPTH, known_at_ms: int | None = None,
          pins: Mapping[str, int] | None = None, identity: Iterable[str] | None = None) -> dict[str, Any]:
    graph = OwnershipGraph(conn, namespace, principal_id=principal_id, scopes=scopes, known_at_ms=known_at_ms,
                           pins=pins, identity=identity)
    if name == "direct_parents":
        return graph.direct_parents(entity, as_of)
    if name == "ultimate_parents":
        return graph.ultimate_parents(entity, as_of, max_depth=max_depth)
    if name == "subsidiaries":
        return graph.subsidiaries(entity, as_of)
    if name == "control_chain":
        return graph.control_chain(entity, as_of, max_depth=max_depth)
    if name == "successor_chain":
        return graph.successor_chain(entity, max_depth=max_depth)
    raise OwnershipError("invalid_request", "query is direct_parents, ultimate_parents, subsidiaries, control_chain "
                                            "or successor_chain")


def replay(conn: Any, namespace: str, result: Mapping[str, Any], *, principal_id: str | None,
           scopes: Iterable[str]) -> dict[str, Any]:
    """Recompute a result from exactly the record revisions and identity decisions it pinned."""
    entity = result["entity"]["entity"]
    return query(conn, namespace, result["query"], entity, principal_id=principal_id, scopes=scopes,
                 as_of=result["as_of"], max_depth=int(result.get("max_depth") or MAX_DEPTH),
                 pins=result["pins"]["records"], identity=result["pins"]["identity"])
