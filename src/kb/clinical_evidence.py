"""Question-to-evidence map and the explained strength-of-evidence view (H09).

Given a clinical question (condition and intervention, optionally population,
comparator and outcomes), the map lists the registered trials and reviews
found, their design fields and registry versions, result availability,
regulatory records (with their providers' disclaimers), linked publications,
retractions and outcome-switching findings, and says which of these are
unknown.

The strength view is *rule-based*: every category comes from a documented
rule in :data:`DESIGN_RULES` / :data:`SUMMARY_RULES` and lists the recorded
fields (record id, revision) it was computed from. No score is invented, and
no GRADE (or other) grade is asserted unless a reviewer recorded every GRADE
domain in methodology provenance; otherwise the view states which inputs are
missing. The view is separate from argument-mining claim conclusions and
always carries the pack's non-advice boundary.

Maps are derived views: each pins the record revisions it used and becomes
stale when any of them (or the trial's postings and links) changes, until it
is recomputed.
"""

from __future__ import annotations

import json
import time

from src.kb.clinical_records import (
    READ_SCOPE,
    WRITE_SCOPE,
    ClinicalRecordError,
    ClinicalRecordStore,
    _require_read,
    _require_write,
    canonical,
    digest,
    normalize_text,
)

MAP_CONTRACT = "noesis-clinical-evidence-map-v1"
STRENGTH_CONTRACT = "noesis-clinical-strength-view-v1"
NON_ADVICE = (
    "This evidence map describes trial registrations, posted results, regulatory records and publications, and "
    "explains the strength of the evidence base from recorded methodology fields only. It is not medical advice, "
    "makes no dosing or treatment recommendation and draws no clinical conclusion. Unknown fields stay unknown; "
    "consult the primary sources and qualified professionals."
)
CLAIMS_SEPARATION = ("Argument-mining claim conclusions (for example noesis-research.literature_claims) are a "
                     "separate view and are not inputs to, or outputs of, this strength view.")
BLINDED = {"double", "triple", "quadruple"}
DESIGN_RULES = {
    "D1": {"category": "randomised-blinded-controlled",
           "rule": "allocation == randomized AND masking in {double, triple, quadruple} AND a comparator is recorded"},
    "D2": {"category": "randomised-open-or-single-blind",
           "rule": "allocation == randomized AND masking in {none, single}"},
    "D3": {"category": "randomised-comparator-unknown",
           "rule": "allocation == randomized AND masking blinded AND no comparator recorded"},
    "D4": {"category": "non-randomised",
           "rule": "allocation in {non-randomized, not-applicable}"},
    "D5": {"category": "design-unknown",
           "rule": "allocation or masking is unknown"},
}
RESULT_RULES = {
    "R1": "a registry result posting with acquired content exists -> results posted",
    "R2": "a registry result posting exists whose content was not acquired -> results available (not acquired)",
    "R3": "the registry states it has no results and no posting exists -> results not posted",
    "R4": "no posting and the registry does not say -> results unknown",
}
SUMMARY_RULES = {
    "S1": "two or more D1 trials with posted results (R1) whose linked publications are known not to be retracted",
    "S2": "exactly one D1 trial with posted results (R1) whose linked publications are known not to be retracted",
    "S3": "randomised trials found, none with posted results",
    "S4": "trials found, none randomised with a known design",
    "S5": "no registered trial found for the question",
    "S6": "randomised trials with posted results exist, but none is a D1 trial whose linked publications are known "
          "not to be retracted",
}
GRADE_DOMAINS = ("risk of bias", "inconsistency", "indirectness", "imprecision", "publication bias")
_DDL = """
CREATE TABLE IF NOT EXISTS clinical_evidence_views(
 view_id TEXT NOT NULL, revision BIGINT NOT NULL, namespace TEXT NOT NULL, owner TEXT NOT NULL,
 request_hash TEXT NOT NULL, content_json TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
 PRIMARY KEY(view_id, revision));
"""


