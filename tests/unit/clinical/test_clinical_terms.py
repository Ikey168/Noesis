"""H10: MeSH and registry-native term modules, crosswalk kinds, explained expansion, unmapped gaps."""

import duckdb

from src.kb.clinical_terms import MESH_MODULE, ClinicalTerms, crosswalk_module, terms_module
from src.kb.ontology import OntologyAlignmentStore, descriptor_concepts, explain_expansion, label_crosswalk
from tests.unit.clinical.harness import NS, Env, load

SCHEMA = {"knowledge:schema:read", "knowledge:schema:register"}


def test_descriptor_hierarchy_and_label_crosswalk_preserve_kinds():
    concepts = {c["concept_id"]: c for c in descriptor_concepts(load("mesh_subset.json")["descriptors"])}
    assert concepts["D003924"]["broader"] == ["D003920"]
    assert concepts["D003920"]["broader"] == ["D004700", "D044882"]  # nearest ancestor in each tree
    source = [{"concept_id": "term:a", "labels": [{"value": "Type 2 Diabetes"}]},
              {"concept_id": "term:b", "labels": [{"value": "Unmapped thing"}]},
              {"concept_id": "term:c", "labels": [{"value": "weird"}]}]
    mappings, unmapped = label_crosswalk(source, list(concepts.values()), curations=[
        {"source": "term:c", "target": "D003920", "kind": "broader", "reviewer": "r"},
        {"source": "term:b", "target": "D003922", "kind": "incompatible", "reviewer": "r"}])
    assert {(m["source"], m["target"], m["kind"]) for m in mappings} == {
        ("term:a", "D003924", "equivalent"), ("term:c", "D003920", "broader"), ("term:b", "D003922", "incompatible")}
    assert unmapped == ["term:b"]  # an incompatible curation is not a mapping


def test_mesh_and_registry_terms_are_published_as_ontology_modules_with_a_crosswalk():
    env = Env()
    env.acquire("r1")
    aligned = env.align_terms()
    ontology = OntologyAlignmentStore(env.conn)
    mesh = ontology.inspect(MESH_MODULE, "2026.0.0", scopes=SCHEMA)
    assert mesh["kind"] == "ontology" and len(mesh["content"]["concepts"]) == 8
    terms = ontology.inspect(terms_module(NS), aligned["terms_module"]["version"], scopes=SCHEMA)
    labels = {c["labels"][0]["value"] for c in terms["content"]["concepts"]}
    assert {"T2DM", "Type 2 Diabetes", "Placebo", "Metformin"} <= labels
    crosswalk = ontology.registry.resolve("crosswalk", crosswalk_module(NS), "*", scopes=SCHEMA)
    kinds = {(m["source"], m["target"], m["kind"]) for m in crosswalk["content"]["mappings"]}
    assert ("term:t2dm", "D003924", "equivalent") in kinds
    assert ("term:type-2-diabetes", "D003922", "incompatible") in kinds
    assert {u["label"] for u in aligned["unmapped"]} == {"Diabetes mellitus type 2 (adolescents)", "NOETIGLUTIDE"}
    assert {r["term"] for r in aligned["unmapped_reaction_terms"]} >= {"NAUSEA", "PANCREATITIS"}
    assert all(r["terminology"].startswith("MedDRA") for r in aligned["unmapped_reaction_terms"])
    again = env.align_terms()
    assert again["terms_module"] == aligned["terms_module"]  # unchanged terms keep their version


def test_question_expansion_is_explained_and_incompatible_mappings_block_it():
    env = Env()
    env.acquire("r1")
    env.align_terms()
    terms = ClinicalTerms(env.conn)
    condition = terms.expand(NS, "type 2 diabetes", scopes=env.scopes(),
                             relationships=("equivalent", "narrower", "related"))
    assert condition["mesh_ids"] == ["D003924"] and "D003922" not in condition["mesh_ids"]
    assert condition["conflicts"] == [{"source": "term:type-2-diabetes", "target": "D003922"}]
    blocked = [s for s in condition["steps"] if s.get("blocked")]
    assert blocked and "incompatible" in blocked[0]["explanation"]
    assert "t2dm" in condition["labels"]  # reached through a reviewed curation, explained
    assert any("reviewed by fixture-reviewer" in s["explanation"] for s in condition["steps"])
    unknown = terms.expand(NS, "noetiglutide", scopes=env.scopes())
    assert unknown["mapped"] is False and unknown["labels"] == ["noetiglutide"] and unknown["reason"]
    unseen = terms.expand(NS, "Not in any vocabulary", scopes=env.scopes())
    assert unseen["start"] is None and "matched as written" in unseen["reason"]


def test_explain_expansion_lists_paths():
    conn = duckdb.connect()
    store = OntologyAlignmentStore(conn)
    descriptors = load("mesh_subset.json")["descriptors"]
    scopes = {"knowledge:schema:read", "knowledge:schema:register"}
    store.publish("clinical-mesh", "1.0.0", descriptor_concepts(descriptors), owner="t",
                  provenance={"kind": "imported", "source": "fixture"}, idempotency_key="mesh-fixture-1", principal_id="p",
                  scopes=scopes)
    expansion = store.expand({"name": "clinical-mesh", "version": "1.0.0"}, "D003920", scopes=scopes)
    steps = explain_expansion(expansion)
    assert any(s["concept_id"] == "D003924" and "narrower -> D003924" in s["explanation"] for s in steps)
