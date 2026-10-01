# Platform transparency (OSINT)

The OSINT pack's optional platform-transparency features (#2580, provider
`osint.platform-transparency`) answer what platforms published about
moderation decisions and political advertising: EU DSA statements of reasons
and Meta and Google political-ad records, each with its source, record
revision and as-of time. Source decisions and the data-minimisation decision:
[`source-audit.md`](../development/platform-transparency-evidence/source-audit.md).

## Features

| Feature (default off) | Adds | Notes |
| --- | --- | --- |
| `platform-transparency-dsa` | DSA Transparency Database light daily dumps of declared platforms and days | statements keyed by platform and `uuid`; dump versions with published SHA-1 |
| `platform-transparency-meta` | Meta Ad Library political and issue ads of declared pages, countries and window | needs `NOESIS_META_AD_LIBRARY_TOKEN` |
| `platform-transparency-google` | Google political ads (BigQuery public dataset) of declared advertisers per region | needs `NOESIS_GOOGLE_BIGQUERY_TOKEN` and a billing project |
| `platform-transparency-lumen` | the `takedown-notice` record kind only | Lumen is **not implemented**: no research token, terms unverifiable |

Elections, campaign-finance and lobbying links use the Political features when
they are installed and report `*_unavailable` or `missing_targets` otherwise.

## Journey

1. Acquire through the source-pack tools (pack `bounded-public-osint` 1.2.0,
   sources `platform-transparency-dsa-sor`, `platform-transparency-meta-ads`,
   `platform-transparency-google-political-ads`). Runs are explicit; nothing is
   polled in the background.
2. `propose_platform_transparency_identity_matches` offers reviewable matches:
   an FEC committee id Google publishes for an advertiser first, then equal
   names in one country against committees, regulated entities, party lists,
   lobbying registrants and legal entities. Persons are never targets.
   Review with `review_platform_transparency_identity_match`; revert with
   `revert_platform_transparency_identity_match`. Unreviewed subjects stay
   `unmatched`. `register_platform_transparency_platforms` records each
   platform as a source identity.
3. `link_platform_transparency_campaign_finance`, `..._elections` and
   `..._lobbying` link ad revisions to filing versions, elections (with the
   delivery dates relative to election day as published) and register
   revisions, each with its basis.
4. Ask:
   * `platform_transparency_ads_by_advertiser` - a page id, Google advertiser
     id, declared name, or an FEC committee id / other pack's record reached
     through accepted decisions (the path is shown);
   * `platform_transparency_ads_for_election` - an elections election id;
   * `platform_transparency_moderation_statements` - a platform and period,
     optionally a ground: counts by decision type, ground and category and the
     automated flags as published, the stored window and the dump versions;
   * `platform_transparency_record_history` - every revision of one record;
   * `export_platform_transparency_evidence_bundle` - the answer as cited
     assertions.
5. Monitor an advertiser, election or platform with
   `create_platform_transparency_monitor`; `run_...` at a committed watermark
   reports `ad_published`, `ad_ranges_revised`, `ad_not_returned`,
   `ad_relisted`, `dump_released` and `dump_republished`, each citing the new
   and previous revision.

## Reading the answers

* Spend and impression ranges are the published bounds (Meta) or the
  published range and bucket text (Google). They are never turned into a
  midpoint, summed or estimated.
* `not-returned` means a complete listing of the same declared unit no longer
  returned the ad; the platform did not say why. It is a revision, never a
  deletion, and the ad keeps its earlier revisions.
* Moderation counts are over stored statements only, for the stored window;
  they are not a platform's totals.
* Every item cites its source, record revision, the source's own as-of date
  and when Noesis recorded it.

## Exclusions

No user-level profiling, no collection of private content, no inference of
coordinated behaviour and no conversion of ranges into point estimates. Tool
outputs that would carry a withheld field (platform user or content ids, the
notifier, free text, targeting or demographic breakdowns, creative text,
snapshot URLs) or a point-estimate key are refused. No tool is behind the
OSINT review gate because none infers location, identity or coordination.

## Evidence

Offline: `tests/unit/domains/test_platform_transparency_acceptance.py`.
Live: none yet (SP14, #2650);
[evidence folder](../development/platform-transparency-evidence/README.md).
