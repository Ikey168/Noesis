# Linguistics: lexical and typological source audit, licences and bounded coverage (LG01, #2179)

Tracking issue: #2178. For each candidate linguistic source, this audit records:

- the access route and authentication;
- the licence, attribution and share-alike duties;
- the rate limits;
- the stable identifiers;
- how releases and revisions are versioned;
- the access decision (`implement`, `link-only` or `not implemented`) with its reason;
- the bounded v1 coverage the Linguistics pack acquires.

**Verification status.** No live request was made while writing this audit.
The build environment has no network access to Wikidata, kaikki.org,
Glottolog, WALS Online, CLDR or SIL. Every claim marked **(verify)** comes
from the providers' published documentation as known when this was written.
Each one must be checked against the live service and its current terms
before live acquisition is accepted (LG13, #2191).

The adapters in `src/ingestion/linguistics_sources.py` parse the documented
formats. All offline fixtures under `tests/fixtures/linguistics/` and
`tests/fixtures/source_packs/linguistics-*.json` are **authored**. They use
fictional or clearly synthetic entries with realistic identifier shapes:
L-ids, `L…-F…`/`L…-S…` ids, Glottocodes such as `fict1234`, ISO 639-3 codes
in the unassigned `qaa`–`qtz` private-use range, and WALS feature ids such as
`81A`. None of them is captured data.

`PROVIDER_CONTRACTS` and `LIVE_VERIFICATION` in
`src/ingestion/linguistics_sources.py` restate these decisions in code. Every
implemented source is `unverified-live` until a dated live run is recorded
under this directory.

## Summary

| Source | Endpoint | Format parsed | Licence | Decision |
| --- | --- | --- | --- | --- |
| Wikidata lexemes (L-entities with forms and senses) | `https://www.wikidata.org/wiki/Special:EntityData/L<n>.json` (per-entity JSON); lexeme dumps at `https://dumps.wikimedia.org/wikidatawiki/entities/` | entity JSON (`wikidata-lexeme-json`) | CC0 1.0 | `implement` (unverified-live), bounded to declared L-ids |
| Wikidata language items (stated Glottocode and ISO 639-3) | `https://www.wikidata.org/wiki/Special:EntityData/Q<n>.json` | entity JSON through `src/ingestion/wikidata.py` | CC0 1.0 | `implement` (unverified-live), bounded to the declared language items |
| Wiktionary through kaikki.org Wiktextract extracts | `https://kaikki.org/dictionary/<Language>/kaikki.org-dictionary-<Language>.jsonl` **(verify path)** | per-language JSONL (`kaikki-jsonl`) | CC BY-SA 4.0 and GFDL (dual) | `implement` (unverified-live), bounded to declared words, behind the optional `linguistics-wiktionary` feature |
| Glottolog | CLDF release `glottolog/glottolog-cldf` (Zenodo DOI per release) | CLDF `languages.csv` and `values.csv` (`glottolog-cldf`) | CC BY 4.0 | `implement` (unverified-live), bounded to declared subtrees |
| WALS Online | CLDF release `cldf-datasets/wals` (Zenodo DOI per release) | CLDF `languages.csv`, `parameters.csv`, `codes.csv`, `values.csv` (`wals-cldf`) | CC BY 4.0 | `implement` (unverified-live), bounded to declared features and languages, behind the optional `linguistics-typology` feature |
| Unicode CLDR | `cldr-json` release packages (`cldr-localenames-full`, `cldr-core`) | CLDR JSON (`cldr-json`) | Unicode License v3 | `implement` (unverified-live), bounded to declared locales and fields |
| Leipzig Glossing Rules (abbreviation list) | `https://www.eva.mpg.de/lingua/resources/glossing-rules.php` | PDF; abbreviation list transcribed into code | see below **(verify)** | `link-only` for the document; the standard abbreviation list is a bundled controlled vocabulary published as an ontology module |
| SIL ISO 639-3 code tables | `https://iso639-3.sil.org/code_tables/download_tables` | tab-delimited `iso-639-3.tab`, `iso-639-3-macrolanguages.tab`, `iso-639-3_Retirements.tab` (`iso639-3-tab`) | SIL terms of use **(verify)** | `implement` (unverified-live), bounded to declared codes; see the access decision below |
| Wiktionary via the MediaWiki API or HTML | `https://<lang>.wiktionary.org/` | n/a | CC BY-SA 4.0 / GFDL | `not implemented`: the Wiktextract extracts are the documented, parsed, bounded route. Scraping wikitext would mean writing a second parser. |
| Glottolog web API / WALS web pages | `https://glottolog.org/`, `https://wals.info/` | n/a | CC BY 4.0 | `link-only`: the pinned CLDF releases carry the same data with release identifiers. Record URLs link to the web pages for humans. |
| Ethnologue | `https://www.ethnologue.com/` | n/a | proprietary, subscription | `not implemented`: licence forbids reuse. Language status verdicts are also a tracker non-goal. |
| Speaker recordings (Wikimedia Commons audio linked from Wiktionary `sounds`, Lingua Libre) | n/a | n/a | various | `not implemented`: recordings of identifiable speakers are excluded (tracker non-goal). The Wiktextract `sounds` array is dropped at parse time and counted, never stored. |

## Licences and attribution duties

| Source | Licence | Attribution text carried on every record and answer | Share-alike |
| --- | --- | --- | --- |
| Wikidata lexemes and items | CC0 1.0 | "Wikidata, <entity id> revision <revid>, CC0" (attribution is courtesy, not a duty) | no |
| Wiktionary via kaikki.org | CC BY-SA 4.0 (and GFDL 1.3 dual licence) **(verify)** | "Wiktionary contributors, entry '<word>' (<language>), dump <dump date>; extracted by kaikki.org Wiktextract <extract date>; CC BY-SA 4.0" plus the page URL | **yes** |
| Glottolog | CC BY 4.0 | "Hammarström, Harald & Forkel, Robert & Haspelmath, Martin & Bank, Sebastian. Glottolog <version>. Leipzig: Max Planck Institute for Evolutionary Anthropology. <DOI>" **(verify citation form per release)** | no |
| WALS Online | CC BY 4.0 | "Dryer, Matthew S. & Haspelmath, Martin (eds.) WALS Online (v<version>). Zenodo. <DOI>", plus the chapter author for the feature **(verify)** | no |
| Unicode CLDR | Unicode License v3 **(verify)** | "Unicode CLDR <release>, © Unicode, Inc., Unicode License v3" | no (the licence notice must travel with copies) |
| Leipzig Glossing Rules | published by the Department of Linguistics, MPI EVA; free use is invited **(verify terms)** | "The Leipzig Glossing Rules (Comrie, Haspelmath, Bickel), MPI EVA, abbreviation list" | no |
| SIL ISO 639-3 code tables | SIL International terms of use for the code tables **(verify)**: download and use for identification are permitted; the tables are identifiers, not content | "ISO 639-3 code tables, SIL International (Registration Authority), <table date>" | no |

### Wiktionary share-alike (CC BY-SA 4.0)

These rules apply to Wiktionary content and everything derived from it:
definitions, glosses, usage examples, etymology notes, forms and translation
tables.

1. **Every record carries its obligations.** A record derived from a
   Wiktextract entry stores `licence = {"id": "CC-BY-SA-4.0 OR GFDL-1.3",
   "share_alike": true, "attribution": …, "url": …}` in its source envelope.
   This happens at parse time, never at display time
   (`src/ingestion/linguistics_sources.py`).
2. **Every answer carries its obligations.** Query answers
   (`src/kb/linguistics_queries.py`) collect the licences of every record
   they cite into a `licences` block. The block has `share_alike: true` when
   any cited record is CC BY-SA, and an attribution list naming each entry
   and dump.
3. **Exports are checked.** `export_lexeme_dossier` takes the licence the
   export will be published under:
   - an export containing CC BY-SA content is **refused** unless the target
     licence is CC BY-SA 4.0 (or a later version the licence permits,
     **(verify)**) and attribution is kept;
   - an export that asks to drop attribution is refused whatever it
     contains.

   The refusal names the records that trigger it. An export can instead
   exclude Wiktionary-derived content (`exclude_share_alike=True`); it then
   lists what was left out.
4. **No mixing into non-SA stores.** Wiktionary text is never copied into
   another record owner. Cross-language links reference Linguistics record
   ids. Definition texts written into `noesis-language-text-v1` records for
   multilingual search keep the licence in their metadata.
5. **The feature is gated.** The `linguistics-wiktionary` optional feature
   is off by default. Turning it on is an explicit choice, recorded in the
   composition activation receipt. Operators also accept the source licence
   through the source-pack runtime (`accept_source_pack_license`), which
   records `redistribution` as the declared share-alike policy.

**Refused exports:** a non-SA licence (CC BY, CC0, proprietary) over any
Wiktionary-derived record; any export with attribution removed; bulk export
of more than the bounded entries (no dump mirroring).

## Wikidata lexemes

- **Endpoint.** `Special:EntityData/L<n>.json`, with optional `?revision=<revid>`.
  It serves the same entity JSON as `wbgetentities`. Lexeme dumps
  (`latest-lexemes.json.bz2`) exist but are not mirrored.
- **Auth.** None.
- **Rate limits.** The Wikimedia User-Agent policy asks for a descriptive
  User-Agent. Bots must be polite: sequential requests, honour `Retry-After`
  and maxlag **(verify)**. The runtime sends one request per declared entity
  and never retries automatically.
- **Identifiers.**
  - `L<n>`: lexeme.
  - `L<n>-F<m>`: form.
  - `L<n>-S<m>`: sense.
  - `Q<n>`: language, lexical category and grammatical feature items.
  - Statement ids: `L<n>$<uuid>`.
- **Versioning.** `lastrevid` (an integer per edit) and `modified` (an ISO
  timestamp). A later revision that changes a gloss, a form representation or
  a grammatical feature is a new source revision. The store appends it as a
  revision when the normalised content changed.
- **Deletion.** A deleted or missing lexeme returns HTTP 404, or an entity
  marked `missing` **(verify)**. It is recorded as a `deleted` observation.
  Earlier revisions stay.
- **Properties used (verify each id).**
  - P5191: derived from lexeme.
  - P5238: combines lexemes (compound-of).
  - P5886: mode of derivation, a qualifier that distinguishes borrowing,
    calque and inheritance.
  - P5137: item for this sense. This is a source-stated link, never an
    identity claim.
  - P1394 (Glottolog code) and P220 (ISO 639-3) on language items.
- **Unreferenced statements.** An etymology statement without references
  becomes an `etymology_assertion` flagged `unreferenced`. It is never
  dropped and never upgraded.
- **Bounded v1 coverage.** Declared L-ids for two languages. The offline
  fixture declares 3 lexemes and 2 language items; production is at most 200
  lexemes per language.

## Wiktionary via kaikki.org Wiktextract

- **Endpoint.** kaikki.org publishes per-language JSONL extracts produced by
  Wiktextract from a named Wiktionary dump. Both the extract date and the dump
  date appear on the download page **(verify)**. The declaration pins both
  dates as the release identifier (`<extract date>/<dump date>`). A changed
  file at the same URL with a different declared release is a new release.
- **Auth.** None.
- **Rate limits.** None published **(verify)**. The per-language files are
  large, so production declares a bounded word list. The adapter streams and
  keeps only declared words. Other lines are counted as `out_of_scope` in the
  page receipt, never silently dropped, and never stored. There is no
  full-dump mirroring.
- **Entry fields parsed.**
  - `word`, `lang`, `lang_code`, `pos`, `etymology_number`.
  - `etymology_text` (kept verbatim as a cited note).
  - `etymology_templates` (`name`, `args`, `expansion`).
  - `forms` (`form`, `tags`).
  - `senses`, each with `id` **(verify: present in recent extracts)**, `senseid`,
    `glosses`, `tags`, `examples` (`text`, `english`/`translation`, `ref`),
    and `wikidata`.
  - `translations` (`lang`, `code`, `word`, `sense`).

  `sounds` is dropped (speaker data exclusion).
- **Identity.**
  - A lexeme is identified by (`lang_code`, NFC `word`, `pos`,
    `etymology_number`). Wiktionary page titles are case-sensitive, so the
    word is **not** case-folded for identity; case folding applies only to
    lookup.
  - A sense uses its source-stated `id`, else its first `senseid`. Without
    either, it falls back to its position. That fallback is labelled
    `identity_basis: position` and flagged as unstable across releases.
- **Etymology templates.** A template becomes an `etymology_assertion` only
  when it is structured and has a known relation:
  - `inh`/`inherited` → inherited;
  - `bor`/`borrowed`/`lbor`/`slbor` → borrowed;
  - `der`/`derived` → derived;
  - `cal`/`calque` → calque;
  - `cog`/`cognate` → cognate;
  - `com`/`compound`/`af`/`affix` → compound-of.

  The native template name is kept. Free text is never parsed into links.
- **Bounded v1 coverage.** Declared words for two languages. The offline
  fixture has two synthetic languages with 2 words each, over two extract
  releases. Production is at most 200 words per language.

## Glottolog (CLDF)

- **Endpoint.** The `glottolog/glottolog-cldf` release archive (GitHub/Zenodo,
  with a DOI per version).
- **Tables used.**
  - `languages.csv` columns: `ID`, `Name`, `Macroarea`, `Latitude`,
    `Longitude`, `Glottocode`, `ISO639P3code`, `Family_ID`, `Language_ID`
    **(verify columns per release)**.
  - `values.csv` rows for the `level` and `classification` parameters.
    Classification values are slash-separated ancestor Glottocodes
    **(verify)**.

  The `aes` endangerment parameter is **not read** (no status verdicts).
- **Identifiers.** Glottocode (`[a-z0-9]{4}[0-9]{4}`), ISO 639-3 where given,
  and level (`language`, `dialect`, `family`).
- **Versioning.** Release version (e.g. `v5.1`) and release date, declared per
  document. A changed parent or classification in a later release is a new
  revision of the languoid.
- **Bounded v1 coverage.** Declared subtree roots. The fixture has one
  synthetic family with 2 languages and 1 dialect, over two releases in which
  the dialect's parent changes. Production is at most 3 subtrees and 500
  languoids.

## WALS Online (CLDF)

- **Endpoint.** The `cldf-datasets/wals` release (Zenodo DOI per version).
- **Tables used.**
  - `languages.csv`: `ID` (the WALS code), `Name`, `Glottocode`,
    `ISO639P3code`, `Latitude`, `Longitude`.
  - `parameters.csv`: `ID` (feature id such as `81A`), `Name`.
  - `codes.csv`: `ID`, `Parameter_ID`, `Name`, `Number`.
  - `values.csv`: `ID`, `Language_ID`, `Parameter_ID`, `Value`, `Code_ID`,
    `Comment`, `Source` (semicolon-separated BibTeX keys with optional
    `[pages]`) **(verify)**.
- **Identifiers.** The WALS code (3 letters) with its Glottocode mapping as
  published. The mapping is kept even where it differs from other sources.
- **Bounded v1 coverage.** Declared features (fixture: `81A`, `87A`) and
  languages (fixture: 2). Production is at most 20 features.

## Unicode CLDR

- **Endpoint.** The `cldr-json` release packages for a pinned CLDR release
  (e.g. `45.0`).
- **Files used.**
  - `cldr-localenames-full/main/<locale>/languages.json` and `scripts.json`,
    read from `main.<locale>.localeDisplayNames.languages|scripts`.
  - `cldr-core/supplemental/plurals.json`, read from
    `supplemental.plurals-type-cardinal`.
- **Licence.** Unicode License v3 **(verify)**. The notice travels with every
  record.
- **Bounded v1 coverage.** Declared locales (fixture: 2) and declared
  subjects (the bounded languages and scripts).

## Leipzig Glossing Rules

The rules are a PDF document with a standard list of about 80 category
abbreviations. There is no machine-readable release. The abbreviation list is
therefore **bundled** as a controlled vocabulary in
`src/kb/linguistics_records.py`. It is published as the ontology module
`linguistics-leipzig-glossing` in the schema registry. Gloss validation reads
that module. The rules' conventions are applied as published:

- person–number combinations such as `3SG`;
- the `N`-prefix negation (non-X);
- the segment separators `-`, `=`, `.`, `\`, `_`, `>`.

An abbreviation outside the list is reported as `unvalidated` with the token.
It is never rejected silently, because sources may define their own
abbreviations. Decision: `link-only` for the document, bundled vocabulary for
validation. Reuse terms **(verify)**.

## SIL ISO 639-3 code tables

**Access decision.** `implement`, bounded to declared codes, from the three
tab-delimited tables:

- `iso-639-3.tab`: `Id`, `Part2b`, `Part2t`, `Part1`, `Scope` (I/M/S),
  `Language_Type`, `Ref_Name`, `Comment`;
- `iso-639-3-macrolanguages.tab`: `M_Id`, `I_Id`, `I_Status`;
- `iso-639-3_Retirements.tab`: `Id`, `Ref_Name`, `Ret_Reason` (C change,
  D duplicate, N non-existent, S split, M merge), `Change_To`, `Ret_Remedy`,
  `Effective`.

The tables identify languages; they do not describe them. The SIL terms of
use for the code tables **(verify)** permit downloading and using the codes.
The ISO standard text itself is not reproduced.

**How the tables are used.**
- Retirements, changes and splits become identity-history events on the
  languoid. They are recorded as decisions in `src/kb/entity_history.py`,
  never as silent code rewrites.
- A macrolanguage (`Scope = M`) never resolves deterministically to one
  individual language. A source that names a macrolanguage code produces
  candidates that stay `proposed` until reviewed.

**Bounded v1 coverage.** Declared codes. The fixture has 5 private-use codes,
including one macrolanguage and one retirement split.

## Stable identifiers at a glance

| Identifier | Shape | Owner |
| --- | --- | --- |
| Lexeme | `L[1-9][0-9]*` | Wikidata |
| Form / sense | `L…-F[1-9][0-9]*` / `L…-S[1-9][0-9]*` | Wikidata |
| Wiktionary entry | (`lang_code`, word, pos, etymology number) | kaikki.org / Wiktionary |
| Wiktionary sense | source `id` or `senseid`, else position (unstable) | kaikki.org / Wiktionary |
| Glottocode | `[a-z0-9]{4}[0-9]{4}` | Glottolog |
| ISO 639-3 | `[a-z]{3}` | SIL (Registration Authority) |
| WALS language / feature | `[a-z]{2,3}` / `[0-9]{1,3}[A-Z]` | WALS |
| CLDR locale | BCP 47 / Unicode locale id | Unicode CLDR |

## Normalisation (applies to every source)

- **Text is stored exactly as published.** An NFC form is stored beside it
  for comparison. Change detection compares the NFC, whitespace-collapsed
  form, so a source that switches between NFD and NFC produces no false
  revision.
- **Lookup keys are case-folded after NFC** (`casefold()`, then NFC again).
  Identity keys are not case-folded where the source is case-sensitive
  (Wiktionary titles, lemma representations).
- **Scripts** are detected per text as ISO 15924 codes from Unicode character
  properties. Common characters (digits, punctuation, spaces) and combining
  marks are ignored. Mixed-script text is labelled with its majority script
  and flagged `mixed_script`. `Zyyy` means only common characters were
  found.
- **Language tags** follow BCP 47 casing: lower-case language, title-case
  script and upper-case region.

## What is never stored

- Machine-translated or model-generated definitions, glosses or etymologies
  presented as sourced. A Noesis translation is a
  `noesis-translation-record-v1` with its producer and review status,
  referenced by id (`src/kb/cross_language.py`).
- Speaker personal data: Wiktextract `sounds` (audio files, speaker names,
  recording metadata), informant names and contributor profiles. The
  Wiktextract parser drops them and counts the drop.
- Language-status, endangerment or correctness verdicts (the Glottolog `aes`
  parameter is not read).
- Full dumps beyond the declared bounded subsets.
