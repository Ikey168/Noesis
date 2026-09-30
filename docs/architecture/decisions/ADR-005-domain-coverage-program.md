# ADR-005: Subdomains and the domain coverage program

Status: accepted, 2026-09-30. Supersedes the "empty cells are not a to-do
list" clause of [ADR-004](ADR-004-pack-taxonomy.md)'s admission rule. The rest
of ADR-004 stands. Plan:
[domain coverage program](../../roadmaps/domain-coverage-program.md).

## Context

ADR-004 closed the taxonomy at nine domains and treated an unused cell as no
reason to build. The project now wants every domain completely covered.
"Completely covered" has no end point unless it is defined. The nine domains
are too coarse to show what is missing: Health has four providers, and nothing
says whether that is complete.

## Decision

1. **Subdomains define coverage.** Each domain lists its subdomains in
   `packs/taxonomy.json`, giving 89 in total. The list is drawn from the
   subjects official statistics and government functions distinguish (UN
   classification of statistical activities, COFOG). A domain is completely
   covered when every one of its subdomains is covered.
2. **Coverage is computed.** Every classified provider names at least one
   subdomain of its effective domain. A subdomain named by no provider is a
   gap.
3. **Two levels, reported separately.**
   - **Offline coverage:** a fixture-tested provider names the subdomain.
   - **Live coverage:** that provider has passed bounded live acceptance.

   Offline coverage is never reported as live coverage.
4. **Gaps are the plan.** The gap table in the coverage program must equal the
   computed gaps, and the gate test enforces it. A track that covers a gap
   removes its row in the same change.
5. **The subdomain list is closed like the domains.** Adding or removing a
   subdomain needs a decision record. Otherwise "complete" could be reached by
   deleting gaps, or pushed away by adding them.
6. **Homes follow the admission rule.** A gap becomes a provider in an
   existing bundle of its domain whenever one fits. A new bundle is allowed
   only where the domain has none suited to the gap. Two are proposed:
   `society` and `culture`.

## Consequences

- Today 56 of 89 subdomains are covered offline, and 33 are gaps, scheduled
  in three waves.
- Each track needs a source audit before code, as the existing tracks do.
  Several candidate sources carry registration or non-commercial terms. Those
  tracks may end with a documented "not implemented" provider contract rather
  than data, and the subdomain then stays a gap.
- The live-validation backlog grows by one issue per track. Complete offline
  coverage does not mean the domains are live.
