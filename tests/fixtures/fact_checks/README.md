# Fact-checks fixtures (synthetic)

Authored responses in the documented shapes of the Google Fact Check Tools API (`claims:search` JSON), the Data
Commons ClaimReview `DataFeed` (schema.org JSON-LD) and the IFCN signatories listing (HTML; markup assumptions per
`docs/development/fact-checks-evidence/source-audit.md`). Every publisher, site, claimant, reviewer and claim is
fictional (`*.example.*` hosts, a fictional Wikidata-shaped identifier). Placeholder reviewer and appearance-author
names, images, logos and job titles exist only to prove that the parsers discard them (FC01 minimisation). `v2/`
holds later acquisitions: a publisher's revised rating, a review dropped from a later release and an IFCN status
change with a signatory missing from the listing. Nothing here is live coverage.
