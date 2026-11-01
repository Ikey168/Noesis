# Patents: second-publisher depth audit (DP01)

Tracking: [#2827](https://github.com/Ikey168/Noesis/issues/2827) DP01 (wave 4
tracker [#2825](https://github.com/Ikey168/Noesis/issues/2825)) · decision
[ADR-006](../../architecture/decisions/ADR-006-source-depth.md) · plan
[source depth program](../../roadmaps/source-depth-program.md) · recorded
2026-11-01.

`patents` is thin: its only provider, `technology.patents`, reads one publisher,
the European Patent Office (`epo-ops-patents` in `research-discovery` 1.4.0,
connector `epo-ops`, record contract `noesis-patent-part-v1`, store
`src/kb/patents.py`). No patents evidence directory existed before this audit
(none under another name either); this directory is new.

**It was written without network access.** The publisher hosts
(`search.patentsview.org`, `patentsview.org`, `data.uspto.gov`, `www.uspto.gov`,
`register.dpma.de`, `www.dpma.de`, `patentscope.wipo.int`, `www.wipo.int`) are
blocked by this runtime's egress proxy (verified 2026-11-01). Terms, endpoints,
key requirements and rate limits come from the publishers' documentation as the
author knows it. Every item marked _verify_ must be checked against the live
pages, the live terms and a real response before the first dated live run. None
of the sources below is live; none counts toward live depth (ADR-006,
decision 6). Credential variables are named here; no value is ever recorded.

## Summary

| Candidate | Publisher | Registry entry | Adds a publisher | Adds an independent origin | Shape fit | Decision |
| --- | --- | --- | --- | --- | --- | --- |
| USPTO PatentsView PatentSearch API (or the USPTO Open Data Portal) | United States Patent and Trademark Office | new (`uspto`) | yes | yes for US publications (USPTO is the origin; EPO DOCDB republishes them) | biblio and claims fit; family is not published as DOCDB family; contract needs a provider widening | **recommended** for `technology.patents` |
| DPMAregister (DPMAconnectPlus) | Deutsches Patent- und Markenamt | new (`dpma`) | yes | yes for DE publications and DE legal status | fits biblio and legal; machine access needs a separate agreement | `not-implemented` (this wave) |
| WIPO PATENTSCOPE | World Intellectual Property Organization | new (`wipo`) | yes | yes for WO (PCT) publications | would fit biblio | `not-implemented`: web services are a paid subscription and the terms restrict bulk retrieval |

## Origin chain (ADR-006, decision 5)

EPO's DOCDB and INPADOC databases, which OPS serves, are built from bibliographic
and legal-status data that national and regional offices exchange with the EPO.
For an EP publication the EPO is the origin. For a US publication the origin is
the USPTO; for a DE publication it is the DPMA; for a WO publication it is WIPO.
OPS republishes those records in DOCDB form.

Consequences:

- A second office adds a publisher and, **for its own publications**, the
  original origin of records that OPS today carries as a republisher. For the
  same US publication, the USPTO record and the OPS record are one origin
  published twice (the OPS copy derived from the USPTO's), not two
  corroborating sources. Record-level independence stays with the
  [Evidence Independence Graph](../../../contracts/noesis-evidence-independence-v1.md).
- Where the two disagree (title normalisation, applicant name format, a kind
  code, a missing priority claim, a legal event OPS has not yet received) both
  records are kept with their own revisions; nothing is reconciled.
- For EP publications a US or DE source adds nothing; depth for the subdomain
  rises, depth for a single EP document does not.

## Families: published identifiers only

The store keeps family membership as "INPADOC membership as asserted by OPS for
one publication at one observation" (`src/kb/patents.py`). A second office
must not change that. Families are matched only through identifiers a
publisher states:

- the **DOCDB/INPADOC family id** that OPS states (`family_id` in the biblio
  part, `family-id` on family members);
- **priority claims** and **application numbers** as each office publishes them
  (the OPS biblio part already keeps `priorities` and `application`);
- **US continuity data** (parent and child applications) and **foreign priority
  claims** as the USPTO publishes them (_verify_ field names per interface).

A USPTO publication is in an EPO family only if OPS lists it as a family member,
or the reviewer accepts a link whose basis is an identical published priority or
application number. Nothing is inferred from titles, applicants, inventors,
classifications or text similarity. A USPTO source writes no `family` part:
USPTO publishes no DOCDB family, so the family part for a US publication stays
whatever OPS asserts, or `not_acquired`.

## Per-candidate contract

### USPTO: PatentsView PatentSearch API or the Open Data Portal

| Item | Record |
| --- | --- |
| Endpoint | PatentsView PatentSearch API, `https://search.patentsview.org/api/v1/patent/` (granted patents) and `.../publication/` (pre-grant publications), JSON, query by patent or document number (_verify_ paths, versions and whether PatentsView remains a separate service or has moved under the Open Data Portal); USPTO Open Data Portal, `https://data.uspto.gov/` and its API host (`api.uspto.gov`, _verify_), for application data, continuity, foreign priority and transaction history (_verify_) |
| Access | API key in an `X-Api-Key` header for both (_verify_); the Open Data Portal key needs a USPTO.gov account with identity verification (_verify_). Proposed variable names: `NOESIS_PATENTSVIEW_API_KEY` for PatentsView, `NOESIS_USPTO_ODP_API_KEY` for the Open Data Portal (to be listed in `docs/guides/connector-api-keys.md` by the delivery change) |
| Cost | free (_verify_) |
| Rate limits | PatentsView: 45 requests per minute per key (_verify_); Open Data Portal: per-key limits stated on the portal (_verify_) |
| Licence | PatentsView data: CC BY 4.0 (_verify_); USPTO bibliographic data: US government work, not subject to US copyright (17 U.S.C. 105) and offered without restriction (_verify_ the Open Data Portal terms) |
| Redistribution | permitted with attribution for PatentsView (_verify_); USPTO public data without restriction (_verify_); evidence bundles carry the attribution |
| Revision model | PatentsView is a periodic (quarterly, _verify_) disambiguated snapshot; disambiguated assignee and inventor ids are PatentsView's own inferences and are **not** stored as identities (applicant and inventor names stay provider strings, as for OPS). The Open Data Portal is updated daily (_verify_) |

Fit with `noesis-patent-part-v1` and `src/kb/patents.py`:

- **Holds.** A US publication has a DOCDB-form number (`US.11000000.B2`,
  `US.2021000001.A1`) that matches the contract's `publication` pattern. The
  `biblio` part (application, priorities, titles, abstracts, applicants,
  inventors, classifications, citations) and the `claims` part map from
  PatentsView fields. Legal events: the Open Data Portal's transaction history
  is prosecution events, not legal status; it may fill `legal` only with that
  meaning recorded (_verify_), otherwise `legal_status` stays `not_acquired`.
- **Does not yet hold.** (a) The contract fixes `provider` to `epo-ops`; it must
  allow a second provider value. (b) `publication_id` and `patent_part_current`
  are keyed by docdb number and part without the provider, so a USPTO and an
  OPS observation of the same publication would overwrite each other's current
  pointer. The current pointer and revisions must be keyed per provider, as
  `src/kb/standards.py` already does (`standard_current` keyed by provider).
  (c) `family.asserted_by` is hard-coded to "EPO OPS (INPADOC)".
- **Judgement.** These are a provider widening of the same shape (parts of a
  DOCDB publication with per-provider revisions), not a new record shape, so no
  decision record is needed under ADR-006 decision 7; the delivery issue bumps
  the contract (`noesis-patent-part` 1.1.0 or 2.0.0, _decide in delivery_) and
  migrates the current-pointer key. If review judges it a new shape, a
  decision record comes first.

### DPMA: DPMAregister

| Item | Record |
| --- | --- |
| Endpoint | DPMAregister web application `https://register.dpma.de/` (interactive); machine access through DPMAconnectPlus, an XML web service for register data (_verify_ name, endpoint and scope); bulk data products through DPMAdatenabgabe (_verify_) |
| Access | DPMAconnectPlus needs a registration and a written usage agreement with the DPMA (_verify_). Proposed variable name `NOESIS_DPMA_CONNECT_CREDENTIALS` |
| Cost | DPMAconnectPlus free of charge after agreement (_verify_); bulk products may carry fees (_verify_) |
| Rate limits | set by the agreement (_verify_) |
| Licence and redistribution | register data are official publications; reuse terms are set by the DPMA terms of use and the agreement (_verify_). The DPMAregister terms prohibit automated queries of the web application outside the offered interfaces (_verify_); it is never scraped |
| Origin chain | DPMA is the origin for DE publications and the authoritative register for their legal status; INPADOC legal events for DE documents derive from DPMA data |

Fit: DE publications fit `biblio` and `legal` (the authoritative German legal
status, kept as events, never as an inferred validity). Same provider-widening
change as USPTO.

Decision: **`not-implemented` this wave.** Machine access depends on a
per-operator agreement that cannot be validated offline and may restrict
redistribution in evidence bundles. It is the strongest candidate for a later
amendment (authoritative DE legal status), once an operator holds the
agreement and its terms are recorded here.

### WIPO PATENTSCOPE

| Item | Record |
| --- | --- |
| Endpoint | PATENTSCOPE search `https://patentscope.wipo.int/search/` (interactive); PATENTSCOPE web services (SOAP/REST, _verify_) |
| Access | web services are a paid annual subscription (as `src/ingestion/patent_sources.py` already records under `PROVIDER_CONTRACTS["wipo-patentscope"]`) (_verify_ current price and tiers) |
| Licence and redistribution | the PATENTSCOPE terms of use restrict systematic and bulk retrieval and automated querying of the search interface (_verify_ wording) |
| Origin chain | WIPO is the origin for WO (PCT) publications; national collections in PATENTSCOPE are republished office data |

Decision: **`not-implemented`**, reason unchanged from `patent_sources.py`:
no permitted free, stable machine interface for the required records; the search
interface is never scraped. WO publications remain reachable as OPS (DOCDB)
records.

## Recommendation

1. **Implement the USPTO in `technology.patents`**, through the PatentsView
   PatentSearch API, or through the USPTO Open Data Portal if PatentsView has been
   folded into it by the time of verification (_verify_). Proposed source id
   `uspto-patents`, publisher string
   `United States Patent and Trademark Office (PatentsView)` (or `(Open Data
   Portal)`), credential `NOESIS_PATENTSVIEW_API_KEY` or
   `NOESIS_USPTO_ODP_API_KEY`. Bounded selection like OPS: up to 25 explicitly
   selected US publications, parts `biblio` and `claims` (and `legal` only with
   the transaction-history meaning recorded); no `family` part.
2. Before the source: the contract widening above (provider value,
   per-provider current pointer, `asserted_by` from the provider). No family is
   ever inferred.
3. In the same delivery change: register `uspto` in `packs/depth.json`
   (`match`: `United States Patent and Trademark Office`), add the source to
   `providers.technology.patents.sources`, remove the `patents` row from the thin
   table, and add `patents` to the depth-two watchlist ("one office per
   jurisdiction; for US documents OPS republishes the USPTO record").
4. DPMAregister and WIPO PATENTSCOPE: `not-implemented`, with the reasons above.

Depth after the recommended delivery: offline 2 (EPO, USPTO), live 0 until the
track's "Validate live coverage" run.

## Live verification

| Candidate | Status | Checked | Evidence |
| --- | --- | --- | --- |
| USPTO (PatentsView / Open Data Portal) | `unverified-live` (proposed) | - | none; publisher hosts blocked 2026-11-01 |
| DPMAregister | `not-implemented` | - | access by agreement; terms not established (_verify_) |
| WIPO PATENTSCOPE | `not-implemented` | - | paid web services; bulk retrieval restricted (_verify_) |
