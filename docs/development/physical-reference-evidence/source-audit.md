# Physical sciences reference: source-contract audit and bounded coverage (PR01)

Tracking: issue #2818 (PR01) · wave 3 tracker #2814 · recorded 2026-11-01.

This audit sets out, per candidate, what the Science bundle's proposed
`science.physical-reference` provider (subdomain `physical-sciences-reference`,
domain Science and knowledge, ADR-005) may acquire, how, and on what terms. No
per-track tracker or delivery issue exists yet; the decision section says
whether one should open. **It was written without network access: the
publisher hosts (`physics.nist.gov`, `www.nist.gov`, `nds.iaea.org`,
`www-nds.iaea.org`, `pdg.lbl.gov`, `pdgapi.lbl.gov`) are blocked by this
runtime's egress proxy (CONNECT refused with 403, verified 2026-11-01), so
terms, endpoints, file names, field names and rate limits were not
re-verified live.** They come from the issue's candidate list and the
publishers' documentation as the author knows it. Every item marked _verify_
must be checked against the live pages, the live terms and a real response
before the track's first dated live run (its "Validate live coverage" issue,
not yet opened). No source is `live` until that run exists.

The machine-readable copy of these decisions does **not** exist yet.
`PROVIDER_CONTRACTS`, `BOUNDED_COVERAGE`, `CAPS`, `EXCLUSIONS` and
`LIVE_VERIFICATION` would be added in
`src/ingestion/physical_reference_sources.py` by the track's acquisition issues
and must match this audit; a difference is resolved by changing this audit
first.

Non-goals for every source: no unit conversion beyond what the publisher
provides, no derived constants or quantities computed by Noesis, no merging,
averaging or "best value" selection across evaluations or editions (CODATA
2018 and 2022, or a CODATA and a PDG value for the same particle, stay side
by side), no rounding or re-expression of uncertainties, no conversion of
values to binary floating point for storage, and no physics computation or
modelling.

## Access decisions

| Source (proposed source-pack id) | Publisher | Delivers | Access | Decision |
| --- | --- | --- | --- | --- |
| `physref-codata-constants` | NIST Physical Measurement Laboratory, CODATA Task Group on Fundamental Constants (NIST SRD 121, _verify_ the SRD number) | recommended values of the fundamental physical constants per adjustment (2018, 2022, ...): quantity name, value, standard uncertainty, unit, exact-value marker | one fixed-width ASCII table per adjustment: the current `allascii.txt` and archived tables for earlier adjustments (_verify_ paths and archive naming) | `unverified-live` |
| `physref-nist-asd` | NIST, Atomic Spectra Database (NIST SRD 78, _verify_) | evaluated spectral lines (wavelength, uncertainty, relative intensity, transition probability, accuracy class, configurations, terms, J, references) and energy levels per spectrum | the ASD lines and levels query forms (`lines_form.html`, `levels_form.html`) with a CSV or tab-delimited output option (_verify_ parameter names and formats); no documented API or published rate limit (_verify_) | `unverified-live` |
| `physref-iaea-livechart` | IAEA Nuclear Data Section, LiveChart of Nuclides (data from ENSDF, AME, NUBASE and other evaluations) | ground-state and level properties per nuclide: half-life with uncertainty and unit, decay modes and branchings, spin-parity, mass excess, with the evaluation each field comes from | LiveChart data download API, CSV over HTTPS (`https://nds.iaea.org/relnsd/v1/data?fields=...&nuclides=...`, _verify_ host, path and field names); a user-agent identification is requested (_verify_) | `unverified-live` |
| `physref-pdg-summary` | Particle Data Group (LBNL and collaboration), Review of Particle Physics | summary-table values per particle and property (mass, width, lifetime, branching fractions) with uncertainties, per edition | the PDG API and/or a per-edition SQLite file distributed for the `pdg` Python package (_verify_ host, file name and schema); the printed Review is published open access (_verify_ licence) | `unverified-live`, **conditional**: kept only if the acquisition issue confirms the PDG data licence permits storage and redistribution with citation (_verify_); otherwise `not-implemented` |

No source needs a key, so none is `gated-not-granted`.

