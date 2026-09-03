# Private corpus quickstart

Noesis can index a personal corpus without RSS, cloud storage, or an external
model API. The supplied `private` domain has no feeds; only files you explicitly
ingest receive its `private` membership tag.

```bash
python -m venv .venv
.venv/bin/pip install duckdb pyyaml pyarrow pandas scikit-learn
.venv/bin/python scripts/private_corpus.py --db data/private/noesis.duckdb \
  ingest ~/Documents/notes ~/Documents/mail/export.eml report.pdf handbook.epub
.venv/bin/python scripts/private_corpus.py --db data/private/noesis.duckdb \
  query "What decisions were made about the launch?"
```

Use `--config config/private-sources.example.json` for a repeatable path list.
Files are deduplicated, assigned to the private domain, argument-mined, and
queried through `noesis-kb-v1`. Answers retain a `file://` locator or a media
timestamp so a user can inspect the source.

## Formats and offline behavior

| Input | Connector | Fully offline | Optional dependency / limitation |
| --- | --- | --- | --- |
| text, Markdown, HTML, CSV | upload | yes | stdlib parser |
| email (`.eml`, `.mbox`) | upload | yes | attachments are not recursively imported |
| DOCX | upload | yes | `python-docx` improves extraction; XML fallback is available |
| PDF | upload/book | yes | PyMuPDF or pdfminer; OCR needs Tesseract/Poppler |
| EPUB/book | book | yes | stdlib fallback; `ebooklib` preserves richer structure |
| audio/video | media | yes with a local Whisper backend | ffmpeg plus Whisper model weights; no API is called unless explicitly configured |

Heuristic claim/stance models are the offline fallback. Pinned pretrained
models run from the local Hugging Face cache when installed; they never trigger
an implicit download during a query.

## Security posture

- The database and source files stay on this machine. The CLI sets the DuckDB
  file to owner read/write (`0600`) after each run.
- DuckDB does **not** provide application-level database encryption. Put
  `data/private` on an encrypted volume (LUKS, FileVault, BitLocker, or an
  encrypted container) for encryption at rest. Do not treat mode bits as
  encryption.
- The `kb_mcp` and `kg_mcp` servers are read-only query surfaces. They do not
  expose file ingestion, deletion, shell access, or arbitrary SQL. Bind any
  HTTP transport to loopback unless you add authentication and TLS.
- Local transcription can be compute-intensive. Remote transcription and
  network harvesters are opt-in and are outside this offline workflow.

To erase the corpus, stop Noesis and delete the explicit database path and its
WAL sidecar, if present. Backups and encrypted-volume snapshots remain the
operator's responsibility.
