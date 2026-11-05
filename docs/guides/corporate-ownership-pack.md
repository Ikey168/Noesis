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
   `subsidiaries`, `control_chain`, `successor_chain`, `holders` or `holdings`,
   plus an `as_of` date).
   Each edge carries its source, kind, validity and as-of status. Results
   include conflicts with reasons, reporting exceptions, minority and person
   holdings kept apart from parents, and the pins needed to replay them.
   `holders` (of an issuer) and `holdings` (of a filer, across issuers) answer
   from parsed SEC filings (the `sec-edgar-holdings` source). They return 13F
   positions and Schedule 13D/13G beneficial ownership in separate lists, never
   summed. Each line cites its accession number. Amendments are applied along
   their chain, and statements no longer in force are listed as `superseded`.
   See `docs/development/ownership-evidence/sec-holdings-source-audit.md`.
6. **Timeline** (`ownership_timeline`, `ownership_state_as_of`): cited entries
   with event time and record time; undated entries are listed as unknown.
   Market corporate actions are included when you pass the market namespace and
   security ids.
7. **Dossier** (`build_ownership_dossier`, `export_ownership_dossier`): one
   pinned dossier per identifier. Export writes an authored report in which
   every sourced statement cites its record revision.

## Holding structures: family offices, foundations, trusts and their links (#2890)

Family offices, private foundations, trusts, holding companies and SPVs are
ordinary ownership entities. You can mark one as such a structure, but only
when a source states it, and then see in one dossier how it connects to
lobbying, political donations and public money.

1. **Classify, only as stated.** Call
   `propose_ownership_structure_classification` with a `structure_type`
   (`family_office`, `private_foundation`, `trust`, `trustee`,
   `holding_company` or `special_purpose_vehicle`), a `basis`
   (`register-legal-form`, `self-description` or `declared-category`), the
   `stated_value` exactly as the source words it, and a `citation`. The
   citation is either one ownership record revision and the field that states
   the value, for example
   `{"kind": "ownership-record", "record_id": ..., "revision": 1, "field": "entity_type.subtype"}`,
   or an official document you obtained:
   `{"kind": "document", "reference", "sha256", "locator", "quote"}`. The
   quote must contain the stated value.
   - A proposal whose cited revision does not state the value is refused with
     `not_stated`. A proposal that cites a name field is refused with
     `name_inference_refused`.
   - `propose_ownership_structure_classifications_from_records` proposes a
     classification only where a record's stated legal form exactly equals an
     entry in `STATED_FORMS` (for example a BODS 0.4 arrangement with subtype
     `trust`). It never matches substrings or names.
2. **Review.** `review_ownership_structure_classification` accepts or rejects
   a proposal and needs `knowledge:ownership:review` and a reason.
   `revert_ownership_structure_classification` undoes the decision. Only an
   accepted classification classifies an entity. An entity without one stays
   `unclassified`, so "Example Family Office Ltd" with no stated legal form is
   not a family office. When accepted types from different sources disagree,
   both are returned side by side (`differ: true`).
3. **Dossier.** `build_ownership_structure_dossier` takes the same
   identifiers as the ownership dossier, plus a `sections` map that gives each
   linked section its namespace, for example
   `{"lobbying": {"namespace": "global"}, "political_donations": {...}, "public_payments": {...}}`.
   It returns these sections side by side:

   | Section | Record owner | Reached through |
   | --- | --- | --- |
   | `classification` | `src.kb.ownership_structures` | the entity's own records |
   | `ownership` (up and down) | `OwnershipGraph` | accepted ownership identity |
   | `sec_holdings` | `ownership_holdings` (13F, 13D/13G) | accepted ownership identity |
   | `bafin_holdings` | `BafinQueries.holdings_of_holder` | accepted identity decisions |
   | `lobbying` | `LobbyingQueries.registrant_declarations` | accepted identity decisions |
   | `political_donations` | `CampaignFinanceLinks` (ownership links) | links resting on accepted decisions |
   | `public_payments` | `PublicFinanceStore.payments` (EU FTS, budgets) | accepted identity decisions |
   | `competition` | `cases_for_company`, `awards_for_beneficiary` | accepted identity decisions |
   | `procurement` | `ProcurementIdentityService.award_history` | reviewed party links (canonical id or LEI) |
   | `sanctions` | `SanctionsQueries.statement_as_of` | accepted identity decisions |
   | `offshore_leaks` | ICIJ Offshore Leaks (#2888), `src.kb.offshore_leaks_links` | `inert` while the offshore-leaks feature is off |

   - Every line cites its owner's record revision and says how it was reached.
     Proposed candidates are listed under `pending` and never joined.
   - Each section has a status:
     - `assembled`: the section has cited lines.
     - `none_on_record`: nothing is linked. This does not mean nothing exists.
     - `inert`: the owning feature is not selected. This is not the same as
       empty.
     - `not_available`: no provider is installed.
     - `not_requested`: no namespace was given for the section.
     - The owner's own refusal code, for example `unauthorized`.
   - Amounts stay as published and are never summed across rows or sources.
     The competition owner's computed totals are not carried over.
   - The dossier never contains an influence score, a wrongdoing verdict or a
     risk rating. The `forbidden_keys` guard refuses any output that does.
   - For a natural person (a `person` record), the dossier is refused unless
     person-research mode (`NOESIS_PERSON_RESEARCH`) is on.
4. **Export.** `export_ownership_structure_dossier` returns a
   `noesis-evidence-bundle-v1`. The dossier is the root receipt, each section
   is a claim, and each cited record revision is an evidence object. Sections
   that are not assembled are listed as omissions, so `verify_bundle` reports
   the bundle as `incomplete`, not complete. Export refuses a dossier that no
   longer matches its hash, and it re-reads ownership citations at their
   pinned revision.

**Offshore Leaks section.** The `offshore_leaks` section resolves the
provider path `src.kb.offshore_leaks_links:structure_section` lazily, which the
ICIJ import (#2888) provides. Pass the ownership namespace holding the ICIJ
records as `sections={"offshore_leaks": {"namespace": ...}}`. It lists the ICIJ
entity records and officer/intermediary roles joined to the structure through
accepted identity decisions, each cited to its record revision, leak dataset
and node id with ICIJ's caveat, and is `inert` while the `offshore-leaks`
feature is off. Any other provider uses the same interface:
`structure_section(ctx: SectionContext) -> {"lines": [...], "pending": [...], "notice": ...}`,
and each line carries `statement`, `reached_by` and a `citation` (`id`,
`namespace`, `record`, `revision`). A provider can also be installed with
`register_section(SectionSpec(...))`. Uncited lines are refused
(`uncited_line`).

Offline evidence: `tests/unit/ownership/test_ownership_structures.py`,
`test_structure_dossier.py` and `test_structure_mcp.py`. They run on the
fictional world in `tests/unit/ownership/structure_harness.py`: a holding
company with ownership, lobbying, donation and public-payment records.

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
