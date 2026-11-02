# Corporate Ownership: ICIJ Offshore Leaks

The Corporate Ownership bundle's optional `offshore-leaks` feature (#2888) is
off by default. It answers one question: *which offshore entities, roles and
intermediaries does the ICIJ Offshore Leaks Database link to this company,
cited to the leak dataset and node id?*

> **ICIJ's caveat travels with every answer.** "There are legitimate uses for
> offshore companies and trusts." Inclusion in the Offshore Leaks Database is
> not intended to suggest or imply that a person or entity broke the law or
> acted improperly. Many people and entities share the same or similar names.
> Noesis never derives a wrongdoing, AML, sanctions or risk verdict from this
> data. Any output carrying such a key is refused (`verdict_forbidden`).

Licence: ODbL for the database and CC BY-SA for its contents. Cite ICIJ. See
the [source audit](../development/offshore-leaks-evidence/source-audit.md),
which marks every item verified or *verify*.

## Acquire a bounded selection

Nothing imports the whole database. A selection names:

- `node_ids`: 1-200 seed node ids; or
- `datasets` (ICIJ `sourceID`, verbatim) and/or `jurisdictions` (ICIJ codes),
  with an explicit `max_nodes` (at most 1000);
- `hops`: 0 or 1 (default 1). One hop adds the relationships that touch a seed
  and the nodes at their other end;
- `node_kinds`: optional `{node_id: "person" | "legal_entity"}` overrides.

**Live, through the bulk-release runner:**

```bash
python -m src.ingestion.bulk run icij-offshore-leaks \
  --params '{"node_ids": ["<node id>"], "hops": 1}' --dry-run   # list the release first
python -m src.ingestion.bulk run icij-offshore-leaks --params '{"node_ids": ["<node id>"]}'
```

The runner downloads the archive once per release (byte budget and quota
ledger apply). It keeps only the selected subset as gzipped JSONL and deletes
the raw archive. Project the subset into Corporate Ownership:

```python
from src.kb.offshore_leaks import project_bulk_subset
project_bulk_subset(conn, "ownership", "<state>/icij-offshore-leaks/<release_id>",
                    principal_id=..., scopes=...)
```

Each record keeps the archive SHA-256, the release id and the line locator.
Re-projecting the same release is idempotent.

**Offline, through the source pack:** source `icij-offshore-leaks` (connector
`icij-offshore-leaks`) replays an extract directory of the CSV files. A default
`acquire_ownership_sources` run skips it unless the feature is selected; name
it in `source_ids` to run it explicitly. A live source-pack run is refused with
`bulk_release_required`.

## What is recorded

| ICIJ | Record | Note |
| --- | --- | --- |
| entity node | `legal_entity` `icij-offshore-leaks:node:<id>` | identifier `icij-node`. ISO jurisdiction only when ICIJ's description maps through a configured table. Other ICIJ values are kept verbatim in `native` |
| officer / intermediary | `person` or `legal_entity` | an entity only by a legal-form token or override; otherwise a natural person |
| `officer_of`, `intermediary_of` | `officer_role` | `role` is ICIJ's label verbatim ("shareholder of", "Nominee Shareholder of", …). **Never** a shareholding or control assertion. It stays out of parent and control queries |
| `registered_address` | entity `addresses` | a natural person's address is never stored |
| `same_as`, `similar`, `connected_to`, … | quoted in `native.icij_links` | identity links are only offered for review |

Every record cites `provider_record_id` = `<dataset>|node:<id>` (or
`<dataset>|rel:<start>-><end>|<type>`) and the ICIJ node page URL. Its
`source.note` carries the caveat.

## Natural persons

The default is owner-scoped storage. The acquiring principal and ownership
reviewers see person records and the roles that name them. Other analysts see
only that a redacted person link exists. With person-research mode on
(`NOESIS_PERSON_RESEARCH=1`, see
[person-research-mode.md](../security/person-research-mode.md)), person
records are namespace-visible under policy `offshore-leaks-person-research-v1`
and person-to-person ICIJ links may be offered for review. Person addresses
stay withheld in both modes.

## Identity: candidates only

`src.kb.offshore_leaks.propose_identity(conn, namespace, ...)` offers
candidates to the shared reviewable identity state machine:

- ICIJ `same_as` and kin get basis `source-stated-link` (confidence 0.3). The
  link is quoted as evidence, and a reviewer must corroborate it.
- ICIJ `similar` links and name-only matches with an unknown jurisdiction get
  basis `similar-name`. These can never be accepted.
- Equal names in a known, equal jurisdiction get `name-jurisdiction`. An
  `ibcRUC` equal to a registry number gets `unqualified-identifier`, and the
  reviewer must establish the issuing register.

Nothing is accepted automatically. Accepting a match is a reversible entity
identity decision, and records are never merged.

## Dossier

`build_dossier_with_offshore_leaks(conn, namespace, scheme, value, ...)` returns
the ownership dossier plus an `offshore_leaks` section for the entity's
reviewed identity cluster. The section lists:

- the leak-dataset records;
- the roles naming the entity, and the roles it holds elsewhere;
- quoted links and registered addresses;
- the related identity candidates and their state;
- a count of redacted person links.

Each line cites its record revision, dataset and node id, and carries the
caveat. `export_dossier` adds an "Offshore leaks (ICIJ)" section of sourced
statements and puts the caveat in the report limitations. The plain ownership
dossier and the OSINT entity dossier's ownership officers also carry the
caveat on ICIJ lines (`source_caveats`, `caveat`).
