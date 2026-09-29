"""Place-to-hazard answers: events affecting a place as of a date, and alerts in force at a time (NH10-NH11).

:func:`events_affecting` returns the hazard events whose *published* geometry
relates to a place, point/radius or bounding box within a window, each with the
revision in force at the as-of date (and which revision and clock that was),
its full parameter revision history, and accepted cross-source correspondents
shown side by side. :func:`alerts_in_force` returns the alerts and advisories
whose *published* validity covered a place at a time, quoting level, wording,
advisory or episode number and the supersession chain.

Answers distinguish **none on record** (a covered source has no record — never
"safe") from **source not covered** (outside every declared bounded coverage)
and **not acquired** (covered but never successfully acquired). Areas and
windows are bounded. Nothing here ranks risk, estimates exposure or damage, or
gives safety, evacuation or protective-action advice.
"""

from __future__ import annotations

from src.ingestion.hazard_sources import BOUNDED_COVERAGE
from src.kb import hazards_records as hr
from src.kb.hazards_identity import HazardIdentity, side_by_side
from src.kb.hazards_records import READ_SCOPE
from src.kb.hazards_store import GEO_SCOPES, HazardStore, HazardStoreError, authorize, cite, iso, ms, valid_at

EVENTS_CONTRACT = "noesis-hazard-events-answer-v1"
ALERTS_CONTRACT = "noesis-hazard-alerts-answer-v1"
MAX_WINDOW_DAYS = 366
MAX_BBOX_DEGREES = 30.0
MAX_RADIUS_M = 500_000.0
MAX_RESULTS = 200
NONE_ON_RECORD = "none on record: covered sources published no matching record; this is not a statement of safety"
NOT_COVERED = "source not covered: the place is outside every declared bounded coverage for these hazard types"
ALERT_TYPES = {"alert": ("gdacs", "glofas"), "advisory": ("nhc",)}


class HazardQueryError(HazardStoreError):
    pass


def _area(store, namespace, *, place_id, point, radius_m, bbox, as_of_ms):
    """The query area from the geospatial owner (place boundary in force at as-of), a point+radius or a bbox."""

    given = [place_id is not None, point is not None, bbox is not None]
    if sum(given) != 1:
        raise HazardQueryError("invalid_area", "give exactly one of place_id, point (+ radius_m) or bbox")
    if place_id is not None:
        place = store.geo.place(namespace, place_id, scopes={"knowledge:geospatial:read"})
        if place is None:
            raise HazardQueryError("place_not_found", "the place is not visible in this namespace")
        geometries = store.geo.geometries(namespace, place_id, scopes={"knowledge:geospatial:read"}, as_of_ms=as_of_ms)
        if not geometries:
            raise HazardQueryError("no_boundary", "the place has no geometry in force at the as-of date")
        geometry = geometries[0]
        return {"kind": "place", "place": place, "geometry": geometry, "names": [n["value"] for n in place["names"]]
                + [place["canonical_name"]], "iso3": (place.get("source_ids") or {}).get("iso3"),
                "boundary": {"geometry_id": geometry["geometry_id"], "generation": geometry["generation"],
                             "valid_from_ms": geometry["valid_from_ms"], "valid_to_ms": geometry["valid_to_ms"],
                             "content_hash": geometry["content_hash"]}}
    if point is not None:
        radius = float(radius_m or 0)
        if not 0 < radius <= MAX_RADIUS_M:
            raise HazardQueryError("unbounded_area", f"radius_m must be in (0, {int(MAX_RADIUS_M)}]")
        return {"kind": "point", "point": [float(point[0]), float(point[1])], "radius_m": radius, "names": [],
                "iso3": None}
    west, south, east, north = (float(v) for v in bbox)
    if not (west < east and south < north) or east - west > MAX_BBOX_DEGREES or north - south > MAX_BBOX_DEGREES:
        raise HazardQueryError("unbounded_area", f"bbox is west<east, south<north and at most {MAX_BBOX_DEGREES} degrees")
    return {"kind": "bbox", "bbox": [west, south, east, north], "names": [], "iso3": None}


