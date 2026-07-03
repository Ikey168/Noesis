# Abuse analysis: geolocate_claims and narrative_coordination

This is the written misuse analysis the OSINT review gate
(`docs/osint-review-gate.md`, criterion 4) requires before either gated tool is
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

## Gate status

Criteria 1 (purpose limitation in code), 3 (evidence discipline: cited,
flagged), and 4 (this abuse analysis) are met. Criteria 2 (calibration on a
labeled fixture) and 5 (human-in-the-loop opt-in) are satisfied operationally by
keeping the tools behind the off-by-default flag: a deployment enables them only
after calibrating the thresholds and accepting this analysis. The absence test
(`tests/unit/osint/test_investigations.py`) proves they stay off until then.
