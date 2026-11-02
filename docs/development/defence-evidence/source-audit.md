# Defence and security: source-contract audit and bounded coverage (DF01)

Tracking: issue #2816 (DF01) · wave 3 tracker #2814 · recorded 2026-11-01.

This audit sets out, per candidate, what the Political bundle's proposed
`political.defence` provider (subdomain `defence-security`, domain Governance
and law, ADR-005) may acquire, how, and on what terms. No per-track tracker or
delivery issue exists yet; the decision section says whether one should open.
**It was written without network access: the publisher hosts
(`www.sipri.org`, `milex.sipri.org`, `armstransfers.sipri.org`, `www.unroca.org`,
`disarmament.unoda.org`, `digitallibrary.un.org`, `www.nato.int`) are blocked by
this runtime's egress proxy (CONNECT refused with 403, verified 2026-11-01), so
terms, endpoints, file names, table layouts and rate limits were not
re-verified live.** They come from the issue's candidate list and the
publishers' documentation as the author knows it. Every item marked _verify_
must be checked against the live pages, the live terms and a real file before
the track's first dated live run (its "Validate live coverage" issue, not yet
opened). No source is `live` until that run exists.

The machine-readable copy of these decisions does **not** exist yet.
`PROVIDER_CONTRACTS`, `BOUNDED_COVERAGE`, `CAPS`, `EXCLUSIONS` and
`LIVE_VERIFICATION` would be added in `src/ingestion/defence_sources.py` by the
track's acquisition issues and must match this audit; a difference is resolved
by changing this audit first.

Non-goals for every source: no assessment of military capability or strength,
no threat, risk or "militarisation" scores, no rankings of our own, no
reconciliation of exporter and importer reports, no blending of SIPRI, NATO,
UN Register or national figures into one series, no currency conversion,
deflation, shares or per-capita figures computed by Noesis, no inference that a
transfer breached a sanctions regime or an embargo, and no forecasts. Values are
stored and shown as each publisher released them, side by side.

## Access decisions

| Source (proposed source-pack id) | Publisher | Delivers | Access | Decision |
| --- | --- | --- | --- | --- |
| SIPRI Military Expenditure Database | Stockholm International Peace Research Institute (SIPRI) | military expenditure by country and year in local currency, constant and current US dollars, share of GDP and of government spending, per capita | one Excel workbook per annual release from the SIPRI website; no documented API (_verify_) | `not-implemented`: the terms are understood to restrict redistribution and republication of the data (in whole or substantial part) without SIPRI's permission (_verify_); see "SIPRI: stored, metadata-only or linked" |
| SIPRI Arms Transfers Database | SIPRI | transfers of major conventional arms by supplier, recipient, weapon designation and year; trend-indicator values (TIV) | interactive query pages that produce trade registers and TIV tables (CSV or RTF output, _verify_); no documented API | `not-implemented`: the same terms (_verify_); query pages are never scraped; TIV is a SIPRI-modelled volume measure, not a reported figure |
| `defence-unroca-reports` | United Nations Office for Disarmament Affairs (UNODA), UN Register of Conventional Arms | national reports of exports and imports in the seven Register categories (and optional background information on holdings, procurement and SALW), per reporting state and calendar year | the UNROCA database (`www.unroca.org`), per-state and per-year report pages and, as the author understands it, the original national submissions as files (_verify_ whether a file or data export exists); the Secretary-General's annual report on the Register (a UN General Assembly document with a document symbol) reproduces the returns (_verify_ that annexes still carry them) | `unverified-live`, **conditional**: kept only if the acquisition issue confirms a stable per-report file or export; if only HTML report pages exist, the source becomes `not-implemented` before any code (pages are never scraped) |
| `defence-nato-expenditure` | NATO, Public Diplomacy Division | "Defence Expenditure of NATO Countries" annual release: defence expenditure in national currency and US dollars (current and constant prices), share of GDP, per-capita, equipment and personnel shares, military personnel, per member and year, with NATO estimates marked | one annual press-release document per release (`www.nato.int`, press release / communiqué series); a PDF with tables and, as the author understands it, a spreadsheet of the same tables (_verify_ that the spreadsheet exists and its layout) | `unverified-live`, **conditional**: kept only for a spreadsheet file; a PDF-only release is `not-implemented` (no table extraction from PDF in first coverage) |

