# Science research entities: researchers, institutions, projects and datasets

The Science bundle's optional `research-entities-ror`, `research-entities-orcid`,
`research-entities-datacite` and `research-entities-cordis` features (each
default off) add the provider `science.research-entities`. It covers the
`research-entities-data` subdomain of the domain coverage program. Each
feature returns registry records as the registry published them, and every
answer cites the source, record revision and as-of time:

- **ROR**: organisations by ROR ID, one vintage per data-dump release. Each
  release carries names, types, status (active, inactive, withdrawn),
  relationships, successors and external identifiers.
- **ORCID**: public records of a bounded, declared set of researchers, reduced
  to the fields of the data-minimisation decision. Employments and works are
  labelled ORCID-asserted and never read as verified authorship.
- **DataCite**: dataset metadata versions, with related identifiers exactly as
  published.
- **CORDIS**: projects by programme and project ID, with participants (PIC,
  VAT number, role) and contributions in the currency the file states.

Source terms, limits and the minimisation decision are in the
[source audit](../development/research-entities-evidence/source-audit.md).

## Acquire

The sources `ror-organisations`, `orcid-public-records`,
`datacite-research-datasets` and `cordis-horizon-projects` are in the
`research-discovery` source pack (1.5.0). They run through the source-pack
runtime and are bounded to declared documents. Each run records a receipt.

A source's `research_entities.live_verification` stays `unverified-live` until
a dated live run is recorded (RE14, #2649). Before you enable ORCID, confirm
that the deployment is non-commercial; this is a condition of the ORCID Public
API terms. `ResearchEntitiesMonitor.refresh` re-acquires one source within its
page budget and honours `Retry-After`.

## Identity and links

`propose_research_entity_matches` proposes two kinds of match:

- **ROR organisation to CORDIS participant**: by website domain, or by name in
  the same country.
- **ROR organisation or participant to an ownership entity**: by published
  identifier first (ISNI, Wikidata, GRID or FundRef against ownership
  identifiers, or VAT number), then by name and jurisdiction as low evidence.

Every match carries its method, evidence and confidence. A reviewer accepts,
rejects or reverts it with `review_research_entity_match` and
`revert_research_entity_match`, and each decision is an entity-history
decision. Nothing is merged, and only accepted matches are used. Researchers
are never matched.

`build_research_entity_links` links records across packs:

- researchers to Scholarly works, through the DOIs they assert;
- datasets to Scholarly works, through their related identifiers;
- projects to Funding records, by shared identifier;
- organisations to ownership entities, through accepted matches.

Every link points at both revisions. A missing provider is reported as
`provider_absent` and a missing record as `target_missing`.

## Ask

| Question | Tool |
| --- | --- |
| A researcher's asserted works and employments on a date (needs `knowledge:research-entities:researchers`) | `researcher_assertions_as_of` |
| An organisation's ROR record, lineage, projects, datasets and ownership links on a date | `research_organisation_as_of` |
| Datasets related to a paper | `research_datasets_for_paper` |
| Every revision of a record | `research_record_history` |
| An evidence bundle for an answer | `export_research_entities_evidence_bundle` |
| Watch an organisation, researcher, project or dataset | `create_research_entities_monitor`, `run_research_entities_monitor`, `poll_research_entities_monitor` |

Contributions are grouped per currency and never summed across currencies.
Once ORCID reports a record deactivated, locked or unknown, the display name is
withheld from every revision.

## Evidence

- **Offline:** `tests/unit/domains/test_research_entities_acceptance.py` and the
  other `test_research_entities_*.py` tests replay synthetic fixtures with
  sockets blocked.
- **Live:** no live evidence exists yet. Live evidence will be recorded
  separately under
  [`docs/development/research-entities-evidence/`](../development/research-entities-evidence/README.md).
