# Space objects: registration, operator and re-entry source contracts and bounded coverage (SO01, #2417)

Tracking issue: #2224. This audit extends the Astronomy and Space source audit
(`source-audit.md` in this directory, AS01 #2150). It records, per source of
state registrations, operator/owner assertions and re-entry records, the access
method, terms, attribution, redistribution, authentication, rate limits and
revision semantics, and the bounded coverage the `astronomy.space-object-registration`
provider implements. It extends the existing Astronomy pack (`packs/astronomy/`);
no new pack is created.

**Verification status.** No live request was made while writing this audit.
The build environment has no network access to UNOOSA, the UN Official
Document System, ESA DISCOSweb or aerospace.org. Every claim marked
**(verify)** comes from the providers' public pages as known at the time of
writing and must be checked before live acquisition is accepted (SO14, #2461,
which this change does not implement). Every fixture under
`tests/fixtures/astronomy_registration/` and
`tests/fixtures/source_packs/astronomy-registration-*.json` is authored and
names fictional objects (the `FICTSAT` objects of 2099, the registering States
"Fictland" and "Republic of Examplia", the intergovernmental "European
Fictional Space Organisation") and fictional document symbols
(`ST/SG/SER.E/99xx`). None of it is captured data.

`PROVIDER_CONTRACTS` and `LIVE_VERIFICATION` in
`src/ingestion/astronomy_registration_sources.py` restate these decisions in
code; every implemented source is `unverified-live` until a dated live run is
recorded under this directory.

## Non-goals enforced by every decision below

* **No collision or re-entry prediction by Noesis.** A re-entry record quotes
  the publisher's reported time, uncertainty window and location as
  published, with the report's revision (a prediction or the post-event
  report). Noesis computes no window, no footprint and no probability.
* **No attribution beyond published registrations.** Registering State,
  operator and owner are what a registration document, the UNOOSA index or a
  DISCOS operator assertion states, with its role as published. No military,
  intelligence or "true operator" attribution is inferred, and nothing is
  derived from orbit, name or launch site.
* **"No UN registration on record" is not "unregistered".** An index entry
  without a registration document means the UN index states none; it is never
  reported as the State having failed to register (a national register may
  exist).
* **Orbital parameters as registered stay registered values.** They are never
  reconciled with, corrected by or compared numerically against SATCAT or
  GCAT catalogue values.
* **Original language is authoritative.** Registration documents are read in
  the language the document declares; no translation-derived claim is stored.
* **No account-restricted data is redistributed** (ESA DISCOS, see below).

## Summary

| Source | Endpoint | Auth | Format parsed | Decision |
| --- | --- | --- | --- | --- |
| UNOOSA Online Index of Objects Launched into Outer Space | `https://www.unoosa.org/oosa/osoindex/` (search results as JSON; the query path is an assumption **(verify)**) | none | JSON (`unoosa-index-json`) | `implement` (unverified-live), bounded to declared international designators or launch years |
| UN registration documents (ST/SG/SER.E series, A/AC.105/INF series) and notifications (change of status, transfer of supervision, re-entry) | `https://www.unoosa.org/oosa/en/spaceobjectregister/` document pages and `https://documents.un.org/` **(verify)** | none | the document's text rendition (`unoosa-registration-text`) read through declared per-language labels | `implement` (unverified-live), bounded to declared document symbols (at most 60 per source); PDF layout is not parsed |
| National registries' notes verbales transmitted by the UN | as ST/SG/SER.E documents | none | as above | covered as UN documents only; national registry web sites are `link-only` (cited, never scraped) |
| ESA DISCOSweb (objects, operators, re-entries) | `https://discosweb.esoc.esa.int/api/` (JSON:API) | **account + personal access token** | JSON:API (`discos-json`) | `implement-gated` (unverified-live): adapter **disabled when no token is configured**; stores only the permitted subset below, otherwise a citation and retrieval receipt |
| The Aerospace Corporation re-entry database (CORDS reentry predictions) | `https://aerospace.org/reentries` (per-object pages; path **(verify)**) | none | HTML table (`aerospace-reentry-html`) through declared columns | `implement` (unverified-live), bounded to declared objects (at most 50) |
| Space-Track.org decay/TIP messages | `https://www.space-track.org/` | account | n/a | `not implemented` (as in AS01): the user agreement restricts redistribution **(verify)** |

## Per-source decisions

### UNOOSA Online Index (SO03)

* **Access.** Public web search; no account. The index is served as HTML with
  a JSON search back end **(verify the path and field names)**; the adapter
  reads a declared JSON results document. Rate limits are not published; the
  pack runs at most daily with one request per declared document.
* **Terms and attribution.** United Nations terms of use; reuse with
  attribution to UNOOSA **(verify)**. Attribution recorded: "United Nations
  Office for Outer Space Affairs, Online Index of Objects Launched into Outer
  Space." Redistribution: facts (designators, names, State, dates, status,
  document symbols) with attribution; no bulk mirror.
* **Keys.** International (COSPAR) designator, normalised as in AS01; the UN
  registration document symbol is kept beside it. National designators are
  kept as published.
