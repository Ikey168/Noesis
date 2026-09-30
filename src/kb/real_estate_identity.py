"""Match transactions to parcels and places through reviewable identity (#2228, RE07 #2494).

Transactions are projected onto places and parcels without inventing either:

* **exact** - a published identifier is shared: a DVF ``id_parcelle`` equal to a
  parcel's published ``nationalCadastralReference`` (scheme ``fr-id-parcelle``),
  or a published place code (UK postcode or postcode district, INSEE commune,
  ONS GSS, Eurostat GEO) equal to a code a Geospatial place carries in its
  ``source_ids``. Exact matches need no review.
* **candidate** - address- or geometry-derived: a place whose ``uk-address`` or
  ``fr-address`` source id equals the transaction's normalised published
  address, or a parcel whose geometry contains the point of a place the
  transaction is matched to (a receipted ``contains`` relation). Candidates
  are never used until a reviewer accepts them; accepting or rejecting records
  an entity identity decision in :class:`src.kb.entity_history.EntityHistoryStore`
  (the existing review flow the review inbox routes ``entity`` targets to), and
  a decision can be reverted.

Every match pins the transaction and parcel revisions it was made on. A parcel
revised since then is reported as ``parcel_revised_since_match`` and its match
changes only through :meth:`RealEstateIdentity.rematch`, which records a new
match (the old one is kept as ``superseded``). Transactions without a parcel
match stay queryable by their published place codes. No owner is resolved.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterable, Mapping
from typing import Any

from src.kb.real_estate import (
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    RealEstateError,
    RealEstateStore,
    authorize,
    canonical,
    digest,
    table_exists,
)

IDENTITY_CONTRACT = "noesis-real-estate-identity-match-v1"
BASES = ("parcel-identifier", "place-code", "address", "geometry-contains")
EVIDENCE_CLASS = {"parcel-identifier": "exact", "place-code": "exact", "address": "candidate",
                  "geometry-contains": "candidate"}
PLACE_SCHEMES = ("uk-postcode", "uk-postcode-district", "insee-commune", "fr-postcode", "ons-gss", "eurostat-geo")
_ENTITY_HISTORY_SCOPES = {"knowledge:entity-history:write", "knowledge:entity-history:review",
                          "knowledge:entity-history:execute", "knowledge:entity-history:read"}
_GEO_SCOPES = {"knowledge:geospatial:read", "knowledge:geospatial:calculate"}
_DDL = """
CREATE TABLE IF NOT EXISTS real_estate_matches (
  namespace TEXT NOT NULL, match_id TEXT NOT NULL, transaction_id TEXT NOT NULL, target_kind TEXT NOT NULL,
  target_id TEXT NOT NULL, basis TEXT NOT NULL, evidence_class TEXT NOT NULL, state TEXT NOT NULL,
  transaction_revision_id TEXT NOT NULL, target_revision_id TEXT, evidence_json TEXT NOT NULL, decision_id TEXT,
  supersedes TEXT, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL, history_json TEXT NOT NULL,
  PRIMARY KEY(namespace, match_id)
);
"""
USABLE = ("exact", "accepted")


def normalise_address(parts: Iterable[Any]) -> str:
    return re.sub(r"\s+", " ", " ".join(str(p) for p in parts if p).upper()).strip()


def transaction_address(published: Mapping[str, Any]) -> str | None:
    address = published.get("address")
    if isinstance(address, Mapping):
        return normalise_address([address.get("saon"), address.get("paon"), address.get("street"),
                                  address.get("postcode")]) or None
    rows = published.get("addresses") or []
    if len(rows) == 1:
        row = rows[0]
        return normalise_address([row.get("numero"), row.get("suffixe"), row.get("voie"), row.get("code_postal")])
    return None


def places(conn: Any, namespace: str) -> list[dict[str, Any]]:
    """Current Geospatial places with their published source ids and geometries."""
    if not table_exists(conn, "geospatial_place_current"):
        return []
    rows = conn.execute(
        "SELECT r.place_id, r.revision_id, r.canonical_name, r.place_type, r.source_ids_json FROM "
        "geospatial_place_current c JOIN geospatial_place_revisions r ON r.revision_id=c.revision_id WHERE "
        "r.namespace=? ORDER BY r.place_id", [namespace]).fetchall()
    out = []
    for place_id, revision_id, name, kind, source_ids in rows:
        geometry = conn.execute("SELECT geometry_id, geometry_type, coordinates_json FROM geospatial_geometries "
                                "WHERE namespace=? AND place_id=? ORDER BY observed_at_ms DESC, geometry_id LIMIT 1",
                                [namespace, place_id]).fetchone()
        out.append({"place_id": place_id, "revision_id": revision_id, "name": name, "place_type": kind,
                    "source_ids": {str(k): str(v) for k, v in json.loads(source_ids or "{}").items()},
                    "geometry_id": geometry[0] if geometry else None,
                    "geometry": {"type": geometry[1], "coordinates": json.loads(geometry[2])} if geometry else None})
    return out


class RealEstateIdentity:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        from src.kb.entity_history import EntityHistoryStore

        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = RealEstateStore(conn, initialize=initialize, now=self.now)
        self.history = EntityHistoryStore(conn, initialize=initialize, now=self.now)
        if initialize:
            conn.execute(_DDL)

    def _ready(self) -> bool:
        return table_exists(self.conn, "real_estate_matches")

    # ------------------------------------------------------------------ discovery

    def _parcels(self, namespace: str) -> dict[tuple[str, str], dict[str, Any]]:
        out = {}
        for record in self.store.records(namespace, record_type="parcel"):
            revision = self.store.current(namespace, record["record_id"])
            published = revision["statement"]["as_published"]
            out[(published["reference_scheme"], published["national_cadastral_reference"])] = {
                "record_id": record["record_id"], "revision_id": revision["revision_id"], "published": published}
        return out

    def propose(self, namespace: str, *, principal_id: str, scopes: Iterable[str]) -> dict[str, Any]:
        """Record exact matches from shared published identifiers and offer address/geometry candidates."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        self.store.require_ready()
        parcels = self._parcels(namespace)
        known_places = places(self.conn, namespace)
        by_code: dict[tuple[str, str], list[dict[str, Any]]] = {}
        by_address: dict[str, list[dict[str, Any]]] = {}
        for place in known_places:
            for scheme, code in place["source_ids"].items():
                if scheme in PLACE_SCHEMES:
                    by_code.setdefault((scheme, code.upper()), []).append(place)
                if scheme in {"uk-address", "fr-address"}:
                    by_address.setdefault(normalise_address([code]), []).append(place)
        changes = []
        for record in self.store.records(namespace, record_type="transaction"):
            revision = self.store.current(namespace, record["record_id"])
            statement = revision["statement"]
            for ref in statement.get("parcel_refs") or []:
                parcel = parcels.get((ref["scheme"], ref["code"]))
                if parcel is not None:
                    changes.append(self._offer(namespace, record["record_id"], revision, "parcel", parcel["record_id"],
                                               parcel["revision_id"], "parcel-identifier",
                                               {"scheme": ref["scheme"], "code": ref["code"],
                                                "note": "the transaction and the parcel publish the same identifier"},
                                               principal_id))
            for ref in statement.get("place_refs") or []:
                for place in by_code.get((ref["scheme"], str(ref["code"]).upper()), []):
                    changes.append(self._offer(namespace, record["record_id"], revision, "place", place["place_id"],
                                               place["revision_id"], "place-code",
                                               {"scheme": ref["scheme"], "code": ref["code"]}, principal_id))
            address = transaction_address(statement["as_published"])
            for place in by_address.get(address or "", []):
                changes.append(self._offer(namespace, record["record_id"], revision, "place", place["place_id"],
                                           place["revision_id"], "address",
                                           {"address": address, "note": "normalised published address equals the "
                                                                        "place's address source id; needs review"},
                                           principal_id))
        changes += self._geometry_candidates(namespace, parcels, known_places, principal_id)
        return {"contract": IDENTITY_CONTRACT, "recorded": sorted(c["match_id"] for c in changes if c["change"]),
                "matches": self.matches(namespace, scopes=scopes), "unmatched": self.unmatched(namespace,
                                                                                               scopes=scopes),
                "policy": "exact matches share a published identifier; address and geometry matches are candidates "
                          "until reviewed; no owner is resolved"}

    def _geometry_candidates(self, namespace, parcels, known_places, principal_id) -> list[dict[str, Any]]:
        """A parcel containing the point of a place a transaction is exactly or acceptedly matched to."""
        from src.kb.geospatial import GeospatialStore

        geo = GeospatialStore(self.conn)
        points = {p["place_id"]: p for p in known_places if p["geometry"] and p["geometry"]["type"] == "Point"}
        out = []
        for match in self._rows(namespace):
            if match["target_kind"] != "place" or match["state"] not in USABLE or match["target_id"] not in points:
                continue
            place = points[match["target_id"]]
            revision = self.store.current(namespace, match["transaction_id"])
            if revision["statement"].get("parcel_refs"):
                continue  # published parcel ids exist; geometry never overrides them
            for parcel in parcels.values():
                geometry_id = parcel["published"]["geometry"].get("geometry_id")
                if not geometry_id:
                    continue
                relation = geo.relation(namespace, "contains", geometry_id, place["geometry"]["coordinates"],
                                        scopes=_GEO_SCOPES, principal_id=principal_id)
                if relation["result"].get("contains"):
                    out.append(self._offer(
                        namespace, match["transaction_id"], revision, "parcel", parcel["record_id"],
                        parcel["revision_id"], "geometry-contains",
                        {"place_id": place["place_id"], "receipt_id": relation.get("receipt_id"),
                         "note": "the parcel geometry contains the matched place's point; spatial proximity is not "
                                 "an identity, so this needs review"}, principal_id))
        return out

    def _offer(self, namespace, transaction_id, revision, target_kind, target_id, target_revision_id, basis,
               evidence, principal_id) -> dict[str, Any]:
        match_id = "real-estate-match:" + digest([namespace, transaction_id, target_kind, target_id, basis])[:24]
        row = self.conn.execute("SELECT state FROM real_estate_matches WHERE namespace=? AND match_id=?",
                                [namespace, match_id]).fetchone()
        if row is not None:
            return {"match_id": match_id, "change": None}  # never updated silently; see rematch()
        state = "exact" if EVIDENCE_CLASS[basis] == "exact" else "proposed"
        now = self.now()
        self.conn.execute(
            "INSERT INTO real_estate_matches VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, match_id, transaction_id, target_kind, target_id, basis, EVIDENCE_CLASS[basis], state,
             revision["revision_id"], target_revision_id, canonical(evidence), None, None, principal_id, now,
             canonical([{"state": state, "by": principal_id, "at_ms": now, "basis": basis}])])
        return {"match_id": match_id, "change": "created"}

    # ------------------------------------------------------------------ reads

    def _rows(self, namespace: str) -> list[dict[str, Any]]:
        if not self._ready():
            return []
        rows = self.conn.execute("SELECT match_id FROM real_estate_matches WHERE namespace=? ORDER BY match_id",
                                 [namespace]).fetchall()
        return [self.match(namespace, r[0]) for r in rows]

    def match(self, namespace: str, match_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT match_id, transaction_id, target_kind, target_id, basis, evidence_class, state, "
            "transaction_revision_id, target_revision_id, evidence_json, decision_id, supersedes, created_by, "
            "created_at_ms, history_json FROM real_estate_matches WHERE namespace=? AND match_id=?",
            [namespace, match_id]).fetchone() if self._ready() else None
        if row is None:
            raise RealEstateError("not_found", "real-estate match is not visible in this namespace")
        result = dict(zip(("match_id", "transaction_id", "target_kind", "target_id", "basis", "evidence_class",
                           "state", "transaction_revision_id", "target_revision_id"), row[:9]))
        result.update({"contract": IDENTITY_CONTRACT, "evidence": json.loads(row[9]), "decision_id": row[10],
                       "supersedes": row[11], "created_by": row[12], "created_at_ms": int(row[13]),
                       "history": json.loads(row[14])})
        drift = None
        if result["target_kind"] == "parcel":
            current = self.store.current(namespace, result["target_id"])
            if current and current["revision_id"] != result["target_revision_id"]:
                drift = {"pinned_revision_id": result["target_revision_id"], "current_revision_id":
                         current["revision_id"], "note": "the parcel was revised since this match; it changes only "
                                                         "through a recorded re-match"}
        result["parcel_revised_since_match"] = drift
        return result

    def matches(self, namespace: str, *, scopes: Iterable[str], state: str | None = None,
                transaction_id: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        return [m for m in self._rows(namespace) if (state is None or m["state"] == state)
                and (transaction_id is None or m["transaction_id"] == transaction_id)]

    def usable(self, namespace: str, transaction_id: str, target_kind: str) -> list[dict[str, Any]]:
        return [m for m in self._rows(namespace) if m["transaction_id"] == transaction_id
                and m["target_kind"] == target_kind and m["state"] in USABLE]

    def unmatched(self, namespace: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        """Transactions with no usable parcel match, queryable by their published place codes (no invented parcel)."""
        authorize(namespace, set(scopes), READ_SCOPE)
        out = []
        for record in self.store.records(namespace, record_type="transaction"):
            if self.usable(namespace, record["record_id"], "parcel"):
                continue
            statement = self.store.current(namespace, record["record_id"])["statement"]
            out.append({"transaction_id": record["record_id"], "provider": record["provider"],
                        "source_transaction_id": record["record_key"], "place_refs": statement.get("place_refs"),
                        "parcel_refs": statement.get("parcel_refs"),
                        "reason": "published parcel id has no acquired parcel" if statement.get("parcel_refs")
                        else "the source publishes no parcel reference"})
        return out

    # ------------------------------------------------------------------ reviews

    def review(self, namespace: str, match_id: str, decision: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        from src.kb.entities import register_canonical_entity

        scopes = set(scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if decision not in {"accept", "reject"} or not str(reason or "").strip():
            raise RealEstateError("invalid_decision", "accept or reject with a reason")
        match = self.match(namespace, match_id)
        if match["state"] not in {"proposed", "reverted"}:
            raise RealEstateError("invalid_state", f"match is {match['state']}; only candidates are reviewed")
        subjects = [match["transaction_id"], f"{match['target_kind']}:{match['target_id']}"]
        entities = []
        for subject in subjects:
            entity = "ent-real-estate-" + digest([namespace, subject])[:20]
            register_canonical_entity(self.conn, entity, subject, "real_estate_subject")
            self.history.register_entity(namespace, entity, [subject], principal_id=principal_id,
                                         scopes=_ENTITY_HISTORY_SCOPES)
            entities.append(entity)
        recorded = self.history.decide(
            namespace, "match" if decision == "accept" else "non-match", entities,
            {"match_id": match_id, "basis": match["basis"], "evidence": match["evidence"], "reason": reason.strip(),
             "pinned": {"transaction_revision_id": match["transaction_revision_id"],
                        "target_revision_id": match["target_revision_id"]},
             "provenance": {"producer": "geospatial.real-estate", "records": subjects},
             "policy": {"merge": False, "owner_resolution": False}},
            reviewer_id=principal_id, principal_id=principal_id, scopes=_ENTITY_HISTORY_SCOPES,
            event_key=f"real-estate-identity:{namespace}:{match_id}:{len(match['history'])}")
        return self._transition(namespace, match, "accepted" if decision == "accept" else "rejected",
                                recorded["decision_id"], principal_id, reason.strip())

    def revert(self, namespace: str, match_id: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if not str(reason or "").strip():
            raise RealEstateError("invalid_decision", "a revert needs a reason")
        match = self.match(namespace, match_id)
        if match["state"] not in {"accepted", "rejected"}:
            raise RealEstateError("invalid_state", "only an accepted or rejected match can be reverted")
        undo = self.history.undo(namespace, match["decision_id"], reviewer_id=principal_id,
                                 principal_id=principal_id, scopes=_ENTITY_HISTORY_SCOPES)
        return self._transition(namespace, match, "reverted", undo["decision_id"], principal_id, reason.strip())

    def rematch(self, namespace: str, match_id: str, reason: str, *, principal_id: str,
                scopes: Iterable[str]) -> dict[str, Any]:
        """Re-evaluate a parcel match against the parcel's current revision; the old match is superseded."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        match = self.match(namespace, match_id)
        if match["target_kind"] != "parcel" or not match["parcel_revised_since_match"]:
            raise RealEstateError("invalid_state", "only a parcel match whose parcel was revised is re-matched")
        if not str(reason or "").strip():
            raise RealEstateError("invalid_decision", "a re-match needs a reason")
        parcel = self.store.current(namespace, match["target_id"])
        transaction = self.store.current(namespace, match["transaction_id"])
        published = parcel["statement"]["as_published"]
        still = match["basis"] != "parcel-identifier" or any(
            r["code"] == published["national_cadastral_reference"] for r in transaction["statement"]["parcel_refs"])
        self._transition(namespace, match, "superseded", None, principal_id, reason.strip())
        state = ("exact" if still and match["evidence_class"] == "exact" else "proposed" if still else "rejected")
        new_id = "real-estate-match:" + digest([match_id, parcel["revision_id"]])[:24]
        now = self.now()
        self.conn.execute(
            "INSERT INTO real_estate_matches VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, new_id, match["transaction_id"], "parcel", match["target_id"], match["basis"],
             match["evidence_class"], state, transaction["revision_id"], parcel["revision_id"],
             canonical({**match["evidence"], "rematched_from": match_id, "reason": reason.strip(),
                        "identifier_still_shared": still}), None, match_id, principal_id, now,
             canonical([{"state": state, "by": principal_id, "at_ms": now, "rematch_of": match_id}])])
        return {"superseded": match_id, "match": self.match(namespace, new_id)}

    def _transition(self, namespace, match, state, decision_id, principal_id, reason) -> dict[str, Any]:
        history = match["history"] + [{"state": state, "by": principal_id, "reason": reason, "at_ms": self.now(),
                                        "decision_id": decision_id}]
        self.conn.execute("UPDATE real_estate_matches SET state=?, decision_id=coalesce(?, decision_id), "
                          "history_json=? WHERE namespace=? AND match_id=?",
                          [state, decision_id, canonical(history), namespace, match["match_id"]])
        return self.match(namespace, match["match_id"])

    def generation(self, namespace: str) -> int:
        if not self._ready():
            return 0
        return int(self.conn.execute("SELECT count(*) FROM real_estate_matches WHERE namespace=? AND state IN "
                                     "('accepted','rejected','reverted','superseded')", [namespace]).fetchone()[0])


__all__ = ["BASES", "EVIDENCE_CLASS", "IDENTITY_CONTRACT", "RealEstateIdentity", "normalise_address", "places",
           "transaction_address"]
