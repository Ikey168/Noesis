# Clinical Evidence surveillance guide

The Clinical Evidence pack's optional `surveillance` feature (default off,
#1917) takes a condition and a geography to a cited surveillance dossier.
The dossier holds notifiable-disease and health-indicator series with their
case definitions and definition revisions, reporting date and reference date
kept apart, reporting-delay notes and published vintages. Conditions align to
MeSH and ICD through the clinical term crosswalks. Regional series are
projected onto the Geospatial boundaries the platform already holds. Trials
and publications are linked only where they cite a series' dataset.

Observation, estimate and model output stay separate, and every value cites
the source revision it came from. The feature never predicts an outbreak,
nowcasts, estimates completeness, suggests a threshold or gives health advice.
A case-definition change is shown as a break in the series and is never
applied to earlier values.

Source coverage and access decisions are in the
[source audit](../roadmaps/clinical-surveillance-source-audit.md). No provider
is live until a dated run verifies it.

## Enable it

Select the feature in the Clinical Evidence bundle composition
(`features: ["surveillance"]`). Enabling it is a coordinator selection change
with an activation receipt; there is no separate flag or pack. It binds the
`clinical.surveillance` provider and the shared `geospatial.core` (feature
query and place resolution) and `science.literature` (claims) providers. With
the feature off, the Clinical Evidence and Science bindings are unchanged.
The `clinical.surveillance-dossier` profile carries the vocabulary (reporting
date, reference date, case definition, kind, vintage, reporting delay) and the
never-sentence. `surveillance_readiness` reports whether the feature is
selected, and gives each provider's access decision, release count and
evidence origin.

## Acquire

Run the `clinical-evidence` (0.1.1) surveillance sources through the
source-pack tools:

| Source | Connector format | Records |
| --- | --- | --- |
| `rki-tuberkulose-meldedaten` | `rki-github-csv` (a pinned release tag) | cases by district and age group with Meldedatum and Refdatum, Falldefinition editions, the RKI delay note |
| `who-gho-tuberculosis` | `who-gho-odata` | notification rate (observation) and estimated incidence (estimate with bounds) |
| `eurostat-causes-of-death-tuberculosis` | `eurostat-sdmx-csv` through `SDMXConnector` | deaths by country and NUTS region with `OBS_FLAG` letters |
| `destatis-todesursachen-tuberkulose` | `destatis-genesis-ffcsv` through `GenesisConnector` (credentialed) | deaths by Land with GENESIS signs |

A new release (a new RKI tag, a new Eurostat `LAST UPDATE`, a new GENESIS
`Updated` stamp) is a new vintage; re-running the same release adds nothing.
The ECDC Surveillance Atlas has no machine interface: an operator downloads an
export and records it with `import_surveillance_export`.

## Align conditions

`align_surveillance_terms` publishes the namespace's source-native condition
terms (RKI disease names, ECDC topics, GHO indicator codes, Eurostat and
Destatis ICD-10 codes) as a term module. It cross-walks them to the
`clinical-mesh` module by exact label and to a version-tagged ICD module
(`clinical-icd10.<system>`, published with its own crosswalk to MeSH) by exact
code. Reviewer curations keep their kind (equivalent, broader, narrower,
related, incompatible). `expand_surveillance_condition` (and
`expand_clinical_question`) returns the series of an expanded condition with
every step explained. Unmapped terms are listed as gaps, and an incompatible
mapping blocks a term.

## Ask

- `surveillance_boundary_series` — the series for a condition within a
  boundary (a feature id, or a name resolved through place resolution), as of
  a reporting date. It lists the series resolved to the boundary and the
  series whose code lies within it, each with its own values, never
  aggregated. Sources for the same period, kind and unit are listed side by
  side. Values without a reporting date are listed apart. Each answer has a
  receipt that `replay_surveillance_query` re-runs with every parameter.
- `surveillance_series_values` — one vintage's values with both dates, kind,
  case-definition revision and source revision.
- `compare_surveillance_vintages` — differences between two published
  vintages, both cited; a value that falls under another case-definition
  edition is attributed to the definition change.
- `surveillance_reporting_delay` — per reference period, the values as first
  reported and in each later vintage, with the source's own delay note. A
  period is labelled `incomplete-by-source-note` only where the source says so.
- `surveillance_definition_history` — every case-definition revision.
- `surveillance_series_links` and `surveillance_series_claims` — publications
  and trials that cite a series' dataset, and the Science claims of those
  documents as a separate view.

## Keep watch

`create_surveillance_monitor` watches one series or a condition within a
boundary as a knowledge subscription. Thresholds are the user's own, with a
unit checked through pint against the series unit; a count is never compared
with a rate. `run_surveillance_monitor` evaluates at a committed watermark and
emits `threshold-exceeded`, `new-vintage`, `value-revised`,
`case-definition-changed`, `geography-break` and `stale-source` events.
Replaying a watermark emits nothing new. Refreshes run only through the source
pack's runtime schedule and the maintenance orchestrator.
`pin_surveillance_vintages` keeps the vintages a view used;
`surveillance_pin_status` reports `stale` when a newer vintage exists.

## Evidence

`tests/unit/domains/test_surveillance_acceptance.py` runs the whole journey
offline against the pinned fixtures. Offline fixture, operator and live
evidence are reported per release (`evidence_origin`). The dated live check
and the published demo are tracked separately (#2034).
