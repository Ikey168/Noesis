# Climate and Environment: waste and circular economy

The Climate and Environment bundle (`packs/climate-environment/`) gains the
provider `environment.waste` (track #2740, subdomain `waste-circular-economy`)
behind four optional features, all default off: `waste-eurostat`,
`waste-eurostat-circular-economy`, `waste-eea-transfers` and `waste-oecd`. It
answers two questions:

- *Given a place, a waste or circularity indicator and a date, what did each
  source publish, under which definition, in which release?*
- *Given a facility known to `environment.core`, what off-site waste transfers
  did it report per reporting year, and how were they corrected?*

It quotes what each source released. It never nowcasts, fills a year a source
did not publish (Eurostat waste generation and treatment are biennial, so odd
years stay absent), blends Eurostat, OECD and EEA figures, sums facility
transfers into national totals, computes a recycling rate, per-capita or
material-flow figure of its own or any other derived indicator, and it makes
no forecast.

## The journey

1. Select one or more waste features in the composition plan (they are
   independent of each other and of the water and biodiversity features).
2. Run the `climate-environment-waste` source pack
   (`packs/climate-environment/source_packs/climate-environment-waste.json`,
   connector `waste`) through the shared source-pack tools. Each declared
   document is one release; the projector appends vintages.
3. Propose and review identity: `propose_waste_place_matches` (countries to
   Geospatial places by published code), `propose_waste_facility_matches`
   (INSPIRE ids to the `environment.core` facility records) and
   `propose_waste_related_indicators` (Eurostat and OECD indicators for one
   accepted place), then `review_waste_identity_match`.
4. Link: `link_waste_records` (Chemicals by published CAS number, Products by
   citation, transfer rows to their accepted facility).
5. Ask: `waste_indicator_for_place`, `waste_facility_transfers`,
   `waste_series_history`, `export_waste_bundle`.
6. Watch: `create_waste_monitor` and `run_waste_monitor` (or
   `poll_waste_monitor`).

## Sources

Four sources, all `unverified-live` until the dated live run (WC13). Access,
terms, rate limits, revision models, the minimisation decision and the bounded
coverage are in the [source audit](../development/waste-evidence/source-audit.md);
it was written without network access, so items marked _verify_ (endpoints,
dataset and dataflow codes, dimension and column names, licences, rate
limits) were not checked live.

| Source | Feature | Bounded first coverage | Access |
| --- | --- | --- | --- |
| `eurostat-waste` | `waste-eurostat` | Germany and France; `env_wasgen` total waste, all NACE activities and households, hazardous and non-hazardous; `env_wastrt` by treatment operation; biennial | SDMX-CSV through the SDMX connector (ESTAT); 2 documents, 60 series |
| `eurostat-circular-economy` | `waste-eurostat-circular-economy` | Germany and France; `cei_wm011`, `cei_srm030` as published | same path; one document per dataset, 10 series |
| `eea-industry-waste-transfers` | `waste-eea-transfers` | the `environment.core` Berlin facility selection; one reporting year | EEA Discodata SQL, the `eea-industry` pinned-query pattern; 500 rows (`nrOfHits`), a full page labelled truncated |
| `oecd-municipal-waste` | `waste-oecd` | Germany (DEU) and France (FRA); municipal waste generated, total | SDMX connector (OECD path, `format=csvfile`); dataflow _verify_; one request per 60 seconds |

## Records and vintages

`noesis-waste-record-v2` (`src/kb/waste_records.py`, `src/kb/waste_store.py`).
No new value store or record shape is introduced:

- A **statistical series** is keyed by source, dataset, indicator, waste
  category (EWC-Stat), hazardousness, NACE activity or households, treatment
  operation (`wst_oper`), unit as published, place and periodicity. Values live
  in the Economics series storage (`economic_vintages`, `dataset_observations`,
  through `register_series`); the `waste_*` tables keep the release, vintage,
  flag and definition bookkeeping. Flags stay verbatim (Eurostat `OBS_FLAG`,
  OECD `OBS_STATUS`); a confidential cell carries no value.
- A **facility transfer row** is keyed by the facility INSPIRE id, reporting
  year, hazardous or non-hazardous, recovery (R) or disposal (D) and domestic or
  transboundary, with the quantity in tonnes as published and the method code
  (measured, calculated, estimated). It is an `environment.core` observation
  series (`environment_vintages`, compared with `src/kb/environment_vintages.py`)
  located at the `eea-industry` facility record. No facility record is ever
  written here, and an unknown INSPIRE id never creates one.

Each changed release is an appended vintage with release and retrieval clocks:
Eurostat `LAST UPDATE`, the EEA dataset version, the declared OECD release
date, or the retrieval time labelled `retrieval_time`. A release dated after its
retrieval is refused. A changed row for a past reporting year is a new vintage,
never an overwrite. A series or row a later complete release no longer states
gets a `removed_by_source` vintage (a transfer row's removal is a vintage with
no value and the `removed_by_source` flag, never a zero). A truncated EEA page
is stored but is never complete, so it removes nothing. A failed document
(HTTP error, redirect to another host, schema drift, a response over the
budget) fails the source's run with its code and a receipt; earlier vintages
stay current and readiness reports the source `stale`.

## Answers

- `waste_indicator_for_place` - one row per series and source for a place (a
  place id through accepted matches, or a published code) as released by the
  date: definition revision, series key, flags, source notes, absent years
  (biennial years not collected) and the cited vintage. Eurostat and OECD stand
  side by side; accepted relations are shown, never reconciled. A place with no
  records answers `no_records`.
- `waste_facility_transfers` - a facility's rows per reporting year with every
  vintage cited, the `environment.core` facility record cited by id and
  revision (no operator field is copied), truncated acquisitions and the
  reporting-threshold note: facilities report only above the E-PRTR
  thresholds, so absence is not zero. Nothing is summed.
- `waste_series_history` - every vintage with its changes.
- `export_waste_bundle` - every item cites its source, record revision and
  as-of time.

## Identity, links and monitoring

- Identity assertions carry method, evidence and confidence and are proposed,
  reviewed, accepted, rejected or reverted; nothing is auto-merged. Published
  codes (ISO, NUTS, Eurostat GEO, INSPIRE id) come before names; names are
  never a match.
- Chemicals links rest only on a published CAS number a waste document cites
  (for example an entry of the E-PRTR pollutant list); Products links rest only
  on the citation of the packaging and WEEE datasets (`env_waspac`,
  `env_waselee`), which are later documents and are not acquired here. Links
  pin both revisions. Without the Chemicals, Products or `environment.core`
  stores a link is `provider_absent`; without a target it is
  `target_not_held`.
- Monitors are knowledge subscriptions on a place, an indicator, a facility, a
  series or a source. Notices for new releases, new years, revised values and
  resubmitted past years, corrected transfer rows and removals cite the
  revisions before and after; they are record changes, not assessments. A
  failed refresh writes nothing and never produces a removal notice.

## Data minimisation

Published aggregates and, per facility, only the INSPIRE id, reporting year
and the published transfer quantities, codes and method. The pinned EEA query
selects no operator, parent-company, address, contact or authority column, and
those fields are refused at write time and in every answer, as are derived,
filled, blended or forecast values. Operator names stay where `environment.core`
holds them. Reads need `knowledge:waste:read` and namespace access; facility
answers also `knowledge:environment:read`.

## Exclusions

No nowcasting, no filled years, no blending of Eurostat, OECD and EEA figures,
no summing of facility transfers into national totals, no recycling rates,
per-capita or material-flow figures of our own, no derived indicators, no
forecasts, no waste-shipment notifications, permit documents or inspection
records, and no second facility register.

## What is not live

Nothing in this provider has been checked against the publishers: the hosts
(`ec.europa.eu`, `discodata.eea.europa.eu`, `industry.eea.europa.eu`,
`sdmx.oecd.org`) are blocked from the runtime that built it. The fixtures are
authored (`tests/unit/waste_fixture_builder.py`): fictional values, reference
years 2094-2097, release dates 2098-2099 and facility names marked as
fixtures. Offline evidence: `tests/unit/domains/test_waste_*.py`, the
acceptance journey `test_waste_acceptance.py` and
`tests/unit/composition/test_environment_waste_composition.py`. Live
validation (WC13) has not run; offline coverage is not live coverage.
