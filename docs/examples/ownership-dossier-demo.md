# Corporate Ownership — explained dossier demo

> **Evidence: offline authored fixtures only.** Every provider response comes from `tests/fixtures/ownership` (hand-written to mirror GLEIF, Companies House, SEC EDGAR and Open Ownership response shapes). The companies, officers, people and filings are **fictional**; identifiers are illustrative. This is **not** a live capture and not a dossier of any real company. Live checks are recorded separately in `docs/development/ownership-evidence/` (the 2026-09-27 run could not reach any provider).
>
> Ownership assertions are what each source states. Conflicts are shown, not resolved. Nothing here is a beneficial-ownership, sanctions or AML determination.

Regenerate with `python scripts/ownership_demo.py --output docs/examples/ownership-dossier-demo.md`. Query: LEI `213800EXAMPLAUKLTD71`, as of 2025-06-01. Reviewer decisions follow the rule stated in the script docstring.

## Identity: which source records were grouped, and why

Grouping happens only through reviewed identity decisions (recorded in `src/kb/entity_history.py`); no record was merged or rewritten.

| Candidate | Basis | Confidence | Decision |
| --- | --- | --- | --- |
| `open-ownership:statement:oo-fixture-ent-decoy-1` ↔ `open-ownership:statement:oo-fixture-ent-hold-1` | name-jurisdiction | 0.35 | rejected |
| `gleif:lei:213800EXAMPLAHOLDS95` ↔ `open-ownership:statement:oo-fixture-ent-decoy-1` | name-jurisdiction | 0.35 | rejected |
| `gleif:lei:213800EXAMPLAHOLDS95` ↔ `open-ownership:statement:oo-fixture-ent-hold-1` | cross-referenced-identifier | 0.8 | accepted |
| `companies-house:gb-coh:09990002` ↔ `gleif:lei:213800EXAMPLAUKLTD71` | cross-referenced-identifier | 0.8 | accepted |
| `companies-house:gb-coh:09990001` ↔ `open-ownership:statement:oo-fixture-ent-decoy-1` | name-jurisdiction | 0.35 | rejected |
| `gleif:lei:213800EXAMPLAUKLTD71` ↔ `open-ownership:statement:oo-fixture-ent-uk-1` | exact-identifier | 0.95 | accepted |
| `companies-house:psc:09990002:FIXPSC002` ↔ `gleif:lei:724500EXAMPLAINTBV75` | name-jurisdiction | 0.35 | accepted |
| `gleif:lei:724500EXAMPLAINTBV75` ↔ `open-ownership:statement:oo-fixture-ent-int-1` | exact-identifier | 0.95 | accepted |
| `companies-house:gb-coh:09990001` ↔ `gleif:lei:213800EXAMPLAHOLDS95` | cross-referenced-identifier | 0.8 | accepted |
| `companies-house:gb-coh:09990001` ↔ `open-ownership:statement:oo-fixture-ent-hold-1` | exact-identifier | 0.95 | accepted |
| `gleif:lei:213800EXAMPLAHOLDS95` ↔ `sec-edgar:cik:0009999101` | cross-referenced-identifier | 0.8 | accepted |
| `companies-house:psc:09990002:FIXPSC002` ↔ `open-ownership:statement:oo-fixture-ent-int-1` | name-jurisdiction | 0.35 | accepted |
| `companies-house:gb-coh:09990002` ↔ `open-ownership:statement:oo-fixture-ent-uk-1` | exact-identifier | 0.95 | accepted |

## Entities

- `companies-house:gb-coh:09990001` — EXAMPLA HOLDINGS PLC (companies-house, GB); Exampla Holdings plc (gleif, GB); EXAMPLA HOLDINGS PLC (open-ownership, GB); EXAMPLA HOLDINGS PLC (sec-edgar)
- `companies-house:gb-coh:09990002` — EXAMPLA UK LIMITED (companies-house, GB); Exampla UK Limited (gleif, GB); EXAMPLA UK LIMITED (open-ownership, GB)
- `companies-house:psc:09990002:FIXPSC002` — EXAMPLA INTERMEDIATE B.V. (companies-house, NL); Exampla Intermediate B.V. (gleif, NL); EXAMPLA INTERMEDIATE B.V. (open-ownership, NL)

