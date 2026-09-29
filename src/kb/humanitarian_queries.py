"""Cited humanitarian answers as of a date (HR09, #2270) and bounded conflict-event queries (HR10, #2274).

``published_about`` answers *what was published about a place or crisis as of
a date*: situation reports, appeals, crisis entries and dataset revisions,
each with the revision in force at that date and its citation (source,
source id, revision, as-of and retrieval time, locator). Place queries reach
records only through **accepted** HR07 place assertions, walking the admin
hierarchy of the geospatial places those assertions point at, and report the
boundary vintage. Every answer lists the sources consulted (with their
state), the sources declined (ACLED: not acquired, licence) and what has no
record ("none on record").

``conflict_events`` returns events in a bounded area (a bounding box of at
most 5 x 5 degrees, or one geospatial admin place) and window (at most 366
days), each coder's events side by side with its precision codes and counts
as published, and states how imprecise events (admin-level, country-level)
were included or excluded. ``event_history`` returns every release revision
of one event with the fields that changed.

Nothing here summarises a situation, ranks severity, merges or sums counts
across coders, estimates casualties or forecasts anything.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from src.ingestion.humanitarian_sources import ACLED_DECISION, LIVE_VERIFICATION, PROVIDERS
from src.kb.humanitarian_records import EXCLUSIONS, READ_SCOPE, HumanitarianError, digest, iso, to_ms
from src.kb.humanitarian_store import HumanitarianStore, authorize, citation

ANSWER_CONTRACT = "noesis-humanitarian-answer-v1"
EVENTS_CONTRACT = "noesis-humanitarian-conflict-events-v1"
HISTORY_CONTRACT = "noesis-humanitarian-event-history-v1"
PUBLISHED_TYPES = ("situation_report", "appeal", "crisis", "dataset")
MAX_BBOX_DEGREES = 5.0
MAX_WINDOW_DAYS = 366
MAX_EVENTS = 2000
IMPRECISE_POLICIES = ("exact-only", "admin", "all")
GEO_READ = {"knowledge:geospatial:read"}
# Coder precision codes grouped by what their coordinates mean.
PRECISION_CLASS = {
    ("ucdp-where_prec", 1): "exact", ("ucdp-where_prec", 2): "exact",
    ("ucdp-where_prec", 3): "admin", ("ucdp-where_prec", 4): "admin",
    ("ucdp-where_prec", 5): "wide", ("ucdp-where_prec", 6): "wide", ("ucdp-where_prec", 7): "wide",
    ("acled-geo_precision", 1): "exact", ("acled-geo_precision", 2): "admin", ("acled-geo_precision", 3): "wide",
}


def sources_declined() -> list[dict[str, Any]]:
    return [{"source": "acled", "status": ACLED_DECISION["status"], "reason": ACLED_DECISION["query_notice"],
             "reference": ACLED_DECISION["reference"]}] if ACLED_DECISION["status"] != "accepted" else []


def _family(source: str) -> str:
    return "ucdp" if source.startswith("ucdp-") else source


def _item(revision: Mapping[str, Any], matched_via: Any) -> dict[str, Any]:
    content = revision["content"]
    fields = {k: content.get(k) for k in ("title", "publishers", "report_date", "formats", "disasters", "name", "glide",
                                          "status", "organization", "licence", "access", "metadata_only",
                                          "dataset_date") if content.get(k) is not None}
    if content["record_type"] == "dataset":
        fields["resources"] = [{"resource_id": r["resource_id"], "hash": r.get("hash"), "hxl": r["hxl"].get("status"),
                                "hashtags": [c.get("hashtag") for c in r["hxl"].get("columns") or []]}
                               for r in content.get("resources") or []]
    return {"record_key": revision["record_key"], "record_type": content["record_type"], **fields,
            "revision_used": {"revision_id": revision["revision_id"], "revision": content["revision"],
                              "as_of": content["as_of"], "seq": revision["seq"]},
            "citation": citation(revision), "matched_via": matched_via, "unknowns": content.get("unknowns") or []}


class HumanitarianQueries:
    def __init__(self, conn: Any, *, now=None) -> None:
        from src.kb.humanitarian_identity import HumanitarianIdentity

        self.conn = conn
        self.store = HumanitarianStore(conn, initialize=False, now=now)
        self.identity = HumanitarianIdentity(conn, initialize=False, now=now)

    # ------------------------------------------------------------ common

    def _consulted(self, namespace: str) -> list[dict[str, Any]]:
        result = []
        for provider in PROVIDERS:
            if provider == "acled" and ACLED_DECISION["status"] != "accepted":
                continue
            state = self.store.provider_state(namespace, provider)
            result.append({"source": provider, "stale": state["stale"], "last_success_ms": state.get("last_success_ms"),
                           "last_failure_code": state.get("last_failure_code"),
                           "live_verification": LIVE_VERIFICATION[provider]["status"],
                           "note": "never acquired" if state.get("last_success_ms") is None else None})
        return result

    def _admin_place(self, namespace: str, place_id: str | None, pcode: str | None) -> dict[str, Any]:
        admin = self.identity.admin_places(namespace)
        for place in admin:
            if place_id and place["place_id"] == place_id:
                return place
            if pcode and pcode.upper() in {str(place["pcode"]).upper(), str(place.get("iso3") or "").upper()}:
                return place
        raise HumanitarianError("unknown_place", "no geospatial admin place with this id or code; import the "
                                                 "boundary file first")

    # ------------------------------------------------------------ HR09

    def published_about(self, namespace: str, *, as_of: Any, scopes: Iterable[str], place_id: str | None = None,
                        pcode: str | None = None, crisis_key: str | None = None, basis: str = "published",
                        record_types: Sequence[str] = PUBLISHED_TYPES) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if bool(place_id or pcode) == bool(crisis_key):
            raise HumanitarianError("invalid_request", "ask about one place (place_id or pcode) or one crisis")
        as_of_ms = to_ms(as_of)
        if as_of_ms is None:
            raise HumanitarianError("invalid_request", "as_of is required")
        items, none_on_record, boundary = [], [], {}
        if crisis_key:
            crisis = self.store.revision(namespace, crisis_key, scopes=scopes, as_of_ms=as_of_ms, basis=basis)
            if crisis is None:
                none_on_record.append({"crisis": crisis_key, "note": "none on record as of this date"})
            else:
                items.append(_item(crisis, {"basis": "the crisis record itself"}))
                disaster_id = crisis["content"]["source_id"]
                related = {a["subject_key"]: a for a in self.identity.assertions(
                    namespace, scopes=scopes, state="accepted", target_kind="crisis") if a["target_id"] == crisis_key}
                for revision in self.store.current(namespace, scopes=scopes, as_of_ms=as_of_ms, basis=basis):
                    content = revision["content"]
                    if content["record_type"] not in record_types or revision["record_key"] == crisis_key:
                        continue
                    if any(str(d.get("id")) == disaster_id for d in content.get("disasters") or []):
                        items.append(_item(revision, {"basis": "the source tags the disaster",
                                                      "disaster_id": disaster_id}))
                    elif revision["record_key"] in related:
                        a = related[revision["record_key"]]
                        items.append(_item(revision, {"basis": "accepted crisis assertion", "assertion_id": a["assertion_id"],
                                                      "method": a["method"]}))
            query = {"crisis_key": crisis_key}
        else:
            place = self._admin_place(namespace, place_id, pcode)
            within = self.identity.place_refs_within(namespace, place["place_id"], scopes=scopes)
            boundary = {"place_id": place["place_id"], "name": place["name"], "pcode": place["pcode"],
                        "boundary_vintage": place["boundary_vintage"], "assertion_vintages": within["boundary_vintages"],
                        "hierarchy": "admin descendants through geospatial parent ids; records only through accepted "
                                     "place assertions"}
            for revision in self.store.current(namespace, scopes=scopes, as_of_ms=as_of_ms, basis=basis):
                content = revision["content"]
                if content["record_type"] not in record_types:
                    continue
                from src.kb.humanitarian_identity import place_ref_key

                hits = [{"place_ref": place_ref_key(p), **within["refs"][place_ref_key(p)]}
                        for p in content.get("places") or [] if place_ref_key(p) in within["refs"]]
                if hits:
                    items.append(_item(revision, {"basis": "accepted place assertion", "places": hits}))
            if not within["refs"]:
                none_on_record.append({"place": place["name"], "pcode": place["pcode"],
                                       "note": "no accepted place assertion reaches this place; unmatched references "
                                               "stay unmatched"})
            query = {"place_id": place["place_id"], "pcode": place["pcode"]}
        if not items and not none_on_record:
            none_on_record.append({**query, "note": "none on record as of this date"})
        for record_type in record_types:
            if not any(i["record_type"] == record_type for i in items):
                none_on_record.append({"record_type": record_type, "note": "none on record as of this date"})
        items.sort(key=lambda i: (i["record_type"], i["record_key"]))
        return {"contract": ANSWER_CONTRACT, "namespace": namespace, "query": query, "as_of": iso(as_of_ms),
                "basis": basis, "items": items, "boundary": boundary or None,
                "sources_consulted": self._consulted(namespace), "sources_declined": sources_declined(),
                "none_on_record": none_on_record, "exclusions": list(EXCLUSIONS),
                "notice": "what was published, quoted with its revision; no situation assessment, ranking or advice",
                "answer_hash": digest([query, iso(as_of_ms), basis, [i["revision_used"] for i in items]])}

    # ------------------------------------------------------------ HR10

    def _area(self, namespace, area):
        area = dict(area or {})
        if area.get("bbox"):
            west, south, east, north = (float(v) for v in area["bbox"])
            if not (east > west and north > south):
                raise HumanitarianError("invalid_area", "bbox is [west, south, east, north]")
            if east - west > MAX_BBOX_DEGREES or north - south > MAX_BBOX_DEGREES:
                raise HumanitarianError("area_too_large", f"bbox is limited to {MAX_BBOX_DEGREES} x {MAX_BBOX_DEGREES} degrees")
            geometry = {"type": "Polygon", "coordinates": [[[west, south], [east, south], [east, north], [west, north],
                                                            [west, south]]]}
            return geometry, {"kind": "bbox", "bbox": [west, south, east, north]}
        if area.get("place_id") or area.get("pcode"):
            from src.kb.geospatial import GeospatialStore

            place = self._admin_place(namespace, area.get("place_id"), area.get("pcode"))
            if place["place_type"] == "country":
                raise HumanitarianError("area_too_large", "a country is not a bounded area; use an admin unit or a bbox")
            geometries = GeospatialStore(self.conn, initialize=False).geometries(namespace, place["place_id"],
                                                                                 scopes=GEO_READ)
            if not geometries:
                raise HumanitarianError("invalid_area", "the admin place has no geometry")
            return geometries[0]["geometry"], {"kind": "admin-place", "place_id": place["place_id"],
                                               "pcode": place["pcode"], "name": place["name"],
                                               "boundary_vintage": place["boundary_vintage"],
                                               "geometry_id": geometries[0]["geometry_id"]}
        raise HumanitarianError("invalid_area", "give a bbox or one admin place")

    def conflict_events(self, namespace: str, *, area: Mapping[str, Any], start: Any, end: Any, scopes: Iterable[str],
                        as_of: Any = None, imprecise: str = "admin") -> dict[str, Any]:
        from src.kb.geospatial import _contains

        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if imprecise not in IMPRECISE_POLICIES:
            raise HumanitarianError("invalid_request", f"imprecise is one of {IMPRECISE_POLICIES}")
        start_ms, end_ms = to_ms(start), to_ms(end)
        if start_ms is None or end_ms is None or end_ms < start_ms:
            raise HumanitarianError("invalid_window", "a closed window with start <= end is required")
        if (end_ms - start_ms) / 86_400_000 > MAX_WINDOW_DAYS:
            raise HumanitarianError("window_too_long", f"the window is limited to {MAX_WINDOW_DAYS} days")
        geometry, area_view = self._area(namespace, area)
        as_of_ms = to_ms(as_of)
        included_classes = {"exact-only": {"exact"}, "admin": {"exact", "admin"}, "all": {"exact", "admin", "wide"}}[imprecise]
        by_coder: dict[str, list[dict[str, Any]]] = {}
        excluded: list[dict[str, Any]] = []
        unlocated: list[str] = []
        for key in self.store.keys(namespace, record_type="conflict_event"):
            revision = self.store.revision(namespace, key, scopes=scopes, as_of_ms=as_of_ms)
            if revision is None:
                continue
            content = revision["content"]
            when = to_ms(str(content.get("date_start") or "")[:10])
            if when is None or when < start_ms or when > end_ms:
                continue
            location = content.get("location") or {}
            if not location.get("published"):
                unlocated.append(key)
                continue
            point = [float(location["longitude"]), float(location["latitude"])]
            if not _contains(geometry, point, 0):
                continue
            where = content["precision"]["where"]
            try:
                klass = PRECISION_CLASS.get((where["scheme"], int(where["code"])), "unknown")
            except (TypeError, ValueError):
                klass = "unknown"
            if klass not in included_classes:
                excluded.append({"record_key": key, "coding_source": content["coding_source"], "precision": where,
                                 "precision_class": klass, "reason": f"{klass}-level location excluded by policy {imprecise!r}"})
                continue
            by_coder.setdefault(_family(content["coding_source"]), []).append({
                "record_key": key, "coding_source": content["coding_source"], "coding_status": content["coding_status"],
                "dataset_version": content["dataset_version"], "date_start": content.get("date_start"),
                "date_end": content.get("date_end"), "precision": content["precision"], "precision_class": klass,
                "location": {k: location.get(k) for k in ("latitude", "longitude", "adm_1", "adm_2", "country_code")},
                "location_note": None if klass == "exact" else
                "coordinates are the coder's representative point for a larger unit, not the event site",
                "actors": content.get("actors") or [], "counts": content["counts"], "conflict": content.get("conflict"),
                "revision_used": {"revision_id": revision["revision_id"], "revision": content["revision"],
                                  "as_of": content["as_of"]},
                "citation": citation(revision)})
            if sum(len(v) for v in by_coder.values()) > MAX_EVENTS:
                raise HumanitarianError("too_many_events", f"more than {MAX_EVENTS} events; narrow the area or window")
        for events in by_coder.values():
            events.sort(key=lambda e: (e["date_start"] or "", e["record_key"]))
        return {"contract": EVENTS_CONTRACT, "namespace": namespace, "area": area_view,
                "window": {"start": iso(start_ms), "end": iso(end_ms)}, "as_of": iso(as_of_ms),
                "by_coder": dict(sorted(by_coder.items())),
                "precision_policy": {"policy": imprecise, "included_classes": sorted(included_classes),
                                     "classes": {"exact": "exact point or within about 25 km",
                                                 "admin": "admin-1/admin-2 unit; coordinates are the unit's point",
                                                 "wide": "larger area, country or international"},
                                     "excluded": excluded, "unlocated": unlocated},
                "sources_consulted": [s for s in self._consulted(namespace) if s["source"] != "reliefweb"
                                      and s["source"] != "hdx"],
                "sources_declined": sources_declined(),
                "none_on_record": [] if by_coder else [{"note": "none on record in this area and window"}],
                "notice": "each coder's events side by side with the coder's own precision and counts; nothing is "
                          "merged, summed, deduplicated across coders, estimated or forecast",
                "exclusions": list(EXCLUSIONS)}

    def event_history(self, namespace: str, record_key: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        scopes = set(scopes)
        history = self.store.history(namespace, record_key, scopes=scopes)
        if not history or history[0]["content"]["record_type"] != "conflict_event":
            raise HumanitarianError("not_found", "no conflict event with this key")
        return {"contract": HISTORY_CONTRACT, "namespace": namespace, "record_key": record_key,
                "revisions": [{"seq": h["seq"], "revision_id": h["revision_id"],
                               "predecessor_revision_id": h["predecessor_revision_id"],
                               "coding_source": h["content"]["coding_source"],
                               "coding_status": h["content"]["coding_status"],
                               "dataset_version": h["content"]["dataset_version"], "as_of": h["content"]["as_of"],
                               "changed_fields": h["changed_fields"], "precision": h["content"]["precision"],
                               "counts": h["content"]["counts"], "citation": citation(h)} for h in history],
                "notice": "every release revision as published; a dropped candidate is a revision, not a deletion"}


def to_evidence_bundle(answer: Mapping[str, Any], *, created_at_ms: int | None = None) -> dict[str, Any]:
    """An answer (published_about or conflict_events) as a ``noesis-evidence-bundle-v1``: one object per cited revision."""

    from src.evidence_bundle.builder import EvidenceBundleBuilder

    builder = EvidenceBundleBuilder("receipt", {"operation": answer["contract"], "query": answer.get("query") or
                                                answer.get("area"), "as_of": answer.get("as_of")},
                                    created_at_ms=created_at_ms or 0, as_of_ms=to_ms(answer.get("as_of")))
    refs = []
    items = list(answer.get("items") or []) + [e for events in (answer.get("by_coder") or {}).values() for e in events]
    for item in items:
        cite = item["citation"]
        object_id = f"humanitarian:{cite['record_key']}@{cite['revision_id']}"
        builder.add_object("evidence", {"kind": "humanitarian-record-revision",
                                        "locator": {"cited": True, "document_id": cite["record_key"],
                                                    "revision_id": cite["revision_id"], "url": cite.get("source_url")},
                                        "citation": cite, "revision_used": item["revision_used"]}, object_id=object_id)
        refs.append(object_id)
        if cite.get("source_url"):
            builder.add_external_reference(f"source:{cite['record_key']}", cite["source_url"], required=False)
    builder.add_object("receipt", {k: v for k, v in answer.items() if k not in {"items", "by_coder"}},
                       object_id=f"{answer['contract']}:{digest(refs)[:16]}", references=refs, root=True)
    for gap in answer.get("none_on_record") or []:
        builder.add_omission("none on record: " + ", ".join(f"{k}={v}" for k, v in sorted(gap.items())))
    for declined in answer.get("sources_declined") or []:
        builder.add_omission(f"source {declined['source']} {declined['reason']}")
    return builder.build()
