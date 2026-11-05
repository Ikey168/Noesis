# ICIJ Offshore Leaks Database: source audit (#2888)

Audit for the Corporate Ownership bundle's optional `offshore-leaks` feature
(provider `icij-offshore-leaks`). It was written on 2026-11-02 from a runtime
that cannot reach `offshoreleaks.icij.org` or `offshoreleaks-data.icij.org`
(DNS refused by the session's egress policy). ICIJ's pages could not be
fetched directly. Facts below are marked **verified** when a web-search result
quoted them from ICIJ's own pages, and **verify** when they are this adapter's
reading and still need a check against the live page or a downloaded release.
No term has been invented. Nothing was downloaded, and no real leak data is in
the repository.

## 1. Publisher and product

| Item | Finding | Status |
| --- | --- | --- |
| Publisher | International Consortium of Investigative Journalists (ICIJ) | verified |
| Product | ICIJ Offshore Leaks Database. It combines Offshore Leaks (2013), Panama Papers (2016), Bahamas Leaks (2016), Paradise Papers (2017) and Pandora Papers (2021) | verified (download page, via search) |
| Download format | A ZIP archive with one CSV per node type and one CSV for all relationships. Neo4j dumps are also offered | verified (download page, via search) |
| Download page | `https://offshoreleaks.icij.org/pages/database` | verified (URL in search results) |
| Archive URL | `https://offshoreleaks-data.icij.org/offshoreleaks/csv/full-oldb.LATEST.zip` (`src/ingestion/offshore_leaks.py: BULK_URL`) | **verify** |
| Member names | `nodes-entities.csv`, `nodes-officers.csv`, `nodes-intermediaries.csv`, `nodes-addresses.csv`, `nodes-others.csv`, `relationships.csv` | **verify**. An older ICIJ help page describes `nodes.csv` and `edges_1direction.csv` for an earlier layout. The adapter matches members by base name and fails closed (`schema_drift`) when `nodes-entities.csv` or `relationships.csv` is missing |
| Columns | Nodes: `node_id`, `name`, `sourceID`, plus optional columns kept verbatim in `native` (`original_name`, `former_name`, `jurisdiction`, `jurisdiction_description`, `company_type`, `address`, `internal_id`, `incorporation_date`, `inactivation_date`, `struck_off_date`, `dorm_date`, `status`, `service_provider`, `ibcRUC`, `country_codes`, `countries`, `valid_until`, `note`). Relationships: `node_id_start`, `node_id_end`, `rel_type`, `sourceID`, plus optional `link`, `status`, `start_date`, `end_date` | **verify**. Third-party write-ups use `node_id_start`/`node_id_end`; Neo4j guides use `node_1`/`node_2` for an older layout. Only the required columns are enforced; a missing one fails as `schema_drift` |
| Node pages | `https://offshoreleaks.icij.org/nodes/<node_id>` (cited as the record URL) | verified (URL pattern in search results) |
| Cadence | Irregular, when an investigation adds data | **verify** |

## 2. Licence and attribution

| Item | Finding | Status |
| --- | --- | --- |
| Database | Open Database License (ODbL) | verified ("The ICIJ Offshore Leaks Database is licensed under the Open Database License", ICIJ pages via search) |
| Contents | Creative Commons Attribution-ShareAlike | verified (same statement); licence **version: verify** |
| Attribution | Cite the International Consortium of Investigative Journalists | verified (via search) |
| Share-alike | ODbL share-alike applies to a publicly used derived database. Noesis keeps the import local and records the licence on every record. Before publishing derived data, check ODbL section 4.4 | obligation from the ODbL text; **verify** the scope for any publication |
| Machine access | Bulk download, no key. Noesis never scrapes the web application (search pages, node pages) | download offered by ICIJ (verified); terms of use for automated access to the web app: **verify** |

The source pack records the licence as `odbl-1.0-database-cc-by-sa-contents`
with `terms_url` `https://offshoreleaks.icij.org/pages/database`. Every record
carries the licence text in `source.license`.

## 3. ICIJ's stated cautions (data limits)

- "There are legitimate uses for offshore companies and trusts." This sentence
  appears on ICIJ database and investigation pages. **Verified** verbatim via
  search.
- Inclusion in the database is "not intended to suggest or imply" that a
  person or entity broke the law or acted improperly. **Verified** as a phrase
  on a node-page disclaimer. The full sentence is **verify**, so Noesis
  paraphrases it and quotes only the first sentence.
- Many people and entities share the same or similar names, so a name alone
  never identifies anyone. Some information may have changed since the
  documents were created. **Verified** as the substance of ICIJ caveats via
  search; exact wording: verify.
- The data comes from leaked records, not from registries. A node's
  `valid_until` statement gives the period the dataset covers. Noesis keeps it
  verbatim. Nothing is updated after that period.
- The database does not hold bank accounts, transactions or the leaked
  documents themselves. **Verify** the exact list on the about page.

Noesis's caveat (`src/ingestion/offshore_leaks.py: CAVEAT`) quotes the first
sentence, paraphrases the second and third, and travels on every record
(`source.note`). It is repeated in every dossier section and export
(`source_caveats`, the `offshore_leaks` section and report limitations).

## 4. Personal-data assessment

- **Officer and intermediary nodes are often natural persons.** ICIJ does
  not type them. Noesis classifies a node as a legal entity only by a
  configured legal-form token in its name (`LEGAL_FORM_TOKENS`) or an
  explicit `node_kinds` override, and records the basis. Every other node is
  treated as a natural person, because that is the more protective error.
- **Default (person-research mode off).** Person records, and the roles that
  name them, are stored *owner-scoped*: visible to the acquiring principal and
  ownership reviewers, and redacted for everyone else. A dossier shows another
  analyst only that a redacted person link exists. ICIJ's person-to-person
  `same_as`/`similar` links are not offered for identity review. They are
  counted as withheld.
- **Person-research mode (`NOESIS_PERSON_RESEARCH`).** Person records and
  roles are stored namespace-visible under policy
  `offshore-leaks-person-research-v1`, and person links may be offered for
  review. Records under that policy are refused on write while the mode is off
  (`minimisation_violation`), the same rule as
  `docs/security/person-research-mode.md`.
- **Addresses.** A natural person's registered address is never stored, in
  either mode. It is counted as `person_addresses_withheld`. Entity and
  intermediary-firm addresses are kept as published.
- **Lawful basis and retention.** Retention follows the local warehouse's
  policy. Deleting the ICIJ namespace records removes the import. Assessing a
  lawful basis for processing named persons is the operator's job (GDPR
  Art. 6/9, journalism exemptions). It needs a **legal review before any
  person-level use**.

