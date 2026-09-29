# Linguistics guide

Tracking issue: [#2178](https://github.com/Ikey168/Noesis/issues/2178). The Linguistics pack takes a word, a
lexeme, a language or a typological feature and assembles what open lexical and typological resources publish
about it. Every definition, gloss and etymology is attributed to its source and revision. Nothing
machine-translated is presented as sourced. Sources that disagree stay side by side, and no consensus is
synthesised.

Source audit and licence decisions:
[`docs/development/linguistics-evidence/source-audit.md`](../development/linguistics-evidence/source-audit.md).

## What it answers

| Question | Tool | Answer |
| --- | --- | --- |
| What does this word mean according to each source? | `lookup_lexeme` | Every source's lexeme for the NFC, case-folded lemma. Each one comes with its forms and paradigm (grouped by grammatical features), senses, definitions as published, usage examples and Leipzig-validated glosses, identity candidates and citations. |
| What did this sense mean on a date? | `sense_history` / `definition_changes` | The definition revision in force per source, by the source's own revision date, and every revision in source order. |
| Where does this word come from? | `lexeme_etymology` | A chain of cited assertions with the source's native relation value beside the normalised one. Disputed etymologies are shown side by side. Proto-forms and unacquired words stay cited text. DOI references resolve to Science literature records. |
| Which language is this? What is its typology? | `languoid_profile` / `typological_profile` | Glottocode, ISO 639-3, level, classification path and its history across releases, location with the Geospatial review state, macrolanguage and retirement context, and WALS values with references. A feature without a value is "no value on record". |
| Which words in other languages are linked? | `lexeme_cross_language_links` | Wiktionary translation-table rows and senses that state the same Wikidata item, each cited. These are links, never identity claims. |
| Can I export this? | `export_lexeme_dossier` | Refused when CC BY-SA content would go out under another licence, or when attribution would be dropped. Share-alike content can instead be excluded and listed. |

Every answer carries a `licences` block with the attributions of the records it cites. It sets
`share_alike: true` when Wiktionary content is included, and it lists `unknowns` instead of leaving them out.

## Sources and releases

The source pack `linguistics-lexical-typological` (`config/source_packs/linguistics.json`) runs through the shared
source-pack runtime with the native connector `linguistics` (`src/ingestion/linguistics_sources.py`). Its sources
are:

- Wikidata lexemes and language items (`Special:EntityData`, CC0);
- kaikki.org Wiktextract per-language JSONL (CC BY-SA 4.0, share-alike). Speaker recordings are dropped;
- Glottolog and WALS CLDF releases (CC BY 4.0);
- Unicode CLDR;
- the SIL ISO 639-3 code, macrolanguage and retirement tables.

Selections are bounded and declared. Rows outside them are counted in the page receipts.

All sources are `unverified-live` until a dated live run is recorded under
`docs/development/linguistics-evidence/`. The declared selection in the source pack is currently the synthetic
offline fixture selection. The production bounds from the audit replace it at live validation (#2191).

## Records, revisions and "current"

`src/kb/linguistics_store.py` stores every acquired record as a **sighting**: record key, source revision,
normalised content hash, the source's revision date and the observation time. Revisions are derived from the
sightings in source order:

- re-acquiring unchanged data adds nothing;
- a changed definition appends a revision, and a change back is a new revision;
- a late older extract becomes history and never becomes current;
- revision ids never change when older data arrives.

"Current" follows the source's own revision date, then its revision id, then observation order. As-of queries
use the revision date. An optional acquisition cutoff limits them to what had been acquired by then.

Text is stored exactly as published, with an NFC form beside it. Change detection compares NFC text with
whitespace collapsed. Lookups use NFC case folding. Scripts are detected as ISO 15924 codes (see
`src/kb/linguistics_records.py`).

## Identity

`src/kb/linguistics_identity.py` resolves a language only through a source-stated Glottocode or ISO 639-3 code:

- Wikidata items resolve through P1394 or P220;
- Wiktionary codes resolve through a documented mapping;
- WALS languages resolve through their published Glottocode.

A macrolanguage, a retired or split ISO code, or a shared code becomes candidates that stay `proposed` until a
reviewer decides (`propose_languoid_matches`, `review_linguistic_identity_match`). ISO retirements are recorded as
identity-history events (`record_iso_code_events`).

Lexemes from Wikidata and Wiktionary are only *proposed* as the same word (`propose_lexeme_matches`). They must
have the same languoid, lemma and lexical category; a shared source-stated sense item strengthens the proposal.
Homographs are never paired, and nothing is merged.

Every decision is an entity-history decision under the candidate's event key, so review-inbox `entity` targets
decide candidates too. Glottolog points project into Geospatial places through saved resolutions that wait for a
Geospatial review (`project_languoid_locations`, `review_languoid_location`).

## Cross-language records

The pack writes no alias, translation or search table of its own:

- `index_linguistic_texts` writes definitions and lemmas as `noesis-language-text-v1` texts, so
  `multilingual_search` surfaces senses;
- `propose_lexeme_alias` records a lemma as a *candidate* multilingual alias, which only
  `review_multilingual_alias` can accept;
- Noesis translations of a definition are `noesis-translation-record-v1` records with their producer and an
  `unreviewed` status. Answers list them under `noesis_translations` with that label, never as definitions.

## Monitors

`create_linguistics_monitor` creates a knowledge subscription on a lexeme, sense, languoid or WALS parameter.
Evaluation runs at committed watermarks (`run_linguistics_monitor`, `poll_linguistics_monitor`), with no new
scheduler. The events are:

- `definition_revised`;
- `sense_added` and `sense_removed`;
- `etymology_changed`;
- `classification_changed`;
- `iso_code_changed`;
- `feature_value_changed`.

Each event cites the old and new revision or release. The first run is an `in_view` baseline.

## Composition

`packs/linguistics/pack.json` (v1) with `composition.json` binds `linguistics.lexicon` and `linguistics.languoids`
plus the shared providers:

- `platform.cross-language`;
- `platform.entity-identity`;
- `platform.subscriptions`;
- `platform.source-runtime`;
- `geospatial.core`;
- `science.literature`.

Two optional features are off by default:

- `linguistics-wiktionary`, gated by the share-alike duties;
- `linguistics-typology`, which adds `linguistics.typology`.

Feature ids use hyphens because composition ids forbid underscores. `linguistics_readiness` reports the selected
features, the per-provider state and live-verification status.

## Never

- Machine-translated or model-generated definitions, glosses or etymologies presented as sourced.
- Speaker personal data (recordings of identifiable speakers, informant names, contributor profiles).
- Language-status, endangerment or correctness verdicts beyond quoting the source.
- Mirroring of full dumps beyond the bounded, licensed subsets.

## Evidence

- Offline: `tests/unit/domains/test_linguistics_acceptance.py` (word to cited lexeme dossier) and
  `tests/unit/linguistics/`.
- Live: none recorded yet (#2191).
