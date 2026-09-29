"""One cited corporate-event and filing timeline per entity, with as-of reconstruction (O10).

Entries come from the ownership records of every source record grouped into
the entity (registrations, officer appointments and resignations, ownership
assertion starts and ends, corporate events, filings), from existing market
corporate actions (:class:`src.domains.market.actions.MarketCorporateActionStore`,
reused read-only) and from existing temporal assertions
(``kb_temporal_assertions``) that name the entity's canonical id. Each entry
carries its source, its event time as stated and the record time at which
Noesis learned it. An entry without a stated date goes to ``undated``; no
date is interpolated.

:func:`state_as_of` reconstructs the registration, officers and ownership at
a date from the record revisions known by a record time.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from src.kb.ownership_graph import OwnershipGraph, as_of_status
from src.kb.ownership_records import digest
from src.kb.ownership_store import canonical_entity_id


def _entry(view: Mapping[str, Any], kind: str, summary: str, date: str | None, status: str) -> dict[str, Any]:
    body = view["record"]
    return {"kind": kind, "summary": summary, "event_time": date, "date_status": status if date else "unknown",
            "record_time_ms": view["observed_at_ms"], "record_id": view["record_id"], "revision": view["revision"],
            "source": {k: body["source"].get(k) for k in ("provider", "provider_record_id", "url", "locator",
                                                             "statement_id", "publisher")}}


def _record_entries(graph: OwnershipGraph, keys: set[str]) -> list[dict[str, Any]]:
    entries = []
    for view in graph.views:
        body = view["record"]
        kind = body["kind"]
        if view.get("redacted"):
            continue
        if kind == "registration" and body["entity_key"] in keys:
            entries.append(_entry(view, "registration", f"Registered with {body['register']} as {body['number']}",
                                  body.get("registered_on"), "stated"))
            if body.get("dissolved_on"):
                entries.append(_entry(view, "registration", f"Dissolved ({body['register']})", body["dissolved_on"], "stated"))
        elif kind == "officer_role" and body["entity_key"] in keys:
            entries.append(_entry(view, "officer", f"{body['officer']['name']} appointed {body['role']}"
                                  + (" (appointed before this date)" if body["appointed_status"] == "before" else ""),
                                  body.get("appointed_on"), body["appointed_status"]))
            if body.get("resigned_on"):
                entries.append(_entry(view, "officer", f"{body['officer']['name']} resigned as {body['role']}",
                                      body["resigned_on"], "stated"))
        elif kind == "ownership_assertion" and body["subject_key"] in keys:
            holder = body.get("holder") or {}
            named = next((v["record"].get("name") for v in graph.entities.get(holder.get("key") or "", [])
                          if not v.get("redacted")), None)
            what = (f"reporting exception {body['reporting_exception']['category']}"
                    if body["assertion_kind"] == "reporting_exception"
                    else f"{body['assertion_kind']} by {holder.get('name') or named or holder.get('key') or 'unidentified holder'}")
            validity = body["validity"]
            entries.append(_entry(view, "ownership", f"Stated from: {what}", validity.get("from"), "stated"))
            if validity["to_status"] == "stated":
                entries.append(_entry(view, "ownership", f"Stated ended: {what}", validity["to"], "stated"))
        elif kind == "corporate_event" and (body["entity_key"] in keys or body.get("related_entity_key") in keys):
            entries.append(_entry(view, "event", f"{body['event_type']}: {body.get('description') or ''}".strip(": "),
                                  body.get("event_date"), body["date_status"]))
        elif kind == "filing_reference" and body["entity_key"] in keys:
            entries.append(_entry(view, "filing", f"{body['form_type']} filed ({body['accession_number']})"
                                  + ("" if body["parsed"] else ", reference only"), body.get("filing_date"), "stated"))
    return entries


def _market_entries(conn: Any, market: Mapping[str, Any]) -> list[dict[str, Any]]:
    from src.domains.market.actions import MarketCorporateActionStore

    store = MarketCorporateActionStore(conn, initialize=False)
    entries = []
    for security_id in market.get("security_ids") or []:
        for action in store.get_actions(market["namespace"], security_id, acquired_by_ms=int(market["acquired_by_ms"]),
                                        publicly_available_by_ms=int(market.get("publicly_available_by_ms") or market["acquired_by_ms"]),
                                        principal_id=market["principal_id"], scopes=set(market["scopes"])):
            date = action.get("effective_date") or action.get("ex_date")
            entries.append({"kind": "market_corporate_action",
                            "summary": f"{action['action_type']} ({action['status']}) for {security_id}",
                            "event_time": date, "date_status": "stated" if date else "unknown",
                            "record_time_ms": action.get("recorded_at_ms"), "record_id": action["action_id"],
                            "revision": action.get("revision"),
                            "source": {"provider": action.get("provider"), "provider_record_id": action.get("provider_record_id"),
                                       "store": "src.domains.market.actions", "revision_id": action.get("revision_id")}})
    return entries


def _temporal_entries(conn: Any, canonical_ids: set[str], known_at_ms: int | None) -> list[dict[str, Any]]:
    if not conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name='kb_temporal_assertions'").fetchone():
        return []
    rows = conn.execute("SELECT temporal_id, domain, assertion_kind, assertion_id, valid_from_ms, valid_time_precision, "
                        "observed_at_ms, payload_json, source_document_id, retracted_at_ms FROM kb_temporal_assertions "
                        "WHERE visibility='public' ORDER BY temporal_id").fetchall()
    entries = []
    for row in rows:
        payload = json.loads(row[7] or "{}")
        if not ({payload.get("canonical_entity_id"), payload.get("entity_id")} & canonical_ids):
            continue
        if known_at_ms is not None and int(row[6]) > known_at_ms or row[9] is not None:
            continue
        date = None
        if row[4] is not None and row[5] != "unknown":
            date = datetime.fromtimestamp(int(row[4]) / 1000, UTC).date().isoformat()
        entries.append({"kind": "temporal_assertion", "summary": str(payload.get("summary") or payload.get("label") or row[3]),
                        "event_time": date, "date_status": "stated" if date else "unknown", "record_time_ms": int(row[6]),
                        "record_id": row[0], "revision": None,
                        "source": {"provider": f"temporal:{row[1]}", "provider_record_id": row[3],
                                   "store": "src.kb.temporal", "source_document_id": row[8]}})
    return entries


def timeline(conn: Any, namespace: str, entity: str, *, principal_id: str | None, scopes: Iterable[str],
             known_at_ms: int | None = None, pins: Mapping[str, int] | None = None,
             identity: Iterable[str] | None = None, market: Mapping[str, Any] | None = None,
             graph: OwnershipGraph | None = None) -> dict[str, Any]:
    graph = graph or OwnershipGraph(conn, namespace, principal_id=principal_id, scopes=scopes,
                                    known_at_ms=known_at_ms, pins=pins, identity=identity)
    cluster = graph.resolve(entity)
    keys = set(graph.members(cluster))
    entries = _record_entries(graph, keys)
    if market:
        entries.extend(_market_entries(conn, market))
    entries.extend(_temporal_entries(conn, {canonical_entity_id(k) for k in keys}, known_at_ms))
    dated = sorted((e for e in entries if e["event_time"]),
                   key=lambda e: (e["event_time"], e["record_time_ms"] or 0, str(e["record_id"])))
    undated = sorted((e for e in entries if not e["event_time"]), key=lambda e: (e["kind"], str(e["record_id"])))
    result = {"entity": graph.describe(cluster), "entries": dated, "undated": undated,
              "note": "event time as stated by each source; record time is when Noesis recorded it. Undated "
                      "entries are unknown, never interpolated.",
              "pins": {"records": dict(sorted(graph.pins.items())), "identity": graph.identity}}
    result["timeline_hash"] = digest({k: v for k, v in result.items() if k != "pins"})
    return result


def state_as_of(conn: Any, namespace: str, entity: str, date: str, *, principal_id: str | None,
                scopes: Iterable[str], known_at_ms: int | None = None, pins: Mapping[str, int] | None = None,
                identity: Iterable[str] | None = None) -> dict[str, Any]:
    """Registration, officers and ownership in force at ``date`` from revisions known by ``known_at_ms``."""
    graph = OwnershipGraph(conn, namespace, principal_id=principal_id, scopes=scopes, known_at_ms=known_at_ms,
                           pins=pins, identity=identity)
    cluster = graph.resolve(entity)
    keys = set(graph.members(cluster))
    registrations, officers, unknown_tenure, names = [], [], [], []
    for view in graph.views:
        body = view["record"]
        cite = {"record_id": view["record_id"], "revision": view["revision"], "provider": body["source"]["provider"]}
        if body["kind"] == "registration" and body["entity_key"] in keys:
            status = as_of_status({"from": body.get("registered_on"), "to": body.get("dissolved_on"),
                                   "from_status": "stated" if body.get("registered_on") else "unknown",
                                   "to_status": "stated" if body.get("dissolved_on") else "open"}, date)
            if status in {"valid", "undetermined"}:
                registrations.append({**cite, "register": body["register"], "number": body["number"],
                                      "status_as_recorded": body.get("status"), "as_of_status": status})
        elif body["kind"] == "officer_role" and body["entity_key"] in keys:
            if body["appointed_status"] == "unknown":
                unknown_tenure.append({**cite, "officer": body["officer"]["name"], "role": body["role"],
                                       "why": "appointment date unknown"})
                continue
            status = as_of_status({"from": body["appointed_on"], "to": body.get("resigned_on"), "from_status": "stated",
                                   "to_status": "stated" if body.get("resigned_on") else "open"}, date)
            if status == "valid":
                officers.append({**cite, "officer": body["officer"]["name"], "role": body["role"],
                                 "appointed_on": body["appointed_on"], "appointed_status": body["appointed_status"]})
        elif body["kind"] in {"legal_entity"} and body["record_key"] in keys:
            names.append({**cite, "name": body["name"], "other_names": body.get("other_names") or []})
    return {"entity": graph.describe(cluster), "date": date, "known_at_ms": known_at_ms,
            "registrations": registrations, "officers": officers, "officers_with_unknown_tenure": unknown_tenure,
            "names": names, "direct_parents": graph.direct_parents(cluster, date),
            "subsidiaries": graph.subsidiaries(cluster, date),
            "note": "reconstructed from the record revisions known at the record time; unknown dates stay unknown",
            "pins": {"records": dict(sorted(graph.pins.items())), "identity": graph.identity}}