def _window(start, end):
    start_ms, end_ms = ms(start), ms(end)
    if start_ms is None or end_ms is None or end_ms < start_ms:
        raise HazardQueryError("invalid_window", "start and end are ISO dates/instants with start <= end")
    if end_ms - start_ms > MAX_WINDOW_DAYS * 86_400_000:
        raise HazardQueryError("unbounded_window", f"windows are at most {MAX_WINDOW_DAYS} days")
    return start_ms, end_ms


def _representative(area):
    from src.kb.geospatial import _points

    if area["kind"] == "point":
        return area["point"]
    if area["kind"] == "bbox":
        west, south, east, north = area["bbox"]
        return [(west + east) / 2, (south + north) / 2]
    points = _points(area["geometry"]["geometry"])
    lons, lats = [p[0] for p in points], [p[1] for p in points]
    return [(min(lons) + max(lons)) / 2, (min(lats) + max(lats)) / 2]


def _covered(provider, hazard_types, point):
    declared = BOUNDED_COVERAGE[provider]
    if hazard_types and not set(hazard_types) & set(declared["hazard_types"]):
        return False
    lon, lat = point
    return any(w <= lon <= e and s <= lat <= n for w, s, e, n in declared["bboxes"])


def coverage(store, namespace, providers, hazard_types, point):
    result = {}
    for provider in providers:
        if not _covered(provider, hazard_types, point):
            result[provider] = "source not covered"
        elif not store.provider_state(namespace, provider)["acquired"]:
            result[provider] = "not acquired"
        else:
            result[provider] = "covered"
    return result


def _match(identity, namespace, area, revision, principal_id):
    """How the revision's published geometry relates to the area (with a spatial receipt when one is computed)."""

    from src.kb.geospatial import _points

    geometry = revision["content"].get("geometry")
    if not geometry:
        return None
    if area["kind"] == "bbox":
        west, south, east, north = area["bbox"]
        inside = [p for p in _points(geometry) if west <= p[0] <= east and south <= p[1] <= north]
        return {"relation": "published geometry has a vertex inside the bbox", "spatial_receipt_id": None} if inside else None
    if area["kind"] == "point":
        relation = identity.store.geo.relation(namespace, "proximity", revision["geometry_id"], area["point"],
                                               scopes=GEO_SCOPES, principal_id=principal_id,
                                               tolerance_m=area["radius_m"])
        if not relation["result"]["within_tolerance"]:
            return None
        return {"relation": f"published geometry within {int(area['radius_m'])} m of the point (search radius, not an "
                            f"impact footprint)", "distance_m": relation["result"]["distance_m"],
                "spatial_receipt_id": relation["receipt_id"]}
    relation, receipt = identity._relate(namespace, revision["geometry_id"], geometry, area["geometry"], principal_id)
    if relation is None:
        return None
    return {"relation": f"published geometry {relation} the place boundary" if relation == "intersects" else
            "place boundary contains the published geometry", "spatial_receipt_id": receipt,
            "boundary": area["boundary"]}


