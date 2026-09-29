# BaFin capital-market notices: source-contract audit and bounded coverage (BF01, #2120)

Tracking issue: #2106. This audit records, for each BaFin-related source, the
access route, export formats, terms, rate limits, stable identifiers, how
corrections, withdrawals and removals appear, how long entries stay listed,
which timestamps the source provides and which one the Market as-of cutoff
uses, and the access decision the Market pack's optional `bafin-notices`
feature implements.

**Verification status.** No live request was made while writing this audit.
The build environment has no network access to the providers. Every claim
marked **(verify)** comes from the providers' public documentation and portal
behaviour as known at the time of writing. Each one must be checked against
the live terms and services before live acquisition is accepted (BF13, #2132).
The adapters in `src/ingestion/bafin_sources.py` parse the documented shapes
with **declared column mappings**, so a changed header is a declaration change,
not a code change. The offline fixtures under `tests/fixtures/bafin_notices/`
are authored and name fictional issuers, fictional companies and fictional
persons. None of them is captured data.

`PROVIDER_CONTRACTS` and `LIVE_VERIFICATION` in `src/ingestion/bafin_sources.py`
restate these decisions in code; every implemented source is `unverified-live`
until a dated live run is recorded under this directory.

## Summary

| Source | Endpoint | Format parsed | Decision |
| --- | --- | --- | --- |
| BaFin voting-rights database (Stimmrechtsmitteilungen, `AnteileInfo`) | `https://portal.mvp.bafin.de/database/AnteileInfo/` | result-list CSV export (`bafin-voting-rights-csv`) | `implement` (unverified-live) |
| BaFin managers'-transactions database (Directors' Dealings, `DealingsInfo`) | `https://portal.mvp.bafin.de/database/DealingsInfo/` | result-list CSV export (`bafin-dealings-csv`) | `implement` (unverified-live); retention after removal **not confirmed**, so person data is withdrawn when a transaction leaves the listing |
| Net short positions (Bundesanzeiger, Art. 11 SSR / § 30i WpHG publication) | `https://www.bundesanzeiger.de/pub/de/nlp` | position-list CSV download (`bundesanzeiger-short-positions-csv`) | `implement` (unverified-live); the download may be session-bound **(verify)** |
| BaFin company database (Unternehmensdatenbank, `InstInfo`) | `https://portal.mvp.bafin.de/database/InstInfo/` | result-list CSV export (`bafin-company-csv`) | `implement` (unverified-live), bounded to declared BaFin IDs |
| BaFin warnings on unauthorised business and published measures | `https://www.bafin.de/` (RSS feeds) | RSS 2.0 (`bafin-notices-rss`) | `implement` (unverified-live); RSS is never a complete listing, so no removal is inferred from it |
| Issuer publications via the Unternehmensregister | `https://www.unternehmensregister.de/` | n/a | `not implemented`: no documented machine interface; the terms restrict automated retrieval **(verify)**. Link-only: a notice keeps a register link when a source states one; officially obtained documents can be recorded with the existing `record_ownership_register_document` tool |
| News-wire distributions of the same notices (EQS, dpa-AFX) | n/a | n/a | `not implemented` (tracker exclusion: licensed redistributions are never mirrored) |

## Timestamps and the as-of cutoff

| Source | Event time | Notification time | Publication time | Cutoff clock |
| --- | --- | --- | --- | --- |
| Voting rights | date the threshold was reached, crossed or fallen below (`Datum der Schwellenberührung`) | date of notification to the issuer and BaFin (when the export states it **(verify)**) | date of publication under § 40 WpHG (`Veröffentlichung`) | publication date |
| Managers' transactions | transaction date | date of notification (`Datum der Mitteilung` **(verify)**) | date of publication by BaFin/issuer (`Datum der Veröffentlichung` **(verify)**) | publication date; when the export states none, the notification date is **not** used and the first observation time is the clock |
| Net short positions | position date (`Datum`) | n/a | not stated in the CSV **(verify)** | first observation time (labelled `first-observed`), unless the declaration maps a publication column |
| Company database | licence start/end dates as published | n/a | not stated | first observation time of each revision |
| Warnings and measures | the measure date if the text states it | n/a | RSS `pubDate` | `pubDate` |

**Rule.** A notice is visible to a point-in-time query only when its
publication clock is at or before the query's public cutoff and, if an
acquisition cutoff is given, it was acquired by then. This is the Market
pack's `public_and_acquired` availability policy in
`src/domains/market/asof.py`. A publication *date* without a time counts as
published at the **end** of that day (UTC), which is later than the end of
the day in Germany. So an intraday cutoff on the publication day never sees
the notice, and a date query for that day does. A missing publication date is
never replaced by the event date or the notification date. The notice's
publication clock is then its first observation time, which is conservative:
no look-ahead is possible. A revision of an already published notice that the
source edits silently carries the time it was first observed.

