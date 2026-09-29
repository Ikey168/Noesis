# Public Procurement pack guide

This guide walks the journey from a private supplier profile to an explained
per-lot shortlist, a bid-preparation workspace and monitoring. Design and
module details are in [the subsystem page](../subsystems/procurement.md).

> **Boundary: Noesis never submits a bid and never contacts a buyer,
> contracting authority or procurement portal.** It reads public notices,
> assesses stated facts against cited notice text, and prepares a checklist
> and drafts for you. Submission happens outside Noesis. You can record it as
> `user_reported_submitted`, or as `buyer_confirmed` with the portal's
> receipt reference.

## 1. Enable the bundle and acquire notices

`procurement_bundle_status` shows each provider as `ready`, `fixture-only`,
`unavailable` or `not-implemented`, and keeps offline and live evidence
apart. Once the bundle is composition-managed,
`set_procurement_bundle_enabled` (operator) is a coordinator selection change
with an activation receipt. Disabling it leaves Funding & Grants and the shared
providers working.

Notices are acquired through the source-pack runtime:
`run_source_pack_execution` with `pack_id: "procurement"` and `operation:
"notices"`. Each source first needs its licence accepted. SAM.gov also needs
the `NOESIS_SAM_API_KEY` secret. Every run is bounded by the source's budget
and pinned selection and leaves a receipt and a watermark. A failed source
marks its provider stale. It never closes a notice.

Discovery tools: `list_procurement_notices`, `inspect_procurement_notice`
(per-lot state, deadlines as published, cited requirements, estimated vs
awarded values) and `procurement_notice_history` (which notice caused each
revision and what changed).

## 2. Create a private supplier profile

```text
create_procurement_profile(namespace, request_key, label, kind="supplier")
update_procurement_profile(namespace, profile_id, command_key, expected_revision, set_facts={...})
```

State only what you can stand behind, for example:

- `supplier.cpv_interests`, `supplier.jurisdictions`;
- `supplier.annual_turnover` (`{"amount": "1500000", "currency": "EUR"}`);
- `supplier.certifications`, `supplier.references_count`,
  `supplier.set_aside_statuses`;
- the `exclusion.*` self-declarations (`false` means the ground does not
  apply to you);
- `preferences.min_days_to_deadline` and
  `preferences.min/max_contract_value`.

Anything you leave out stays **unknown** and is named in every assessment.
Only you can read the profile; operators cannot. Withdrawing it makes every
view pinned to it unreadable.

## 3. Eligibility and the explained shortlist

`assess_procurement_eligibility` returns a verdict per lot. Every exclusion
ground and selection criterion is cited with the notice id, procedure revision,
lot, locator and quote, together with the profile facts it used. Missing facts
are `unknown` and unparsed criteria are `unparsed`. Neither is ever a pass.
Thresholds in another currency are unknown, because nothing is converted.

`build_procurement_shortlist` ranks one item per lot by these inputs:

- CPV fit;
- jurisdiction;
- lot value (with currency and VAT basis);
- submission effort;
- days to the published deadline;
- incumbency.

Each input shows its source and reasons. The score is an ordering aid, **not a
probability of winning**. Past awards for the same buyer or CPV branch appear
as `award_context`. They are context only and never evidence that a procedure
is open. `replay_procurement_shortlist` reproduces a stored shortlist from its
pinned revisions.

Award history and identity:

- `procurement_award_history` answers per buyer, supplier, CPV or linked
  entity.
- `procurement_incumbency` explains who holds comparable awards.
- `procurement_party_candidates` proposes LEI or canonical-entity matches.
- `decide_procurement_party_link` and `revert_procurement_party_link` record
  reviewed, reversible decisions (review scopes). Nothing is ever merged
  automatically.

## 4. Prepare a bid

`create_procurement_workspace(shortlist_id, item_id)` starts from one
shortlisted lot. Excluded lots are refused. The workspace is a research project
with a checklist:

- **Requirements:** each one cites its notice passage.
- **Documents to produce, with source and status:** the exclusion
  self-declaration, financial standing, references, certificates, set-aside
  representations and the buyer's procurement documents.
- **Milestones:** the published deadline text. Internal milestones are
  labelled as suggestions.
- **Gaps:** unknown facts and unparsed criteria.

`update_procurement_workspace_items` records your progress, and
`draft_procurement_bid` writes a cited draft that is pinned to the notice
revision. Unanswered items stay explicit. `export_procurement_bid_draft`
re-checks your access and shows whether the notice has changed since.

## 5. Monitor

`create_procurement_monitor(profile_id, providers, watch={"buyers": [...], "cpv": [...]})`
creates a knowledge subscription. `run_procurement_monitor` evaluates it at a
committed procurement source-pack watermark. Refreshes follow the source
pack's schedule. There is no separate scheduler, and replaying a watermark
creates no new events.

Notifications cite the changed notice and revision. They cover:

- new matching notices;
- corrigenda;
- deadline changes, with the old and new published text;
- cancellations;
- awards (context);
- requirement and eligibility changes;
- stale sources.

Stale shortlists and workspaces are listed until you rebuild the shortlist or
call `refresh_procurement_workspace`. A refresh keeps your preparation status
and flags the changed items.

## Per-provider live state

| Provider | Implementation | `LIVE_VERIFICATION` | Last bounded live run (2026-09-27) |
| --- | --- | --- | --- |
| TED (eForms API v3) | native `ted` connector | `unverified-live` | blocked: `source_unavailable` (egress proxy refused CONNECT, 403) |
| UK Find a Tender (OCDS) | native `ocds` connector | `unverified-live` | blocked: `source_unavailable` (egress proxy refused CONNECT, 403) |
| UK Contracts Finder (OCDS) | native `ocds` connector | `unverified-live` | blocked: `source_unavailable` (egress proxy refused CONNECT, 403) |
| SAM.gov | native `sam-gov` connector (API key) | `unverified-live` | blocked at preflight: `credential_missing` (no key configured; no request sent) |
| service.bund.de | not implemented | `not-implemented` | no documented notice API; not scraped |
| Berlin Vergabeplattform | not implemented | `not-implemented` | no documented API or export; not scraped |
| OpenTender | not implemented | `not-implemented` | historical award bulk data only |

## Offline and live evidence

- **Offline evidence:** `tests/unit/domains/test_procurement_acceptance.py`
  runs the whole journey with sockets disabled. It uses authored fixtures
  (`tests/fixtures/procurement/README.md`) through the real runtime and
  adapters, and its receipts show `execution: injected`. The demo
  [`docs/examples/procurement-shortlist-demo.md`](../examples/procurement-shortlist-demo.md)
  is built the same way for a synthetic supplier.
- **Live evidence:** `scripts/procurement_live_check.py` writes
  `docs/development/procurement-evidence/live-check-<date>.json`. The
  2026-09-27 run reached no provider, so no live coverage is claimed. Rerun it
  from a network that can reach the provider hosts, with `NOESIS_SAM_API_KEY`
  set.
