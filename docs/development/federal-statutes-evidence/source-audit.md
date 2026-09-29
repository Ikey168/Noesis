# Federal statutes: source-contract audit and bounded statute set (FL01, #2107)

Tracking issue: #2105. This audit records, for each German federal law source,
the access route, authentication, terms, formats, stable identifiers, how
versions and validity are expressed, and the access decision the Legal pack's
`federal-statutes` feature implements.

**Verification status.** No live request was made while writing this audit
(the build environment has no network access to the providers). Every claim
marked **(verify)** is based on the providers' public documentation as known
at the time of writing and must be checked against the live terms and
services before live acquisition is accepted (FL13, #2119). The adapters parse
the documented shapes; the offline fixtures under
`tests/fixtures/federal_statutes/` are authored and describe a fictional
statute, not captured data.

## Summary

| Source | Endpoint | Auth | Validity | Decision |
| --- | --- | --- | --- | --- |
| gesetze-im-internet.de | `https://www.gesetze-im-internet.de/gii-toc.xml`, `/<dir>/xml.zip` | none | `observed` (current consolidation only) | `implement` (unverified-live) |
| rechtsinformationen.bund.de | `https://testphase.rechtsinformationen.bund.de/v1/legislation` **(verify host)** | none **(verify)** | `source_stated` (per-expression `temporalCoverage`) | `implement` behind the `federal-statutes` feature (trial service) |
| recht.bund.de (digital BGBl, since 2023) | listing `/rss/bgbl-1.xml` **(verify path)**; document `/eli/bund/bgbl-{part}/{year}/{number}/regelungstext-verkuendung-1.xml` **(verify path)** | none | promulgation date + entry-into-force text as published | `implement` (unverified-live) |
| bgbl.de (historical BGBl, before 2023) | `https://www.bgbl.de/` | none | n/a | `link-only`: references in "Stand" notes and amending acts keep their citation (`bgbl-1/<year>/s-<page>`); no acquisition, no scraping |
| Bundestag DIP | `https://search.dip.bundestag.de/api/v1/` | API key (public key published in the DIP documentation; **verify** current key policy) | n/a | `link-only` for this feature: dossiers are acquired by the Political pack; amendment acts link to them by the BGBl citation a dossier stage states |
| State law beyond Berlin | various | various | n/a | `not implemented` (pack exclusion: complete German or state law) |

## gesetze-im-internet.de

* **Access:** public table of contents `gii-toc.xml` (`<items><item><title/><link/></item>`) and
  one `xml.zip` per statute directory; the ZIP holds one XML document following `gii-norm.dtd`
  (`dokumente` > `norm` > `metadaten` / `textdaten`). No API key. The TOC links use `http://`; the
  adapter fetches the same host over HTTPS and refuses any link to another host.
* **Terms:** statutes are official works without copyright (§ 5 UrhG); the portal's notes
  (`/hinweise.html`) **(verify)** state that the texts are not the authoritative promulgated version.
* **Rate limits:** none documented **(verify)**; the source declares a 120 s timeout, a 60 MB byte
  budget and one statute per page.
* **Identifiers:** `jurabk` / `amtabk`, the statute document number (`doknr`, e.g. `BJNR…`), per-norm
  document numbers (`…BJNE…`) and the statute directory (e.g. `bgb`, `kredwg` for the KWG
  **(verify directories)**).
* **Versions and validity:** the current consolidation only. The header norm's `standangabe` notes
  (`Stand`, `Neuf`, `Hinweis`) are kept **verbatim**; they are never turned into a validity
  interval. Each fetch is compared with the version current at its observation time by content
  hash: the same text is an `unchanged` sighting, changed text a new **observed** version with its
  observation time.
* **Provisions:** `enbez` (`§ 5`, `Art 20`) gives the norm path (`§5`, `art20`); `(n)` paragraphs
  give Absatz paths (`§5/abs2`); `gliederungseinheit` norms become headings; `fussnoten` become
  footnote passages (e.g. implementation statements).
* **Conditional fetching:** the source-pack runtime has no ETag/If-Modified-Since state; the
  "unchanged" outcome is decided by content hash in the Legal store.

## rechtsinformationen.bund.de (federal legal information portal)

* **Status:** a trial service ("Testphase") documented at `https://docs.rechtsinformationen.bund.de/`
  **(verify: current host, stability commitments and whether the trial terms permit automated
  acquisition)**. The feature therefore ships behind the optional `federal-statutes` composition
  feature, default off.
* **Access:** `GET /v1/legislation?searchTerm=<abbreviation>&size=<n>` returns JSON-LD
  (`member[].item` of type `Legislation` with `abbreviation`, `legislationIdentifier` (work ELI)
  and `workExample` (expression ELI, `temporalCoverage`, `encoding[].contentUrl`)); each expression's
  LegalDocML.de XML is fetched from the same host **(verify parameter names and shapes)**. Only
  members whose `abbreviation` equals the selected statute exactly are used.
* **Terms / rate limits:** official works (§ 5 UrhG); trial rate limits **(verify)**.
* **Identifiers:** ELI for work (`eli/bund/bgbl-1/<year>/<number>`) and expression
  (`…/<point-in-time>/<version>/deu`).
* **Versions and validity:** each expression states its validity interval (`temporalCoverage`
  `from/to`, `..` open) → **source-stated** versions (`validity_basis = source_stated`). The portal's
  `legislationLegalForce` label is kept as source metadata only and never surfaced as a force
  statement. `passiveRef` references to BGBl ELIs are kept as the version's stated amending acts.
  Which statutes of the bounded set have a version history is **(verify)**; statutes without
  expressions are `not_found` outcomes.
* **Corrections:** an expression re-published with changed text or validity becomes a correction of
  its current row; a replay adds nothing; a late older row (by `dateModified`) is history only.

## recht.bund.de (digital Federal Law Gazette since 1 January 2023)

* **Access:** a listing of promulgations (RSS/Atom) whose items name the ELI
  `eli/bund/bgbl-<part>/<year>/<number>`; each promulgation's LegalDocML.de document at a declared
  path template **(verify listing URL, document path and formats; PDF is the authoritative form)**.
* **Terms:** official works (§ 5 UrhG) **(verify)**.
* **Identifiers:** BGBl citation (part I/II, year, number), e.g. `BGBl. 2030 I Nr. 45`, keyed
  `bgbl-1/2030/nr-45`; the promulgation's ELI.
* **What is stored:** title, promulgation date (`FRBRdate` named `verkuendung…`), signature date
  ("Vom …"), the entry-into-force article verbatim, and each amending instruction with its target
  statute and provision where unambiguous (`§ 5 Absatz 2 wird wie folgt gefasst` →
  `§5/abs2`). Instructions naming the table of contents or an annex, ranges or no provision stay
  `ambiguous` with their text and locator. Instructions are **never applied**; no consolidation is
  computed.
* **Bounding:** only acts whose amending articles name a statute of the bounded set are acquired;
  others are receipted `seen_not_acquired`. Listing items before 2023 are `link-only`.

## Historical BGBl (bgbl.de)

`link-only`. The historical gazette is published as scanned/PDF issues without a documented
machine-readable interface **(verify)**; it is never scraped. Legacy references (`BGBl. I S. 2708`,
Stand notes `I 42`) are parsed into `bgbl-1/<year>/s-<page>` keys (when the year is stated) and
reported as `not_acquired` / "historical Federal Law Gazette issue (link-only)".

## Bundestag DIP

The Political pack acquires DIP records into legislative dossiers
(`src/domains/political/legislative_dossiers.py`). DIP's Vorgang records carry the promulgation
("Verkündung", `fundstelle` such as `BGBl I 2023 Nr. 411`) **(verify field names)**. An amendment act
links (`enacted_as`) to a dossier only when a dossier stage states the act's BGBl citation (in its
instrument identifier, title or a quoted legal event); titles are never compared. Unmatched acts stay
unlinked with the reason. API key: DIP publishes a public API key with an expiry **(verify)**.