## BaFin voting-rights database (AnteileInfo)

* **Access:** public search over issuers (`Emittent`) that lists, per issuer, the notified
  holdings of voting rights (WpHG §§ 33 ff.). Result lists offer an export link
  (the displaytag parameters `6578706f7274=1&d-<table>-e=1` select CSV) **(verify: parameter names,
  CSV availability and encoding; windows-1252 and `;` separators are assumed and declared per
  source)**. No authentication.
* **Terms:** BaFin website terms of use (`https://www.bafin.de/DE/Service/Impressum/impressum_node.html`)
  **(verify whether automated retrieval and reuse are permitted and whether attribution is required)**.
* **Rate limits:** none documented **(verify)**. The source declares one export per run, a 30 s
  timeout and a 5 MB byte budget.
* **Identifiers:** the database's own notification identifier where the export states one
  **(verify)**. Otherwise the key is issuer ISIN + notifier + event date + publication date + a
  hash of the stated percentages (`source_id_basis = derived`). Issuers carry an ISIN
  **(verify: the export may state the issuer name only; the declaration can then map the issuer
  to its ISIN through the bounded issuer set)** and, since 2018 notifications, the issuer LEI.
* **Content (per WpAIV notification form, as the database presents it):** issuer, notifier (natural
  or legal person, seat, country), thresholds touched, percentages split into § 33 (shares),
  § 38 para. 1 no. 1 and no. 2 (instruments) and § 39 (total), the previous notification's total,
  and the complete chain of controlled undertakings ("Vollständige Kette der Tochterunternehmen",
  starting with the top controlling person) with each member's stated percentages **(verify: the
  chain may only be in the published notification text, not in the export; the adapter maps an
  optional chain column in the form's own table layout)**.
