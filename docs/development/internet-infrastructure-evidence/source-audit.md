# Internet infrastructure: source-contract audit and bounded coverage (II01)

Tracking: wave 2 tracker #2736 · recorded 2026-10-03.

No per-track tracker or delivery issue exists yet. They are opened once this
audit names a surviving source, which it does (RIPEstat, PeeringDB, RDAP and
crt.sh below), so the next step is to open them and link them here.

This audit sets out, per source, what the Technology bundle's
`technology.internet-infrastructure` provider (subdomain
`internet-infrastructure`, ADR-005) may acquire, how, and on what terms.
**It was written without network access: the provider hosts
(`stat.ripe.net`, `www.peeringdb.com`, `data.iana.org`, the RIR RDAP servers,
`crt.sh`, `www.caida.org`) were blocked by this runtime's egress proxy
(verified 2026-10-03), so terms, endpoints and field names were not
re-verified live.** They come from the providers' published documentation as
the author knows it. Every item marked _verify_ must be checked against the
live terms page and a real response before the first dated live run. No source
is `live` until that run exists.

**There is no machine-readable copy yet.** `PROVIDER_CONTRACTS`,
`BOUNDED_COVERAGE`, `CAPS`, `MINIMISATION`, `EXCLUSIONS` and
`LIVE_VERIFICATION` will be added in
`src/ingestion/internet_infrastructure_sources.py` by the track's acquisition
issues, and must match this audit; a difference is a defect in one of the two.

Non-goals for every source: no exposed-service search, port or banner data, no
subdomain enumeration or attack-surface mapping, no IP-keyed "what else is
hosted here" pivots, no person-keyed lookups (e-mail, handle, name), no
reputation, risk, hijack or misconfiguration verdicts, no ranking of networks,
and no merging of RIPEstat, PeeringDB and RDAP statements about one ASN or
prefix into a single "true" record. These repeat the OSINT pack's `exclusions`
(`packs/osint/pack.json`) and apply here although the provider lives in the
Technology bundle.

## Access decisions

| Source | Publisher | Delivers | Access | Decision |
| --- | --- | --- | --- | --- |
| RIPEstat Data API | RIPE NCC | routing state and history per declared ASN or prefix: announced prefixes, routing status and visibility, RPKI validation, AS overview | JSON data calls, no key; `sourceapp` identifier requested (_verify_) | `unverified-live` |
| PeeringDB API | PeeringDB | self-declared network, organisation, IX and facility records; network-to-IX presence | REST JSON; anonymous read, optional API key for higher limits (_verify_) | `unverified-live` |
| RDAP (IP networks and autnums; domains) | IANA bootstrap, RIR and registry RDAP servers | registration records for declared ASNs, prefixes and one domain: holder organisation, status, events | RFC 9224 bootstrap, RFC 9083 JSON, no key | `unverified-live` |
| Certificate transparency via crt.sh | Sectigo (crt.sh) | issuance facts for one exactly named domain: issuer, validity, DNS SANs | `?q=<domain>&output=json`, no key, no documented API contract (_verify_) | `unverified-live` |
| CT log list | Google Chrome CT log list v3 (_verify_ host and version) | log ids, operators, states (usable, readonly, retired) | one JSON file, no key | `unverified-live` |
| Direct RFC 6962 log APIs | CT log operators | raw log entries (`get-entries`), signed tree heads | per-log HTTP API | `not-implemented`: logs cannot be queried by domain, so finding one domain's certificates means mirroring a log; that breaks the bounded-coverage rule |
| CAIDA datasets (AS relationships, AS Rank, restricted datasets) | CAIDA, UC San Diego | inferred AS relationships, rankings, topology | public downloads under CAIDA's acceptable-use agreement; many datasets need a request (_verify_) | `not-implemented`: the AUP limits use and redistribution (research use, citation, no redistribution, commercial use by separate licence, _verify_), which conflicts with exporting cited evidence bundles; AS Rank is a ranking, which the non-goals exclude |

Documented, not acquired: raw BGP MRT dumps from RIPE RIS and RouteViews
(bulk files; RIPEstat answers the bounded questions from the same collectors),
WHOIS over port 43 (unstructured, contact-heavy; RDAP supersedes it), IRR
databases (the routing-policy model is out of first coverage), and crt.sh's
public PostgreSQL interface (not a documented service contract).

**Unavailable-access fallback.** A failed request (HTTP error, 429 after one
retry, redirect to an undeclared host, schema drift, a response over the byte
budget) fails that unit with its code and a receipt. Earlier revisions stay
current; nothing is marked removed because of a failure. The provider's
readiness reports the source as `stale`.

