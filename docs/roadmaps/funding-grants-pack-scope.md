# Funding & Grants pack scope

Status: proposed implementation backlog, 2026-09-25. Planning only; creating
issues does not implement integrations or submit funding applications.

Tracking: [#1761](https://github.com/Ikey168/Noesis/issues/1761).

## Outcome

Given an explicit private applicant/project profile, discover open and upcoming
funding opportunities, explain eligibility and competitive fit separately, and
prepare a requirements checklist and application drafts. Keep missing facts,
source freshness, and application obligations visible.

## Implementation issues

- [ ] [#1762](https://github.com/Ikey168/Noesis/issues/1762) — Audit funding source contracts and select bounded provider coverage.
- [ ] [#1763](https://github.com/Ikey168/Noesis/issues/1763) — Define funding programme, call, deadline and financial-term records.
- [ ] [#1764](https://github.com/Ikey168/Noesis/issues/1764) — Add private applicant and funding-project profiles.
- [ ] [#1765](https://github.com/Ikey168/Noesis/issues/1765) — Acquire NLnet funds and current application calls.
- [ ] [#1766](https://github.com/Ikey168/Noesis/issues/1766) — Acquire EU funding calls and authoritative call documents.
- [ ] [#1767](https://github.com/Ikey168/Noesis/issues/1767) — Acquire German Förderdatenbank programme listings and call references.
- [ ] [#1768](https://github.com/Ikey168/Noesis/issues/1768) — Acquire EXIST programme and application requirements.
- [ ] [#1769](https://github.com/Ikey168/Noesis/issues/1769) — Normalize funding opportunities, revisions and application deadlines.
- [ ] [#1770](https://github.com/Ikey168/Noesis/issues/1770) — Evaluate funding eligibility with cited rules and unknowns.
- [ ] [#1771](https://github.com/Ikey168/Noesis/issues/1771) — Rank funding opportunities by fit, usable support and effort.
- [ ] [#1772](https://github.com/Ikey168/Noesis/issues/1772) — Create funding application workspaces and requirement checklists.
- [ ] [#1773](https://github.com/Ikey168/Noesis/issues/1773) — Draft cited funding applications, milestones and budget plans.
- [ ] [#1774](https://github.com/Ikey168/Noesis/issues/1774) — Monitor new calls, eligibility changes and funding deadlines.
- [ ] [#1775](https://github.com/Ikey168/Noesis/issues/1775) — Compose the Funding & Grants bundle with existing workflow capabilities.
- [ ] [#1776](https://github.com/Ikey168/Noesis/issues/1776) — Add offline funding discovery-to-application acceptance coverage.
- [ ] [#1777](https://github.com/Ikey168/Noesis/issues/1777) — Validate live funding coverage and publish an explained shortlist demo.

Each issue contains acceptance criteria and prerequisite issue references.

## Sources

- [NLnet](https://nlnet.nl/propose/): selected funds, calls and open-project conditions.
- [EU programmes and calls](https://research-and-innovation.ec.europa.eu/funding/funding-opportunities/funding-programmes-and-open-calls_en):
  selected Funding & Tenders calls/topics and authoritative conditions.
- [Förderdatenbank](https://www.foerderdatenbank.de/FDB/DE/Home/home.html):
  selected federal, state and EU programme listings and administering-body links.
- [EXIST](https://exist.de/en/): programme variants, application documents, and
  applicant/institution requirements.

F01 verifies each provider's supported access method and terms before
implementation. These links establish source candidates, not guaranteed APIs or
complete live coverage. Past award records provide context; they do not establish
that an application window is open.

## Composition and reuse

Follow the [pack/workflow architecture](../architecture/pack-workflow-composition.md).
Reuse source execution, Research, Decision Support, Creation, Awareness,
subscriptions, and project/intake state. Keep applicant facts owner-scoped;
shared source records must not contain private profile data.

The composition architecture is proposed. Source contracts, record design and
connector work can advance independently. F14 depends on architecture slices
C02–C07 for resolved bindings, readiness, lifecycle and actual authorized dispatch.
Do not introduce a parallel scheduler, permissions ledger or project store.

Existing `config/extraction_schemas/eu-funding.json` models award metadata.
Audit it for reuse but keep programmes, calls/rounds, applications and awards
distinct.

## Acceptance

- A reproducible profile-to-shortlist journey explains eligibility, missing
  facts, source conditions, usable support, effort, deadlines and next actions.
- Eligibility conclusions pin applicant facts and official call-rule revisions;
  competitive fit is not a probability of winning funding.
- Grants, loans, equity, credits, programme budgets, applicant award sizes and
  co-financing are distinguished. Unknown costs and amounts remain unknown.
- Deadlines preserve original timezone/text, stages, rolling windows and changes.
  Failed refreshes do not imply closure.
- A preparation workspace tracks requirements, evidence, drafts, milestones and
  budgets through existing project/Creation workflows, preserving user edits.
- Changes to profiles or source rules flag affected recommendations and drafts;
  authorized configured alerts are deduplicated and replay-safe.
- Tests cover access isolation/revocation, source conflicts, restarts, and shared
  providers remaining available to other packs.
- Offline fixtures and bounded live checks have separate receipts. A fixture-only
  public recipe call is not evidence that its named tools executed.

Automatic application submission, funder outreach, investor brokerage, and
funding guarantees are outside this scope.
