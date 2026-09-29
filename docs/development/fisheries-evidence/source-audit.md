# Fisheries and Maritime Activity: source contract audit and bounded coverage (FI01, #2305)

Parent: #2222. This audit records, per source, the access method, licence and
attribution, redistribution, rate limits, authentication and revision/as-of
semantics, and selects the bounded provider coverage of the
`fisheries-maritime` source pack (`config/source_packs/fisheries.json`). The
machine-readable copy of each decision is `PROVIDER_CONTRACTS`,
`LIVE_VERIFICATION` and `EXCLUDED_RFMOS` in `src/ingestion/fisheries_sources.py`
(served by the `fisheries_source_contracts` MCP tool).

Nothing here was verified against a live endpoint from this runtime. Every
provider is `unverified-live`; items marked *verify* must be checked during the
live validation issue (FI14, #2346) and recorded in
[`README.md`](README.md) beside this file. Offline evidence (fixtures) is kept
in the tests and never recorded here.

## Scope boundary (all sources)

- No inference of illegal fishing from movement or effort patterns, no
  "illegal", "legal", "compliant" or "IUU" status derived for any vessel, and no
  enforcement recommendations. Listing reasons are quoted verbatim.
- A vessel with no record is reported as having none on record *in the covered
  registers and snapshots*, never as legal, compliant or authorised elsewhere.
- No per-vessel tracks, positions or events are acquired or stored.

## Per-source decisions

| Source | Access | Licence and attribution | Redistribution | Rate limits | Authentication | Revision / as-of semantics | Decision |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Global Fishing Watch - Vessels API v3 (identity) | HTTPS GET `/v3/vessels/{id}?dataset=public-global-vessel-identity:v3.0` | GFW API terms of use; data CC BY-NC 4.0 (*verify*); attribution "Global Fishing Watch" plus dataset version | Non-commercial, with attribution; identity segments only | Per-token limits set by GFW (*verify*); one vessel per page | API token (registration) as secret `NOESIS_GFW_API_TOKEN`; never in manifests, receipts or records | Dataset version (e.g. `v3.0`) re-derives identity; each version is a new revision; self-reported segments carry transmission periods | Selected, `unverified-live` |
| Global Fishing Watch - 4Wings report API (fishing effort) | HTTPS GET `/v3/4wings/report` (datasets, date-range, spatial/temporal resolution, group-by, region) (*verify* parameter names; the API also accepts POST with a region body) | As above | Aggregates only; never per-vessel effort | As above | As above | Dataset version (`public-global-fishing-effort:v3.0`) and period; a new version supersedes; *apparent* fishing effort estimated by a model | Selected, `unverified-live` |
| FAO FishStat - global capture production | Bulk release file (CSV inside FAO's zipped bundle) (*verify* the file URL; the adapter reads `RELEASE`/`RELEASE_DATE` columns) | CC BY-NC-SA 3.0 IGO (FAO database terms, *verify* per release); attribution "Source: FAO FishStat" plus release | Non-commercial, share-alike, with attribution | None published; one file per page | None | One annual release (plus corrections) republishes the full series; earlier years may be revised; every cell is a revision tied to its release; status flags (e.g. `E` estimate, `N` not significant) kept as published; units kept, never converted | Selected (capture only), `unverified-live` |
| ICCAT Record of Vessels | CSV export (*verify* URL) | Public record under ICCAT recommendations; attribution to ICCAT (*verify* terms) | With attribution | None published; one list per page | None | Continuously updated register; each download is a snapshot dated by `Last-Modified`; per-entry authorisation start/end; absence from a later snapshot recorded as a dated removal | Selected, `unverified-live` |
| ICCAT IUU Vessel List | CSV export (*verify*) | As above | With attribution | As above | None | Listings and delistings dated in the list after Commission decisions; reasons stated per entry | Selected, `unverified-live` |
| WCPFC Record of Fishing Vessels | JSON export (*verify*) | Public record under the WCPFC Convention (*verify*) | With attribution | None published | None | Snapshot `as_of` in the export; authorisation periods; removals by absence | Selected, `unverified-live` |
| WCPFC IUU Vessel List | JSON export (*verify*) | As above | With attribution | None published | None | Listing / removal dates in the export | Selected, `unverified-live` |
| IOTC Record of Authorised Vessels | Semicolon CSV export (*verify*) | Public record under IOTC resolutions (*verify*) | With attribution | None published | None | Snapshot by `Last-Modified`; authorisation periods; removals by absence | Selected, `unverified-live` |
| IOTC IUU Vessel List | Semicolon CSV export (*verify*) | As above | With attribution | None published | None | Listing / delisting dates in the list | Selected, `unverified-live` |
| Combined IUU Vessel List (iuu-vessels.org) | JSON export (*verify*: the site publishes an HTML list and downloads) | Public information with attribution to the Combined IUU Vessel List and the originating RFMOs (*verify* reuse terms) | With attribution | None published | None | A compilation that follows RFMO decisions; each entry cites the originating listings, which remain the authority; entries are never counted as independent confirmations | Selected, `unverified-live` |

### GFW licence decision (which datasets, at which aggregation level)

Stored:

- vessel identity segments (self-reported name, flag, call sign, IMO, MMSI,
  transmission period, gear types) for **explicitly selected** GFW vessel ids,
  keyed by vessel id and dataset version;
- fishing-effort aggregates at the **published grid** (`LOW`, 0.1 degree) and
  **monthly** period, grouped by flag and gear, for the selected region, with
  GFW's method note ("apparent fishing effort ... not confirmed fishing").

Not stored: per-vessel tracks or positions, events (fishing, encounters,
loitering, port visits), per-vessel effort, insights or risk indicators. A
response that carries any of them has those fields dropped and named in the
page receipt (`excluded_fields_dropped`).

## Bounded coverage

| Dimension | Selected | Rationale |
| --- | --- | --- |
| RFMOs (registers and IUU lists) | ICCAT, WCPFC, IOTC | The three tuna RFMOs with public, downloadable registers and IUU lists covering the Atlantic, Western and Central Pacific and Indian Ocean |
| Combined IUU Vessel List | whole list | Compilation across RFMOs; short |
| Flag states on the registers | ICCAT: ESP, GHA; WCPFC: KIR, ESP; IOTC: ESP, SYC | A distant-water fleet present in several RFMOs, plus a coastal state per ocean; keeps each register page bounded |
| IUU lists | taken whole | Each list holds tens of entries |
| FAO major areas | 27 (NE Atlantic), 34 (E Central Atlantic), 51 (W Indian Ocean) | Overlap with the ICCAT and IOTC convention areas |
| Species (ASFIS) | COD, SKJ, YFT | One demersal and two tuna species |
| Countries (FishStat) | ESP, GHA, NOR | Match the register flags plus one North Atlantic producer |
| Years | 2021-2022 | Two years to show release revisions without mirroring the series |
| GFW effort | ICCAT convention area, 2024-01 to 2024-02, LOW grid, monthly, by flag and gear | One region and two months of aggregates |
| GFW vessels | explicit vessel ids only | No enumeration or search |

Absence from a register snapshot is only compared within one list **and the
same declared selection** (the flag filter), so narrowing or widening the
selection never reads as a removal.

### Excluded RFMOs

| RFMO | Reason |
| --- | --- |
| CCSBT | Authorised-vessel records overlap the three selected tuna RFMOs for the bounded flags; deferred to keep coverage bounded |
| IATTC | Register export requires form navigation (no stable file URL found); deferred |
| NAFO | Not selected: bounded coverage is the three tuna RFMOs plus the combined list |
| NEAFC | IUU A/B lists are carried by the Combined IUU Vessel List; its own register is not selected |
| SPRFMO | Not selected for the bounded coverage |

## Composition with other packs (by citation only)

- **Legal sanctions** (`legal.sanctions`): a link only when the sanctions entry
  states the same IMO number, citing the listing revision.
- **Geospatial** (`src/kb/geospatial.py`): FAO major areas, RFMO convention
  areas, GFW regions and published grid cells are registered as places by their
  published codes; no geometry is guessed from free text.
- **OSINT vessel movements** (#2221) and **Agriculture & Food Systems**: read
  through the generic citing interface in `src/kb/fisheries_identity.py`; when
  no provider is registered the link step reports `provider_unavailable`.
