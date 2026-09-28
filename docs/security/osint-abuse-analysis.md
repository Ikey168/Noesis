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
where the abuse surface is. Video keyframes sampled by the media connector
(OX04) are indexed into the same corpus-internal pipeline, so reuse detection
covers video; they are matched as images, never used for face recognition or
person identification.

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

## chronolocate_image (OX09)

**What it does.** Proposes *when* a corpus image was captured: a time-of-day
band from shadow direction and length and a season window, computed locally
from solar geometry for a place that is either a *confirmed* geolocation
suggestion or an explicit operator hypothesis. The result is a queued
suggestion, `cited: false` and `verified: false`.

| Misuse | Mitigation (in code) |
|---|---|
| Inferring a person's routine (when someone is somewhere) | Per image and place-conditioned: the tool takes one corpus asset hash, has no person or entity parameter, and never joins across images, so it cannot build a timeline of anyone. It reasons about the sun and the scene, never the subject. |
| Passing an estimate off as a fact | The output states the method, the place hypothesis it depends on, the tolerance and an interval, with `verified: false`; it is queued uncited until an operator confirms it. |
| Laundering EXIF timestamps | EXIF `DateTimeOriginal` is shown as file-claimed and never used as ground truth. |
| Inferring the place too | The place is never inferred here: it comes from a confirmed suggestion or a named operator's hypothesis. |

**Residual risk.** An operator could run the tool on many images of the same
person one by one. The gate, the per-image shape and the audit log (every
invocation is recorded in the provisioning trail) make that visible; the
permanent non-goal of no movement-pattern inference stands.

## reference_imagery (OX10)

**What it does.** Fetches one satellite tile or street-level view for the
place of a queued geolocation suggestion, so a reviewer can compare it with
the corpus image before confirming. References are stored in the review-queue
store, linked to the suggestion, with provider attribution; they never become
citations.

| Misuse | Mitigation (in code) |
|---|---|
| Monitoring a private location over time | One reference per suggestion and kind; no date parameter and no time-series fetches; no free-form "look at coordinates" tool on the served surface. |
| Using the tool as a general imagery viewer | A fetch requires a queued suggestion (or an explicit operator hypothesis attached to one); the provider is allowlisted and budgeted (request cap, byte cap). |
| Unbounded cost or egress | Key-gated, allowlisted, budgeted; **no default provider ships**, so the tool is inert until an operator supplies one. |

**Residual risk.** Reference imagery of a residential street is still imagery
of a residential street; the constraint is that it is only fetched for a place
already under review for a corpus image, once, and the fetch is audited.

## Organization-keyed infrastructure pivoting (OX08)

`infrastructure_pivot` is served **ungated**. It walks cited source-identity
relationships (`ownership` from RDAP registrant organizations, probable
`shared-infrastructure` from certificate SAN sets) between *publications and
organizations* already known as sources in the namespace. These are registry
facts about publishers: they add no person data (RDAP natural-person
registrants and e-mail SANs are dropped before storage) and no external calls
at query time.

E-mail, username/handle, IP-address and `person:` identifiers are **refused in
code** (`status: person_identifier_refused`). Those pivots (email → username →
IP and similar) are the classic de-anonymisation chain and are excluded by the
OSINT pack's non-goals; IP-keyed lookups would also enumerate unrelated
parties sharing a host. The output carries the caveat that shared hosting and
CDNs commonly explain shared infrastructure, and there is no same-operator or
attribution field.

## Gate status

Criteria 1 (purpose limitation in code), 3 (evidence discipline: cited,
flagged), and 4 (this abuse analysis) are met. Criteria 2 (calibration on a
labeled fixture) and 5 (human-in-the-loop opt-in) are satisfied operationally by
keeping the tools behind the off-by-default flag: a deployment enables them only
after calibrating the thresholds and accepting this analysis. The absence test
(`tests/unit/osint/test_investigations.py`) proves they stay off until then.

The **imagery external tier** (C4: `reverse_image_search`, `geolocate_image`,
and the OX09/OX10 extensions `chronolocate_image`, `reference_imagery`)
inherits the same five criteria and the same off-by-default posture. Its
purpose-limitation (criterion 1) is the permanent no-person-identification
non-goal plus the corpus-images-only submission rule; its evidence discipline
(criterion 3) is the review queue, where a suggestion is `cited: false` until an
operator confirms it. C4 must not ship until this section is reviewed and
attached to its enabling PR, and no default reverse-search provider may be
bundled — the tier stays inert until an operator supplies a key and provider.
