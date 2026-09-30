"""Place and parcel transaction, index and parcel answers as of a date (#2228, RE09 #2501).

Given a place (a Geospatial place, or a published place code) or a parcel and
an as-of date, the answer lists what was *published* by that date:

* each transaction's revision in force (known from its release's publication
  date), with the revisions known by then - a PPD row withdrawn by then is shown
  as withdrawn, a DVF mutation removed from a later release as removed;
* price-index observations for the place's published geography codes, grouped
  per source and index edition, side by side with unit, base and period as
  published - never averaged, converted, rebased or interpolated;
* parcels (geometry reference, source CRS and revision history) matched to the
  place's transactions through exact or reviewed identity, plus parcels whose
  representative point the place geometry contains (a receipted ``contains``
  relation, as containment context).

Every item cites its source record (provider, publisher, URL, release,
publication date, licence and attribution, revision id). A place or parcel with
nothing on record is reported as ``none_on_record``. No valuation, price
estimate or investment advice is produced, and no owner appears anywhere.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from src.ingestion.real_estate_sources import DVF_NOTICE, EXCLUSIONS
from src.kb.real_estate import READ_SCOPE, RealEstateError, RealEstateStore, authorize, forbidden_keys
from src.kb.real_estate_identity import PLACE_SCHEMES, RealEstateIdentity, places
from src.kb.real_estate_links import RealEstateLinks, containment_context, representative_point

ANSWER_CONTRACT = "noesis-real-estate-answer-v1"
_GEO_SCOPES = {"knowledge:geospatial:read", "knowledge:geospatial:calculate"}


def _as_of(value: str | None) -> str:
    if value is None:
        return datetime.now(tz=UTC).date().isoformat()
    text = str(value)[:10]
    try:
        datetime.fromisoformat(text)
    except ValueError as exc:
        raise RealEstateError("invalid_request", "as_of is a YYYY-MM-DD date") from exc
    return text


def citation(revision: Mapping[str, Any]) -> dict[str, Any]:
    source = dict(revision["statement"].get("source") or {})
    return {"revision_id": revision["revision_id"], "record_key": revision["statement"]["record_key"],
            "provider": source.get("provider"), "publisher": source.get("publisher"), "url": source.get("url"),
            "release": revision["release"], "published_on": revision["published_on"],
            "known_from": revision["known_from"], "licence": source.get("licence"),
            "attribution": source.get("attribution"), "evidence_origin": source.get("evidence_origin")}


def _history(revisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"revision_id": r["revision_id"], "revision_no": r["revision_no"], "event": r["event"],
             "release": r["release"], "known_from": r["known_from"], "supersedes": r["supersedes"]}
            for r in revisions]


def transaction_view(store: RealEstateStore, namespace: str, record_id: str, as_of: str) -> dict[str, Any] | None:
    revisions = store.revisions(namespace, record_id, as_of=as_of)
    if not revisions:
        return None
    current = revisions[-1]
    published = current["statement"]["as_published"]
    return {"record_id": record_id, "provider": current["statement"]["provider"],
            "source_transaction_id": published["source_transaction_id"],
            "status": {"withdrawn": "withdrawn", "removed": "removed"}.get(current["event"], "published"),
            "event": current["event"], "price": published["price"],
            "price_scope": published.get("price_scope", "the price as published for this transaction"),
            "transfer_date": published.get("transfer_date"),
            "attributes": {k: published[k] for k in ("property_type", "tenure", "category", "new_build",
                                                     "nature_mutation", "locals", "lot_counts") if k in published},
            "address": published.get("address") or published.get("addresses"),
            "place_refs": current["statement"]["place_refs"], "parcel_refs": current["statement"]["parcel_refs"],
            "revisions_known": _history(revisions), "later_revisions_exist": len(store.revisions(namespace, record_id))
            > len(revisions), "citation": citation(current)}


def parcel_view(store: RealEstateStore, namespace: str, record_id: str, as_of: str) -> dict[str, Any] | None:
    revisions = store.revisions(namespace, record_id, as_of=as_of)
    if not revisions:
        return None
    current = revisions[-1]
    published = current["statement"]["as_published"]
    return {"record_id": record_id, "provider": current["statement"]["provider"],
            "inspire_id": published["inspire_id"], "national_cadastral_reference":
            published["national_cadastral_reference"], "label": published.get("label"),
            "area_m2": published.get("area_m2"), "valid_from": published.get("valid_from"),
            "geometry": published["geometry"], "revision_history": _history(revisions),
            "citation": citation(current)}


def index_view(store: RealEstateStore, namespace: str, codes: set[tuple[str, str]], as_of: str) -> list[dict]:
    """Index observations per source and index edition, side by side; each observation's revision in force."""
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for record in store.records(namespace, record_type="price_index_observation"):
        revisions = store.revisions(namespace, record["record_id"], as_of=as_of)
        if not revisions:
            continue
        current = revisions[-1]
        published = current["statement"]["as_published"]
        geography = published["geography"]
        if (geography["scheme"], str(geography["code"]).upper()) not in codes:
            continue
        key = (current["statement"]["provider"], published["index_id"], published.get("edition") or "")
        group = groups.setdefault(key, {
            "provider": key[0], "index_id": key[1], "edition": published.get("edition"),
            "unit": published["unit"], "base_period": published.get("base_period"),
            "frequency": published.get("frequency"), "geography": geography, "observations": []})
        group["observations"].append({
            "record_id": record["record_id"], "period": published["period"], "value": published["value"],
            "flags": published.get("flags") or [], "vintage": current["release"],
            "revisions_known": len(revisions), "citation": citation(current)})
    out = []
    for key in sorted(groups):
        group = groups[key]
        group["observations"].sort(key=lambda o: o["period"])
        out.append(group)
    return out


