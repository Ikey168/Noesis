# Scholarly paper families

Paper families group exact captured scholarly revisions for navigation. They retain each document identity and source revision. A family link records its relation type and provenance; a similar title or shared keyword creates only a candidate. A reviewer with `knowledge:paper-family:review` can accept or reject a candidate. An accepted link records the reviewer and rationale.

The scientific source pack at `config/source_packs/scientific.json` collects arXiv preprints and DataCite records. DataCite `relatedIdentifiers` become `related_resources_json` on the captured document revision. A provider relation can be accepted when the exact link is present in that revision and resolves to one member without an identifier collision. The research pack also includes Crossref metadata. `CrossrefNoticeCollection` retains correction, withdrawal, retraction and concern notices; attach a notice by its retained `notice_id`.

Use these MCP calls after acquiring source documents. Replace the IDs with committed document and revision IDs from the ingestion receipt:

```json
{"tool":"create_paper_family","arguments":{"namespace":"research","family_key":"contribution-2025-001","root_member":{"document_id":"preprint-doc","revision_id":"revision:preprint","stage":"preprint","identifiers":[{"kind":"arxiv","value":"2501.00001v2"},{"kind":"doi","value":"10.1234/preprint"}]}}}
```

Then call `add_paper_family_member` with `family_id`, `expected_revision`, the publication's exact `document_id` and `revision_id`, `source_member_id`, `relation_type: "is-preprint-of"`, and `provenance: {"kind":"provider","relation":<the captured related_resources_json entry>}`. An inferred relationship uses `{"kind":"inference","reason":"..."}` and remains a candidate until `review_paper_family_relation` records a separate review. `correct_paper_family_relation` supersedes a mistaken relation with a new provider link or review candidate. `remove_paper_family_member` reverses a mistaken assignment in a new family revision; prior versions remain inspectable.

Call `attach_paper_family_notice` with a retained Crossref notice ID. The notice targets only the member identified by its DOI and, where available, document binding. Ambiguous or unavailable targets remain unresolved with the pinned notice locator. `review_paper_family_notice_target` records a reviewed target for an unresolved notice when the DOI matches. The exported lifecycle summary lists notices per member and keeps unresolved notices separate.

Call `select_paper_family_citation` with an active `member_id`, exact locator, and current family revision. This appends a citation selection with the member's source revision, identifiers, availability and notice IDs. `export_paper_family` returns bibliography entries that can be copied into an authored report's bibliography with the selection ID as the stable `id` and its `text` as authored text. Add the selected source revision to the report assertion's evidence dependency. A later family revision never rewrites an existing report citation.

`inspect_paper_family` pages up to 100 members. `compare_paper_family_members` compares two to ten members and shows full text, abstract-only, metadata-only or unknown representation. All reads check current family and source access. Loss of source access or a reclaimed source fails explicitly. These calls use retained fixtures in tests; they do not verify bibliographic truth, perform live provider requests, or supply an independent human judgment.
