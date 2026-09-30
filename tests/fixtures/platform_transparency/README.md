Authored, synthetic responses for the OSINT platform-transparency features (#2580), in the providers' documented
shapes: DSA Transparency Database light-dump CSV columns (zipped deterministically by
`tests/unit/platform_transparency_harness.py`), Meta Ad Library `ads_archive` JSON pages, and BigQuery REST
`tables.get` and `jobs.query` responses for the `google_political_ads` tables. Every platform, page, advertiser,
funder and statement is fictional; the year 2099 and `example.invalid` are placeholders. Withheld columns (notifier
identity, platform content id, free text, snapshot URL, demographic breakdown) carry synthetic placeholders that the
parser discards. `v2/` holds a later acquisition: a republished dump with one corrected statement, revised Meta
ranges with one ad no longer returned and one new ad, and a later Google refresh with a moved impression bucket and
one creative no longer returned. Nothing here is live evidence.
