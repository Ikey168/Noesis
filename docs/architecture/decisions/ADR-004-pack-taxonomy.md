# ADR-004: Pack taxonomy and admission rule

Status: accepted, 2026-09-30. The "empty cells are not a to-do list" clause of
the admission rule is superseded by
[ADR-005](ADR-005-domain-coverage-program.md). Classification only; it changes no runtime
behaviour, bundle id or provider id. The overlay is
[`packs/taxonomy.json`](../../../packs/taxonomy.json), enforced by
`tests/unit/composition/test_pack_taxonomy.py`.

## Context

`packs/` holds 30 bundles with 100 provider descriptors, in no structure beyond
a flat list. Neither manifest format has a category field. Three problems
follow:

- **Misplaced providers.** Several sit in whichever bundle owned the nearest
  store, not their subject. Examples: `science.education-statistics`,
  `science.media-metadata`, `economics.demographics` and
  `legal.justice-statistics`. `sports` fits no neighbour at all.
- **Repeated machinery.** Bundles rebuild the same record shapes. `src/kb/`
  has 16 separate series-with-vintages stores, 43 `*_monitoring.py`, 36
  `*_identity.py` and 17 `*_bundle.py` modules.
- **No stopping point.** Without a map, every new source looks like a new
  pack.

## Decision

1. **Three axes.**
   - **Domain** (subject). Each bundle has exactly one domain. A provider
     inherits it and may name its own domain when its subject differs.
   - **Record shape.** Each provider names one or more shapes.
   - **Theme** (cross-cutting). Each provider may carry any number of themes.
2. **Nine closed domains.** These were checked against COFOG, the UN
   classification of government functions:
   - governance and law
   - society and population
   - economy and markets
   - earth and environment
   - science and knowledge
   - health
   - technology
   - culture and leisure
   - information and investigation

   Religion and philosophy have too little structured public data to justify
   a domain. Life sciences fits under Science or Health until a real question
   needs more.
3. **Six record shapes.**
   - statistical series with release vintages
   - registries and identity records
   - events, notices and incidents
   - versioned documents
   - observations and measurements
   - places and geometry
4. **Themes instead of extra domains.** Security and defence, transport and
   mobility, and climate cut across domains. Making them domains would give
   records two homes, so they are tags.
5. **Infrastructure stays outside.** `platform` provides shared machinery, so
   it has no domain. Its providers stay unclassified unless one serves subject
   records; `platform.web-archives` names Information and investigation.
6. **An overlay, not a move.** Classification lives in one file instead of the
   manifests, for two reasons:
   - Manifest v1 silently drops unknown fields (divergence 3 in the
     [composition inventory](../pack-composition-inventory.md)).
   - Bundle and provider ids are preserved identifiers.

   A misplaced provider is recorded as a domain override, not relocated.

## Admission rule

- **A new source names its cell**, meaning its domain and record shape. When
  both already exist, the source is a provider inside an existing bundle, not
  a new pack.
- **A new record shape is an architecture decision.** Record it before
  implementing.
- **A new domain needs a decision record superseding this one.** Update the
  domain set in the gate test in the same change.
- **Themes are cheap.** Add one when a question needs to cut across domains.
- **Empty cells are not a to-do list.** The gate refuses a domain, shape or
  theme that nothing uses. Filling a cell still needs a real question the
  existing bundles cannot answer.

## Consequences

- **The gate keeps the overlay current.** Adding a bundle or provider
  descriptor without classifying it fails
  `tests/unit/composition/test_pack_taxonomy.py`, and so does leaving a stale
  entry.
- **Some domains start with overrides only.** Society and population holds no
  bundle of its own yet: demographics, labour, housing, justice statistics and
  education statistics reach it through overrides. Culture and leisure holds
  `sports` plus the cultural and media-metadata overrides. Moving providers
  between bundles would change their ids and needs its own migration.
- **Shared cores are the follow-up.** The record-shape axis marks where
  per-bundle code repeats. The series-with-vintages stores are the first
  candidate for one shared core. Each consolidation is its own change.
- **Nothing reads the taxonomy at runtime yet.** Exposing it through the
  catalog or `noesis-domain-packs` is a separate change.
