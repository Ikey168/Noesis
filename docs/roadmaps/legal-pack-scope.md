# Legal pack scope

Status: standalone pack implemented offline, 2026-09-26 (see
`docs/guides/legal-pack.md`). Pack live acceptance is outstanding, and
relevance-ranked legal retrieval stays deferred until an independent
human-judged evaluation passes the recorded thresholds.

## Delivery state (audited 2026-09-26)

- Shipped: Shipped offline in `src/kb/legal.py` with the `legal-research` source pack. Pack live acceptance and relevance-ranked retrieval remain outstanding, as stated above.
- Composition dependency: record owner `legal.core` (`packs/legal/providers/legal.core.json`). The remaining live acceptance and ranked-retrieval evaluation need none.

Tracking: [#1720](https://github.com/Ikey168/Noesis/issues/1720). Its child
issues define dependencies and acceptance criteria.

## GitHub implementation issues

- [ ] [#1721](https://github.com/Ikey168/Noesis/issues/1721) — Pin legal source coverage, access conditions and reuse policy.
- [ ] [#1722](https://github.com/Ikey168/Noesis/issues/1722) — Define shared legal work, version, expression and decision records.
- [ ] [#1723](https://github.com/Ikey168/Noesis/issues/1723) — Integrate CELLAR legal works and case law into the Legal model.
- [ ] [#1724](https://github.com/Ikey168/Noesis/issues/1724) — Integrate German federal decisions with legal identities and locators.
- [ ] [#1725](https://github.com/Ikey168/Noesis/issues/1725) — Integrate Berlin laws and decisions as distinct legal records.
- [ ] [#1726](https://github.com/Ikey168/Noesis/issues/1726) — Resolve legal version and applicability history without guessing current law.
- [ ] [#1727](https://github.com/Ikey168/Noesis/issues/1727) — Establish legal retrieval quality evidence for supported jurisdictions.
- [ ] [#1728](https://github.com/Ikey168/Noesis/issues/1728) — Expose cited legal research and cross-jurisdiction comparison workflows.
- [ ] [#1729](https://github.com/Ikey168/Noesis/issues/1729) — Package and register the standalone Legal domain and source pack.
- [ ] [#1730](https://github.com/Ikey168/Noesis/issues/1730) — Add end-to-end offline Legal pack acceptance coverage.
- [ ] [#1731](https://github.com/Ikey168/Noesis/issues/1731) — Validate Legal source access and publish a bounded research demonstration.

## Outcome

Support bounded EU, German federal and Berlin legal research: identify an exact
legal work or decision, inspect the source-supported version, retrieve cited
passages, follow explicit citations/amendments and compare versions with clear
jurisdiction, date and source-coverage evidence.

## Existing work to reuse

- [CELLAR acquisition](https://github.com/Ikey168/Noesis/issues/1480): bounded
  CELLAR SPARQL and dissemination paths, CELEX/ELI/ECLI, language/version and
  explicit amendment/citation relationship mapping.
- [German federal decisions](https://github.com/Ikey168/Noesis/issues/1481):
  official decision index/download parsing, court/docket/date and paragraph
  locators. Coverage is published federal decisions, not all German case law.
- [Berlin laws and decisions](https://github.com/Ikey168/Noesis/issues/1482):
  official-publication parsing/import where available; no unrestricted API is
  asserted.
- [Political legislative timelines](https://github.com/Ikey168/Noesis/issues/1534)
  and [dossier comparisons](https://github.com/Ikey168/Noesis/issues/1535): reuse
  these to connect legislative process evidence to legal instruments, without
  treating process stages as definitive legal effect.

Relevant code includes `src/ingestion/regional_providers.py`,
`src/ingestion/roadmap_integrations.py`,
`src/domains/political/legislative_dossiers.py`, and
`config/source_packs/political.json`. The latter declares EUR-Lex inside the
political source pack; CELLAR and explicit Berlin import paths have distinct
operational readiness. No `src/domains/legal`, `packs/legal`, or
`config/source_packs/legal.json` exists in the audited checkout.

## Main gaps

1. A shared legal model for normative works, versions/expressions, court
   decisions, amendments/corrections, applicability assertions and precise
   citations, compatible with existing graph, evidence and temporal stores.
2. A bridge from the existing native provider adapters into those shared legal
   records, including complete provenance and refresh semantics.
3. Independent legal-specific relevance evaluation. Current exact-reference
   diagnostic reports explicitly say they do not establish general legal
   evidence relevance; retrieval modes remain opt-in.
4. A separate Legal domain/source pack, readiness and coverage reporting,
   source-aware lookup/version comparison and cited workflows.
5. Offline integration and dated, bounded live-source acceptance across every
   claimed jurisdiction/source.

## Exclusions and uncertainty

Do not claim exhaustive EU/German/Berlin law, all German case law, legal advice,
or current legal effect unless exact source evidence supports it. Other
jurisdictions and unverified providers are out of scope. The main unknowns are
how much shared data modeling is already supplied by generic legal/evidence
contracts, live access/terms by source, and whether independent retrieval labels
can support enabling any semantic mode. Resolve these through the issue sequence
before estimating effort or reporting coverage as complete.
