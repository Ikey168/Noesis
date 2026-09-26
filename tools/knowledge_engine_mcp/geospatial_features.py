"""Acquired geospatial feature tools: import, inspection and boundary queries."""

from typing import Any

from src.kb.geospatial import CALCULATE_SCOPE, READ_SCOPE, WRITE_SCOPE

GEOSPATIAL_FEATURE_WRITES = {
    "import_geospatial_features",
    "query_geospatial_features_within",
    "retry_geospatial_feature_projection",
}
GEOSPATIAL_FEATURE_SCOPES = {
    "import_geospatial_features": [WRITE_SCOPE],
    "retry_geospatial_feature_projection": [WRITE_SCOPE],
    "query_geospatial_features_within": [CALCULATE_SCOPE],
    "inspect_geospatial_feature": [READ_SCOPE],
    "resolve_geospatial_boundary": [READ_SCOPE],
    "inspect_geospatial_feature_coverage": [READ_SCOPE],
    "replay_geospatial_feature_query": [READ_SCOPE],
    "inspect_geospatial_pack_readiness": [READ_SCOPE],
}


def register(mcp, safe, context):
    from src.kb.geospatial_features import GeospatialFeatureStore, pack_readiness

    def call(method, *args, write=False, scope=READ_SCOPE, principal=False, **kwargs):
        principal_id, scopes = context()
        extra = {"principal_id": principal_id} if principal else {}
        return safe(
            lambda conn: getattr(GeospatialFeatureStore(conn, initialize=write), method)(
                *args, scopes=scopes, **extra, **kwargs
            ),
            write=write,
            required_scope=scope,
        )

    @mcp.tool()
    def import_geospatial_features(
        namespace: str,
        provider: str,
        collection: str,
        source_crs: str,
        feature_collection: dict[str, Any] | None = None,
        path: str | None = None,
        axis_order: str = "east_north",
        id_property: str | None = None,
        title_property: str | None = None,
        snapshot: str = "partial",
        precision: dict[str, Any] | None = None,
        attribution: str | None = None,
        max_features: int = 10_000,
    ) -> dict:
        """Import one bounded GeoJSON FeatureCollection into a caller namespace.

        Pass the collection inline or as a path under NOESIS_GEOSPATIAL_IMPORT_ROOT.
        Features without IDs or with unsupported geometry are reported, not dropped;
        only snapshot="complete" lets absent features be marked removed.
        """
        from src.ingestion.geojson_features import load_local_feature_collection

        if (feature_collection is None) == (path is None):
            return {"ok": False, "error": {"code": "invalid_request",
                    "message": "provide exactly one of feature_collection or path"}}
        try:
            payload = feature_collection if path is None else load_local_feature_collection(path)
        except Exception as exc:  # noqa: BLE001 - surfaced as a structured error
            return {"ok": False, "error": {"code": getattr(exc, "code", "import_failed"),
                                           "message": str(exc)[:300]}}
        return call(
            "import_feature_collection", namespace, payload, provider=provider,
            collection=collection, source_crs=source_crs, axis_order=axis_order,
            id_property=id_property, title_property=title_property, snapshot=snapshot,
            precision=precision, attribution=attribution, max_features=max_features,
            write=True, scope=WRITE_SCOPE, principal=True,
        )

    @mcp.tool()
    def inspect_geospatial_feature(
        namespace: str, feature_id: str, include_history: bool = True
    ) -> dict:
        """Show a feature's current revision, history, provenance and snapshot state."""
        return call("feature", namespace, feature_id, include_history=include_history)

    @mcp.tool()
    def resolve_geospatial_boundary(
        namespace: str, name: str, collection: str | None = None
    ) -> dict:
        """Match a boundary by name; more than one match stays unresolved for review."""
        return call("resolve_boundary", namespace, name, collection=collection)

    @mcp.tool()
    def inspect_geospatial_feature_coverage(namespace: str, collection: str) -> dict:
        """Report a collection's active/removed counts and snapshot completeness."""
        return call("collection_coverage", namespace, collection)

    @mcp.tool()
    def query_geospatial_features_within(
        namespace: str,
        collection: str,
        boundary_feature_id: str | None = None,
        boundary_name: str | None = None,
        boundary_collection: str | None = None,
        limit: int = 1000,
    ) -> dict:
        """List point features inside one boundary with source evidence and a receipt.

        Membership is exact ring parity on stored WGS84 geometry; an ambiguous
        boundary name returns candidates instead of choosing one.
        """
        return call(
            "within", namespace, collection=collection,
            boundary_feature_id=boundary_feature_id, boundary_name=boundary_name,
            boundary_collection=boundary_collection, limit=limit,
            write=True, scope=CALCULATE_SCOPE, principal=True,
        )

    @mcp.tool()
    def replay_geospatial_feature_query(namespace: str, receipt_id: str) -> dict:
        """Recompute a points-within receipt from its pinned geometry revisions."""
        return call("replay_within", namespace, receipt_id)

    @mcp.tool()
    def retry_geospatial_feature_projection(run_id: str) -> dict:
        """Re-project features whose projection failed, from their retained records."""
        return call("retry_failed", run_id, write=True, scope=WRITE_SCOPE, principal=True)

    @mcp.tool()
    def inspect_geospatial_pack_readiness(pack_id: str = "geospatial-berlin") -> dict:
        """Report optional spatial dependencies, pack state and accepted source terms."""
        return safe(lambda conn: pack_readiness(conn, pack_id), required_scope=READ_SCOPE)
