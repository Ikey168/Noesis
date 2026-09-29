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

Public-health surveillance conditions (#1917, I06) use the same store: an ICD
module (``clinical-icd10.<system>``, version-tagged, e.g. ICD-10 WHO 2019) is
published beside ``clinical-mesh`` with its own crosswalk to MeSH, and the
source-native condition terms of the namespace's surveillance series (RKI
disease names, ECDC health topics, GHO indicator codes, Eurostat and Destatis
ICD-10 codes) are published as a registry-term module cross-walked to MeSH
(exact labels are ``equivalent``, reviewer curations keep their kind) and to
the ICD module (exact codes are ``equivalent``). :meth:`ClinicalTerms.expand_surveillance`
finds the surveillance series of an expanded condition with every step
explained; unmapped source terms are listed as gaps and an ``incompatible``
mapping blocks a term as it always has.
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


def icd_module(system):
    return f"clinical-icd10.{system}"


def icd_crosswalk_module(system):
    return f"clinical-icd10-mesh.{system}"


def surveillance_terms_module(namespace):
    return f"clinical-surveillance-terms.{_ns(namespace)}"


def surveillance_mesh_module(namespace):
    return f"clinical-surveillance-terms-mesh.{_ns(namespace)}"


def surveillance_icd_module(namespace):
    return f"clinical-surveillance-terms-icd.{_ns(namespace)}"


def surveillance_term_id(scheme, code):
    return f"sv:{scheme}:{code}"


