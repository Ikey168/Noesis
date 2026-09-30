Authored fixtures for the News pack's fact-checks provider (#2659). Every publisher, claimant, claim, URL
and identifier is fictional (`.example` hosts; Wikidata `Q99999901` and ROR `0abcd1234` are placeholders).
The shapes follow the providers' documented formats as known without network access (Fact Check Tools
`claims:search` JSON, Data Commons ClaimReview `DataFeed` JSON-LD) and an assumed IFCN listing markup;
none of it is live evidence. Personal fields (a job title, an image, a social profile, a review's author)
are present on purpose so tests can show that the parser drops them. `v2/` holds the later responses
(a revised review, a later release and a later listing).