## 5. Mapping decisions

| ICIJ | Corporate Ownership | Notes |
| --- | --- | --- |
| entity node | `legal_entity` (`icij-offshore-leaks:node:<id>`) | `identifiers` `icij-node`. Jurisdiction is set only through `JURISDICTION_NAMES` (from `jurisdiction_description`). ICIJ's code stays in `native`. The incorporation date is parsed only from ISO or `DD-MON-YYYY` form. Inactivation, struck-off and dormancy dates are native only and never become a dissolution date |
| officer / intermediary node | `person` (owner-scoped) or `legal_entity` | classification basis recorded |
| address node | none | text attached to the entity record through `registered_address` |
| `officer_of`, `intermediary_of` | `officer_role` with `role` = ICIJ `link` verbatim | **never** an ownership or control assertion. "shareholder of" carries no percentage and does not enter the parent, control or ultimate-owner graph |
| `same_as` and kin, `probably_same_officer_as` | quoted in `native.icij_links`; offered as `source-stated-link` candidates | reviewer decides; never auto-accepted |
| `similar`, `similar_company_as` | quoted; offered as `similar-name` candidates | can never be accepted |
| other types (`connected_to`, `underlying`, …) | quoted in `native.icij_links` | no identity or ownership meaning |
| registry match | `name-jurisdiction`, `similar-name` (jurisdiction unknown) or `unqualified-identifier` (`ibcRUC` equals a registry number) | candidates only |

## 6. Bounds and exclusions

- A selection is 1-200 seed `node_ids`, or `datasets`/`jurisdictions` with an
  explicit `max_nodes` (at most 1000). It covers at most one relationship hop.
  Truncation is reported. The whole database is never imported.
- The source-pack adapter replays extracts only. A live source-pack run is
  refused (`bulk_release_required`). Live acquisition goes through the
  bulk-release runner (`icij-offshore-leaks`) and `project_bulk_subset`.
- Excluded: wrongdoing, AML, sanctions or risk verdicts (refused by the
  forbidden-key guard), beneficial-ownership determination, scraping of
  the web application, and download of the leaked source documents.

## 7. Live verification

`unverified-live` (`LIVE_VERIFICATION`). No dated live run exists. The archive
URL, member names and columns must be checked against a downloaded release
before the first live run. Record that run as dated evidence in this
directory. Offline fixture coverage is never reported as live coverage.
