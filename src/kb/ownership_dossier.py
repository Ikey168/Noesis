"""Identifier-to-ownership-dossier assembly and export (``noesis-ownership-dossier-v1``).

A dossier starts from one explicit identifier (LEI, register number, CIK, or
name plus jurisdiction) and assembles, from the acquired records only:
entities, direct/ultimate parents, subsidiaries, control and successor
chains, officers, filings and one timeline, with conflicts, reporting
exceptions and unknowns visible and the record revisions it used pinned.
A name lookup never picks an entity: it returns candidates for the caller to
choose from. Export saves the dossier as an authored report through the shared
``AuthoredReportStore`` (platform.authored-reports); every sourced statement
cites the ownership record revision it came from.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from src.kb.ownership_graph import NOTICE, OwnershipGraph
from src.kb.ownership_records import DOSSIER_CONTRACT, digest
from src.kb.ownership_store import OwnershipError, OwnershipStore
from src.kb.ownership_timeline import timeline


def _unknowns(graph: OwnershipGraph, keys: set[str]) -> list[dict[str, Any]]:
    result = []
    for view in graph.views:
        body = view["record"]
        anchor = body.get("record_key") if body["kind"] in {"legal_entity", "person"} else (
            body.get("subject_key") or body.get("entity_key"))
        if anchor in keys and body.get("unknowns") and not view.get("redacted"):
            result.append({"record_id": view["record_id"], "kind": body["kind"], "provider": body["source"]["provider"],
                           "unknown": body["unknowns"]})
    return result


def build_dossier(conn: Any, namespace: str, scheme: str, value: str, *, principal_id: str | None,
                  scopes: Iterable[str], as_of: str | None = None, known_at_ms: int | None = None,
                  market: Mapping[str, Any] | None = None, evidence_kind: str = "unspecified",
                  pins: Mapping[str, int] | None = None, identity: Iterable[str] | None = None) -> dict[str, Any]:
    scopes = set(scopes)
    store = OwnershipStore(conn, initialize=False)
    found = store.lookup(namespace, scheme, value, principal_id=principal_id, scopes=scopes)
    if found["status"] == "not_found":
        raise OwnershipError("not_found", "no acquired entity carries this identifier in this namespace")
    graph = OwnershipGraph(conn, namespace, principal_id=principal_id, scopes=scopes, known_at_ms=known_at_ms,
                           pins=pins, identity=identity)
    clusters = sorted({graph.cluster(m["record"]["record_key"]) for m in found["matches"]})
    if found["status"] == "candidates" or len(clusters) > 1:
        return {"contract": DOSSIER_CONTRACT, "status": "needs_selection", "query": {"scheme": scheme, "value": value},
                "candidates": [graph.describe(c) for c in clusters],
                "note": "the identifier matches more than one entity; choose one (or review identity candidates). "
                        "Noesis never picks by name."}
    root = clusters[0]
    keys = set(graph.members(root))
    parents = graph.direct_parents(root, as_of)
    ultimate = graph.ultimate_parents(root, as_of)
    subsidiaries = graph.subsidiaries(root, as_of)
    chain = graph.control_chain(root, as_of)
    successors = graph.successor_chain(root)
    related = {root} | {g["holder"] for g in parents["groups"]} | {g["holder"] for g in ultimate["groups"]} | {
        s["subject"] for s in subsidiaries["subsidiaries"]} | {e for p in chain["paths"] for e in p["entities"]}
    officers, filings = [], {}
    for view in graph.views:
        body = view["record"]
        if body["kind"] == "officer_role" and body["entity_key"] in keys:
            officers.append({"record_id": view["record_id"], "revision": view["revision"], "officer": body["officer"],
                             "role": body["role"], "appointed_on": body.get("appointed_on"),
                             "appointed_status": body["appointed_status"], "resigned_on": body.get("resigned_on"),
                             "company": body["entity_key"], "source": body["source"]})
        elif body["kind"] == "filing_reference" and body["entity_key"] in keys:
            entry = filings.setdefault(body["accession_number"], {"accession_number": body["accession_number"],
                                                                   "form_type": body["form_type"], "statements": []})
            entry["statements"].append({"record_id": view["record_id"], "revision": view["revision"],
                                        "filing_date": body.get("filing_date"), "document_url": body.get("document_url"),
                                        "description": body.get("description"), "parsed": body["parsed"],
                                        "provider": body["source"]["provider"], "record_key": body["record_key"]})
    from src.kb.ownership_identity import OwnershipIdentityService

    identity_service = OwnershipIdentityService(conn, initialize=False)
    candidates = {}
    for key in sorted(keys | {k for c in related for k in graph.members(c)}):
        for candidate in identity_service.candidates(namespace, scopes=scopes, record_key=key):
            candidates[candidate["candidate_id"]] = {k: candidate[k] for k in (
                "candidate_id", "left_key", "right_key", "basis", "confidence", "state", "decision_id")}
    conflicts = [{"slot": "direct_parents", **c} for c in parents["conflicts"]] + [
        {"slot": "ultimate_parents", **c} for c in ultimate["conflicts"]]
    for group in parents["groups"] + ultimate["groups"]:
        if len(group["assertions"]) > 1 and group["differences"]:
            conflicts.append({"slot": "same_edge", "holders": [group["holder"]],
                              "assertion_ids": [a["record_id"] for a in group["assertions"]],
                              "reasons": group["differences"],
                              "resolution": "returned together; not resolved"})
    line = timeline(conn, namespace, root, principal_id=principal_id, scopes=scopes, market=market, graph=graph)
    unknowns = _unknowns(graph, keys)
    if not parents["groups"] and not parents["reporting_exceptions"]:
        unknowns.append({"record_id": None, "kind": "relationship", "provider": None,
                         "unknown": ["no acquired source states a direct parent or a reporting exception"]})
    dossier = {
        "contract": DOSSIER_CONTRACT, "status": "assembled", "namespace": namespace,
        "query": {"scheme": scheme, "value": value}, "as_of": as_of, "known_at_ms": known_at_ms,
        "entities": [graph.describe(c) for c in sorted(related)],
        "relationships": {"direct_parents": parents, "ultimate_parents": ultimate, "subsidiaries": subsidiaries,
                          "control_chain": chain, "successor_chain": successors},
        "officers": sorted(officers, key=lambda o: (o["appointed_on"] or "", o["record_id"])),
        "filings": sorted(filings.values(), key=lambda f: f["accession_number"]),
        "timeline": {"entries": line["entries"], "undated": line["undated"], "note": line["note"]},
        "conflicts": conflicts, "unknowns": unknowns,
        "reporting_exceptions": parents["reporting_exceptions"] + ultimate["reporting_exceptions"],
        "identity": {"root": root, "members": sorted(keys), "candidates": sorted(candidates.values(), key=lambda c: c["candidate_id"])},
        "pins": {"records": dict(sorted(graph.pins.items())), "identity": graph.identity},
        "notice": NOTICE, "evidence": {"kind": evidence_kind,
                                       "runs": sorted({v["run_id"] for v in graph.views})},
    }
    dossier["dossier_hash"] = digest({k: v for k, v in dossier.items() if k not in {"pins", "dossier_hash"}})
    return dossier


def _statement(identifier: str, text: str, record_ids: list[tuple[str, int]], namespace: str) -> dict[str, Any]:
    return {"id": identifier, "text": text, "kind": "sourced", "citations": sorted({r for r, _ in record_ids}),
            "dependencies": [{"kind": "source", "id": r, "revision": str(rev), "namespace": namespace, "locator": {}}
                             for r, rev in sorted(set(record_ids))]}


def export_dossier(conn: Any, dossier: Mapping[str, Any], request_key: str, *, principal_id: str,
                   scopes: Iterable[str]) -> dict[str, Any]:
    """Save an assembled dossier as an authored report with per-statement citations."""
    from src.kb.authored_reports import AuthoredReportStore

    if dossier.get("status") != "assembled":
        raise OwnershipError("invalid_request", "only an assembled dossier can be exported")
    namespace = dossier["namespace"]
    bibliography: dict[str, dict[str, str]] = {}
    store = OwnershipStore(conn, initialize=False)

    def cite(record_id: str, revision: int) -> tuple[str, int]:
        if record_id not in bibliography:
            view = store.get(namespace, record_id, principal_id=principal_id, scopes=scopes, revision=revision)
            source = view["record"]["source"]
            bibliography[record_id] = {"id": record_id, "text": f"{source['provider']} {source.get('provider_record_id')} "
                                                                f"({view['record']['kind']}, revision {revision})"
                                                                + (f" {source['url']}" if source.get("url") else "")}
        return record_id, revision

    sections = []
    entity_statements = []
    for index, entity in enumerate(dossier["entities"]):
        for name in entity["names"]:
            if name["redacted"]:
                continue
            entity_statements.append(_statement(f"entity:{index}:{name['record_id']}",
                                                f"{name['provider']} records {name['name']} ({name['record_key']}).",
                                                [cite(name["record_id"], name["revision"])], namespace))
    sections.append({"id": "entities", "title": "Entities", "assertions": entity_statements or [
        {"id": "entities:none", "text": "No entity names are visible to this principal.", "kind": "commentary",
         "dependencies": [], "citations": []}]})
    rel = []
    for slot in ("direct_parents", "ultimate_parents"):
        for group in dossier["relationships"][slot]["groups"]:
            for edge in group["assertions"]:
                share = edge.get("share") or {}
                figure = share.get("exact") and f"{share['exact']}%" or (
                    share.get("band") and f"{share['band'].get('min')}-{share['band'].get('max')}% band") or "no percentage stated"
                rel.append(_statement(
                    f"{slot}:{edge['record_id']}",
                    f"{edge['source']['provider']} states {edge['assertion_kind']} of {edge['subject']} by "
                    f"{(edge['holder'] or {}).get('name') or group['holder']} ({figure}); validity "
                    f"{edge['validity']['from'] or 'unknown'} to {edge['validity']['to'] or edge['validity']['to_status']}; "
                    f"as-of status {edge['as_of_status']}.", [cite(edge["record_id"], edge["revision"])], namespace))
        for edge in dossier["relationships"][slot]["reporting_exceptions"]:
            rel.append(_statement(f"{slot}:exception:{edge['record_id']}",
                                  f"{edge['source']['provider']} states a reporting exception "
                                  f"{edge['reporting_exception']['category']} ({edge['reporting_exception']['level']}).",
                                  [cite(edge["record_id"], edge["revision"])], namespace))
    sections.append({"id": "relationships", "title": "Relationships", "assertions": rel or [
        {"id": "relationships:none", "text": "No acquired source states a parent or a reporting exception.",
         "kind": "commentary", "dependencies": [], "citations": []}]})
    sections.append({"id": "officers", "title": "Officers", "assertions": [
        _statement(f"officer:{o['record_id']}", f"{o['officer']['name']}, {o['role']}, appointed "
                   f"{o['appointed_on'] or 'unknown'} ({o['appointed_status']}), resigned {o['resigned_on'] or 'not stated'}.",
                   [cite(o["record_id"], o["revision"])], namespace) for o in dossier["officers"]] or [
        {"id": "officers:none", "text": "No officers acquired.", "kind": "commentary", "dependencies": [], "citations": []}]})
    sections.append({"id": "filings", "title": "Filings", "assertions": [
        _statement(f"filing:{f['accession_number']}", f"{f['form_type']} {f['accession_number']} filed "
                   f"{next((s['filing_date'] for s in f['statements'] if s['filing_date']), 'on an unknown date')}"
                   + ("; ownership figures parsed" if any(s["parsed"] for s in f["statements"]) else "; reference only") + ".",
                   [cite(s["record_id"], s["revision"]) for s in f["statements"]], namespace)
        for f in dossier["filings"]] or [
        {"id": "filings:none", "text": "No filings acquired.", "kind": "commentary", "dependencies": [], "citations": []}]})
    conflict_text = [{"id": f"conflict:{i}", "kind": "commentary", "dependencies": [], "citations": [],
                      "text": f"{c['slot']}: {', '.join(c['holders'])} — {', '.join(c['reasons'])}; not resolved."}
                     for i, c in enumerate(dossier["conflicts"])]
    sections.append({"id": "conflicts", "title": "Conflicts and unknowns", "assertions": conflict_text + [
        {"id": f"unknown:{i}", "kind": "commentary", "dependencies": [], "citations": [],
         "text": f"Unknown in {u['kind']} ({u['provider'] or 'no source'}): {', '.join(u['unknown'])}."}
        for i, u in enumerate(dossier["unknowns"])] or [
        {"id": "conflicts:none", "kind": "commentary", "dependencies": [], "citations": [], "text": "None recorded."}]})
    content = {"title": f"Ownership dossier: {dossier['query']['scheme']} {dossier['query']['value']}",
               "sections": sections,
               "snapshot": {"id": f"ownership-dossier:{dossier['dossier_hash'][:32]}",
                            "generations": {namespace: len(dossier["pins"]["records"])}},
               "bibliography": sorted(bibliography.values(), key=lambda b: b["id"]),
               "limitations": [NOTICE, f"Evidence kind: {dossier['evidence']['kind']}.",
                               "Coverage is bounded to the explicitly selected sources and identifiers."]}
    report = AuthoredReportStore(conn).create(namespace, "ownership-dossier:" + request_key, content,
                                              principal_id=principal_id, scopes=set(scopes))
    return {"report_id": report["report_id"], "revision": report["revision"], "dossier_hash": dossier["dossier_hash"],
            "citations": len(bibliography)}
