# Legal regulatory enforcement

The Legal pack's `legal.enforcement` provider (#2651) answers two questions
with citations:

* given a **company** (an ownership entity) and a date, which enforcement
  actions regulators published against it - and, through accepted ownership
  matches, against its group - with outcomes, penalties and appeal history as
  published by that date;
* given an **authority** or a **legal basis** and a period, which actions were
  published, with outcomes and penalties as published.

Coverage is four optional Legal features, default off and independent:
`enforcement-sec`, `enforcement-fca`, `enforcement-epa` and
`enforcement-edpb`; select them in the Legal composition. Sources, terms and
the minimisation decision: [source audit](../development/enforcement-evidence/source-audit.md).

## Sources (`legal-research` 1.5.0, connector `enforcement`)

| Source | Provider | Delivers |
| --- | --- | --- |
| `sec-litigation-releases` | U.S. SEC | litigation releases: respondents (CIK where stated), charges and legal bases, outcome and admission wording verbatim, sanctions, related court case as a citation |
| `sec-administrative-proceedings` | U.S. SEC | administrative-proceeding releases keyed by release and file number |
| `fca-final-notices` | UK FCA | final notices (PDF) to firms: FRN, rule breaches cited, penalty after and before the settlement discount, Upper Tribunal references |
| `epa-echo-cases` | U.S. EPA ECHO | civil and criminal case reports: statutes and sections, defendants, facilities with FRS ids and published coordinates, penalties, SEP cost, cost recovery, compliance action cost |
| `edpb-art60-decisions` | EDPB | Article 60 register entries: lead and concerned supervisory authorities, GDPR provisions, corrective measures and fines as published |

Every provider is `unverified-live` until a dated live run is recorded
(#2720). Every published update is a new revision; a corrected notice is a
revision marked `corrected`, and a notice the publisher withdraws (404/410) is
a revision marked `removed_by_source` - never a deletion.

## Journeys

1. **Company to actions.** Run the `legal-research` pack, then
   `propose_enforcement_respondent_matches` (published identifiers such as a
   CIK first; names only as low evidence) and have a reviewer accept or reject
   each candidate with `review_enforcement_respondent_match`.
   `enforcement_actions_for_entity(entity=..., ownership_namespace=...,
   as_of=..., group=true)` returns the actions reached through accepted
   matches, the group members and the accepted ownership decisions used, and
   for each action the outcome as published - a settlement "without
   admitting or denying" keeps that wording - with the decisions, penalties
   and appeals dated by the as-of date and every revision cited.
   `no_action_on_record` is explicit and is not a clean bill.
2. **Authority or legal basis over a period.**
   `enforcement_actions_by_authority(authority="uk-fca", date_from=...,
   date_to=...)` or `legal_basis="Article 6 (Lawfulness of processing)"`.
   Penalties are listed per authority and currency with a count and are
   never summed; unpublished figures (an ECHO cost recovery, a fine the
   register does not publish) stay explicit unknowns.
3. **Links.** `link_enforcement_records` links legal bases to Legal works by
   exact citation (GDPR, FSMA 2000, the US Code and named US Acts), cited
   competition cases, court dockets of related cases and appeals, Market
   issuers by published CIK and ownership entities by accepted match. Each
   link records its basis and the citing and target revisions; a missing
   provider is reported as `provider_unavailable`, a missing target as
   `unresolved`.
4. **History and monitors.** `enforcement_action_history` lists every revision
   of an action and its decisions, penalties, appeals and notice documents
   (also as recorded at a `known_at_ms`). `create_enforcement_monitor` watches
   an entity, a published identifier, an authority, a legal basis or one
   action; `run_enforcement_monitor` reports new and revised records, stating
   the changed fields and citing the before and after revisions.
5. **Evidence.** `export_enforcement_evidence_bundle` exports an answer as a
   `noesis-evidence-bundle-v1` citing every revision with its source, record
   revision and as-of time.

## Boundaries

* No risk or compliance scoring, no inference of wrongdoing from an initiated
  action, no merging of settled outcomes into findings and no legal advice.
* Natural persons keep an action-scoped pseudonym and role only; actions naming
  only individuals are not recorded; natural persons are never matched,
  queried or monitored.
* Without the Corporate Ownership store, entity questions answer
  `ownership_unavailable`; `enforcement_actions_for_identifier` still finds
  actions by an identifier the regulator published.