def validate_question(question):
    if not isinstance(question, dict) or set(question) - {"population", "condition", "intervention", "comparator",
                                                          "outcomes"}:
        raise ClinicalRecordError("invalid_question", "question uses population/condition/intervention/comparator/"
                                                      "outcomes")
    for key in ("condition", "intervention"):
        if not isinstance(question.get(key), str) or not question[key].strip():
            raise ClinicalRecordError("invalid_question", f"question.{key} is required")
    outcomes = question.get("outcomes") or []
    if not isinstance(outcomes, list) or any(not isinstance(o, str) for o in outcomes):
        raise ClinicalRecordError("invalid_question", "question.outcomes is a list of text")
    return json.loads(canonical({"population": question.get("population"), "condition": question["condition"],
                                 "intervention": question["intervention"], "comparator": question.get("comparator"),
                                 "outcomes": outcomes}))


def _strip_type(name):
    import re

    return re.sub(r"^[a-z ]+:\s*", "", str(name or ""), flags=re.IGNORECASE)


class EvidenceMapService:
    def __init__(self, conn, *, initialize=True, now=None):
        self.conn, self.initialize = conn, initialize
        self.now = now or (lambda: int(time.time() * 1000))
        self.records = ClinicalRecordStore(conn, initialize=initialize, now=self.now)
        if initialize:
            conn.execute(_DDL)

    # --------------------------------------------------------------- inputs

    def _expand(self, namespace, question, scopes):
        from src.kb.clinical_terms import ClinicalTerms

        terms = ClinicalTerms(self.conn, initialize=self.initialize, now=self.now)
        expansions = {}
        for key in ("condition", "intervention", "comparator"):
            if question.get(key):
                expansions[key] = terms.expand(namespace, question[key], scopes=scopes)
        return expansions

    def _trial_terms(self, namespace, row):
        trial = row["record"]
        conditions = {normalize_text(c["term"]) for c in trial.get("conditions") or []}
        interventions = {normalize_text(_strip_type(i["name"])) for i in trial.get("interventions") or []}
        interventions |= {normalize_text(n) for i in trial.get("interventions") or [] for n in i.get("other_names") or []}
        condition_mesh, intervention_mesh = set(), set()
        for (terms_json,) in self.conn.execute(
                "SELECT terms_json FROM clinical_term_annotations WHERE namespace=? AND record_id=?",
                [namespace, row["record_id"]]).fetchall():
            terms = json.loads(terms_json)
            condition_mesh |= {m["id"] for m in (terms.get("conditions") or {}).get("meshes") or []}
            intervention_mesh |= {m["id"] for m in (terms.get("interventions") or {}).get("meshes") or []}
        return conditions, interventions, condition_mesh, intervention_mesh

    def _matches(self, expansion, labels, mesh):
        hits = sorted(set(expansion["labels"]) & labels)
        mesh_hits = sorted(set(expansion["mesh_ids"]) & mesh)
        if hits:
            return {"via": "registry term", "terms": hits}
        if mesh_hits:
            return {"via": "registry-derived MeSH annotation", "mesh_ids": mesh_hits}
        return None

    # ---------------------------------------------------------------- trial

    def _trial_entry(self, namespace, row, *, principal_id, scopes, pins, documents_for_methods):
        from src.kb.clinical_methodology import TrialMethodology
        from src.kb.clinical_publications import PublicationLinker

        view = self.records.trial(namespace, row["record_id"], scopes=scopes)
        trial = view["trial"]
        pins[row["record_id"]] = view["current_revision"]
        history = self.records.history(namespace, row["record_id"], scopes=scopes)
        versions = [{"revision": r["revision"], "version_key": r["version_key"], "date": r["version_date"],
                     "status": (r["record"].get("status") or {}).get("normalized")} for r in history["revisions"]]
        for member in view["arms"] + view["outcomes"] + view["result_postings"]:
            pins[member["record_id"]] = member["revision"]
        for link in view["links"] + view["linked_from"]:
            pins[link["link_id"]] = self.records.get(namespace, link["link_id"], scopes=scopes)["revision"]
        postings = [p["record"] for p in view["result_postings"]]
        indicator = (trial.get("results_indicator") or {}).get("has_results")
        if any(p["content_acquired"] for p in postings):
            results = {"state": "posted", "rule": "R1"}
        elif postings:
            results = {"state": "available-not-acquired", "rule": "R2"}
        elif indicator is False:
            results = {"state": "not-posted", "rule": "R3"}
        else:
            results = {"state": "unknown", "rule": "R4"}
        results.update(postings=[{"record_id": p["record_id"], "revision": p["revision"],
                                  "posted": p["record"]["posted"], "source_url": p["record"]["source_url"],
                                  "results_summary_url": p["record"].get("results_summary_url")}
                                 for p in view["result_postings"]],
                       registry_indicator=indicator)
        status = (trial.get("status") or {}).get("normalized")
        completion = (trial.get("registration") or {}).get("completion_date")
        if status in {"completed", "terminated", "ended"} and results["state"] in {"not-posted", "unknown"}:
            results["note"] = (f"registry status is {status} (completion {completion or 'date unknown'}) and no "
                               "results posting is recorded; publications are not treated as results")
        publications = PublicationLinker(self.conn, initialize=self.initialize, now=self.now).publications(
            namespace, row["record_id"], principal_id=principal_id, scopes=scopes)
        methodology = TrialMethodology(self.conn, now=self.now)
        docs = [d for d in documents_for_methods if d["document_id"] in {p["document_id"] for p in
                                                                          publications["publications"]}]
        try:
            switching = methodology.outcome_switching(namespace, row["record_id"], principal_id=principal_id,
                                                      scopes=scopes, documents=docs)
            design = (switching["study_revisions"][-1]["trial"] if switching["study_revisions"] else None)
            methodology_ref = {"study_id": switching["study_id"],
                               "study_revision_id": switching["study_revisions"][-1]["study_revision_id"]}
            findings = switching["findings"]
        except Exception as exc:  # noqa: BLE001 - methodology access is reported, not assumed
            design, findings, methodology_ref = None, [], {"unavailable": getattr(exc, "code", type(exc).__name__)}
        design = design or {}
        category = self._design_category(trial, view, design)
        unknown = sorted(set(trial.get("unknowns") or []) | {f"methodology.{u}" for u in design.get("unknown") or []}
                         | ({"results"} if results["state"] == "unknown" else set())
                         | ({"publications"} if not publications["publications"] else set()))
        return {
            "record_id": row["record_id"], "revision": view["current_revision"], "registry": trial["registry"],
            "identifier": trial["identifier"], "title": trial.get("title"), "source_url": trial["source_url"],
            "status": {"normalized": status, "native": (trial.get("status") or {}).get("native")},
            "versions": versions, "version_conflicts": view["version_conflicts"],
            "design": {"phase": design.get("phase"), "allocation": design.get("allocation"),
                       "masking": design.get("masking"), "sample_size_planned": design.get("sample_size_planned"),
                       "sample_size_actual": design.get("sample_size_actual"),
                       "preregistration": design.get("preregistration"),
                       "primary_outcomes": design.get("primary_outcomes"), "category": category,
                       "methodology": methodology_ref},
            "results": results,
            "publications": publications["publications"], "publication_candidates": publications["candidates"],
            "retracted": publications["retracted"], "retractions": publications["retractions"],
            "outcome_switching": findings,
            "cross_registry": view["cross_registry"],
            "unknowns": unknown,
            "source_freshness": view["source_freshness"],
        }

    @staticmethod
    def _design_category(trial, view, design):
        allocation = design.get("allocation") or (trial.get("design") or {}).get("allocation", "unknown")
        masking = design.get("masking") or (trial.get("design") or {}).get("masking", "unknown")
        arm_types = {str(a["record"].get("arm_type") or "") for a in view["arms"] if a["record"]["present"]}
        comparator = bool(arm_types & {"PLACEBO_COMPARATOR", "ACTIVE_COMPARATOR", "SHAM_COMPARATOR",
                                       "NO_INTERVENTION"}) or (trial.get("design") or {}).get("controlled") is True \
            or len([i for i in trial.get("interventions") or []]) >= 2
        inputs = {"allocation": allocation, "masking": masking, "comparator_recorded": comparator,
                  "arm_types": sorted(t for t in arm_types if t)}
        missing = [k for k in ("allocation", "masking") if inputs[k] in (None, "unknown")]
        if allocation in {"non-randomized", "not-applicable"}:
            rule = "D4"
        elif missing:
            rule = "D5"
        elif allocation == "randomized" and masking in BLINDED:
            rule = "D1" if comparator else "D3"
        else:
            rule = "D2"
        return {"rule_id": rule, "category": DESIGN_RULES[rule]["category"], "definition": DESIGN_RULES[rule]["rule"],
                "inputs": inputs, "missing_inputs": missing,
                "source": {"record_id": view["record_id"], "revision": view["revision"]}}

    # ---------------------------------------------------------------- build

    def linked_documents(self, namespace, *, scopes):
        """Current revisions of documents accepted as publications of any trial in the namespace."""
        from src.ingestion.document_store import DocumentStore

        store, docs = DocumentStore(self.conn), {}
        for row in self.records.find(namespace, scopes=scopes, kinds={"registry-link"}):
            link = row["record"]
            if link["link_kind"] != "registry-publication" or link["status"] != "accepted":
                continue
            document = store.get(link["to"]["document_id"])
            if document:
                docs[document["document_id"]] = {**document, "revision_id": link["to"]["revision_id"]}
        return [docs[k] for k in sorted(docs)]

    def build(self, namespace, question, *, principal_id, scopes, request_key, documents=None):
        """Build (or rebuild) an evidence map for a question; returns the stored view revision."""
        _require_write(namespace, scopes)
        if documents is None:
            documents = self.linked_documents(namespace, scopes=scopes)
        question = validate_question(question)
        view_id = "clinical-map:" + digest([namespace, principal_id, request_key])[:24]
        request_hash = digest(question)
        prior = self.conn.execute(
            "SELECT request_hash, revision FROM clinical_evidence_views WHERE view_id=? ORDER BY revision DESC LIMIT 1",
            [view_id]).fetchone()
        if prior and prior[0] != request_hash:
            raise ClinicalRecordError("idempotency_conflict", "request key already identifies another question")
        expansions = self._expand(namespace, question, scopes)
        pins: dict[str, int] = {}
        trials, excluded = [], []
        for row in self.records.find(namespace, scopes=scopes, kinds={"registered-trial"}):
            conditions, interventions, condition_mesh, intervention_mesh = self._trial_terms(namespace, row)
            condition = self._matches(expansions["condition"], conditions, condition_mesh)
            intervention = self._matches(expansions["intervention"], interventions, intervention_mesh)
            if condition and intervention:
                entry = self._trial_entry(namespace, row, principal_id=principal_id, scopes=scopes, pins=pins,
                                          documents_for_methods=list(documents))
                entry["matched"] = {"condition": condition, "intervention": intervention}
                if question.get("comparator"):
                    comparator = self._matches(expansions["comparator"], interventions, intervention_mesh)
                    entry["matched"]["comparator"] = comparator or {"via": None,
                                                                    "note": "comparator not recorded for this trial"}
                trials.append(entry)
            else:
                excluded.append({"record_id": row["record_id"], "identifier": row["native_id"],
                                 "condition_match": bool(condition), "intervention_match": bool(intervention)})
        intervention_labels = set(expansions["intervention"]["labels"])
        condition_labels = set(expansions["condition"]["labels"])
        regulatory = []
        for row in self.records.find(namespace, scopes=scopes, kinds={"regulatory-record"}):
            record = row["record"]
            names = {normalize_text(n) for n in (record.get("product") or {}).get("generic_names") or []}
            names |= {normalize_text((record.get("product") or {}).get(k)) for k in ("inn", "active_substance", "name")}
            if names & intervention_labels:
                pins[row["record_id"]] = row["revision"]
                regulatory.append({"record_id": row["record_id"], "revision": row["revision"],
                                   "authority": record["authority"], "regulatory_kind": record["regulatory_kind"],
                                   "title": record.get("title"), "native_version": record["native_version"],
                                   "status": record.get("status"), "dates": record.get("dates"),
                                   "source_url": record["source_url"], "disclaimer": record.get("disclaimer"),
                                   "counts": record.get("counts"), "reports_total": record.get("reports_total"),
                                   "count_semantics": record.get("count_semantics"),
                                   "omitted_sections": record.get("omitted_sections"),
                                   "unknowns": record.get("unknowns")})
        reviews = []
        for row in self.records.find(namespace, scopes=scopes, kinds={"review-registration"}):
            record = row["record"]
            text = normalize_text(" ".join(str(record.get(k) or "") for k in ("title", "condition", "interventions",
                                                                                 "review_question")))
            if any(label and label in text for label in condition_labels) and any(
                    label and label in text for label in intervention_labels):
                pins[row["record_id"]] = row["revision"]
                reviews.append({"record_id": row["record_id"], "revision": row["revision"],
                                "identifier": record["identifier"], "title": record["title"],
                                "status": record.get("status"), "registration_date": record.get("registration_date"),
                                "source": record["source"], "protocol_link": record.get("protocol_link"),
                                "note": "a registered review protocol, not review findings"})
        from src.kb.clinical_publications import PublicationLinker

        gaps = PublicationLinker(self.conn, initialize=self.initialize, now=self.now).coverage_gaps(
            namespace, scopes=scopes, trial_record_ids={t["record_id"] for t in trials})
        term_gaps = [{"question_field": key, "term": exp["input"], "reason": exp.get("reason")}
                     for key, exp in expansions.items() if not exp["mapped"]]
        content = {
            "contract": MAP_CONTRACT, "view_id": view_id, "namespace": namespace, "owner": principal_id,
            "request_key": request_key, "question": question,
            "expansion": {key: {k: v for k, v in exp.items() if k != "labels"} | {"matched_labels": exp["labels"]}
                          for key, exp in expansions.items()},
            "trials": sorted(trials, key=lambda t: (t["registry"], t["identifier"])),
            "excluded_trials": excluded, "reviews": reviews, "regulatory": regulatory,
            "coverage_gaps": {**{k: v for k, v in gaps.items() if k != "gaps_hash"}, "unmapped_terms": term_gaps},
            "boundary": NON_ADVICE, "claims": {"included": False, "note": CLAIMS_SEPARATION},
            "evidence_kind": self._evidence_kind(namespace),
        }
        content["strength"] = self.strength(content, namespace=namespace, scopes=scopes)
        revision = (prior[1] if prior else 0) + 1
        content.update(revision=revision, created_at_ms=self.now(), pins=dict(sorted(pins.items())))
        self.conn.execute("INSERT INTO clinical_evidence_views VALUES (?,?,?,?,?,?,?)",
                          [view_id, revision, namespace, principal_id, request_hash, canonical(content), self.now()])
        self.records.register_view(namespace, view_id, principal_id, pins)
        return {**content, "freshness": self.records.view_status(view_id)}

    def _evidence_kind(self, namespace):
        rows = self.conn.execute("SELECT provider, last_execution FROM clinical_provider_state WHERE namespace=?",
                                 [namespace]).fetchall()
        executions = {provider: execution for provider, execution in rows}
        live = sorted(p for p, e in executions.items() if e == "network")
        return {"providers": dict(sorted(executions.items())), "live_providers": live,
                "offline_only": not live,
                "note": "records acquired from injected fixtures are offline evidence, never live coverage"}

    # ------------------------------------------------------------- strength

    def strength(self, content, *, namespace, scopes):
        trials = content["trials"]
        rows = []
        for trial in trials:
            rows.append({"record_id": trial["record_id"], "revision": trial["revision"],
                         "identifier": trial["identifier"], "design_rule": trial["design"]["category"]["rule_id"],
                         "design_category": trial["design"]["category"]["category"],
                         "design_inputs": trial["design"]["category"]["inputs"],
                         "design_missing_inputs": trial["design"]["category"]["missing_inputs"],
                         "phase": trial["design"]["phase"],
                         "sample_size": {"planned": trial["design"]["sample_size_planned"],
                                         "actual": trial["design"]["sample_size_actual"]},
                         "prospectively_registered": (trial["design"].get("preregistration") or {}).get("prospective"),
                         "results_rule": trial["results"]["rule"], "results": trial["results"]["state"],
                         "publications_linked": len(trial["publications"]), "retracted": trial["retracted"],
                         "outcome_switching_findings": len(trial["outcome_switching"]),
                         "cross_registry_disagreements": sum(len(c["disagreements"]) for c in trial["cross_registry"]),
                         "unknowns": trial["unknowns"]})
        d1_posted = [r for r in rows if r["design_rule"] == "D1" and r["results"] == "posted"]
        clean = [r for r in d1_posted if r["retracted"] is False]
        randomised = [r for r in rows if r["design_rule"] in {"D1", "D2", "D3"}]
        if not rows:
            rule = "S5"
        elif len(clean) >= 2:
            rule = "S1"
        elif len(clean) == 1:
            rule = "S2"
        elif d1_posted or [r for r in randomised if r["results"] == "posted"]:
            rule = "S6"
        elif randomised:
            rule = "S3"
        else:
            rule = "S4"
        grade = self._grade(namespace, trials, scopes)
        unknown = sorted({u for r in rows for u in r["unknowns"]})
        return {
            "contract": STRENGTH_CONTRACT,
            "summary": {"rule": rule, "description": SUMMARY_RULES[rule],
                        "inputs": [{"record_id": r["record_id"], "revision": r["revision"], "design_rule": r["design_rule"],
                                    "results": r["results"], "retracted": r["retracted"]} for r in rows]},
            "trials": rows,
            "reviews_registered": len(content["reviews"]),
            "counts": {"trials": len(rows), "randomised": len(randomised),
                       "results_posted": sum(r["results"] == "posted" for r in rows),
                       "results_not_posted": sum(r["results"] == "not-posted" for r in rows),
                       "with_publications": sum(r["publications_linked"] > 0 for r in rows),
                       "retracted": sum(r["retracted"] is True for r in rows),
                       "retraction_status_unknown": sum(r["retracted"] is None for r in rows),
                       "with_outcome_switching_findings": sum(r["outcome_switching_findings"] > 0 for r in rows)},
            "grade": grade, "unknowns": unknown,
            "rules": {"design": DESIGN_RULES, "results": RESULT_RULES, "summary": SUMMARY_RULES},
            "score": None, "score_note": "no numeric strength score is computed",
            "boundary": NON_ADVICE, "claims": CLAIMS_SEPARATION,
        }

    def _grade(self, namespace, trials, scopes):
        """GRADE is reported only as recorded by a reviewer for every domain; never computed or inferred."""
        recorded = []
        if self.conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name='methodology_assessment_current'"
                             ).fetchone():
            for trial in trials:
                study = (trial["design"].get("methodology") or {}).get("study_id")
                if not study:
                    continue
                rows = self.conn.execute(
                    "SELECT r.dimension, r.rating, r.reviewer_id, r.assessment_revision_id FROM "
                    "methodology_assessment_current c JOIN methodology_assessment_revisions r ON "
                    "r.assessment_revision_id=c.assessment_revision_id WHERE r.namespace=? AND r.study_id=? AND "
                    "lower(r.framework)='grade'", [namespace, study]).fetchall()
                recorded += [{"study_id": study, "domain": d.casefold(), "rating": rating, "reviewer": reviewer,
                              "assessment_revision_id": revision} for d, rating, reviewer, revision in rows]
        rated = {r["domain"] for r in recorded if r["rating"] is not None}
        missing = [d for d in GRADE_DOMAINS if d not in rated]
        if missing:
            return {"asserted": False, "inputs_missing": missing, "recorded": recorded,
                    "reason": "GRADE needs reviewer judgements for every domain; missing inputs are not inferred"}
        return {"asserted": False, "recorded": recorded, "inputs_missing": [],
                "reason": "every GRADE domain has a recorded reviewer rating; they are shown as recorded, "
                          "no overall grade is computed by this view"}

    # ----------------------------------------------------------------- read

    def inspect(self, namespace, view_id, *, scopes, revision=None):
        _require_read(namespace, scopes)
        row = self.conn.execute(
            "SELECT content_json, revision FROM clinical_evidence_views WHERE view_id=? AND namespace=? AND "
            "revision=coalesce(?, (SELECT max(revision) FROM clinical_evidence_views WHERE view_id=?))",
            [view_id, namespace, revision, view_id]).fetchone()
        if not row:
            raise ClinicalRecordError("view_not_found", "evidence map is unavailable")
        content = json.loads(row[0])
        latest = self.conn.execute("SELECT max(revision) FROM clinical_evidence_views WHERE view_id=?",
                                   [view_id]).fetchone()[0]
        freshness = self.records.view_status(view_id) if row[1] == latest else {
            "view_id": view_id, "current": False, "stale": True, "superseded_by_revision": latest}
        return {**content, "freshness": freshness}

    def rebuild(self, namespace, view_id, *, principal_id, scopes, documents=None):
        current = self.inspect(namespace, view_id, scopes=scopes)
        if current["owner"] != principal_id and "operator" not in scopes:
            raise ClinicalRecordError("unauthorized", "only the map owner recomputes it")
        return self.build(namespace, current["question"], principal_id=current["owner"], scopes=scopes,
                          request_key=current["request_key"], documents=documents)

    # --------------------------------------------------------------- export

    def _record_evidence(self, namespace, record_id, revision, scopes):
        """A clinical record revision as a bundle ``evidence`` object citing its captured source document."""
        current = self.records.get(namespace, record_id, scopes=scopes, revision=revision)
        document = (current["evidence"] or {}).get("document") or {}
        return {"kind": "clinical-record", "record_id": record_id, "revision": current["revision"],
                "version_key": current["version_key"], "record": current["record"],
                "locator": {"cited": bool(document.get("document_id")), "document_id": document.get("document_id"),
                            "revision_id": document.get("revision_id"),
                            "source_url": current["record"].get("source_url")},
                "acquisition": {k: v for k, v in (current["evidence"] or {}).items() if k != "document"}}

    def export_bundle(self, namespace, view_id, *, scopes, revision=None):
        """An evidence bundle (noesis-evidence-bundle-v1) for one map revision, gaps as omissions."""
        from src.evidence_bundle.builder import EvidenceBundleBuilder

        content = self.inspect(namespace, view_id, scopes=scopes, revision=revision)
        builder = EvidenceBundleBuilder("receipt", {"operation": "clinical-evidence-map", "view_id": view_id,
                                                    "revision": content["revision"], "question": content["question"]},
                                        created_at_ms=content["created_at_ms"])
        refs = []

        def add_record(record_id, record_revision):
            object_id = f"clinical-record:{record_id}@{record_revision}"
            builder.add_object("evidence", self._record_evidence(namespace, record_id, record_revision, scopes),
                               object_id=object_id)
            refs.append(object_id)

        for trial in content["trials"]:
            add_record(trial["record_id"], trial["revision"])
            for posting in trial["results"]["postings"]:
                add_record(posting["record_id"], posting["revision"])
            for publication in trial["publications"]:
                eid = f"evidence:{trial['registry']}:{trial['identifier']}:{publication['document_id']}@" \
                      f"{publication['revision_id']}"
                builder.add_object("evidence", {"kind": "linked-publication",
                                                "locator": {"cited": True, "document_id": publication["document_id"],
                                                            "revision_id": publication["revision_id"]},
                                                "evidence_kinds": publication["evidence_kinds"],
                                                "notices": publication.get("notices") or []}, object_id=eid)
                refs.append(eid)
            builder.add_external_reference(f"registry:{trial['registry']}:{trial['identifier']}", trial["source_url"],
                                           required=False)
        for item in content["regulatory"] + content["reviews"]:
            add_record(item["record_id"], item["revision"])
        map_payload = {k: v for k, v in content.items() if k not in {"freshness"}}
        builder.add_object("receipt", {"kind": "clinical-evidence-map", **map_payload},
                           object_id=f"{view_id}@{content['revision']}", references=refs, root=True)
        gaps = content["coverage_gaps"]
        for trial in gaps["unlinked_trials"]:
            builder.add_omission(f"unlinked trial {trial['identifier']}: {trial['reason']}")
        for publication in gaps["unregistered_publications"]:
            builder.add_omission(f"unregistered publication {publication['document_id']}: {publication['reason']}")
        for term in gaps["unmapped_terms"]:
            builder.add_omission(f"unmapped term {term['term']!r}: {term['reason']}")
        if content["freshness"].get("stale"):
            builder.add_omission("the map is stale: recorded sources changed after it was computed")
        bundle = builder.build()
        return {"bundle": bundle, "view_id": view_id, "revision": content["revision"],
                "boundary": NON_ADVICE, "freshness": content["freshness"]}


__all__ = ["EvidenceMapService", "NON_ADVICE", "READ_SCOPE", "WRITE_SCOPE", "validate_question"]
