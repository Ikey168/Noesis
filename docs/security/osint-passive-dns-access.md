# Passive DNS access decision (OX07)

Tracking: #2040, delivery issue #2047. This page records whether passive DNS
history for declared source domains may enter Noesis, provider by provider. It
follows the access-decision pattern used for the national company registers in
the Corporate Ownership pack (#1856, `docs/subsystems/corporate-ownership.md`).

**Decision: no provider is adopted. No passive DNS adapter ships.** The
co-hosting signal is served by the certificate-transparency projection (OX06,
`src/ingestion/crtsh.py`) and registrant organizations by RDAP (OX05,
`src/ingestion/rdap.py`), both of which are public registries with terms that
permit locator-level reuse.

> **Verification status.** The terms summarized below were assessed from the
> providers' published documentation as known when this page was written. No
> live terms page, contract or API was fetched for this decision. Every row
> marked *verify* must be checked against the provider's current terms before
> the decision is revisited; nothing below is legal advice.

## What Noesis would need from a provider

A passive DNS provider is usable only if **all** of these hold:

1. Historical resolutions (domain → IP, first/last seen) may be **stored** in
   a customer's own system beyond the session that fetched them.
2. Those resolutions may be **cited** in material shown to other people (an
   investigation dossier, an exported evidence bundle), at least as a locator
   plus attribution.
3. Lookups can be restricted to **domain-keyed** queries for domains Noesis
   already knows as sources. IP-keyed ("what else is on this address") and
   person-keyed lookups are out of scope for the OSINT pack (tracker non-goals
   in #2040, `exclusions` in `packs/osint/pack.json`).
4. Access is by per-deployment key with a documented rate limit, so it fits
   the agent-host budget model (key-gated, allowlisted, budgeted, no default).

## Candidates

| Provider | Access model | Authentication | Rate limits | May historical resolutions be stored and cited? | Decision |
| --- | --- | --- | --- | --- | --- |
| Farsight DNSDB (DomainTools) | Commercial REST API (DNSDB API v2, "flexible search"), tiered subscriptions; a limited community tier has existed | API key per account | Per-subscription query quotas (daily / per-minute) | Subscription terms restrict redistribution of DNSDB data to third parties and generally limit use to the licensee's internal security operations. Storing results in a shared evidence store and citing them to other readers is not something the standard terms grant. *verify* | **Reject** |
| SecurityTrails (Recorded Future) | Commercial REST API with a free developer tier (historical DNS, WHOIS) | API key | Monthly query quota per plan; free tier very small | API terms restrict resale and redistribution of returned data; free tier is for personal / evaluation use. Citation in material shared outside the licensee is not clearly permitted. *verify* | **Reject** |
| CIRCL Passive DNS (Luxembourg CERT) | REST API following the passive DNS common output format (draft-dulaunoy-dnsop-passive-dns-cof) | Account on request, granted to vetted security teams | Fair use, per account | Access is granted for incident response and research under CIRCL's terms; data is shared under traffic-light (TLP) handling and must not be republished. Noesis's evidence discipline requires shareable citations, which TLP-restricted data cannot provide. *verify* | **Reject** |
| VirusTotal (Google) domain resolutions | REST API v3 (`/domains/{domain}/resolutions`) | API key; public key for non-commercial use, premium for commercial | Public API: 4 requests/min, 500/day | Public API terms forbid use in commercial products and restrict redistribution; premium terms are contract-specific. *verify* | **Reject** |
| Mnemonic Passive DNS | REST API with a limited anonymous tier and keyed access | Optional API key | Anonymous tier heavily rate-limited | Terms of use for anonymous access restrict automated bulk use; redistribution terms unclear. *verify* | **Reject (unclear terms)** |

No candidate meets criterion 2 (citable in shared material) under its
standard terms as understood here, and several gate access on vetting that a
general-purpose deployment cannot assume. Per the issue's acceptance
criteria, the decision is recorded and **the item closes without code**.

## What would change the decision

A deployment that holds its own contract with a provider which explicitly
permits storing and citing historical resolutions may add an adapter later.
It must then:

- implement a `ReverseSearchProvider`-style protocol in `src/ingestion/`
  (one domain per call, bytes/timeout caps, no retries, receipted like
  `src/ingestion/wayback.py`), key-gated through the agent-host budget model
  and allowlisted, returning `no_provider_configured` when unset, with no
  default provider or key in the repository;
- accept only domains of existing source identities or an explicit domain
  argument, and refuse IP-keyed and person-keyed lookups in code (the domain
  validator in `src/ingestion/osint_observations.py` already refuses IP
  literals, e-mail addresses and wildcards);
- store observations as `osint-observation-v1` and project IP-to-domain
  co-hosting only as a `shared-infrastructure` relationship revision
  (`status: probable`, with the shared-hosting/CDN caveat), exactly as the
  certificate-transparency projection does;
- update this page with the provider's verified terms, the review in
  `docs/security/osint-review-gate.md`, and the abuse analysis.

Nothing here enumerates infrastructure of arbitrary organizations; that stays
excluded by the tracker's non-goals.
