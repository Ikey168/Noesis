# Mathematics expansion scope

Status: proposed, 2026-09-25. This expands the existing Science/Research
capabilities; it does not create a separate domain or pack. Provider contracts
and live integrations have not yet been validated by this scope document.

Tracking: [#1751](https://github.com/Ikey168/Noesis/issues/1751).

## Implementation issues

- [ ] [#1752](https://github.com/Ikey168/Noesis/issues/1752) — Define source access, licensing, identifiers, and update contracts.
- [ ] [#1753](https://github.com/Ikey168/Noesis/issues/1753) — Add bounded scholarly-metadata ingestion for mathematics sources.
- [ ] [#1754](https://github.com/Ikey168/Noesis/issues/1754) — Add OEIS sequence records and literature references to Research.
- [ ] [#1755](https://github.com/Ikey168/Noesis/issues/1755) — Ingest versioned declarations from formal mathematics libraries.
- [ ] [#1756](https://github.com/Ikey168/Noesis/issues/1756) — Normalize mathematical objects and extract theorem-level records.
- [ ] [#1757](https://github.com/Ikey168/Noesis/issues/1757) — Link papers, mathematical objects, and formal declarations with evidence.
- [ ] [#1758](https://github.com/Ikey168/Noesis/issues/1758) — Track formal proof dependencies and library revisions.
- [ ] [#1759](https://github.com/Ikey168/Noesis/issues/1759) — Expose mathematics discovery through existing Research workflows.
- [ ] [#1760](https://github.com/Ikey168/Noesis/issues/1760) — Add offline acceptance and a reproducible demo.

## Implementation status (2026-09-26)

Implemented with offline acceptance; see [the mathematics guide](../guides/mathematics.md).
Formal-library sources (mathlib4, AFP) were validated live against their pinned
commits; zbMATH Open and OEIS live acceptance remains open (hosts unreachable
from the build environment, fixtures authored in the documented shapes).

## Intended use

Follow mathematical results from literature into reusable mathematical
objects and machine-checked declarations. A paper, a text-extracted theorem,
and a formal theorem remain distinct records; connect them only through an
explicit identifier, citation, or reviewed link. Text or expression similarity
can suggest a candidate but does not establish equivalence.

## Candidate sources

- **Literature:** zbMATH Open, arXiv, Crossref, and OpenAlex. Assess MathSciNet
  only for users with appropriate licensed access; it is not a v1 dependency.
- **Sequences:** OEIS records and their supplied references, subject to its
  access and reuse terms.
- **Formal libraries:** Lean/mathlib, Coq, Isabelle, and the Archive of Formal
  Proofs, using bounded snapshots pinned to repository revisions.

The source-contract issue must verify current API/dump availability, access,
reuse terms, rate limits, stable identifiers, and update semantics before
live acquisition. Preserve provider identity and revisions; deduplication must
not erase source records.

## V1 acceptance

- Pinned offline fixtures cover literature metadata, an OEIS sequence, formal
  declarations, dependencies, revisions, and explicit/candidate links.
- Re-ingestion is idempotent and all records and links retain source provenance.
- Formal declarations and dependency edges identify immutable repository
  revisions so queries and exports can be reproduced.
- A demo answers a mathematical research query using papers and relevant
  formal-library declarations, with source records and link evidence visible.
- Offline replay and bounded live provider checks are reported separately.

Defer universal formula OCR, automatic proof translation, unrestricted
repository mirroring, and reliance on licensed MathSciNet access.
