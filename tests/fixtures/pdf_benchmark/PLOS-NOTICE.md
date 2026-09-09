# Publisher PDF attribution and annotation scope

`plos-ioannidis-2005.pdf` is an unmodified publisher PDF of:

Ioannidis JPA (2005), *Why Most Published Research Findings Are False*, PLoS Medicine 2(8): e124. DOI: https://doi.org/10.1371/journal.pmed.0020124.

Copyright © 2005 John P. A. Ioannidis. The publisher grants distribution and reproduction under the Creative Commons Attribution License, provided the original work is cited. See the copyright paragraph on https://journals.plos.org/plosmedicine/article?id=10.1371/journal.pmed.0020124. The article has a separately published 2022 correction; this fixture preserves the exact downloaded PDF rather than applying that correction silently.

Downloaded 2026-09-08 from the publisher URL in `scientific-manifest.json`. That manifest records the exact SHA-256. The generated fixtures in `manifest.json` retain their separate CC0 license.

The assistant visually inspected rendered pages 1, 2 and 6 and recorded sparse title, heading, first-table-header and reference anchors. These are agent-authored structural expectations, not independent human labels or a comprehensive gold transcription. Rectangles are approximate visual bounds. Table spans and wrapped bibliography text are explicit failure cases for the current metrics. Token precision against sparse anchors must not be interpreted as whole-document extraction accuracy.

Use the same manifest for all parsers:

```sh
.venv/bin/python scripts/evaluate_pdf_backends.py --manifest tests/fixtures/pdf_benchmark/scientific-manifest.json --backends pymupdf docling grobid --grobid-url http://127.0.0.1:18070 --timeout-s 90 --out /tmp/scientific-pdf-results.json
```
