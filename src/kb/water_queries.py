"""As-of answers: a value at a station and time, stations and water bodies for a place, status history (WA08, WA09).

* :func:`value_at` - given a station, a parameter (published code such as
  ``W`` or ``00060``, or ``water_level``/``discharge``) and a time, the value
  **on record at** ``as_of`` (retrieval time) with its unit and time as
  published, its quality state (provisional or approved) and qualifiers, the
  gauge zero or datum the station published for that time, the revision it
  cites and every later revision. A time the source published no value for is
  answered as missing, with the neighbouring published times - never filled.
* :func:`series` - the published values in a window with the gaps the
  station's own published interval shows; nothing is resampled or filled.
* :func:`station_history` - the station's revisions as location and
  gauge-zero/datum vintages.
* :func:`for_place` - the stations and water bodies of a place or river:
  by accepted identity match and by geospatial containment with the stated
  geometry version, each water body with its latest and historical status per
  reporting cycle, and the unmatched records kept visible.
* :func:`status_history` - one water body's WFD status per reporting cycle as
  published, each cycle cited; cycles are never merged.
* :func:`bundle` - an evidence bundle for a place, station or water body in
  which every item cites source, record revision and as-of time.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable
from datetime import UTC, date, datetime
from typing import Any

from src.kb.water_records import (
    BUNDLE_CONTRACT,
    MINIMISATION,
    NEVER_SENTENCE,
    PLACE_ANSWER_CONTRACT,
    QUANTITIES,
    READ_SCOPE,
    STATUS_ANSWER_CONTRACT,
    VALUE_ANSWER_CONTRACT,
    WaterError,
    authorize,
    minimise,
    water_body_subject,
)
from src.kb.water_store import WaterStore, cite, iso

NOT_FILLED = "missing periods stay missing: no interpolation, resampling or gap filling"


def _ms(as_of: Any) -> int | None:
    if as_of is None or isinstance(as_of, int):
        return as_of
    text = str(as_of)
    if len(text) == 10:
        text += "T23:59:59.999+00:00"
    return int(datetime.fromisoformat(text).timestamp() * 1000)


def _moment(text: str) -> datetime | date:
    """A published or requested time: an aware instant (UTC-compared) or a calendar date."""
    value = str(text).strip()
    if len(value) == 10:
        return date.fromisoformat(value)
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise WaterError("invalid_request", "times carry an offset (or are a calendar date)")
    return moment.astimezone(UTC)


def _same(a: datetime | date, b: datetime | date) -> bool:
    return type(a) is type(b) and a == b


def _store(conn: Any, namespace: str, scopes: Iterable[str]) -> WaterStore:
    authorize(namespace, scopes, READ_SCOPE)
    store = WaterStore(conn, initialize=False)
    store.require_ready()
    return store


def resolve_station(store: WaterStore, namespace: str, station: str) -> dict[str, Any]:
    """A station by subject key (``provider:id``), native id or published number."""
    text = str(station or "").strip()
    stations = store.records(namespace, record_type="station")
    for record in stations:
        if text in {record["subject_key"], record["record_key"]}:
            return record
    for record in stations:
        current = store.current(namespace, record["record_id"])
        if current and text == str(current["statement"]["as_published"].get("number") or ""):
            return record
    raise WaterError("not_found", f"no station {text!r} on record in this namespace")


def _parameters(provider: str, parameter: str) -> set[str]:
    wanted = str(parameter or "").strip()
    codes = {code for (p, code), kind in QUANTITIES.items() if p == provider and kind == wanted}
    return codes or {wanted}


def _observations(store, namespace, station_key, parameter):
    provider = station_key.split(":", 1)[0]
    codes = _parameters(provider, parameter)
    for record in store.records(namespace, record_type="observation", provider=provider,
                                subject_keys=[station_key]):
        first = store.revisions(namespace, record["record_id"])[:1]
        if first and first[0]["statement"]["as_published"]["parameter"] in codes:
            yield record


def _view(record, revision) -> dict[str, Any]:
    published = revision["statement"]["as_published"]
    return {"parameter": published["parameter"], "parameter_name": published.get("parameter_name"),
            "quantity": published.get("quantity"), "statistic": published.get("statistic"),
            "time": published["time"], "value": published.get("value"), "unit": published["unit"],
            "quality": published["quality"], "qualifiers": published.get("qualifiers") or [],
            "event": revision["event"], "changes": revision["changes"], "citation": cite(record, revision)}


def _datum(store, namespace, station, moment, parameter, as_of_ms):
    """The gauge zero or datum the station revision on record published for the observation's date."""
    revision = store.current(namespace, station["record_id"], as_of_ms=as_of_ms)
    if revision is None:
        return None
    day = moment if isinstance(moment, date) and not isinstance(moment, datetime) else moment.date()
    candidates = [d for d in revision["statement"]["as_published"].get("datums") or []
                  if d.get("series") in (None, parameter)
                  and (d.get("valid_from") is None or d["valid_from"][:10] <= day.isoformat())]
    chosen = sorted(candidates, key=lambda d: d.get("valid_from") or "")[-1:] or [None]
    return None if chosen[0] is None else {**chosen[0], "station_revision_id": revision["revision_id"],
                                           "basis": "as published by the station revision on record; values are "
                                                    "never converted"}