## Direct parents and control as of 2025-06-01

| Holder | Kind | Figure | Validity | As-of status | Source |
| --- | --- | --- | --- | --- | --- |
| Exampla Holdings plc | direct_parent (consolidation-as-stated) | no figure stated | 2016-04-06 → open | valid | gleif |
| EXAMPLA INTERMEDIATE B.V. | shareholding (majority-as-stated) | band ≥75% ≤100% | 2025-01-01 → unknown | undetermined | open-ownership |
| EXAMPLA INTERMEDIATE B.V. | shareholding (majority-as-stated) | band ≥75% ≤100% | 2025-01-01 → open | valid | companies-house |
| EXAMPLA INTERMEDIATE B.V. | voting_rights (majority-as-stated) | band ≥75% ≤100% | 2025-01-01 → unknown | undetermined | open-ownership |
| EXAMPLA INTERMEDIATE B.V. | voting_rights (majority-as-stated) | band ≥75% ≤100% | 2025-01-01 → open | valid | companies-house |
| person (owner-scoped) (person; owner-scoped) | significant_influence | no figure stated | 2025-01-01 → unknown | undetermined | open-ownership |

Excluded as of this date (kept, not deleted): appoint_directors from companies-house (ended), shareholding from companies-house (ended), voting_rights from companies-house (ended).

## Conflicts (returned together, never ranked)

- **direct_parents**: `companies-house:gb-coh:09990001`, `companies-house:psc:09990002:FIXPSC002` — different_source, different_date, different_kind. not resolved; each assertion is what its source states.
- **same_edge**: `companies-house:psc:09990002:FIXPSC002` — different_source, different_date, different_kind. returned together; not resolved.

## Ultimate parent

- Stated by gleif: `companies-house:gb-coh:09990001` (ultimate_parent, 2016-04-06 → open).
- Derived chain top(s): `companies-house:gb-coh:09990001` — top of the stated direct/control chain; a derived path, not a stated ultimate parent.

## Reporting exceptions (a statement that an owner is not reported — not 'no owner')

- `companies-house:gb-coh:09990001`: psc-exempt-as-trading-on-regulated-market (psc) per companies-house, validity 2016-06-30 → open.
- `companies-house:gb-coh:09990001`: interested-party-exempt-from-disclosure (any) per open-ownership, validity unknown → unknown.
- `companies-house:gb-coh:09990001`: NO_KNOWN_PERSON (direct) per gleif, validity unknown → unknown.

## Successors and predecessors (linked by events, never collapsed)

- `gleif:lei:213800EXAMPLATRADE88` → `companies-house:gb-coh:09990002`: succession, date unknown (gleif).

## Officers

| Officer | Role | Appointed | Resigned | Source |
| --- | --- | --- | --- | --- |
| POE, Kim | secretary | unknown | — | companies-house |
| DOE, Alex | director | 2015-02-01 | 2021-05-31 | companies-house |
| ROE, Sam | director | 2021-06-01 | — | companies-house |

## Filings

| Accession / transaction | Form | Filed | Parsed | Sources |
| --- | --- | --- | --- | --- |
| FIXTX001 | NEWINC | 2012-03-01 | reference only | companies-house |
| FIXTX002 | NM01 | 2015-07-01 | reference only | companies-house |
| FIXTX003 | AA | 2016-09-30 | reference only | companies-house |
| FIXTX004 | PSC07 | 2025-01-10 | reference only | companies-house |
| FIXTX005 | PSC02 | 2025-01-10 | reference only | companies-house |

## Timeline

