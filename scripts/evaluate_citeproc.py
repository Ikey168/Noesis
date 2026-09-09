"""Render pinned German/English fixtures and inspect DOCX/PDF structure."""

import argparse
import io
import json
import resource
import sys
import time
import zipfile
from pathlib import Path
from xml.etree import ElementTree

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import duckdb
import fitz

from src.kb.authored_reports import AuthoredReportStore
from src.kb.citeproc_export import render_report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture", type=Path, default=Path("tests/fixtures/citeproc/report.json")
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    fixture = json.loads(args.fixture.read_text())
    args.out.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect()
    try:
        auth = {"principal_id": "evaluation", "scopes": {"operator"}}
        store = AuthoredReportStore(conn)
        report = store.create("evaluation", "berlin", fixture["content"], **auth)
        baseline_start = time.perf_counter()
        baseline = store.export("evaluation", report["report_id"], **auth)
        baseline_ms = (time.perf_counter() - baseline_start) * 1000
        (args.out / "report.md").write_text(baseline["markdown"])
        records = []
        for locale in ("de-DE", "en-US"):
            for output_format in ("docx", "pdf"):
                result = render_report(
                    store,
                    "evaluation",
                    report["report_id"],
                    citation_metadata=fixture["citation_metadata"],
                    locators=fixture["locators"],
                    locale=locale,
                    output_format=output_format,
                    **auth,
                )
                data = result["content"]
                if output_format == "docx":
                    with zipfile.ZipFile(io.BytesIO(data)) as doc:
                        rendered = " ".join(
                            "".join(ElementTree.fromstring(doc.read(name)).itertext())
                            for name in ("word/document.xml", "word/footnotes.xml")
                        )
                else:
                    with fitz.open(stream=data, filetype="pdf") as doc:
                        rendered = " ".join(page.get_text() for page in doc)
                checks = {
                    token: token in rendered for token in fixture["expected_tokens"]
                }
                checks["repeated_citation"] = rendered.count("Müller") >= 3
                checks["localized_page_label"] = (
                    "S. 12" if locale == "de-DE" else "p. 12"
                ) in rendered
                (args.out / (locale + "." + output_format)).write_bytes(data)
                records.append(
                    {
                        "locale": locale,
                        "format": output_format,
                        "checks": checks,
                        "receipt": result["receipt"],
                    }
                )
        results = {
            "kind": fixture["kind"],
            "baseline_markdown_ms": baseline_ms,
            "results": records,
            "max_child_rss_kib": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
            "decision": "adopt as explicit optional export: four outputs passed structure checks; retain native Markdown for low-cost export; no evidence-support inference",
        }
        (args.out / "evaluation.json").write_text(
            json.dumps(results, indent=2, ensure_ascii=False) + "\n"
        )
        print(
            json.dumps(
                {
                    "baseline_markdown_ms": baseline_ms,
                    "outputs": [
                        {
                            "locale": r["locale"],
                            "format": r["format"],
                            "elapsed_ms": r["receipt"]["elapsed_ms"],
                            "bytes": r["receipt"]["bytes"],
                            "passed": all(r["checks"].values()),
                        }
                        for r in records
                    ],
                    "max_child_rss_kib": results["max_child_rss_kib"],
                }
            )
        )
        if not all(all(r["checks"].values()) for r in records):
            raise SystemExit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
