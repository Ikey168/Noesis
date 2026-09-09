# Optional formatted research reports (#1523)

Install `.[report-rendering]`: pypandoc_binary **1.17**, bundled Pandoc **3.9**,
and Typst **0.15.0**. Pandoc uses GPL-2.0-or-later; the Python wrapper is MIT;
Typst uses Apache-2.0. The repository's own `noesis-author-date-v1.csl` style has
German/English page labels; its SHA-256 and locale are pinned in each receipt.
No downloaded style or locale files are required. This implementation currently
targets Linux/POSIX because the worker uses process resource limits.

```python
from src.kb.citeproc_export import render_report

exported = render_report(
    report_store, "research", report_id,
    principal_id="researcher", scopes=current_scopes,
    revision=3, locale="de-DE", output_format="docx",  # or pdf
    citation_metadata={
        "existing-bibliography-id": {
            "type": "report", "title": "Berliner Forschungsdaten",
            "author": [{"family": "Müller", "given": "Jörg"}],
            "issued": {"date-parts": [[2025]]}, "publisher": "Berlin",
        },
    },
    locators={"existing-assertion-id": {"existing-bibliography-id": "12"}},
)
output_bytes = exported["content"]
```

Citation metadata is a versioned export sidecar keyed by existing bibliography
IDs. The authored report schema and native Markdown exporter remain unchanged.
Metadata allows a bounded subset of CSL JSON: type, title, authors, date,
container-title, publisher, DOI, URL, volume, issue and page. Missing title,
author or year causes explicit authored-text fallback; malformed supplied fields
are rejected. Every authored entry remains in the output even when a formatted
reference is available. Page locators are separate per assertion/citation.
Unknown IDs and citations not attached to that assertion are rejected.

The renderer constructs a literal Pandoc JSON AST. Report text is not interpreted
as Markdown, HTML or Typst code, and citation fields cannot contain markup.
Assertion IDs, report ID/revision, limitations and source dependency locators
appear in the output; full dependencies are retained in footnotes and the export
receipt's original report package. Repeated citations share one formatted
bibliography entry. Formatting does not certify evidence support.

DOCX uses Pandoc's bundled writer. PDF uses Pandoc's Typst writer followed by the
pinned Typst Python compiler, with a temporary project root. No external images,
filters, user templates, arbitrary renderer flags or package imports are exposed.
Pandoc runs with `--sandbox`; the restricted AST prevents raw includes or asset
fetches. The worker drops inherited provider credentials from its environment.
PDF font rendering uses the compiler's default fonts; embedding a custom font
set and large-table/page-break fidelity have not been evaluated.

Input report plus metadata is capped at 2 MB; output and each child-created file
at 20 MB. Each child has 20 seconds of CPU, 30 seconds wall time and 64 file
descriptors. Pandoc's heap cap is 512 MB (GHC virtual address reservations are
not resident memory); Typst has a 2 GB address-space cap and one Rayon worker.
Limits are per child, not an aggregate host quota. Temporary files are removed
after success or failure. There are no external service costs.

Exports persist in `report_citeproc_exports` with their input/configuration hash,
renderer/style versions, output bytes/hash, original package and fallback IDs.
Repeating an authorized request verifies and returns stored bytes without
rendering again. Current report authorization is checked before replay; changed
revisions or metadata create a new export identity. Concurrent writers should
use the repository's normal serialized DuckDB writer; this API does not add a
new distributed lock.

## Measured evaluation and decision

```sh
.venv/bin/python -m pytest tests/unit/kb/test_citeproc_export.py tests/unit/kb/test_authored_reports.py tests/unit/kb/test_report_updates.py tests/unit/kb/test_integrity_ledger.py -q --override-ini addopts=''
.venv/bin/python scripts/evaluate_citeproc.py --out docs/development/workflow-implementation-evidence/citeproc
```

The combined regression passed **20 tests**, including actual DOCX/PDF engines,
umlauts, German/English page labels, repeated citations, source footnotes,
incomplete metadata, literal asset syntax, authorization, tamper detection,
database-reopen replay and actual timeout/file-limit failures.

On 2026-09-06 the synthetic Berlin fixture passed all independent structure
checks in four outputs. DOCX took **179/180 ms** (de/en), PDF **245/250 ms**;
native Markdown took **0.85 ms**. Outputs were 11–25 KB; maximum observed child
RSS was **193300 KiB**. These are single-run measurements, not statistical
performance claims. The German PDF was also visually inspected: one page with
readable footnotes and no clipped text.

Decision: **adopt as an explicit optional export**, retaining Markdown as the
low-cost default. The tested format/locale support adds useful document outputs
at a higher execution cost. This small fixture does not establish arbitrary
document-layout fidelity or independently evaluate research assertions.

Review [German PDF](../development/workflow-implementation-evidence/citeproc/de-DE.pdf),
[German DOCX](../development/workflow-implementation-evidence/citeproc/de-DE.docx),
[English PDF](../development/workflow-implementation-evidence/citeproc/en-US.pdf),
and [measurements and receipts](../development/workflow-implementation-evidence/citeproc/evaluation.json).
Primary setup references: [Pandoc manual](https://pandoc.org/MANUAL.html),
[Typst Python binding](https://github.com/messense/typst-py).
