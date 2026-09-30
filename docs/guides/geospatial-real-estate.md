# Geospatial real estate: transactions, land parcels and price indices

The optional `real-estate` feature of the Geospatial pack (default off, #2228)
takes a place or a parcel to the property transactions, house price indices and
cadastral parcels publishers released, each with its release vintage, revisions
and citation. It deepens the housing layer ([housing guide](geospatial-housing.md))
and reuses its record owner, scopes and namespaces.

It never produces a valuation, price estimate, per-square-metre price, owner or
party profile, re-identification of a person or investment advice. Prices and
indices are shown as published, side by side, with currency, unit and period.

## Sources

| Source | Path | What becomes a record |
| --- | --- | --- |
| HM Land Registry Price Paid Data | `real-estate` connector, monthly change file, declared postcode districts | transactions; `A`/`C`/`D` rows are added, changed and withdrawn revisions |
| UK House Price Index | `real-estate` connector, one full file per release, declared GSS codes | index, average price and sales volume observations per release vintage |
| DVF géolocalisées | `real-estate` connector, one commune-year file per semi-annual release | mutations with one published value each and every parcel id they name; a mutation missing from a later release is a dated removal |
| Eurostat `prc_hpi_q` | `real-estate` connector through the existing `EurostatConnector` | index observations with flags; the cube's updated stamp is the vintage; a rebase is a new series edition |
| INSPIRE Cadastral Parcels (France, Nordrhein-Westfalen) | existing `wfs` connector, pinned bbox and property list | parcels and their revisions, geometries in the Geospatial store in source CRS with the transform recorded |

The sources ship in the `geospatial-real-estate` source pack
(`packs/geospatial/source_packs/geospatial-real-estate.json`). Access,
licences, identifiers, reuse conditions and the bounded coverage are in the
[source audit](../roadmaps/geospatial-real-estate-source-audit.md). Every source
is `unverified-live` until the dated live run (#2519); the offline evidence is
authored and fictional.

## Enabling

Select the feature in the Geospatial bundle (`features: ["real-estate"]`). It
binds `geospatial.real-estate` plus feature query, spatial relation and place
resolution from `geospatial.core`, `legal.works`, subscriptions and the source
runtime. Acquisition runs through the shared source-pack tools; parcel runs are
held to the declared bounding box.

## Journey

1. `real_estate_source_contracts`, `real_estate_readiness` - decisions and counts.
2. `propose_real_estate_matches` - exact matches from shared identifiers (DVF
   `id_parcelle` = INSPIRE `nationalCadastralReference`; postcode, INSEE, GSS
   and GEO codes a place carries); address- and geometry-derived candidates for
   review (`review_real_estate_match`, `revert_real_estate_match`). A parcel
   revised after a match is flagged; `rematch_real_estate_parcel` records a new
   match and keeps the old one as superseded. Unmatched transactions stay
   queryable by place code.
3. `real_estate_place_as_of` / `real_estate_parcel_as_of` - what was published
   by the date (release publication date): transactions with the revision in
   force (withdrawn and removed shown as such), indices per source and edition,
   parcels with revision history and geometry reference, citations everywhere,
   `none_on_record` when there is nothing.
4. `link_real_estate_records`, `cite_real_estate_legal_work`,
   `list_real_estate_links` - links by parcel reference, geography code,
   dataset code or explicit citation only; containment in a housing zone is
   context, not a link.
5. `create_real_estate_monitor`, `run_real_estate_monitor`,
   `poll_real_estate_monitor` - new transactions, revisions and withdrawals,
   new index vintages and parcel revisions for followed places and parcels.

## Personal data

No selected source publishes an owner, buyer or seller; a file with such a
column is refused and a WFS property outside the declared list (for example a
rights holder) is dropped in the adapter. DVF answers carry the DVF notice: no
re-identification of the persons concerned and no indexing by external search
engines.

## Tests

`tests/unit/real_estate/` (sources, identity, links, answers, monitoring) and
`tests/unit/domains/test_real_estate_acceptance.py` (offline place-to-transactions
journey through the MCP tools with sockets blocked). Regenerate fixtures with
`python -m tests.unit.real_estate.fixture_builder`.
