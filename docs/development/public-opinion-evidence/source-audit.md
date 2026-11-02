# Public opinion and wellbeing: source-contract audit and bounded coverage (PO01)

Tracking: issue #2817 (PO01) · wave 3 tracker #2814 · recorded 2026-11-01.

This audit sets out, per candidate, what the Society bundle's proposed
`society.public-opinion` provider (subdomain `public-opinion-wellbeing`, domain
Society and population, existing `society` bundle, ADR-005) may acquire, how,
and on what terms. No per-track tracker or delivery issue exists yet; the
decision section says whether one should open. **It was written without
network access: the publisher hosts (`europa.eu`, `data.europa.eu`,
`www.gesis.org`, `search.gesis.org`, `www.europeansocialsurvey.org`,
`ess.sikt.no`, `sdmx.oecd.org`, `www.oecd.org`) are blocked by this runtime's
egress proxy (CONNECT refused with 403, verified 2026-11-01), so terms,
endpoints, dataflow ids, file names and rate limits were not re-verified
live.** They come from the issue's candidate list and the publishers'
documentation as the author knows it. Every item marked _verify_ must be
checked against the live pages, the live terms and a real response before the
track's first dated live run (its "Validate live coverage" issue, not yet
opened). No source is `live` until that run exists.

The machine-readable copy of these decisions does **not** exist yet.
`PROVIDER_CONTRACTS`, `BOUNDED_COVERAGE`, `CAPS`, `EXCLUSIONS` and
`LIVE_VERIFICATION` would be added in `src/ingestion/public_opinion_sources.py`
by the track's acquisition issues and must match this audit; a difference is
resolved by changing this audit first.

Non-goals for every source: no reweighting, pooling, imputation or new
estimates from microdata; no person-level (respondent-level) records; no
indices, composites or rankings of our own; no harmonisation of question
wordings across surveys or waves; no blending of Eurobarometer, ESS, OECD and
poll figures into one series; no trend lines, nowcasts or forecasts. Values
are stored and shown as each publisher released them, side by side.

## Access decisions

| Source (proposed source-pack id) | Publisher | Delivers | Access | Decision |
| --- | --- | --- | --- | --- |
| `opinion-oecd-wellbeing` | OECD, How's Life? / Well-being Database (WISE Centre) | current well-being indicators per country and year (life satisfaction, trust, social support, safety feeling, and other dimensions), with the indicator's underlying survey source | OECD Data Explorer SDMX REST API, `format=csvfile`, **through the existing SDMX connector's OECD path** (`src/ingestion/connectors/dataset/sdmx.py`, provider `OECD`, as `oecd-socx` in `src/ingestion/social_protection_sources.py` uses it); dataflow id _verify_ | `unverified-live` |
| `opinion-eurobarometer-tables` | European Commission (DG COMM), Eurobarometer | published topline results per survey: question, answer categories, percentages per member state and EU total, as tabulated by the Commission | per-survey report files from the Eurobarometer pages (`europa.eu/eurobarometer`) and their listing on `data.europa.eu`; the author understands that Standard Eurobarometer surveys publish an annex or data tables as a spreadsheet besides the PDF report (_verify_ that the file exists and its layout) | `unverified-live`, **conditional**: kept only for a per-survey tabular file; PDF-only reports and the interactive results pages are `not-implemented` (never scraped) |
| Eurobarometer microdata (GESIS) | GESIS - Leibniz Institute for the Social Sciences, archive of the Eurobarometer data files | respondent-level files (Stata/SPSS) per survey, with codebooks | GESIS Search download after login with a GESIS account and acceptance of the usage regulations (_verify_ the current terms and whether some files are open without login) | `gated-not-granted`: no account is held and none was requested; even with access, microdata are excluded by the non-goals |
| European Social Survey | ESS ERIC | respondent-level files per round and country, with weights and documentation | ESS Data Portal download after registration (_verify_ the portal host and terms on local retention and derived tables) | `gated-not-granted` for microdata (no registration held); the ESS published aggregate outputs known to the author are narrative "topline results" PDFs and an online analysis tool (_verify_), so no machine-readable aggregate source exists and the aggregates are `not-implemented` |

Why microdata stay out even if registration were granted: every figure
Noesis would show from microdata is an estimate Noesis computed (weighting,
design effects, missing-value handling). That is a new estimate, which the
non-goals exclude. Reopening ESS or GESIS microdata needs a decision record
that permits publisher-defined estimates computed locally, names the account
holder, and states the retention terms; it is not proposed.

