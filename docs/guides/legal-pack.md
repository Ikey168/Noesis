# Legal pack

Bounded legal research across three jurisdictions:

- **EU:** CELLAR works, language expressions and manifestations.
- **DE:** published federal court decisions.
- **DE-BE:** selected Berlin laws, regulations and judgments.

Answers are source facts with exact locators. They are not legal advice, and
nothing states that an instrument is currently in force. Tracking:
[#1720](https://github.com/Ikey168/Noesis/issues/1720).

## Source audit and coverage

| Provider | Jurisdiction | Access | Identifiers | Formats | Coverage boundary |
| --- | --- | --- | --- | --- | --- |
| CELLAR (`cellar`) | EU | Public SPARQL endpoint `publications.europa.eu/webapi/rdf/sparql`, no credential | CELEX, ELI, ECLI, CELLAR work/expression/manifestation/item URIs | SPARQL JSON | Explicitly selected works only; expressions (languages) and manifestations (formats) stay distinct |
| Rechtsprechung im Internet (`rii`) | DE | Official `rii-toc.xml` index and per-decision ZIP/XML downloads, no credential | `doknr`, docket number, ECLI where published | XML, ZIP (single XML member) | Federal decisions published on the portal, not all German case law |
| gesetze.berlin.de (`berlin-law`) | DE-BE | Documented portal downloads (juris XML ZIP) and rendered judgment pages; no unrestricted API | juris `doknr` (`jlr-…`, `NJRE…`), GVBl. reference | ZIP (juris XML), HTML | Selected official publications; historical versions stay historical; editorial text is excluded from evidence |

**Foundations reused.** The parsers are the reviewed implementations from
#1480 (CELLAR), #1481 (federal decisions) and #1482 (Berlin), in
`src/ingestion/regional_providers.py`. The CELLAR query and result grouping
are now module functions, `cellar_query` and `parse_cellar_results`, shared
by `RegionalClient.cellar` and the pack adapter. The query text is unchanged.

**Gaps that remain:**
- CELLAR relationships and dates come from one bounded query page.
- Consolidated versions are not modelled beyond CELLAR's own works.
- Federal decisions carry cited norms and prior instances as text, and are not
  resolved to norms.
- Berlin gazette (GVBl.) PDFs use the explicit import path
  (`parse_berlin_publication` via `RegionalEvidenceStore.import_bytes`), not a
  runtime connector.

**The political pack's EUR-Lex source is not usable as-is for Legal.**
`config/source_packs/political.json` declares `eu-eurlex-regulatory` as a
declarative REST source pointing at the EUR-Lex web-service help page, with a
required `NOESIS_EURLEX_API_KEY` (the registered EUR-Lex SOAP service).
The Legal pack instead uses the public CELLAR SPARQL endpoint, which needs no
credential. The two have separate readiness.

**Terms.** Official legal texts and decisions are official works (§ 5 UrhG
for German sources; EU reuse under Commission Decision 2011/833/EU). The
source declarations still mark "operator must confirm", because portal
terms, redactions and editorial material apply. Record acceptance per source
(`accept_source_pack_license`) before a live run.

## Install and run

1. Install `config/source_packs/legal.json` (`install_source_pack`) and
   `packs/legal/pack.json`. The built-in domain module `src/domains/legal`
   registers the `legal` domain pack. It is enabled independently and does
   not change the political pack.
2. Accept each source's terms.
3. Choose the selections, all of them bounded:
   - `cellar`: 1–20 CELEX numbers and 1–24 languages, with a page size.
   - `rii`: explicit `doknr` values and/or one index window (`court`,
     modified-`since`, `limit` ≤ 50).
   - `berlin-law`: explicit portal URLs, each with `official_id`, `format`
     (`juris-xml-zip` or `rendered-html`) and `historical` (true, false or
     null for unknown).
4. Run with `run_source_pack_execution` (pack `legal-research`, operation
   `records`). `legal_selection_outcomes` lists per-selection results
   (`returned` or `not_found`). Schema drift, rate limits, service errors and
   oversized payloads fail the source with their own codes.

`legal_readiness` reports each provider and jurisdiction separately. The
pack's own live acceptance stays `outstanding`. Prior adapter live evidence
is linked, but it is not the pack's acceptance.

## Model

- **Work:** a legal work or decision in one jurisdiction, keyed by source-native
  identity, with CELEX/ELI/ECLI, docket and gazette identifiers as supplied.
- **Expression:** one language. Translations are separate expressions and are
  never assumed to be legally identical.
- **Version:** one manifestation or captured text. Changed text is a new
  version, and earlier versions are kept.
- **Passages:** the official text with its XML-path or paragraph locator.
- **Citations:** explicit source relationships only (cites, amends, corrects,
  cited norms, prior instances). A citation target resolves to a known work
  only by exact native identity.
- **Dates:** enactment, publication, entry into force, commencement, validity
  end and decision dates. Each is stored as a legal fact and as a bitemporal
  assertion in `kb_temporal_assertions` (valid time from the source,
  observation time from acquisition).
- **Procedure links:** `link_legal_dossier` connects a political legislative
  dossier to an enacted work, citing evidence. Procedure is context, not the
  authoritative text.

## Research workflows

| Intent | Tool | Notes |
| --- | --- | --- |
| Find a work | `lookup_legal_work` | Identifiers match exactly. Title and citation lookups need a jurisdiction and never return another jurisdiction's hits; they report `not_found_in_jurisdiction` and name the jurisdictions that do match. |
| Choose a version for a date | `select_legal_version_as_of` | Returns `selected` only for one applying expression. Missing commencement is `unknown`; conflicting dates are `ambiguous`. Formats of one expression are `equivalent_manifestations`. |
| Quote a passage | `get_legal_passages` | Exact locator fragment or substring inside one version. Metadata-only versions say so. |
| Compare versions | `compare_legal_versions` | Passage changes by locator. It is a source change, not a legal-effect assessment, and cross-language comparisons are flagged. |

Every response that selects or compares versions carries the review
boundary: legal effect and current force need human legal review.

## Retrieval modes

Only deterministic modes are enabled (`legal_retrieval_modes`,
`config/legal/retrieval_modes.json`):

- `exact-identifier`
- `exact-locator-or-substring` inside one chosen version

Lexical, semantic, hybrid and reranked legal retrieval are **deferred**. The
existing runs (`legal-reference-retrieval-2026-09-09.json`) used
publisher-identifier and exact-membership labels, and those cannot establish
legal relevance.

To enable a mode, run a new evaluation with `src/kb/legal_retrieval.py`. The
module validates and scores; it does not create judgments.

1. **Query set.** Build a source-versioned query set with a stratum for each
   query (jurisdiction, language, source version) and a split (`held-out` or
   `development`).
2. **Judgments.** At least two independent human assessors judge the candidate
   passages under a written protocol version. The scale is 0 not relevant,
   1 related, 2 supporting passage. Every disagreement is adjudicated and
   recorded. Labels with origin model-generated, exact-term membership,
   publisher identifier or synthetic are rejected.
3. **Runs.** Run each candidate mode and the two baselines (`exact-identifier`,
   `keyword`) on the held-out split. Record rankings, latency and resources.
4. **Evaluate.** `evaluate(...)` reports recall@10, nDCG@10, MRR,
   supported-passage precision@5, overall and per stratum, with assessor
   agreement (Cohen's kappa).
5. **Decide.** `decide(...)` adopts a mode only if it passes every threshold
   in the config: at least 20 held-out queries per stratum, kappa ≥ 0.6,
   recall@10 ≥ 0.8, nDCG@10 ≥ 0.6, supported precision@5 ≥ 0.7, a +0.05 nDCG
   gain over keyword, and p95 latency ≤ 1.5 s. Record the result in the config
   with the evaluation receipt.

## Offline fixtures

- `legal-cellar-*.json`: the SPARQL bindings captured live on 2026-09-09, one
  100-row page per query. Runs are bounded to that page, so an incremental
  run resumes at offset 100.
- `legal-rii.json`: decision XML rebuilt from the parsed 2026-09-08 live
  captures, with every scalar field and every paragraph at its original XML
  path. The original byte hashes are kept, and the index page and one 404 are
  authored.
- `legal-berlin.json`: an authored fictional Berlin law (historical and
  current versions) and judgment page in the documented formats. Real Berlin
  captures are recorded by hash in `berlin-native-2026-09-09.json`.

## Live acceptance

```bash
python scripts/legal_live_check.py --output docs/development/legal-evidence/live-check-<date>.json
```

The script runs the installed selections live and records dates,
identifiers, versions, counts, hashes, latencies and failures, never secrets.
Source acquisition acceptance is separate from retrieval relevance
acceptance.

## Exclusions

- Complete EU, German or Berlin law, and all German case law.
- Legal advice and inferred current force.
- Relevance-ranked retrieval without an accepted human evaluation.
- Other jurisdictions and undocumented provider APIs.
