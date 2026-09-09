# EU and optional provider integration status

This guide covers issues #1471, #1478–#1490, #1495 and #1496. The implementations in `src/ingestion/roadmap_integrations.py` are bounded adapters/evaluation boundaries; they do not silently change production defaults. Offline tests use authored fixtures and injected transports. Those fixtures establish contract behavior, not live provider availability, quality or independent human relevance.

## Full text and regional public evidence

`OpenAlexContentAcquirer` consumes `has_content`/`content_urls`, preserves the OpenAlex work ID, per-work license, content hash, retrieval time and binary snapshot, parses TEI sections/references with coordinates, rejects malformed/oversized content, reserves configured metered cost before a request and locally replays the same work without a duplicate charge. Missing content remains `abstract-or-metadata-only`; it is never promoted to full text. Production OpenAlex paid-content acceptance remains **unavailable** until an explicitly authorised account-gated run records the actual plan, prices and native responses.

The regional adapter records provider-native data alongside a normalized identity/provenance envelope for CTIS, DRKS, CELLAR, Rechtsprechung im Internet, Berlin law, OpenCorporates, OpenSanctions, EMA and BfArM. Access posture is deliberately provider-specific: CTIS and Berlin are export/import paths unless a supported automation contract is verified; no undocumented REST API is invented. OpenSanctions records are review candidates only. OpenCorporates is enrichment and is not represented as an official-registry replacement. EMA coverage is not generalized to every national authorization, and BfArM scope is not generalized to PEI products.

The deterministic fixture `tests/fixtures/roadmap_integrations/eu-provider-records.json` contains German/EU identity, missing-field, locator, relationship, jurisdiction and review semantics. It is synthetic conformance data, not provider-native evidence. Live/export acquisition, licensing and changed-record replay must be recorded separately before those live acceptance boxes are treated as satisfied.

## Hosted discovery and extraction

`HostedDiscovery` supports Exa and Tavily only when explicitly enabled, credentialed and given an integer request-cost/budget ceiling. Query, domain and date controls are bounded. Native result IDs and provider summaries are retained on discovery candidates, but summaries are marked `provider_summary_is_evidence=false` and selected URLs require normal Noesis acquisition. No credentialed Exa/Tavily relevance/cost comparison was run in this implementation, so the adoption status is **defer / live evaluation unavailable** rather than a fabricated result.

`JinaReaderFallback` requires explicit remote-processing opt-in and an existing original-source snapshot. Its output is labeled transformed and document-level only; Markdown is never assigned precise source locators. Credentialed/public-page fidelity and cost comparison remains unavailable in the offline environment, so production fallback adoption is deferred.

## Document conversion and OCR evaluation

`optional_document_conversion` is an evaluation-only MarkItDown boundary. It retains the original SHA-256, enforces input/output bounds, reports a real installed package version when available, and marks Markdown locator fidelity as approximate. Malformed/converter failures are explicit. If the optional package is absent the status is `unavailable`; no conversion result is fabricated. Format-specific adoption therefore remains deferred pending real German/English office-document runs.

`normalize_paddleocr` maps actual or fixture PaddleOCR regions into page/bounding-box provenance with confidence and uncertainty flags, model/configuration identity, latency and RSS fields. The committed tests exercise adaptation only. No PaddleOCR model inference was run for this change, so character/word error, multilingual scan quality, rotated/table behavior and resource comparison are **not available** and the backend remains deferred pending a reproducible real-model benchmark.

## Reproduction

Run the deterministic contract checks with:

```sh
pytest -q tests/unit/ingestion/test_roadmap_integrations.py
ruff check src/ingestion/roadmap_integrations.py tests/unit/ingestion/test_roadmap_integrations.py
```

Any live/provider evaluation must use explicit credentials/terms, bounded public material, and separately persisted native responses/results. Paid or remote services must not receive private corpora by default.
