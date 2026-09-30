"""Link transactions, parcels and indices to housing, legal and statistical records by citation (#2228, RE08 #2498).

It extends the housing citation-link model (:mod:`src.kb.housing_links`): a
link exists only when both sides publish the same identifier or one side
explicitly cites the other, never from a shared keyword or mere spatial
proximity. Each link records its basis, both records and the identifier or
citation it rests on:

* ``parcel-reference`` - a parcel's ``nationalCadastralReference`` published
  verbatim by a housing record (e.g. a plan or zone listing its parcels);
* ``geography-code`` - a price-index observation's geography code published by
  a housing statistic or indicator vintage for the same area;
* ``dataset-code`` - a Eurostat index observation and an Economics series in the
  dataset ``ObservationStore`` with the same dataset code, GEO and selection;
* ``explicit-citation`` - a stated citation of a legal work, resolved to exactly
  one work through :meth:`src.kb.legal.LegalStore.lookup` or kept unresolved.

Links are between *records*, so they survive revisions; reading a link resolves
the revision in force (optionally as of a date) on the real-estate side. Spatial
containment (a parcel inside a land-value zone) is containment *context*,
reported by :func:`containment_context`, never a link. No value is carried
across a link: nothing is interpolated between zones or indices.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping
from typing import Any

from src.kb.real_estate import (
    READ_SCOPE,
    WRITE_SCOPE,
    RealEstateError,
    RealEstateStore,
    authorize,
    canonical,
    digest,
    table_exists,
)

LINK_CONTRACT = "noesis-real-estate-link-v1"
BASES = ("parcel-reference", "geography-code", "dataset-code", "explicit-citation")
LEGAL_READ = "knowledge:legal:read"
NOTICE = "a citation link: both records as published; no value is carried across it and it is not advice"
_DDL = """
CREATE TABLE IF NOT EXISTS real_estate_links (
  namespace TEXT NOT NULL, link_id TEXT NOT NULL, record_id TEXT NOT NULL, target_kind TEXT NOT NULL,
  target_namespace TEXT, target_id TEXT, basis TEXT NOT NULL, state TEXT NOT NULL, identifier TEXT NOT NULL,
  evidence_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL, seq BIGINT NOT NULL,
  PRIMARY KEY(namespace, link_id)
);
CREATE SEQUENCE IF NOT EXISTS real_estate_link_seq;
"""


class RealEstateLinks:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = RealEstateStore(conn, initialize=initialize, now=self.now)
        if initialize:
            for sql in _DDL.split(";"):
                if sql.strip():
                    conn.execute(sql)

    def _ready(self) -> bool:
        return table_exists(self.conn, "real_estate_links")

    def _insert(self, namespace, record_id, target_kind, target_namespace, target_id, basis, state, identifier,
                evidence, principal_id) -> str | None:
        link_id = "real-estate-link:" + digest([namespace, record_id, target_kind, target_id, basis, identifier])[:24]
        if self.conn.execute("SELECT 1 FROM real_estate_links WHERE namespace=? AND link_id=?",
                             [namespace, link_id]).fetchone():
            return None
        seq = int(self.conn.execute("SELECT nextval('real_estate_link_seq')").fetchone()[0])
        self.conn.execute("INSERT INTO real_estate_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          [namespace, link_id, record_id, target_kind, target_namespace, target_id, basis, state,
                           identifier, canonical({**evidence, "notice": NOTICE}), principal_id, self.now(), seq])
        return link_id

    # ------------------------------------------------------------------ identifier links

    def link_identifiers(self, namespace: str, *, principal_id: str, scopes: Iterable[str]) -> dict[str, Any]:
        """Create every link a shared published identifier supports; report records that have none."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        self.store.require_ready()
        created: list[str] = []
        housing = _housing_rows(self.conn, namespace)
        for record in self.store.records(namespace):
            statement = self.store.current(namespace, record["record_id"])["statement"]
            published = statement["as_published"]
            if record["record_type"] == "parcel":
                reference = published["national_cadastral_reference"]
                for row in housing:
                    if reference in _published_strings(row["content"]):
                        created.append(self._insert(namespace, record["record_id"], "housing-record", namespace,
                                                    row["record_id"], "parcel-reference", "linked", reference,
                                                    {"housing_table": row["table"], "record_type": row["record_type"],
                                                     "published_by_both": "nationalCadastralReference"},
                                                    principal_id))
            if record["record_type"] == "price_index_observation":
                geography = published["geography"]["code"]
                for row in housing:
                    if row.get("area_code") == geography:
                        created.append(self._insert(namespace, record["record_id"], "housing-record", namespace,
                                                    row["record_id"], "geography-code", "linked", geography,
                                                    {"housing_table": row["table"], "record_type": row["record_type"],
                                                     "scheme": published["geography"]["scheme"]}, principal_id))
                if record["provider"] == "eurostat-hpi":
                    for series in _eurostat_series(self.conn, published["dataset"], geography):
                        selection = dict(series["metadata"].get("filters") or {})
                        dims = published.get("dimensions") or {}
                        differs = sorted(k for k in ("unit", "purchase") if selection.get(k) not in (None, dims.get(k)))
                        created.append(self._insert(
                            namespace, record["record_id"], "dataset-series", None, series["series_id"],
                            "dataset-code", "linked" if not differs else "unresolved",
                            f"{published['dataset']}:{geography}",
                            {"series_title": series["title"], "series_unit": series["unit"],
                             "selection": selection, "differs_in": differs,
                             "note": "same dataset code and GEO" if not differs else
                             "same dataset code and GEO but a different selection; not linked"}, principal_id))
        return {"contract": LINK_CONTRACT, "created": sorted(c for c in created if c),
                "links": self.links(namespace, scopes=scopes)}

    # ------------------------------------------------------------------ explicit citations

    def cite(self, namespace: str, record_id: str, identifier: str, *, relation: str, stated_in: str,
             legal_namespace: str, principal_id: str, scopes: Iterable[str],
             jurisdiction: str | None = None) -> dict[str, Any]:
        """An explicit citation stated by a publication, resolved to exactly one legal work or kept unresolved."""
        from src.kb.legal import LegalError, LegalStore

        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        if LEGAL_READ not in scopes and "operator" not in scopes:
            raise RealEstateError("unauthorized", f"{LEGAL_READ} scope is required")
        if not str(identifier or "").strip() or not str(stated_in or "").strip():
            raise RealEstateError("invalid_request", "a citation names the identifier and where it is stated")
        self.store.record(namespace, record_id)  # visible record or not_found
        works, status = [], "not_covered"
        if table_exists(self.conn, "legal_works"):
            try:
                found = LegalStore(self.conn, initialize=False).lookup(
                    legal_namespace, scopes=scopes, identifier=identifier.strip(), jurisdiction=jurisdiction)
            except LegalError as exc:
                raise RealEstateError(exc.code, str(exc)) from exc
            works, status = found["works"], found["status"]
        state = "linked" if len({w["work_id"] for w in works}) == 1 else "unresolved"
        target = works[0]["work_id"] if state == "linked" else None
        evidence = {"relation": relation, "stated_in": stated_in, "lookup_status": status,
                    **({"work_title": works[0].get("title"), "jurisdiction": works[0].get("jurisdiction")}
                       if state == "linked" else {"note": "the cited work is not acquired or is ambiguous; kept as "
                                                          "the stated identifier"})}
        link_id = self._insert(namespace, record_id, "legal-work", legal_namespace, target, "explicit-citation",
                               state, identifier.strip(), evidence, principal_id)
        return {"link_id": link_id, "state": state, "links": self.links(namespace, scopes=scopes,
                                                                          record_id=record_id)}

    # ------------------------------------------------------------------ reads

    def links(self, namespace: str, *, scopes: Iterable[str], record_id: str | None = None,
              as_of: str | None = None) -> list[dict[str, Any]]:
        """Links with the real-estate revision in force (as of a date) and the target as linked."""
        authorize(namespace, set(scopes), READ_SCOPE)
        if not self._ready():
            return []
        rows = self.conn.execute(
            "SELECT link_id, record_id, target_kind, target_namespace, target_id, basis, state, identifier, "
            "evidence_json, created_by, created_at_ms FROM real_estate_links WHERE namespace=? AND "
            "(? IS NULL OR record_id=? OR target_id=?) ORDER BY link_id",
            [namespace, record_id, record_id, record_id]).fetchall()
        out = []
        for row in rows:
            link = dict(zip(("link_id", "record_id", "target_kind", "target_namespace", "target_id", "basis", "state",
                             "identifier"), row[:8]))
            in_force = self.store.current(namespace, link["record_id"], as_of=as_of)
            out.append({"contract": LINK_CONTRACT, **link, "evidence": json.loads(row[8]), "created_by": row[9],
                        "created_at_ms": int(row[10]),
                        "revision_in_force": None if in_force is None else {
                            "revision_id": in_force["revision_id"], "event": in_force["event"],
                            "release": in_force["release"], "known_from": in_force["known_from"]}})
        return out

    def generation(self, namespace: str) -> int:
        if not self._ready():
            return 0
        return int(self.conn.execute("SELECT coalesce(max(seq), 0) FROM real_estate_links WHERE namespace=?",
                                     [namespace]).fetchone()[0])