def events_affecting(conn, namespace, *, start, end, scopes, principal_id, place_id=None, point=None, radius_m=None,
                     bbox=None, as_of=None, basis="publisher", hazard_types=None, now=None):
    """Hazard events whose published geometry relates to the area in the window, as known at ``as_of``."""

    authorize(namespace, scopes, READ_SCOPE)
    store = HazardStore(conn, initialize=False, now=now)
    identity = HazardIdentity(conn, initialize=False, now=now)
    as_of_ms = ms(as_of) if as_of else None
    start_ms, end_ms = _window(start, end)
    area = _area(store, namespace, place_id=place_id, point=point, radius_m=radius_m, bbox=bbox, as_of_ms=as_of_ms)
    wanted = set(hazard_types or hr.HAZARD_TYPES)
    items, not_yet, without_geometry = [], 0, 0
    for record_id in store.record_ids(namespace, record_type="hazard_event"):
        header = store._header(record_id)
        if header["hazard_type"] not in wanted:
            continue
        revision = store.revision_at(record_id, as_of_ms=as_of_ms, basis=basis)
        if revision is None:
            not_yet += 1
            continue
        content = revision["content"]
        begins = ms(content.get("event_time")) or revision["published_at_ms"]
        ends = ms(content.get("event_end")) or begins
        if ends < start_ms or begins > end_ms:
            continue
        if not content.get("geometry"):
            without_geometry += 1
            continue
        matched = _match(identity, namespace, area, revision, principal_id)
        if matched is None:
            continue
        history = store.revisions(namespace, record_id, scopes=scopes)["revisions"]
        known = [h for h in history if as_of_ms is None or (h["published_at"] and ms(h["published_at"]) <= as_of_ms)] \
            if basis == "publisher" else history
        items.append({
            "record_id": record_id, "provider": header["provider"], "native_id": header["native_id"],
            "hazard_type": header["hazard_type"], "issuing_body": content["issuing_body"], "title": content["title"],
            "status": content.get("status"), "merged_into": content.get("merged_into"),
            "event_time": content.get("event_time"), "parameters": content.get("parameters"),
            "revision_used": {"revision_id": revision["revision_id"], "revision_key": revision["revision_key"],
                              "published_at": iso(revision["published_at_ms"]), "published_basis": revision["published_basis"],
                              "acquired_at": iso(revision["observed_at_ms"]), "as_of_basis": basis,
                              "later_revisions_exist": len(history) > len(known)},
            "revision_history": [{"revision_id": h["revision_id"], "revision_key": h["revision_key"],
                                  "published_at": h["published_at"], "acquired_at": h["observed_at"],
                                  "source_url": h["content"]["source_url"], "changes": h["changes"],
                                  "known_at_as_of": h in known} for h in history],
            "matched_by": matched,
            "correspondents": side_by_side(store, namespace, record_id, scopes=scopes, as_of_ms=as_of_ms, basis=basis),
            "citation": cite(header, revision),
        })
    items.sort(key=lambda i: (i["event_time"] or "", i["provider"], i["native_id"]))
    truncated = len(items) > MAX_RESULTS
    point_ = _representative(area)
    covered = coverage(store, namespace, [p for p in BOUNDED_COVERAGE if {"hazard_event"} &
                                          hr.PROVIDER_SCOPE[p][0]], sorted(wanted), point_)
    if items:
        answer = "events on record"
    elif "covered" in covered.values():
        answer = NONE_ON_RECORD
    elif "not acquired" in covered.values():
        answer = "not acquired: covering sources have never been acquired successfully"
    else:
        answer = NOT_COVERED
    return {"contract": EVENTS_CONTRACT, "namespace": namespace, "area": {k: v for k, v in area.items()
                                                                        if k not in {"geometry", "place"}},
            "window": [iso(start_ms), iso(end_ms)], "as_of": iso(as_of_ms) if as_of_ms else "latest",
            "as_of_basis": basis, "events": items[:MAX_RESULTS], "truncated": truncated, "answer": answer,
            "coverage": covered, "not_yet_published_at_as_of": not_yet, "events_without_published_geometry": without_geometry,
            "exclusions": list(hr.NEVER)}


def _place_named(area_text, names):
    text = area_text.casefold()
    return [n for n in names if n and n.casefold() in text]


