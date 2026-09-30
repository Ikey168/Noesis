"""Stations and water bodies linked to other packs by citation, shared identifier or accepted match (WA07 #2617).

Following :mod:`src.kb.hazards_links`, a link is created only on an explicit
basis, and records which:

* ``citation`` - a hazard record (e.g. a flood event or notification) or a
  document quotes the station's or water body's published identifier; the
  quote is kept as evidence.
* ``published_relation`` - a weather station identifier statement names the
  water station's published identifier (the weather source states the
  relation); water stations are never linked to weather stations by distance.
* ``shared_identifier`` - an infrastructure asset (dam, waterway or plant in
  the infrastructure registry) publishes the same identifier (scheme and
  value) as the water record.
* ``accepted_match`` - a WA06 station- or water-body-to-place match a reviewer
  accepted.

Every link points at the water record revision and at the target's revision
(hazard revision, document content hash, weather identifier statement,
infrastructure asset revision, place revision). Target owners missing from the
deployment are reported as ``unavailable`` with the reason, never silently
skipped. Co-occurrence in time or space is not a basis: no link asserts that
an observation caused, preceded or measured an event.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable
from typing import Any

from src.kb.water_records import (
    LINK_CONTRACT,
    READ_SCOPE,
    WRITE_SCOPE,
    WaterError,
    authorize,
    canonical,
    digest,
)
from src.kb.water_store import WaterStore, table_exists

BASES = ("citation", "published_relation", "shared_identifier", "accepted_match")
NO_CAUSATION = ("a citation, published relation, shared identifier or accepted match only; no causal, attribution "
                "or impact claim is made")
MIN_TOKEN = 6
_DDL = """
CREATE TABLE IF NOT EXISTS water_links (
  namespace TEXT NOT NULL, link_id TEXT NOT NULL, record_id TEXT NOT NULL, revision_id TEXT NOT NULL,
  target_kind TEXT NOT NULL, target_namespace TEXT NOT NULL, target_id TEXT NOT NULL, target_revision TEXT NOT NULL,
  basis TEXT NOT NULL, evidence_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, link_id)
);
"""


def identifiers(published: dict[str, Any], record_type: str) -> list[dict[str, str]]:
    """Published identifiers of a station or water body (scheme and value), exactly as published."""
    ids = [{"scheme": str(i["scheme"]), "value": str(i["value"])} for i in published.get("identifiers") or []]
    if record_type == "water_body":
        ids.append({"scheme": "eu-water-body", "value": published["eu_code"]})
    return sorted({(i["scheme"], i["value"]): i for i in ids}.values(), key=lambda i: (i["scheme"], i["value"]))


def providers(conn: Any) -> dict[str, dict[str, Any]]:
    """Which target owners exist here; a missing one is reported, not skipped."""

    def state(available: bool, reason: str) -> dict[str, Any]:
        return {"status": "available" if available else "unavailable", "reason": None if available else reason}

    return {
        "natural-hazards": state(table_exists(conn, "hazard_record_revisions"),
                                 "natural-hazards provider (hazards.core) has no records in this deployment"),
        "documents": state(table_exists(conn, "documents"), "document store not initialised"),
        "weather": state(table_exists(conn, "weather_station_identifiers"),
                         "weather provider has no station identifier statements in this deployment"),
        "infrastructure": {**state(table_exists(conn, "infra_asset_revisions"),
                                   "infrastructure asset registry has no records in this deployment"),
                           "coverage_note": "the infrastructure registry has no dam or waterway asset class; only "
                                            "assets publishing a water identifier can be linked"},
        "places (accepted matches)": state(table_exists(conn, "water_identity_matches"),
                                           "no station or water-body match has been proposed yet"),
    }


class WaterLinks:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = WaterStore(conn, initialize=initialize, now=self.now)
        if initialize:
            conn.execute(_DDL)

    def _ready(self) -> bool:
        return table_exists(self.conn, "water_links")

    def _insert(self, namespace, record, revision, target, basis, evidence, principal_id) -> dict[str, Any]:
        if basis not in BASES:
            raise WaterError("invalid_basis", f"a link's basis is one of {BASES}; co-occurrence is not a basis")
        if not target.get("revision"):
            raise WaterError("revision_required", "links point at a specific target revision")
        link_id = "water-link:" + digest([namespace, revision["revision_id"], target["kind"],
                                          target.get("namespace") or namespace, target["id"], target["revision"],
                                          basis])[:24]
        self.conn.execute("INSERT INTO water_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                          [namespace, link_id, record["record_id"], revision["revision_id"], target["kind"],
                           target.get("namespace") or namespace, target["id"], target["revision"], basis,
                           canonical(evidence), principal_id, self.now()])
        return self._link(namespace, link_id)

    def _subjects(self, namespace):
        for record in self.store.records(namespace):
            if record["record_type"] not in {"station", "water_body"}:
                continue
            revision = self.store.current(namespace, record["record_id"])
            if revision and revision["event"] == "published":
                yield record, revision

    def discover(self, namespace: str, *, principal_id: str, scopes: Iterable[str]) -> dict[str, Any]:
        """Create links from citations, published relations, shared identifiers and accepted matches only."""
        from src.kb.water_identity import WaterIdentity

        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        self.store.require_ready()
        state = providers(self.conn)
        created = []
        weather = self._weather_identifiers(namespace) if state["weather"]["status"] == "available" else []
        assets = self._assets(namespace) if state["infrastructure"]["status"] == "available" else []
        identity = WaterIdentity(self.conn, initialize=False, now=self.now)
        # The runtime keeps each acquired page as a document too; those are this pack's own records, not citations.
        own = sorted({r["source_id"] for r in self.store.runs(namespace)}) or [""]
        for record, revision in self._subjects(namespace):
            published = revision["statement"]["as_published"]
            ids = identifiers(published, record["record_type"])
            tokens = sorted({i["value"] for i in ids if len(i["value"]) >= MIN_TOKEN})
            if state["natural-hazards"]["status"] == "available":
                for token in tokens:
                    for hazard_id, hazard_revision, hazard_type, content in self.conn.execute(
                            "SELECT r.record_id, r.revision_id, h.hazard_type, r.content_json FROM "
                            "hazard_record_revisions r JOIN hazard_records h USING(record_id) WHERE r.namespace=? AND "
                            "position(? IN r.content_json) > 0 ORDER BY r.record_id, r.revision", [namespace, token]
                    ).fetchall():
                        at = content.find(token)
                        created.append(self._insert(
                            namespace, record, revision, {"kind": "hazard-record", "id": hazard_id,
                                                          "revision": hazard_revision}, "citation",
                            {"cited_identifier": token, "hazard_type": hazard_type,
                             "quote": content[max(0, at - 80): at + len(token) + 80]}, principal_id))
            if state["documents"]["status"] == "available":
                for token in tokens:
                    for document_id, content_hash, source_type, title, content in self.conn.execute(
                            "SELECT document_id, content_hash, source_type, title, content FROM documents WHERE "
                            "(position(? IN coalesce(content, '')) > 0 OR position(? IN coalesce(title, '')) > 0) "
                            f"AND coalesce(source_id, '') NOT IN ({', '.join('?' for _ in own)}) "
                            "ORDER BY document_id LIMIT 50", [token, token, *own]).fetchall():
                        text = content or title or ""
                        at = text.find(token)
                        created.append(self._insert(
                            namespace, record, revision, {"kind": "document", "id": document_id,
                                                          "revision": content_hash}, "citation",
                            {"cited_identifier": token, "source_type": source_type,
                             "quote": text[max(0, at - 80): at + len(token) + 80] if at >= 0 else title},
                            principal_id))
            pairs = {(i["scheme"], i["value"]) for i in ids}
            for item in weather:
                if (item["scheme"], item["value"]) in pairs:
                    created.append(self._insert(
                        namespace, record, revision, {"kind": "weather-station", "id": item["station"],
                                                      "revision": item["statement_id"]}, "published_relation",
                        {"identifier": {"scheme": item["scheme"], "value": item["value"]},
                         "stated_by": item["stated_by"], "locator": item["locator"],
                         "note": "the weather source states this identifier; no distance is used"}, principal_id))
            for asset in assets:
                shared = sorted(pairs & asset["pairs"])
                if shared:
                    created.append(self._insert(
                        namespace, record, revision, {"kind": "infrastructure-asset", "id": asset["asset_id"],
                                                      "revision": asset["revision_id"]}, "shared_identifier",
                        {"identifiers": [{"scheme": s, "value": v} for s, v in shared],
                         "asset_class": asset["asset_class"], "asset_name": asset["name"]}, principal_id))
            for match in identity.accepted(namespace, subject_key=record["subject_key"]):
                created.append(self._insert(
                    namespace, record, revision, {"kind": "place", "id": match["place_id"],
                                                  "revision": match["place_revision_id"]}, "accepted_match",
                    {"match_id": match["match_id"], "relation": match["relation"], "method": match["method"],
                     "reviewer": match["reviewer"], "matched_subject_revision_id": match["subject_revision_id"]},
                    principal_id))
        unavailable = {k: v for k, v in state.items() if v["status"] != "available"}
        return {"contract": LINK_CONTRACT, "links": sorted({x["link_id"]: x for x in created}.values(),
                                                            key=lambda x: x["link_id"]),
                "providers": state, "unavailable_providers": unavailable, "claims": NO_CAUSATION}

    def _weather_identifiers(self, namespace: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT station_key, scheme, value, stated_by, locator_json, first_seen_ms, run_id FROM "
            "weather_station_identifiers WHERE namespace IN (?, 'environment') ORDER BY station_key, scheme, value",
            [namespace]).fetchall()
        return [{"station": r[0], "scheme": r[1], "value": r[2], "stated_by": r[3], "locator": json.loads(r[4]),
                 "statement_id": "weather-identifier:" + digest([r[0], r[1], r[2], r[3]])[:24]} for r in rows]

    def _assets(self, namespace: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT a.asset_id, a.asset_class, r.revision_id, r.record_json FROM infra_assets a JOIN "
            "infra_asset_revisions r USING(asset_id) WHERE a.namespace=? AND r.sequence = (SELECT max(sequence) FROM "
            "infra_asset_revisions x WHERE x.asset_id=a.asset_id) ORDER BY a.asset_id", [namespace]).fetchall()
        result = []
        for asset_id, asset_class, revision_id, record_json in rows:
            value = json.loads(record_json)
            pairs = {(str(i.get("scheme")), str(i.get("value"))) for i in value.get("identifiers") or []
                     if isinstance(i, dict)}
            result.append({"asset_id": asset_id, "asset_class": asset_class, "revision_id": revision_id,
                           "name": value.get("name"), "pairs": pairs})
        return result

    # ------------------------------------------------------------------ reads

    def _link(self, namespace: str, link_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT record_id, revision_id, target_kind, target_namespace, target_id, target_revision, basis, "
            "evidence_json, created_by, created_at_ms FROM water_links WHERE namespace=? AND link_id=?",
            [namespace, link_id]).fetchone()
        return {"contract": LINK_CONTRACT, "link_id": link_id, "namespace": namespace, "record_id": row[0],
                "record_revision_id": row[1], "target": {"kind": row[2], "namespace": row[3], "id": row[4],
                                                         "revision": row[5]},
                "basis": row[6], "evidence": json.loads(row[7]), "created_by": row[8], "created_at_ms": int(row[9]),
                "claims": NO_CAUSATION}

    def links(self, namespace: str, *, scopes: Iterable[str], record_id: str | None = None,
              target_id: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not self._ready():
            return []
        rows = self.conn.execute(
            "SELECT link_id FROM water_links WHERE namespace=? AND (? IS NULL OR record_id=?) AND "
            "(? IS NULL OR target_id=?) ORDER BY record_id, target_kind, target_id, link_id",
            [namespace, record_id, record_id, target_id, target_id]).fetchall()
        return [self._link(namespace, r[0]) for r in rows]

    def generation(self, namespace: str) -> int:
        if not self._ready():
            return 0
        return int(self.conn.execute("SELECT count(*) FROM water_links WHERE namespace=?", [namespace]).fetchone()[0])


__all__ = ["BASES", "NO_CAUSATION", "WaterLinks", "identifiers", "providers"]
