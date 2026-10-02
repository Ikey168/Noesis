# Platform transparency (OSINT `platform-transparency-*` features)

Tracking: #2580 (subdomain `social-platforms`, ADR-005). The OSINT pack's
optional platform-transparency features (default off, provider
`osint.platform-transparency`) answer two questions:

- Given an **advertiser or an election**, which political ads did the
  platforms publish, with delivery dates and spend and impression ranges
  exactly as published?
- Given a **platform and a period**, which moderation decisions did it report
  to the EU DSA Transparency Database, by decision type and ground?

Every record carries its source, record revision and as-of time. Source
contracts, licences and the data-minimisation decision are in the
[source audit](../development/platform-transparency-evidence/source-audit.md).
There is no user-level profiling, no collection of private content, no
inference of coordinated behaviour and no conversion of spend or impression
ranges into point estimates.

## Enabling it

1. Select one or more features in the OSINT composition:
   `platform-transparency-dsa`, `platform-transparency-meta`,
   `platform-transparency-google`, `platform-transparency-lumen`. Each binds
   `osint.platform-transparency`, `ownership.identity`,
   `platform.subscriptions` and `platform.source-acquisition`. Elections,
   campaign finance, lobbying and Corporate Ownership are never required; their
   links degrade to an `unavailable` report when absent.
2. Install the separate `osint-platform-transparency` 1.0.0 source pack
   (`config/source_packs/osint-platform-transparency.json`), accept the
   licences of its sources and declare the bounded units in each source's
   `platform_transparency.selection`:
   - `dsa-sor-dumps` - (platform, day, `light`) dump files;
   - `meta-ad-library-political` - a page id, reached countries, a delivery
     window of at most 366 days and optionally an elections id; needs
     `NOESIS_META_AD_LIBRARY_TOKEN` (identity-confirmed account);
   - `google-political-ads` - up to 20 advertiser ids (`AR…`) and the mapping
     of Google's published election labels to elections ids;
   - `lumen-notices` - a recipient platform and at most 92 days; needs a
     Lumen researcher token `NOESIS_LUMEN_API_TOKEN`. **This deployment has no
     researcher access**: the source is `gated-not-granted`, a run without the
     token fails preflight with `credential_missing`, and readiness lists Lumen
     as degraded.
3. Run the sources explicitly; nothing polls in the background.

All four sources are `unverified-live` (Lumen: `gated-not-granted`) until the
dated live run (#2650). Offline coverage is not live coverage.

## What is stored (`src/kb/platform_transparency_records.py`)

`noesis-platform-transparency-record-v2` revisions of statements of reasons,
DSA dump releases (file, variant, SHA-256 - the dump version), ads, advertisers
and takedown notices. Revisions are immutable: a changed record is `revised`,
an older response is an `older-observation`, and a record the source no longer
returns for the same declared selection is a **`not-returned` revision** - an
observed absence, never a deletion. `as_of` answers which revision was on
record at a time. The minimisation decision is enforced at write time: DSA
free text, content ids and notifier identity, ad creative text and audience
distributions, Lumen bodies, works and URLs, and any access token are refused.

## Answers (`src/kb/platform_transparency_queries.py`)

| Tool | Answer |
| --- | --- |
| `political_ads_for_advertiser` | ads of a Google advertiser (`AR…`), a Meta page id, a declared funding entity, or a campaign-finance committee, lobbying registrant or legal entity reached **only through an accepted identity decision**; ranges as published, removed ads with their removal revision, every ad cited by revision; `as_of` for an earlier state; `none_on_record` is not evidence that none ran |
| `political_ads_for_election` | ads linked to an elections record by declared selection or a published election label, grouped by advertiser |
| `moderation_statement_counts` | counts of stored statements by decision type, ground and category, automated flags as published, the stored window, days without a dump and the dump versions used - not the platform's totals |
| `platform_takedown_notices` | Lumen notices with redactions as published, only with `knowledge:osint:platform-transparency:notices:read` (otherwise counted) |
| `export_platform_transparency_evidence_bundle` | the answer as assertions citing source, revision and as-of time |

## Identity and links

`propose_platform_transparency_identity_matches` proposes published FEC
committee ids (from Google's `Public_IDs_List`) before names, then
name-and-country candidates against campaign-finance committees and regulated
entities, lobbying registrants and clients and Corporate Ownership legal
entities. Reviewers accept, reject or revert in the shared ownership identity
state machine; nothing is merged. Natural-person records are never targets.
`link_platform_transparency_records` then links ad revisions to elections,
campaign-finance registrations (with the filing versions on record), lobbying
register revisions and ownership records, each with its basis; missing targets
are reported.

## Monitoring

`create_platform_transparency_monitor` watches an advertiser, an election or a
platform as a knowledge subscription (no new scheduler). Notices `new_ad`,
`ad_revised`, `ad_not_returned`, `ad_listed_again`, `new_dump_release` and
`dump_republished` cite the new and previous revision and state what changed;
revisions that only restate a source's refresh time notify nothing.

## Offline acceptance

`tests/unit/domains/test_platform_transparency_acceptance.py` replays the
pinned fixtures through the source-pack runtime with sockets blocked; every
platform, advertiser and notice in them is fictional.
