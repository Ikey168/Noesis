# Mathematics expansion scope

Status: implemented offline, 2026-09-26, as a Science/Research expansion (no separate pack). Bounded live acceptance is tracked in #1786.

## Delivery state (audited 2026-09-26)

- Shipped: #1752–#1760 are closed; the code is `src/kb/mathematics.py` (projector `noesis-math-record-v1` in `research-discovery`). Bounded live acceptance for zbMATH Open and OEIS is tracked in #1786.
- Composition dependency: record owner `science.mathematics` (`packs/science/providers/science.mathematics.json`), bound by the science bundle. The remaining live acceptance (#1786) needs none.

Tracking: [#1751](https://github.com/Ikey168/Noesis/issues/1751).

## Implementation issues

- [x] [#1752](https://github.com/Ikey168/Noesis/issues/1752) — Define source access, licensing, identifiers, and update contracts.
- [x] [#1753](https://github.com/Ikey168/Noesis/issues/1753) — Add bounded scholarly-metadata ingestion for mathematics sources.
- [x] [#1754](https://github.com/Ikey168/Noesis/issues/1754) — Add OEIS sequence records and literature references to Research.
- [x] [#1755](https://github.com/Ikey168/Noesis/issues/1755) — Ingest versioned declarations from formal mathematics libraries.
- [x] [#1756](https://github.com/Ikey168/Noesis/issues/1756) — Normalize mathematical objects and extract theorem-level records.
- [x] [#1757](https://github.com/Ikey168/Noesis/issues/1757) — Link papers, mathematical objects, and formal declarations with evidence.
- [x] [#1758](https://github.com/Ikey168/Noesis/issues/1758) — Track formal proof dependencies and library revisions.
- [x] [#1759](https://github.com/Ikey168/Noesis/issues/1759) — Expose mathematics discovery through existing Research workflows.
- [x] [#1760](https://github.com/Ikey168/Noesis/issues/1760) — Add offline acceptance and a reproducible demo.

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
