# Legal pack: sanctions designations and trade-control lists

The Legal bundle's optional `sanctions` feature (#1907) answers what each
sanctions list, or each edition of the EU dual-use control list, **stated** on
a date, with citations. It is an expansion of Legal, not a new pack: one more
provider (`legal.sanctions`, record owner `src/kb/sanctions*.py`) plus sources
in the existing `legal-research` source pack.

It never produces a screening verdict, a risk score, a sanctions or AML
compliance determination or legal advice, and it never treats a similar name
as the same party. EU, UN, OFAC and UK entries are separate records with
separate revision histories; a cross-list match is a reviewable, reversible
identity decision.

## Enabling

The feature is off by default. Select it with the Legal root in the
composition coordinator (`features: ["sanctions"]`); it then binds
`legal.sanctions`, `legal.core`, `ownership.core` (identity candidates),
`market.lei`, `platform.entity-identity`, `economics.core` (Comext vintages)
and `platform.subscriptions`. If a consumed provider is missing, the plan
records a visible omission and Legal keeps resolving without the feature.
`src.kb.sanctions.feature_enabled` reads the active plan; there is no separate
enablement flag.

## Sources (pack `legal-research` 1.1.0)

| Source | What | Access decision |
| --- | --- | --- |
| `eu-sanctions-consolidated` | EU FSF XML 1.1 full file | `unverified-live` |
| `un-sc-consolidated` | UN SC Consolidated List XML | `unverified-live` |
| `ofac-sls` | OFAC SLS legacy SDN.XML | `unverified-live` (cross-host redirects are refused) |
| `uk-sanctions-list` | FCDO UK Sanctions List XML | `unverified-live` |
| `cellar-sanctions-acts-eng` | Regulation (EU) No 269/2014 with XHTML text | `unverified-live` |
| `cellar-dual-use-2021-821` | Regulation (EU) 2021/821, one annex amendment and two consolidated editions with annex text | `unverified-live` |

The EU Sanctions Map is `not-implemented` (no documented machine-readable
interface; never scraped). Comext flows are acquired through the Eurostat
dataset connector (`SanctionsTrade.acquire`), not the source-pack runtime.
See `docs/roadmaps/legal-sanctions-source-audit.md` for terms, cadences,
identifiers and the items still to verify.

A list file is one page and one snapshot. Run budgets must cover the whole
list (for example `max_results` above the list's entry count); a larger list
fails with `budget_exhausted` instead of storing a partial snapshot.

## Records (`noesis-sanctions-record-v1`)

- **snapshot** — list, publication date, file digest, acquisition time; the
  source revision of everything derived from it. Replaying the same file adds
  nothing, whatever the fetch time; an older file than the latest is refused.
- **designation** — one entry of one list keyed by its per-list identifier.
- **listing revision / delisting** — appended when a new snapshot adds,
  changes or drops the entry, citing both snapshots compared.
- **programme**, **legal basis** (resolved to a `legal_works` row by exact
  CELEX/ELI, else kept as the source string with status `unresolved`),
  **identifier alias** (names, transliterations, dates of birth, passports,
  IMO numbers, addresses, each tied to its revision).
- **control-list entry** — a control code in one annex edition at its passage
  locator.

## Answers

| Question | Tool |
| --- | --- |
| Current statements for an entry, stated identifier or exactly stated name | `lookup_sanctions_designation` |
| What each list stated on a date (revision, aliases, programme, legal-basis passages, delisting, identity links) | `designation_history_as_of` |
| Which annex edition applied on a date, and the control code's passage | `control_list_entry_as_of` |
| How a control code's passage changed between two editions | `compare_control_list_editions` |
| Correlated product codes (lookup aid) and Comext flows with vintages | `sanctions_trade_context` |

Status values are explicit: `unknown` before the first acquired snapshot or
when the list's statement changed between two snapshots (both statements are
returned side by side), `not_listed_in_snapshot` with the delisting if one was
recorded, and `listed` with the listing revision in force. Differences in
what lists state (for example two dates of birth) are shown side by side.

## Identity review

`propose_sanctions_identity_matches` proposes candidates only from identifiers
lists state (passport, national ID, IMO, LEI, registration number within its
register's country), across lists and to Corporate Ownership records.
`propose_sanctions_identity_link` records a reviewer's candidate resting on an
identifier or a name plus corroborating attributes the list states. All
candidates live in the Corporate Ownership identity state machine; review and
revert are entity identity decisions. A `similar-name` candidate can never be
accepted, and sanctions links never regroup ownership entities.

## Monitoring

`create_sanctions_monitor` subscribes to a designation, a programme within a
list, or a control code; `run_sanctions_monitor` evaluates it at a committed
watermark and emits `listed`, `amended`, `delisted`, `relisted` or
`new_edition` notifications citing the snapshots or editions compared. Events
go through the subscription store's poll and outbox paths.

## Evidence

Offline: `tests/unit/domains/test_sanctions_*.py`, with the full journey in
`tests/unit/domains/test_sanctions_acceptance.py`, all on authored fixtures
naming fictional parties. Live: none yet; the dated live validation and demo
are #1998 (S12).