def value_at(conn: Any, namespace: str, station: str, parameter: str, time: str, *, scopes: Iterable[str],
             as_of: Any = None) -> dict[str, Any]:
    """The value known at ``as_of`` for a station, parameter and time, with quality state and later revisions."""
    store = _store(conn, namespace, scopes)
    record = resolve_station(store, namespace, station)
    moment, as_of_ms = _moment(time), _ms(as_of)
    values, published_times = [], []
    for obs in _observations(store, namespace, record["subject_key"], parameter):
        revisions = store.revisions(namespace, obs["record_id"])
        published_time = _moment(revisions[0]["statement"]["as_published"]["time"])
        known = [r for r in revisions if as_of_ms is None or r["observed_at_ms"] <= as_of_ms]
        if known:
            published_times.append((published_time, revisions[0]["statement"]["as_published"]["time"]))
        if not _same(published_time, moment):
            continue
        later = [r for r in revisions if r not in known]
        values.append({
            "on_record": _view(obs, known[-1]) if known else None,
            "status": ("value on record" if known else "not yet on record at the as-of time"),
            "later_revisions": [{**_view(obs, r), "retrieved_at": r["retrieved_at"]} for r in later],
            "revision_count": len(revisions),
            "reference_datum": _datum(store, namespace, record, published_time,
                                      revisions[0]["statement"]["as_published"]["parameter"], as_of_ms)})
    answer = {"contract": VALUE_ANSWER_CONTRACT, "namespace": namespace, "station": record["subject_key"],
              "station_name": record["subject_name"], "parameter": parameter, "time": time,
              "as_of": iso(as_of_ms), "as_of_basis": "revisions retrieved on or before the as-of time",
              "values": values, "notice": NEVER_SENTENCE}
    if not any(v["on_record"] for v in values):
        comparable = sorted(t for t in published_times if type(t[0]) is type(moment))
        before = [text for t, text in comparable if t < moment]
        after = [text for t, text in comparable if t > moment]
        answer["missing"] = {"status": "no value published for that time" if not values else
                             "not yet on record at the as-of time",
                             "previous_published_time": before[-1] if before else None,
                             "next_published_time": after[0] if after else None, "policy": NOT_FILLED}
    answer["status"] = "value on record" if any(v["on_record"] for v in values) else answer["missing"]["status"]
    return minimise(answer)