No source here needs a key, so none is `gated-not-granted`. A SIPRI data
licence agreement, if SIPRI offers one, was not requested and is not held; the
decision would only change through a decision record that names the agreement
holder and the export restriction.

**Unavailable-access fallback.** A failed document (HTTP error, redirect to an
undeclared host, a workbook whose header row or sheet names differ from the
declared layout, a file over the byte budget) fails that source's run with its
code and a receipt; earlier vintages stay current and nothing is marked
removed or revised because of a failure. Readiness reports the source as
`stale`. Queries for SIPRI report `not-implemented` with the reason, never an
empty result.

## SIPRI: stored, metadata-only or linked

The issue asks whether SIPRI can be used citation-only or metadata-only.
Decision, pending the live terms (_verify_ all three points):

1. **Stored values: no.** Storing SIPRI figures and re-displaying them,
   exporting them in evidence bundles or serving them through MCP tools is
   redistribution. The terms as the author understands them permit use and
   citation in one's own work but not republication of the data without
   permission. That conflicts with the intended use.
2. **Metadata-only records: not worth a source.** A record that says "SIPRI
   Milex, release 20xx, country X" without the value adds nothing a citation
   does not, and would let the subdomain look covered while answering no
   question. It is not implemented, so coverage is never reported for it.
3. **Linked or cited: yes, outside this provider.** A user may cite a SIPRI
   table in authored research through the existing citation path (a locator
   and the release year in the bibliography). This provider does not create,
   preserve or snapshot SIPRI pages; preservation copies would themselves be
   redistribution until the terms are confirmed.

Reopening SIPRI needs an amendment of this audit with the confirmed terms or a
written permission, and a source-pack version bump.

## Per-source contract

| Source | Endpoints | Authentication | Licence and redistribution | Rate limits | Revision model: updates, corrections, removals |
| --- | --- | --- | --- | --- | --- |
| SIPRI Milex and Arms Transfers | none acquired | - | SIPRI terms: use with citation, no redistribution or republication without permission (_verify_) | - | annual releases revise the whole back series (Milex, as the author understands it) and the TIV series; not stored, so not modelled |
| UN Register (`defence-unroca-reports`) | `https://www.unroca.org/` per-state, per-year report (_verify_ URL pattern and file export); fallback: the Secretary-General's annual report on the Register by document symbol through the UN Digital Library (_verify_) | none | United Nations terms of use for its websites (_verify_; the un.org terms are understood to permit copying for personal, non-commercial use, which needs checking against evidence-bundle export); the reports are official submissions of member states, published by the UN | none published (_verify_); one request per declared report, paced at 1 request/second | a report is keyed by reporting state, calendar year and submission; a later submission or correction for the same state and year is a new revision of that report, never a merge; a nil report is a report stating nil, stored as such; no report is a status (`not_reported`), never zero; late submissions after the annual SG report are recorded with their own submission date where the source states it (_verify_ whether UNROCA states one) |
| NATO (`defence-nato-expenditure`) | the release's spreadsheet file on `www.nato.int` (_verify_ path pattern; files are linked from each year's press release) | none | NATO website terms: reproduction permitted with acknowledgement of the source (_verify_ wording and any non-commercial limit) | none published (_verify_); one file per release | each annual release restates earlier years (estimates become figures, base year of constant prices moves, _verify_); every release is a new vintage of every series it states, never an overwrite; values NATO marks as estimates (`e`, _verify_ the marker) keep that status; a member or year a later release omits becomes a `removed_by_source` vintage for that series; constant-price series carry the base year as part of the series key, so a rebased series is a different series |

## Record shapes and reuse

Record shapes (`packs/taxonomy.json`): **statistical-series** for NATO
expenditure and personnel tables (release vintages, as-of selection), and
**registry-records** for the UN Register's reported transfer lines (entries in
a published register, revised by later submissions). No new shape is needed.

