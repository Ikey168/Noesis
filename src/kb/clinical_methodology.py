"""Trial design in methodology provenance and outcome-switching detection (H08).

Every registry version of a trial becomes one study revision in the existing
:class:`~src.kb.methodology_provenance.MethodologyStore` (external id
``<registry>:<identifier>``) with phase, allocation, masking, planned and
actual sample size, pre-registration status and the primary-outcome
definitions of that version. A linked publication's stated primary outcome is
recorded as an extracted methodology statement with an exact passage locator.
Outcome switching is computed by the methodology store and returned as
findings that cite both versions; nothing here judges a trial.
"""

from __future__ import annotations

import re

from src.kb.clinical_records import ClinicalRecordError, ClinicalRecordStore, _require_read
from src.kb.methodology_provenance import EXTRACT_SCOPE, MethodologyStore

_PRIMARY = re.compile(r"\bprimary\s+(?:outcome|end\s*-?\s*point|efficacy\s+end\s*-?\s*point)s?\b", re.IGNORECASE)


def external_id(trial):
    return f"{trial['registry']}:{trial['identifier']}"


def _registry_order(history):
    """Registry-history revisions in version order when available, else store order."""
    revisions = history["revisions"]
    versioned = [r for r in revisions if r["version_key"].startswith("registry-history:")]
    if versioned:
        return sorted(versioned, key=lambda r: int(r["version_key"].split(":", 1)[1]))
    return revisions


def _outcomes_at(store, namespace, trial, version_key, *, scopes):
    """Outcome definitions as registered in one version (present outcomes only)."""
    primary, secondary = [], []
    for row in store.find(namespace, scopes=scopes, kinds={"outcome-measure"}, provider=trial["registry"],
                          native_id=trial["identifier"]):
        revisions = store.history(namespace, row["record_id"], scopes=scopes)["revisions"]
        match = [r for r in revisions if r["version_key"] == version_key] or (
            [revisions[-1]] if not version_key.startswith("registry-history:") else [])
        if not match or not match[-1]["record"]["present"]:
            continue
        outcome = match[-1]["record"]
        item = {"measure": outcome["measure"], "time_frame": outcome.get("time_frame")}
        (primary if outcome["role"] == "primary" else secondary).append(item)
    return sorted(primary, key=lambda o: o["measure"]), sorted(secondary, key=lambda o: o["measure"])


def design_fields(trial, primary, secondary, planned_from=None):
    design = trial.get("design") or {}
    registration = trial.get("registration") or {}
    phases = (trial.get("phase") or {}).get("normalized") or ["unknown"]
    return {
        "phase": None if phases == ["unknown"] else phases,
        "allocation": design.get("allocation", "unknown"),
        "masking": design.get("masking", "unknown"),
        "sample_size_planned": design.get("enrollment_planned") if design.get("enrollment_planned") is not None
        else planned_from,
        "sample_size_actual": design.get("enrollment_actual"),
        "preregistration": {"prospective": registration.get("prospective"),
                            "first_submitted": registration.get("first_submitted"),
                            "start_date": registration.get("start_date")},
        "primary_outcomes": primary or None,
        "secondary_outcomes": secondary or None,
    }


class TrialMethodology:
    def __init__(self, conn, *, now=None):
        self.conn = conn
        self.records = ClinicalRecordStore(conn, now=now)
        self.methods = MethodologyStore(conn, now=now)

    def sync(self, namespace, trial_record_id, *, principal_id, scopes):
        """Register each registry version of a trial as a methodology study revision."""
        _require_read(namespace, scopes)
        history = self.records.history(namespace, trial_record_id, scopes=scopes)
        ordered = _registry_order(history)
        if not ordered or ordered[0]["record"]["record_kind"] != "registered-trial":
            raise ClinicalRecordError("not_a_trial", "record is not a registered trial")
        trial = ordered[-1]["record"]
        registered, planned = [], None
        for generation, revision in enumerate(ordered):
            record = revision["record"]
            primary, secondary = _outcomes_at(self.records, namespace, record, revision["version_key"],
                                              scopes=scopes)
            if (record.get("design") or {}).get("enrollment_planned") is not None:
                planned = record["design"]["enrollment_planned"]
            fields = design_fields(record, primary, secondary, planned_from=planned)
            result = self.methods.register_trial_revision(
                namespace, external_id(trial), revision["version_key"], record.get("title") or record["identifier"],
                fields, principal_id=principal_id, scopes=scopes, generation=generation,
                observed_at_ms=revision["observed_at_ms"],
                population={"conditions": [c["term"] for c in record.get("conditions") or []]},
                interventions=[{"name": i["name"], "type": i.get("type")} for i in record.get("interventions") or []],
                outcomes=[{"measure": o["measure"], "time_frame": o["time_frame"], "role": "primary"} for o in primary],
                provenance={"clinical_record_id": trial_record_id, "clinical_revision": revision["revision"],
                            "version_key": revision["version_key"], "source_url": record["source_url"]})
            registered.append({"study_revision_id": result["study_revision_id"], "version": result["version"],
                               "idempotent": result["idempotent"], "trial": result["design"]["trial"]})
        return {"study_id": "study:" + _study_digest(namespace, external_id(trial)), "revisions": registered}

    def record_publication_outcomes(self, namespace, study_id, documents, *, principal_id, scopes):
        """Extract 'primary outcome was ...' sentences from linked publications with passage locators."""
        if EXTRACT_SCOPE not in scopes and "operator" not in scopes:
            raise ClinicalRecordError("unauthorized", "methodology extraction scope is required")
        receipts = []
        for document in documents:
            statements = []
            for index, sentence in enumerate(re.split(r"(?<=[.!?])\s+", str(document.get("content") or ""))):
                if _PRIMARY.search(sentence):
                    statements.append({"kind": "primary-outcome", "text": sentence.strip(), "confidence": 0.8,
                                       "uncertainty": "rule-based sentence match; wording as published",
                                       "locator": {"document_id": document["document_id"], "passage": index,
                                                   "section": "abstract", "revision_id": document.get("revision_id")}})
            if statements:
                receipts.append(self.methods.extract(namespace, study_id, document["document_id"], statements,
                                                     principal_id=principal_id, scopes=scopes,
                                                     provenance={"extractor": "clinical-primary-outcome-sentence:1.0.0"}))
        return receipts

    def outcome_switching(self, namespace, trial_record_id, *, principal_id, scopes, documents=()):
        synced = self.sync(namespace, trial_record_id, principal_id=principal_id, scopes=scopes)
        if documents:
            self.record_publication_outcomes(namespace, synced["study_id"], documents, principal_id=principal_id,
                                             scopes=scopes)
        report = self.methods.outcome_switching(namespace, synced["study_id"], scopes=scopes)
        return {**report, "trial_record_id": trial_record_id, "study_revisions": synced["revisions"]}


def _study_digest(namespace, external):
    from src.kb.methodology_provenance import _digest

    return _digest([namespace, external])[:24]
