"""Berlin district budget lines linked to the existing Geospatial boundaries and places (#1909, B04).

A Berlin budget line belongs to a district only when the source states it: the
Bereich the line is published under (31-42) is one of the twelve districts,
through the crosswalk the source manifest declares. Such a line is linked to
the district's boundary feature in the Geospatial feature store
(``alkis_bezirke:bezirksgrenzen``, matched on the published district code
``gem``) and, where one is registered, to the ``GeospatialStore`` place whose
source identifiers carry that code (``berlin-bezirk``). Nothing is matched by
name or by geometry. Lines without a district, or whose code matches no
feature or several, stay unlinked with the reason recorded. No spatial store
is introduced: links reference the Geospatial owners' feature and place ids
and revisions.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from src.kb.public_finance import (
    READ_SCOPE,
    WRITE_SCOPE,
    PublicFinanceStore,
    authorize,
    digest,
    require_scope,
    table_exists,
)

GEO_READ = "knowledge:geospatial:read"
DISTRICT_COLLECTION = "alkis_bezirke:bezirksgrenzen"
PLACE_SOURCE_KEY = "berlin-bezirk"
_DDL = """
CREATE TABLE IF NOT EXISTS public_finance_place_links (
  namespace TEXT NOT NULL, link_id TEXT NOT NULL, line_id TEXT NOT NULL, district_code TEXT NOT NULL,
  collection TEXT NOT NULL, state TEXT NOT NULL, feature_id TEXT, feature_revision_id TEXT, feature_title TEXT,
  place_id TEXT, place_revision_id TEXT, reason TEXT, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, link_id)
);
"""


def district_code(value: Any) -> str | None:
    """The three-digit ALKIS district code, normalised the same way for the line and the feature."""
    text = "".join(ch for ch in str(value or "") if ch.isdigit())
    return text.zfill(3)[-3:] if text else None


class PublicFinancePlaces:
    def __init__(self, conn: Any, *, now=None, initialize: bool = True) -> None:
        self.store = PublicFinanceStore(conn, initialize=initialize, now=now)
        self.conn, self.now = conn, self.store.now
        if initialize:
            conn.execute(_DDL)

    def _features(
        self, geo_namespace: str, collection: str
    ) -> list[tuple[str, str, str, str | None, dict]]:
        if not table_exists(self.conn, "geospatial_features"):
            return []
        rows = self.conn.execute(
            "SELECT f.feature_id, f.native_id, r.revision_id, r.title, r.properties_json FROM geospatial_features f "
            "JOIN geospatial_feature_current c ON c.feature_id=f.feature_id JOIN geospatial_feature_revisions r ON "
            "r.revision_id=c.revision_id WHERE f.namespace IN (?, 'global') AND f.collection=? AND c.lifecycle='active' "
            "ORDER BY f.feature_id",
            [geo_namespace, collection],
        ).fetchall()
        return [(r[0], r[1], r[2], r[3], json.loads(r[4] or "{}")) for r in rows]

    def _places(self, geo_namespace: str, code: str) -> list[tuple[str, str]]:
        if not table_exists(self.conn, "geospatial_place_revisions"):
            return []
        rows = self.conn.execute(
            "SELECT p.place_id, r.revision_id, r.source_ids_json FROM geospatial_places p JOIN "
            "geospatial_place_current c ON c.place_id=p.place_id JOIN geospatial_place_revisions r ON "
            "r.revision_id=c.revision_id WHERE p.namespace IN (?, 'global') ORDER BY p.place_id",
            [geo_namespace],
        ).fetchall()
        return [
            (place_id, revision_id)
            for place_id, revision_id, source_ids in rows
            if district_code(json.loads(source_ids or "{}").get(PLACE_SOURCE_KEY))
            == code
        ]

    def link_districts(
        self,
        namespace: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
        geo_namespace: str = "global",
        collection: str = DISTRICT_COLLECTION,
    ) -> dict[str, Any]:
        """Link every district-scoped line to its boundary feature (and place) by the published code; idempotent."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        require_scope(scopes, GEO_READ)
        features = self._features(geo_namespace, collection)
        linked, unresolved = [], []
        for line in self.store.lines(namespace, scheme="de-be-haushalt", limit=100_000):
            if not line["district"]:
                continue  # a Hauptverwaltung line states no district and stays unlinked
            code = district_code(line["district"].get("code"))
            matches = [f for f in features if district_code(f[4].get("gem")) == code]
            places = self._places(geo_namespace, code)
            if len(matches) == 1 and len(places) <= 1:
                feature_id, _native, revision_id, title, _props = matches[0]
                state, reason = "linked", None
            else:
                feature_id = revision_id = title = None
                state = "unresolved"
                reason = (
                    "no boundary feature states this district code"
                    if not matches
                    else "more than one boundary feature states this district code"
                    if len(matches) > 1
                    else "more than one place carries this district code"
                )
            place_id, place_revision = (
                places[0] if len(places) == 1 and state == "linked" else (None, None)
            )
            link_id = (
                "pf-place:"
                + digest(
                    [
                        namespace,
                        line["line_id"],
                        collection,
                        revision_id,
                        place_revision,
                    ]
                )[:24]
            )
            if self.conn.execute(
                "SELECT 1 FROM public_finance_place_links WHERE namespace=? AND link_id=?",
                [namespace, link_id],
            ).fetchone():
                continue
            self.conn.execute(
                "INSERT INTO public_finance_place_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    namespace,
                    link_id,
                    line["line_id"],
                    code,
                    collection,
                    state,
                    feature_id,
                    revision_id,
                    title,
                    place_id,
                    place_revision,
                    reason,
                    principal_id,
                    self.now(),
                ],
            )
            (linked if state == "linked" else unresolved).append(link_id)
        return {
            "linked": linked,
            "unresolved": unresolved,
            "collection": collection,
            "geo_namespace": geo_namespace,
        }

    def place(
        self, namespace: str, line_id: str, *, scopes: Iterable[str]
    ) -> dict[str, Any]:
        """The line's district link as of the latest run, or why it has none."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        line = self.store.line(namespace, line_id)
        if not line["district"]:
            return {
                "line_id": line_id,
                "state": "no-district",
                "note": "the source states no district for this line",
            }
        if not table_exists(self.conn, "public_finance_place_links"):
            return {
                "line_id": line_id,
                "state": "not-linked",
                "district": line["district"],
            }
        row = self.conn.execute(
            "SELECT link_id, district_code, collection, state, feature_id, feature_revision_id, feature_title, "
            "place_id, place_revision_id, reason, created_at_ms FROM public_finance_place_links WHERE namespace=? AND "
            "line_id=? ORDER BY created_at_ms DESC, link_id DESC LIMIT 1",
            [namespace, line_id],
        ).fetchone()
        if row is None:
            return {
                "line_id": line_id,
                "state": "not-linked",
                "district": line["district"],
            }
        view = dict(
            zip(
                (
                    "link_id",
                    "district_code",
                    "collection",
                    "state",
                    "feature_id",
                    "feature_revision_id",
                    "feature_title",
                    "place_id",
                    "place_revision_id",
                    "reason",
                    "created_at_ms",
                ),
                row,
            )
        )
        return {
            "line_id": line_id,
            "district": line["district"],
            **view,
            "basis": "published district code (Bereich crosswalk declared by the source manifest)",
            "note": "matched on the published district code (gem) only, never on a name or a geometry",
        }
