# Technology internet infrastructure

The Technology bundle's optional internet-infrastructure features add the
`technology.internet-infrastructure` provider (track #2743, subdomain
`internet-infrastructure`). For one **declared** ASN, one prefix it announces
and one domain, it keeps what each source states, side by side and cited:

- **Routing** - RIPEstat answers (`as-overview`, `announced-prefixes`,
  `routing-status`, `rpki-validation`, `prefix-overview`) as observations
  dated by RIPEstat's stated time and data-call version.
- **Interconnection** - the network's PeeringDB self-declaration: network,
  organisation, IX presence (`netixlan`), IXs and declared facilities.
- **Registration** - RDAP autnum, IP network and domain registrations from the
  server the IANA bootstrap names: holder organisation, country, RIR, status,
  events and nameservers.
- **Certificates** - crt.sh issuance facts for the exact domain and the CT log
  list v3.

Statements of different sources are never merged into one record. Coverage is
offline: every source is `unverified-live` until the track's live validation
(II13) runs where the provider hosts are reachable. Audit and evidence:
[source audit](../development/internet-infrastructure-evidence/source-audit.md).

## What it never does

No exposed-service, port or banner data; no subdomain enumeration or
attack-surface mapping; no IP-keyed "what else is hosted here" pivots; no
person-keyed lookups (e-mail, handle, name); no reputation, risk, hijack or
misconfiguration verdicts (RIPEstat's RPKI status is kept as RIPEstat states
it); no ranking of networks. RDAP natural persons and contacts, PeeringDB
`poc` objects, certificate subjects beyond `O`, e-mail SANs and revocation are
never stored; the store refuses them at write time.

The tools are checked against the
[OSINT review gate](../security/osint-review-gate.md) and are not gated:
they answer declared resources only. Undeclared and reverse lookups are out
of scope.

## Enable

1. Select one or more features of the Technology bundle (each default off and
   independent): `internet-infrastructure-ripestat`,
   `internet-infrastructure-peeringdb`, `internet-infrastructure-rdap`,
   `internet-infrastructure-ct`.
2. Install the separate `technology-internet-infrastructure` 1.0.0 source pack
   (`config/source_packs/technology-internet-infrastructure.json`), accept the
   source licences and declare the resources in each source's
   `internet_infrastructure.selection` (a new resource is a source-pack
   version bump). PeeringDB accepts an optional key in
   `NOESIS_PEERINGDB_API_KEY`; it is sent as a header and never recorded.
3. Run the sources through the source-pack runtime (or
   `InfrastructureMonitor.refresh`). Each unit (one declared resource) is
   bounded: 5 RIPEstat calls per resource at one request per second, 1
   PeeringDB network, 1 organisation, 20 `netixlan` rows and 5 IXs, 3 RDAP
   objects with the bootstrap read once per file per 24 h, 1 crt.sh request of
   at most 200 certificates (more is `budget_exhausted`, never truncated) and
   one CT log list of at most 2 MB. A failed unit records a receipt and never
   a removal.

## Ask

All tools live on `noesis-knowledge-engine` and declare their scopes
(`knowledge:technical:internet-infrastructure:read`, `...:write`,
`...:review`).

| Question | Tool |
| --- | --- |
| Which resources are answered? | `list_internet_infrastructure_resources` |
| What did each source state about AS64500 on 2096-01-01? | `internet_infrastructure_records_as_of` (`resource="AS64500"`, `as_of="2096-01-01"`) |
| Every revision and observation of a resource | `internet_infrastructure_history` |
| A cited evidence bundle of an answer | `export_internet_infrastructure_bundle` |
| Relate records of different sources | `propose_internet_infrastructure_identity`, then `review_internet_infrastructure_identity` (and `revert_...`) |
| Link to OSINT source identities and Vulnerabilities | `link_internet_infrastructure_osint`, `link_internet_infrastructure_vulnerabilities` |
| Watch a resource | `create_internet_infrastructure_monitor`, `run_...`, `poll_...` |

An as-of answer selects, per record, the revision whose as-of time (the
source's `updated`, `last changed`, CT log list timestamp or crt.sh entry
time, else the retrieval) is the latest on or before the date, and per
RIPEstat data call the latest observation. `removed_by_source` revisions (a
404, `status=deleted`, or absence from a complete listing) are shown as such.
A declared resource with nothing on record answers `none_on_record`.

Identity: records of different sources are related only by assertions that
rest on stated identifiers (the same ASN, prefix or exact domain; the
PeeringDB organisation the ASN's network names and the RDAP holder of the same
ASN). They are proposed, then accepted or rejected by a reviewer, and can be
reverted. A shared organisation name is never a match. Unmatched records stay
listed.

Links: domain records link to existing OSINT source identities by the stated
domain only; Vulnerabilities links exist only where a source states a CVE id
(none does in first coverage). Absent providers are reported
`provider_absent`, missing targets `target_not_held`.

## Monitor

A monitor is a knowledge subscription on a declared resource (optionally one
provider) or a provider. Notices - `registration_change`, `peeringdb_update`,
`new_certificate`, `ct_log_state_change`, `routing_observation_changed`,
`removed_by_source` - cite the new record and the one before and state what
changed. Unchanged answers and replays notify nothing.

## Offline acceptance

`tests/unit/domains/test_internet_infrastructure_acceptance.py` replays the
pinned fixtures with sockets blocked and takes AS64500 and `example.org` to
cited routing, interconnection, registration and certificate records with
revisions. It is offline evidence, not live coverage.