| Event time | Kind | What | Source |
| --- | --- | --- | --- |
| 2012-03-01 | registration | Registered with Companies House as 09990002 | companies-house |
| 2012-03-01 | event | incorporation: Incorporated as ltd in england-wales | companies-house |
| 2012-03-01 | filing | NEWINC filed (FIXTX001), reference only | companies-house |
| 2015-02-01 | officer | DOE, Alex appointed director | companies-house |
| 2015-07-01 | event | name_change: Ceased to use the name EXAMPLA SERVICES LIMITED | companies-house |
| 2015-07-01 | filing | NM01 filed (FIXTX002), reference only | companies-house |
| 2016-04-06 | ownership | Stated from: appoint_directors by EXAMPLA HOLDINGS PLC | companies-house |
| 2016-04-06 | ownership | Stated from: shareholding by EXAMPLA HOLDINGS PLC | companies-house |
| 2016-04-06 | ownership | Stated from: voting_rights by EXAMPLA HOLDINGS PLC | companies-house |
| 2016-04-06 | ownership | Stated from: direct_parent by Exampla Holdings plc | gleif |
| 2016-04-06 | ownership | Stated from: ultimate_parent by Exampla Holdings plc | gleif |
| 2016-09-30 | filing | AA filed (FIXTX003), reference only | companies-house |
| 2021-05-31 | officer | DOE, Alex resigned as director | companies-house |
| 2021-06-01 | officer | ROE, Sam appointed director | companies-house |
| 2024-12-31 | ownership | Stated ended: appoint_directors by EXAMPLA HOLDINGS PLC | companies-house |
| 2024-12-31 | ownership | Stated ended: shareholding by EXAMPLA HOLDINGS PLC | companies-house |
| 2024-12-31 | ownership | Stated ended: voting_rights by EXAMPLA HOLDINGS PLC | companies-house |
| 2025-01-01 | ownership | Stated from: shareholding by EXAMPLA INTERMEDIATE B.V. | companies-house |
| 2025-01-01 | ownership | Stated from: voting_rights by EXAMPLA INTERMEDIATE B.V. | companies-house |
| 2025-01-01 | ownership | Stated from: shareholding by EXAMPLA INTERMEDIATE B.V. | open-ownership |
| 2025-01-01 | ownership | Stated from: significant_influence by Jordan Example (fictional) | open-ownership |
| 2025-01-01 | ownership | Stated from: voting_rights by EXAMPLA INTERMEDIATE B.V. | open-ownership |
| 2025-01-10 | filing | PSC02 filed (FIXTX005), reference only | companies-house |
| 2025-01-10 | filing | PSC07 filed (FIXTX004), reference only | companies-house |
| 2025-05-20 | market_corporate_action | cash_dividend (confirmed) for security:exampla-ord | fixture-provider |

Undated (unknown, not interpolated):

- event: succession: GLEIF names 213800EXAMPLAUKLTD71 as successor entity; entity status INACTIVE, registration status RETIRED (gleif)
- officer: POE, Kim appointed secretary (companies-house)
- registration: Registered with Companies House as 09990002 (gleif)

## Unknowns (listed, never defaulted)

- filing_reference (companies-house): ownership_figures (filing not parsed) — 5 records
- legal_entity (gleif): founding_date — 1 record
- legal_entity (open-ownership): founding_date, status — 1 record
- officer_role (companies-house): appointed_on — 1 record
- ownership_assertion (open-ownership): validity.to — 3 records
- registration (gleif): registered_on — 1 record

## The listed parent: Exampla Holdings plc (fictional; CIK 0009999101)

Subsidiaries stated as of the same date, minority holdings disclosed on Schedule 13G cover pages (holdings, not parents) and SEC filing references:

- Subsidiary `companies-house:gb-coh:09990002`: direct_parent (gleif)
- Subsidiary `companies-house:psc:09990002:FIXPSC002`: direct_parent (gleif)
- Minority holding: Northwind GP LLC (fictional) 8.2% of Ordinary Shares as of 2025-03-31 (end unknown), per SCHEDULE 13G cover page, as reported by the filer.
- Minority holding: Northwind Capital LP (fictional) 8.2% of Ordinary Shares as of 2025-03-31 (end unknown), per SCHEDULE 13G cover page, as reported by the filer.
- SC 13G 0001888001-23-000002 filed 2023-02-14 (reference only).
- SCHEDULE 13G 0001888001-25-000003 filed 2025-04-10 (cover page parsed).
- 20-F 0009999101-25-000010 filed 2025-04-15 (reference only).
- 6-K 0009999101-25-000011 filed 2025-05-02 (reference only).

## Replay

Dossier hash `3d020f5fd540cf66b09be238feab3b60e441ee45ce20ea954f32f8cbd1202a5e` over 62 pinned record revisions and 10 accepted identity decisions; evidence kind `offline-fixture`.
