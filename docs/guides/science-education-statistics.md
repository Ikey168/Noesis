# Science education statistics

The Science bundle's optional `education-statistics` feature (default off)
takes an institution (a ROR id, an IPEDS UNITID or an ETER ID) or a country to
the statistics its publishers released: enrolment, graduates, staff, finances
and R&D spending, each with its definition, unit, reference period, release
vintage and as-of time, joined to Science and Funding records by citation.

Tracking issue: #2227. Source audit and access decisions:
[`docs/development/education-evidence/source-audit.md`](../development/education-evidence/source-audit.md).

## Sources

| Source | Connector format | Keyed by | Vintage |
| --- | --- | --- | --- |
| US IPEDS | `ipeds-csv-zip` (Data Center complete data files) | UNITID | declared provisional or final release |
| ETER | `eter-csv` (export) | ETER ID (+ national id, published identifiers) | declared data release |
| UNESCO UIS | `uis-json` (UIS Data API) | ISO alpha-3 country | UIS data version (`lastDataUpdate`) |
| OECD Education at a Glance | `oecd-sdmx-csv` (SDMX connector, OECD) | `REF_AREA`, ISCED level | EAG edition |
| Eurostat R&D | `eurostat-sdmx-csv` (SDMX connector, ESTAT) | Eurostat GEO, sector of performance | `LAST UPDATE` |

All five are sources of the `primary-scientific-evidence` source pack (1.2.0)
and stay `unverified-live` until the dated live check of #2444.

## Enabling

Select the feature in the Science bundle's composition; nothing else changes
while it is off.

```python
coordinator.select("science", version, features=["education-statistics"])
coordinator.activate("science-education-on")
```

The feature binds `science.education-statistics`, `economics.knowledge` (the
SDMX handling), `platform.entity-identity` (ROR match decisions),
`platform.subscriptions` and `platform.source-acquisition`.

## Journey

1. Acquire through the source-pack runtime (`run_source_pack_execution` with
   the pack above). Each document becomes one release; re-acquisition adds
   nothing and a publication that changes values without a new release date is
   refused.
2. Record ROR records (`record_education_ror_records`, through
   `src/ingestion/ror.py`) and propose matches
   (`propose_education_ror_matches`). A ROR id the source publishes, or an
   identifier shared with the ROR record's external ids, is **exact**; an equal
   name in the same country is a **candidate** that
   `review_education_ror_match` accepts or rejects. ROR status changes,
   successors and predecessors are reported, never re-pointed.
3. Ask `institution_statistics_as_of` (by `ror` or `scheme`/`code`) or
   `country_education_statistics_as_of` with an `as_of` date. The answer
   groups figures by concept and period: each source's value side by side with
   its own definition, unit, ISCED level, vintage (stage, `revision_of`, every
   earlier vintage of the value), the publisher's comparability notes and a
   citation. Missing, not applicable, confidential and suppressed values keep
   the publisher's code; a subject without data is `none_on_record`.
4. Link an institution to Funding records, scholarly works or methodology
   studies with `link_education_record`. The citation names the identifier and
   a held record must state it; names and keywords never link records.
5. Watch a ROR id, institution, country or indicator with
   `create_education_monitor` / `run_education_monitor`: `new_vintage`,
   `revised_value` and `identity_match_change` notices cite the release and the
   record ids; unchanged releases emit nothing.

## Never

No university rankings, league tables, quality or composite scores; no merging,
averaging or harmonising of sources; no currency conversion or rounding; no
estimated values for missing or suppressed cells; no per-student, per-staff or
per-capita ratios.

## Evidence

Offline: `tests/unit/domains/test_education_*.py`, with the journey in
`tests/unit/domains/test_education_statistics_acceptance.py` (fictional
fixtures, sockets blocked). Live evidence is recorded only under
`docs/development/education-evidence/` (#2444).