**NIST terms.** Works of NIST employees are generally not subject to
copyright in the United States (17 U.S.C. 105), but NIST Standard Reference
Data may be copyrighted under the Standard Reference Data Act (15 U.S.C.
290e), and individual NIST databases carry their own terms (_verify_ the
current terms of SRD 121 and SRD 78 and any attribution wording). The
acquisition issue must confirm that storing the published values and
exporting them with citation is permitted; if a database's terms forbid it,
that source becomes `not-implemented`.

**Unavailable-access fallback.** A failed request (HTTP error, 429 after one
retry, redirect to an undeclared host, a table whose header or column widths
differ from the declared layout, a response over the byte budget) fails that
source's run with its code and a receipt; earlier editions stay current and
nothing is marked removed or revised because of a failure. Readiness reports
the source as `stale`.

## Per-source contract

| Source | Endpoints | Authentication | Licence and redistribution | Rate limits | Revision model: updates, corrections, removals |
| --- | --- | --- | --- | --- | --- |
| CODATA (`physref-codata-constants`) | `https://physics.nist.gov/cuu/Constants/Table/allascii.txt` (current) and the archived per-adjustment ASCII tables (`https://physics.nist.gov/cuu/Constants/ArchiveASCII/allascii_<year>.txt`, _verify_) | none | NIST SRD terms (_verify_); citation of the CODATA adjustment paper as NIST recommends (_verify_ wording) | none published (_verify_); one request per declared adjustment | each adjustment is an **edition**: its values never revise another edition's; the edition year is part of every record key, so 2018 and 2022 values sit side by side; a constant that appears in one edition and not another is simply absent from that edition; a corrected re-issue of an edition's table (same year) is a new revision of that edition, compared by content digest |
| NIST ASD (`physref-nist-asd`) | `https://physics.nist.gov/cgi-bin/ASD/lines1.pl?spectra=<spectrum>&low_w=...&upp_w=...&unit=...&format=...` and `energy1.pl` for levels (_verify_ every parameter) | none | NIST SRD 78 terms (_verify_); cite the ASD version (for example "NIST ASD version 5.x", _verify_) and the original references it lists per line | undocumented (_verify_); paced at 1 request every 5 seconds; at most the declared queries per run | the database states a version number per release (_verify_ where); a changed answer for the same query under a new version is a new revision; per-line bibliographic references are kept verbatim; a line absent from a later version's answer becomes `removed_by_source` for that query |
| IAEA LiveChart (`physref-iaea-livechart`) | `https://nds.iaea.org/relnsd/v1/data?fields=ground_states&nuclides=<nuclide>` and level/decay fields (_verify_) | none | IAEA terms of use for its websites and NDS data (_verify_; NDS data are understood to be freely available with citation of the evaluation and of LiveChart) | none published (_verify_); paced at 1 request/second; one request per declared nuclide and field set | each field cites its evaluation (ENSDF evaluation date or reference, AME or NUBASE edition, _verify_ the columns that state them); a changed value with a changed evaluation reference is a new revision tied to that evaluation; values are never merged across evaluations; the retrieval date alone never dates a value |
| PDG (`physref-pdg-summary`) | the PDG API (`pdgapi.lbl.gov`, _verify_) or the per-edition SQLite file (_verify_) | none | PDG data licence (_verify_; the Review is understood to be published open access under CC BY 4.0, which may not cover the database) | none published (_verify_); one file or bounded set of requests per edition | each Review edition (2022, 2024, ...) and its annual update is an edition, part of every record key; an edition never revises another; PDG identifiers (`pdgid`) are kept as published |

## Record shape and reuse

Record shape (`packs/taxonomy.json`): **registry-records**. Each record is an
entry in a published reference catalogue (a constant, a spectral line or
level, a nuclide property, a particle property), keyed by publisher, edition
or evaluation, and the publisher's own identifier (quantity name, spectrum
and line, nuclide Z and A, `pdgid`). Its revisions are corrections within one
edition; another edition is a different record. This is how
`science.life-sciences` and `chemicals.substances` use the shape.

The issue asks whether a "reference-value" variant is needed. This audit
does **not** propose one: value, uncertainty, unit and edition are
attributes of a registry record. If review finds that value-with-uncertainty
semantics do not fit `registry-records` (or `observations`, which covers
experimental properties in `materials.experimental`), a new shape needs a
decision record first, before any provider code.

