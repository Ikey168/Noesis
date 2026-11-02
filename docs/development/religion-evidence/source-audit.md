# Religion and belief: source-contract audit and bounded coverage (RB01)

Tracking: wave 3 tracker #2814 · audit issue #2824 · recorded 2026-11-01.
Bundle decision: [ADR-007](../../architecture/decisions/ADR-007-culture-bundle.md)
(CU01, #2815).

No per-track tracker or delivery issue exists yet. This audit leaves two
sources, both `unverified-live` and both **published census aggregates only**:
the **England and Wales Census 2021 religion tables (ONS, through Nomis)** and
the **Zensus 2022 religion tables (Destatis and the statistical offices of the
Länder)**, the latter with a precondition on machine access. Pew Research
Center's religious composition data and the Association of Religion Data
Archives (ARDA) are `not-implemented`.

**Written offline.** The publisher hosts (`www.ons.gov.uk`,
`www.nomisweb.co.uk`, `api.beta.ons.gov.uk`, `www.zensus2022.de`,
`ergebnisse.zensus2022.de`, `www-genesis.destatis.de`, `www.pewresearch.org`,
`www.thearda.com`) were blocked by this runtime's egress proxy (verified
2026-11-01), and nothing was fetched. Table codes, question wordings, response
categories, disclosure-control methods and terms come from the publishers'
documentation as the author knows it. Every item marked _verify_ is unchecked,
and the wordings in particular must be copied from the publishers' own
questionnaires before any fixture is written. The only facts confirmed are those
in this repository: the GENESIS path and its `NOESIS_DESTATIS_GENESIS_TOKEN`
secret declared for `destatis-genesis-population-migration`
(`config/source_packs/economic.json`) and the census-base handling of
`src/kb/demographics.py`. No source here is `live`.

**The machine-readable copy does not exist yet.** The track's acquisition
issues add `PROVIDER_CONTRACTS`, `BOUNDED_COVERAGE`, `MINIMISATION` and
`LIVE_VERIFICATION` for `culture.religion` and must match this audit.

Non-goals for every source: no person-level records or microdata; no
estimates, imputation, projections, small-area breakdowns or shares of our own;
no harmonisation of categories or question wordings across censuses or
countries; no ranking of places by religion; nothing that infers an individual's
religion from any other record.

## Access decisions

| Source (proposed source-pack id) | Publisher | Delivers | Access | Decision |
| --- | --- | --- | --- | --- |
| `ons-census2021-religion` | Office for National Statistics (ONS), through Nomis (operated for ONS by Durham University, _verify_) | Census 2021 England and Wales religion counts and published percentages by area, usual residents, from the voluntary religion question; topic summary TS030 "Religion" (_verify_ code; Nomis dataset believed `NM_2049_1`, _verify_) | Nomis API v01, CSV per dataset with declared geography and cell selections, no key required for queries within the row limit (_verify_; an optional `uid` raises it) | `unverified-live` |
| `zensus2022-religion` | Statistisches Bundesamt (Destatis) and the statistical offices of the Länder, Zensus 2022 | Zensus 2022 counts by legal membership of a public-law religious society (believed: Roman Catholic Church, Protestant Church (EKD), other or none or not stated, _verify_ categories), reference date 15 May 2022 (_verify_), by area | the Zensus 2022 results database (`ergebnisse.zensus2022.de`, believed GENESIS-based with a GENESIS-style REST interface, _verify_ whether it requires a registered token, and whether the tables are also in GENESIS-Online) | `unverified-live` (precondition below) |
| `pew-religious-composition` | Pew Research Center | estimated religious composition by country (for example 2010 and 2020 estimates and 2010-2050 projections, _verify_ editions), modelled from censuses, surveys and registers | downloadable files after account registration and acceptance of dataset terms (_verify_); no API | `not-implemented` |
| `arda-datasets` | Association of Religion Data Archives (ARDA, Pennsylvania State University, _verify_) | archived survey datasets (mostly person-level microdata), the US Religion Census county files (congregations and adherents, from the Association of Statisticians of American Religious Bodies, _verify_) and national profiles partly drawn from licensed compilations (_verify_) | downloads per dataset after accepting its terms (_verify_); no API known | `not-implemented` |

Reasons:

- **ONS Census 2021.** A national statistics release under the Open Government
  Licence v3.0 (_verify_ for Nomis downloads), published as aggregates after
  ONS's own disclosure control (_verify_: targeted record swapping and cell key
  perturbation). The question was voluntary, and "Not answered" is a published
  category, kept as a category. Scotland (census 2022, National Records of
  Scotland) and Northern Ireland (census 2021, NISRA) asked different questions
  and are not in this source (_verify_); each would be its own source in an
  amendment.