def icd_concepts(codes):
    """ICD code records (code, title, parent, inclusions) as concepts; ``broader`` only to a parent in the set."""
    known = {str(item["code"]) for item in codes}
    concepts = []
    for item in codes:
        code = str(item["code"])
        labels = [{"value": str(item["title"]), "language": "en", "kind": "preferred"},
                  {"value": code, "language": "zxx", "kind": "notation"}]
        labels += [{"value": str(term), "language": "en", "kind": "alternative"} for term in item.get("inclusions")
                   or []]
        parent = str(item.get("parent") or "")
        concepts.append({"concept_id": code, "labels": labels, "definition": str(item.get("definition")
                                                                                 or item["title"]),
                         "broader": [parent] if parent in known else []})
    return concepts


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

    # ------------------------------------------------------------ ICD and surveillance terms

    def publish_icd(self, codes, version, *, system, principal_id, scopes, mesh_version=None, curations=(),
                    provenance=None):
        """Publish a version-tagged ICD module beside ``clinical-mesh`` and, given a MeSH version, its crosswalk."""
        if not str(system or "").replace("-", "").isalnum():
            raise ClinicalRecordError("invalid_icd_system", "ICD systems are named like who or gm")
        concepts = icd_concepts(codes)
        module = self.ontology.publish(
            icd_module(system), version, concepts, owner=OWNER,
            provenance=dict(provenance or {"kind": "imported", "source": f"ICD-10 ({system}) codes, selected subset"}),
            idempotency_key=f"{icd_module(system)}:{version}:{digest(concepts)[:16]}", principal_id=principal_id,
            scopes=scopes, compatibility_policy="none")
        result = {"icd": {"name": icd_module(system), "version": version, "module_id": module["module_id"]},
                  "crosswalk": None, "mappings": [], "unmapped": []}
        if mesh_version is None:
            return result
        mesh = self.ontology.inspect(MESH_MODULE, mesh_version, scopes=set(scopes) | {SCHEMA_READ})
        mappings, unmapped = label_crosswalk(concepts, mesh["content"]["concepts"], curations=list(curations))
        if mappings:
            crosswalk = self.ontology.register_crosswalk(
                icd_crosswalk_module(system), version, {"name": icd_module(system), "version": version},
                {"name": MESH_MODULE, "version": mesh_version}, mappings, owner=OWNER,
                provenance={"kind": "agent", "source": "normalized-label-match and reviewer curations"},
                idempotency_key=f"{icd_crosswalk_module(system)}:{version}:{digest(mappings)[:16]}",
                principal_id=principal_id, scopes=scopes)
            result["crosswalk"] = {"name": icd_crosswalk_module(system), "version": version,
                                   "module_id": crosswalk["module_id"]}
        result["mappings"], result["unmapped"] = mappings, unmapped
        return result

    def surveillance_terms(self, namespace, *, scopes):
        """Source-native condition terms of the namespace's surveillance series, with the series that use them."""
        from src.kb.surveillance import SurveillanceStore, authorize

        authorize(namespace, scopes, "knowledge:clinical:read")
        terms: dict[str, dict[str, Any]] = {}
        for series in SurveillanceStore(self.conn, initialize=False).find_series(namespace, limit=5000):
            condition = series["condition"]
            concept_id = surveillance_term_id(condition["scheme"], condition["code"])
            entry = terms.setdefault(concept_id, {"scheme": condition["scheme"], "code": condition["code"],
                                                  "labels": set(), "providers": set(), "series": []})
            if condition.get("label"):
                entry["labels"].add(str(condition["label"]))
            entry["providers"].add(series["provider"])
            entry["series"].append(series["series_id"])
        return terms

    def align_surveillance(self, namespace, *, principal_id, scopes, mesh_version, icd=None, curations=(),
                           version=None):
        """Publish surveillance condition terms and their crosswalks to MeSH (labels) and ICD (codes)."""
        _require_write(namespace, scopes)
        terms = self.surveillance_terms(namespace, scopes=scopes)
        if not terms:
            raise ClinicalRecordError("no_terms", "no surveillance series are recorded in this namespace")
        concepts = []
        for concept_id, entry in sorted(terms.items()):
            labels = [{"value": label, "language": "und", "kind": "native"} for label in sorted(entry["labels"])]
            labels.append({"value": entry["code"], "language": "zxx", "kind": "notation"})
            concepts.append({"concept_id": concept_id, "labels": labels, "scheme": entry["scheme"],
                             "definition": f"Surveillance condition {entry['scheme']}:{entry['code']} as published by "
                                           + ", ".join(sorted(entry["providers"]))})
        name = surveillance_terms_module(namespace)
        version = version or self._next_version(name, concepts)
        module = self.ontology.publish(
            name, version, concepts, owner=OWNER,
            provenance={"kind": "agent", "source": "surveillance condition terms (namespace " + _ns(namespace) + ")"},
            idempotency_key=f"{name}:{version}:{digest(concepts)[:16]}", principal_id=principal_id, scopes=scopes,
            compatibility_policy="none")
        mesh = self.ontology.inspect(MESH_MODULE, mesh_version, scopes=set(scopes) | {SCHEMA_READ})
        mesh_curations = [c for c in curations if c.get("target_module", "mesh") == "mesh"]
        icd_curations = [c for c in curations if c.get("target_module") == "icd"]
        # Labels only: a code is never matched against a MeSH label.
        by_label = [{**c, "labels": [label for label in c["labels"] if label["kind"] != "notation"]}
                    for c in concepts]
        mesh_mappings, mesh_unmapped = label_crosswalk(by_label, mesh["content"]["concepts"],
                                                       curations=mesh_curations)
        result = {"terms_module": {"name": name, "version": version, "module_id": module["module_id"]},
                  "mesh": {"name": MESH_MODULE, "version": mesh_version}, "mesh_crosswalk": None, "icd": None,
                  "icd_crosswalk": None, "mappings": {"mesh": mesh_mappings, "icd": []}}
        if mesh_mappings:
            crosswalk = self.ontology.register_crosswalk(
                surveillance_mesh_module(namespace), version, {"name": name, "version": version},
                {"name": MESH_MODULE, "version": mesh_version}, mesh_mappings, owner=OWNER,
                provenance={"kind": "agent", "source": "normalized-label-match and reviewer curations"},
                idempotency_key=f"{surveillance_mesh_module(namespace)}:{version}:{digest(mesh_mappings)[:16]}",
                principal_id=principal_id, scopes=scopes)
            result["mesh_crosswalk"] = {"name": surveillance_mesh_module(namespace), "version": version,
                                        "module_id": crosswalk["module_id"]}
        icd_mapped = set()
        if icd:
            icd_ref = {"name": icd_module(icd["system"]), "version": icd["version"]}
            icd_ids = {c["concept_id"] for c in self.ontology.inspect(
                icd_ref["name"], icd_ref["version"], scopes=set(scopes) | {SCHEMA_READ})["content"]["concepts"]}
            mappings = []
            for concept in concepts:
                code = concept["concept_id"].split(":", 2)[2]
                if concept["scheme"] in {"eurostat-icd10", "destatis-icd10"} and code in icd_ids:
                    mappings.append({"source": concept["concept_id"], "target": code, "kind": "equivalent",
                                     "confidence": 1.0, "evidence": [{"rule": "published-icd-code", "code": code}]})
            for curation in icd_curations:
                if curation.get("target") in icd_ids and curation.get("source") in terms:
                    mappings.append({"source": curation["source"], "target": curation["target"],
                                     "kind": curation["kind"], "confidence": float(curation.get("confidence", 1.0)),
                                     "evidence": [{"rule": "curation", "reviewer": curation.get("reviewer"),
                                                   "rationale": curation.get("rationale")}]})
            result["icd"] = icd_ref
            result["mappings"]["icd"] = mappings
            if mappings:
                crosswalk = self.ontology.register_crosswalk(
                    surveillance_icd_module(namespace), version, {"name": name, "version": version}, icd_ref,
                    mappings, owner=OWNER, provenance={"kind": "agent", "source": "published ICD codes and "
                                                                                  "reviewer curations"},
                    idempotency_key=f"{surveillance_icd_module(namespace)}:{version}:{digest(mappings)[:16]}",
                    principal_id=principal_id, scopes=scopes)
                result["icd_crosswalk"] = {"name": surveillance_icd_module(namespace), "version": version,
                                           "module_id": crosswalk["module_id"]}
            icd_mapped = {m["source"] for m in mappings if m["kind"] != "incompatible"}
        labels = {c["concept_id"]: c["labels"][0]["value"] for c in concepts}
        result["unmapped"] = [
            {"concept_id": c, "label": labels[c], "series": terms[c]["series"],
             "reason": "no MeSH label, published ICD code or reviewed curation maps this term; left unmapped"}
            for c in sorted(set(mesh_unmapped) - icd_mapped)]
        return result

    def _crosswalk(self, name, scopes):
        try:
            module = self.ontology.registry.resolve("crosswalk", name, "*", scopes={SCHEMA_READ})
        except SchemaRegistryError:
            return None
        source = module["content"]["source"]
        concepts = {c["concept_id"]: c for c in self.ontology.inspect(
            source["name"], source["version"], scopes={SCHEMA_READ} | set(scopes))["content"]["concepts"]}
        return {"module": module, "mappings": module["content"]["mappings"], "source": source, "concepts": concepts}

    def expand_surveillance(self, namespace, text, *, scopes, max_depth=3):
        """The surveillance series of an expanded condition; every step explained, unmapped terms listed as gaps."""
        from src.kb.surveillance import SurveillanceStore, authorize

        authorize(namespace, scopes, "knowledge:clinical:read")
        key = normalize_label(text)
        base = self.expand(namespace, text, scopes=scopes, max_depth=max_depth)
        mesh_ids, steps, conflicts = set(base["mesh_ids"]), list(base["steps"]), list(base["conflicts"])
        terms_xw = self._crosswalk(surveillance_mesh_module(namespace), scopes)
        icd_xw = self._crosswalk(surveillance_icd_module(namespace), scopes)
        matched: dict[str, dict[str, Any]] = {}
        blocked: list[dict[str, Any]] = []
        # A surveillance term named as written (its label or its code) is the start when no vocabulary names it.
        term_module = None
        try:
            term_module = self.ontology.registry.resolve("ontology", surveillance_terms_module(namespace), "*",
                                                         scopes={SCHEMA_READ})
        except SchemaRegistryError:
            pass
        if term_module is not None:
            for concept in term_module["content"]["concepts"]:
                if key and key in {normalize_label(label["value"]) for label in concept["labels"]}:
                    matched[concept["concept_id"]] = {"via": "named as written", "steps": []}
                    for mapping in (terms_xw or {}).get("mappings") or []:
                        if mapping["source"] == concept["concept_id"] and mapping["kind"] == "equivalent":
                            mesh_ids.add(mapping["target"])
                            steps.append({"concept_id": mapping["target"], "ontology": MESH_MODULE,
                                          "explanation": f"{concept['concept_id']} maps equivalent to "
                                                         f"{mapping['target']} via "
                                                         f"{terms_xw['module']['module_id']}"})
        if mesh_ids and not base["mesh_ids"]:
            # Expand the MeSH concepts reached through the surveillance term within the MeSH hierarchy.
            mesh = self.ontology.registry.resolve("ontology", MESH_MODULE, "*", scopes={SCHEMA_READ})
            for concept_id in sorted(mesh_ids):
                if concept_id not in {c["concept_id"] for c in mesh["content"]["concepts"]}:
                    continue
                expansion = self.ontology.expand({"name": MESH_MODULE, "version": mesh["semantic_version"]},
                                                 concept_id, scopes={SCHEMA_READ} | set(scopes),
                                                 relationships=("equivalent", "narrower"), max_depth=max_depth,
                                                 max_terms=50)
                conflicts += expansion["conflicts"]
                steps += explain_expansion(expansion)
                mesh_ids |= {t["concept_id"] for t in expansion["terms"] if t["ontology"] == MESH_MODULE}
        # ICD concepts equivalent to (or narrower than) a reached MeSH concept, and their ICD descendants.
        icd_reached: dict[str, str] = {}
        icd_blocked: set[str] = set()
        icd_systems = set()
        if icd_xw is not None:
            icd_systems.add(icd_xw["module"]["content"]["target"]["name"].split(".", 1)[1])
        for system in sorted(icd_systems):
            icd_mesh = self._crosswalk(icd_crosswalk_module(system), scopes)
            if icd_mesh is None:
                continue
            for mapping in icd_mesh["mappings"]:
                if mapping["target"] not in mesh_ids:
                    continue
                if mapping["kind"] == "incompatible":
                    icd_blocked.add(mapping["source"])
                elif mapping["kind"] in {"equivalent", "broader"}:
                    icd_reached.setdefault(mapping["source"], f"{mapping['source']} maps {mapping['kind']} to "
                                                              f"{mapping['target']} via "
                                                              f"{icd_mesh['module']['module_id']}")
            children: dict[str, list[str]] = {}
            for concept in icd_mesh["concepts"].values():
                for parent in concept.get("broader") or []:
                    children.setdefault(parent, []).append(concept["concept_id"])
            queue = list(icd_reached)
            while queue:
                parent = queue.pop()
                for child in sorted(children.get(parent, [])):
                    if child not in icd_reached and child not in icd_blocked:
                        icd_reached[child] = f"{child} is narrower than {parent} in {icd_mesh['source']['name']}"
                        queue.append(child)
        for concept_id, explanation in sorted(icd_reached.items()):
            steps.append({"concept_id": concept_id, "ontology": "icd", "explanation": explanation})
        reached = mesh_ids | set(icd_reached)
        for crosswalk in (terms_xw, icd_xw):
            if crosswalk is None:
                continue
            bad = {m["source"]: m["target"] for m in crosswalk["mappings"]
                   if m["kind"] == "incompatible" and m["target"] in reached}
            blocked += [{"concept_id": source, "incompatible_with": target,
                         "reason": "an incompatible mapping to a reached concept blocks this term"}
                        for source, target in sorted(bad.items())
                        if source not in {b["concept_id"] for b in blocked}]
            for mapping in crosswalk["mappings"]:
                if mapping["target"] not in reached or mapping["kind"] not in {"equivalent", "broader"}:
                    continue
                if mapping["source"] in bad:
                    continue
                evidence = (mapping.get("evidence") or [{}])[0]
                entry = matched.setdefault(mapping["source"], {"via": "crosswalk", "steps": []})
                entry["steps"].append(
                    f"{mapping['source']} maps {mapping['kind']} to {mapping['target']} via "
                    f"{crosswalk['module']['module_id']} ({evidence.get('rule')}"
                    + (f", reviewed by {evidence['reviewer']}" if evidence.get("reviewer") else "") + ")")
        for item in blocked:
            matched.pop(item["concept_id"], None)
        store = SurveillanceStore(self.conn, initialize=False)
        series = []
        for concept_id, entry in sorted(matched.items()):
            _, scheme, code = concept_id.split(":", 2)
            for found in store.find_series(namespace, condition_scheme=scheme, condition_codes=[code]):
                series.append({"series_id": found["series_id"], "provider": found["provider"],
                               "condition": found["condition"], "geography": found["geography"],
                               "kind": found["kind"], "unit": found["unit"], "interval": found["interval"],
                               "term": concept_id, "via": entry["via"], "steps": entry["steps"]})
        gaps = []
        if term_module is not None:
            mapped = {m["source"] for xw in (terms_xw, icd_xw) if xw for m in xw["mappings"]
                      if m["kind"] != "incompatible"}
            gaps = [{"concept_id": c["concept_id"], "label": c["labels"][0]["value"],
                     "reason": "unmapped source term: no MeSH or ICD mapping; its series are not reached by "
                               "expansion"} for c in term_module["content"]["concepts"] if c["concept_id"] not in mapped]
        return {"input": text, "normalized": key, "mesh_ids": sorted(mesh_ids), "icd_codes": sorted(icd_reached),
                "steps": steps, "conflicts": conflicts, "blocked": sorted(blocked, key=lambda b: b["concept_id"]),
                "series": series, "unmapped_terms": gaps,
                "note": "series of every source are listed side by side under the terms that reached them; nothing "
                        "is merged"}

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


__all__ = ["ClinicalTerms", "MESH_MODULE", "OntologyError", "crosswalk_module", "icd_crosswalk_module", "icd_module",
           "surveillance_icd_module", "surveillance_mesh_module", "surveillance_terms_module", "term_id",
           "terms_module"]
