# Corporate Ownership pack: from identifier to explained dossier

This guide walks the Corporate Ownership and Registries journey end to end,
states its boundaries, and lists each provider's live state. Architecture and
module map: `docs/subsystems/corporate-ownership.md`.

## Boundaries

- An ownership assertion is what one source states. Nothing infers
  beneficial ownership or makes a sanctions or AML determination.
- Identity matches are proposals. Accepting or rejecting one is a recorded
  entity identity decision, and every decision can be reverted. Records are
  never merged or rewritten.
- Conflicting assertions coexist and are returned together with reasons. A
  reporting exception is a statement that an owner is not reported; it does not
  mean there is no owner.
- Unknown dates, shares and holders stay unknown and are listed.
- Handelsregister, Unternehmensregister and BRIS are not implemented because
  they offer no supported machine access. No scraper exists. Official documents
  you obtain yourself can be recorded with their reference and digest.
- Coverage is bounded to the identifiers selected in the installed source pack.

## The journey

1. **Install and accept terms** (operator):
   `ownership_bundle.install_source_pack(conn, principal_id=..., scopes={"operator"}, accept_terms=True)`
   installs `config/source_packs/corporate-ownership.json` and records licence
   acceptance per source.
2. **Acquire** (`acquire_ownership_sources`, scopes `knowledge:ownership:write`
   and `knowledge:ingestion:execute`): runs the pack through `SourcePackRuntime`
   with cursors, budgets and receipts, then projects GLEIF Level 2 data from the
   market.lei owner. Companies House needs `NOESIS_COMPANIES_HOUSE_API_KEY` and
   SEC EDGAR needs `NOESIS_SEC_CONTACT` (a real contact for the User-Agent);
   without them the runtime preflight stops that source.
3. **Look up** (`lookup_ownership_entity`): by `lei`, `gb-coh` or
   `company_number`, `cik`, or `name` as `"Name|CC"`. A name returns candidates
   and never picks one.
4. **Reconcile** (`propose_ownership_identity_matches`, then
   `review_ownership_identity_match` with `knowledge:ownership:review`, and
   `revert_ownership_identity_match` to undo a decision). Each candidate shows
   its basis (exact identifier, cross-referenced identifier, or
   name + jurisdiction), its evidence and a confidence.
5. **Query** (`ownership_graph` with `direct_parents`, `ultimate_parents`,
   `subsidiaries`, `control_chain` or `successor_chain`, plus an `as_of` date).
   Each edge carries its source, kind, validity and as-of status. Results
   include conflicts with reasons, reporting exceptions, minority and person
   holdings kept apart from parents, and the pins needed to replay them.
6. **Timeline** (`ownership_timeline`, `ownership_state_as_of`): cited entries
   with event time and record time; undated entries are listed as unknown.
   Market corporate actions are included when you pass the market namespace and
   security ids.
7. **Dossier** (`build_ownership_dossier`, `export_ownership_dossier`): one
   pinned dossier per identifier. Export writes an authored report in which
   every sourced statement cites its record revision.

## Per-provider live state

| Provider | Offline (fixtures) | Live (`LIVE_VERIFICATION`, run 2026-09-27) |
| --- | --- | --- |
| GLEIF Level 2 | covered | unverified-live: `source_unavailable` (egress proxy refused CONNECT) |
| Companies House | covered | unverified-live: `credential_missing` (no API key configured); host unreachable from this runtime |
| SEC EDGAR | covered | unverified-live: `credential_missing` (no fair-access contact configured); host unreachable from this runtime |
| Open Ownership BODS | covered (GB PSC source only) | unverified-live: not attempted, no verified dataset path; host unreachable from this runtime |
| OpenCorporates | reused provider (#1483) | as recorded by that provider |
| Handelsregister, Unternehmensregister, BRIS | not implemented | not implemented |

## Offline and live evidence, separately

- **Offline**: `pytest tests/unit/ownership tests/unit/domains/test_corporate_ownership_acceptance.py`
  runs the whole journey on authored fixtures (`tests/fixtures/ownership`, all
  companies fictional) with network access refused. The demo
  `docs/examples/ownership-dossier-demo.md` (regenerate with
  `python scripts/ownership_demo.py --output docs/examples/ownership-dossier-demo.md`)
  is offline evidence too.
- **Live**: `python scripts/ownership_live_check.py --output docs/development/ownership-evidence/live-check-<date>.json`
  records a dated bounded run per provider with failure causes. The only
  recorded run (2026-09-27) verified no provider. Treat any claim of live
  coverage as unproven until a run from a network that can reach the providers
  succeeds.
