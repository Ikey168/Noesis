"""Regenerate the composition contract fixture corpus (C02.6): PYTHONPATH=. python tests/fixtures/composition/build_contract_fixtures.py"""
import copy, json, pathlib
from src.composition import contracts as c
OUT = pathlib.Path("tests/fixtures/composition/contracts")
OUT.mkdir(parents=True, exist_ok=True)
for f in OUT.glob("*.json"): f.unlink()
def put(name, contract, document, expect=None, context=None, covers=()):
    body = {"contract": contract, "valid": expect is None, "covers": list(covers), "document": document}
    if expect: body["expect"] = expect
    if context: body["context"] = context
    (OUT / f"{name}.json").write_text(json.dumps(body, indent=1, sort_keys=True) + "\n")

manifest = {
  "pack_format": "noesis-pack-v2", "name": "sample-research", "version": "1.0.0",
  "description": "Fixture pack consuming the shared spatial capability.",
  "requires": [{"capability": "spatial.relation", "range": "^1.0.0",
                "input_contract": {"name": "noesis-geospatial-geometry-v2", "range": "^2.0.0"},
                "effects": ["local-mutation"]},
               {"capability": "spatial.geometry-store", "range": "^1.0.0", "feature": "geometry-capture"}],
  "optional_features": {"geometry-capture": {"description": "Store new geometries"}},
  "contributes": {"capability_labels": ["place-evidence"], "profiles": [{"profile_id": "places", "vocabulary": ["place"]}]},
  "aliases": {"capability_labels": {"place-evidence": "spatial.relation"}},
  "references": {"source_packs": [{"pack_id": "geospatial-berlin", "range": "^1.0.0"}],
                 "contracts": [{"name": "noesis-spatial-result-v1", "range": "~1.0.0"}]},
  "planner_keywords": {"anything": ["advisory"]},
}
known = {"context": {"known_capabilities": ["spatial.relation", "spatial.geometry-store"]}}
put("manifest-valid", "manifest", manifest, context=known["context"], covers=["aliases", "ranges", "effects", "advisory-fields"])
m = copy.deepcopy(manifest); m["aliases"]["capability_labels"]["place-evidence"] = "Not A Capability"
put("manifest-invalid-alias-target", "manifest", m, "invalid_manifest", covers=["aliases"])
m = copy.deepcopy(manifest); m["requires"][0]["range"] = ">>1.0"
put("manifest-invalid-malformed-range", "manifest", m, "invalid_range", covers=["ranges"])
m = copy.deepcopy(manifest); m["requires"][0]["range"] = "latest"
put("manifest-invalid-floating-range", "manifest", m, "floating_range", covers=["ranges"])
m = copy.deepcopy(manifest); m["x-note"] = {"owner": "fixtures"}
put("manifest-valid-noncritical-extension", "manifest", m, covers=["critical-unknown-fields"])
m = copy.deepcopy(manifest); m["x-sandbox"] = {"mode": "strict"}; m["critical_extensions"] = ["x-sandbox"]
put("manifest-invalid-unknown-critical-extension", "manifest", m, "unknown_critical_extension", covers=["critical-unknown-fields"])
m = copy.deepcopy(manifest); m["requirements"] = []
put("manifest-invalid-unknown-field", "manifest", m, "invalid_manifest", covers=["critical-unknown-fields"])
m = copy.deepcopy(manifest); m["requires"].append({"capability": "spatial.teleport", "range": "^1.0.0"})
put("manifest-invalid-unknown-capability", "manifest", m, "unknown_capability", context=known["context"], covers=["unknown-capability"])
m = copy.deepcopy(manifest); m["metadata"] = {"hooks": {"module": "os", "callable": "system"}}
put("manifest-invalid-executable-reference", "manifest", m, "executable_reference", covers=["executable-reference"])
m = copy.deepcopy(manifest); m["requires"][0]["effects"] = ["delete-everything"]
put("manifest-invalid-effect", "manifest", m, "invalid_manifest", covers=["effects"])

provider = json.loads(pathlib.Path("config/composition/providers/geospatial.json").read_text())
put("provider-valid", "provider", provider, covers=["effects", "store-owners"])
p = copy.deepcopy(provider); del p["capabilities"][0]["effect"]
put("provider-invalid-missing-effect", "provider", p, "invalid_provider", covers=["effects"])
p = copy.deepcopy(provider); p["capabilities"][0]["effect"] = "teleport"
put("provider-invalid-unknown-effect", "provider", p, "invalid_provider", covers=["effects"])
p = copy.deepcopy(provider); del p["capabilities"][1]["readiness"]
put("provider-invalid-missing-readiness-probe", "provider", p, "invalid_provider", covers=["readiness"])
p = copy.deepcopy(provider); p["capabilities"][0]["bindings"] = [{"kind": "mcp-tool", "id": "noesis-knowledge-engine.drop_all_tables"}]
put("provider-invalid-unregistered-tool", "provider", p, "unregistered_tool", covers=["bindings"])
p = copy.deepcopy(provider); p["capabilities"][0]["bindings"] = [{"kind": "registered", "id": "os.system"}]
put("provider-invalid-unregistered-binding", "provider", p, "unregistered_binding", covers=["bindings"])
other = copy.deepcopy(provider); other["provider_id"] = "example.other-spatial"; other.pop("descriptor_hash", None)
put("providers-invalid-conflicting-store-owner", "providers", [provider, other], "conflicting_store_owner", covers=["store-owners"])
distinct = copy.deepcopy(other); distinct["stores"] = [{"record_kind": "sketch", "tables": ["sketches"]}]
for cap in distinct["capabilities"]: cap["record_kinds"] = []
put("providers-valid-distinct-store-owners", "providers", [provider, distinct], covers=["store-owners"])