## Per-source contract

| Source | Endpoints | Authentication | Licence and redistribution | Rate limits | Revision model: updates, corrections, removals |
| --- | --- | --- | --- | --- | --- |
| RIPEstat | `https://stat.ripe.net/data/{call}/data.json?resource=<ASN or prefix>&sourceapp=noesis`; calls `as-overview`, `announced-prefixes`, `routing-status`, `rpki-validation` (`resource` + `prefix`), `prefix-overview` (_verify_ call names and parameters) | none; `sourceapp` is an identifier, not a secret | RIPEstat terms and conditions; reuse with attribution "RIPEstat, RIPE NCC" (_verify_ wording and whether redistribution of results is permitted) | fair use; no fixed public quota known (_verify_); paced at 1 request/second; at most 5 calls per resource | every response carries `data_call_status`, the data call `version` and a `query_time` or `latest_time` (_verify_ field names): each answer is an observation dated by RIPEstat's stated time, never by retrieval time alone; routing state has no corrections, a later answer is a later observation; a deprecated data call version is recorded and the unit fails rather than reading a new shape silently |
| PeeringDB | `https://www.peeringdb.com/api/net?asn=<ASN>`, `/api/org/{id}`, `/api/netixlan?net_id=<id>`, `/api/ix/{id}`, `/api/fac/{id}`; `depth=0` (_verify_) | anonymous; optional `Authorization: Api-Key <key>` from `NOESIS_PEERINGDB_API_KEY` (`optional-secret`, never stored in a record or receipt) | PeeringDB Acceptable Use Policy: data for interconnection and operational purposes, no use for unsolicited marketing (_verify_ the AUP's reuse and redistribution clauses before the live run) | anonymous and keyed per-minute limits, HTTP 429 when exceeded (_verify_ numbers) | each object has `created`, `updated` and `status` (`ok`, `pending`, `deleted`; _verify_): a changed `updated` is a new revision; `status=deleted` (or a 404 for a declared id) is a `removed_by_source` revision; the record is the network's self-declaration and is labelled so |
| RDAP | bootstrap `https://data.iana.org/rdap/asn.json`, `ipv4.json`, `ipv6.json`, `dns.json`; then `{base}autnum/{asn}`, `{base}ip/{prefix}`, `{base}domain/{name}` on the server the bootstrap names | none | per registry: RIPE Database terms, ARIN RDAP terms of use, other RIR and registry terms (_verify_ each); `redistribution: provider-specific`, as the OSINT `rdap-domain` source declares | per RIR, HTTP 429 (_verify_); one request per declared object; the bootstrap fetched once per 24 h | `events` (`registration`, `last changed`) and `status` per object; a changed `last changed` or content digest is a new revision; a 404 for a declared object is a `removed_by_source` revision; a transfer between RIRs appears as a different bootstrap target and is recorded as stated |
| crt.sh | `https://crt.sh/?q=<domain>&output=json` (exact domain, no `%` patterns) | none | Sectigo operates crt.sh; no published licence (_verify_); `redistribution: locator-only`, as the OSINT `crt-sh` source declares: certificate ids and facts are cited by crt.sh locator | undocumented; the service is slow and times out on large domains (_verify_); one request per declared domain per run | CT logs are append-only; a new certificate is a new issuance record; revocation is not in the JSON output (_verify_) and is never inferred; expired certificates stay recorded with their validity dates |
| CT log list | `https://www.gstatic.com/ct/log_list/v3/log_list.json` (_verify_) | none | published for CT policy use; terms _verify_ | one file per run | `version` and `log_list_timestamp` (_verify_) date each release; a log changing state (usable to retired) is a new revision of that log |

## Record shape and reuse

Record shapes (`packs/taxonomy.json`): **registry-records** for ASNs,
prefixes, PeeringDB networks, IXs and facilities, RDAP registrations,
certificates and CT logs; **observations** for RIPEstat routing state, which
is dated by the source and never rewritten. No new shape is needed.

Reuse points (paths verified in this checkout):

- `src/ingestion/rdap.py` (OSINT OX05): `parse_bootstrap`, the RFC 9083
  parser and its person-data rules. Its bootstrap table is keyed by TLD; the
  `asn.json` ranges and IP prefix keys need an extension, not a second parser.
- `src/ingestion/crtsh.py` (OSINT OX06): `parse_crtsh` and its e-mail SAN
  drop; `src/ingestion/osint_observations.py`: `normalize_domain`, which
  refuses wildcards, IP literals and e-mail addresses, and the bounded,
  receipted fetch path.
- `src/ingestion/source_pack_runtime.py` and
  `src/ingestion/provider_execution.py` (`DurableHTTP`) for the transport,
  budgets and receipts, as `src/ingestion/oss_ecosystem_sources.py` and
  `src/ingestion/vulnerability_sources.py` use them.
- The OSINT sources `rdap-domain` and `crt-sh` in
  `config/source_packs/osint.json` stay where they are and keep projecting into
  source identities (`src/kb/source_identity.py`). This provider stores
  registry facts in the Technology namespace and does not write source
  identities.

Links: to OSINT by the stated domain of an existing source identity only; to
`technology.vulnerabilities` only where a source states a CVE id (none of the
sources above does in first coverage). No link is inferred from shared
addresses.

## Data minimisation decision

RDAP, PeeringDB contact records and certificates can carry natural-person data
(registrant names, e-mail addresses, telephone numbers, postal addresses,
individual-validated certificate subjects). Decision:

- **Stored:** ASN, prefix, domain, holder or registrant **organisation** name
  as published, country as published, RIR, status, registration events,
  nameservers; RIPEstat routing observations; PeeringDB network, organisation,
  IX and facility records (names, ASN, `info_type`, policy fields, IX
  presence with speeds and addresses on the IX LAN); certificate crt.sh id,
  issuer DN, validity, DNS SANs, log ids.
- **Redacted:** RDAP entities with vCard `kind` `individual` are never
  persisted; for any other entity only the organisation name is kept; e-mail,
  telephone and postal address properties are never read.
- **Excluded:** RDAP administrative, technical, billing and abuse contacts;
  PeeringDB `poc` objects (all visibilities) and user accounts; the RIPEstat
  `whois` and `abuse-contact-finder` calls; certificate subject DNs beyond the
  organisation (`O`), e-mail SANs, S/MIME and client certificates; anything
  keyed by an IP address alone.
- **Retention:** revisions and observations are kept for provenance with their
  run; no stored field identifies a person, so no erasure workflow applies.
- **Who may query:** `knowledge:technical:internet-infrastructure:read` with
  namespace access; writes `...:write`; identity reviews `...:review`.

**OSINT gate.** None of the tools this provider needs is a gated tool under
`docs/security/osint-review-gate.md`: they answer registry and routing facts
for declared resources. They must still refuse IP-keyed, person-keyed and
wildcard queries in code (reusing `normalize_domain`), accept only declared
ASNs, prefixes and domains, and never list "other networks of this
organisation" beyond what a declared ASN returns. If a later issue wants
undeclared or reverse lookups, it goes through the gate and the abuse
analysis (`docs/security/osint-abuse-analysis.md`) first.

## Bounded first coverage

| Source | Selection | Caps |
| --- | --- | --- |
| RIPEstat | one declared ASN and one declared prefix it announces | 5 data calls per resource, 2 resources, 2,000 prefixes per response |
| PeeringDB | the network record of the same ASN, its organisation and IX presence | 1 network, 1 organisation, 20 `netixlan` rows, 5 IXs |
| RDAP | the same ASN and prefix, and one declared domain | 3 objects, 1 bootstrap fetch per file per 24 h |
| crt.sh | the same domain, exact match | 1 request, 200 certificates; more is `budget_exhausted`, never truncated |
| CT log list | the current list | 1 file, 2 MB |

Justification: one network seen through its routing state, its
self-declaration, its registration and one domain's certificates is the
smallest selection that shows the four sources side by side without merging
them. The live selection is named in the acquisition issue; every further
resource is a source-pack version bump.

## Live verification

| Source | Status | Checked | Evidence |
| --- | --- | --- | --- |
| RIPEstat | `unverified-live` | - | none yet |
| PeeringDB | `unverified-live` | - | none yet |
| RDAP (autnum, ip, domain) | `unverified-live` | - | none yet (the OSINT domain path has offline fixtures only) |
| crt.sh | `unverified-live` | - | none yet |
| CT log list | `unverified-live` | - | none yet |
| Direct RFC 6962 logs | `not-implemented` | - | bounded coverage impossible without mirroring |
| CAIDA datasets | `not-implemented` | - | AUP restricts redistribution (_verify_) |

Fixtures will be authored, not captured: documentation ASNs (AS64496-AS64511),
documentation prefixes (192.0.2.0/24, 198.51.100.0/24, 2001:db8::/32), the
domain `example.org`, fictional organisations, and dates in 2094-2099, so
nothing can be mistaken for a published record.
