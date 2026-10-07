# ADR-006: Source depth

Status: accepted, 2026-10-07. Extends [ADR-005](ADR-005-domain-coverage-program.md);
nothing in it is superseded. Plan: [source depth program](../../roadmaps/source-depth-program.md).
Overlay: [`packs/depth.json`](../../../packs/depth.json), enforced by
`tests/unit/composition/test_source_depth.py`.

## Context

ADR-005 makes coverage binary: a subdomain is covered when one provider names it.
A covered subdomain backed by a single publisher still cannot do what the
evidence contract promises. An answer can only repeat that publisher. It cannot
show a second figure, a disagreement or a corroboration from another origin.

Counting providers is a poor proxy for this. 54 of the 72 covered subdomains
have one provider, but `agrifood.core` alone reads FAO, USDA, Eurostat and the
European Commission. Counting the publishers behind each subdomain shows only 4
of the 72 resting on one publisher. Coverage gave no way to see that difference.

## Decision

1. **Depth is computed.** A covered subdomain's depth is the number of distinct
   publishers behind the providers that name it. `packs/depth.json` names, per
   classified provider, the source-pack sources it acquires. A provider that
   reads a publisher outside source packs names it with a file that quotes it.
   An analytic provider names the providers it derives from. A provider with no
   publisher yet gives the reason. The test resolves every source to a
   publisher and computes depth from those lists. Nobody enters depth by hand.
2. **A publisher is an organisation.** The registry in `packs/depth.json` maps
   each source's `publisher` string to one organisation. Services of one
   organisation count once: USDA's NASS and FAS, NLM with NCBI, OCHA's ReliefWeb
   and HDX. A republisher whose own publisher string names the origin is
   `derived_from` that origin and adds no depth. Examples are Open-Meteo for DWD
   and ECMWF, WITS for UN Statistics Division tables, and Wiktextract for
   Wiktionary. A source whose string names a distributor rather than the origin
   is bound to the origin explicitly. AccessGUDID is bound to FDA, and FAA
   airworthiness directives published in the Federal Register are bound to FAA.
3. **Every source resolves.** Each source in every source pack must resolve to
   exactly one publisher, and every registry entry must be in use. A new source
   therefore arrives with its publisher recorded, and its effect on depth is
   computed in the same change.
4. **The minimum depth is two.** A covered subdomain below it is **thin**. The
   thin table in the program must equal the computed thin subdomains, and the
   gate test enforces it, as ADR-005's gate does for gaps. A track that brings a
   subdomain to depth two removes its row in the same change. A wave 3 track
   that covers a gap with one publisher adds a thin row in its own change.
5. **Depth is necessary, not sufficient.** Two publishers can share an origin
   chain. Eurostat publishes figures that national statistics offices
   transmit. An aggregator can republish a national aggregator's records.
   Record-level independence stays with the
   [Evidence Independence Graph](../../../contracts/noesis-evidence-independence-v1.md).
   Depth never raises a corroboration count. Where independence is doubtful at
   depth two, the program says so.
6. **Offline and live depth are reported separately.** Depth counts
   fixture-tested sources in a source pack. Live depth counts only sources whose
   track has passed live validation, and it is never reported from offline
   results.
7. **A depth track follows the existing track pattern.** A source audit records
   access, licence and redistribution terms before any code. Where a
   provider's record shape fits, the second publisher joins that provider. A
   new record shape needs a decision record first. A source whose terms do not
   allow the intended use is recorded as not implemented, and the subdomain
   stays thin.

## Consequences

- Of the 72 covered subdomains, 4 are thin (wave 4), 8 sit at depth two and 60
  at three or more.
- Depth two often means one publisher per jurisdiction: one for the EU, one
  for the US. A question about a single place then still sees one publisher.
  Measuring depth per place is a separate decision and is not made here.
- The registry merges and binds publishers by judgement, and those judgements
  can be reviewed. Changing one changes computed depth and fails the gate until
  the thin table agrees.
- The subdomain list stays closed (ADR-005). Depth adds a measure to it; it
  adds no subdomain.
