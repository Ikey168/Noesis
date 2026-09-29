"""Energy series linked to climate-environment, market and infrastructure records by citation only (EN09).

A link exists only where a source *explicitly* shares an identifier or cites
the other record, and every link stores that basis with the citing source and
its locator:

* **climate-environment**: an ENTSO-E energy series and a Climate and
  Environment ``grid_event`` published in the same ENTSO-E document share the
  document mRID (and zone); the link cites the document and both records.
  The environment store is read through its own API, never modified.
* **market**: a price vintage written through Market storage
  (:mod:`src.kb.energy_market`) is linked to the bars whose source reference
  names the vintage (``source_revision_id``); the market store is read, not
  modified.
* **infrastructure / facility records**: a plant- or unit-level series links
  to a facility only when its subject has an **accepted** EN08 identity match
  to an entity and the facility's owner states the same entity, cited with
  source and locator. A rejected or reverted match blocks the link.
* **explicit citation**: any other record can be linked when a citing source
  and a locator are given.

Nothing is linked by name similarity, co-location or correlation; a request
without a shared identifier or citation is refused, and a series without
links reports ``no links on record``.
"""

from __future__ import annotations

import json

from src.kb.energy_records import READ_SCOPE, WRITE_SCOPE, canonical, digest
from src.kb.energy_store import EnergyStore, EnergyStoreError, authorize, table_exists

CONTRACT = "noesis-energy-link-v1"
_DDL = """
CREATE TABLE IF NOT EXISTS energy_links(
 link_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, series_id TEXT NOT NULL, vintage_id TEXT, relation TEXT NOT NULL,
 target_owner TEXT NOT NULL, target_kind TEXT NOT NULL, target_id TEXT NOT NULL, basis TEXT NOT NULL,
 citation_json TEXT NOT NULL, identity_match_id TEXT, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL);
"""
REFUSED_BASES = ("name similarity", "co-location", "correlation")


def _citation(citation):
    if not isinstance(citation, dict) or not str(citation.get("source") or "").strip() \
            or not str(citation.get("locator") or "").strip():
        raise EnergyStoreError("citation_required", "a link names the citing source and a locator")
    if any(word in str(citation.get("basis") or "").casefold() for word in REFUSED_BASES):
        raise EnergyStoreError("inferred_link_refused", "links are never inferred from names, co-location or correlation")
    return {k: citation.get(k) for k in ("source", "locator", "quote", "basis", "url") if citation.get(k) is not None}


