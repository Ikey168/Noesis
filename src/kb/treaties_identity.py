"""Treaty participants and treaties matched across sources through reviewable identity (#2581, TR06).

Every subject stays the record its source published: a participant as UNTC,
CELLAR or the Council of Europe names it, a treaty under its own key. Links are
*proposed* into the shared reviewable state machine
(:class:`src.kb.ownership_identity.OwnershipIdentityService`, the one the
sanctions, courts and campaign-finance features use), whose accepted and
reverted decisions are :class:`src.kb.entity_history.EntityHistoryStore`
decisions on canonical entities. Nothing is merged and nothing is accepted
automatically; every candidate carries its method, evidence and confidence.

Methods, published identifiers first:

* ``iso3166-code`` (``exact-identifier``) - a participant's published ISO
  3166-1 code (CELLAR's country authority codes) equal to a
  :mod:`src.kb.geospatial` place's ``iso3166-1-alpha2``/``-alpha3`` source id;
* ``published-cross-reference`` (``cross-referenced-identifier``) - two
  treaties from different sources sharing an identifier one of them publishes
  as its own (a CELLAR title citing ``CETS No. 999`` and the Council of Europe
  treaty 999, a UNTS registration number);
* ``name-as-published`` (``name-jurisdiction``) - only for participants that
  publish **no** code (UNTC and Council of Europe): an equal normalised name
  with a place, or with a participant of another source. Always low
  confidence, never accepted without a reviewer; an EU participant is paired
  only with another EU participant.

Unmatched participants and treaties stay visible as ``unmatched``. No natural
person is ever a subject (TR01).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.kb.treaties_records import (
    READ_SCOPE,
    TreatiesError,
    TreatiesStore,
    authorize,
    table_exists,
)

GEO_READ = "knowledge:geospatial:read"
CODE_SCHEMES = ("iso3166-1-alpha2", "iso3166-1-alpha3")
NOTICE = "a reviewable identity decision; records are never merged and nothing is accepted automatically"


def _norm(value: Any) -> str:
    from src.kb.entities import normalize_surface

    return normalize_surface(str(value or ""))


def entity(key: str) -> str:
    from src.kb.ownership_store import canonical_entity_id

    return canonical_entity_id(key)


def place_key(place_id: str) -> str:
    return f"geospatial:place:{place_id}"


class TreatiesIdentity:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        from src.kb.ownership_identity import OwnershipIdentityService

        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = TreatiesStore(conn, initialize=initialize, now=self.now)
        self.service = OwnershipIdentityService(conn, now=self.now, initialize=initialize)

    # ------------------------------------------------------------------ subjects

    def participants(self, namespace: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        """Participants per source as published, each citing the record revisions it was read from."""
        out: dict[str, dict[str, Any]] = {}
        for row in self.store.records(namespace, scopes=scopes, kinds=["participant"]):
            fields = row["record"]["fields"]
            subject = out.setdefault(row["record_key"], {
                "record_key": row["record_key"], "provider": row["provider"], "entity_id": entity(row["record_key"]),
                "name_as_published": fields.get("name_as_published"),
                "participant_type": fields.get("participant_type"), "codes": {}, "treaties": [], "cited": []})
            for code in fields.get("codes") or []:
                subject["codes"][code["scheme"]] = code["value"]
            subject["treaties"].append(row["treaty_key"])
            subject["cited"].append(row["citation"])
        return sorted(out.values(), key=lambda s: s["record_key"])

    def treaties(self, namespace: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        out = []
        for row in self.store.records(namespace, scopes=scopes, kinds=["treaty"]):
            fields = row["record"]["fields"]
            out.append({"record_key": row["record_key"], "provider": row["provider"],
                        "entity_id": entity(row["record_key"]), "title": fields.get("title_as_published"),
                        "cross_references": fields.get("cross_references") or [], "cited": [row["citation"]]})
        return out

    def _places(self, geo_namespace: str) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "geospatial_place_revisions"):
            return []
        from src.kb.geospatial import READ_SCOPE as GEO_SCOPE
        from src.kb.geospatial import GeospatialStore

        geo = GeospatialStore(self.conn, initialize=False)
        rows = self.conn.execute("SELECT DISTINCT place_id, namespace FROM geospatial_place_revisions WHERE "
                                 "namespace=? ORDER BY place_id", [geo_namespace]).fetchall()
        return [p for p in (geo.place(ns, pid, scopes={GEO_SCOPE}) for pid, ns in rows) if p]

    # ------------------------------------------------------------------ proposals

    def propose(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                geo_namespace: str | None = None) -> dict[str, Any]:
        """Offer reviewable candidates; idempotent, never an automatic merge."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        participants = self.participants(namespace, scopes=scopes)
        offered, unavailable = [], []
        places: list[dict[str, Any]] = []
        if geo_namespace is None:
            unavailable.append({"provider": "geospatial.core", "reason": "no geospatial namespace given"})
        elif GEO_READ not in scopes and "operator" not in scopes:
            unavailable.append({"provider": "geospatial.core", "reason": f"{GEO_READ} is required"})
        else:
            places = self._places(geo_namespace)
            if not places:
                unavailable.append({"provider": "geospatial.core", "reason": "no places in the namespace"})
        for subject in participants:
            if subject["participant_type"] == "eu":
                continue  # the EU is not a place
            for place in places:
                ids = dict(place["source_ids"])
                right = {"record_key": place_key(place["place_id"]), "place_id": place["place_id"],
                         "revision_id": place["revision_id"], "geo_namespace": geo_namespace}
                shared = [s for s in CODE_SCHEMES if subject["codes"].get(s) and ids.get(s) == subject["codes"][s]]
                if shared:
                    offered.append(self._offer(namespace, subject, right["record_key"], "exact-identifier",
                                               "iso3166-code", {"scheme": shared[0],
                                                                "value": subject["codes"][shared[0]],
                                                                "right": right}, principal_id, scopes))
                elif not subject["codes"]:
                    names = {_norm(place.get("canonical_name"))} | {_norm(n.get("value")) for n in
                                                                     place.get("names") or []}
                    if _norm(subject["name_as_published"]) in names:
                        offered.append(self._offer(namespace, subject, right["record_key"], "name-jurisdiction",
                                                   "name-as-published", {
                                                       "value": subject["name_as_published"], "right": right,
                                                       "note": "the source publishes no code; an equal name is a "
                                                               "weak signal and a reviewer decides"},
                                                   principal_id, scopes))
        # participants across sources: EU with EU, and code-less participants by equal names
        for index, left in enumerate(participants):
            for right in participants[index + 1:]:
                if left["provider"] == right["provider"]:
                    continue
                if left["participant_type"] == "eu" or right["participant_type"] == "eu":
                    if left["participant_type"] == right["participant_type"] == "eu":
                        offered.append(self._offer(namespace, left, right["record_key"], "name-jurisdiction",
                                                   "eu-designation", {
                                                       "value": "European Union", "right": self._side(right),
                                                       "note": "both sources name the European Union as the "
                                                               "participant"}, principal_id, scopes))
                    continue
                if left["codes"] and right["codes"]:
                    shared = [s for s in CODE_SCHEMES if left["codes"].get(s) and
                              left["codes"].get(s) == right["codes"].get(s)]
                    if shared:
                        offered.append(self._offer(namespace, left, right["record_key"], "exact-identifier",
                                                   "iso3166-code", {"scheme": shared[0],
                                                                    "value": left["codes"][shared[0]],
                                                                    "right": self._side(right)},
                                                   principal_id, scopes))
                    continue
                if not left["codes"] and not right["codes"] and left["name_as_published"] and \
                        _norm(left["name_as_published"]) == _norm(right["name_as_published"]):
                    offered.append(self._offer(namespace, left, right["record_key"], "name-jurisdiction",
                                               "name-as-published", {
                                                   "value": left["name_as_published"], "right": self._side(right),
                                                   "note": "neither source publishes a code; a reviewer decides"},
                                               principal_id, scopes))
        # treaties across sources through published cross-references only
        treaties = self.treaties(namespace, scopes=scopes)
        for index, left in enumerate(treaties):
            for right in treaties[index + 1:]:
                if left["provider"] == right["provider"]:
                    continue
                ours = {(c["scheme"], c["value"]): c for c in left["cross_references"]}
                for ref in right["cross_references"]:
                    mine = ours.get((ref["scheme"], ref["value"]))
                    if mine and "native identifier" in {mine["basis"], ref["basis"]}:
                        offered.append(self.service.offer(
                            namespace, left_key=left["record_key"], right_key=right["record_key"],
                            left_entity=left["entity_id"], right_entity=right["entity_id"],
                            basis="cross-referenced-identifier",
                            evidence=[{"method": "published-cross-reference", "scheme": ref["scheme"],
                                       "value": ref["value"],
                                       "left": {"record_key": left["record_key"], "as_published": mine["as_published"],
                                                "basis": mine["basis"], "cited": left["cited"]},
                                       "right": {"record_key": right["record_key"],
                                                 "as_published": ref["as_published"], "basis": ref["basis"],
                                                 "cited": right["cited"]}}],
                            principal_id=principal_id, scopes=scopes))
                        break
        return {"proposed": sorted({o["candidate_id"] for o in offered if o["created"] or o.get("change")}),
                "candidates": self.candidates(namespace, scopes=scopes), "unavailable": unavailable,
                "notice": NOTICE}

    @staticmethod
    def _side(subject: Mapping[str, Any]) -> dict[str, Any]:
        return {"record_key": subject["record_key"], "name_as_published": subject["name_as_published"],
                "codes": subject["codes"], "cited": subject["cited"][:3]}

    def _offer(self, namespace, subject, right_key, basis, method, evidence, principal_id, scopes):
        return self.service.offer(
            namespace, left_key=subject["record_key"], right_key=right_key, left_entity=subject["entity_id"],
            right_entity=entity(right_key), basis=basis,
            evidence=[{**evidence, "method": method, "left": self._side(subject)}],
            principal_id=principal_id, scopes=scopes)

    # ------------------------------------------------------------------ review and reads

    @staticmethod
    def view(candidate: Mapping[str, Any]) -> dict[str, Any]:
        last = candidate["history"][-1]
        evidence = candidate["evidence"][0] if candidate["evidence"] else {}
        return {
            "candidate_id": candidate["candidate_id"], "state": candidate["state"],
            "review_state": {"accepted": "reviewed-match", "rejected": "reviewed-non-match",
                             "reverted": "reverted", "proposed": "unreviewed-candidate"}[candidate["state"]],
            "basis": candidate["basis"], "method": evidence.get("method"), "confidence": candidate["confidence"],
            "records": [candidate["left_key"], candidate["right_key"]],
            "entities": [candidate["left_entity"], candidate["right_entity"]],
            "decision_id": candidate["decision_id"],
            "reviewer": last.get("by") if candidate["state"] != "proposed" else None,
            "reason": last.get("reason"), "evidence": candidate["evidence"], "history": candidate["history"],
            "notice": NOTICE,
        }

    def candidates(self, namespace: str, *, scopes: Iterable[str], record_key: str | None = None
                   ) -> list[dict[str, Any]]:
        rows = [c for c in self.service.candidates(namespace, scopes=scopes, record_key=record_key)
                if c["left_key"].startswith("treaties:") or c["right_key"].startswith("treaties:")]
        return [self.view(c) for c in rows]

    def _own(self, namespace: str, candidate_id: str, scopes: Iterable[str]) -> None:
        if not any(c["candidate_id"] == candidate_id for c in self.candidates(namespace, scopes=scopes)):
            raise TreatiesError("not_found", "no treaties identity candidate with that id")

    def review(self, namespace: str, candidate_id: str, decision: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        self._own(namespace, candidate_id, scopes)
        return self.view(self.service.review(namespace, candidate_id, decision, reason, principal_id=principal_id,
                                             scopes=scopes))

    def revert(self, namespace: str, candidate_id: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        self._own(namespace, candidate_id, scopes)
        return self.view(self.service.revert(namespace, candidate_id, reason, principal_id=principal_id,
                                             scopes=scopes))

    def accepted(self, namespace: str, record_key: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        """Accepted, unreverted links of one subject: the other record, method and decision."""
        out = []
        for view in self.candidates(namespace, scopes=scopes, record_key=record_key):
            if view["state"] != "accepted":
                continue
            other = view["records"][1] if view["records"][0] == record_key else view["records"][0]
            out.append({"candidate_id": view["candidate_id"], "record_key": other, "basis": view["basis"],
                        "method": view["method"], "confidence": view["confidence"],
                        "decision_id": view["decision_id"], "reviewer": view["reviewer"]})
        return out

    def equivalents(self, namespace: str, key: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        """Participant or treaty keys reached from a key (a participant, a treaty or ``geospatial:place:<id>``)
        through accepted, unreverted matches, one hop and through a shared place. Each carries its path."""
        try:
            first = self.accepted(namespace, key, scopes=scopes)
        except Exception:  # noqa: BLE001 - identity access is optional for an answer
            return []
        out: dict[str, dict[str, Any]] = {}
        for link in first:
            if link["record_key"].startswith("treaties:"):
                out.setdefault(link["record_key"], {"record_key": link["record_key"], "path": [link]})
            if link["record_key"].startswith("geospatial:place:"):
                for second in self.accepted(namespace, link["record_key"], scopes=scopes):
                    if second["record_key"].startswith("treaties:") and second["record_key"] != key:
                        out.setdefault(second["record_key"], {"record_key": second["record_key"],
                                                              "path": [link, second]})
        return sorted(out.values(), key=lambda e: e["record_key"])

    def identity(self, namespace: str, record_key: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        """Accepted links and open candidates for one subject; without an accepted link it is unmatched."""
        try:
            views = self.candidates(namespace, scopes=scopes, record_key=record_key)
        except Exception as exc:  # noqa: BLE001 - identity access is optional for an answer
            return {"state": "unmatched", "links": [], "candidates": [], "unavailable": [getattr(exc, "code", "x")]}
        links = [v for v in views if v["state"] == "accepted"]
        return {"state": "matched" if links else "unmatched",
                "links": [{"candidate_id": v["candidate_id"], "records": v["records"], "method": v["method"],
                           "confidence": v["confidence"], "reviewer": v["reviewer"]} for v in links],
                "candidates": [v["candidate_id"] for v in views if v["state"] == "proposed"]}

    def unmatched(self, namespace: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        """Participants and treaties without an accepted link; they stay visible as unmatched."""
        scopes = set(scopes)
        participants = [{"record_key": s["record_key"], "name_as_published": s["name_as_published"],
                         "provider": s["provider"], "state": "unmatched"}
                        for s in self.participants(namespace, scopes=scopes)
                        if self.identity(namespace, s["record_key"], scopes=scopes)["state"] == "unmatched"]
        treaties = [{"record_key": t["record_key"], "title": t["title"], "provider": t["provider"],
                     "state": "unmatched"}
                    for t in self.treaties(namespace, scopes=scopes)
                    if self.identity(namespace, t["record_key"], scopes=scopes)["state"] == "unmatched"]
        return {"participants": participants, "treaties": treaties, "notice": NOTICE}
