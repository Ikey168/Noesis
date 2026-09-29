"""Evaluate a supplier profile against a notice revision, lot by lot, with cited passages.

This reuses the Funding & Grants eligibility engine
(:func:`src.kb.funding_eligibility.assess` with its three-valued rule
grammar) unchanged: exclusion grounds, selection criteria (economic and
financial standing, technical and professional ability, suitability),
certifications, set-asides and thresholds extracted from the notice are
requirements with machine rules; a rule over a fact the supplier has not
stated evaluates to *unknown*, and a criterion without an extracted rule is
*unparsed*. Neither is ever a pass. Money thresholds compare only in the
same currency (no conversion). Each conclusion cites the notice revision, the
notice that caused it, the lot, the source passage and the profile facts it
used.

Assessments pin both the notice revision and the profile revision; when either
moves on, :meth:`ProcurementEligibilityService.stale_assessments` lists them and
:meth:`ProcurementEligibilityService.reassess` recomputes against the current
revisions. This is a requirements assessment, not the contracting authority's
decision and not a prediction of winning.
"""

from __future__ import annotations

import json
import re
import time

from src.kb.funding_eligibility import assess as funding_assess
from src.kb.funding_records import canonical, digest
from src.kb.procurement_notices import ProcurementNoticeStore, lot_ids, lot_view
from src.kb.procurement_profiles import ProcurementProfileError, ProcurementProfileStore, money_facts

CONTRACT = "noesis-procurement-eligibility-v1"
DISCLAIMER = ("Requirements assessment of stated supplier facts against cited notice text; not the contracting "
              "authority's decision, not legal advice and not a prediction of winning.")
VERDICTS = ("eligible", "ineligible", "needs_clarification")
_DDL = """
CREATE TABLE IF NOT EXISTS procurement_assessments(
 assessment_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, owner TEXT NOT NULL, profile_id TEXT NOT NULL,
 profile_revision BIGINT NOT NULL, procedure_key TEXT NOT NULL, notice_revision BIGINT NOT NULL,
 content_json TEXT NOT NULL, created_at_ms BIGINT NOT NULL);
"""


class ProcurementEligibilityError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def canonical_certification(name):
    """Normalise certificate names so 'ISO/IEC 27001:2022' and 'iso/iec 27001' compare equal."""
    text = " ".join(str(name).split())
    match = re.search(r"\bISO(?:\s?/\s?IEC)?\s?(\d{4,5}(?:-\d)?)", text, re.IGNORECASE)
    if match:
        return ("ISO/IEC " if re.search(r"IEC", match.group(0), re.IGNORECASE) else "ISO ") + match.group(1)
    return text


def rule_facts(facts):
    """Profile facts as the rule engine sees them: per-currency money, canonical certificate names."""
    result = money_facts(facts)
    if isinstance(facts.get("supplier.certifications"), list):
        result["supplier.certifications"] = sorted({canonical_certification(c) for c in facts["supplier.certifications"]})
    return result


def assess_lot(procedure, lot_id, facts):
    """Pure assessment of one lot of one procedure revision (``lot_id=None``: a notice without lots)."""
    record = procedure["record"]
    view = lot_view(record, lot_id)
    opportunity = {"opportunity_id": procedure["procedure_key"], "revision": procedure["revision"],
                   "record": {"requirements": view["requirements"], "source_url": record["source_url"]}}
    result = funding_assess(opportunity, rule_facts(facts))
    cause = procedure["cause"]
    by_id = {r["requirement_id"]: r for r in view["requirements"]}
    for finding in result["findings"]:
        if finding.get("citation"):
            requirement = by_id.get(finding["requirement_id"]) or {}
            finding["citation"] = {**finding["citation"], "procedure_key": procedure["procedure_key"],
                                   "notice_revision": procedure["revision"], "notice_id": cause["notice_id"],
                                   "notice_stage": cause["stage"], "lot_id": lot_id,
                                   "language": requirement.get("language"), "quote": (requirement.get("locator") or {}).get("quote")
                                   or requirement.get("text", "")[:400]}
            finding["citation"].pop("opportunity_id", None)
            finding["citation"].pop("revision", None)
        if finding.get("reason") == "the call revision states no applicant/project requirements":
            finding["reason"] = "the notice revision states no exclusion grounds or selection criteria for this lot"
        finding["profile_facts"] = {key: facts.get(key.rsplit(".", 1)[0] if key.rsplit(".", 1)[-1].isupper() else key)
                                    for key in finding.get("facts_used") or []}
    result["contract"], result["disclaimer"] = CONTRACT, DISCLAIMER
    result["lot_id"] = lot_id
    return result