def series(conn: Any, namespace: str, station: str, parameter: str, start: str, end: str, *,
           scopes: Iterable[str], as_of: Any = None) -> dict[str, Any]:
    """Published values of one parameter in a window, each cited, with gaps shown and never filled."""
    store = _store(conn, namespace, scopes)
    record = resolve_station(store, namespace, station)
    begin, finish, as_of_ms = _moment(start), _moment(end), _ms(as_of)
    rows = []
    for obs in _observations(store, namespace, record["subject_key"], parameter):
        revision = store.current(namespace, obs["record_id"], as_of_ms=as_of_ms)
        if revision is None:
            continue
        moment = _moment(revision["statement"]["as_published"]["time"])
        if type(moment) is type(begin) and begin <= moment <= finish:
            rows.append((moment, _view(obs, revision)))
    rows.sort(key=lambda item: (item[0], item[1]["parameter"]))
    station_revision = store.current(namespace, record["record_id"], as_of_ms=as_of_ms)
    intervals = {s["parameter"]: s.get("interval_min") for s in
                 (station_revision["statement"]["as_published"].get("series") or [])} if station_revision else {}
    gaps = []
    for (a, row_a), (b, row_b) in itertools.pairwise(rows):
        step = intervals.get(row_a["parameter"])
        if isinstance(a, datetime) and step and row_a["parameter"] == row_b["parameter"] and \
                (b - a).total_seconds() > step * 60:
            gaps.append({"parameter": row_a["parameter"], "after": row_a["time"], "before": row_b["time"],
                         "published_interval_min": step, "status": "no value published", "policy": NOT_FILLED})
    return minimise({"contract": VALUE_ANSWER_CONTRACT, "namespace": namespace, "station": record["subject_key"],
                     "parameter": parameter, "window": {"start": start, "end": end}, "as_of": iso(as_of_ms),
                     "values": [r for _, r in rows], "gaps": gaps,
                     "interval_basis": "the station's published series interval; gaps are only reported where it is "
                                       "published",
                     "quality_states": sorted({r["quality"]["state"] for _, r in rows}),
                     "status": "values on record" if rows else "no value on record in the window",
                     "notice": NEVER_SENTENCE})


def station_history(conn: Any, namespace: str, station: str, *, scopes: Iterable[str]) -> dict[str, Any]:
    """Every station revision as a vintage: location, gauge zero or datum, thresholds; removals kept."""
    store = _store(conn, namespace, scopes)
    record = resolve_station(store, namespace, station)
    vintages = []
    for revision in store.revisions(namespace, record["record_id"]):
        published = revision["statement"]["as_published"]
        vintages.append({"revision_no": revision["revision_no"], "event": revision["event"],
                         "changes": revision["changes"], "location": published.get("location"),
                         "datums": published.get("datums") or [], "thresholds": published.get("thresholds") or [],
                         "river": published.get("river"), "citation": cite(record, revision)})
    return minimise({"contract": VALUE_ANSWER_CONTRACT, "namespace": namespace, "station": record["subject_key"],
                     "name": record["subject_name"], "vintages": vintages,
                     "location_changes": sum("location" in v["changes"] for v in vintages),
                     "datum_changes": sum("datum" in v["changes"] for v in vintages),
                     "notice": "station revisions as published; values are never re-referenced to another datum"})


# ------------------------------------------------------------------ water bodies


def _assessments(store, namespace, code, as_of_ms):
    cycles = []
    for record in store.records(namespace, record_type="water_body_assessment",
                                subject_keys=[water_body_subject(code)]):
        revisions = store.revisions(namespace, record["record_id"])
        known = [r for r in revisions if as_of_ms is None or r["observed_at_ms"] <= as_of_ms]
        if not known:
            continue
        revision = known[-1]
        published = revision["statement"]["as_published"]
        cycles.append({"cycle": published["cycle"], "event": revision["event"],
                       "status_elements": published["status_elements"],
                       "ecological_status": published.get("ecological_status"),
                       "chemical_status": published.get("chemical_status"), "category": published.get("category"),
                       "modification": published.get("modification"), "citation": cite(record, revision),
                       "earlier_revisions": [cite(record, r) for r in known[:-1]],
                       "later_revisions": [cite(record, r) for r in revisions[len(known):]]})
    return sorted(cycles, key=lambda c: c["cycle"])


