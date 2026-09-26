"""Builder for the shared composition-contract fixture corpus (C02.6).

``python tests/unit/composition_corpus.py`` regenerates
``tests/fixtures/composition/corpus.json``; a test asserts the committed file
matches this builder. Every case names its contract, whether it is valid, the
expected issue code when invalid, and any validator options.

Categories required by #1809, each with a valid and an invalid variant:
compatibility aliases, contract ranges, conflicting store owners, side-effect
classes and critical unknown fields; plus the invalid examples #1804, #1806,
#1807 and #1808 list.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from src.composition.contracts import seal_manifest, seal_plan

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "tests/fixtures/composition/corpus.json"
H = "sha256:" + "0" * 64
H2 = "sha256:" + "1" * 64
GEOSPATIAL_TOOLS = ["noesis-knowledge-engine.calculate_spatial_relation",
                    "noesis-knowledge-engine.store_geospatial_geometry",
                    "noesis-knowledge-engine.record_geospatial_resolution"]


def manifest(**overrides):
    body = {
        "pack_format": "noesis-pack-composition-v1",
        "id": "osint",
        "version": "1.0.0",
        "description": "Investigative bundle consuming the shared spatial capability.",
        "contributes": {
            "capabilities": [{"id": "osint.origin-aware-corroboration",
                              "contract": {"name": "noesis-osint-corroboration", "version": "1.0.0"}}],
            "workflow_templates": [{"id": "osint.location-investigation", "version": "1.0.0"}],
        },
        "requires": [{"capability": "geospatial.spatial-relation", "contract": "noesis-spatial-relation",
                      "range": "^1.0.0"}],
        "optional_features": [{"id": "imagery", "default": False,
                               "requires": [{"capability": "osint.media-provenance",
                                             "contract": "noesis-media-provenance", "range": ">=1.0.0,<2.0.0"}]}],
        "compatibility_aliases": {"neuronews-osint": "osint"},
        "advisory": {"panels": [], "query_examples": [{"intent": "Where was this photographed?"}]},
        "extensions": {"x-review": {"owner": "osint-team"}},
    }
    body.update(overrides)
    return seal_manifest(body)


def provider(**overrides):
    body = {
        "contract": "noesis-provider-descriptor-v1",
        "id": "geospatial.core",
        "version": "1.0.0",
        "implementation": {"name": "noesis-geospatial", "version": "1.0.0", "server": "noesis-knowledge-engine"},
        "capabilities": [{
            "id": "geospatial.spatial-relation",
            "contract": {"name": "noesis-spatial-relation", "version": "1.0.0"},
            "operations": ["calculate-spatial-relation", "store-geometry", "record-resolution"],
            "semantic_constraints": {"crs": "EPSG:4326", "distance_units": "meters",
                                     "containment": "precision-aware point-in-polygon"},
        }],
        "operations": [
            {"id": "calculate-spatial-relation", "tool": GEOSPATIAL_TOOLS[0], "side_effect": "read-only",
             "idempotency": {"supported": True}, "readiness_probe": "geometries",
             "required_scopes": ["knowledge:geospatial:read"], "required_context": ["namespace"]},
            {"id": "store-geometry", "tool": GEOSPATIAL_TOOLS[1], "side_effect": "local-mutation",
             "idempotency": {"supported": True, "key": "content-derived geometry_id"},
             "readiness_probe": "geometries", "required_scopes": ["knowledge:geospatial:write"],
             "required_context": ["namespace", "principal"]},
            {"id": "record-resolution", "tool": GEOSPATIAL_TOOLS[2], "side_effect": "local-mutation",
             "idempotency": {"supported": False, "receipt": "geocode_resolutions"},
             "readiness_probe": "places", "required_scopes": ["knowledge:geospatial:write"],
             "required_context": ["namespace", "principal"]},
        ],
        "stores": [
            {"record_type": "place", "store": "src.kb.geospatial", "tables": ["geospatial_places"],
             "revision_addressable": True, "namespace_scoped": True},
            {"record_type": "geometry", "store": "src.kb.geospatial", "tables": ["geospatial_geometries"],
             "revision_addressable": True, "namespace_scoped": True},
        ],
        "readiness_probes": [{"id": "geometries", "kind": "table-exists", "target": "geospatial_geometries"},
                             {"id": "places", "kind": "table-exists", "target": "geospatial_places"}],
    }
    body.update(overrides)
    return body


def plan(**overrides):
    body = {
        "contract": "noesis-composition-plan-v1",
        "resolver_version": "1.0.0",
        "roots": [{"pack": "osint", "version": "1.0.0", "content_hash": H, "features": []}],
        "packs": [{"id": "osint", "version": "1.0.0", "content_hash": H}],
        "providers": [{"id": "geospatial.core", "version": "1.0.0", "content_hash": H2}],
        "graph": [{"from": "osint", "to": "geospatial.spatial-relation", "kind": "requires",
                   "capability": "geospatial.spatial-relation", "optional": False}],
        "features": {"osint": []},
        "bindings": [{"capability": "geospatial.spatial-relation",
                      "contract": {"name": "noesis-spatial-relation", "version": "1.0.0"},
                      "provider": "geospatial.core", "provider_version": "1.0.0",
                      "operations": ["calculate-spatial-relation"], "reason": "only-compatible",
                      "consumers": ["osint"]}],
        "omissions": [{"pack": "osint", "feature": "imagery", "reason": "not selected"}],
        "compatibility_aliases": {"neuronews-osint": "osint"},
    }
    body.update(overrides)
    return seal_plan(body)


def readiness(**overrides):
    body = {"contract": "noesis-composition-readiness-v1", "plan_digest": plan()["digest"],
            "context": {"namespace": "global"}, "observed_at_ms": 1790000000000,
            "operations": [{"capability": "geospatial.spatial-relation", "operation": "calculate-spatial-relation",
                            "provider": "geospatial.core", "state": "blocked", "prerequisites": ["geometries"],
                            "blockers": [{"kind": "empty_data", "detail": "no geometries retained"}]}]}
    body.update(overrides)
    return body


def receipt(**overrides):
    body = {"contract": "noesis-composition-activation-receipt-v1", "receipt_id": "activation:1",
            "idempotency_key": "enable-osint-1", "status": "published",
            "previous_generation": None, "new_generation": {"id": "gen-1", "plan_digest": plan()["digest"]},
            "affected_registrations": [{"kind": "pack", "id": "osint", "action": "register"}],
            "source_upgrade_receipts": [],
            "stages": [{"stage": "preview", "status": "applied"}, {"stage": "publish", "status": "applied"}],
            "recovery_status": "none", "created_at_ms": 1790000000000}
    body.update(overrides)
    return body


def without(document, key):
    value = copy.deepcopy(document)
    value.pop(key)
    return value


def cases():
    m, p = manifest(), provider()
    op_without_side_effect = copy.deepcopy(p)
    op_without_side_effect["operations"][0].pop("side_effect")
    unknown_side_effect = copy.deepcopy(p)
    unknown_side_effect["operations"][0]["side_effect"] = "writes"
    missing_probe = copy.deepcopy(p)
    missing_probe["operations"][0]["readiness_probe"] = "not-declared"
    unregistered = copy.deepcopy(p)
    unregistered["operations"][0]["tool"] = "noesis-knowledge-engine.not_a_tool"
    other_store = copy.deepcopy(p)
    other_store.update(id="geospatial.transit", stores=[{"record_type": "transit-stop", "store": "src.kb.transit"}])
    same_store = copy.deepcopy(p)
    same_store.update(id="osint.places", stores=[{"record_type": "place", "store": "src.osint.places"}])
    every_effect = copy.deepcopy(p)
    for effect, operation in zip(("read-only", "local-mutation", "acquisition"), every_effect["operations"],
                                 strict=True):
        operation["side_effect"] = effect
    publication = copy.deepcopy(p)
    publication["operations"][2]["side_effect"] = "external-publication"
    valid_opts = {"registered_tools": GEOSPATIAL_TOOLS}
    known = {"known_capabilities": ["geospatial.spatial-relation", "osint.media-provenance"]}
    requires_unknown = copy.deepcopy(without(m, "content_hash"))
    requires_unknown["requires"][0]["capability"] = "geospatial.unknown-capability"
    executable = copy.deepcopy(without(m, "content_hash"))
    executable["advisory"]["entrypoint"] = "src.osint.plugin:install"
    health_plan = plan()
    health_plan["bindings"][0]["health"] = "up"
    credential_plan = plan()
    credential_plan["credentials"] = {"ops": "key"}
    blocker_no_kind = readiness()
    blocker_no_kind["operations"][0]["blockers"] = [{"detail": "missing"}]
    return [
        # manifest: aliases, ranges, unknown fields, capabilities, executables
        {"name": "manifest-valid", "contract": "noesis-pack-composition-v1", "valid": True,
         "document": m, "options": known},
        {"name": "manifest-alias-valid", "contract": "noesis-pack-composition-v1", "valid": True,
         "document": manifest(compatibility_aliases={"old-osint": "osint", "neuronews-osint": "osint"})},
        {"name": "manifest-alias-self", "contract": "noesis-pack-composition-v1", "valid": False,
         "expect": "alias_cycle", "document": manifest(compatibility_aliases={"osint": "osint"})},
        {"name": "manifest-range-comparator-valid", "contract": "noesis-pack-composition-v1", "valid": True,
         "document": manifest(requires=[{"capability": "geospatial.spatial-relation",
                                          "contract": "noesis-spatial-relation", "range": ">=1.2.0,<2.0.0"}])},
        {"name": "manifest-range-latest", "contract": "noesis-pack-composition-v1", "valid": False,
         "expect": "invalid_range",
         "document": manifest(requires=[{"capability": "geospatial.spatial-relation",
                                          "contract": "noesis-spatial-relation", "range": "latest"}])},
        {"name": "manifest-range-malformed", "contract": "noesis-pack-composition-v1", "valid": False,
         "expect": "invalid_range",
         "document": manifest(requires=[{"capability": "geospatial.spatial-relation",
                                          "contract": "noesis-spatial-relation", "range": "1.x"}])},
        {"name": "manifest-unknown-advisory-valid", "contract": "noesis-pack-composition-v1", "valid": True,
         "document": manifest(advisory={"panels": [], "future_hint": {"any": "shape"}})},
        {"name": "manifest-unknown-critical-field", "contract": "noesis-pack-composition-v1", "valid": False,
         "expect": "unknown_field", "document": {**without(m, "content_hash"), "installer": {"mode": "auto"}}},
        {"name": "manifest-unknown-required-capability", "contract": "noesis-pack-composition-v1",
         "valid": False, "expect": "unknown_capability", "document": requires_unknown, "options": known},
        {"name": "manifest-executable-reference", "contract": "noesis-pack-composition-v1", "valid": False,
         "expect": "executable_reference", "document": executable},
        {"name": "manifest-hash-mismatch", "contract": "noesis-pack-composition-v1", "valid": False,
         "expect": "hash_mismatch", "document": {**m, "description": "edited after sealing"}},
        # provider: side effects, probes, tools, stores
        {"name": "provider-valid", "contract": "noesis-provider-descriptor-v1", "valid": True,
         "document": p, "options": valid_opts},
        {"name": "provider-every-side-effect-valid", "contract": "noesis-provider-descriptor-v1", "valid": True,
         "document": every_effect, "options": valid_opts},
        {"name": "provider-external-publication-valid", "contract": "noesis-provider-descriptor-v1",
         "valid": True, "document": publication, "options": valid_opts},
        {"name": "provider-missing-side-effect", "contract": "noesis-provider-descriptor-v1", "valid": False,
         "expect": "schema", "document": op_without_side_effect, "options": valid_opts},
        {"name": "provider-unknown-side-effect", "contract": "noesis-provider-descriptor-v1", "valid": False,
         "expect": "schema", "document": unknown_side_effect, "options": valid_opts},
        {"name": "provider-missing-probe", "contract": "noesis-provider-descriptor-v1", "valid": False,
         "expect": "missing_probe", "document": missing_probe, "options": valid_opts},
        {"name": "provider-unregistered-tool", "contract": "noesis-provider-descriptor-v1", "valid": False,
         "expect": "unregistered_tool", "document": unregistered, "options": valid_opts},
        {"name": "provider-unknown-critical-field", "contract": "noesis-provider-descriptor-v1", "valid": False,
         "expect": "unknown_field", "document": {**p, "auto_install": True}, "options": valid_opts},
        {"name": "provider-set-distinct-stores-valid", "contract": "provider-set", "valid": True,
         "document": [p, other_store]},
        {"name": "provider-set-conflicting-store", "contract": "provider-set", "valid": False,
         "expect": "conflicting_store_owner", "document": [p, same_store]},
        # plan
        {"name": "plan-valid", "contract": "noesis-composition-plan-v1", "valid": True, "document": plan()},
        {"name": "plan-health-field", "contract": "noesis-composition-plan-v1", "valid": False,
         "expect": "dynamic_field", "document": health_plan},
        {"name": "plan-credential-field", "contract": "noesis-composition-plan-v1", "valid": False,
         "expect": "dynamic_field", "document": credential_plan},
        {"name": "plan-digest-mismatch", "contract": "noesis-composition-plan-v1", "valid": False,
         "expect": "digest_mismatch", "document": {**plan(), "digest": H}},
        # readiness and receipt
        {"name": "readiness-valid", "contract": "noesis-composition-readiness-v1", "valid": True,
         "document": readiness()},
        {"name": "readiness-without-observation-time", "contract": "noesis-composition-readiness-v1",
         "valid": False, "expect": "schema", "document": without(readiness(), "observed_at_ms")},
        {"name": "readiness-blocker-without-kind", "contract": "noesis-composition-readiness-v1",
         "valid": False, "expect": "schema", "document": blocker_no_kind},
        {"name": "readiness-credential-value", "contract": "noesis-composition-readiness-v1", "valid": False,
         "expect": "unknown_field", "document": {**readiness(), "api_key": "secret-value"}},
        {"name": "receipt-valid", "contract": "noesis-composition-activation-receipt-v1", "valid": True,
         "document": receipt(), "options": {"generation_digests": {"gen-1": plan()["digest"]}}},
        {"name": "receipt-without-idempotency-key", "contract": "noesis-composition-activation-receipt-v1",
         "valid": False, "expect": "schema", "document": without(receipt(), "idempotency_key")},
        {"name": "receipt-generation-digest-mismatch", "contract": "noesis-composition-activation-receipt-v1",
         "valid": False, "expect": "generation_digest_mismatch", "document": receipt(),
         "options": {"generation_digests": {"gen-1": H}}},
    ]


def build() -> str:
    return json.dumps({"corpus": "noesis-composition-contracts", "cases": cases()}, indent=1,
                      ensure_ascii=False, sort_keys=True) + "\n"


if __name__ == "__main__":
    CORPUS.parent.mkdir(parents=True, exist_ok=True)
    CORPUS.write_text(build())
    print(CORPUS)
