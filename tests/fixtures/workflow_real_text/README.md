# Pinned real text for production integration

`corpus.json` contains short government-published excerpts from the Federal Reserve's
2022-06-15 FOMC statement and NASA's 2022-07-12 Webb image article. Each document
records its source URL, publication date, retrieval date, and exact UTF-8 SHA-256.
There are no prepared `metadata.knowledge` outputs.

Run the real claim detector and local semantic provider with:

```bash
python scripts/workflow_production_check.py --output /tmp/noesis-production-check.json
```

Model dependencies and pinned claim weights must be installed. The script reports
failure when unavailable; it never substitutes fixture claims or hash vectors.
The two semantic queries are handpicked integration assertions, not independent
human relevance judgments. Metadata corrections and retractions are simulated on
test copies; neither source publisher is alleged to have retracted the article.

The check covers production extraction, interruption/resume, stable replay,
source-revision citations, semantic retrieval, subscription events, corrections,
retractions in derived projections, and structural export verification. Unit tests
separately cover unavailable models and partial source coverage.
