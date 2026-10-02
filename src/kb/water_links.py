"""Stations and water bodies linked to other packs by citation, shared identifier or accepted match (WA07 #2617).

A link is created only on one of three bases, as in
:mod:`src.kb.hazards_links`, and records which:

* ``citation`` - the other record quotes the water record's published
  identifier verbatim (a flood event naming a gauge number, a station UUID or
  an EU water-body code); the quoted text is kept as evidence;
* ``shared_identifier`` - both records publish the same identifier (an
  infrastructure asset stating an EU water-body code, a weather source stating
  the gauge it sits beside);
* ``accepted_match`` - a WA06 station or water-body to place match a reviewer
  accepted.

Every link points at specific revisions on both sides (the water record
revision and the target's revision id). Target owners that are absent from
this deployment, not readable with the caller's scopes, or that declare no
matching asset class (the infrastructure registry declares no dam or waterway
class today) are reported as ``unavailable`` with the reason - never silently
skipped. Proximity is never a basis: no link places a weather station "near" a
gauge, and no link asserts that an observation caused or indicated an event.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterable, Mapping
from typing import Any

from src.kb.water_records import (
    LINK_CONTRACT,
    READ_SCOPE,
    WRITE_SCOPE,
    authorize,
    canonical,
    digest,
)
from src.kb.water_store import WaterStore, table_exists

BASES = ("citation", "shared_identifier", "accepted_match")
NO_CAUSATION = "a citation, shared identifier or accepted match only; no causal, proximity or impact claim is made"
TARGETS = {
    "hazards": {"table": "hazard_record_revisions", "scope": "knowledge:hazards:read", "provider": "hazards.core"},
    "weather": {"table": "weather_station_identifiers", "scope": "knowledge:weather:read",
                "provider": "weather.core"},
    "infrastructure": {"table": "infra_asset_revisions", "scope": "knowledge:infrastructure:read",
                       "provider": "geospatial.infrastructure"},
    "places": {"table": "water_identity_matches", "scope": READ_SCOPE, "provider": "geospatial.core"},
}
WATER_ASSET_CLASSES = ("dam", "waterway", "lock", "weir")
_DDL = """
CREATE TABLE IF NOT EXISTS water_links (
  namespace TEXT NOT NULL, link_id TEXT NOT NULL, record_id TEXT NOT NULL, revision_id TEXT NOT NULL,
  subject_key TEXT NOT NULL, target_kind TEXT NOT NULL, target_id TEXT NOT NULL, target_revision TEXT NOT NULL,
  basis TEXT NOT NULL, evidence_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, link_id)
);
"""


def _strings(value: Any, path: str = "") -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            out += _strings(item, f"{path}.{key}" if path else str(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            out += _strings(item, f"{path}[{index}]")
    elif isinstance(value, str):
        out.append((path, value))
    return out


def identifier_tokens(published: Mapping[str, Any], record_type: str) -> dict[str, str]:
    """Published identifiers specific enough to be cited verbatim: token -> scheme (never shortened)."""
    tokens: dict[str, str] = {}
    if record_type == "station":
        tokens[str(published["native_id"])] = "station-id"
        if published.get("number"):
            tokens[str(published["number"])] = "station-number"
    else:
        tokens[str(published["eu_code"])] = "eu-water-body-code"
    for item in published.get("related_identifiers") or []:
        tokens[str(item.get("value"))] = str(item.get("scheme"))
    return {t: s for t, s in tokens.items() if len(t) >= 8}


def cited(content: Any, token: str) -> list[dict[str, str]]:
    """Places where ``token`` appears verbatim as a whole value or a whole word in a string field."""
    hits = []
    pattern = re.compile(r"(?<![A-Za-z0-9_-])" + re.escape(token) + r"(?![A-Za-z0-9_-])")
    for path, text in _strings(content):
        if text == token or pattern.search(text):
            hits.append({"field": path, "quoted": text[:300]})
    return hits


def linked_providers(conn: Any, scopes: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Which target owners exist and are readable here; a missing one is reported, not skipped."""
    from src.kb.infrastructure_assets import ASSET_CLASSES

    scopes = set(scopes)
    state = {}
    for name, spec in TARGETS.items():
        if not table_exists(conn, spec["table"]):
            state[name] = {"status": "unavailable", "provider": spec["provider"],
                           "reason": f"{spec['provider']} has no records in this deployment"}
        elif spec["scope"] not in scopes and "operator" not in scopes:
            state[name] = {"status": "unavailable", "provider": spec["provider"],
                           "reason": f"{spec['scope']} is required to read {spec['provider']} records"}
        else:
            state[name] = {"status": "available", "provider": spec["provider"], "reason": None}
    missing = [c for c in WATER_ASSET_CLASSES if c not in ASSET_CLASSES]
    state["infrastructure"]["water_asset_classes"] = {
        "declared": [c for c in WATER_ASSET_CLASSES if c in ASSET_CLASSES], "missing": missing,
        "note": ("the infrastructure registry declares no " + ", ".join(missing) + " asset class; dams and "
                 "waterways are linked only when an asset of another class publishes a shared identifier")
        if missing else None}
    if missing:
        state["dams-and-waterways"] = {"status": "unavailable", "provider": TARGETS["infrastructure"]["provider"],
                                       "reason": state["infrastructure"]["water_asset_classes"]["note"]}
    return state


