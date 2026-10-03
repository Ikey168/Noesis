# Internet infrastructure evidence (technology.internet-infrastructure)

Evidence for the Technology bundle's `technology.internet-infrastructure`
provider (wave 2 tracker #2736, subdomain `internet-infrastructure`).

- [source-audit.md](source-audit.md) - II01: per-source contract, licence,
  access, rate limits, revision model, the data-minimisation decision and the
  bounded first coverage. Written without network access; terms not
  re-verified live. RIPEstat, PeeringDB, RDAP, crt.sh and the CT log list are
  `unverified-live`; direct RFC 6962 log access and CAIDA datasets are
  `not-implemented`.
- Machine-readable copy: `src/ingestion/internet_infrastructure_sources.py`
  (II03-II06, track #2743), matching the audit; the source-pack entries are
  the separate `technology-internet-infrastructure` 1.0.0 pack
  (`config/source_packs/technology-internet-infrastructure.json`).
- Offline evidence: authored fixtures (documentation ASN AS64500, prefixes
  192.0.2.0/24 and 198.51.100.0/24, 2001:db8::/32, `example.org`, fictional
  organisations, dates in 2094-2099):
  `tests/fixtures/source_packs/technology-internet-infrastructure-*.json`
  (pinned in the source pack) and the revision, removal, deprecation and
  transfer fixtures in `tests/fixtures/internet_infrastructure/`.
- Offline acceptance (II12): `tests/unit/domains/test_internet_infrastructure_acceptance.py`
  (a declared ASN and domain to cited routing, interconnection, registration
  and certificate records with revisions, sockets blocked) and the
  [guide](../../guides/technology-internet-infrastructure.md). Status:
  covered offline (II02-II12), not live.
- Live evidence: none. II13 (live validation) has not run: the provider hosts
  are blocked by this runtime's egress proxy. Until a dated live run exists
  every source is `unverified-live`, and offline coverage is never reported
  as live coverage.
