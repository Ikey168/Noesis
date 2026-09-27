# Public Procurement pack scope

Status: implemented offline, 2026-09-27 (see `docs/subsystems/procurement.md` and `docs/guides/procurement-pack.md`). Live coverage is not validated. The pack never submits bids or contacts buyers.

## Delivery state (audited 2026-09-27)

- **Shipped.** The code for #1877–#1891 is in:
  - `src/kb/procurement_*.py`;
  - `src/ingestion/procurement_providers.py`;
  - the `procurement` source pack (`config/source_packs/procurement.json`);
  - `packs/procurement/`.

  Tests are in `tests/unit/procurement/`, the offline acceptance suite is
  `tests/unit/domains/test_procurement_acceptance.py`, and the composition tests
  are in `tests/unit/composition/test_procurement_composition.py`. The issues
  are not closed by this change.
- **Composition: composed.** `packs/procurement/manifest.json` binds:
  - `procurement.core`;
  - `funding.core`, for the reused profile, eligibility, shortlist and
    workspace capabilities;
  - `market.lei`;
  - the shared research-project, authored-report, subscription and
    source-runtime providers.

  `set_procurement_bundle_enabled` is a coordinator selection change once the
  bundle is cut over. Funding & Grants keeps working when Procurement is
  disabled.
- **Live verification is not done.** The 2026-09-27 bounded live run reached no
  provider. TED, Find a Tender and Contracts Finder returned
  `source_unavailable` (egress proxy CONNECT 403). SAM.gov returned
  `credential_missing`. `LIVE_VERIFICATION` stays `unverified-live`, and the
  demo is offline. #1891's live half remains open.
- **German portals:** service.bund.de and the Berlin Vergabeplattform are
  `not-implemented` with reasons. Neither offers supported machine access to
  notice content, and neither is scraped. Their above-threshold notices come
  through TED.

Tracking: [#1848](https://github.com/Ikey168/Noesis/issues/1848).

## Outcome

Given an explicit private supplier or buyer profile, discover open and
upcoming public tenders, explain eligibility and competitive fit separately,
track award and contract history, and prepare a bid requirements checklist and
workspace. Deadlines, corrigenda and source freshness stay visible.

## Implementation issues

- [x] [#1877](https://github.com/Ikey168/Noesis/issues/1877) — P01 Audit procurement source contracts and select bounded provider coverage.
- [x] [#1878](https://github.com/Ikey168/Noesis/issues/1878) — P02 Define notice, lot, buyer, CPV, deadline, award, contract and supplier records.
- [x] [#1879](https://github.com/Ikey168/Noesis/issues/1879) — P03 Add private supplier and buyer profiles.
- [x] [#1880](https://github.com/Ikey168/Noesis/issues/1880) — P04 Acquire TED notices through the eForms API with field mapping (offline; live unverified).
- [x] [#1881](https://github.com/Ikey168/Noesis/issues/1881) — P05 Acquire UK Find a Tender and Contracts Finder OCDS releases (offline; live unverified).
- [x] [#1882](https://github.com/Ikey168/Noesis/issues/1882) — P06 Acquire SAM.gov opportunities; German federal and Berlin portals `not-implemented` with reason (offline; live unverified).
- [x] [#1883](https://github.com/Ikey168/Noesis/issues/1883) — P07 Normalise notices, revisions, deadlines, CPV codes and lots across sources.
- [x] [#1884](https://github.com/Ikey168/Noesis/issues/1884) — P08 Evaluate eligibility against exclusion grounds, selection criteria and thresholds with cited passages.
- [x] [#1885](https://github.com/Ikey168/Noesis/issues/1885) — P09 Rank opportunities by fit, value and effort with award history as context.
- [x] [#1886](https://github.com/Ikey168/Noesis/issues/1886) — P10 Link buyers and suppliers to corporate identity and award history across notices.
- [x] [#1887](https://github.com/Ikey168/Noesis/issues/1887) — P11 Create bid workspaces with requirement checklists, document lists and milestones.
- [x] [#1888](https://github.com/Ikey168/Noesis/issues/1888) — P12 Monitor new notices, corrigenda, deadline changes and awards.
- [x] [#1889](https://github.com/Ikey168/Noesis/issues/1889) — P13 Compose the Public Procurement bundle over funding and platform providers.
- [x] [#1890](https://github.com/Ikey168/Noesis/issues/1890) — P14 Add offline profile-to-shortlist-to-workspace acceptance coverage.
- [ ] [#1891](https://github.com/Ikey168/Noesis/issues/1891) — P15 Validate live procurement coverage and publish an explained shortlist demo. The live-check harness and an offline demo shipped, and the dated live run is recorded as blocked. Live coverage is **not** validated.

Order: P01 → P02 → P03 → {P04, P05, P06} → P07 → P08 → P09 → {P10, P11} → P12 → P13 → P14 → P15.

## Sources

- [TED API](https://docs.ted.europa.eu/api/index.html): eForms notice search.
- [Find a Tender API](https://www.find-tender.service.gov.uk/Search/Api) and [Contracts Finder API](https://www.contractsfinder.service.gov.uk/apidocumentation): OCDS releases.
- [SAM.gov Get Opportunities API](https://open.gsa.gov/api/get-opportunities-public-api/): credentialed.
- [service.bund.de](https://www.service.bund.de/) and the Berlin Vergabeplattform: `not-implemented`, because neither offers supported machine access to notice content.
- [Open Contracting Data Standard](https://standard.open-contracting.org/): the release model for the UK sources.

P01 verified the documented access method, terms and identifiers per provider.
These links establish source candidates, not live coverage. Award history
provides context; it never shows that a procedure is open.

## Composition and reuse

Follow the [pack/workflow architecture](../architecture/pack-workflow-composition.md).
The pack reuses the Funding & Grants profile, eligibility, ranking, workspace
and monitoring machinery (the funding stores are subclassed or their functions
called, never copied). It also reuses the source-pack runtime, subscriptions,
research projects, authored reports, `market.lei`, `canonical_entities` and
`EntityHistoryStore`. It adds procurement record owners only where records
differ: procedures, awards, assessments, shortlists, party links, workspaces
and monitors. It introduces no scheduler, permission ledger, project store or
submission engine.

## Acceptance

- A reproducible journey takes a supplier profile to an explained, current
  shortlist and a bid preparation workspace (offline:
  `test_profile_to_shortlist_to_workspace_journey_with_monitoring_and_restart`).
- Every hard eligibility conclusion cites the notice revision, and missing
  facts remain unknown.
- Deadlines preserve the original timezone and text, and corrigenda invalidate
  affected recommendations.
- Private profiles remain owner-scoped.
- Offline and bounded live evidence are reported separately. Offline evidence
  is the acceptance suite plus the demo. Live evidence is
  `docs/development/procurement-evidence/`, and it is blocked so far.

Automatic bid submission, buyer outreach, portal scraping, win-probability
prediction and legal advice are outside this scope.