def alerts_in_force(conn, namespace, *, at, scopes, principal_id, place_id=None, point=None, radius_m=None,
                    country=None, as_of=None, now=None):
    """Alerts and advisories whose published validity covered the place at ``at``, quoted as issued.

    Products issued up to ``at`` are considered, each in its revision known at ``as_of`` (latest by
    default); a product issued later by the same body for the same event marks where the earlier one
    was superseded. With ``as_of=at`` the answer is what was on record at that moment.
    """

    authorize(namespace, scopes, READ_SCOPE)
    store = HazardStore(conn, initialize=False, now=now)
    at_ms = ms(at)
    if at_ms is None:
        raise HazardQueryError("invalid_time", "at is an ISO instant")
    as_of_ms = ms(as_of) if as_of else None
    area = _area(store, namespace, place_id=place_id, point=point, radius_m=radius_m, bbox=None, as_of_ms=at_ms)
    iso3 = country or area.get("iso3")
    rep = _representative(area)
    items = []
    groups: dict[tuple[str, str], list] = {}
    for record_type in ("alert", "advisory"):
        for record_id in store.record_ids(namespace, record_type=record_type):
            revision = store.revision_at(record_id, as_of_ms=as_of_ms)
            if revision is None:
                continue  # not yet on record at the as-of date
            content = revision["content"]
            key = (store._header(record_id)["provider"], content.get("event_native_id") or content["native_id"])
            groups.setdefault(key, []).append((record_id, revision))
    for (provider, _event), members in sorted(groups.items()):
        members.sort(key=lambda m: (ms(m[1]["content"].get("issued_at")) or m[1]["published_at_ms"], m[1]["revision"]))
        chain = [{"record_id": rid, "number": rev["content"].get("advisory_number") or rev["content"].get("episode_id")
                  or rev["content"]["native_id"], "issued_at": rev["content"].get("issued_at")} for rid, rev in members]
        for index, (record_id, revision) in enumerate(members):
            content = revision["content"]
            if (ms(content.get("issued_at")) or revision["published_at_ms"]) > at_ms:
                continue  # not yet issued at that time
            later = [ms(m[1]["content"].get("issued_at")) for m in members[index + 1:]]
            supersedes = provider in {"nhc", "gdacs"}
            validity = valid_at(content, at_ms, next_issued_ms=later[0] if later and supersedes else None)
            if not validity["in_force"]:
                continue
            matched = _alert_match(store, namespace, area, iso3, revision, principal_id)
            if matched is None:
                continue
            header = store._header(record_id)
            items.append({
                "record_id": record_id, "record_type": header["record_type"], "provider": provider,
                "issuing_body": content["issuing_body"], "title": content["title"],
                "level": content.get("level"), "wording": content.get("wording"),
                "advisory_number": content.get("advisory_number"), "episode_id": content.get("episode_id"),
                "storm_id": content.get("storm_id"), "issued_at": content.get("issued_at"),
                "watches_warnings": content.get("watches_warnings"), "thresholds": content.get("thresholds"),
                "modelled": content.get("modelled", False), "model": content.get("model"),
                "validity": validity, "supersession_chain": chain,
                "superseded_numbers": [c["number"] for c in chain[:index]],
                "matched_by": matched, "citation": cite(header, revision),
                "quoted": "level, wording and watches/warnings are quoted as issued, never re-classified"})
    providers = sorted({p for ps in ALERT_TYPES.values() for p in ps})
    covered = coverage(store, namespace, providers, None, rep)
    per_provider = {p: ("alerts on record in force" if any(i["provider"] == p for i in items) else
                        "no alert on record" if covered[p] == "covered" else covered[p]) for p in providers}
    return {"contract": ALERTS_CONTRACT, "namespace": namespace, "at": iso(at_ms),
            "as_of": iso(as_of_ms) if as_of_ms else "latest",
            "area": {k: v for k, v in area.items() if k not in {"geometry", "place"}}, "country": iso3,
            "alerts": items, "per_provider": per_provider,
            "answer": "alerts in force on record" if items else (
                "no alert on record" if "covered" in covered.values() else NOT_COVERED),
            "exclusions": list(hr.NEVER),
            "notice": "record of what issuing bodies published; not a warning, safety or protective-action advice"}


def _alert_match(store, namespace, area, iso3, revision, principal_id):
    content = revision["content"]
    geometry = content.get("geometry")
    rep = _representative(area)
    if geometry and geometry["type"] in {"Polygon", "MultiPolygon"} and revision["geometry_id"]:
        relation = store.geo.relation(namespace, "contains", revision["geometry_id"], rep, scopes=GEO_SCOPES,
                                      principal_id=principal_id)
        if relation["result"]["contains"]:
            return {"basis": "published alert area contains the place", "spatial_receipt_id": relation["receipt_id"]}
    for warning in content.get("watches_warnings") or []:
        named = _place_named(warning["area_text"], area.get("names") or [])
        if named:
            return {"basis": "the published watch/warning area text names the place", "quoted_area": warning["area_text"],
                    "type": warning["type"], "names_matched": named}
    if iso3 and iso3 in (content.get("countries") or []):
        return {"basis": "the place's country is in the published affected-country list", "country": iso3}
    return None
