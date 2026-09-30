# Corporate Ownership: competition cases and state aid

The Corporate Ownership bundle's optional `competition` feature (#2217, default
off) answers: *given a company, which competition-authority cases name it -
merger notifications and decisions, antitrust proceedings, market
investigations - and which state-aid awards did it receive, with case stages,
cited decision documents and as-of time?*

It records what each authority published. It never predicts case outcomes,
assesses market power or market definition, assesses the compatibility or
legality of aid, or gives legal advice.

## Sources

Five sources in the `corporate-ownership` source pack (1.1.0), connector
`competition`, all `unverified-live` until the dated live run (CS14, #2368).
The access decisions, reuse terms, rate limits, instruments in and out of
scope and stable identifiers are in the
[source audit](../development/competition-evidence/source-audit.md).

| Source | Publisher | Units |
| --- | --- | --- |
| `ec-competition-cases` | European Commission case search | declared `M.`, `AT.`, `SA.` case numbers |
| `eu-state-aid-tam` | State Aid Transparency Award Module | member state + SA measure |
| `uk-cma-cases` | CMA via the GOV.UK Content API | case slugs |
| `us-ftc-cases` | FTC legal library | case page slugs |
| `us-doj-atr-cases` | DOJ Antitrust Division | civil case page slugs (criminal cases declined) |

A default `acquire_ownership_sources` run leaves these sources out unless the
feature is selected; name them in `source_ids` to acquire them explicitly.

## Records

`noesis-competition-record-v1` (`src/kb/competition_records.py`) - cases keyed
by authority and native number, append-only stages, parties (name and role as
published, no identity resolution), decision documents (linked, not
mirrored, with CELEX/OJ/decision numbers) and state-aid awards (amounts,
ranges and currencies verbatim). They are persisted through the ownership
record store with one revision per published change; corrections and
withdrawals in TAM and GOV.UK page updates are new revisions.

## Journey

1. `propose_competition_identity_matches` - case parties and aid beneficiaries
   against ownership entities: published identifiers first (a TAM KvK number),
   names as low evidence. `review_competition_identity_match` accepts or
   rejects (reviewer and time recorded); `list_competition_identity_candidates`
   shows unmatched parties and conflicts (one party, several entities).
2. `link_competition_citations` - legal acts by exact CELEX/ELI, TFEU article,
   UK Act or US Code reference through Legal works; TAM awards to their SA
   cases; cross-authority case references only when explicit. Unresolved
   references stay as source text.
3. `lookup_competition_cases` - cases naming a company (or its group through
   the ownership graph as of the date), with the party role, the stage in force
   and the identity match; authorities side by side; `no_case_on_record` is
   never a clean bill.
4. `competition_case_history` and `state_aid_awards_for_beneficiary` - the
   dated stage history with documents and citations; awards as published with
   the measure case, totals as computed views listing their inputs.
5. `build_competition_dossier` - the ownership dossier with a competition
   section.
6. `create_competition_monitor` / `run_competition_monitor` - new cases, stage
   changes, decision documents and new or corrected awards, each citing its
   before and after revision.

## Composition

Provider `ownership.competition`
(`packs/corporate-ownership/providers/ownership.competition.json`) is bound only
when the `competition` feature is selected. The Market pack does not reference
it: Corporate Ownership already depends on `market.lei`, so a Market feature
requiring ownership capabilities would be a bundle cycle (the same decision as
`bafin-voting-rights`); Market users reach the feature through the Corporate
Ownership bundle.

## Evidence

Offline: `tests/unit/domains/test_competition_acceptance.py` and the other
`test_competition_*` suites over authored, fictional fixtures. Live: none yet.