- NATO series reuse the economic series storage (`register_series` in
  `src/domains/economic/model.py`, as `src/kb/labour_statistics.py` uses it);
  the series key carries member, indicator, unit, price basis and base year.
- UN Register lines are stored per reporting state, year, direction (export
  or import), category, as stated: the partner state named, number of items,
  description and remarks columns verbatim. An exporter's line and the
  importer's line for the same transfer are two records from two reports; they
  are never matched, reconciled or deduplicated.
- Links: to `legal.sanctions` and `humanitarian.core` only by the ISO country
  code a record states, shown side by side with their citations. No link
  asserts that a transfer is sanctioned, embargoed or connected to a crisis.

## Data minimisation decision

The sources publish state-level aggregates and state-reported transfer lines;
none is about a natural person.

- **Stored:** published values with their units, price basis, estimate
  markers, notes and citations; UN Register report lines as reported.
- **Redacted:** nothing; no personal field is acquired.
- **Excluded:** SIPRI data of any kind; the UN Register's optional background
  information on military holdings and procurement in first coverage (it is
  sparsely reported and is a separate document per state, _verify_); any
  company, end-user or contract-level detail that a national report's remarks
  column may contain beyond the verbatim cell; derived, reconciled, converted
  or blended values, which are refused at write time.
- **Retention:** vintages and report revisions are kept for provenance; no
  erasure workflow applies.
- **Theme:** `security-defence`, as for `legal.sanctions` and
  `humanitarian.core`.

## Bounded first coverage

| Source | Selection | Caps |
| --- | --- | --- |
| NATO expenditure | one declared annual release; three declared members; the tables for expenditure in national currency (current prices), share of GDP and equipment share | 1 file, 2 MB, 3 tables, 60 series |
| UN Register | one declared reporting year; two declared reporting states, both directions | 4 reports, 200 lines per report |

Justification: one NATO release shows estimate markers and the restatement of
earlier years in a later vintage; two states' Register reports for the same
year show an exporter and an importer report side by side without
reconciliation, and a nil report. Every further release, member, state or year
is a source-pack version bump.

## Open questions, answered

- **Can any source be stored and re-displayed locally, or only linked?**
  NATO tables and UN Register reports can be stored and re-displayed with
  attribution, subject to the _verify_ terms. SIPRI can only be cited by
  locator outside this provider; nothing from SIPRI is stored.
- **Are national submissions to the UN Register reproduced as reported?**
  Yes. Each report is stored as the reporting state submitted it, with its
  own revision chain. Exporter and importer figures are never reconciled,
  matched or summed, and a missing report is `not_reported`, not zero.

## Live verification

| Source | Status | Checked | Evidence |
| --- | --- | --- | --- |
| SIPRI Military Expenditure Database | `not-implemented` | - | terms restrict redistribution (_verify_) |
| SIPRI Arms Transfers Database | `not-implemented` | - | terms restrict redistribution (_verify_); no machine access |
| UN Register (`defence-unroca-reports`) | `unverified-live` | - | none; conditional on a file export (_verify_) |
| NATO (`defence-nato-expenditure`) | `unverified-live` | - | none; conditional on a spreadsheet (_verify_) |

Fixtures, when written, are authored, not captured: fictional states from the
ISO user-assigned range (`XA`, `XB`, `XC`), fictional reference years 2094-2097
with past-dated releases, as the wave 2 tracks settled.

## Decision and recommendation

Two sources survive, both `unverified-live` and both conditional on a
machine-readable file: NATO defence expenditure tables (spreadsheet) and UN
Register national reports. SIPRI's two databases are `not-implemented` because
their terms are understood to restrict redistribution; citation by locator in
authored research stays possible outside the provider.

Recommendation: open the DF per-track tracker (DF02-DF12, DF13 "Validate live
coverage"), with its first acquisition issue confirming the two file
conditions before any parser is written. If both conditions fail, the track
ends here, `defence-security` stays a gap, and the tracker records that.