- **Zensus 2022.** The German census does not, to the author's knowledge, carry
  a self-identification question on religion in 2022: the published variable is
  legal membership of the Roman Catholic or Protestant churches from registers,
  with the rest grouped (_verify_ the variable, its source and its categories,
  and whether Zensus 2011's voluntary creed question had any 2022 successor).
  That makes it a different concept from the ONS question, which is the reason
  both are kept side by side and never harmonised. Licence: Datenlizenz
  Deutschland Namensnennung 2.0 (_verify_). **Precondition:** the source stays
  `unverified-live` only while one of two paths is confirmed by a live check:
  the Zensus database's REST interface (with a required secret
  `NOESIS_ZENSUS2022_TOKEN`, name proposed, if it needs one) or the same tables
  in GENESIS-Online through the existing `NOESIS_DESTATIS_GENESIS_TOKEN`. If
  neither exists, the source moves to `not-implemented` (interactive pages are
  never scraped).
- **Pew.** Pew's figures are its own modelled estimates and projections,
  combining sources with demographic models. Storing them as Pew's stated
  estimates would not breach the "no estimates of our own" rule, but the reuse
  terms do not allow it as far as the author can establish offline: dataset
  downloads require an account and acceptance of terms that the author believes
  restrict redistribution of the data (_verify_), and no API exists. Not
  implemented; Pew reports may be cited link-only by URL and publication date.
- **ARDA.** Terms differ per dataset and are set by each depositor (_verify_).
  Most holdings are person-level survey microdata, which RB never acquires. The
  aggregate candidates (US Religion Census county files) carry their own terms
  from ASARB (_verify_), and the national profiles mix in licensed compilations
  such as the World Religion Database (_verify_). Not implemented; a dataset may
  be cited link-only. A single aggregate ARDA file with confirmed terms could be
  added by amendment.

### Further candidates not audited

Other national censuses with religion tables (for example Statistics Canada,
Australian Bureau of Statistics, National Records of Scotland, NISRA) and church
membership statistics published by the churches themselves (for example EKD and
the German Bishops' Conference, which are organisational statistics, not
census) were not audited. Each needs an amendment and a source-pack version
bump.

## Per-source contract (as far as known)

| Source | Endpoints | Authentication and keys | Licence and redistribution | Rate limits | Revision model: updates, corrections, removals |
| --- | --- | --- | --- | --- | --- |
| ONS Census 2021 via Nomis | `https://www.nomisweb.co.uk/api/v01/dataset/{dataset}.data.csv?geography=...&measures=...` (_verify_ dataset id, dimension names and measure codes for count and percent) | none; optional `uid` as secret `NOESIS_NOMIS_UID` (name proposed) only if row limits require it | Open Government Licence v3.0 (_verify_); attribution "Source: Office for National Statistics licensed under the Open Government Licence v.3.0" (_verify_ wording) | row limit per query (_verify_); one request per declared document | Census 2021 is a single release per table, but ONS has published corrections and re-releases of some tables (_verify_ for TS030); each changed release is a new vintage with its stated release date; a geography change (for example 2021 to 2023 local authority boundaries, _verify_) is a different place key, never re-aggregated |
| Zensus 2022 | Zensus database REST interface (_verify_ base URL) or GENESIS-Online `https://www-genesis.destatis.de/genesisWS/rest/2020` (confirmed in the economic pack) with the Zensus table codes (_verify_) | `NOESIS_ZENSUS2022_TOKEN` (proposed, if required) or the existing `NOESIS_DESTATIS_GENESIS_TOKEN` | Datenlizenz Deutschland Namensnennung 2.0 (_verify_); attribution as Destatis states | _verify_ | tables carry an Updated stamp (as GENESIS does); each changed release is a new vintage; the census base is part of the definition, as in `src/kb/demographics.py` |
| Pew | none acquired | - | terms believed to restrict redistribution (_verify_); link-only | - | - |
| ARDA | none acquired | - | per dataset (_verify_); link-only | - | - |

**Unavailable-access fallback.** A failed document fails that source's run with
its code and a receipt; earlier vintages stay current and nothing is marked
removed. Queries for Pew and ARDA report `not-implemented`, never empty.

## Definitions kept side by side

Answering the issue: each table is stored with a **definition revision**, as
`src/kb/demographics.py` stores definitions, and two definitions are never
merged.

