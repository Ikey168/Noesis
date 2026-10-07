---
name: reference-integrity
description: Pre-submission reference check for a Noesis paper — cite the right version of each work (preprint vs. accepted manuscript vs. version of record) through reviewable paper families, surface retractions, corrections, withdrawals and expressions of concern from retained Crossref notices, preserve cited web pages (policy documents, reports, tool pages) as policy-gated content-addressed snapshots, re-verify that each preserved passage still supports the sentence citing it, and repair rotted links only to approved archive copies. Use before submitting or circulating a paper, after a long drafting gap, or when a reviewer questions a citation. Drives the noesis-knowledge-engine MCP paper-family and citation-preservation tools.
---

# Reference integrity

A citation can be wrong in four ways that the draft itself won't show: it points
at a superseded version, the work has since been retracted or corrected, the web
page has changed or disappeared, or the passage no longer says what the sentence
claims. This skill checks all four for every entry in a **paper-draft** report's
bibliography and feeds the outcome back into the report. Tools are on the
**`noesis-knowledge-engine`** MCP server.

Run it on a pinned report revision (`inspect_authored_report`) so the check and
the fixes refer to the same text.

## 1. Versions: cite the right member of each paper family

Preprints dominate AI-in-education research (arXiv, EdArXiv, SSRN), and many
later appear in a journal with changed numbers. For each scholarly reference:

1. `create_paper_family(namespace, family_key=<DOI or arXiv id>, root_member={
   "document_id": ..., "revision_id": ..., "stage": "preprint"|"accepted-manuscript"|"version-of-record"|"dataset"|"supplement"|"other",
   "identifiers": [{"kind": "doi", "value": "10.…"}]})` — identifier kinds are
   `doi`, `arxiv` or `provider` (e.g. an SSRN or EdArXiv id); `family_key`
   stable, one family per work.
2. `add_paper_family_member(..., member=<same shape>, source_member_id=...,
   relation_type="is-preprint-of"|"has-preprint"|"is-version-of"|"has-version"|…,
   provenance={"kind": "provider", ...} | {"kind": "inference", ...}, expected_revision=...)`.
   A provider-stated link (Crossref/DataCite relation in the captured source)
   is accepted; an inferred link (same title and authors) is only a
   **candidate** until another person reviews it with
   `review_paper_family_relation`. Do not review your own inference.
3. `compare_paper_family_members(namespace, family_id, member_ids)` — where
   versions differ (sample size, effect, conclusion), the paper must cite the
   version whose wording it relies on.
4. `select_paper_family_citation(..., member_id, expected_revision, locator=...)`
   pins the cited member. Prefer the version of record when it is accessible;
   cite a preprint only if no later version exists or the claim depends on the
   preprint's content, and say "preprint" in the reference.

## 2. Lifecycle notices: retractions and corrections

Notices come from the Crossref update-notice collector
(`src.ingestion.crossref_notices.CrossrefNoticeCollection`, bounded by a date
window and a DOI → document map). It has no MCP tool; running it fetches from
Crossref and writes to the store, so it needs the user's or operator's go-ahead.
Once notices are retained:

- `attach_paper_family_notice(namespace, family_id, command_key, notice_id, expected_revision)`
  attaches a `retraction`, `correction`, `withdrawal` or
  `expression_of_concern` to the member it targets, or marks the target
  unresolved; an unresolved target needs an independent
  `review_paper_family_notice_target`.
- `export_paper_family(namespace, family_id)` gives the selected citation and
  its version-specific lifecycle history.

Then act on what you find:

| Notice on the cited member | Action in the paper |
|---|---|
| Retraction / withdrawal | Remove it as support. If the paper discusses it, cite it *as retracted*, with the notice. Re-check every assertion that depended on it. |
| Expression of concern | Keep only with an explicit caveat in the sentence; do not let it carry a conclusion alone. |
| Correction | Check whether the corrected content changes the cited claim; cite the correction too. |

If no notices were collected, the paper may not say references were checked for
retractions — say instead which check was, and was not, run.

## 3. Web sources: preserve, verify, repair

Policy guidance, ministry reports, vendor claims and tool pages change without
notice. For each non-DOI web citation:

1. Once per paper, `register_citation_archive_policy(namespace, policy_id, version,
   allowed_licenses=..., approved_archives=[...], preserve_excerpts=True, max_bytes=...)`.
   Robots-denied and private sources stay excluded unless the user
   explicitly decides otherwise.
2. `capture_citation_snapshot(namespace, policy_id, citation_id, source_url,
   content=..., retrieved_at_ms=..., excerpts=[...], license_id=...)` — a
   content-addressed manifest of what the page said when cited.
3. `verify_preserved_citation(namespace, citation_id, snapshot_id, assertion=<the report sentence>,
   expected_excerpt=...)` → `supports`, `contradicts`, `ambiguous`,
   `unverifiable` or `citation_mismatch`. Only `supports` keeps the citation
   as is.
4. `record_citation_health(namespace, citation_id, url, http_status, paywall=..., takedown=...)`
   for the live URL; `get_citation_status` shows the history.
5. Rotted link: `preview_citation_repair(namespace, policy_id, citation_id,
   snapshot_id, candidates=[archive copies])` then `accept_citation_repair` —
   only for an exact-content copy in an approved archive. The original record
   is kept.
6. `export_preserved_citations(namespace, citation_ids)` — closure for the
   paper's supplementary material.

## 4. Feed the results back

For each problem, revise the report with **paper-draft**
(`revise_authored_report`), or run `assess_authored_report_changes` →
`propose_authored_report_edit` → `decide_authored_report_edit` when the change
comes from an evidence revision. Add a limitations line for anything left
unresolved (unreviewed family candidates, notice check not run, unverifiable
pages).

## Rules

- Never silently swap a citation for a "better" one; every change is a report
  revision with a reason.
- An inferred version link or notice target is a candidate until a different
  person reviews it.
- Report what was checked and what was not, with dates.
