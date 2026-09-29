# Legal courts and justice statistics

The Legal pack's optional `courts` and `justice-statistics` features (#2218)
answer two questions with citations:

* given a **statute provision, court or organisational party** and a date,
  which dockets, docket entries and decisions were on record, with the
  dispositions quoted from the court record;
* given a **place**, which official crime and justice statistics were
  published for it, with their definitions, vintages, flags and coverage
  notes - and never compared across jurisdictions without a stated
  comparability note.

Both features are default off and independent; select them in the Legal
composition. Sources and terms: [source audit](../development/courts-justice-evidence/source-audit.md).

## Sources (`legal-research` 1.4.0, connector `courts-justice`)

| Source | Provider | Delivers |
| --- | --- | --- |
| `courtlistener-dockets` | CourtListener REST API v4 (token) | dockets, RECAP docket entries (verbatim, documents linked), parties minimised |
| `courtlistener-opinions` | CourtListener REST API v4 (token) | opinion clusters: citations, disposition quoted, opinions with paragraph passages |
| `fbi-cde-summarized` | FBI Crime Data Explorer (api.data.gov key) | UCR offence counts, rates, populations and participated populations; refresh date as vintage |
| `police-uk-street-crime` | data.police.uk | street-level crime and outcome counts per category for declared areas and months |
| `eurostat-crime-iccs` | Eurostat (JSON-stat) + ESMS | ICCS offences with flags, footnotes and the ESMS comparability section |

Every provider is `unverified-live` until a dated live run is recorded (#2434).

## Journeys

1. **Provision to dockets and decisions.** Run the `legal-research` pack,
   then `link_court_citations` (exact US Code, CFR and reporter citations;
   unresolved citations are kept with their text). `lookup_dockets` with
   `provision="42 U.S.C. § 1983"` and `as_of` returns the docket revision
   current at the date, the entries citing the provision and decisions with
   `disposition.quoted` and a locator. `no_docket_on_record` is explicit.
2. **Court or organisational party.** `lookup_dockets(court_id="dcd")`, or
   `propose_court_party_matches` then `review_court_party_match` (a reviewer
   accepts) and `lookup_dockets(entity_id=...)`. Natural persons are
   pseudonymised (`natural person 1 (Defendant)`), never matched, queried or
   monitored.
3. **Place to statistics.** `justice_statistics_for_place(place="eurostat-geo:DE")`
   (or a geospatial `place_id` after `resolve_justice_places`) returns each
   source's series with definition, unit, flags, suppressed values, gaps,
   coverage notes and the vintage released by `as_of`.
4. **Comparison.** `compare_justice_statistics` returns each series
   separately; an aligned view appears only when every cross-jurisdiction pair
   has a comparability note (`record_justice_comparability` +
   `review_justice_comparability` by another principal, or a source-published
   ESMS note). Otherwise the status is `refused_no_comparability_note`.
5. **Monitoring.** `create_courts_justice_monitor` for a docket, court,
   organisational party, provision or place series; `run_courts_justice_monitor`
   reports new entries, opinions, citing decisions, vintages and revised
   observations with before/after revisions.

## Exclusions

No personal profiles of private individuals, no recidivism or risk scoring, no
neighbourhood safety ratings, no rankings of places, no derived win/loss
labels or outcome predictions, and no legal advice.

## Evidence

Offline: `tests/unit/domains/test_courts_justice_acceptance.py` and the other
`test_courts_justice_*` suites over authored fixtures in
`tests/fixtures/courts_justice/`. Live: outstanding (#2434).