## Bounded statute set (v1)

| jurabk | Statute | Reason |
| --- | --- | --- |
| `BGB` | Bürgerliches Gesetzbuch | the civil-law statute most cited by federal court decisions |
| `HGB` | Handelsgesetzbuch | commercial law cited by BGH decisions |
| `GmbHG` | GmbH-Gesetz | company law cited by BGH decisions and ownership records |
| `AktG` | Aktiengesetz | stock-corporation law cited by BGH decisions and ownership records |
| `WpHG` | Wertpapierhandelsgesetz | securities trading law BaFin notices (#2106) rely on, incl. the MAR implementation provisions |
| `KWG` | Kreditwesengesetz | banking supervision law BaFin notices (#2106) rely on |
| `VwVfG` | Verwaltungsverfahrensgesetz | administrative procedure law cited by BVerwG decisions and supervisory notices |
| `GG` | Grundgesetz | constitutional provisions cited across federal court decisions |
| `ZPO` | Zivilprozessordnung | cited by the BGH decision already in the Legal pack fixtures (§ 13, § 251 ZPO) |
| `InsO` | Insolvenzordnung | cited by the BGH decision already in the Legal pack fixtures (§ 4 InsO) |
| `VwGO` | Verwaltungsgerichtsordnung | procedural law of the BVerwG decisions already in the Legal pack fixtures |

The set is `FEDERAL_STATUTE_SET` in `src/kb/legal_citations.py`; the citation parser resolves only
these abbreviations plus statutes acquired into a namespace. An unknown abbreviation is
`unresolved`, never guessed.

## Source-supported vs observed

* **Source-stated validity:** rechtsinformationen.bund.de expressions only.
* **Observed only:** gesetze-im-internet.de (the current text on the fetch date).
* **Neither:** recht.bund.de states promulgation and entry-into-force *text*; entry into force is
  stored verbatim and never computed.

As-of answers use a source-stated version whose interval covers the date; otherwise the nearest
observed version on or before it, labelled "observed on <date>, validity not stated"; otherwise "no
version on record". An observation inside a stated interval with different text is a conflict
returned side by side. No answer states that a provision is in force.

## Offline and live evidence

* Offline: `tests/unit/domains/test_federal_statutes_acceptance.py` and the other
  `test_federal_statute*` / `test_legal_citations.py` tests, on authored fictional fixtures.
* Pinned production fixtures (`tests/fixtures/source_packs/legal-federal-*.json`) replay the
  production selections offline: the bounded statute set is absent from the authored TOC and search
  result (`not_found`), and the two authored promulgations amend none of it (`seen_not_acquired`).
* Live: outstanding (FL13, #2119).
