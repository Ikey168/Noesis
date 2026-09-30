"""Capacity indicators for a place as of a date, with definitions, notes, breaks and vintages (#2215, HS08).

Given a Geospatial place (or a published code), :func:`capacity_as_of` returns the beds, workforce and expenditure
indicators of every source per domain, side by side: each series with its unit, the definition revision in force on
every value, the source's flags verbatim, its citations (source revision: release, URL, native revision, publication
and retrieval time), its breaks inline (definition changes and publisher-flagged breaks), the active comparability
notes and mappings of its indicator and - where the chosen vintage revised an earlier one - the vintage comparison of
:mod:`src.kb.surveillance_vintages` citing both vintages.

The as-of time selects, per series, the vintage released at or before the end of that day (the release clock), so an
answer can be replayed as it would have been given then. Sources are never blended, averaged or preferred; a missing
value is shown as unknown; places are never ranked or ordered by an indicator (the answer is for one place, and its
series are ordered by source and code only). A place with no capacity data is reported as having none on record.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from src.kb.health_capacity import DOMAINS, NEVER_SENTENCE, HealthCapacityError, as_of_ms
from src.kb.health_capacity_comparability import HealthCapacityComparability
from src.kb.surveillance import READ_SCOPE, _day, authorize

ANSWER_CONTRACT = "noesis-health-capacity-answer-v1"
NONE_ON_RECORD = "no series on record states a code of this place"


def _resolve(tool: HealthCapacityComparability, namespace: str, place_id: str | None, place_code: str | None):
    """(place view, the used code pairs, excluded resolutions) or a status explaining why nothing is resolved."""
    if bool(place_id) == bool(place_code):
        raise HealthCapacityError("invalid_request", "name one place_id or one place_code")
    current = tool.resolutions(namespace, scopes={"operator"})
    if place_code:
        found = tool.places_for_code(namespace, place_code)
        aggregates = [r for r in found if r["aggregate"]]
        if aggregates:
            # An aggregate is answered as itself (labelled), never as a country.
            return ({"kind": "aggregate", "code": place_code,
                     "systems": sorted({r["geography_system"] for r in aggregates})},
                    [(r["geography_system"], r["geography_code"]) for r in aggregates], [], None)
        used = [r for r in found if r["used"]]
        if not used:
            if not found and not tool.store.find_series(namespace, geography_code=place_code, limit=1):
                return {"kind": "unresolved", "code": place_code}, [], [], NONE_ON_RECORD
            reason = found[0]["reason"] if found else "the code is not resolved; run resolve_health_capacity_places"
            if found and found[0]["state"] == "matched":
                reason = "the code's resolution was rejected"
            return {"kind": "unresolved", "code": place_code}, [], found, reason
        place_id = used[0]["place_id"]
    matched = [r for r in current if r["place_id"] == place_id]
    used = [r for r in matched if r["used"]]
    place = {"kind": "place", "place_id": place_id,
             "name": used[0]["place_name"] if used else None,
             "codes": [{"system": r["geography_system"], "code": r["geography_code"],
                        "resolution_id": r["resolution_id"], "review_state": r["review_state"]} for r in used]}
    reason = None if used else NONE_ON_RECORD if not matched else "every code of this place was rejected"
    return place, [(r["geography_system"], r["geography_code"]) for r in used], \
        [r for r in matched if not r["used"]], reason


def _entry(store, tool, conn, namespace, indicator, as_of, scopes) -> dict[str, Any]:
    from src.kb.surveillance_vintages import compare

    observations = store.observations(namespace, indicator["series_id"], scopes=scopes, as_of=as_of)
    ref = {"provider": indicator["provider"], "source_code": indicator["source_code"]}
    cutoff = as_of_ms(as_of)
    vintage = observations.get("vintage")
    vintages = store.series_store.vintage_rows(namespace, indicator["series_id"])
    known = {v["vintage_id"] for v in vintages if cutoff is None or v["release_at_ms"] <= cutoff}
    breaks = [b for b in indicator["breaks"] if b["first_vintage_id"] in known]
    definitions = [r for r in indicator["definition_history"]
                   if cutoff is None or r["source_revision"]["release_id"] in {
                       v["release_id"] for v in vintages if v["vintage_id"] in known}]
    differences = None
    if vintage and vintage.get("revision_of"):
        differences = compare(conn, namespace, indicator["series_id"], scopes={"operator"},
                              left=vintage["revision_of"], right=vintage["vintage_id"])
        differences = {k: differences[k] for k in ("status", "left", "right", "changes", "summary", "notice")}
    return {
        "series_id": indicator["series_id"],
        "provider": indicator["provider"],
        "source_code": indicator["source_code"],
        "label": indicator["indicator"].get("label"),
        "indicator_notes": {k: indicator["indicator"][k] for k in ("source_note", "country_note", "version")
                            if indicator["indicator"].get(k)},
        "place": indicator["place"],
        "aggregate": indicator["aggregate"],
        "unit": indicator["unit"]["label"],
        "interval": indicator["interval"],
        "kind": indicator["kind"],
        "status": observations["status"],
        "values": observations["values"],
        "vintage": vintage,
        "later_vintages": observations["later_vintages"],
        "vintage_differences": differences,
        "citation": {"source_revision": observations.get("source_revision"), "identifiers": indicator["citations"]},
        "definitions": definitions,
        "breaks": breaks,
        "comparability_notes": tool.notes(namespace, scopes={"operator"}, ref=ref, active_only=True),
        "mappings": tool.mappings(namespace, scopes={"operator"}, ref=ref, active_only=True),
    }


def capacity_as_of(
    conn: Any,
    namespace: str,
    *,
    scopes: Iterable[str],
    place_id: str | None = None,
    place_code: str | None = None,
    as_of: str | None = None,
    domains: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Beds, workforce and expenditure indicators for one place as published at ``as_of``, per source."""
    scopes = set(scopes)
    authorize(namespace, scopes, READ_SCOPE)
    wanted = list(domains) if domains else list(DOMAINS)
    unknown = [d for d in wanted if d not in DOMAINS]
    if unknown:
        raise HealthCapacityError("invalid_domain", f"domains are among {DOMAINS}")
    tool = HealthCapacityComparability(conn, initialize=False)
    store = tool.capacity
    day = None if as_of is None else _day(as_of)
    place, pairs, excluded, reason = _resolve(tool, namespace, place_id, place_code)
    base = {
        "contract": ANSWER_CONTRACT,
        "namespace": namespace,
        "request": {"place_id": place_id, "place_code": place_code, "as_of": day, "domains": wanted},
        "place": place,
        "excluded_resolutions": [{k: r[k] for k in ("resolution_id", "geography_system", "geography_code", "state",
                                                    "review_state")} for r in excluded],
        "boundary": NEVER_SENTENCE,
        "ordering": "by domain, source and code only; places are never ranked or ordered by an indicator",
    }
    if not pairs:
        status = "none_on_record" if reason == NONE_ON_RECORD else "place_unresolved"
        return {**base, "status": status, "reason": reason, "domains": {},
                "note": "no capacity indicator is on record for this place" if status == "none_on_record"
                else "the place is not resolved to any published code; nothing is guessed"}
    entries: dict[str, dict[str, list[dict[str, Any]]]] = {d: {} for d in wanted}
    for system, code in pairs:
        for indicator in store.indicators(namespace, scopes={"operator"}, geography_system=system,
                                          geography_code=code):
            if indicator["domain"] not in entries:
                continue
            entry = _entry(store, tool, conn, namespace, indicator, day, scopes)
            entries[indicator["domain"]].setdefault(indicator["provider"], []).append(entry)
    for providers in entries.values():
        for items in providers.values():
            items.sort(key=lambda e: (e["source_code"], e["place"]["system"], e["place"]["code"], e["series_id"]))
    domains_out = {d: dict(sorted(p.items())) for d, p in entries.items()}
    held = [d for d, p in domains_out.items() if p]
    if not held:
        return {**base, "status": "none_on_record", "domains": domains_out,
                "note": "no capacity indicator is on record for this place"}
    return {
        **base,
        "status": "answered",
        "domains": domains_out,
        "domains_without_data": [d for d in wanted if d not in held],
        "note": "each source's series as published at the as-of time, side by side and never blended; missing "
        "values are unknown; definition breaks and comparability notes are inline",
    }


__all__ = ["ANSWER_CONTRACT", "capacity_as_of"]
