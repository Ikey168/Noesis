# Society: social protection

The Society bundle (`packs/society/`) gains the provider
`society.social-protection` (track #2741, subdomain `social-protection`)
behind three optional features, one per source: `esspros`, `socx` and
`ilo-coverage` (all default off). It answers: *given a country, a measure and
a date, what social protection expenditure, pension beneficiaries and coverage
did Eurostat, the OECD and the ILO publish, under which classification,
definition and release, as of that date?*

It quotes what each source released. It never nowcasts, fills a year a source
did not publish, blends ESSPROS, SOCX and ILO figures into one series,
re-classifies one publisher's functions into another's, combines anything with
COFOG, computes a per-capita, per-beneficiary or share-of-GDP figure of its
own or any other derived indicator, and it makes no forecast.

## Sources

Three sources in the `society-social-protection` source pack (1.0.0,
`config/source_packs/society-social-protection.json`), connector
`social-protection`, all `unverified-live` until the dated live run (SS13).
Access, terms, rate limits, the revision model, the minimisation decision and
the bounded coverage are in the
[source audit](../development/social-protection-evidence/source-audit.md). It
was written without network access, so items marked _verify_ (endpoints,
dataset and dataflow codes, dimension and attribute names, licences, rate
limits) were not checked live.

| Source | Publisher | Bounded first coverage | Access |
| --- | --- | --- | --- |
| `eurostat-esspros` | Eurostat ESSPROS (SDMX-CSV through the SDMX connector, ESTAT path) | Germany and France; `spr_exp_sum` total expenditure (`MIO_EUR`, `PC_GDP`), `spr_exp_func` old age and sickness/health care, `spr_pns_ben` pension beneficiaries; 3 documents, 60 series per response | no key |
| `oecd-socx` | OECD SOCX (SDMX REST, `format=csvfile`, OECD path) | Germany and France; public social expenditure, total and old age, % of GDP; 1 document, 20 series; paced at one request a minute until the OECD limits are verified | no key |
| `ilo-social-protection-coverage` | ILOSTAT SDG 1.3.1 (SDMX-CSV, ILO path, `ILO,DF_SDG_0131_SEX_SOC_RT,1.0`, _verify_) | Germany and France; total and old-age coverage, both sexes; 1 document, 20 series | no key |

The ILO World Social Protection Data Dashboards are `not-implemented` and never
scraped. ILO regional and global modelled estimates and OECD net social
expenditure are separate series and not in first coverage.

## Records and vintages

`noesis-social-protection-record-v2` (`src/kb/social_protection_records.py`,
`src/kb/social_protection_store.py`). A **series** is keyed by source, dataset
or dataflow, measure (`expenditure`, `beneficiaries`, `coverage`), the
publisher's own classification (ESSPROS function `spfunc` or pension category,
SOCX policy area, ILO contingency), scheme or benefit type, source of
financing, cash or in kind, gross or net basis, sex, unit as published, place
and frequency. ILO's population denominator is stored per series. The ESSPROS
manual edition, the SOCX methodology and ILO's `NOTE_SOURCE`,
`NOTE_INDICATOR` and `NOTE_CLASSIF` (verbatim) are definition revisions.

Each value keeps its text as published (published percentages and
per-inhabitant values are never recomputed), its cell status (`reported`,
`confidential`, `not_published`), the publication status the source states
(`provisional`, `estimated`, `projected`; OECD estimates and projections keep
it and are never final) and its flags verbatim (`b`, `p`, `e`, `d`, `c`; `c` is
a status, never a value). Each changed release is an appended **vintage**
dated by Eurostat's `LAST UPDATE`, a declared OECD or ILO release date, or the
retrieval time labelled `retrieval_time`. A World Social Protection Report
edition that restates earlier years is a new vintage; a series a complete
later release no longer states gets a `removed_by_source` vintage. Values also
live in the Economics series storage (`economic_vintages`,
`dataset_observations`, domain `society`).

## The journey and the tools

1. Acquire through the shared source-pack tools (pack
   `society-social-protection`). A failed document (HTTP error, redirect to
   another host, schema drift, an over-budget response) fails that source's
   run with its code and a receipt; earlier vintages stay current and
   `social_protection_readiness` reports the source `stale`.
2. `propose_social_protection_place_matches` matches Eurostat GEO and ISO
   alpha-3 codes to Geospatial places by published ISO code (names are context
   only); `review_social_protection_identity` accepts or rejects as another
   principal; `revert_social_protection_identity` undoes it. Unmatched codes
   stay visible in `list_social_protection_identity`.
3. `propose_social_protection_function_relations` (same published label,
   confidence low) and `record_social_protection_function_relation` (stated
   with citations) propose that one publisher's function is *related* to
   another's; only an accepted relation lets a function request reach the
   other publisher, labelled as such. A COFOG function is refused: COFOG stays
   a third, distinct concept.
4. `link_social_protection_series` links series to the population denominator
   in `economics.demographics` and to COFOG social-protection expenditure
   (`gov_10a_exp`, `GF10`) in `economics.public-finance` by shared code or
   accepted match, pinning both revisions; nothing is computed from linked
   series. Absent providers are `provider_absent`, places without a target
   `target_not_held`.
5. `social_protection_indicator_for_place` returns each source's values for a
   place as released by the date, grouped by measure: expenditure, beneficiary
   counts and coverage are never paired as the same thing
   (`different_measure`); rows of one measure list the source-stated
   ESSPROS-SOCX scope notes, else `comparability_unknown`; COFOG appears only
   as cited links. `social_protection_profile` gathers the three measures and
   `export_social_protection_profile` returns a `noesis-evidence-bundle-v1`
   citing source, record revision and as-of time per item.
6. `social_protection_series_history` lists every vintage with new, revised
   and dropped periods, estimates replaced, report-edition restatements,
   manual edition and definition changes and removals, and per release pair
   the notes that apply, else `comparability_unknown`.
7. `create_social_protection_monitor` / `run_social_protection_monitor` notify
   new releases, new periods, revised values, edition restatements,
   definition changes and removals through `platform.subscriptions`, citing
   the record revisions before and after; a failed run never yields a removal
   notice.

Every answer declares the exclusions and is checked against the minimisation
decision before it leaves.

## Data minimisation

Published aggregates only. Person- and household-level fields
(benefit-recipient registers, administrative or survey microdata, Social
Security Inquiry returns) and derived, filled, blended, re-classified or
forecast values are refused at write time; confidential cells are stored as
their status, never as a value. Reads need
`knowledge:social-protection:read` and namespace access.

## What is not live

Nothing here has been run against the publishers: their hosts are blocked by
the cloud runtime's egress proxy. Offline evidence is
`tests/unit/domains/test_social_protection_*.py`, the acceptance journey
`test_social_protection_acceptance.py` and
`tests/unit/composition/test_society_social_protection_composition.py`, on
authored fixtures with synthetic values (reference years 2094-2098, releases
in 2098-2099). Live coverage waits for SS13 (#2808). Offline coverage is not
live coverage.