plan = {"contract": "noesis-composition-plan-v1", "resolver_version": "1", "roots": [{"name": "sample-research", "version": "1.0.0"}],
        "features": {"sample-research": []}, "manifests": [{"name": "sample-research", "version": "1.0.0", "manifest_hash": "sha256:" + "0"*64}],
        "providers": [{"provider_id": "noesis.geospatial", "version": "1.0.0", "descriptor_hash": "sha256:" + "1"*64}],
        "contracts": [{"name": "noesis-spatial-result-v1", "version": "1.0.0"}], "source_packs": [],
        "bindings": [{"consumer": "sample-research@1.0.0", "capability": "spatial.relation", "range": "^1.0.0", "provider_id": "noesis.geospatial",
                      "provider_version": "1.0.0", "capability_version": "1.0.0", "effect": "local-mutation", "reason": "only-compatible"}],
        "edges": [{"from": "sample-research@1.0.0", "to": "noesis.geospatial@1.0.0", "kind": "binds"}],
        "omissions": [{"consumer": "sample-research@1.0.0", "capability": "spatial.geometry-store", "feature": "geometry-capture", "code": "feature-not-selected"}],
        "aliases": {}, "output_contracts": [], "conservative": []}
plan["digest"] = c.plan_digest(plan)
put("plan-valid", "plan", plan, covers=["canonical-digest"])
p = copy.deepcopy(plan); p["providers"][0]["health"] = "healthy"; p["digest"] = c.plan_digest(p)
put("plan-invalid-dynamic-health-field", "plan", p, "dynamic_field_in_plan", covers=["dynamic-fields"])
p = copy.deepcopy(plan); p["bindings"][0]["credential"] = {"secret_ref": "x"}; p["digest"] = c.plan_digest(p)
put("plan-invalid-dynamic-credential-field", "plan", p, "dynamic_field_in_plan", covers=["dynamic-fields"])
p = copy.deepcopy(plan); p["providers"][0]["descriptor_hash"] = "sha256:" + "2"*64
put("plan-invalid-stale-digest", "plan", p, "plan_digest_mismatch", covers=["canonical-digest"])

readiness = {"contract": "noesis-composition-readiness-v1", "plan_digest": plan["digest"], "context": {"namespace": "global", "network": "disabled"},
             "observed_at_ms": 1000,
             "operations": [{"consumer": "sample-research@1.0.0", "capability": "spatial.relation", "operation": "noesis-knowledge-engine.calculate_spatial_relation",
                             "provider_id": "noesis.geospatial", "effect": "local-mutation", "state": "blocked",
                             "prerequisites": [{"kind": "credential", "name": "none", "satisfied": True}],
                             "blockers": [{"kind": "empty-data", "reason": "the local spatial store holds no geometries"}]}],
             "optional_omissions": [{"consumer": "sample-research@1.0.0", "capability": "spatial.geometry-store", "feature": "geometry-capture"}]}
put("readiness-valid", "readiness", readiness, covers=["blocker-kinds"])
r = copy.deepcopy(readiness); del r["observed_at_ms"]
put("readiness-invalid-no-observation-time", "readiness", r, "invalid_readiness", covers=["observation-time"])
r = copy.deepcopy(readiness); del r["operations"][0]["blockers"][0]["kind"]
put("readiness-invalid-blocker-without-kind", "readiness", r, "invalid_readiness", covers=["blocker-kinds"])
r = copy.deepcopy(readiness); r["operations"][0]["blockers"][0]["credential_value"] = "hunter2"
put("readiness-invalid-credential-value", "readiness", r, "invalid_readiness", covers=["forbidden-fields"])
r = copy.deepcopy(readiness); r["selections"] = [{"principal": "someone-else"}]
put("readiness-invalid-other-selections", "readiness", r, "invalid_readiness", covers=["forbidden-fields"])

receipt = {"contract": "noesis-composition-activation-receipt-v1", "activation_id": "composition-activation:fixture", "idempotency_key": "fixture-1",
           "operation": "activate", "previous_generation": None, "new_generation": 1, "plan_digest": plan["digest"],
           "registrations": {"added": ["sample-research@1.0.0"], "removed": [], "retained": []}, "source_upgrade_refs": [],
           "stages": [{"stage": "previewed", "status": "applied"}, {"stage": "staged", "status": "applied"}, {"stage": "verified", "status": "applied"}, {"stage": "published", "status": "applied"}],
           "status": "applied", "recovery_status": "not-needed", "error": None, "principal_id": "operator", "created_at_ms": 1000}
receipt["receipt_hash"] = c.receipt_hash(receipt)
put("receipt-valid", "receipt", receipt, context={"generation_plans": {"1": plan["digest"]}}, covers=["generation-consistency"])
r = copy.deepcopy(receipt); del r["idempotency_key"]; r["receipt_hash"] = c.receipt_hash(r)
put("receipt-invalid-no-idempotency-key", "receipt", r, "invalid_receipt", covers=["idempotency"])
put("receipt-invalid-generation-digest-mismatch", "receipt", receipt, "generation_digest_mismatch", context={"generation_plans": {"1": "sha256:" + "9"*64}}, covers=["generation-consistency"])
print(len(list(OUT.glob("*.json"))))