class WaterLinks:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = WaterStore(conn, initialize=initialize, now=self.now)
        if initialize:
            conn.execute(_DDL)

    def _ready(self) -> bool:
        return table_exists(self.conn, "water_links")

    def _insert(self, namespace, subject, target_kind, target_id, target_revision, basis, evidence,
                principal_id) -> bool:
        revision = subject["revision"]
        link_id = "water-link:" + digest([namespace, revision["revision_id"], target_kind, target_id,
                                          target_revision, basis])[:24]
        inserted = self.conn.execute(
            "INSERT INTO water_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING RETURNING link_id",
            [namespace, link_id, subject["record"]["record_id"], revision["revision_id"], subject["record"][
                "subject_key"], target_kind, target_id, target_revision, basis,
             canonical({**evidence, "notice": NO_CAUSATION}), principal_id, self.now()]).fetchall()
        return bool(inserted)

    def _subjects(self, namespace):
        from src.kb.water_identity import WaterIdentity

        return WaterIdentity(self.conn, initialize=False).subjects(namespace)

    # ------------------------------------------------------------------ targets

    def _hazards(self, namespace):
        rows = self.conn.execute(
            "SELECT r.record_id, r.provider, r.native_id, r.hazard_type, v.revision_id, v.content_json "
            "FROM hazard_records r JOIN hazard_record_revisions v ON v.record_id=r.record_id WHERE r.namespace=? "
            "AND v.revision=(SELECT max(revision) FROM hazard_record_revisions x WHERE x.record_id=r.record_id) "
            "ORDER BY r.record_id", [namespace]).fetchall()
        return [{"record_id": r[0], "provider": r[1], "native_id": r[2], "hazard_type": r[3], "revision_id": r[4],
                 "content": json.loads(r[5])} for r in rows]

    def _infrastructure(self, namespace):
        rows = self.conn.execute(
            "SELECT a.asset_id, a.asset_class, v.revision_id, v.record_json FROM infra_assets a JOIN "
            "infra_asset_revisions v ON v.asset_id=a.asset_id WHERE a.namespace=? AND v.sequence=(SELECT "
            "max(sequence) FROM infra_asset_revisions x WHERE x.asset_id=a.asset_id) ORDER BY a.asset_id",
            [namespace]).fetchall()
        return [{"asset_id": r[0], "asset_class": r[1], "revision_id": r[2], "record": json.loads(r[3])}
                for r in rows]

    def _weather(self, namespace):
        rows = self.conn.execute(
            "SELECT station_key, scheme, value, stated_by, first_seen_ms FROM weather_station_identifiers WHERE "
            "namespace=? ORDER BY station_key, scheme, value", [namespace]).fetchall()
        out = []
        for station, scheme, value, stated_by, first_seen in rows:
            revision = None
            if table_exists(self.conn, "weather_revisions"):
                revision = self.conn.execute(
                    "SELECT v.revision_id FROM weather_records r JOIN weather_revisions v ON v.record_id=r.record_id "
                    "WHERE r.namespace=? AND r.subject_key=? ORDER BY v.seq DESC LIMIT 1",
                    [namespace, station]).fetchone()
            out.append({"station": station, "scheme": scheme, "value": value, "stated_by": stated_by,
                        "revision": revision[0] if revision else f"identifier-statement:{stated_by}:{first_seen}"})
        return out

    # ------------------------------------------------------------------ linking

    def link(self, namespace: str, *, principal_id: str, scopes: Iterable[str]) -> dict[str, Any]:
        """Link current stations and water bodies to hazards, weather, infrastructure and accepted places."""
        from src.kb.water_identity import WaterIdentity

        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        self.store.require_ready()
        providers = linked_providers(self.conn, scopes)
        subjects = self._subjects(namespace)
        counts = {basis: 0 for basis in BASES}
        hazards = self._hazards(namespace) if providers["hazards"]["status"] == "available" else []
        assets = self._infrastructure(namespace) if providers["infrastructure"]["status"] == "available" else []
        weather = self._weather(namespace) if providers["weather"]["status"] == "available" else []
        identity = WaterIdentity(self.conn, initialize=False)
        for key, subject in sorted(subjects.items()):
            tokens = identifier_tokens(subject["published"], subject["kind"])
            for event in hazards:
                shared = {t: s for t, s in tokens.items()
                          if t in {str(v) for v in dict(event["content"].get("identifiers") or {}).values()}}
                for token, scheme in sorted(tokens.items()):
                    hits = cited(event["content"], token)
                    if not hits:
                        continue
                    basis = "shared_identifier" if token in shared else "citation"
                    counts[basis] += self._insert(
                        namespace, subject, "hazard", event["record_id"], event["revision_id"], basis,
                        {"token": token, "scheme": scheme, "hazard_type": event["hazard_type"],
                         "hazard_provider": event["provider"], "hazard_native_id": event["native_id"],
                         "quotes": hits}, principal_id)
            for asset in assets:
                published_ids = {(i["scheme"], i["value"]) for i in asset["record"].get("identifiers") or []}
                published_ids |= {(r.get("scheme"), r.get("identifier"))
                                  for r in asset["record"].get("cited_references") or []}
                for token, scheme in sorted(tokens.items()):
                    same = sorted(s for s, v in published_ids if v == token)
                    if same:
                        counts["shared_identifier"] += self._insert(
                            namespace, subject, "infrastructure", asset["asset_id"], asset["revision_id"],
                            "shared_identifier", {"token": token, "scheme": scheme, "stated_as": same,
                                                  "asset_class": asset["asset_class"]}, principal_id)
            if subject["kind"] == "station":
                for item in weather:
                    if item["value"] in tokens:
                        counts["shared_identifier"] += self._insert(
                            namespace, subject, "weather-station", item["station"], item["revision"],
                            "shared_identifier", {"token": item["value"], "scheme": item["scheme"],
                                                  "stated_by": item["stated_by"],
                                                  "basis": "the weather source publishes this gauge's identifier"},
                            principal_id)
            for match in identity.accepted(namespace, subject_key=key):
                counts["accepted_match"] += self._insert(
                    namespace, subject, "place", match["place_id"], match["evidence"]["place_revision_id"],
                    "accepted_match", {"match_id": match["match_id"], "method": match["method"],
                                       "relation": match["relation"], "reviewer": match["reviewer"],
                                       "geometry_id": match["evidence"].get("geometry_id")}, principal_id)
        return {"contract": LINK_CONTRACT, "created": counts, "providers": providers,
                "unavailable": {k: v for k, v in providers.items() if v["status"] != "available"},
                "links": self.links(namespace, scopes=scopes), "notice": NO_CAUSATION}

    # ------------------------------------------------------------------ reads

    def links(self, namespace: str, *, scopes: Iterable[str], subject_key: str | None = None,
              target_kind: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not self._ready():
            return []
        rows = self.conn.execute(
            "SELECT link_id, record_id, revision_id, subject_key, target_kind, target_id, target_revision, basis, "
            "evidence_json, created_by FROM water_links WHERE namespace=? AND (? IS NULL OR subject_key=?) AND "
            "(? IS NULL OR target_kind=?) ORDER BY subject_key, target_kind, target_id, link_id",
            [namespace, subject_key, subject_key, target_kind, target_kind]).fetchall()
        return [{"contract": LINK_CONTRACT, **dict(zip(("link_id", "record_id", "revision_id", "subject_key",
                                                        "target_kind", "target_id", "target_revision", "basis"),
                                                       r[:8])),
                 "evidence": json.loads(r[8]), "created_by": r[9]} for r in rows]

    def generation(self, namespace: str) -> int:
        if not self._ready():
            return 0
        return int(self.conn.execute("SELECT count(*) FROM water_links WHERE namespace=?", [namespace]).fetchone()[0])


__all__ = ["BASES", "NO_CAUSATION", "WaterLinks", "cited", "identifier_tokens", "linked_providers"]
