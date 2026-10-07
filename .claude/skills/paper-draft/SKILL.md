---
name: paper-draft
description: Draft, cite, revise and export an academic paper as a Noesis authored report — sections made of assertions that are explicitly sourced (with evidence dependencies pinned to document revisions and character spans) or labelled author commentary, a bibliography whose ids are Zotero citation keys, a pinned research snapshot, and stated limitations. Covers Zotero sync and CSL JSON/BibTeX export with citation-closure checks, Pandoc DOCX/HTML rendering, producing a venue-formatted manuscript from the report, and keeping the paper current when cited evidence changes. Use when writing or revising the manuscript itself; set the project up first with paper-project. Drives the noesis-knowledge-engine MCP authored-report and Zotero tools.
---

# Drafting the paper

The manuscript's authority is an **authored report** (contract
`noesis-authored-report-v1`). Every sentence-level claim is an *assertion*
with a stable id, so each one stays traceable to the evidence it rests on and
can be re-checked when that evidence changes. Tools are on the
**`noesis-knowledge-engine`** MCP server.

## 1. Bibliography first (Zotero)

If the user keeps references in Zotero:

1. `sync_zotero_library(namespace, library_id, library_type="user"|"group", mode="web"|"local", credential_env="NOESIS_ZOTERO_<NAME>")`
   — read-only, incremental; never writes back. A Web API key is referenced
   by an environment-variable name, never pasted. `mode="local"` reads the
   desktop app's local API and takes no key.
2. `list_zotero_items(...)` / `inspect_zotero_item(...)` to pick the items cited.
3. `export_zotero_bibliography(namespace, library, item_keys, item_versions=...)`
   returns `csl_json`, `bibtex` and stable **citation keys**. Use those keys as
   the report's bibliography ids so the closure check in step 5 works.

Without Zotero, use stable ids of your own (e.g. `doi:10.1234/abcd`) and keep
the CSL JSON items for rendering yourself.

## 2. Structure the content

`create_authored_report(namespace, request_key, content)`; `content` has exactly
these keys:

```
{
 "title": "Generative AI Tutors and Learning Outcomes: A Systematic Review",
 "snapshot": {"id": "<research snapshot token or id>",
              "generations": {"paper-ai-education-2026": 42}},
 "sections": [
  {"id": "sec-results", "title": "Results", "assertions": [
    {"id": "a-results-01",
     "kind": "sourced",
     "text": "Of the <M> included studies, <N> were randomised controlled trials.",
     "citations": [],
     "dependencies": [{"kind": "artifact", "id": "<systematic review export sha256>",
                       "revision": "1", "namespace": "paper-ai-education-2026", "locator": {}}]},
    {"id": "a-results-02",
     "kind": "sourced",
     "text": "<What the cited study reports, within its own population, design and outcome.>",
     "citations": ["<citation key>"],
     "dependencies": [{"kind": "source", "id": "<document id>", "revision": "<revision id>",
                       "namespace": "paper-ai-education-2026",
                       "locator": {"document_id": "<document id>", "revision_id": "<revision id>",
                                   "start": 18234, "end": 18511, "page": 7}}]},
    {"id": "a-results-03", "kind": "commentary",
     "text": "<The authors' interpretation, e.g. what the pattern of studies does and does not allow one to conclude.>",
     "citations": [], "dependencies": []}]}],
 "bibliography": [{"id": "<citation key>", "text": "Author, A. (2025). Title. Journal, 1(2), 3-4."}],
 "limitations": ["ERIC was searched outside Noesis on 2026-09-30; those hits carry no Noesis provenance."]
}
```

- `kind` is `sourced` or `commentary`. A sourced assertion **must** have at
  least one dependency (`claim`, `calculation`, `source`, `entity`, `artifact`);
  locators take only `document_id`, `revision_id`, `start`, `end`, `page`,
  `section`.
- Citations must be ids in `bibliography`. Section and assertion ids are unique
  and stable — keep them across revisions; do not renumber.
- Interpretation, synthesis and argument are `commentary`. Labelling them so is
  the point, not a weakness: readers can tell what the evidence says from what
  the authors conclude.
- Numbers you computed (pooled effect, proportions, counts) are `calculation`
  dependencies that point at the inputs; statistics come from the education
  tools with their vintage (see **ai-education-evidence**).

A typical paper's sections: Introduction · Background · Methods (search,
eligibility, screening, extraction; for an empirical paper data and analysis)
· Results · Discussion · Limitations · Conclusion. The Methods assertions
depend on the review protocol and export, not on recollection.

## 3. Check support before calling a draft done

Exports label each sourced assertion "support not independently verified" —
because a dependency only says where the support should be. For every sourced
assertion, open the cited span and confirm it says what the sentence says
(scope, population, direction, size). Use the claim evidence and fact-check
endpoints (`/api/v1/arguments/claims/*`) as a second check. Downgrade, rewrite,
or drop assertions the span does not carry; never stretch a citation.

## 4. Revise

`revise_authored_report(namespace, report_id, expected_revision, content)` —
`expected_revision` must equal the current revision (`inspect_authored_report`);
a conflict means someone else changed it: inspect, merge, retry. Earlier
revisions stay readable with `inspect_authored_report(..., revision=n)`.

## 5. Export

Run **reference-integrity** first; figures and tables come from
**paper-figures**.

- **Citation closure:** `export_zotero_bibliography(..., report_id=<id>)` fails
  with `citation_closure_failed` if the report cites a key the export lacks.
- **Native:** `export_authored_report(namespace, report_id)` → Markdown, the
  full report state and a SHA-256 (`noesis-report-export-v1`). Keep this
  package; `reopen_authored_report` restores it after verifying the hash.
- **Rendered:** `export_authored_report(..., output_format="docx"|"html",
  references=<csl_json items>, locale="en-US"|"en-GB"|"de-DE")` runs Pandoc with
  CSL citations. The default locale is `de-DE`; set it. The rendering is an
  **evidence-audit draft**: every paragraph carries its sourced/commentary
  label.
- **Submission manuscript:** produce the venue-formatted text (Word template,
  LaTeX with the exported `bibtex`) from the report's sections, joining
  assertions into prose without changing their meaning. Keep the report as the
  authority: wording changes made for submission go back in with
  `revise_authored_report`, so the traceable version and the submitted one do
  not drift apart.

## 6. Keep it current

When cited documents are revised or claims change:
`assess_authored_report_changes(namespace, report_id)` lists affected
assertions → `propose_authored_report_edit(...)` per assertion →
`decide_authored_report_edit(..., decision, rationale)`. Accepting preserves
history.

## Rules

- Never invent a citation, DOI, page or quotation. If a reference is not in
  Zotero or Noesis, it is not in the paper.
- No assertion is `sourced` without a dependency the evidence layer holds.
- Do not submit, publish, or write to the user's Zotero; export only.
- Limitations listed by the review export, the coverage report, and
  unverified-live statistics providers go into `limitations`.