Value representation, for every source:

- the value and its uncertainty are stored as the **published decimal
  strings**, digit for digit (for example a CODATA value with its spaces and
  ellipsis, or "(exact)"), never parsed to binary floating point; a parsed
  decimal may be kept beside the string for sorting only;
- the unit is stored as published; no unit is converted;
- relative uncertainties, correlations and derived quantities are not
  computed; the CODATA correlation coefficients are out of first coverage.

Links: to `chemicals.substances` and `materials.*` only by an identifier the
records state (element symbol or atomic number), shown side by side with
citations. No physical property is attached to a substance or material by
inference.

## Data minimisation decision

The sources publish physical reference data; none is about a natural person.

- **Stored:** published values, uncertainties, units, exactness markers,
  edition or evaluation, publisher identifiers, and the per-value
  bibliographic references the source lists.
- **Redacted:** nothing; no personal field is acquired.
- **Excluded:** author contact details in bibliographic records beyond the
  citation string; full spectra or full nuclide charts beyond the declared
  selection; ENSDF raw files and evaluator worksheets; PDG review articles'
  text (cited by locator, never mirrored); derived, converted or merged
  values, which are refused at write time.
- **Retention:** editions and revisions are kept for provenance; no erasure
  workflow applies.

## Bounded first coverage

| Source | Selection | Caps |
| --- | --- | --- |
| CODATA | adjustments 2018 and 2022, complete tables | 2 files, 200 KB each, 400 rows each |
| NIST ASD | one declared spectrum (for example H I) in one declared wavelength range | 1 lines query, 1 levels query, 500 rows each |
| IAEA LiveChart | five declared nuclides (for example H-3, C-14, Co-60, Cs-137, U-235), ground-state fields | 5 requests, 50 rows each |
| PDG | one declared edition; five declared particles (for example electron, muon, proton, neutron, Z boson), mass and lifetime or width | 1 file or 10 requests, 20 MB file budget |

Justification: two CODATA editions side by side answer the issue's first
question directly; the electron, muon, proton and neutron masses appear in
both CODATA and PDG, which shows two evaluations of the same quantity kept
apart; one spectrum and five nuclides show per-value references and
evaluation dates. The demand for this subdomain is narrow, so first coverage
stays small. Every further edition, spectrum, nuclide or particle is a
source-pack version bump.

## Open questions, answered

- **Is every value stored with its uncertainty, unit and edition, so CODATA
  2018 and 2022 sit side by side?** Yes. Value and uncertainty are stored as
  published decimal strings, with the published unit and the edition (CODATA
  adjustment year, ASD version, LiveChart field evaluation, PDG edition) in
  the record key. Editions never revise each other.
- **Which existing record shape fits?** `registry-records`, with the edition
  in the key. No new shape is proposed; if one is needed, a decision record
  comes first.

## Live verification

| Source | Status | Checked | Evidence |
| --- | --- | --- | --- |
| CODATA (`physref-codata-constants`) | `unverified-live` | - | none; SRD terms _verify_ |
| NIST ASD (`physref-nist-asd`) | `unverified-live` | - | none; query parameters, rate limits and SRD terms _verify_ |
| IAEA LiveChart (`physref-iaea-livechart`) | `unverified-live` | - | none; API path and terms _verify_ |
| PDG (`physref-pdg-summary`) | `unverified-live` | - | none; conditional on the data licence (_verify_) |

Fixtures, when written, are authored, not captured: invented quantity names
and values clearly marked as fixtures (for example "fixture coupling
constant"), fictional editions 2094 and 2098, fictional spectra and nuclide
values, so nothing can be mistaken for a published constant.

## Decision and recommendation

All four sources survive as `unverified-live`: CODATA constants, NIST ASD and
IAEA LiveChart, subject to their terms (_verify_), and PDG summary tables,
conditional on its data licence. This is the most open wave 3 track; demand
is narrow, so first coverage stays small.

Recommendation: open the PR per-track tracker (PR02-PR12, PR13 "Validate live
coverage") and deliver it first in wave 3, starting with CODATA (one file per
edition, simplest layout), then IAEA LiveChart, NIST ASD and PDG. The shape
question above should be confirmed in the tracker's records issue before
any provider code.
