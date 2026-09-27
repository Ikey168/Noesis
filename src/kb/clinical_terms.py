"""Align clinical condition and intervention terms to MeSH through ontology crosswalks (H10).

MeSH descriptors are published as an ontology module (``clinical-mesh``)
through :class:`~src.kb.ontology.OntologyAlignmentStore`, keeping their tree
hierarchy. Registry-native terms (CT.gov/CTIS/EU CTR conditions and
interventions as written, EMA therapeutic-area text, openFDA substance names)
are published per namespace as their own ontology module, and a crosswalk maps
them to MeSH with preserved kinds: exact label matches are ``equivalent``;
reviewed curations may be ``equivalent``, ``broader``, ``narrower``,
``related`` or ``incompatible``. Question expansion uses the existing
``expand`` (incompatible pairs block expansion as they always have) and
explains every step. Terms with no mapping stay unmapped and are listed as
coverage gaps; FAERS reaction terms are MedDRA (a licensed terminology) and
are kept as reported, not mapped.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from src.kb.clinical_records import ClinicalRecordError, ClinicalRecordStore, _require_read, _require_write, digest
from src.kb.ontology import (
    OntologyAlignmentStore,
    OntologyError,
    descriptor_concepts,
    explain_expansion,
    label_crosswalk,
    normalize_label,
)
from src.kb.schema_registry import SchemaRegistryError

MESH_MODULE = "clinical-mesh"
OWNER = "clinical-evidence"
SCHEMA_READ = "knowledge:schema:read"
SCHEMA_REGISTER = "knowledge:schema:register"


def _ns(namespace):
    return hashlib.sha256(namespace.encode()).hexdigest()[:12]


def terms_module(namespace):
    return f"clinical-terms.{_ns(namespace)}"


def crosswalk_module(namespace):
    return f"clinical-terms-mesh.{_ns(namespace)}"


def term_id(label):
    return "term:" + normalize_label(label).replace(" ", "-")


def _strip_type(name):
    return re.sub(r"^(drug|biological|device|procedure|other|behavioral|dietary supplement):\s*", "", str(name or ""),
                  flags=re.IGNORECASE)


class ClinicalTerms:
    def __init__(self, conn, *, initialize=True, now=None):
        self.conn = conn
        self.records = ClinicalRecordStore(conn, initialize=initialize, now=now)
        self.ontology = OntologyAlignmentStore(conn, initialize=initialize, now=now)

    # -------------------------------------------------------------- publish

    def publish_mesh(self, descriptors, version, *, principal_id, scopes, provenance=None):
        concepts = descriptor_concepts(descriptors)
        return self.ontology.publish(
            MESH_MODULE, version, concepts, owner=OWNER,
            provenance=dict(provenance or {"kind": "imported", "source": "MeSH descriptors (NLM), selected subset"}),
            idempotency_key=f"{MESH_MODULE}:{version}:{digest(concepts)[:16]}", principal_id=principal_id,
            scopes=scopes, compatibility_policy="none")

    def registry_terms(self, namespace, *, scopes):
        """Registry-native terms in the namespace with the records that use them."""
        _require_read(namespace, scopes)
        terms: dict[str, dict[str, Any]] = {}

        def add(label, role, source):
            key = normalize_label(label)
            if not key:
                return
            entry = terms.setdefault(key, {"labels": set(), "roles": set(), "sources": []})
            entry["labels"].add(str(label).strip())
            entry["roles"].add(role)
            if source not in entry["sources"]:
                entry["sources"].append(source)

        reactions = set()
        for row in self.records.find(namespace, scopes=scopes, kinds={"registered-trial", "regulatory-record",
                                                                       "review-registration"}):
            record, source = row["record"], {"record_id": row["record_id"], "provider": row["provider"],
                                             "native_id": row["native_id"]}
            if record["record_kind"] == "registered-trial":
                for condition in record.get("conditions") or []:
                    add(condition["term"], "condition", source)
                for intervention in record.get("interventions") or []:
                    add(_strip_type(intervention["name"]), "intervention", source)
            elif record["record_kind"] == "regulatory-record":
                for mesh in record.get("mesh_terms") or []:
                    add(mesh, "condition", source)
                for name in (record.get("product") or {}).get("generic_names") or []:
                    add(name, "intervention", source)
                reactions |= {c["term"] for c in record.get("counts") or []}
            elif record.get("condition"):
                add(record["condition"], "condition", source)
        return terms, sorted(reactions)

    def align(self, namespace, *, principal_id, scopes, mesh_version, curations=(), version=None):
        """Publish the namespace's registry-native terms and their crosswalk to MeSH."""
        _require_write(namespace, scopes)
        terms, reactions = self.registry_terms(namespace, scopes=scopes)
        if not terms:
            raise ClinicalRecordError("no_terms", "no registry-native terms are recorded in this namespace")
        mesh = self.ontology.inspect(MESH_MODULE, mesh_version, scopes=scopes)
        concepts = [{"concept_id": term_id(key),
                     "labels": [{"value": label, "language": "en", "kind": "native"} for label in sorted(entry["labels"])],
                     "definition": "Registry-native " + "/".join(sorted(entry["roles"])) + " term as written in "
                                   + ", ".join(f"{s['provider']}:{s['native_id']}" for s in entry["sources"][:10]),
                     "roles": sorted(entry["roles"])}
                    for key, entry in sorted(terms.items())]
        version = version or self._next_version(terms_module(namespace), concepts)
        module = self.ontology.publish(
            terms_module(namespace), version, concepts, owner=OWNER,
            provenance={"kind": "agent", "source": "registry-native terms from clinical records (namespace "
                                                   + _ns(namespace) + ")"},
            idempotency_key=f"{terms_module(namespace)}:{version}:{digest(concepts)[:16]}",
            principal_id=principal_id, scopes=scopes, compatibility_policy="none")
        curated = [{**c, "source": term_id(c["source"])} for c in curations]
        mappings, unmapped = label_crosswalk(concepts, mesh["content"]["concepts"], curations=curated)
        crosswalk = None
        if mappings:
            crosswalk = self.ontology.register_crosswalk(
                crosswalk_module(namespace), version, {"name": terms_module(namespace), "version": version},
                {"name": MESH_MODULE, "version": mesh_version}, mappings, owner=OWNER,
                provenance={"kind": "agent", "source": "normalized-label-match and reviewer curations"},
                idempotency_key=f"{crosswalk_module(namespace)}:{version}:{digest(mappings)[:16]}",
                principal_id=principal_id, scopes=scopes)
        labels = {c["concept_id"]: c["labels"][0]["value"] for c in concepts}
        return {"terms_module": {"name": terms_module(namespace), "version": version,
                                 "module_id": module["module_id"]},
                "crosswalk": None if crosswalk is None else {"name": crosswalk_module(namespace), "version": version,
                                                             "module_id": crosswalk["module_id"]},
                "mesh": {"name": MESH_MODULE, "version": mesh_version},
                "mappings": [{**m, "source_label": labels.get(m["source"])} for m in mappings],
                "unmapped": [{"concept_id": c, "label": labels[c],
                              "reason": "no MeSH descriptor label or reviewed curation matches; left unmapped"}
                             for c in unmapped],
                "unmapped_reaction_terms": [{"term": t, "terminology": "MedDRA (as reported by FAERS)",
                                             "reason": "MedDRA is licensed; reaction terms are kept as reported"}
                                            for t in reactions]}

    def _next_version(self, name, concepts):
        try:
            latest = self.ontology.registry.resolve("ontology", name, "*", scopes={SCHEMA_READ})
        except SchemaRegistryError:
            return "1.0.0"
        if [c["concept_id"] for c in latest["content"]["concepts"]] == sorted(c["concept_id"] for c in concepts) and \
                digest(latest["content"]["concepts"]) == digest(_normalized(concepts)):
            return latest["semantic_version"]
        major, minor, patch = (int(v) for v in latest["semantic_version"].split("."))
        return f"{major}.{minor + 1}.0"

    # --------------------------------------------------------------- expand

    def expand(self, namespace, text, *, scopes, relationships=("equivalent", "narrower"), max_depth=3):
        """Expand one question term; every added term carries its explained path."""
        _require_read(namespace, scopes)
        key = normalize_label(text)
        result = {"input": text, "normalized": key, "labels": {key} if key else set(), "mesh_ids": set(),
                  "steps": [], "conflicts": [], "mapped": False, "start": None}
        start = None
        try:
            latest = self.ontology.registry.resolve("ontology", terms_module(namespace), "*", scopes={SCHEMA_READ})
            if term_id(text) in {c["concept_id"] for c in latest["content"]["concepts"]}:
                start = ({"name": terms_module(namespace), "version": latest["semantic_version"]}, term_id(text))
        except SchemaRegistryError:
            latest = None
        if start is None:
            try:
                mesh = self.ontology.registry.resolve("ontology", MESH_MODULE, "*", scopes={SCHEMA_READ})
                for concept in mesh["content"]["concepts"]:
                    if key in {normalize_label(label["value"]) for label in concept["labels"]}:
                        start = ({"name": MESH_MODULE, "version": mesh["semantic_version"]}, concept["concept_id"])
                        break
            except SchemaRegistryError:
                pass
        if start is None:
            result["reason"] = "term is in no published vocabulary; matched as written only"
            return _finish(result)
        expansion = self.ontology.expand(start[0], start[1], scopes={SCHEMA_READ} | set(scopes),
                                         relationships=relationships, max_depth=max_depth, max_terms=50)
        result.update(start={"ontology": start[0]["name"], "concept_id": start[1]},
                      conflicts=expansion["conflicts"], steps=explain_expansion(expansion),
                      expansion_hash=expansion["expansion_hash"])
        modules = {}
        for term in expansion["terms"]:
            name, version = term["ontology"], term["version"]
            if (name, version) not in modules:
                modules[(name, version)] = {c["concept_id"]: c for c in self.ontology.inspect(
                    name, version, scopes={SCHEMA_READ} | set(scopes))["content"]["concepts"]}
            concept = modules[(name, version)].get(term["concept_id"]) or {}
            result["labels"] |= {normalize_label(label["value"]) for label in concept.get("labels") or []}
            if name == MESH_MODULE:
                result["mesh_ids"].add(term["concept_id"])
        self._reverse(namespace, result, scopes)
        result["mapped"] = bool(result["mesh_ids"])
        if not result["mapped"]:
            result["reason"] = "no crosswalk mapping to MeSH; matched through registry-native labels only"
        return _finish(result)


    def _reverse(self, namespace, result, scopes):
        """Registry terms of the namespace that map equivalent (or narrower) to a reached MeSH concept."""
        if not result["mesh_ids"]:
            return
        try:
            crosswalk = self.ontology.registry.resolve("crosswalk", crosswalk_module(namespace), "*",
                                                       scopes={SCHEMA_READ})
            source = crosswalk["content"]["source"]
            terms = {c["concept_id"]: c for c in self.ontology.inspect(
                source["name"], source["version"], scopes={SCHEMA_READ} | set(scopes))["content"]["concepts"]}
        except SchemaRegistryError:
            return
        mappings = crosswalk["content"]["mappings"]
        reached = set(result["mesh_ids"])
        blocked = {m["source"] for m in mappings if m["kind"] == "incompatible" and m["target"] in reached}
        for mapping in mappings:
            # "broader" means the MeSH concept is broader than the registry term, so the term is narrower.
            if mapping["target"] not in reached or mapping["kind"] not in {"equivalent", "broader"}:
                continue
            if mapping["source"] in blocked or mapping["source"] not in terms:
                continue
            labels = {normalize_label(label["value"]) for label in terms[mapping["source"]]["labels"]}
            if labels <= set(result["labels"]):
                continue
            result["labels"] |= labels
            evidence = (mapping.get("evidence") or [{}])[0]
            result["steps"].append({
                "concept_id": mapping["source"], "ontology": source["name"], "score": mapping["confidence"],
                "explanation": f"registry term maps {mapping['kind']} to {mapping['target']} via crosswalk "
                               f"{crosswalk['module_id']} ({evidence.get('rule')}"
                               + (f", reviewed by {evidence['reviewer']}" if evidence.get("reviewer") else "") + ")"})


def _finish(result):
    result["labels"] = sorted(result["labels"])
    result["mesh_ids"] = sorted(result["mesh_ids"])
    return result


def _normalized(concepts):
    from src.kb.ontology import _validate_concepts

    return _validate_concepts(concepts)


__all__ = ["ClinicalTerms", "MESH_MODULE", "OntologyError", "crosswalk_module", "term_id", "terms_module"]