- **Question wording, verbatim**, in the publisher's language, with its
  instructions: for England and Wales the voluntary "What is your religion?"
  (_verify_ exact wording and tick-box list); for Zensus 2022 the register
  variable's published label (_verify_). A register variable is marked
  `basis: register`, a questionnaire variable `basis: self-reported`.
- **Response categories, verbatim**, each as its own code with the publisher's
  label: for example ONS "No religion", "Christian", "Buddhist", "Hindu",
  "Jewish", "Muslim", "Sikh", "Other religion", "Not answered" (_verify_), and
  the Zensus categories as published. "No religion", "none", "not stated", "not
  answered" and "other or none" are different categories and stay different;
  no category is mapped onto another publisher's.
- **Voluntary or compulsory**, **population base** (usual residents, register
  population), **reference date**, and the publisher's **disclosure-control
  method** as stated.
- A comparison across the two sources returns both tables, each with its own
  definition, and the marker `not-harmonised`. Nothing computes a difference,
  ratio, trend or common total across them.

## Data minimisation decision

Religion is special-category data (GDPR Art. 9). Policy
`religion-minimisation-v1`, following ADR-007, decision 5:

- **Stored:** published aggregate cells per table, place, category and
  reference date, with the publisher's flags, notes, definitions and citations;
  published percentages only where the publisher released them.
- **Disclosure control kept as published:** perturbed, rounded or swapped values
  are stored exactly as released and marked with the publisher's method; a
  suppressed or confidential cell is stored as its status, never as a value or
  zero; totals are never recomputed from cells (perturbed cells need not add up);
  no cell is derived by differencing two published tables.
- **Geography floor:** first coverage stops at local-authority level (England
  and Wales) and Land and Berlin-district level where published (Germany). Lower
  published levels (for example output areas) are not acquired in first
  coverage, although published, to keep small counts out until an amendment
  decides on them.
- **Excluded:** microdata and person-level records of every kind; ARDA survey
  files; any join of religion tables to records about individuals in other
  providers; any estimate, imputation or projection, including Pew's.
- **Retention:** vintages are kept for provenance; no erasure workflow applies
  because no personal data is held.
- **Who may query:** `knowledge:culture:religion:read` with namespace access;
  writes need `knowledge:culture:religion:write`.

## Record shape and reuse

Shape: **statistical-series** (census tables with release vintages). No new
shape. Reuse: the Economics series storage (`register_series`,
`economic_vintages`) for values and the definition-revision, census-base and
break handling pattern of `src/kb/demographics.py`; the GENESIS request path of
the economic source pack for Zensus; places through the Geospatial bundle's
shared codes (LAD codes, AGS, NUTS). Population totals are linked to
`economics.demographics` by shared place code and citation, never divided into
the religion counts.

## Bounded first coverage

| Source | Places | Tables | Caps |
| --- | --- | --- | --- |
| ONS Census 2021 via Nomis | England and Wales, England, Wales, and 2 declared local authorities | TS030 religion, counts and published percentages (_verify_ code) | 1 document, 5 places, 9 categories |
| Zensus 2022 | Germany, Berlin | the religion table by legal membership (_verify_ code) | 1 document, 2 places, published categories only |

Justification: two countries whose censuses measure religion differently show
the side-by-side rule at the smallest scale. Berlin links to the place the
Geospatial bundle already resolves and to the `economics.demographics` Berlin
series. Every further place, level or table is a source-pack version bump.

## Live verification

| Source | Status | Checked | Evidence |
| --- | --- | --- | --- |
| ONS Census 2021 via Nomis | `unverified-live` | - | none; dataset id and dimensions _verify_ |
| Zensus 2022 | `unverified-live` (precondition) | - | none; machine access path and token requirement _verify_ |
| Pew religious composition | `not-implemented` | - | account-gated downloads, redistribution terms believed restrictive (_verify_), modelled estimates |
| ARDA | `not-implemented` | - | per-dataset terms; holdings mostly person-level microdata |

Fixtures are authored, not captured: fictional places and counts for fictional
reference dates in 2094-2097, past-dated releases as the tourism audit's
deviation note requires, and category labels copied from the verified
questionnaires once checked.

## Conclusion

- **Survives:** ONS Census 2021 religion tables via Nomis, and Zensus 2022 under
  its precondition, both `unverified-live`, published aggregates only, with
  disclosure control kept and definitions side by side. A per-track tracker may
  open; its first issue is a bounded live check of the Nomis dataset id and of a
  machine path to the Zensus table.
- **Not implemented:** Pew (terms, modelled estimates) and ARDA (per-dataset
  terms, microdata).
- **Depth:** two publishers (ONS, Destatis), but one per country; ADR-006 notes
  that depth two by jurisdiction still leaves one publisher per place.
