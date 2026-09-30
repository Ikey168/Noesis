# Legal regulatory enforcement guide

Regulatory enforcement actions outside competition law, in the Legal pack
(provider `legal.enforcement`, subdomain `regulatory-enforcement`, tracker
#2651): SEC litigation releases and administrative proceedings, FCA final
notices, EPA ECHO enforcement cases and EDPB Article 60 final decisions. Given a
company, a regulator or a legal basis, the tools return the actions the
regulators published - proceedings, decisions, settlements, penalties and
appeals - each with source, record revision and as-of time.

**Coverage is offline only.** Every provider is `unverified-live`: the parsers
are tested against authored fixtures in the publishers' documented shapes, and
the source audit was written without network access to the publishers. The
dated live run and cited demo belong to #2720. See the
[source audit](../development/enforcement-evidence/source-audit.md).

## What it will not do

- No risk or compliance scoring, and no ranking of companies or regulators.
- No inference of wrongdoing from an initiated action.
- No merging of a settled "neither admit nor deny" outcome into a finding: the
  published admission wording is shown as it is.
- No profiling of named individuals. Individuals are counted, never named,
  matched or monitored (the EN01 minimisation decision).
- No sums of penalties across currencies or authorities, and no conversion.
- No legal advice.

## Enable

Coverage is four independent optional Legal features, all default off:
`enforcement-sec`, `enforcement-fca`, `enforcement-epa` and
`enforcement-edpb`. Select them in the Legal bundle's composition plan. The
sources ship in `legal-research` 1.5.0:

| Source id | Feature | Selection unit |
| --- | --- | --- |
| `sec-enforcement-releases` | `enforcement-sec` | release page path (`litigation-releases/lr-NNNNN`, `administrative-proceedings/34-NNNNN`) |
| `fca-final-notices` | `enforcement-fca` | notice slug (the PDF's text layer; needs the optional `pdfminer.six` for real PDFs) |
| `epa-echo-enforcement-cases` | `enforcement-epa` | ECHO case number |
| `edpb-art60-final-decisions` | `enforcement-edpb` | register entry slug |

Live SEC requests need `NOESIS_SEC_USER_AGENT` (operator name and contact, as
the SEC fair-access policy requires). Without it a live run fails with
`source_unavailable`; requests are never sent anonymously. Units are declared,
never crawled; each run takes at most 20 units per source.

## Journey: company to cited actions

1. Acquire the Corporate Ownership sources and the enforcement sources.
2. `propose_enforcement_identity_matches` offers organisational respondents
   against ownership entities. Published identifiers (CIK, FRN, LEI, company
   number) come first; name matches are marked low evidence. A reviewer
   accepts or rejects each with `review_enforcement_identity_match`, and can
   revert with `revert_enforcement_identity_match`. Unmatched respondents stay
   visible.
3. `link_enforcement_records` links actions to Legal works (cited statutes,
   rules and GDPR articles), competition cases and court dockets by exact
   citation, to SEC EDGAR filers by a published CIK, and to ownership entities
   through accepted matches. A missing provider is reported as
   `provider_unavailable` and a missing target as `unresolved`; neither is
   dropped.
4. `enforcement_actions_for_entity` (`group: true`, optional `as_of`) returns
   actions against the company and its group as of the date. Each carries its
   outcome, settlement wording, penalties, appeals and notices as published,
   the group member and ownership path, and the revisions and identity
   decision it cites. `no_action_on_record` is never a clean bill.
5. `enforcement_actions_by_authority` lists an authority's actions (or a lead
   supervisory authority's), or those citing a legal basis, over a period.
   Penalty figures are grouped by authority and currency and never summed.
   Undisclosed amounts are listed explicitly.
6. `enforcement_action_history` shows the revision chain, including amended
   notices and removals by the source.
7. `export_enforcement_evidence_bundle` cites every assertion with its source,
   record revision and as-of time.
8. `create_enforcement_monitor` watches an entity, an authority, a legal basis
   or one action. `run_enforcement_monitor` reports new actions, decisions,
   penalties and appeals, as well as corrections and removals. Each report cites
   the before and after revisions and names the fields that changed.

## Revisions

Every published change is an immutable revision:

- a corrected notice ("This Final Notice was amended on ...");
- a settled case with its penalties;
- an entry the publisher withdraws (HTTP 404 or 410), recorded as
  `removed_by_source`.

Nothing is deleted. Record-time reads (`known_at_ms`) return what the store
held at that time.

## Evidence

- Offline: `tests/unit/domains/test_enforcement_*.py`. The acceptance journey
  is `test_enforcement_acceptance.py`, which runs with sockets blocked.
- Live: none yet (#2720).
