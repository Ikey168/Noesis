# Person-research mode (`NOESIS_PERSON_RESEARCH`)

Off by default. Set `NOESIS_PERSON_RESEARCH=1` (or `on`, `true`, `yes`) to lift
the natural-person limits in the modules used to research ownership and
capital, for an operator's own local warehouse. With the switch off, every
minimisation decision listed below stays in force exactly as before.

## What changes when it is on

| Module | Default (off) | Person-research mode (on) |
|---|---|---|
| OSINT movements (`src/osint/movements.py`) | Aircraft whose registry record names a natural person: windows refused with `private_aircraft_refused`, registrant name withheld, registrant never matched | Answered like any aircraft; registrant name returned; natural-person registrants offered to identity review against person entities |
| On-chain (`src/kb/onchain.py`) | Label references to person entities refused (`person_reference_refused`) and filtered from entity lookups | Allowed and returned; untyped entities are still refused |
| OSINT dossier (`src/osint/dossier.py`) | Ownership, designations and on-chain sections never assembled for a person; a person without document mentions refused | Assembled for persons like organisations (including the subsidiaries a person holds); no document-mention requirement |
| Campaign finance (`src/ingestion/campaign_finance_sources.py`, `src/kb/campaign_finance_*`) | Individual donors stored with no name, employer, occupation, city or id (CF01); never subjects; contributions hidden without the individual-items scope; never notified | Stored with name, employer, occupation, city, state and the regulator's donor id under policy `campaign-finance-person-research-v1`; `donor-person` identity subjects matchable to ownership persons; returned and notified with the read scope |
| BaFin notices (`src/domains/market/bafin_identity.py`) | Natural persons keyed per issuer; never proposed automatically; `bafin_holdings_of_holder` reads a person within its issuer only | Same normalised person name under different issuers proposed as a cross-issuer candidate; once a reviewer accepts it, `bafin_holdings_of_holder` reads the person's holdings across issuers |
| Extractives (`src/ingestion/extractives_sources.py`, `src/kb/extractives_*`) | Individual payers' names and identifiers withheld | Kept as published and offered to identity matching |
| Enforcement, EN01 (`src/ingestion/enforcement_sources.py`, `src/kb/enforcement_*`) | Individual respondents counted, never named; `[individual]` replaces their names in stored text; sentences about them dropped; releases, cases, notices and register entries whose only respondents are individuals (FCA notices addressed to individuals) not acquired; never identity subjects | Individual respondents stored as `natural_person` respondents with name and role as published (no identifier) under policy `enforcement-person-research-v1`; names kept in titles, captions, charges and outcomes; an individual's own sanction recorded and attributed; individual-only units acquired; respondents proposed against ownership **persons** (name, low evidence) and reachable through `actions_for_entity` and entity monitors once a reviewer accepts |
| Courts, CJ01 (`src/ingestion/courts_justice_sources.py`, `src/kb/legal_dockets.py`, `src/kb/courts_justice_*`) | Natural-person parties pseudonymised (`natural person 1 (Defendant)`); never matched, queried or monitored | Parties keep the published name and a docket-scoped party key under policy `courts-person-research-v1`; proposed against ownership persons (name, low evidence); `dockets_for_party` and party monitors accept a person through an accepted match or by the published name (`published-name`, not an identity decision) |

## What stays the same

- **Street addresses and ZIP/postcodes** of individual donors are still
  withheld, also in this mode.
- **Dates of birth, ages, home addresses and personal registration numbers**
  (FCA IRN, FINRA CRD) of enforcement respondents stay withheld: they are
  replaced by `[... withheld]` in stored text, an individual respondent never
  carries an identifier, and `date_of_birth`, `home_address` and the other
  personal fields are still refused on write. Court parties' addresses, dates
  of birth, `extra_info`, attorneys and CourtListener party ids are still never
  stored.
- **Records stored under this mode** carry the person-research policy. Writing
  such a record with the switch off is refused (`minimisation_violation`), so a
  default warehouse never receives person data by accident.
- **Registry privacy opt-outs** (FAA LADD/PIA, withheld G-INFO entries,
  operator declarations) are still honoured (`privacy_opt_out`).
- **Movement queries keyed on a person's name** are still refused
  (`person_identifier_refused`). The module has no name-to-aircraft
  resolution, so query by registration, ICAO address, IMO or MMSI.
- **Identity matches** stay reviewable, reversible decisions; nothing is merged
  automatically.
- **Unchanged modules:** fact checks, platform transparency, treaties,
  research entities and medical devices keep their minimisation.
- **Enforcement and courts records naming no individual** are stored exactly
  as with the switch off (no policy, no new revision); only records that differ
  from the default minimisation carry the person-research policy.
- **The geolocation refusal** in the gated OSINT tools is unchanged.

## Data already acquired

Ingestion decides what is stored. Campaign-finance, extractives, enforcement
and court-docket data acquired with the switch off has no names, so re-acquire
it with the switch on (an enforcement or docket re-read becomes a new revision
under the person-research policy).

Turning the switch off later does not delete stored person data. Remove it from
the warehouse yourself if you need to.