* **Corrections and withdrawals:** a corrected notification is published again and marked as a
  correction ("Korrektur der Veröffentlichung vom …") **(verify how the database marks it)**. The
  adapter maps a correction reference (the corrected notification's identifier) or, when only a
  flag and the corrected publication date are stated, links to the notice of the same issuer and
  notifier published on that date. When no unique notice matches, the link stays unresolved and is
  reported. Corrections **supersede**: queries show one notification per correction chain, the
  latest one published by the cutoff. A withdrawn notification is kept and marked `withdrawn`.
* **Retention:** the database shows the last notified holding per holder **(verify)**; an entry
  that leaves a complete issuer listing is marked `no_longer_listed` with the date observed, never
  deleted and never read as a zero holding.
* **Decision:** `implement` (unverified-live).

## BaFin managers'-transactions database (DealingsInfo)

* **Access:** public search by issuer or period with a CSV export like AnteileInfo **(verify)**.
* **Content (Art. 19 MAR notification template, Implementing Regulation (EU) 2016/523):** name of
  the person discharging managerial responsibilities or closely associated person, position or
  status, initial notification or amendment, issuer name and LEI, instrument type and ISIN, nature of
  the transaction, price(s) and volume(s) with currency, aggregated volume and price, date and place
  of the transaction. Notifications that aggregate several trades list each trade and an aggregate;
  both are stored as published, and nothing is recomputed.
* **Identifiers:** the database's notification identifier **(verify)**; trade rows of one
  notification share it.
* **Corrections:** an amended notification ("Korrektur", MAR template field 4b "amendment")
  references the notification it amends **(verify how the export states it)** and supersedes it.
* **Retention:** BaFin lists notifications for a limited period **(verify the period; issuers
  must keep their own publications available for at least five years under Art. 17(1) MAR, but
  BaFin's listing period is a separate question)**. **Decision on retention: not confirmed.** When a
  transaction leaves a complete listing, the notice stays in Noesis marked `no_longer_listed` with
  the date observed. The production declaration uses the retention policy `withdraw-person-data`:
  the person's name is erased from the person table. Earlier revisions only ever held a
  pseudonymous reference, so nothing personal remains, and the transaction facts (issuer,
  instrument, nature, price, volume, dates, stated role) remain. Switching to `retain` needs a
  human decision recorded here once BaFin's terms are verified.
* **Exclusions:** no insider-sentiment metric, signal or aggregate across persons.
* **Decision:** `implement` (unverified-live).

## Net short positions (Bundesanzeiger)

* **Access:** the Bundesanzeiger publishes net short positions of at least 0.5 % of the issued
  share capital (Art. 9 and 11 of Regulation (EU) No 236/2012, SSR). Current and historical
  positions can be downloaded as CSV **(verify)**. The download link is generated per session
  (a Wicket session URL) **(verify)**. The runtime transport keeps no cookies and refuses
  cross-host redirects, so a live run may fail with `schema_drift`, since an HTML page is returned
  instead of CSV. That is reported, never scraped around.
* **Terms:** Bundesanzeiger Verlag terms of use **(verify whether automated download and reuse
  are permitted)**.
* **Content:** position holder, issuer, ISIN, position in %, position date.
* **Identifiers:** none per row. The key is holder + ISIN + position date.
* **Publication end:** a position that falls below 0.5 % is published once with its value (for
  example 0.49 %) and then leaves the current list. Both are recorded: a published value below
  0.5 % is a `publication_ended` marker, and a holder/issuer pair that leaves a complete current
  listing gets a `no_longer_listed` observation. Queries answer "below publication threshold or
  closed", **never 0 %**. A position that drops out is never extrapolated.
* **Timestamps:** the CSV states the position date only **(verify)**. The publication clock is
  the first observation time unless the declaration maps a publication column.
* **Aggregation:** only a sum of the published positions, labelled as such, is ever shown. There
  is no "total short interest".
* **Decision:** `implement` (unverified-live).

## BaFin company database (InstInfo)

* **Access:** public search of supervised and authorised entities (credit institutions,
  financial services institutions, payment and e-money institutions, investment firms,
  management companies, insurers). Result-list export **(verify)**; the detail view lists each
  authorisation with its start and end dates.
* **Identifiers:** the BaFin identifier (`BaFin-ID`, numeric **(verify)**), name, place, country;
  an LEI when the database states one **(verify)**. BaFin IDs are normalised the same way on both
  sides of every match (digits only, leading zeros removed).
* **Changes:** the database shows the current state. Changes between observations are appended as
  revisions, each with its first-observation time. An entity that leaves a complete listing is
  marked `no_longer_listed`, never deleted.
* **Bounded coverage:** entities linked to the bounded issuer set and their groups, declared by
  BaFin ID in the source declaration.
* **Decision:** `implement` (unverified-live).

## Warnings on unauthorised business and published measures

* **Access:** BaFin publishes warnings (e.g. under § 37 para. 4 KWG, § 6 para. 2 WpHG, § 10
  para. 7 VermAnlG) and measures and sanctions (e.g. § 123 and § 124 WpHG) as news items. They are
  available through RSS feeds on `www.bafin.de` **(verify feed URLs and whether separate feeds
  exist for warnings and for measures)**.
* **Content:** title, link, `pubDate`, description. Named entities are **source strings**: the
  declaration gives a pattern that extracts the named company from the title (for example
  `Warnung vor der <name>`). When the pattern does not match, the item keeps no entity string, and
  `named_entities` is listed under `unknowns`. The legal basis is parsed from the text with the
  shared statutory-citation parser (`src/kb/legal_citations.py`) and the EU-act parser, so
  `§ 37 Abs. 4 KWG` resolves to KWG `§37/abs4`.
* **Removals:** BaFin removes warnings from its site after some time **(verify)**. An RSS feed is
  never a complete listing, so no removal is inferred from it. A declared complete listing
  document can record removals, which are kept as removal observations and never deleted.
* **Attribution:** a warning naming a string is **never** attributed to a canonical entity
  without a reviewed identity match (BF07).
* **Decision:** `implement` (unverified-live).

## Unternehmensregister (issuer publications)

* The register publishes issuer notifications (voting rights, managers' transactions) as part of
  the regulated information system. It has no documented API, sessions and captchas guard access,
  and the terms of use restrict automated retrieval **(verify)**.
* **Decision:** `not implemented`. Notices keep a register link when a source states one, and an
  officially obtained document can be recorded with the existing Corporate Ownership
  `record_ownership_register_document` tool (user-supplied, digest retained).

## Bounded coverage

* **Issuer set:** the DAX 40 constituents on the index review effective date the operator records
  in the source declaration, starting with the ISINs declared in
  `config/source_packs/market-bafin.json` **(verify the constituent list and every ISIN against
  the index provider's published composition before a live run)**. Only notices for declared
  ISINs are stored. Rows for other issuers are counted in the page receipt as `out_of_scope` and
  never dropped silently.
* **Date window:** publications on or after 2025-01-01, declared per source as `window.from`
  **(verify BaFin's listing periods, which bound what can still be acquired)**.
* **Company database:** declared BaFin IDs only.
* **Warnings and measures:** every item in the declared feeds within the window.

## Personal data

Managers and closely associated persons, and notifiers or short sellers who are natural persons,
are stored only as the source publishes them. Guardrails:

* Person names in managers' transactions are held in a separate person table keyed by a
  pseudonymous reference scoped to the notice and its issuer. Notice revisions only hold the reference, so a
  person name can be withdrawn without rewriting history.
* Natural persons are matched only **within the issuer context**. There is no cross-issuer person
  candidate unless a reviewer proposes one, and every candidate stays `proposed` until reviewed.
* No profile across issuers, no sentiment, no scoring.