class RealEstateQueries:
    def __init__(self, conn: Any, *, now=None) -> None:
        self.conn = conn
        self.store = RealEstateStore(conn, initialize=False, now=now)
        self.identity = RealEstateIdentity(conn, initialize=True, now=now)
        self.links = RealEstateLinks(conn, initialize=True, now=now)

    def _finish(self, answer: dict[str, Any]) -> dict[str, Any]:
        bad = forbidden_keys(answer)
        if bad:
            raise RealEstateError("forbidden_field", "an answer never carries valuations or party names", paths=bad)
        providers = {t["provider"] for t in answer.get("transactions") or []}
        answer["notices"] = [EXCLUSIONS] + ([DVF_NOTICE] if "dvf" in providers else [])
        return answer

    def place(self, namespace: str, *, scopes: Iterable[str], principal_id: str, as_of: str | None = None,
              place_id: str | None = None, codes: list[Mapping[str, str]] | None = None) -> dict[str, Any]:
        """Transactions, indices and parcels for a Geospatial place or published place codes, as of a date."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        day = _as_of(as_of)
        if bool(place_id) == bool(codes):
            raise RealEstateError("invalid_request", "give a place_id or published place codes")
        place = None
        if place_id:
            place = next((p for p in places(self.conn, namespace) if p["place_id"] == place_id), None)
            if place is None:
                raise RealEstateError("not_found", "place is not visible in this namespace")
            wanted = {(s, c.upper()) for s, c in place["source_ids"].items() if s in PLACE_SCHEMES}
        else:
            wanted = {(str(c["scheme"]), str(c["code"]).upper()) for c in codes or []}
            if any(s not in PLACE_SCHEMES for s, _ in wanted):
                raise RealEstateError("invalid_request", f"place code schemes are {PLACE_SCHEMES}")
        transactions, parcel_ids = [], {}
        for record in self.store.records(namespace, record_type="transaction"):
            view = transaction_view(self.store, namespace, record["record_id"], day)
            if view is None:
                continue
            by_code = {(r["scheme"], str(r["code"]).upper()) for r in view["place_refs"]} & wanted
            by_identity = [m for m in self.identity.usable(namespace, record["record_id"], "place")
                           if place and m["target_id"] == place["place_id"]]
            if not by_code and not by_identity:
                continue
            view["place_match"] = ({"basis": "published-place-code", "codes": sorted(f"{s}:{c}" for s, c in by_code)}
                                   if by_code else {"basis": by_identity[0]["basis"],
                                                    "match_id": by_identity[0]["match_id"],
                                                    "state": by_identity[0]["state"]})
            view["parcel_matches"] = [
                {"parcel_record_id": m["target_id"], "basis": m["basis"], "evidence_class": m["evidence_class"],
                 "state": m["state"], "match_id": m["match_id"], "pinned_parcel_revision_id": m["target_revision_id"],
                 "parcel_revised_since_match": m["parcel_revised_since_match"]}
                for m in self.identity.usable(namespace, record["record_id"], "parcel")]
            for m in view["parcel_matches"]:
                parcel_ids[m["parcel_record_id"]] = f"matched ({m['basis']}, {m['state']})"
            transactions.append(view)
        if place and place["geometry"] and place["geometry"]["type"] in {"Polygon", "MultiPolygon"}:
            from src.kb.geospatial import GeospatialStore

            geo = GeospatialStore(self.conn)
            for record in self.store.records(namespace, record_type="parcel"):
                view = parcel_view(self.store, namespace, record["record_id"], day)
                geometry_id = view and view["geometry"].get("geometry_id")
                if not geometry_id:
                    continue
                point = representative_point(geo.geometry(namespace, geometry_id, scopes=_GEO_SCOPES)["geometry"])
                relation = geo.relation(namespace, "contains", place["geometry_id"], point, scopes=_GEO_SCOPES,
                                        principal_id=principal_id)
                if relation["result"].get("contains"):
                    parcel_ids.setdefault(record["record_id"], f"within the place (receipt {relation['receipt_id']})")
        parcels = []
        for record_id, why in sorted(parcel_ids.items()):
            view = parcel_view(self.store, namespace, record_id, day)
            if view is not None:
                parcels.append({**view, "included_because": why})
        indices = index_view(self.store, namespace, wanted, day)
        transactions.sort(key=lambda t: (t["provider"], t["transfer_date"] or "", t["source_transaction_id"]))
        empty = not (transactions or indices or parcels)
        return self._finish({
            "contract": ANSWER_CONTRACT, "namespace": namespace, "as_of": day,
            "place": {"place_id": place["place_id"], "name": place["name"], "revision_id": place["revision_id"]}
            if place else None, "codes": sorted(f"{s}:{c}" for s, c in wanted),
            "status": "none_on_record" if empty else "answered",
            "transactions": transactions, "indices": indices, "parcels": parcels,
            "side_by_side": "sources, currencies, units and periods as published; nothing averaged, converted or "
                            "interpolated",
            "as_of_rule": "revisions known by the as-of date (release publication date, or retrieval day when a "
                          "source publishes none)"})

    def parcel(self, namespace: str, *, scopes: Iterable[str], principal_id: str, as_of: str | None = None,
               record_id: str | None = None, reference: str | None = None) -> dict[str, Any]:
        """A parcel's revision in force and history, matched transactions, links and containment context."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        day = _as_of(as_of)
        if record_id is None:
            record_id = next((r["record_id"] for r in self.store.records(namespace, record_type="parcel")
                              if self.store.current(namespace, r["record_id"])["statement"]["as_published"]
                              ["national_cadastral_reference"] == reference), None)
        view = parcel_view(self.store, namespace, record_id, day) if record_id else None
        if view is None:
            return self._finish({"contract": ANSWER_CONTRACT, "namespace": namespace, "as_of": day,
                                 "parcel": None, "reference": reference, "status": "none_on_record",
                                 "transactions": [], "links": [], "containment_context": []})
        transactions = []
        for match in self.identity._rows(namespace):
            if match["target_kind"] == "parcel" and match["target_id"] == record_id and match["state"] in (
                    "exact", "accepted"):
                tx = transaction_view(self.store, namespace, match["transaction_id"], day)
                if tx is not None:
                    transactions.append({**tx, "match": {"basis": match["basis"], "state": match["state"],
                                                         "match_id": match["match_id"],
                                                         "parcel_revised_since_match":
                                                             match["parcel_revised_since_match"]}})
        return self._finish({
            "contract": ANSWER_CONTRACT, "namespace": namespace, "as_of": day, "parcel": view,
            "status": "answered", "transactions": transactions,
            "links": self.links.links(namespace, scopes=scopes, record_id=record_id, as_of=day),
            "containment_context": containment_context(self.conn, namespace, record_id, principal_id=principal_id)})


__all__ = ["ANSWER_CONTRACT", "RealEstateQueries", "citation", "index_view", "parcel_view", "transaction_view"]
