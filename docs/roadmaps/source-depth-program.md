# Source depth program

Status: wave 4 audited offline, 2026-11-01 (DT01, DP01, DS01, DG01; no source
live). Tracker:
[#2825](https://github.com/Ikey168/Noesis/issues/2825). Decision:
[ADR-006](../architecture/decisions/ADR-006-source-depth.md). Overlay:
[`packs/depth.json`](../../packs/depth.json). Coverage program:
[domain coverage program](domain-coverage-program.md).

A covered subdomain's **depth** is the number of distinct publishers behind the
providers that name it. Below two, an answer can only repeat one publisher. It
cannot show a second figure, a disagreement or a corroboration. Such a
subdomain is **thin**. This program brings every thin subdomain to depth two,
or records why it cannot.

## How depth is counted

- **Computed, not claimed.** `packs/depth.json` names the sources each
  classified provider acquires. `tests/unit/composition/test_source_depth.py`
  resolves every source in every source pack to one publisher and computes
  depth per subdomain. The thin table and the depth-two watchlist below must
  equal what it computes.
- **Organisations, not feeds.** Services of one organisation count once. A
  republisher counts as the publisher it derives from (ADR-006, decision 2).
- **Offline depth.** Fixture-tested sources in a source pack count. Live depth
  counts only sources whose track has passed live validation, and the two are
  reported separately.
- **Necessary, not sufficient.** Two publishers can share an origin chain.
  Record-level independence stays with the Evidence Independence Graph.

## Where depth stands (2026-10-07)

Counting providers made coverage look thin: 54 of the 72 covered subdomains have
one provider. Counting publishers shows most of them are not thin.

| Depth | Covered subdomains |
| --- | --- |
| 1 (thin) | 4 |
| 2 | 8 |
| 3–4 | 37 |
| 5 or more | 23 |

The 17 gaps of the coverage program have no depth yet. A wave 3 track that
covers a gap with one publisher adds a row to the thin table in its own change.

## Track rules

Each track follows the pattern of the coverage tracks:
1. **Source audit.** Name the candidate publishers and record access, licence
   and redistribution terms before any code. Record the origin chain, including
   whether the candidate republishes the existing publisher's figures. The
   audit is `docs/development/<dir>-evidence/depth-audit.md`.
2. **Source.** Add the source to the existing provider's source pack and
   connector, reusing its record shape. Fixture-test it offline. A new shape
   needs a decision record first.
3. **Depth.** Register the publisher in `packs/depth.json` and add the source to
   the provider's list. Remove the thin-table row in the same change. The gate
   fails until both agree.
4. **Live validation.** The source joins its track's "Validate live coverage"
   issue. It counts toward live depth only after that run.

A source whose terms do not allow the intended use is recorded as not
implemented, and the subdomain stays thin.

## Wave 4: thin table

| Subdomain | Domain | Provider | Only publisher | Audit outcome (offline, 2026-11-01) | Audit |
| --- | --- | --- | --- | --- | --- |
| `tourism-hospitality` | Economy and markets | `economics.tourism` | Eurostat | Destatis GENESIS 45412 recommended (same origin chain as Eurostat: depth, not independence); OECD, AfS not implemented | [#2826](https://github.com/Ikey168/Noesis/issues/2826) DT01, [audit](../development/tourism-evidence/depth-audit.md) |
| `patents` | Technology | `technology.patents` | European Patent Office | USPTO (PatentsView / Open Data Portal) recommended after a patent-part provider widening; DPMAregister, WIPO PATENTSCOPE not implemented | [#2827](https://github.com/Ikey168/Noesis/issues/2827) DP01, [audit](../development/patents-evidence/depth-audit.md) |
| `standards-certification` | Technology | `technology.standards` | ISO | W3C TR API recommended (RFC Editor index as alternative) after a catalogue provider widening; NANDO not implemented (new record shape needs a decision record), ETSI not implemented | [#2828](https://github.com/Ikey168/Noesis/issues/2828) DS01, [audit](../development/standards-evidence/depth-audit.md) |
| `places-geometry` | Earth and environment | `geospatial.core` | Land Berlin | BKG VG250 recommended (Berlin outline; shares Land Berlin's origin chain; needs a Land Berlin state-boundary layer, _verify_); GISCO not implemented pending licence clause; OSM not implemented (ODbL share-alike, ring assembly) | [#2829](https://github.com/Ikey168/Noesis/issues/2829) DG01, [audit](../development/geospatial-evidence/depth-audit.md) |

Track risks:
- **Tourism:** Destatis transmits the German figures that Eurostat publishes, so
  it adds a publisher but not an independent origin for the same number. The
  OECD figures share the same national origins.
- **Patents:** USPTO and DPMA need an account or key. Family links must come
  from published identifiers, never inference.
- **Standards:** no standard text is stored, only catalogue metadata and links.
  NANDO would be the first certification-side registry. RFCs may not fit the
  catalogue shape.
- **Places:** the second publisher must cover Berlin, so one place gets two
  geometries with their own vintages. They are never merged.
- **All four:** each recommended second publisher raises depth without an
  independent origin for the same figure or boundary (Destatis→Eurostat;
  USPTO/DPMA records republished in DOCDB; BKG/GISCO compiled from Länder data).
  When a row leaves the thin table, its watchlist note says so.

## Depth-two watchlist

Not a to-do list. These subdomains meet the minimum. They are listed because
depth two often means one publisher per jurisdiction, or independence is
doubtful. The gate keeps this list equal to the computed depth-two subdomains.

| Subdomain | Publishers | Note |
| --- | --- | --- |
| _courts-case-law_ | Free Law Project; Bundesministerium der Justiz | one per jurisdiction (US, DE) |
| _cultural-heritage_ | Deutsche Digitale Bibliothek; Europeana Foundation | Europeana aggregates national aggregators, DDB among them; origins may be shared |
| _fact-checks_ | Google (Fact Check Tools, Data Commons); IFCN | IFCN lists signatories and publishes no fact-checks, so fact-check content has one publisher |
| _financial-markets_ | BaFin; Bundesanzeiger Verlag | both German notices; `market.core` has no publisher until #1780 |
| _industry-business_ | Eurostat; US Census Bureau | one per jurisdiction (EU, US) |
| _medical-devices_ | FDA; European Commission (EUDAMED) | one per jurisdiction (US, EU) |
| _trade_ | Eurostat; UN Statistics Division | Comext and Comtrade give reporter and mirror figures; WITS derives from UNSD |
| _weather_ | Deutscher Wetterdienst; NOAA | one per jurisdiction (DE, US); Open-Meteo derives from DWD |
