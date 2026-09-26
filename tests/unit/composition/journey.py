"""A bounded offline composition world: Berlin source pack, providers, packs, sessions.

Provider input is fixture data served through the source-pack runtime's real
WFS adapter (``WfsServer`` pages); every operation after that runs the real
runtime, projection, spatial store, document store and intake ledger code.
"""

from __future__ import annotations

from typing import Any

import duckdb

from src.composition import contracts as c
from src.composition import lifecycle as lc
from src.composition.dispatcher import Dispatcher
from src.kb.geospatial import GeospatialStore
from src.kb.intake_modes import IntakeStore
from tests.unit.geospatial_pack_helpers import PUBLIC_DNS, WfsServer, install, point, source, square

NAMESPACE = "global"
DISTRICTS = "alkis_bezirke:bezirksgrenzen"
SCHOOLS = "schulen:schulen"
ANALYST_SCOPES = {
    "knowledge:recipes:read", "knowledge:recipes:execute", "knowledge:recipes:write",
    "knowledge:intake:read", "knowledge:intake:write", f"namespace:{NAMESPACE}:read",
    f"namespace:{NAMESPACE}:write", "knowledge:geospatial:read", "knowledge:geospatial:calculate",
    "knowledge:geospatial:write", "knowledge:read",
}


def district_server() -> WfsServer:
    return WfsServer([
        square("11000001", -1000, -1000, 2000, namgem="Mitte"),
        square("11000002", 5000, 5000, 2000, namgem="Pankow"),
    ])


def school_server() -> WfsServer:
    return WfsServer([
        point("school-mitte-1", 10, 10, bezirk="Mitte", schulname="Mitte School One"),
        point("school-mitte-2", -500, 300, bezirk="Mitte", schulname="Mitte School Two"),
        point("school-pankow-1", 5500, 5500, bezirk="Pankow", schulname="Pankow School"),
    ])


def location_template(owner: str, template_id: str, *, extra_steps=(), effect_override=None) -> dict[str, Any]:
    steps = [
        {"id": "acquire", "capability": "sources.acquire", "range": "^1.0.0", "effect": "acquisition",
         "arguments": {"pack_id": "geospatial-berlin",
                       "source_ids": ["berlin-bezirksgrenzen", "berlin-schulen"],
                       "max_results": 5000, "max_bytes": 5_000_000}},
        {"id": "within", "capability": "spatial.points-within-boundary", "range": "^1.0.0",
         "effect": "read-only", "depends_on": ["acquire"],
         "arguments": {"collection": SCHOOLS, "boundary_name": {"$param": "place"},
                       "boundary_collection": DISTRICTS}},
        {"id": "evidence", "capability": "evidence.document-references", "range": "^1.0.0",
         "effect": "read-only", "depends_on": ["within"],
         "arguments": {"document_ids": {"$step": "within.members[].document_id"}}},
        {"id": "artifact", "capability": "intake.session-artifact", "range": "^1.0.0",
         "effect": effect_override or "local-mutation", "depends_on": ["evidence"],
         "arguments": {"name": template_id, "content": {"$step": "within.total_members"},
                       "references": {"$step": "evidence.references"}}},
        *extra_steps,
    ]
    return {
        "contract": "noesis-workflow-template-v1", "template_id": template_id, "version": "1.0.0",
        "owner_pack": owner, "description": f"{template_id} fixture journey",
        "parameters": {"place": {"type": "string", "required": True}},
        "steps": steps, "permitted_record_kinds": ["geospatial_feature", "document"],
        "expected_artifacts": [{"kind": "session-artifact", "name": template_id, "from_step": "artifact"}],
    }


def consumer_manifest(name: str, template: dict[str, Any] | None, *, requires=None, features=None) -> dict[str, Any]:
    capabilities = requires or ["sources.acquire", "spatial.points-within-boundary",
                                "evidence.document-references", "intake.session-artifact"]
    manifest = {"pack_format": c.PACK_FORMAT_V2, "name": name, "version": "1.0.0",
                "requires": [{"capability": cap, "range": "^1.0.0"} for cap in capabilities],
                "contributes": {}}
    if template:
        manifest["contributes"]["workflow_templates"] = [template]
    if features:
        manifest["optional_features"] = {f: {"description": f} for f in features}
    return c.validate_manifest(manifest)


class World:
    def __init__(self, *, consumers: dict[str, dict[str, Any]] | None = None) -> None:
        self.conn = duckdb.connect(":memory:")
        GeospatialStore(self.conn)
        self.source_pack, self.runtime = install(self.conn)
        self.coordinator = lc.Coordinator(self.conn, contracts=c.contract_candidates(include_domain_packs=False))
        for descriptor in c.load_providers():
            self.coordinator.store.install_provider(descriptor, principal_id="operator")
        self.grants: dict[str, set[str]] = {"analyst": set(ANALYST_SCOPES), "operator": {"operator"}}
        self.intake = IntakeStore(self.conn, active_research_limit=100)
        self.districts, self.schools = district_server(), school_server()
        for name, manifest in (consumers or {}).items():
            self.coordinator.store.install_manifest(manifest, principal_id="operator")
            self.coordinator.store.select(name, "^1.0.0", principal_id="operator")
        if consumers:
            receipt = self.coordinator.activate("world-gen-1", principal_id="operator")
            assert receipt["status"] == "applied", receipt

    def adapters(self) -> dict[str, Any]:
        compile_ = self.runtime.factory.compile
        return {"geospatial-berlin": {
            "berlin-bezirksgrenzen": compile_(source(self.source_pack, "berlin-bezirksgrenzen"),
                                              transport=self.districts),
            "berlin-schulen": compile_(source(self.source_pack, "berlin-schulen"), transport=self.schools),
        }}

    def dispatcher(self, **kwargs: Any) -> Dispatcher:
        inputs = {"source_adapters": self.adapters(), "dns_resolver": PUBLIC_DNS}
        inputs.update(kwargs.pop("inputs", {}))
        return Dispatcher(self.conn, authority=lambda principal: self.grants.get(principal, set()),
                          coordinator=self.coordinator, now=self.runtime.now, inputs=inputs, **kwargs)

    def session(self, key: str, principal: str = "analyst") -> dict[str, Any]:
        return self.intake.create(NAMESPACE, "Deep Research", key, intent=f"journey {key}",
                                  principal_id=principal, scopes=self.grants[principal])