* **Revision semantics.** Status fields (in orbit, decayed, de-orbited,
  function) are edited in place without a change date **(verify)**. Each
  acquisition stores them as published with the **index retrieval date**
  (the source-pack document's `source_as_of`, else the acquisition day); a
  changed value is a new revision, the earlier one kept.
* **Absence.** `un_registered: false` or no registration document is stored as
  published and answered as "no UN registration on record".

### UN registration documents and notifications (SO04)

* **Locator scheme.** Document symbol (`ST/SG/SER.E/1234`,
  `A/AC.105/INF.456`) plus the paragraph number (`para 3`) or table and row
  (`table 1, row 2`) the entry occupies in the document's text rendition, plus
  the language code of the rendition. The entry block is quoted verbatim.
* **Language handling.** Each declared document names its language (UN
  official languages: `en`, `fr`, `es`, `ru`, `zh`, `ar`). The adapter reads
  structured values only through labels declared for that language (English
  labels ship as the default; any other language must declare its labels).
  A value is never translated; the quotation is the original text.
* **Registered values.** Nodal period, inclination, apogee and perigee are
  stored as the registered text with the unit as printed; they are never
  reconciled with SATCAT/GCAT elements.
* **Revision semantics.** A registration is one document entry. A later
  notification (change of status, transfer of supervision, re-entry notice,
  additional information) is its own entry, ordered by the document date;
  the original registration is never overwritten. The answer resolves the
  registration state, the supervising State and the status as of a date from
  the dated chain.
* **Terms.** UN documents are public; attribution "United Nations, document
  <symbol>" **(verify)**.

### ESA DISCOS (SO05)

* **Access.** DISCOSweb requires a personal account and an API token passed
  as a bearer header; rate limits apply per token (around 20 requests per
  minute **(verify)**). The token is a `NOESIS_ESA_DISCOS_TOKEN` secret
  reference of the source-pack runtime; it is never committed. Without a
  configured token the adapter refuses to run (`credential_missing`) and the
  bundle status reports the source `disabled: no account configured`.
* **Terms.** The DISCOSweb terms of use restrict redistribution of the
  database content and require acknowledgement of ESA's Space Debris Office
  **(verify)**. **Decision:** the only data stored is the *permitted subset*
  of identifiers and assertions an account holder may keep for their own
  analysis **(verify with ESA)**: DISCOS object ID, COSPAR and NORAD
  cross-references, object name and class, operator attribution (name and
  role as DISCOS states it) and re-entry epoch. Nothing else (mass, shape,
  dimensions, orbit history, fragmentation data) is read. A declaration may
  set `mode: citation-only`, in which case each object becomes a citation
  record holding only the DISCOS object URL, the identifiers the request
  named and the retrieval receipt.
* **No redistribution.** DISCOS records are marked `restricted`; evidence
  bundles export only their citation and retrieval receipt, never the stored
  values.
* **Revision semantics.** DISCOS edits objects in place; each acquisition's
  changed operator attribution or re-entry epoch is a revision of that DISCOS
  assertion.

### The Aerospace Corporation re-entry database (SO06)

* **Access.** Public web pages; no account. One page per object lists the
  published predictions with their issue time, the predicted re-entry time
  and window, and, after re-entry, the confirmed report **(verify the page
  layout)**. Columns are read through a declared mapping, so a changed header
  is a declaration change.
* **Terms and attribution.** © The Aerospace Corporation; predictions may be
  cited with attribution **(verify)**. Attribution recorded: "Reentry data
  courtesy of The Aerospace Corporation (CORDS)." No page is mirrored; only the
  prediction rows are stored.
* **Keys.** NORAD catalogue number and COSPAR designator as the page states
  them (the declaration names the object).
* **Revision semantics.** Every published prediction is a dated revision of
  the object's re-entry record (its issue time orders it); the post-event
  (confirmed) report is the latest revision and supersedes the predictions
  without deleting them.
* **Location.** Stored as published (text); projected through
  `src/kb/geospatial.py` only when the page states coordinates.

## Bounded v1 coverage

| Dimension | Bound |
| --- | --- |
| States and registrants | the declared documents' registrants (v1 fixtures: Fictland, Republic of Examplia, European Fictional Space Organisation) |
| Years | international designators of the declared launch years (v1: 2099 in fixtures; live selection to be declared in SO14) |
| Object classes | payloads and rocket bodies as the index lists them; debris is out of scope |
| UNOOSA index | at most 50 designators per declared document |
| Registration documents | at most 60 declared symbols per source |
| DISCOS | at most 50 declared objects; permitted subset only |
| Aerospace re-entries | at most 50 declared objects |

Anything outside the declared bounds is counted as `out_of_scope` in the page
receipt and never stored.

## Stable identifiers and record keys

| Record | Source record ID | Linked to SATCAT objects by |
| --- | --- | --- |
| UNOOSA index entry | COSPAR designator | COSPAR (exact) |
| Registration document entry | document symbol + locator | the COSPAR designator (and NORAD number) the entry states; a name-only entry is a review candidate |
| Operator assertion | provider + object + operator name + role | its object's identifiers |
| DISCOS object / citation | DISCOS object ID | COSPAR and NORAD as DISCOS states them |
| Aerospace re-entry | NORAD number | NORAD and COSPAR |

Linking to the orbital objects from AS05 (`orbital_object` records of GCAT and
CelesTrak SATCAT) is by exact identifier with evidence; conflicting
identifiers across sources stay as reviewable candidates, and records are
never merged (SO07).
