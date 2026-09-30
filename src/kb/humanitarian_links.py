"""Humanitarian records linked to news, OSINT, population and hazard records by citation (HR08, #2266).

A link exists only on one of three recorded bases:

* ``explicit-citation`` - the humanitarian source cites the target (a news
  article, an OSINT event dossier, a population series): the declaring
  principal supplies the citation locator (and optionally the quoted passage);
* ``shared-identifier`` - both sides publish the same identifier (a GLIDE
  number on a ReliefWeb disaster and on a Natural Hazards event, #2207);
* ``accepted-identity-match`` - an HR07 place assertion that a reviewer
  accepted ties a published place reference to the geospatial place a
  population series is published for.

Every link points at a specific *revision* on both sides (the humanitarian
record revision, and the target's document revision, dossier revision or
series vintage). A target that cannot be found is kept as a ``broken`` link
with its reason, never dropped. Population denominators are attached by
citation with their vintage; no per-capita rate or other figure is computed,
and nothing is inferred about causes or impacts from co-location.

Targets are read through their existing owners (``documents`` /
``document_revision_records`` for news, ``event_dossier_revisions`` for OSINT
dossiers, ``demographic_series`` / ``demographic_vintages`` for population,
and the Natural Hazards event store when it is installed); no copy is made.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from src.kb.humanitarian_records import READ_SCOPE, WRITE_SCOPE, HumanitarianError, canonical, digest
from src.kb.humanitarian_store import HumanitarianStore, authorize, table_exists

CONTRACT = "noesis-humanitarian-link-v1"
TARGET_KINDS = ("news-article", "osint-event-dossier", "population-series", "hazard-event")
BASES = ("explicit-citation", "shared-identifier", "accepted-identity-match")
NO_INFERENCE = ("a citation link: it does not assert a cause, an impact or a rate; population figures are "
                "denominators by citation with their vintage and no per-capita value is computed")
HAZARD_TABLES = ("hazard_event_revisions", "hazard_events")
_DDL = """
CREATE TABLE IF NOT EXISTS humanitarian_links(
 namespace TEXT NOT NULL, link_id TEXT NOT NULL, record_key TEXT NOT NULL, record_revision_id TEXT NOT NULL,
 target_kind TEXT NOT NULL, target_id TEXT NOT NULL, target_revision TEXT, basis TEXT NOT NULL,
 evidence_json TEXT NOT NULL, state TEXT NOT NULL, reason TEXT, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
 PRIMARY KEY(namespace, link_id));
