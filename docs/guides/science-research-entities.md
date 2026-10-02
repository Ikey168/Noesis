# Science research entities

The Science bundle's provider `science.research-entities` takes an organisation
(a ROR id) or a researcher (an ORCID iD) to the public records identifier
registries published: ROR organisations, public ORCID records, DataCite dataset
metadata and CORDIS projects, each with its source, record revision and as-of
time, joined to Scholarly literature, Funding & grants and Corporate Ownership
records by citation, shared identifier or accepted identity match only.

Tracking issue: #2579. Source audit, access decisions and the researcher
data-minimisation decision:
[`docs/development/research-entities-evidence/source-audit.md`](../development/research-entities-evidence/source-audit.md).
Taxonomy: domain Science and knowledge, subdomain `research-entities-data`,
shape `registry-records` (offline coverage; live coverage is tracked by #2649).

## Sources and features

| Feature (default off) | Source id | Connector format | Keyed by | Revision |
| --- | --- | --- | --- | --- |
| `research-entities-ror` | `research-entities-ror` | `ror-dump-zip` (Zenodo data dump release) | ROR id | each dump release (vintage) |
| `research-entities-orcid` | `research-entities-orcid` | `orcid-record-json` (public API v3.0) | ORCID iD | record last-modified time |
| `research-entities-datacite` | `research-entities-datacite` | `datacite-doi-json` (REST API) | DOI | metadata version and `updated` |
| `research-entities-cordis` | `research-entities-cordis` | `cordis-projects-csv-zip` (project export) | programme and project id | `contentUpdateDate` |

All four are sources of the `research-discovery` source pack (1.5.0) and stay
`unverified-live` until the dated live check of #2649. The OpenAIRE Graph is
documented and not implemented (its relations include inferred links).

The ORCID source needs registered public-API client credentials: the operator
supplies the `/read-public` token as `NOESIS_ORCID_PUBLIC_TOKEN`. Without it the
source fails with `authentication_failed` and `research_entities_readiness`
reports the ORCID provider degraded; the other features are unaffected.

## Enabling

Select any of the four features in the Science bundle's composition; nothing
else changes while they are off.

```python
coordinator.select("science", version, features=["research-entities-ror", "research-entities-cordis"])
coordinator.activate("science-research-entities-on")
```

Each feature binds `science.research-entities`, `platform.entity-identity`
(reviewable organisation matches), `platform.subscriptions` and
`platform.source-acquisition`. Literature, funding and ownership are never
required: when their stores are absent, links are recorded as
`provider_absent` and re-resolved on a later run.

## Journey

1. **Acquire** the declared selections through the source-pack runtime
   (`research-discovery`, operation `selection`). Every unit leaves a receipt;
   a ROR id missing from a release or a project missing from an export is
   reported as `not_in_response`, never recorded as a deletion.
2. **Review identity** with `propose_research_entity_identity_matches` (ROR
   organisations and CORDIS participants to each other and to Corporate
   Ownership entities: published identifiers first, names only as low evidence)
   and `review_research_entity_identity_match`. Nothing is merged or accepted
   automatically; researchers are never proposed.
3. **Link** with `link_research_entities`: researchers' ORCID-asserted work DOIs
   and datasets' related identifiers to papers, dataset award numbers to CORDIS
   projects, project topics and calls to Funding records, accepted matches to
   ownership entities. Each link records its basis and the revisions it joins.
4. **Answer**:
   * `organisation_lineage_projects_datasets` - ROR relationships and
     successors as published in the release in force at the date, CORDIS
     participation through accepted matches with contributions as published
     (totals per currency, never summed across currencies), linked datasets and
     ownership matches;
   * `researcher_asserted_works_as_of` - employments and works asserted in the
     ORCID record version in force at the date, labelled ORCID-asserted, never
     verified authorship (researcher scope required);
   * `datasets_for_paper` - datasets whose metadata relates a paper's DOI;
   * `export_research_entities_evidence_bundle` - every item cited with source,
     record revision, as-of time and observation time.
5. **Monitor** with `create_research_entities_monitor` (organisation,
   researcher or project); notices cite the new and previous revision and state
   what changed. No new scheduler: the source-pack schedule acquires and the
   maintenance orchestrator commits the watermarks.

## Personal data

Researcher records keep only the public ORCID fields of the minimisation
decision (iD, public name, last-modified time, public employments and works).
They are returned only to principals holding
`knowledge:science:research-entities:researchers:read`; others see a count.
Personal DataCite creators keep only their ORCID iD and affiliation ROR ids.
The record store refuses any other personal field at write time, and an
operator can redact a researcher's name from every stored revision
(`ResearchEntityStore.redact_researcher`).

## Exclusions

No researcher rankings or metrics, no inference of affiliation from
co-authorship, no author disambiguation by name, no personal data beyond the
minimisation decision, no summing of contributions across currencies and no
inferred collaboration or influence links.

## Evidence

Offline: `tests/unit/domains/test_research_entities_*.py`; the journey is
`tests/unit/domains/test_research_entities_acceptance.py` (pinned fixtures,
sockets blocked, fictional Exampla and Northwind entities). No live evidence
exists yet; see [`docs/development/research-entities-evidence/`](../development/research-entities-evidence/README.md).
