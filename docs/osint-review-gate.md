# OSINT review gate (R11)

Track OSINT is defensive and analytical: it reads already-ingested public
documents and never crawls, targets, or de-anonymizes. Two tools named in the
plan are the most abusable and the most false-positive-prone, so they stay
behind an explicit review gate. They are **absent from the served tool
surface** by default, and a test
(`tests/unit/osint/test_investigations.py::test_gated_tools_are_absent`)
asserts they are not exposed unless the gate is deliberately opened.

**Status (issue #639 item 3): implemented, gated off.** Both tools now exist,
purpose-limited in code, in `src/osint/gated.py`, and are registered on the
OSINT server only when `NOESIS_OSINT_GATED_TOOLS` is turned on. The flag off
(the default) keeps them absent; turning it on is the human sign-off after
reviewing the abuse analysis in `docs/osint-abuse-analysis.md`. The flag *is*
the enforcement of criterion 5.

## Gated tools

| Tool | Why gated | Constraint if it ever ships |
|---|---|---|
| `geolocate_claims` | Location inference is the most abusable OSINT primitive. | Strictly event-geography derived from document content (where an event is reported to have happened), never person location. |
| `narrative_coordination` | Coordinated-behavior detection is highly false-positive-prone and can smear coincidental cohorts. | Findings flag a cohort for human review, never accuse; every edge cited; a calibrated null model, not a threshold on raw co-occurrence. |

`src/osint/investigations.py` names them in `GATED_TOOLS`; `is_gated(tool)`
returns True for both. They are registered on `tools/osint_mcp/server.py` only
behind the `NOESIS_OSINT_GATED_TOOLS` flag (off by default).

## Gate criteria (must all pass before either tool is served)

1. **Purpose limitation, in code.** `geolocate_claims` resolves only
   event-geography from document text; a test proves it never emits a person
   location. `narrative_coordination` outputs cohorts marked "for review" with
   no accusatory language and a citation on every edge.
2. **Calibration.** Both reuse the Track DS calibration/conformal work: a
   documented false-positive rate on a labeled fixture, not an unvalidated
   threshold.
3. **Evidence discipline.** Every output line carries a citation
   (`src/osint/evidence.py`); uncited findings are flagged, never hidden.
4. **Abuse review.** A written misuse analysis (who could weaponize this, and
   the mitigations) is reviewed and attached to the enabling PR.
5. **Human-in-the-loop.** Both are opt-in behind an explicit flag, off by
   default, and log every invocation to the provisioning audit trail.

Until all five hold, the tools do not ship. This document is the gate; the
absence test is its enforcement.

## Why "investigation" is a provisioned KG

An investigation is not a new abstraction. It is a Track P-provisioned,
namespaced knowledge graph (R8) fed by a chosen source set. Every provisioning
action (deploy, attach, ingest, teardown) is already written to the
provisioning lineage log, so an investigation is fully reconstructable from its
audit trail via `investigation_audit(name)`
(`src/osint/investigations.py`). That gives investigations their accountability
for free: nothing happens to an investigation that is not logged and replayable.