class ProcurementEligibilityService:
    """Pins supplier-profile and notice revisions and records replayable, per-lot assessments."""

    def __init__(self, conn, *, initialize=True, now=None):
        self.conn, self.now = conn, now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)
        self.profiles = ProcurementProfileStore(conn, initialize=initialize, now=self.now)
        self.notices = ProcurementNoticeStore(conn, initialize=initialize, now=self.now)

    def assess(self, namespace, profile_id, key, *, principal_id, scopes, profile_revision=None, notice_revision=None,
               as_of=None):
        profile = self.profiles.inspect(namespace, profile_id, principal_id=principal_id, scopes=scopes)
        if profile.get("profile_kind") != "supplier":
            raise ProcurementEligibilityError("not_a_supplier_profile", "eligibility is assessed for supplier profiles")
        pinned = self.profiles.pinned_facts(namespace, profile_id, profile_revision or profile["revision"],
                                            principal_id=principal_id, scopes=scopes, as_of=as_of)
        procedure = self.notices.get(namespace, key, scopes=scopes, revision=notice_revision)
        lots = {lot_id or "_": assess_lot(procedure, lot_id, pinned["facts"]) for lot_id in lot_ids(procedure["record"])}
        verdicts = {lot: result["verdict"] for lot, result in lots.items()}
        content = {
            "contract": CONTRACT, "disclaimer": DISCLAIMER, "namespace": namespace,
            "profile": {"profile_id": profile_id, "revision": pinned["profile_revision"], "unreviewed_facts": pinned["unreviewed"]},
            "procedure": {"procedure_key": key, "revision": procedure["revision"], "provider": procedure["provider"],
                          "procedure_id": procedure["procedure_id"], "notice": procedure["cause"], "title": procedure["record"]["title"]},
            "lots": lots, "verdicts": verdicts,
            "summary": {verdict: sorted(lot for lot, v in verdicts.items() if v == verdict) for verdict in VERDICTS},
            "missing_facts": sorted({m for r in lots.values() for m in r["missing_facts"]}),
            "as_of": as_of,
        }
        assessment_id = "procurement-assessment:" + digest(
            [namespace, principal_id, profile_id, pinned["profile_revision"], key, procedure["revision"], as_of])[:32]
        content["assessment_id"] = assessment_id
        self.conn.execute("INSERT INTO procurement_assessments VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                          [assessment_id, namespace, principal_id, profile_id, pinned["profile_revision"], key,
                           procedure["revision"], canonical(content), self.now()])
        self.notices.register_view(namespace, assessment_id, principal_id, {key: procedure["revision"]})
        return content

    def stale_assessments(self, namespace, *, principal_id, scopes):
        """The owner's latest assessment per (profile, procedure) whose pinned revisions are no longer current."""
        rows = self.conn.execute(
            """SELECT assessment_id, profile_id, profile_revision, procedure_key, notice_revision FROM procurement_assessments a
               WHERE namespace=? AND owner=? AND created_at_ms = (SELECT max(created_at_ms) FROM procurement_assessments b
               WHERE b.namespace=a.namespace AND b.owner=a.owner AND b.profile_id=a.profile_id AND b.procedure_key=a.procedure_key)
               ORDER BY assessment_id""", [namespace, principal_id]).fetchall()
        stale = []
        for assessment_id, profile_id, profile_revision, key, notice_revision in rows:
            try:
                profile = self.profiles.inspect(namespace, profile_id, principal_id=principal_id, scopes=scopes)
            except ProcurementProfileError:
                continue
            except ValueError:
                continue
            current_notice = self.conn.execute("SELECT revision FROM procurement_procedures WHERE procedure_key=?", [key]).fetchone()
            reasons = []
            if profile["revision"] != profile_revision:
                reasons.append(f"profile revision {profile_revision} -> {profile['revision']}")
            if current_notice and current_notice[0] != notice_revision:
                reasons.append(f"notice revision {notice_revision} -> {current_notice[0]}")
            if reasons:
                stale.append({"assessment_id": assessment_id, "profile_id": profile_id, "procedure_key": key, "reasons": reasons})
        return stale

    def reassess(self, namespace, *, principal_id, scopes):
        """Recompute every stale assessment against the current profile and notice revisions."""
        return [{**item, "reassessed": self.assess(namespace, item["profile_id"], item["procedure_key"],
                                                   principal_id=principal_id, scopes=scopes)["assessment_id"]}
                for item in self.stale_assessments(namespace, principal_id=principal_id, scopes=scopes)]

    def inspect(self, namespace, assessment_id, *, principal_id, scopes):
        row = self.conn.execute("SELECT owner, profile_id, content_json FROM procurement_assessments WHERE assessment_id=? AND namespace=?",
                                [assessment_id, namespace]).fetchone()
        if not row or row[0] != principal_id:
            raise ProcurementEligibilityError("assessment_not_found", "assessment is unavailable")
        self.profiles.inspect(namespace, row[1], principal_id=principal_id, scopes=scopes)
        return {**json.loads(row[2]), "freshness": self.notices.view_status(assessment_id)}