def _code(store, namespace, water_body):
    text = str(water_body or "").strip().removeprefix("eu-wb:")
    for record in store.records(namespace):
        if record["record_type"] in {"water_body", "water_body_assessment"} and \
                (record["subject_key"] == water_body_subject(text) or
                 (record["subject_name"] or "").casefold() == text.casefold()):
            return record["subject_key"].removeprefix("eu-wb:")
    raise WaterError("not_found", f"no water body {water_body!r} on record in this namespace")


def status_history(conn: Any, namespace: str, water_body: str, *, scopes: Iterable[str],
                   as_of: Any = None) -> dict[str, Any]:
    """WFD status per reporting cycle as published; cycles are never merged into one status."""
    store = _store(conn, namespace, scopes)
    code, as_of_ms = _code(store, namespace, water_body), _ms(as_of)
    cycles = _assessments(store, namespace, code, as_of_ms)
    published = [c for c in cycles if c["event"] == "published"]
    return minimise({
        "contract": STATUS_ANSWER_CONTRACT, "namespace": namespace, "water_body": water_body_subject(code),
        "as_of": iso(as_of_ms), "cycles": cycles,
        "latest_cycle": published[-1]["cycle"] if published else None,
        "status": "status on record" if published else "no status on record for this water body",
        "notice": "each reporting cycle as the member state reported it to the EEA; cycles are never merged and "
                  "Noesis makes no status assessment of its own"})


# ------------------------------------------------------------------ places


def _within(place, point) -> bool:
    """Point in the place boundary, by the geospatial owner's own containment test (read-only, no receipt)."""
    from src.kb.geospatial import _contains

    return bool(_contains(place["geometry"], point, 0))


def for_place(conn: Any, namespace: str, place_id: str, *, scopes: Iterable[str], as_of: Any = None
              ) -> dict[str, Any]:
    """Stations and water bodies of a place or river with their latest and historical status, each cited."""
    from src.kb.water_identity import WaterIdentity, geometry_relation, place_view

    store = _store(conn, namespace, scopes)
    as_of_ms = _ms(as_of)
    place = place_view(conn, namespace, place_id)
    if place is None:
        raise WaterError("not_found", "place is not visible in this namespace")
    identity = WaterIdentity(conn, initialize=False)
    accepted = {}
    for match in identity.accepted(namespace, place_id=place_id):
        accepted.setdefault(match["subject_key"], []).append(
            {"basis": "accepted_match", "match_id": match["match_id"], "relation": match["relation"],
             "method": match["method"], "confidence": match["confidence"], "reviewer": match["reviewer"],
             "subject_revised_since": match["subject_revised_since"]})
    stations, water_bodies, unmatched = [], [], []
    for record in store.records(namespace):
        if record["record_type"] not in {"station", "water_body"}:
            continue
        revision = store.current(namespace, record["record_id"], as_of_ms=as_of_ms)
        if revision is None:
            continue
        published = revision["statement"]["as_published"]
        bases = list(accepted.get(record["subject_key"], []))
        if place["geometry"] and revision["event"] == "published":
            if record["record_type"] == "station" and published.get("location"):
                point = [published["location"]["longitude"], published["location"]["latitude"]]
                if _within(place, point):
                    bases.append({"basis": "geospatial-containment", "algorithm": "wgs84-stdlib-v1 contains",
                                  "geometry_version": place["geometry_version"],
                                  "note": "published station coordinates inside the stated boundary version"})
            elif record["record_type"] == "water_body" and published.get("geometry"):
                relation = geometry_relation(place["geometry"], published["geometry"])
                if relation["relation"] != "outside":
                    bases.append({"basis": "geospatial-intersection", "relation": relation["relation"],
                                  "geometry_version": place["geometry_version"],
                                  "water_body_geometry_vintage": published.get("geometry_vintage")})
        if record["record_type"] == "station" and place["place_type"] == "river":
            river = published.get("river") or {}
            if river.get("shortname") and place["source_ids"].get("pegelonline-water") == river["shortname"] \
                    and not any(b["basis"] == "accepted_match" and b["relation"] == "on-river" for b in bases):
                bases.append({"basis": "published-river-identifier (unreviewed)",
                              "identifier": {"scheme": "pegelonline-water", "value": river["shortname"]}})
        if not identity.accepted(namespace, subject_key=record["subject_key"]):
            unmatched.append({"subject_key": record["subject_key"], "kind": record["record_type"],
                              "name": record["subject_name"], "state": "unmatched (no accepted match)"})
        if not bases:
            continue
        item = {"subject_key": record["subject_key"], "name": record["subject_name"], "event": revision["event"],
                "bases": bases, "citation": cite(record, revision)}
        if record["record_type"] == "station":
            stations.append({**item, "river": published.get("river"), "location": published.get("location"),
                             "datums": published.get("datums") or []})
        else:
            cycles = _assessments(store, namespace, published["eu_code"], as_of_ms)
            live = [c for c in cycles if c["event"] == "published"]
            water_bodies.append({**item, "eu_code": published["eu_code"], "geometry_vintage":
                                 published.get("geometry_vintage"), "latest": live[-1] if live else None,
                                 "history": cycles})
    return minimise({
        "contract": PLACE_ANSWER_CONTRACT, "namespace": namespace, "as_of": iso(as_of_ms),
        "place": {k: place[k] for k in ("place_id", "name", "place_type", "revision_id", "geometry_version")},
        "stations": stations, "water_bodies": water_bodies, "unmatched": unmatched,
        "status": "water records on record" if stations or water_bodies else
        "no station or water body on record for this place",
        "notice": NEVER_SENTENCE})