def _published_strings(content: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(content, Mapping):
        for value in content.values():
            found |= _published_strings(value)
    elif isinstance(content, list):
        for value in content:
            found |= _published_strings(value)
    elif isinstance(content, str):
        found.add(content.strip())
    return found


def _housing_rows(conn: Any, namespace: str) -> list[dict[str, Any]]:
    """Current housing records with the area code they publish (statistics and indicator vintages)."""
    from src.kb.housing import _TABLES

    rows = []
    for record_type, table in _TABLES.items():
        if not table_exists(conn, table):
            continue
        extra = {"housing_building_statistics": "area_code", "housing_indicator_vintages": "geography"}.get(table)
        for record_id, content, area in conn.execute(
                f"SELECT record_id, content_json, {extra or 'NULL'} FROM {table} t WHERE namespace=? AND "
                f"revision_no=(SELECT max(revision_no) FROM {table} u WHERE u.namespace=t.namespace AND "
                "u.entity_key=t.entity_key)", [namespace]).fetchall():
            rows.append({"record_id": record_id, "table": table, "record_type": record_type,
                         "content": json.loads(content), "area_code": area})
    return rows


def _eurostat_series(conn: Any, dataset: str, geography: str) -> list[dict[str, Any]]:
    if not table_exists(conn, "dataset_series"):
        return []
    rows = conn.execute("SELECT series_id, title, unit, geography, metadata FROM dataset_series WHERE "
                        "provider='eurostat' AND geography=? ORDER BY series_id", [geography]).fetchall()
    out = []
    for series_id, title, unit, geo, metadata in rows:
        meta = json.loads(metadata) if isinstance(metadata, str) else dict(metadata or {})
        if meta.get("dataset") == dataset:
            out.append({"series_id": series_id, "title": title, "unit": unit, "geography": geo, "metadata": meta})
    return out


def containment_context(conn: Any, namespace: str, parcel_record_id: str, *, principal_id: str) -> list[dict]:
    """Housing zones and plans whose geometry contains the parcel's representative point: context, never a link."""
    from src.kb.geospatial import GeospatialStore

    store = RealEstateStore(conn, initialize=False)
    parcel = store.current(namespace, parcel_record_id)
    geometry_id = parcel["statement"]["as_published"]["geometry"].get("geometry_id") if parcel else None
    if not geometry_id:
        return []
    geo = GeospatialStore(conn)
    scopes = {"knowledge:geospatial:read", "knowledge:geospatial:calculate"}
    point = representative_point(geo.geometry(namespace, geometry_id, scopes=scopes)["geometry"])
    out = []
    for table, key in (("housing_land_value_revisions", "zone_id"), ("housing_plan_stages", "plan_key")):
        if not table_exists(conn, table):
            continue
        for record_id, ident, zone_geometry in conn.execute(
                f"SELECT record_id, {key}, geometry_id FROM {table} WHERE namespace=? AND geometry_id IS NOT NULL",
                [namespace]).fetchall():
            relation = geo.relation(namespace, "contains", zone_geometry, point, scopes=scopes,
                                    principal_id=principal_id)
            if relation["result"].get("contains"):
                out.append({"kind": "containment_context", "housing_record_id": record_id, "identifier": ident,
                            "receipt_id": relation["receipt_id"],
                            "note": "spatial containment of the parcel's representative point; context only, not a "
                                    "citation link, and no value applies to the parcel from it"})
    return out


def representative_point(geometry: Mapping[str, Any]) -> list[float]:
    """Mean of the exterior-ring vertices (closing vertex excluded) - a stated, reproducible point."""
    ring = geometry["coordinates"][0] if geometry["type"] == "Polygon" else geometry["coordinates"][0][0]
    points = ring[:-1] if len(ring) > 1 and ring[0] == ring[-1] else ring
    return [sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points)]


__all__ = ["BASES", "LINK_CONTRACT", "RealEstateLinks", "containment_context", "representative_point"]