"""


class HumanitarianLinks:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        self.conn = conn
        self.store = HumanitarianStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        if initialize:
            conn.execute(_DDL)

    # ------------------------------------------------------------ target resolution (existing owners)

    def _news(self, target_id, revision):
        if not table_exists(self.conn, "documents"):
            return None, "news document store is not initialised"
        row = self.conn.execute("SELECT document_id, url, title, content_hash FROM documents WHERE document_id=? OR url=? "
                                "OR canonical_url=? ORDER BY document_id LIMIT 1", [target_id, target_id, target_id]).fetchone()
        if row is None:
            return None, "no news document with this id or URL"
        pinned = revision
        if pinned is None and table_exists(self.conn, "document_revision_records"):
            latest = self.conn.execute("SELECT revision_id FROM document_revision_records WHERE document_id=? "
                                       "ORDER BY revision DESC LIMIT 1", [row[0]]).fetchone()
            pinned = latest[0] if latest else None
        return {"document_id": row[0], "url": row[1], "title": row[2], "revision": pinned or f"content:{row[3]}"}, None

    def _dossier(self, target_id, revision):
        if not table_exists(self.conn, "event_dossier_revisions"):
            return None, "OSINT event dossiers are not initialised"
        row = self.conn.execute("SELECT revision, content_hash FROM event_dossier_revisions WHERE dossier_id=? "
                                "AND (? IS NULL OR revision=?) ORDER BY revision DESC LIMIT 1",
                                [target_id, revision, revision]).fetchone()
        if row is None:
            return None, "no OSINT event dossier revision with this id"
        return {"dossier_id": target_id, "revision": str(row[0]), "content_hash": row[1]}, None

    def _population(self, namespace, target_id, vintage):
        if not table_exists(self.conn, "demographic_series"):
            return None, "demographic series are not initialised"
        series = self.conn.execute("SELECT provider, indicator, geography_code, geography_label, unit_label FROM "
                                   "demographic_series WHERE series_id=? AND namespace IN (?, 'global') ORDER BY namespace "
                                   "LIMIT 1", [target_id, namespace]).fetchone()
        if series is None:
            return None, "no demographic series with this id"
        row = self.conn.execute("SELECT vintage_id, release_at_ms FROM demographic_vintages WHERE series_id=? "
                                "AND (? IS NULL OR vintage_id=?) ORDER BY release_at_ms DESC, sequence DESC LIMIT 1",
                                [target_id, vintage, vintage]).fetchone()
        if row is None:
            return None, "the series has no vintage to cite"
        return {"series_id": target_id, "provider": series[0], "indicator": series[1], "geography_code": series[2],
                "geography_label": series[3], "unit": series[4], "revision": row[0], "vintage_released_at_ms": row[1]}, None

    def _hazard(self, target_id, revision):
        table = next((t for t in HAZARD_TABLES if table_exists(self.conn, t)), None)
        if table is None:
            return None, "the Natural Hazards bundle (#2207) is not installed"
        columns = {r[0] for r in self.conn.execute("SELECT column_name FROM information_schema.columns WHERE table_name=?",
                                                   [table]).fetchall()}
        key = next((c for c in ("event_id", "record_key", "record_id") if c in columns), None)
        if key is None:
            return None, "the Natural Hazards event store has no recognised identifier column"
        row = self.conn.execute(f"SELECT {key} FROM {table} WHERE {key}=? LIMIT 1", [target_id]).fetchone()
        if row is None:
            return None, "no Natural Hazards event with this id"
        return {"event_id": row[0], "revision": revision or "current"}, None

    def resolve_target(self, namespace, target_kind, target_id, revision=None):
        if target_kind not in TARGET_KINDS:
            raise HumanitarianError("invalid_link", f"target_kind must be one of {TARGET_KINDS}")
        if target_kind == "news-article":
            return self._news(target_id, revision)
        if target_kind == "osint-event-dossier":
            return self._dossier(target_id, revision)
        if target_kind == "population-series":
            return self._population(namespace, target_id, revision)
        return self._hazard(target_id, revision)

    # ------------------------------------------------------------ writes

    def _insert(self, namespace, subject, target_kind, target_id, target, reason, basis, evidence, principal_id):
        state = "broken" if target is None else "linked"
        link_id = "hum-link:" + digest([namespace, subject["revision_id"], target_kind, target_id, basis])[:24]
        self.conn.execute(
            "INSERT INTO humanitarian_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
            [namespace, link_id, subject["record_key"], subject["revision_id"], target_kind, target_id,
             (target or {}).get("revision"), basis, canonical({**evidence, "target": target}), state, reason,
             principal_id, self.now()])
        return self.link(namespace, link_id, scopes={"operator"})

    def _subject(self, namespace, record_key, scopes, revision_id=None):
        subject = self.store.revision(namespace, record_key, scopes=scopes, revision_id=revision_id)
        if subject is None:
            raise HumanitarianError("not_found", "no such humanitarian record revision")
        return subject

    def cite(self, namespace: str, record_key: str, target_kind: str, target_id: str, *, citation: Mapping[str, Any],
             principal_id: str, scopes: Iterable[str], target_revision: str | None = None,
             record_revision_id: str | None = None) -> dict[str, Any]:
        """An explicit citation the humanitarian source makes; a missing target is kept as ``broken``."""

        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        if not str(dict(citation or {}).get("locator") or "").strip():
            raise HumanitarianError("invalid_link", "an explicit citation needs the locator where the source cites it")
        subject = self._subject(namespace, record_key, scopes, record_revision_id)
        target, reason = self.resolve_target(namespace, target_kind, target_id, target_revision)
        evidence = {"citation": {k: citation.get(k) for k in ("locator", "quote", "note") if citation.get(k)}}
        return self._insert(namespace, subject, target_kind, target_id, target, reason, "explicit-citation", evidence,
                            principal_id)

    def link_shared_identifiers(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                                hazard_events: Mapping[str, str] | None = None) -> dict[str, Any]:
        """GLIDE numbers on crises (and reports' disaster tags) against Natural Hazards events that publish them.

        ``hazard_events`` maps GLIDE -> hazard event id when the Natural Hazards bundle is not installed in this
        deployment (e.g. from its export); otherwise the installed store is searched by its ``glide`` column.
        """

        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        index = {str(k).upper(): v for k, v in dict(hazard_events or {}).items()}
        table = next((t for t in HAZARD_TABLES if table_exists(self.conn, t)), None)
        created, missing = [], []
        for revision in self.store.current(namespace, scopes=scopes):
            content = revision["content"]
            glides = {str(content.get("glide") or "").upper()} if content["record_type"] == "crisis" else \
                {str(d.get("glide") or "").upper() for d in content.get("disasters") or []}
            for glide in sorted(g for g in glides if g):
                event_id = index.get(glide)
                if event_id is None and table and "glide" in {r[0] for r in self.conn.execute(
                        "SELECT column_name FROM information_schema.columns WHERE table_name=?", [table]).fetchall()}:
                    row = self.conn.execute(f"SELECT * FROM {table} WHERE upper(glide)=? LIMIT 1", [glide]).fetchone()
                    event_id = row[0] if row else None
                if event_id is None:
                    missing.append({"record_key": revision["record_key"], "glide": glide,
                                    "reason": "no Natural Hazards event publishes this GLIDE number"})
                    continue
                target, reason = self._hazard(event_id, None) if table else (
                    {"event_id": event_id, "revision": "declared"}, None)
                created.append(self._insert(namespace, revision, "hazard-event", str(event_id), target, reason,
                                            "shared-identifier", {"identifier": {"scheme": "glide", "value": glide}},
                                            principal_id))
        return {"links": created, "unlinked": missing}

    def attach_population(self, namespace: str, record_key: str, series_id: str, *, principal_id: str,
                          scopes: Iterable[str], vintage_id: str | None = None,
                          citation: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Attach a population denominator by citation or through an accepted HR07 place match, with its vintage."""

        from src.kb.humanitarian_identity import HumanitarianIdentity, place_ref_key

        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        subject = self._subject(namespace, record_key, scopes)
        target, reason = self._population(namespace, series_id, vintage_id)
        if citation and str(citation.get("locator") or "").strip():
            return self._insert(namespace, subject, "population-series", series_id, target, reason, "explicit-citation",
                                {"citation": dict(citation), "notice": NO_INFERENCE}, principal_id)
        if target is None:
            return self._insert(namespace, subject, "population-series", series_id, None, reason,
                                "accepted-identity-match", {"notice": NO_INFERENCE}, principal_id)
        identity = HumanitarianIdentity(self.conn, initialize=False)
        links = identity.accepted_place_links(namespace, scopes=scopes)
        admin = {p["place_id"]: p for p in identity.admin_places(namespace)}
        for place in subject["content"].get("places") or []:
            for link in links.get(place_ref_key(place), []):
                geo = admin.get(link["place_id"]) or {}
                if str(target["geography_code"]).upper() in {str(geo.get("pcode") or "").upper(),
                                                              str(geo.get("iso3") or "").upper()}:
                    return self._insert(namespace, subject, "population-series", series_id, target, None,
                                        "accepted-identity-match",
                                        {"place_ref": place_ref_key(place), "assertion_id": link["assertion_id"],
                                         "boundary_vintage": link["boundary_vintage"], "notice": NO_INFERENCE},
                                        principal_id)
        raise HumanitarianError("no_basis", "no citation and no accepted place match ties this record to the series "
                                            "geography; nothing is attached")

    # ------------------------------------------------------------ reads

    def link(self, namespace: str, link_id: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        authorize(namespace, set(scopes), READ_SCOPE)
        row = self.conn.execute("SELECT link_id, record_key, record_revision_id, target_kind, target_id, target_revision, "
                                "basis, evidence_json, state, reason, created_by, created_at_ms FROM humanitarian_links "
                                "WHERE namespace=? AND link_id=?", [namespace, link_id]).fetchone()
        if row is None:
            raise HumanitarianError("not_found", "link is not visible in this namespace")
        return {"contract": CONTRACT, "link_id": row[0], "record_key": row[1], "record_revision_id": row[2],
                "target_kind": row[3], "target_id": row[4], "target_revision": row[5], "basis": row[6],
                "evidence": json.loads(row[7]), "state": row[8], "reason": row[9], "created_by": row[10],
                "created_at_ms": row[11], "notice": NO_INFERENCE}

    def links(self, namespace: str, *, scopes: Iterable[str], record_key: str | None = None) -> dict[str, Any]:
        """Links with a fresh target check: broken or vanished targets are reported, never dropped."""

        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if not table_exists(self.conn, "humanitarian_links"):
            return {"links": [], "broken": []}
        result, broken = [], []
        for (link_id,) in self.conn.execute("SELECT link_id FROM humanitarian_links WHERE namespace=? AND "
                                            "(? IS NULL OR record_key=?) ORDER BY link_id",
                                            [namespace, record_key, record_key]).fetchall():
            link = self.link(namespace, link_id, scopes=scopes)
            target, reason = self.resolve_target(namespace, link["target_kind"], link["target_id"], link["target_revision"]) \
                if link["state"] == "linked" else (None, link["reason"])
            current = self.store.revision(namespace, link["record_key"], scopes=scopes)
            link["check"] = {"target_found": target is not None, "reason": reason,
                             "record_revised_since": bool(current and current["revision_id"] != link["record_revision_id"])}
            (broken if target is None else result).append(link)
        return {"links": result, "broken": broken}
