# Chemicals and Substances

The Chemicals and Substances bundle (`packs/chemicals/`, provider
`chemicals.substances`) takes a substance name, CAS or EC number, InChIKey or
DTXSID to a cited dossier: the reviewable identity that connects the provider
records, the harmonised and notified CLP classifications with every ATP
revision, REACH registration status, SVHC Candidate List and Annex XIV/XVII
entries over time, the regulation text behind each cited act, the product
safety notices that cite the substance, and toxicity data points as their
source published them. Every value carries its source, revision and as-of
time.

It never produces a hazard or exposure assessment, a hazard score or safety
advice, never combines or ranks data points, never acquires synthesis or
preparation information, and never reports a substance without entries as
safe or unregulated: it has *none on record* in the acquired sources.

Tracking: #2212. Source audit:
[chemicals-substances-source-audit.md](../roadmaps/chemicals-substances-source-audit.md).

## Enabling

`packs/chemicals/pack.json` (`noesis-pack-v1`) with its `composition.json`
overlay binds `chemicals.substances` plus the shared `legal.core`
(`legal.works`), `products.safety` (`products.safety-notices`),
`platform.entity-identity`, `platform.subscriptions` and
`platform.source-runtime` providers. Once composition-managed,
`set_chemicals_bundle_enabled` is a coordinator selection change; disabling it
blocks only the chemicals entry points. `chemicals_bundle_status` reports each
provider as `ready`, `fixture-only` or `unavailable`, with live verification
kept separate.

Install the `chemicals-substances` source pack (`config/source_packs/chemicals.json`),
accept each source's licence, and set `NOESIS_COMPTOX_API_KEY` for the CompTox
source (auth `required-secret`; without it the source preflight reports it
unavailable).

## Components

| Part | Owner |
| --- | --- |
| Source contracts, selections and parsers | `src/ingestion/substance_sources.py` (connector `substances`); audit in `docs/roadmaps/chemicals-substances-source-audit.md` |
| Record contract (`noesis-substance-record-v1`) | `src/kb/substances_records.py`, `contracts/schemas/jsonschema/noesis-substance-record-v1.json` |
| Record owner and runtime projector | `src/kb/substances_store.py` |
| Reviewable identity | `src/kb/substances_identity.py` on `canonical_entities` and `src/kb/entity_history.py` |
| Citation links | `src/kb/substances_links.py` (Legal, Products safety, Materials, Clinical literature) |
| Status, history and dossiers (`noesis-substance-dossier-v1`) | `src/kb/substances_queries.py` |
| Monitors | `src/kb/substances_monitoring.py` on `SubscriptionStore` |
| Bundle declaration, enablement, readiness | `src/kb/substances_bundle.py` |
| MCP tools | `tools/knowledge_engine_mcp/substances.py` |

## Records and revisions

- A **substance** record is one provider's record: a PubChem CID, an ECHA
  substance or CLP group entry, a CompTox DTXSID. Its kind (single- or
  multi-component, group, mixture, salt, isomer) is kept as published.
- **Identifiers** (CAS, EC, index number, InChI/InChIKey, CID, DTXSID, names
  and synonyms) are stored as published. CAS and EC check digits are verified
  before an identifier is used for matching; a malformed one is kept, flagged
  and never matched. When depositors disagree (two valid CAS numbers on one
  PubChem compound) both are kept and flagged, and neither is chosen.
- A **classification** is harmonised (CLP Annex VI, by index number) or
  notified (C&L inventory aggregate, quoted with its notifier count). Each ATP
  that introduced, amended or deleted a harmonised entry is a separate
  **classification revision** dated by its date of application, with the act's
  CELEX; the prior classification stays.
- **Registration** status, **Candidate List** events, **Annex XIV**
  authorisation entries (sunset and latest application dates) and **Annex
  XVII** restriction entries (conditions verbatim) are dated events; a
  removal is a revision, and absence from a later response never removes an
  entry.
- **Data points** keep endpoint, value, qualifier, unit, study reference,
  source and the ToxValDB data version, labelled as the source's data.

## Identity

`propose_substance_identities` offers candidates between records of
different providers: `exact-identifier` (shared CAS, EC, DTXSID or index
number), `inchikey`, or `synonym` (an exact published name, no structural
identifier in common). Nothing joins until `review_substance_identity`
accepts a candidate, recorded as a `match` identity decision (`non-match` on
rejection); `revert_substance_identity` appends an `undo`. CLP group entries,
mixtures, salts and isomers are never proposed against another kind of
substance (they are listed as `withheld`); only
`propose_substance_identity_manual` followed by a review connects them.
`resolve_substance` returns every reviewable substance a query reaches with
the matches (basis, evidence, decision, reviewer) that joined its records;
`list_unmatched_substances` reports records no accepted match connects.

## Citations

`link_substance_citations`:

- links each classification, restriction, authorisation and Candidate List
  revision to the Legal work its act names, by exact CELEX or ELI, with the
  entry locator. The dossier shows the Legal owner's passage at that entry as
  the regulation text;
- links a product safety notice revision when its hazard, identification or
  corrective-action text states a CAS, EC, InChIKey or DTXSID the substance
  publishes, or one of the substance's own published names as a whole word
  (names shorter than five characters are never used); the link stores the
  citing text and locator;
- links toxicology literature (scholarly documents in the document store) and
  Materials records only on a stated identifier or a reviewed substance
  identity (`ent-substance-…`), never a name. Materials reports
  `provider_unavailable` until a Materials provider is composed.

A rejected identity never carries a link from one record to another.

## Status, history and dossiers

`substance_status_as_of` selects, for the date, the harmonised classification
revision whose date of application is on or before it (with the ATP that set
it), the Candidate List, Annex XIV and Annex XVII entries whose latest event
on or before it is an inclusion or amendment, and the registration status
last updated on or before it. Revisions dated later are listed under
`scheduled`, never as in force. Each category without an entry is listed
under `none_on_record` with the statement that this is not a statement that
the substance is safe, unregulated or not hazardous. An ambiguous query (no
accepted match joins the records it reaches) is refused with the candidate
records instead of a guess.

`substance_history` lists every revision with dates, event, legal act and
citation. `substance_dossier` assembles identity, status, history, the
regulation text, linked notices, materials and literature, data points and the
sources consulted with their fixture or live evidence, plus a reproducible
`dossier_hash`.

## Monitoring

`create_substance_monitor` creates a knowledge subscription over one or more
substances; `run_substance_monitor` evaluates it at the latest committed
`chemicals-substances` watermark whose run completed every source, and
`poll_substance_monitor` reads delivered events. Events cover new ATP
classification revisions, Candidate List, Annex XIV and Annex XVII
inclusions, amendments and removals, and newly linked notices, each with the
prior and new status, both citations and effective dates. The first
evaluation is marked as a baseline; replays and partial runs deliver nothing.

## Offline and live evidence

`tests/unit/domains/test_chemicals_acceptance.py` replays the whole journey
from pinned, authored fixtures with sockets blocked. Identifiers in the
fixtures are the substances' public identifiers; classification, list and
data-point values are transcribed or authored for illustration, and data-point
references are explicitly fictional. Every provider is `unverified-live`
until the dated live validation (#2317), which is reported separately from
the offline results.