**Gallup World Poll inside How's Life.** Several OECD subjective well-being
indicators (life satisfaction, some trust and safety measures) are, as the
author understands it, sourced from the Gallup World Poll under OECD's
arrangement with Gallup (_verify_ per indicator). The OECD licence may not
extend to every indicator whose source is a third party (_verify_ the OECD
terms' third-party clause). The acquisition issue must confirm, per declared
indicator, that the OECD terms cover redistribution; an indicator whose terms
are third-party-restricted is dropped from the selection, not acquired.

**Not audited here.** Eurostat's quality-of-life indicators (EU-SILC
well-being modules, for example overall life satisfaction, dataset codes
_verify_) would be an open, SDMX-reachable candidate through the existing
Eurostat path. They are not in the issue's candidate list; adding them needs
an amendment to this audit and a source-pack version bump.

**Unavailable-access fallback.** A failed request or file (HTTP error, 429
after one retry, redirect to an undeclared host, schema or layout drift, a
response over the byte budget) fails that source's run with its code and a
receipt; earlier vintages stay current and nothing is marked removed or
revised because of a failure. Readiness reports the source as `stale`.
Queries for ESS or GESIS report `gated-not-granted`, never empty.

## Per-source contract

| Source | Endpoints | Authentication | Licence and redistribution | Rate limits | Revision model: updates, corrections, removals |
| --- | --- | --- | --- | --- | --- |
| OECD How's Life (`opinion-oecd-wellbeing`) | `https://sdmx.oecd.org/public/rest/data/{agency},{dataflow},{version}/{key}?format=csvfile&startPeriod=...` (_verify_ the How's Life / WISE dataflow id and dimension order) | none | OECD terms; OECD data under CC BY 4.0 since 2024 (_verify_), with third-party-sourced indicators possibly excluded (_verify_) | the recorded OECD limits (about 20 data queries a minute, about 60 an hour, as `social_protection_sources.py` records them, _verify_); paced at the stricter one | each How's Life edition or database update restates values; every changed release is a new vintage; observation status attributes (estimated, break, _verify_ codes) are kept per value; a series a later complete release no longer states becomes `removed_by_source` |
| Eurobarometer tables (`opinion-eurobarometer-tables`) | per-survey file URLs listed on the survey page (`europa.eu/eurobarometer/surveys/detail/{id}`, _verify_) or its `data.europa.eu` dataset record (_verify_) | none | Commission reuse policy (Decision 2011/833/EU), CC BY 4.0 for Commission content unless stated otherwise (_verify_) | none published (_verify_); one file per declared survey | a survey (wave) is identified by its Eurobarometer number and fieldwork period; a corrected file re-published for the same survey is a new vintage of that survey's tables; the same question asked in a later wave is a new observation, never a revision of the earlier one |
| Eurobarometer microdata (GESIS) | none acquired | GESIS account (not held) | GESIS usage regulations (_verify_) | - | - |
| European Social Survey | none acquired | ESS registration (not held) | ESS conditions of use (_verify_; the data are understood to be free for non-commercial use with citation) | - | - |

## Metadata that travels with every value

The issue asks which wording, fieldwork and sample metadata must travel with
every value. Every stored value carries, as the publisher states it:

- **Question:** the survey and wave id (Eurobarometer number, How's Life
  edition), the question id, the question wording verbatim in the language the
  publisher gives (English for the Commission's tables), the answer category,
  and the response scale (for example 0-10, or "very satisfied" to "not at all
  satisfied").
- **Fieldwork:** first and last fieldwork date, mode (face-to-face, online,
  mixed, _verify_ per wave), and the fieldwork agency where stated.
- **Sample:** population (for example residents aged 15 and over), country,
  achieved sample size where stated, and the publisher's weighting statement
  (Eurobarometer tables are weighted by the Commission; Noesis never reweights).
- **Value:** the percentage or score as published, with its rounding, unit
  and the publisher's notes ("don't know" and refusals as separate categories,
  never removed or redistributed).
- **For OECD indicators:** the indicator definition, unit, the underlying
  survey source the OECD names (for example Gallup World Poll or EU-SILC), and
  the reference year, which may differ from the release year.

A value without its question wording and fieldwork dates is refused at write
time.

## Record shape and reuse

Record shape (`packs/taxonomy.json`): **statistical-series**. Each series is
keyed by publisher, survey or indicator, question id, answer category,
geography and unit; each release is a vintage. No new shape is needed. Survey
metadata above are definition and observation attributes, as Eurostat flags
and notes are for the tourism and social protection series.

- OECD values reuse the SDMX connector's OECD path and the economic series
  storage (`register_series` in `src/domains/economic/model.py`), as
  `society.social-protection` does; no second OECD client.
- Eurobarometer tables need a spreadsheet parser for the declared layout
  only; an undeclared layout fails the unit.
- Links: to `political.elections` poll readings only side by side, by country
  and period, with both citations. A survey value is never a poll reading,
  and a poll reading is never a survey value.

## Data minimisation decision

The kept sources publish aggregate tabulations; none returns data about a
respondent.

- **Stored:** published aggregates per survey, question, answer category,
  geography and period, with the metadata above and citations.
- **Redacted:** nothing; no personal field is acquired.
- **Excluded:** all microdata (GESIS, ESS, any other archive); breakdowns
  below a published cell the publisher flags as based on too few respondents
  (stored as a status only, _verify_ whether the Commission flags them);
  open-ended answer texts; interactive tool exports; derived, reweighted,
  pooled or blended values, which are refused at write time.
- **Retention:** vintages are kept for provenance; no erasure workflow
  applies.
- **Special-category note:** some Eurobarometer questions concern political
  opinions, religion or health. They are stored only as published national
  aggregates, which identify no person.

## Bounded first coverage

| Source | Selection | Caps |
| --- | --- | --- |
| OECD How's Life | three declared countries (for example DE, FR, IT); life satisfaction and one trust indicator whose OECD terms are confirmed to cover redistribution | 2 queries, 50 series per response |
| Eurobarometer tables | one declared Standard Eurobarometer wave; two declared questions (for example life satisfaction and trust in the national government, _verify_ question ids); the same three countries and the EU total | 1 file, 5 MB, 2 questions, 40 series |

Justification: one well-being indicator from two publishers for the same
countries shows two definitions side by side without blending, the OECD's
underlying-source attribution, and the Commission's question wording and
fieldwork metadata. Every further wave, question, indicator or country is a
source-pack version bump.

## Open questions, answered

- **Can published aggregates be used without microdata registration?** Yes,
  for OECD How's Life (open SDMX, terms _verify_ per third-party indicator)
  and for Eurobarometer if a per-survey tabular file exists (_verify_). No for
  ESS: no machine-readable published aggregates are known (_verify_), and
  computing them from microdata is a non-goal.
- **Which question wording, fieldwork dates and sample metadata must travel
  with every value?** The list under "Metadata that travels with every value":
  survey and wave id, question id and verbatim wording, answer category and
  scale, fieldwork start and end, mode, population, sample size, the
  publisher's weighting statement and notes, and for OECD the underlying
  survey source and reference year.

## Live verification

| Source | Status | Checked | Evidence |
| --- | --- | --- | --- |
| OECD How's Life (`opinion-oecd-wellbeing`) | `unverified-live` | - | none; dataflow id and third-party terms _verify_ |
| Eurobarometer tables (`opinion-eurobarometer-tables`) | `unverified-live` | - | none; conditional on a per-survey tabular file (_verify_) |
| Eurobarometer microdata (GESIS) | `gated-not-granted` | - | no GESIS account; microdata excluded by non-goals |
| European Social Survey microdata | `gated-not-granted` | - | no ESS registration; microdata excluded by non-goals |
| European Social Survey aggregates | `not-implemented` | - | no machine-readable aggregate source known (_verify_) |

Fixtures, when written, are authored, not captured: fictional countries from
the ISO user-assigned range, fictional survey numbers and reference periods in
2094-2097 with past-dated releases, and invented question texts marked as
fixtures, so nothing can be mistaken for a published figure.

## Decision and recommendation

Two sources survive as `unverified-live`: OECD How's Life through the existing
OECD SDMX path, and the Commission's Eurobarometer topline tables, conditional
on a per-survey tabular file. Eurobarometer microdata at GESIS and ESS
microdata are `gated-not-granted`; ESS aggregates are `not-implemented`.

Recommendation: open the PO per-track tracker (PO02-PO12, PO13 "Validate live
coverage"). OECD How's Life alone is enough to start; its first acquisition
issue must confirm the dataflow id and that the selected indicators' terms
allow redistribution. If no indicator's terms allow it and the Eurobarometer
file condition fails, the track ends here and `public-opinion-wellbeing`
stays a gap.
