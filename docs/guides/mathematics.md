# Mathematics in Research

Mathematics extends the existing Science/Research capability
([#1751](https://github.com/Ikey168/Noesis/issues/1751)); it is not a separate
domain or pack. The sources live in the `research-discovery` source pack
(`config/source_packs/research.json`). Their records are projected by
`src/kb/mathematics.py`, and the MCP tools are in
`tools/knowledge_engine_mcp/mathematics.py`.

The design follows one rule. A paper, a text-extracted theorem and a formal
theorem stay distinct records. They are connected only by an explicit
identifier or citation from a source, or by a candidate link that a person
reviews. Similar names or statements can suggest a link but never establish
equivalence.

## Sources (#1752, #1753, #1754, #1755)

| Source id | Connector | What is read | Bounded by | Live status |
|---|---|---|---|---|
| `zbmath-open` | `zbmath` | zbMATH Open API v1 `/document/{id}`: authors, MSC classes, Zbl/DOI/arXiv identifiers, references | 1–50 pinned document ids | `unverified-live` (host unreachable from the build environment) |
| `oeis-sequences` | `oeis` | `search?q=id:A……&fmt=json`: terms (≤200), name, references, links, keywords, revision/time | 1–50 pinned A-numbers | `unverified-live` (host unreachable) |
| `mathlib4-fib` | `formal-library` | `Mathlib/Data/Nat/Fib/{Basic,Zeckendorf}.lean` at commit `b2bf0519…` | a full 40-hex commit and 1–50 paths | validated live, fixture byte-identical |
| `afp-zeckendorf` | `formal-library` | `thys/Zeckendorf/Zeckendorf.thy` (AFP mirror) at commit `5818ae44…` | a full 40-hex commit and 1–50 paths | validated live, fixture byte-identical |

Provider contracts (`math_source_contracts`) record access, terms,
identifiers and update semantics:

- **arXiv, Crossref and OpenAlex** are reused from the existing research
  sources, so no second route is added.
- **MathSciNet** is optional licensed access and is not a v1 dependency.
- **Coq** is supported by the scanner (tested) but has no pack source yet.

Provider records never collapse. Each observed revision is kept, together
with its provider id and identifiers.

## Formal declarations, dependencies and revisions (#1755, #1758)

The formal-library connector reads only the selected files at an immutable
commit. It scans them lexically for Lean 4, Isabelle and Coq, collecting:

- declaration name (namespace-qualified in Lean, theory-qualified in Isabelle)
- kind, file and line span
- statement text and its hash
- doc comment
- module imports

The scan is not elaboration:

- **`module-import`** edges are explicit, taken from the import lines.
- **`lexical-reference`** edges come from tokens in a statement. They are
  labelled as lexical and are not proof dependencies.

Every declaration query requires the commit. A bare branch name is rejected
with `revision_required`, and results include a GitHub permalink to the exact
lines.

`compare_formal_snapshots` diffs two commits and reports:

- declarations added, removed or changed (by statement hash)
- rename candidates, found by statement-token similarity within a module
- import changes

For example, between mathlib v4.18.0 (`aa936c36`) and `b2bf0519` it reports
renames such as `Nat.fast_fib_aux_eq` → `Nat.fastFibAux_eq`.

`export_formal_references` produces a hashed bundle pinned to one commit, so
a proof reference can be reproduced.

## Objects and notation (#1756)

`record_math_object` stores expressions, definitions, theorem statements,
proofs and notation aliases. Each object keeps its exact source span, the
extraction method and a confidence. `extract_math_text_objects` finds
candidates in literature text:

- `$…$` expressions, with confidence 0.9
- named results such as "X's theorem", with confidence 0.6

The normalized form maps Isabelle symbols and common LaTeX macros to Unicode,
using NFC so `ℕ` stays `ℕ`. It is used only for search. It is never a claim of
equivalence.

## Links (#1757)

`propose_math_links` records two kinds of link.

**Explicit links** come from source data and cannot be reviewed:

- a literature reference carrying a zbMATH id or DOI that matches another
  record
- an OEIS reference or link carrying such an identifier
- a DOI or arXiv id in a formal declaration's doc comment

**Candidate links** need review:

- `candidate-name`: distinctive title or sequence-name words appear in a
  declaration name. Results are capped per library.
- `candidate-statement`: theorems in different libraries share distinctive
  name tokens and statement tokens. Tokens that are common across the corpus,
  such as `fib`, are ignored.
- `candidate-expression`: expression objects that are equal after
  normalization.

`review_math_link` accepts or rejects a candidate. An accepted candidate stays
a reviewed candidate, and every link reports `asserts_identity: false`.

## Research queries (#1759)

`search_mathematics` searches papers (by title, author or MSC class),
sequences (by A-number, name or a comma-separated run of terms) and the
latest formal snapshot of each library. The limit applies per kind. Every
result carries:

- its source record
- its revision (the commit for formal results)
- an access note (terms or licence)
- its links with basis, state and evidence

The Science pack (`packs/science/pack.json`, v1.2.0) lists the mathematics
vocabulary, capabilities and query examples.

## Acceptance and demo (#1760)

- **Offline**: `tests/unit/domains/test_mathematics.py` covers:
  - pinned fixtures and conformance
  - bounded adapters
  - the Lean, Isabelle and Coq scanners
  - idempotent re-ingestion
  - pinned declarations and dependencies
  - revision comparison
  - objects and spans
  - explicit versus candidate links and review
  - search
  - the demo
- **Demo**: `python scripts/mathematics_demo.py` replays the fixtures and
  answers "Zeckendorf representation" with papers, sequences and declarations
  from both libraries, showing the link evidence. Its `answer_sha256` is
  stable across runs.
- **Live**: `python scripts/mathematics_live_check.py --output …` runs the four
  sources live. The build-environment run is in
  `docs/development/mathematics-evidence/live-check-build-environment.json`:
  - `mathlib4-fib` and `afp-zeckendorf` completed, and their captured
    fixtures are byte-identical to the live files.
  - `zbmath-open` and `oeis-sequences` failed with `source_unavailable`
    because the egress policy blocks those hosts.

The zbMATH and OEIS fixtures are authored in the documented response shapes.
Their bibliographic facts and sequence terms are real, but their zbMATH ids,
Zbl numbers, DOIs (the `10.5555` test prefix) and revisions are placeholders.
Live zbMATH and OEIS acceptance remains open until it runs with access to
those hosts.
