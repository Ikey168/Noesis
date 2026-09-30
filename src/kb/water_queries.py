"""As-of answers: a value at a station, a series window, stations and water-body status for a place (WA08, WA09).

* :func:`value_at` (WA08 #2622) - given a station, a parameter and a time,
  the value **on record at** ``as_of`` (retrieval time) with its quality state
  (provisional or approved, as published), its qualifiers, the observation
  revision it cites and every later revision (for example the approved value
  that replaced a provisional one). A time the source published no value for
  is answered as missing, with the neighbouring published timestamps - never
  interpolated.
* :func:`series` - the published values of one station and parameter in a
  window, each cited, with the missing steps of the published interval listed
  as missing.
* :func:`place_water` (WA09 #2627) - given a place (optionally a river), its
  stations and water bodies. Membership is either an *accepted* WA06 match or
  a containment computed through the geospatial owner's geometry with the
  geometry version stated; each station carries its latest values as of the
  date and each water body its status history per reporting cycle, every item
  cited.
* :func:`status_history` - one water body's status per reporting cycle as
  reported; cycles are listed side by side and never merged, and no trend or
  own assessment is derived.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any

from src.kb.water_records import (
    NEVER_SENTENCE,
    PARAMETERS,
    PLACE_ANSWER_CONTRACT,
    READ_SCOPE,
    STATUS_ANSWER_CONTRACT,
    VALUE_ANSWER_CONTRACT,
    WaterError,
    authorize,
)
from src.kb.water_store import WaterStore, iso

STATUS_NOTICE = ("status as reported per WFD reporting cycle; cycles are not merged, no trend is derived and Noesis "
                 "makes no status assessment")


def _ms(as_of: Any) -> int | None:
    if as_of is None or isinstance(as_of, int):
        return as_of
    text = str(as_of)
    if len(text) == 10:
        text += "T23:59:59.999+00:00"
    return int(datetime.fromisoformat(text).timestamp() * 1000)


def _instant(text: str) -> datetime | date:
    if len(text) == 10:
        return date.fromisoformat(text)
    value = datetime.fromisoformat(text)
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _same_time(published: str, wanted: str) -> bool:
    a, b = _instant(published), _instant(wanted)
    if isinstance(a, datetime) != isinstance(b, datetime):
        return False
    return a == b


def _sort_key(text: str) -> datetime:
    value = _instant(text)
    return value if isinstance(value, datetime) else datetime(value.year, value.month, value.day, tzinfo=UTC)


def _cite(record: Mapping[str, Any], revision: Mapping[str, Any], as_of_ms: int | None) -> dict[str, Any]:
    return {"record_id": record["record_id"], "revision_id": revision["revision_id"],
            "revision_no": revision["revision_no"], "source_url": revision["statement"]["source"]["url"],
            "attribution": revision["statement"]["source"].get("attribution"),
            "retrieved_at": revision["retrieved_at"], "as_of": iso(as_of_ms) if as_of_ms is not None else None}


def _identity(conn):
    from src.kb.water_identity import WaterIdentity

    return WaterIdentity(conn, initialize=False)


def _station(conn, store, namespace, station) -> dict[str, Any]:
    keys = [k for k in _identity(conn).find(namespace, station) if not k.startswith("wfd:")]
    if not keys:
        raise WaterError("not_found", f"{station!r} reaches no acquired station")
    if len(keys) > 1:
        raise WaterError("ambiguous", f"{station!r} reaches several stations: {keys}")
    record = next(r for r in store.records(namespace, record_type="station") if r["subject_key"] == keys[0])
    return record


def _parameter(value: str) -> tuple[str | None, str | None]:
    if value in PARAMETERS:
        return value, None
    return None, value


def _observations(store, namespace, station_record, parameter):
    name, code = _parameter(parameter)
    native = station_record["record_key"]
    for record in store.records(namespace, record_type="observation", provider=station_record["provider"]):
        if not record["record_key"].startswith(native + "|"):
            continue
        first = store.revisions(namespace, record["record_id"])[0]["statement"]["as_published"]
        if (name and first["parameter"] == name) or (code and first["parameter_code"] == code):
            yield record, first


def _gauge_zero_at(station_published: Mapping[str, Any], parameter_code: str, time: str) -> dict[str, Any] | None:
    series = next((s for s in station_published.get("timeseries") or [] if s["parameter_code"] == parameter_code),
                  None)
    zero = (series or {}).get("gauge_zero")
    if not zero:
        return None
    applies = zero.get("valid_from") is None or str(zero["valid_from"]) <= time[:10]
    return {**zero, "applies_to_time": applies,
            "note": None if applies else "the gauge zero on record became valid after this time; the value refers "
                                         "to the gauge zero valid then, which the station's earlier revision states"}


def value_at(conn: Any, namespace: str, station: str, parameter: str, time: str, *, scopes: Iterable[str],
             as_of: Any = None) -> dict[str, Any]:
    """The value of one parameter at one station and time, as on record at ``as_of``, with later revisions."""
    authorize(namespace, scopes, READ_SCOPE)
    store = WaterStore(conn, initialize=False)
    store.require_ready()
    as_of_ms = _ms(as_of)
    station_record = _station(conn, store, namespace, station)
    station_revision = store.current(namespace, station_record["record_id"], as_of_ms=as_of_ms) or store.current(
        namespace, station_record["record_id"])
    station_published = station_revision["statement"]["as_published"]
    base = {"contract": VALUE_ANSWER_CONTRACT, "namespace": namespace, "station": {
        "subject_key": station_record["subject_key"], "name": station_published["name"],
        "number": station_published.get("number"), "citation": _cite(station_record, station_revision, as_of_ms)},
        "parameter": parameter, "time": time, "as_of": iso(as_of_ms) if as_of_ms is not None else None,
        "as_of_basis": "revisions retrieved on or before as_of (retrieval time)", "notice": NEVER_SENTENCE}
    candidates = list(_observations(store, namespace, station_record, parameter))
    if not candidates:
        return {**base, "status": "parameter not published for this station", "value": None}
    match = next(((r, p) for r, p in candidates if _same_time(p["time"], time)), None)
    if match is None:
        times = sorted((p["time"] for _, p in candidates), key=_sort_key)
        wanted = _sort_key(time)
        before = [t for t in times if _sort_key(t) < wanted]
        after = [t for t in times if _sort_key(t) > wanted]
        return {**base, "status": "no value published for this time", "value": None,
                "missing": {"previous_published": before[-1] if before else None,
                            "next_published": after[0] if after else None,
                            "note": "missing values stay missing; nothing is interpolated or filled"}}
    record, _first = match
    revisions = store.revisions(namespace, record["record_id"])
    known = [r for r in revisions if as_of_ms is None or r["observed_at_ms"] <= as_of_ms]
    later = [r for r in revisions if r not in known]
    later_view = [{"revision_id": r["revision_id"], "retrieved_at": r["retrieved_at"], "event": r["event"],
                   "value": r["statement"]["as_published"]["value"],
                   "quality": r["statement"]["as_published"]["quality"],
                   "qualifiers": r["statement"]["as_published"].get("qualifiers") or []} for r in later]
    if not known:
        return {**base, "status": "not yet on record at as_of", "value": None, "later_revisions": later_view}
    current = known[-1]
    published = current["statement"]["as_published"]
    status = "withdrawn by the source" if current["event"] == "removed" else "value on record"
    return {**base, "status": status, "value": None if current["event"] == "removed" else published["value"],
            "unit": published["unit"], "parameter_code": published["parameter_code"],
            "statistic": published.get("statistic"), "published_time": published["time"],
            "quality": published["quality"], "qualifiers": published.get("qualifiers") or [],
            "gauge_zero": _gauge_zero_at(station_published, published["parameter_code"], published["time"])
            if published["parameter"] == "water_level" else None,
            "citation": _cite(record, current, as_of_ms),
            "withdrawn": current["statement"]["effective"] if current["event"] == "removed" else None,
            "earlier_revisions": [{"revision_id": r["revision_id"], "value": r["statement"]["as_published"]["value"],
                                   "quality": r["statement"]["as_published"]["quality"]["state"]}
                                  for r in known[:-1]],
            "later_revisions": later_view}


def _step(station_published, code, statistic):
    if statistic and statistic != "instantaneous":
        return timedelta(days=1)
    series = next((s for s in station_published.get("timeseries") or [] if s["parameter_code"] == code), None)
    minutes = (series or {}).get("equidistance_min")
    return timedelta(minutes=minutes) if minutes else None


def series(conn: Any, namespace: str, station: str, parameter: str, *, start: str, end: str,
           scopes: Iterable[str], as_of: Any = None) -> dict[str, Any]:
    """Published values of one parameter in a window as on record at ``as_of``; missing steps listed, not filled."""
    authorize(namespace, scopes, READ_SCOPE)
    store = WaterStore(conn, initialize=False)
    store.require_ready()
    as_of_ms = _ms(as_of)
    station_record = _station(conn, store, namespace, station)
    station_published = (store.current(namespace, station_record["record_id"], as_of_ms=as_of_ms) or store.current(
        namespace, station_record["record_id"]))["statement"]["as_published"]
    lo, hi = _sort_key(start), _sort_key(end)
    values, kinds = [], set()
    for record, first in _observations(store, namespace, station_record, parameter):
        kinds.add((first["parameter_code"], first.get("statistic")))
        if not lo <= _sort_key(first["time"]) <= hi:
            continue
        revision = store.current(namespace, record["record_id"], as_of_ms=as_of_ms)
        if revision is None or revision["event"] == "removed":
            continue
        published = revision["statement"]["as_published"]
        values.append({"time": published["time"], "value": published["value"], "unit": published["unit"],
                       "quality": published["quality"]["state"], "qualifiers": published.get("qualifiers") or [],
                       "citation": _cite(record, revision, as_of_ms)})
    values.sort(key=lambda v: _sort_key(v["time"]))
    missing = []
    if values and len(kinds) == 1:
        code, statistic = next(iter(kinds))
        step = _step(station_published, code, statistic)
        if step:
            seen = {_sort_key(v["time"]) for v in values}
            cursor = _sort_key(values[0]["time"])
            while cursor <= _sort_key(values[-1]["time"]):
                if cursor not in seen:
                    missing.append(cursor.isoformat() if "T" in values[0]["time"] else cursor.date().isoformat())
                cursor += step
    return {"contract": VALUE_ANSWER_CONTRACT, "namespace": namespace, "station": station_record["subject_key"],
            "parameter": parameter, "window": {"start": start, "end": end},
            "as_of": iso(as_of_ms) if as_of_ms is not None else None, "values": values, "missing": missing,
            "status": "values on record" if values else "no value on record in this window",
            "notice": "missing steps of the published interval are listed as missing; " + NEVER_SENTENCE}


# ------------------------------------------------------------------ water bodies


def _assessments(store, namespace, eu_code, as_of_ms):
    rows = []
    for record in store.records(namespace, record_type="assessment", subject_keys=[f"wfd:{eu_code}"]):
        revision = store.current(namespace, record["record_id"], as_of_ms=as_of_ms)
        if revision is None:
            continue
        published = revision["statement"]["as_published"]
        rows.append({"reporting_cycle": published["reporting_cycle"], "cycle_year": published["cycle_year"],
                     "ecological": published.get("ecological"), "chemical": published.get("chemical"),
                     "elements": published.get("elements") or [], "natural_status": published.get("natural_status"),
                     "event": revision["event"], "revisions": len(store.revisions(namespace, record["record_id"])),
                     "citation": _cite(record, revision, as_of_ms)})
    return sorted(rows, key=lambda r: r["cycle_year"])


def status_history(conn: Any, namespace: str, water_body: str, *, scopes: Iterable[str],
                   as_of: Any = None) -> dict[str, Any]:
    authorize(namespace, scopes, READ_SCOPE)
    store = WaterStore(conn, initialize=False)
    store.require_ready()
    as_of_ms = _ms(as_of)
    keys = [k for k in _identity(conn).find(namespace, water_body) if k.startswith("wfd:")]
    if not keys:
        raise WaterError("not_found", f"{water_body!r} reaches no acquired water body")
    code = keys[0].split(":", 1)[1]
    cycles = _assessments(store, namespace, code, as_of_ms)
    return {"contract": STATUS_ANSWER_CONTRACT, "namespace": namespace, "eu_code": code,
            "as_of": iso(as_of_ms) if as_of_ms is not None else None, "cycles": cycles,
            "latest_cycle": cycles[-1]["cycle_year"] if cycles else None,
            "status": "reported" if cycles else "no reporting cycle on record", "notice": STATUS_NOTICE}


# ------------------------------------------------------------------ place


def place_water(conn: Any, namespace: str, place_id: str, *, scopes: Iterable[str], river: str | None = None,
                as_of: Any = None) -> dict[str, Any]:
    """Stations and water bodies of a place (optionally on one river), each with cited latest values or status."""
    from src.kb.water_identity import contains, place_view, vertices

    authorize(namespace, scopes, READ_SCOPE)
    store = WaterStore(conn, initialize=False)
    store.require_ready()
    as_of_ms = _ms(as_of)
    place = place_view(conn, namespace, place_id)
    if place is None:
        raise WaterError("not_found", "place is not visible in this namespace")
    identity = _identity(conn)
    river_view = place_view(conn, namespace, river) if river else None
    if river and river_view is None:
        raise WaterError("not_found", "river place is not visible in this namespace")
    on_river = {m["subject_key"] for m in identity.accepted(namespace, place_id=river)} if river else None
    accepted = {m["subject_key"]: m for m in identity.accepted(namespace, place_id=place_id)}
    stations, bodies = [], []
    for key, subject in sorted(identity.subjects(namespace, as_of_ms=as_of_ms).items()):
        published = subject["published"]
        membership = None
        if key in accepted:
            match = accepted[key]
            membership = {"basis": "accepted identity match", "match_id": match["match_id"],
                          "method": match["method"], "reviewer": match["reviewer"],
                          "geometry_id": match["evidence"].get("geometry_id"),
                          "place_revision_id": match["evidence"].get("place_revision_id")}
        elif place["geometry"]:
            points = ([[published["location"]["longitude"], published["location"]["latitude"]]]
                      if subject["kind"] == "station" and published["location"].get("latitude") is not None
                      else vertices(published["geometry"]) if subject["kind"] == "water_body" and
                      published.get("geometry") else [])
            if points and all(contains(place["geometry"], p) for p in points):
                membership = {"basis": "containment of the published location in the place boundary (not "
                                       "reviewed)", "geometry_id": place["geometry_id"],
                              "place_revision_id": place["revision_id"]}
        if membership is None:
            continue
        if subject["kind"] == "station":
            if on_river is not None and key not in on_river:
                continue
            latest = {}
            for record in store.records(namespace, record_type="observation", provider=subject["record"]["provider"]):
                if not record["record_key"].startswith(subject["record"]["record_key"] + "|"):
                    continue
                revision = store.current(namespace, record["record_id"], as_of_ms=as_of_ms)
                if revision is None or revision["event"] == "removed":
                    continue
                value = revision["statement"]["as_published"]
                slot = latest.get(value["parameter"])
                if slot is None or _sort_key(value["time"]) > _sort_key(slot["time"]):
                    latest[value["parameter"]] = {"time": value["time"], "value": value["value"],
                                                  "unit": value["unit"], "quality": value["quality"]["state"],
                                                  "qualifiers": value.get("qualifiers") or [],
                                                  "citation": _cite(record, revision, as_of_ms)}
            stations.append({"subject_key": key, "name": published["name"], "number": published.get("number"),
                             "river": published.get("river"), "location": published["location"],
                             "datum": published.get("datum"), "thresholds": published.get("thresholds") or [],
                             "membership": membership, "latest": latest,
                             "citation": _cite(subject["record"], subject["revision"], as_of_ms)})
        elif on_river is None or key in on_river:
            bodies.append({"subject_key": key, "eu_code": published["eu_code"], "name": published["name"],
                           "category": published["category"], "membership": membership,
                           "geometry_vintage": (published.get("geometry") or {}).get("source"),
                           "status_history": _assessments(store, namespace, published["eu_code"], as_of_ms),
                           "citation": _cite(subject["record"], subject["revision"], as_of_ms)})
    empty = not stations and not bodies
    return {"contract": PLACE_ANSWER_CONTRACT, "namespace": namespace,
            "place": {k: place[k] for k in ("place_id", "name", "place_type", "revision_id", "geometry_id")},
            "river": river_view and {k: river_view[k] for k in ("place_id", "name", "identifiers")},
            "as_of": iso(as_of_ms) if as_of_ms is not None else None, "stations": stations, "water_bodies": bodies,
            "status": "no station or water body on record for this place" if empty else "records on record",
            "containment": "accepted WA06 matches, or containment in the place boundary geometry named by its id "
                           "(geometry version)", "notice": STATUS_NOTICE + "; " + NEVER_SENTENCE}


# ------------------------------------------------------------------ bundle section


def bundle(conn: Any, namespace: str, place_id: str, *, scopes: Iterable[str], as_of: Any = None) -> dict[str, Any]:
    """The water section of a place bundle; every item cites source, record revision and as-of time."""
    answer = place_water(conn, namespace, place_id, scopes=scopes, as_of=as_of)
    items = []
    for station in answer["stations"]:
        items.append({"kind": "station", "subject_key": station["subject_key"], **station["citation"]})
        for parameter, value in sorted(station["latest"].items()):
            items.append({"kind": "observation", "subject_key": station["subject_key"], "parameter": parameter,
                          "quality": value["quality"], **value["citation"]})
    for body in answer["water_bodies"]:
        items.append({"kind": "water_body", "subject_key": body["subject_key"], **body["citation"]})
        for cycle in body["status_history"]:
            items.append({"kind": "assessment", "subject_key": body["subject_key"],
                          "reporting_cycle": cycle["cycle_year"], **cycle["citation"]})
    return {"feature": "water", "as_of": answer["as_of"], "place": answer, "citations": items,
            "citation_rule": "every item names its source URL, record revision and retrieval time"}


__all__ = ["bundle", "place_water", "series", "status_history", "value_at"]