class EnergyLinks:
    def __init__(self, conn, *, initialize=True, now=None):
        self.conn = conn
        self.store = EnergyStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        if initialize:
            conn.execute(_DDL)

    def _insert(self, namespace, series_id, vintage_id, relation, owner, kind, target, basis, citation, match_id,
                principal_id):
        link_id = "energy-link:" + digest([namespace, series_id, vintage_id, relation, owner, kind, target])[:24]
        self.conn.execute("INSERT INTO energy_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                          [link_id, namespace, series_id, vintage_id, relation, owner, kind, target, basis,
                           canonical(citation), match_id, principal_id, self.now()])
        return link_id

    def link_environment(self, namespace, *, environment_namespace, principal_id, scopes, environment_scopes):
        """Link ENTSO-E energy vintages to Climate and Environment grid events of the same ENTSO-E document."""

        from src.kb.environment_store import EnvironmentStore

        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        if not table_exists(self.conn, "environment_records"):
            return {"linked": [], "absent": "no climate-environment records on record"}
        environment = EnvironmentStore(self.conn, initialize=False)
        events = {}
        for record in environment.records(environment_namespace, scopes=environment_scopes, record_type="grid_event",
                                          provider="entsoe"):
            document = (record["content"].get("document") or {})
            key = (document.get("mrid"), document.get("revision"), record["content"]["bidding_zone"].get("code"),
                   (record["content"].get("production_type") or {}).get("code"))
            events.setdefault(key, []).append(record)
        linked = []
        for series in self.store.series(namespace, scopes=scopes, provider="entsoe"):
            for vintage in self.store.vintages(namespace, series["series_id"], scopes=scopes):
                locator = vintage["record"].get("locator") or {}
                key = (locator.get("document_mrid"), locator.get("revision"), series["subject"]["code"],
                       (series["facets"].get("fuel") or {}).get("code"))
                for event in events.get(key, []):
                    citation = {"source": "ENTSO-E Transparency Platform document", "locator":
                                f"mRID {key[0]} revision {key[1]}", "basis": "shared identifier (document mRID + zone + production type)"}
                    linked.append(self._insert(namespace, series["series_id"], vintage["vintage_id"],
                                               "same-published-document", "climate-environment", "grid_event",
                                               event["record_id"], "shared identifier: ENTSO-E document mRID, zone and production type",
                                               citation, None, principal_id))
        return {"linked": sorted(set(linked))}

    def link_market(self, namespace, *, principal_id, scopes):
        """Link price vintages to the market bars whose source references cite them."""

        from src.kb.energy_market import market_refs

        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        linked = []
        for ref in market_refs(self.conn, namespace, scopes=scopes):
            for bar in ref["bars"]:
                citation = {"source": "noesis-market-bar-v1 source_refs", "locator":
                            f"{bar['revision_id']} source_revision_id={ref['vintage_id']}",
                            "basis": "explicit citation: the bar's source reference names the energy vintage"}
                linked.append(self._insert(namespace, ref["series_id"], ref["vintage_id"], "published-as-market-bar",
                                           "market", "price_bar", bar["revision_id"],
                                           "explicit citation (market bar source reference)", citation, None,
                                           principal_id))
        return {"linked": sorted(set(linked))}

    def link_facility(self, namespace, series_id, facility, *, citation, principal_id, scopes):
        """Link a plant/unit series to a facility record through an accepted EN08 identity match only."""

        from src.kb.energy_identity import EnergyIdentity

        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        cited = _citation(citation)
        series = next(iter(self.store.series(namespace, scopes=scopes, series_ids={series_id})), None)
        if series is None:
            raise EnergyStoreError("not_found", "series is unavailable")
        if series["subject"]["kind"] not in {"plant", "unit"}:
            raise EnergyStoreError("not_plant_level", "facility links are for plant- or unit-level series")
        if not isinstance(facility, dict) or not facility.get("owner") or not facility.get("record_id") \
                or not facility.get("entity_id"):
            raise EnergyStoreError("invalid_facility", "facility names its owner, record id and the entity its owner states")
        identity = EnergyIdentity(self.conn, initialize=False)
        matches = identity.matches(namespace, scopes=scopes, subject_code=series["subject"]["code"])
        relevant = [m for m in matches if m["target"]["id"] == facility["entity_id"]]
        accepted = [m for m in relevant if m["state"] == "accepted"]
        if not accepted:
            state = relevant[-1]["state"] if relevant else "no match"
            return {"linked": None, "blocked": True,
                    "reason": f"identity of {series['subject']['code']} to {facility['entity_id']} is {state}; "
                              "facility links require an accepted EN08 identity match"}
        link_id = self._insert(namespace, series_id, None, "same-entity-as-facility", facility["owner"], "facility",
                               facility["record_id"], f"accepted identity match {accepted[0]['match_id']} to "
                                                      f"{facility['entity_id']}", cited, accepted[0]["match_id"],
                               principal_id)
        return {"linked": link_id, "blocked": False}

    def link_cited(self, namespace, series_id, target, *, citation, principal_id, scopes):
        """A link another source explicitly states (citing source and locator are mandatory)."""

        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        cited = _citation(citation)
        if not isinstance(target, dict) or not all(target.get(k) for k in ("owner", "kind", "id")):
            raise EnergyStoreError("invalid_target", "target names owner, kind and id")
        if not self.store.series(namespace, scopes=scopes, series_ids={series_id}):
            raise EnergyStoreError("not_found", "series is unavailable")
        return {"linked": self._insert(namespace, series_id, None, "cited", target["owner"], target["kind"],
                                       target["id"], "explicit citation", cited, None, principal_id)}

    def links(self, namespace, series_id=None, *, scopes):
        authorize(namespace, scopes, READ_SCOPE)
        if not table_exists(self.conn, "energy_links"):
            return {"links": [], "status": "no links on record"}
        rows = self.conn.execute(
            "SELECT link_id, series_id, vintage_id, relation, target_owner, target_kind, target_id, basis, citation_json, "
            "identity_match_id FROM energy_links WHERE namespace=? AND (? IS NULL OR series_id=?) ORDER BY link_id",
            [namespace, series_id, series_id]).fetchall()
        items = []
        for row in rows:
            item = {"contract": CONTRACT, "link_id": row[0], "series_id": row[1], "vintage_id": row[2],
                    "relation": row[3], "target": {"owner": row[4], "kind": row[5], "id": row[6]}, "basis": row[7],
                    "citation": json.loads(row[8]), "identity_match_id": row[9]}
            if row[9]:
                from src.kb.energy_identity import EnergyIdentity

                state = EnergyIdentity(self.conn, initialize=False).match(namespace, row[9])["state"]
                item["identity_state"] = state
                item["active"] = state == "accepted"
            else:
                item["active"] = True
            items.append(item)
        return {"links": items, "status": "linked" if items else "no links on record"}