# ------------------------------------------------------------------ evidence bundle


def bundle(conn: Any, namespace: str, *, scopes: Iterable[str], place_id: str | None = None,
           station: str | None = None, water_body: str | None = None, as_of: Any = None) -> dict[str, Any]:
    """An evidence bundle whose every item cites source, record revision and as-of (retrieval) time."""
    if not (place_id or station or water_body):
        raise WaterError("invalid_request", "name a place, a station or a water body")
    store = _store(conn, namespace, scopes)
    as_of_ms = _ms(as_of)
    items: list[dict[str, Any]] = []
    stations: list[str] = [resolve_station(store, namespace, station)["subject_key"]] if station else []
    codes: list[str] = [_code(store, namespace, water_body)] if water_body else []
    place = None
    if place_id:
        place = for_place(conn, namespace, place_id, scopes=scopes, as_of=as_of)
        stations += [s["subject_key"] for s in place["stations"]]
        codes += [w["eu_code"] for w in place["water_bodies"]]
    for key in sorted(set(stations)):
        record = resolve_station(store, namespace, key)
        revision = store.current(namespace, record["record_id"], as_of_ms=as_of_ms)
        items.append({"kind": "station", "subject_key": key, "name": record["subject_name"],
                      "as_published": revision["statement"]["as_published"], "citation": cite(record, revision)})
        for obs in store.records(namespace, record_type="observation", subject_keys=[key]):
            known = store.current(namespace, obs["record_id"], as_of_ms=as_of_ms)
            if known:
                items.append({"kind": "observation", "subject_key": key, **_view(obs, known)})
    for code in sorted(set(codes)):
        for cycle in _assessments(store, namespace, code, as_of_ms):
            items.append({"kind": "water_body_assessment", "subject_key": water_body_subject(code), **cycle})
    uncited = [i for i in items if not (i.get("citation") or {}).get("revision_id")]
    return minimise({
        "contract": BUNDLE_CONTRACT, "namespace": namespace, "feature": "water", "as_of": iso(as_of_ms),
        "subject": {"place_id": place_id, "station": station, "water_body": water_body},
        "place": place and place["place"], "items": items, "item_count": len(items),
        "every_item_cited": not uncited, "minimisation": MINIMISATION["decision"], "notice": NEVER_SENTENCE})


__all__ = ["NOT_FILLED", "bundle", "for_place", "resolve_station", "series", "station_history", "status_history",
           "value_at"]
