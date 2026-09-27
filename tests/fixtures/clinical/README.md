# Clinical Evidence offline fixtures

**Every file here is authored.** None is a live capture. They were written by
hand in the response shape of each provider so the Clinical Evidence adapters,
the source-pack runtime and the stores can be exercised offline. They are not
evidence about any real trial, medicine or publication.

The intervention *noetiglutide*, its sponsor "Fixture Therapeutics", every
trial number (`NCT0900000x`, EudraCT `2015-900001-10`, EU CT
`2023-509001-12-00`), PMID (`9900000x`), DOI (`10.5555/...`, Crossref's test
prefix), label set id, FDA application (`NDA299001`), EMA product number
(`EMEA/H/C/009001`) and PROSPERO number (`CRD42099000001`) are placeholders. The
openFDA disclaimer text is the provider's published wording. MeSH descriptor
identifiers and tree numbers in `mesh_subset.json` are believed to match MeSH but
were not verified from this environment.

| File | Shape of | Scenario |
| --- | --- | --- |
| `ctgov_NCT09000001.json` | ClinicalTrials.gov API v2 `/api/v2/studies/{NCT}` (protocol, results, derived sections) | completed phase 3 trial with posted results, EudraCT secondary id, declared PMID |
| `ctgov_NCT09000001_history.json`, `_v0..v3.json` | site history endpoint `/api/int/studies/{NCT}/history[/{v}]` (not part of the documented v2 contract) | four registry versions; the primary outcome is re-timed from week 26 to week 52 in version 2 |
| `ctgov_NCT09000001_conflicting_current.json` | v2 study | current record disagreeing with the latest registry version (sponsor) |
| `ctgov_NCT09000002*.json` | v2 study and history | terminated phase 2 trial, results never posted |
| `ctgov_NCT09000003.json`, `ctgov_search_page1/2.json` | v2 study and search pages with `nextPageToken` | recruiting trial with a CTIS secondary id; search pagination |
| `ctis_2023-509001-12-00.json` | CTIS public portal retrieve JSON (undocumented portal endpoint) | member-state decisions, NCT secondary id, enrolment differing from CT.gov |
| `euctr_2015-900001-10.txt` | EU CTR full-text protocol download | two member-state protocols, NCT cross-reference, results available |
| `openfda_label_v7.json`, `openfda_label_v8.json` | openFDA `/drug/label.json` with `meta.disclaimer` | label version 7, then version 8 with a boxed warning; dosing sections present but never retained |
| `openfda_drugsfda.json` | openFDA `/drug/drugsfda.json` | original approval and a labeling supplement |
| `openfda_event_counts.json`, `openfda_event_total.json` | openFDA `/drug/event.json` count and total queries | FAERS reporting counts |
| `ema_medicines.json` | EMA medicines data export (JSON) | authorised product with revision number; an unrelated product filtered out |
| `prospero_CRD42099000001.json` | a PROSPERO record export as a user would supply it | review registration linked to a systematic-review protocol |
| `pubmed_esummary.json`, `pubmed_efetch.xml` | NCBI E-utilities esummary and efetch (MEDLINE XML with `DataBankList`) | trial reports with declared NCT/EudraCT ids; one trial report without any registry id |
| `europepmc_search.json`, `europepmc_annotations.json` | Europe PMC REST search (core) and Annotations API | protocol paper with a text-mined NCT accession |
| `medrxiv_details.json` | medRxiv details API | preprint with the DOI of its journal version |
| `crossref_retraction.json` | Crossref works with `update-to` | retraction notice for the NOETIC-2 journal article |
| `mesh_subset.json`, `term_curations.json` | MeSH descriptor records; reviewer curations | crosswalk with equivalent, incompatible and related mappings |

The source-pack fixtures `tests/fixtures/source_packs/clinical-*.json` hold the
same bodies as `native_pages` (keyed by request path and query) and are pinned
by hash in `config/source_packs/clinical-evidence.json`. When a body changes,
rebuild the native pages and pins together.
