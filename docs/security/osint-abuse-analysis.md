# Abuse analysis: geolocate_claims and narrative_coordination

This is the written misuse analysis the OSINT review gate
(`docs/security/osint-review-gate.md`, criterion 4) requires before either gated tool is
served. The tools are implemented (`src/osint/gated.py`) and stay behind the
`NOESIS_OSINT_GATED_TOOLS` flag, off by default; turning the flag on is the
human sign-off that this analysis has been reviewed and accepted.

## geolocate_claims

**What it does.** Extracts *event geography* from claim text: the places a
claim reports an event as having happened, matched against a small static
gazetteer, cited to the source document and flagged unverified.

**Who could weaponize it, and how it is mitigated:**

| Misuse | Mitigation (in code) |
|---|---|
| Locating a person ("where is X") | Refused: if the entity resolves to a person (by `person:` id or person-only roles), the tool returns `person_geolocation_refused` and emits nothing. It only ever ties a location to a claim/event, never to an individual. |
| Passing off guesses as fact | Every location carries `verified: false` and a caveat; the method (gazetteer match on text) is stated, so a consumer cannot mistake it for resolved coordinates. |
| Inferring home/movement patterns | The tool reads only public document content about events; it has no access to person attributes, location history, or device data, and does not join across documents to build a person timeline. |

**Residual risk.** Place mentions in a claim about an event can incidentally
co-occur with a named person. The refusal is on the *query* (person entity) and
the *output shape* (event-tagged, never person-tagged); a determined analyst
could still read a person's name and a place from the same claim, but that is
already visible in the source document the tool cites. The tool adds no new
inference beyond "this claim mentions this place".

## narrative_coordination

**What it does.** Flags cohorts of sources that publish near-identical claims on
a topic (claim-text Jaccard echo graph, connected components), for human review.

**Who could weaponize it, and how it is mitigated:**

| Misuse | Mitigation (in code) |
|---|---|
| Accusing sources of collusion | Never accuses: every cohort is `status: "warrants review"` with a `note` that it is not an accusation, and a `caveat` that similarity is often coincidental (shared wire copy, a common event). |
| Treating similarity as proof | The output states the method and a null-model caveat explicitly; there is no "coordinated: true" field, only a `coordination_score` framed for triage. |
| Smearing a coincidental cohort | The tool surfaces the *evidence* (the echoing claim pairs and a sample) so a human can dismiss a shared-wire false positive rather than acting on a bare label. |

**Residual risk.** A high-similarity cohort of independent outlets running the
same agency wire will be flagged. This is by design a *review* signal, not a
verdict; the caveat and the surfaced evidence exist precisely so a reviewer
rejects such cases. Calibration against a labeled fixture (gate criterion 2)
should set `min_similarity` before the flag is turned on in any real deployment.

## Imagery: reverse_image_search and geolocate_image (Track C / C4)

This is the imagery threat model that the OSINT imagery threat model
§2 (guardrail 4) requires *before* the C4 gated external tier ships. The
corpus-internal imagery capabilities already merged — EXIF extraction (C1),
perceptual-hash reuse detection (C2), and C2PA verification (C3) — are **not**
gated: they read only the operator's own ingested assets, add no external calls,
and identify *images*, never people. Only the C4 external tier is gated, for the
same reason `geolocate_claims` is: pointing a capability at the outside world is
where the abuse surface is.

**Permanent non-goal (not a setting).** No face recognition and no person
identification of any kind — not via an adapter, not behind a flag. The
perceptual-hash pipeline matches *images*; the geolocation assist reasons about
*places*. Changing this line requires revisiting this analysis, not a PR.

### reverse_image_search

**What it does (when built).** Submits an asset to an external reverse-image
provider and returns where else the image appears on the open web, as
*suggestions* for an operator to confirm.

| Misuse | Mitigation (in design) |
|---|---|
| Treating a match as fact | Results enter the review queue as `cited: false` (the flagged state the evidence discipline already renders) and become citable only on operator confirmation. Nothing model- or provider-suggested flows into a dossier, timeline, or the ledger unconfirmed. |
| De-anonymizing a person via their photo | The tool submits *corpus images* (figures, article photos) for provenance, never operator-supplied photos of individuals; combined with the permanent no-person-identification non-goal, there is no "who is this person" path. |
| Unbounded external calls / cost / leakage | Key-gated, rate-limited, and allowlisted per the agent-host budget model; **no default provider ships**, so the tier is inert until an operator supplies one. Off by default. |

### geolocate_image

**What it does (when built).** A VLM proposes visible-landmark hypotheses for
where a *scene* was photographed — a suggestion for a human to verify.

| Misuse | Mitigation (in design) |
|---|---|
| Passing a guess off as a location | Output is suggestion-grade, plainly labeled, and never auto-cited; operator confirmation through the review gate is what makes it citable. |
| Locating a person | Reasons about the *place in the scene*, never the subject; the person non-goal applies. No EXIF-GPS is treated as fact (it is file-claimed, per C1). |
| Inferring movement patterns | Operates per-image; it does not join across a person's photos to build a track. |

**Residual risk.** A confirmed reverse-search hit or landmark guess is only as
good as the operator who confirms it; the gate makes confirmation an explicit,
audited step rather than an automatic inference. The budget/allowlist posture
bounds cost and egress but cannot prevent an operator from misusing a *confirmed*
result — the same residual that applies to every review-gated OSINT tool.

## Gate status

Criteria 1 (purpose limitation in code), 3 (evidence discipline: cited,
flagged), and 4 (this abuse analysis) are met. Criteria 2 (calibration on a
labeled fixture) and 5 (human-in-the-loop opt-in) are satisfied operationally by
keeping the tools behind the off-by-default flag: a deployment enables them only
after calibrating the thresholds and accepting this analysis. The absence test
(`tests/unit/osint/test_investigations.py`) proves they stay off until then.

The **imagery external tier** (C4: `reverse_image_search`, `geolocate_image`)
inherits the same five criteria and the same off-by-default posture. Its
purpose-limitation (criterion 1) is the permanent no-person-identification
non-goal plus the corpus-images-only submission rule; its evidence discipline
(criterion 3) is the review queue, where a suggestion is `cited: false` until an
operator confirms it. C4 must not ship until this section is reviewed and
attached to its enabling PR, and no default reverse-search provider may be
bundled — the tier stays inert until an operator supplies a key and provider.
