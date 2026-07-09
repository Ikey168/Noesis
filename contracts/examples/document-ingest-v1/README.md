# document-ingest-v1 examples

Golden fixtures for the generalized `document-ingest-v1` contract (M0 keystone of
the knowledge-engine pivot — see `docs/architecture/knowledge-engine-pivot.md`).

`Document` generalizes the news-specific `ArticleIngest`; `news` is now one
`source_type` among `news | blog | book | paper | transcript | web | note`.
Enrichments (sentiment, topics, ...) are produced downstream and are **not** part
of this core record.

Timestamps (`created_at`, `ingested_at`) are milliseconds since epoch, matching the
Avro schema (`contracts/schemas/avro/document-ingest-v1.avsc`) used for runtime
validation by `services/ingest/common/document_contracts.py`.

## valid/
| Fixture | source_type | Highlights |
| --- | --- | --- |
| `valid-paper.json` | paper | arXiv id, DOI, `content_ref` to object storage, reference DOIs |
| `valid-book.json` | book | ISBN, multiple authors, no `url`/`source_id` |
| `valid-news.json` | news | country/section in `metadata` (article specialization) |
| `valid-blog-minimal.json` | blog | only the four required fields |
| `valid-transcript.json` | transcript | media metadata, speakers, timestamped body |

## invalid/
| Fixture | Why it fails |
| --- | --- |
| `invalid-missing-document-id.json` | missing required `document_id` |
| `invalid-bad-source-type.json` | `source_type` not in the allowed enum |
| `invalid-missing-language.json` | missing required `language` |
| `invalid-missing-ingested-at.json` | missing required `ingested_at` |

Required fields: `document_id`, `source_type`, `language`, `ingested_at`.

Tested by `contracts/tests/test_document_contract.py`.
