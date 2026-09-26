# Source expansion candidates

Status: proposed, 2026-09-25. These candidates extend existing Noesis
capabilities where practical; they do not assume four new domain packs.

Tracking: [#1746](https://github.com/Ikey168/Noesis/issues/1746).



## GitHub implementation issues

- [ ] [#1747](https://github.com/Ikey168/Noesis/issues/1747) — Extend Research and Technical with patent-family and legal-event sources.
- [ ] [#1748](https://github.com/Ikey168/Noesis/issues/1748) — Extend existing company identity work with official registries and LEI ownership data.
- [ ] [#1749](https://github.com/Ikey168/Noesis/issues/1749) — Extend the Technical pack with non-software standards and certification evidence.
- [ ] [#1750](https://github.com/Ikey168/Noesis/issues/1750) — Add bounded public transport operations to Geospatial and an existing source profile.

## Implementation status (2026-09-26)

All four tracks have a fixture-tested source path; see
[the source expansions guide](../guides/source-expansions.md). Live access is
`unverified-live` for EPO OPS (credentials), GLEIF, ISO Open Data and VBB GTFS.
Espacenet, WIPO PATENTSCOPE, official national registers, ETSI, certification
registries, VBB GTFS Realtime and rail/flight-status feeds are recorded as not
implemented, each with a reason in its provider contract.

## Patents and inventions

Extend Research/Technical with patent publications, family relations, inventor
and applicant identities, citations and source-supported legal events. EPO OPS
is a concrete machine interface, but requires credentials and sets non-paying
usage at up to 4 GB/week; respect its terms and fair-use limits. Espacenet
discovery and OPS data can overlap, so validate the distinct user value before
implementing both. WIPO PATENTSCOPE is a source candidate whose stable machine
access and reuse terms must be established. Do not infer patent validity,
enforceability or freedom-to-operate from bibliographic/legal-event metadata.

Sources: [EPO OPS](https://www.epo.org/en/searching-for-patents/data/web-services/ops),
[WIPO PATENTSCOPE](https://patentscope.wipo.int/).

## Company registries and ownership

Extend existing company identity and relationship work. OpenCorporates is
already implemented as provider enrichment in #1483, and market company/product
relationships exist in #1674. Add GLEIF LEI records/relationship data and
selected official national registers where documented access, history and
rights support the use case. Preserve reporting exceptions, jurisdiction,
successors and conflicting ownership assertions. Do not describe OpenCorporates
as an authoritative substitute for national registers.

Sources: [GLEIF API](https://www.gleif.org/en/lei-data/gleif-api),
[GLEIF data access](https://www.gleif.org/en/lei-data/access-and-use-lei-data),
[OpenCorporates API](https://api.opencorporates.com/documentation/API-Reference).

## Standards and certification

Extend the existing Technical standards capability beyond software standards
and RFCs. ETSI standards/catalogue and IPR-declaration records are candidates.
ISO/IEC catalogue access and text reuse require explicit rights validation;
metadata discovery does not license standards text redistribution. Name
specific official certification registries and schemes before implementing
them. Represent standards, editions/amendments, conformance claims, certificates
and issuing bodies distinctly; link to existing Products/Technical records by
evidence rather than inference.

Source candidates: [ETSI](https://www.etsi.org/standards),
[ETSI IPR database](https://ipr.etsi.org/),
[ISO catalogue](https://www.iso.org/standards.html).

## Transport operations

Start with GTFS Schedule and GTFS Realtime from a named Berlin-area agency.
Project feed stops/routes into the existing Geospatial store; retain schedule
versions separately from realtime observations. GTFS models routes, trips,
stops, stop times, calendars, trip updates and service alerts. Audit official
rail and flight-status providers separately before promising access or historic
coverage. A current timetable is not historical evidence of actual service.

Sources: [GTFS overview](https://gtfs.org/documentation/overview/),
[GTFS Schedule reference](https://gtfs.org/documentation/schedule/reference/).

## Existing overlap and implementation issues

The issue tracker defines one acceptance-oriented track per candidate. Each
must select bounded providers, preserve source-specific rights and evidence,
reuse existing stores, and distinguish fixture coverage from bounded live
acceptance. Company, standards and transport work are explicitly framed as
extensions to existing capabilities unless the scoped implementation proves a
separate lifecycle is necessary.
