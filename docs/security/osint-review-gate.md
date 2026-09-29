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
reviewing the abuse analysis in `docs/security/osint-abuse-analysis.md`. The flag *is*
the enforcement of criterion 5.

## Gated tools

| Tool | Why gated | Constraint if it ever ships |
|---|---|---|
| `geolocate_claims` | Location inference is the most abusable OSINT primitive. | Strictly event-geography derived from document content (where an event is reported to have happened), never person location. |
| `narrative_coordination` | Coordinated-behavior detection is highly false-positive-prone and can smear coincidental cohorts. | Findings flag a cohort for human review, never accuse; every edge cited; a calibrated null model, not a threshold on raw co-occurrence. |
| `reverse_image_search` (Track C / C4) | Any capability pointed at the open web is where the imagery abuse surface is. | Submits *corpus images* only; results enter the review queue as `cited: false` until an operator confirms; key-gated, rate-limited, allowlisted, **no default provider**. No person identification, ever. |
| `geolocate_image` (Track C / C4) | Scene geolocation can be misread as person location. | Reasons about the *place in the scene*, never the subject; suggestion-grade, never auto-cited; EXIF-GPS stays file-claimed, not fact. |
| `chronolocate_image` (OX09, #2049) | Capture-time estimates across someone's photos could reconstruct a routine. | Per image, place-conditioned (a *confirmed* geolocation suggestion or an explicit operator hypothesis), never joins across images; a time band and season window with `verified: false`, queued `cited: false`; EXIF `DateTimeOriginal` shown as file-claimed only; injected shadow estimator, **no default backend**. |
| `reference_imagery` (OX10, #2050) | Fetching imagery of a place on demand could become monitoring of a private location. | Only for the place of a queued suggestion (or an explicit operator hypothesis attached to one); no free-form coordinate tool on the served surface; one reference per suggestion and kind (no time series); allowlisted, budgeted, key-gated provider with **no default**; references live in the review-queue store and never become citations. |

`src/osint/investigations.py` names every one of them in `GATED_TOOLS`;
`is_gated(tool)` returns True for each. They are registered on `tools/osint_mcp/server.py` only
behind the `NOESIS_OSINT_GATED_TOOLS` flag (off by default). The imagery
external tier (C4) is analysed in
[`osint-abuse-analysis.md`](osint-abuse-analysis.md) ("Imagery") and inherits
the same five criteria; the corpus-internal imagery tools (`image_provenance`,
`image_reuse_findings`) are **not** gated — they read only the operator's own
assets and identify images, never people.

## Gate criteria (must all pass before either tool is served)

1. **Purpose limitation, in code.** `geolocate_claims` resolves only
   event-geography from document text; a test proves it never emits a person
   location. `narrative_coordination` outputs cohorts marked "for review" with
   no accusatory language and a citation on every edge.
2. **Calibration. Met (M7.3).** Both thresholds are calibrated on a labeled
   fixture with a documented false-positive rate, not an unvalidated threshold.
   `src/osint/gated_calibration.py` sweeps `narrative_coordination`'s
   `min_similarity` over coordinated vs coincidental cohorts and reports the
   FPR/TPR per threshold; on the fixture in
   `tests/unit/osint/test_gated_calibration.py` the loose thresholds (0.3-0.5)
   flag the coincidental cohort at an FPR of 0.5, while the served default of
   0.6 reaches FPR 0.0 with a true-positive rate of 1.0, so 0.6 is the
   recommended (smallest within-target) threshold. `geolocate_claims` is
   measured to refuse every person location on a labeled set of person entities:
   its person-location false-positive rate is 0.0. The calibration reruns as a
   test, so the documented rates stay honest.
3. **Evidence discipline.** Every output line carries a citation
   (`src/osint/evidence.py`); uncited findings are flagged, never hidden.
4. **Abuse review.** A written misuse analysis (who could weaponize this, and
   the mitigations) is reviewed and attached to the enabling PR.
5. **Human-in-the-loop.** Both are opt-in behind an explicit flag, off by
   default, and log every invocation to the provisioning audit trail.

### How the imagery extensions meet the five criteria

| Criterion | `chronolocate_image` (OX09) | `reference_imagery` (OX10) |
| --- | --- | --- |
| 1. Purpose limitation in code | Corpus asset only (`image_assets` lookup); place from a confirmed suggestion or a named operator's hypothesis, never inferred; one image per call, no person/entity parameter. | Requires a queued suggestion id; no coordinate-only entry point on the server; one reference per suggestion and kind. |
| 2. Calibration | `tests/unit/osint/test_imagery_gated.py::test_chronolocation_calibration_interval_hit_rate` reports the interval hit-rate on `tests/fixtures/osint/chronolocation-calibration.json` (8 cases with known capture times; the current model covers all 8, threshold 0.875). The solar model is also checked against textbook noon elevations. The fixture's shadow measurements are synthetic (see the fixture note). | Not a classifier; bounded by the budget (allowlist, request cap, byte cap), tested with a fake provider. |
| 3. Evidence discipline | Queued `cited: false`, `verified: false`, method, place hypothesis and interval stated. | References are review aids (`cited: false` always); confirmation records which ones the operator viewed. |
| 4. Abuse review | `osint-abuse-analysis.md`, "chronolocate_image". | `osint-abuse-analysis.md`, "reference_imagery". |
| 5. Human in the loop | Behind `NOESIS_OSINT_GATED_TOOLS`; invocations logged to the provisioning audit trail in the queue store. | Same flag and audit logging. |

## Deploying a reverse-image provider (OX11)

Reverse image search stays a deployment decision: no provider ships with
Noesis, and the served `reverse_image_search` passes `provider=None`, so the
tool answers `no_provider_configured` until an operator wires one.

1. **Flag.** The tool is only registered when `NOESIS_OSINT_GATED_TOOLS=on`
   (the human sign-off for the whole gated tier, after reading
   `osint-abuse-analysis.md`).
2. **Adapter.** Write a `ReverseSearchProvider` (`src/osint/imagery_gated.py`):
   a callable taking the corpus image **bytes only** and returning
   `[{"url", "provider", "seen_at", "title"?}]`. It must declare `max_bytes`
   and `timeout_s` (at most 60 s), refuse larger images without calling out,
   raise on failure instead of retrying, and never touch the corpus
   connection. It must not accept a URL, a query, a face or a person, and must
   not return names, faces, profiles, accounts or any other identity field.
3. **Conformance.** Run the harness before wiring anything:
   `from tests.unit.osint.provider_conformance import assert_conformant`, then
   `assert_conformant(factory)` where `factory(transport, corpus_conn)` builds
   your adapter around an injected transport. A conformant fake and a
   deliberately non-conformant adapter exercise the harness in
   `tests/unit/osint/test_imagery_gated.py`.
4. **Injection point.** In `tools/osint_mcp/server.py`, the gated
   `reverse_image_search` tool calls
   `reverse_image_search(con, sha256, provider=None, queue_conn=queue)`.
   Replace `None` with your adapter, constructed in the deployment (not in
   the repository).
5. **Keys and budget.** Load the provider key from the deployment's secret
   store (a `NOESIS_*` environment variable), never from the repository.
   Apply the agent-host budget model: rate limit, byte cap and an allowlist of
   the provider's host(s); the agent runtime already counts OSINT tool calls
   against its step budget (`src/agent/runtime.py`).
6. **Operator confirmation.** Hits enter the imagery review queue as
   `cited: false`. Only `confirm_suggestion(queue, suggestion_id, operator)`
   makes one citable; an operator identity is required.

**Permanent non-goal.** Adapters that return person identities or accept face
queries fail conformance and must not be wired, whatever the provider offers.
No face recognition, no person identification (see "Imagery" in
`osint-abuse-analysis.md`).

## Passive DNS

Passive DNS history has a recorded access decision in
[`osint-passive-dns-access.md`](osint-passive-dns-access.md) (OX07): no
provider is adopted, so no adapter ships. Organization-keyed infrastructure
facts come from RDAP and certificate transparency instead.

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
