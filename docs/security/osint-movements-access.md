# Aviation and vessel movement access decision (MV01)

Tracking: #2221, delivery issue #2233. This page records, source by source,
whether aircraft and vessel registry records and bounded movement samples may
enter the OSINT pack, under which licence, and within which volume bounds. It
follows the access-decision pattern of the passive DNS decision
(`osint-passive-dns-access.md`) and the Fisheries source audit
(`docs/development/fisheries-evidence/source-audit.md`). Nothing is acquired
from a source that has no row here.

The movement extension is an **optional feature of the existing OSINT pack**
(`packs/osint/`, feature `movements`, default off; sources in
`config/source_packs/osint.json`). There is no new pack, no real-time
tracking, no continuous mirroring and no targeting use.

> **Verification status.** The terms, endpoints and rate limits below were
> assessed from the providers' published documentation as known when this page
> was written. No live terms page, contract or API was fetched for this
> decision, and every source is `unverified-live` until a dated live run
> (#2291). Rows marked *verify* must be checked against the provider's current
> terms before a deployment enables the source; nothing below is legal advice.

## What the movement extension may store

A movement source is usable only if **all** of these hold:

1. Its records may be **stored** in the operator's own warehouse beyond the
   session that fetched them, and **cited** (locator plus attribution) in
   material shown to other people, such as an evidence bundle.
2. Queries can be restricted to **one named aircraft or vessel identifier and
   one bounded window** (or, for aggregates, one declared area and period). No
   area-wide, background or "everything in view" query is issued.
3. The source is not a person register: aircraft and vessels are keyed by
   ICAO 24-bit address, registration mark, IMO number, MMSI, call sign or the
   provider's vessel id, never by the name of a pilot, crew member or owner.
4. Access is by per-deployment account or key where the provider requires one,
   held as a `NOESIS_*` secret reference, never in manifests or records.

Continuous bulk ADS-B or AIS mirroring is **rejected** for every source,
including sources whose terms would permit it: the extension exists to cite
bounded samples, not to hold a movement archive.

## Volume bounds

`src/osint/movements.py` (`BOUNDS`) enforces these numbers; a request, source
selection or record outside them is refused with a stated reason.

| Source | Max identifiers per query | Max window | Max samples stored per identifier and window | Gap threshold |
| --- | --- | --- | --- | --- |
| FAA aircraft registry | 1 per page; 50 per source | registry state, no window | n/a (registry revisions only) | n/a |
| UK CAA G-INFO | 1 per page; 50 per source | registry state, no window | n/a | n/a |
| OpenSky Network | 1 | 48 hours | 500 position samples; at most 5 flight tracks | 15 minutes without a position |
| Global Fishing Watch port visits | 1 | 366 days | 200 port-visit events; no positions | n/a (events only) |
| Kystdatahuset open AIS | 1 | 72 hours | 500 position samples | 30 minutes without a position |
| UNCTAD port-call statistics | 10 economies, 5 years per selection | annual periods | aggregates only | n/a |
| Movement answers (`movement_window`) | 1 | 92 days | only what the windows above stored | as recorded per window |
| Movement monitors | 25 identifiers per monitor | n/a | registry and listing changes only | n/a |

When a source publishes more positions than the bound for one window, the
window stores an evenly spaced subset, records `thinned: true` and the number
published, and never stores the remainder. A window whose samples leave an
interval longer than the gap threshold records that interval as a declared
gap. A window with no positions records `no_coverage_observed`: **absence of
positions is absence of receiver coverage, never evidence that the aircraft or
vessel did not move or was not somewhere.**

## Decisions

| Source | Terms and attribution | Redistribution | Rate limits and authentication | Samples may be stored? | Decision |
| --- | --- | --- | --- | --- | --- |
| **FAA Aircraft Registry** (registry.faa.gov, Aircraft Inquiry by N-number; the Releasable Aircraft Database carries the same fields) | US federal government public record; attribution "Source: FAA Aircraft Registry" | Public record; no restriction on redistribution of registry facts | Anonymous HTTPS; no published limit, the pack fetches one N-number per page within the source budget | Registry state only; no positions | **Adopted** (`faa-aircraft-registry`), keyed by N-number and Mode S code (hex). Entries withheld under the FAA privacy programmes are not acquired (below). |
| **UK CAA G-INFO** (siteapps.caa.co.uk/g-info) | UK Civil Aviation Authority register of civil aircraft; attribution "Source: UK Civil Aviation Authority, G-INFO"; reuse under the CAA's terms for G-INFO data (*verify*: the CAA states the data may be reused for non-commercial purposes with attribution) | Registry facts with attribution; no bulk republication | Anonymous HTTPS; one registration mark per page (*verify* the endpoint and field names) | Registry state only | **Adopted** (`uk-caa-ginfo`), keyed by the G- registration mark with its nationality prefix; ICAO 24-bit address only when G-INFO publishes it. |
| Transport Canada CCAR | Open Government Licence - Canada | Permitted with attribution | Bulk download only (whole register as a file) | Registry state only | **Out of scope for the bounded coverage**: no per-mark query; a whole-register download exceeds the per-source bound. Revisit with a per-mark endpoint. |
| German LBA Luftfahrzeugrolle, French DGAC, other EU registries | Not published as open data, or published only as individual look-ups without a reuse licence | Not granted | n/a | n/a | **Excluded**: no recorded access or reuse right. |
| **OpenSky Network** (opensky-network.org REST API: `/api/flights/aircraft`, `/api/tracks/all`) | OpenSky data is provided for research and non-commercial use; publications must cite the OpenSky paper (Schäfer et al., IPSN 2014) and name the OpenSky Network (*verify* the current terms of use and the research licence) | Not for commercial redistribution; bounded samples cited with attribution | Anonymous access limited to recent data and a daily credit budget; historical flights and tracks need a registered account (`NOESIS_OPENSKY_CREDENTIAL`, *verify*). Flights by aircraft accept at most a 2-day interval (*verify*) | **Yes, bounded**: one named ICAO 24-bit address and one window of at most 48 hours per selection; no `/api/states/all` area query; Impala historical database access is not used | **Adopted under the research licence** (`opensky-aircraft-samples`). Flight records (first/last seen, estimated departure and arrival airports) are stored as source-published calls with OpenSky's "estimated" qualifier. |
| ADS-B Exchange, Flightradar24, FlightAware | Commercial terms forbid storage beyond the session or redistribution (ADS-B Exchange API terms prohibit republication; FR24 and FlightAware terms prohibit scraping and redistribution) | Not granted | Keyed commercial APIs | No | **Excluded**. |
| **Global Fishing Watch** (Vessels API and Events API v3) | GFW API terms: non-commercial use; data CC BY-NC 4.0; attribution "Global Fishing Watch" with the dataset version (*verify*) | Non-commercial with attribution | Bearer token `NOESIS_GFW_API_TOKEN`; per-token limits (*verify*) | **Port-visit events only** for one named GFW vessel id and a window of at most 366 days; GFW's own confidence level is stored as published; no tracks | **Adopted** (`gfw-port-visits`). **Vessel identity is not re-acquired**: GFW self-reported identity segments are the Fisheries pack's records (`src/ingestion/fisheries_sources.py`, #2222) and are read and cited from the Fisheries store. Fishing-effort data belongs to Fisheries. |
| **Kystdatahuset** (Norwegian Coastal Administration open AIS, kystdatahuset.no) | Norwegian Licence for Open Government Data (NLOD 2.0) with attribution "Kystverket" (*verify*) | Permitted with attribution | Registered-user token `NOESIS_KYSTDATAHUSET_TOKEN` (*verify*); the pack issues one MMSI and one window per page | **Yes, bounded**: one named MMSI (optionally stated with its IMO number) and one window of at most 72 hours per selection | **Adopted** (`kystdatahuset-ais`) as the approved public AIS source for per-vessel samples. |
| **UNCTAD port-call statistics** (UNCTADstat, derived from AIS by MarineTraffic for UNCTAD) | UNCTADstat terms: free reuse with attribution "UNCTADstat" (*verify*) | Permitted with attribution | Anonymous bulk CSV; filtered to declared economies and years | **Aggregates only**: number of port calls and median time in port per economy, year and market segment, never disaggregated into vessels | **Adopted** (`unctad-port-calls`) as an aggregate. |
| NOAA MarineCadastre AIS | US public domain | Permitted | Bulk daily or zone files only | Would require bulk download to extract one vessel | **Excluded**: bulk area downloads only; the per-vessel bound cannot be met without mirroring. |
| Danish Maritime Authority AIS | Open data (DMA terms) | Permitted with attribution | Daily whole-area files only | Same as NOAA | **Excluded**: bulk area files only. |
| AISHub | Data sharing requires contributing a receiver feed; redistribution of the aggregated feed is restricted to members' own use | Not granted | Member key | No | **Excluded**. |
| MarineTraffic, VesselFinder, MyShipTracking | Commercial terms forbid scraping, storage and redistribution | Not granted | Keyed commercial APIs | No | **Excluded**. |

`src/ingestion/osint_movement_sources.py` holds these decisions as
`PROVIDER_CONTRACTS`, `EXCLUDED_SOURCES` and `LIVE_VERIFICATION`, and the
`movement_source_contracts` tool returns them.

## Privacy and person restrictions

- **Opted-out aircraft.** Aircraft on the FAA Limiting Aircraft Data
  Displayed (LADD) list, those using a Privacy ICAO Address (PIA), and any
  identifier the operator records as opted out are held in a refusal list
  (`osint_movement_privacy_refusals`, filled from each source's
  `privacy_refusals` declaration and from registry pages that state the entry
  is withheld). A selection naming such an identifier is refused before any
  request, a withheld registry entry is not stored, and a movement question
  about it is refused with `privacy_opt_out`. The LADD list itself is
  distributed to data vendors under agreement and is **not** shipped; an
  operator who holds it records it through the same declaration.
- **Aircraft registered to a natural person.** The registrant is stored only
  as published, flagged `natural_person`, never indexed as an identifier and
  never used as a lookup key; movement answers withhold the name. Positions of
  an aircraft whose current registry record names a natural person are refused
  at projection (`private_aircraft_refused`): an individual's aircraft
  movements are an individual's movements.
- **Person-keyed identifiers.** Names of pilots, crew or owners who are
  natural persons, e-mail addresses, handles and `person:` ids are not movement
  keys; any request keyed on them is refused with `person_identifier_refused`.
- **Organisations only.** Operators and owners that are organisations may be
  matched to `canonical_entities` as reviewable candidates; natural persons are
  never matched or resolved.

## How the OSINT review gate and abuse analysis apply

The movement tools are an extension of the review-gated tier
(`osint-review-gate.md`), assessed in `osint-abuse-analysis.md` ("Aircraft and
vessel movements"):

- `NOESIS_OSINT_MOVEMENTS` (off by default) is the feature flag: without it no
  movement tool except `movement_source_contracts` is served.
- The position-bearing tools (`movement_window`, `movement_calls`) are also
  behind the existing review gate (`NOESIS_OSINT_GATED_TOOLS`) and are named in
  `src/osint/investigations.py` (`MOVEMENT_GATED_TOOLS`), so a workflow step
  calling them needs the `osint-review` gate. They require the
  `knowledge:osint:movements` scope and a stated purpose, and every request is
  written with its purpose to the movement request log.
- Criterion 1 (purpose limitation in code): answers report registry state,
  bounded samples and calls with coverage caveats, and never infer behaviour,
  predict routes, build patterns of life or derive a sanctions-evasion or
  compliance verdict.
- Movement subscriptions are refused: monitors cover registry revisions,
  identity decisions and sanctions listing changes only.

## What this decision does not cover

Real-time tracking, alerting on positions, area surveillance, prediction and
any use against individuals are out of scope for the pack (`exclusions` in
`packs/osint/pack.json`) and are not unlocked by any flag.
