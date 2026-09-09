"""Compare native constrained/unconstrained proposals with actual review notices."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.evaluation.report_generation import propose_report_revision
from src.evaluation.runtime_jobs import execute_job


def evaluate(berlin_xml=None):
    import duckdb

    from src.ingestion.revisions import DocumentRevisionStore
    from src.kb.report_updates import ReportUpdateStore

    report = {
        "contract": "noesis-report-proposal-comparison-v1",
        "label_origin": "authored regression expectations, not independent human quality judgments",
        "configuration": {
            "max_new_tokens": 512,
            "timeout_s": 120,
            "max_rss_bytes": 3 * 1024**3,
        },
        "dependency_versions": {
            name: importlib.metadata.version(name)
            for name in ("outlines", "llguidance", "transformers", "torch")
        },
        "cases": [],
    }
    cases = (
        (
            "de",
            "Das Beispielprojekt begann 2023.",
            "Korrektur: Das Beispielprojekt begann 2024.",
        ),
        (
            "en",
            "The example project began in 2023.",
            "Correction: the example project began in 2024.",
        ),
    )
    expected_new, expected_old = "2024", "2023"
    source_metadata = {}
    if berlin_xml is not None:
        from src.ingestion.regional_providers import parse_berlin_juris_xml

        if berlin_xml.stat().st_size > 20_000_000:
            raise ValueError("bounded captured Berlin XML required")
        raw = berlin_xml.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest != "b701bc5b07cb78c22a9db24b39c74a15f7a896ea2b333a3002e6c41f8610fb8a":
            raise ValueError("frozen native Berlin capture changed")
        source_url = "https://gesetze.berlin.de/jportal/bsbeAizDownload/Verf_BE.zip?doc.id=jlr-NNLBE000047B0&doc.part=X&_=%2FVerf_BE.zip"
        parsed = parse_berlin_juris_xml(raw, source_url=source_url)[0]
        expected_new, expected_old = parsed["fields"]["enactment_date"], "1996-11-23"
        excerpt = json.dumps(parsed["native"]["norms"][0], ensure_ascii=False)
        source_metadata = {
            "source_url": source_url,
            "original_sha256": digest,
            "locator": "/dokumente/norm[1]/metadaten/ausfertigung-datum",
            "expected_value": expected_new,
        }
        report["source_capture"] = source_metadata
        report["label_origin"] = (
            "Independent published Berlin enactment-date metadata; deliberately incorrect authored starting assertions. No independent human edit-quality study."
        )
        cases = (
            (
                "de",
                "Die Verfassung von Berlin wurde am 1996-11-23 ausgefertigt.",
                excerpt,
            ),
            ("en", "The Berlin constitution was enacted on 1996-11-23.", excerpt),
        )
    for language, before, after in cases:
        conn = duckdb.connect(config={"threads": 1, "memory_limit": "256MB"})
        try:
            sources = DocumentRevisionStore(conn)
            initial = sources.observe(
                {"document_id": "doc", "content": before, "metadata": {}}
            )
            dep = {
                "kind": "source",
                "id": "doc",
                "revision": initial["revision_id"],
                "namespace": "sources",
                "locator": {
                    "document_id": "doc",
                    "revision_id": initial["revision_id"],
                    "start": 0,
                    "end": len(before),
                },
            }
            assertion = {
                "id": "a",
                "text": before,
                "kind": "sourced",
                "dependencies": [dep],
                "citations": ["source-1"],
            }
            content = {
                "title": "Authored regression report",
                "snapshot": {"id": "snapshot:1", "generations": {"sources": 1}},
                "sections": [
                    {"id": "s", "title": "Summary", "assertions": [assertion]}
                ],
                "bibliography": [
                    {"id": "source-1", "text": "Authored regression source"}
                ],
                "limitations": ["Not independent human evaluation"],
            }
            auth = {
                "principal_id": "benchmark",
                "scopes": {
                    "knowledge:reports:read",
                    "knowledge:reports:write",
                    "namespace:r:write",
                    "namespace:sources:read",
                    "document:doc:read",
                },
            }
            store = ReportUpdateStore(conn)
            original = store.create("r", "report", content, **auth)
            sources.observe(
                {"document_id": "doc", "content": after, "metadata": source_metadata}
            )
            assessment = store.assess("r", original["report_id"], **auth)
            start = time.monotonic()
            notice = store.propose("r", assessment["assessment_id"], "a", **auth)
            row = {
                "language": language,
                "before": before,
                "captured_correction": after,
                "notice": {"elapsed_s": time.monotonic() - start, "result": notice},
                "runs": {},
            }
            captured = []

            class Captured(Exception):
                pass

            def capture(request, captured=captured):
                captured.append(request)
                raise Captured

            try:
                propose_report_revision(
                    store,
                    "r",
                    assessment["assessment_id"],
                    "a",
                    generator=capture,
                    **auth,
                )
            except Captured:
                pass
            request = captured[0]
            row["authorized_request"] = request
            for operation in ("outlines", "report-unconstrained"):
                job = execute_job(
                    operation,
                    {**request, "max_new_tokens": 512},
                    timeout_s=120,
                    max_rss_bytes=3 * 1024**3,
                )
                result = {
                    "job": job,
                    "usable_schema_valid_proposal": job["status"] == "completed",
                    "independent_substantive_quality": "not_measured",
                }
                if job["status"] == "completed":
                    value = job["result"]
                    text = value["proposal"]["assertion"]["text"]
                    result["authored_correction_probe"] = {
                        "contains_expected_value": expected_new in text,
                        "retains_incorrect_value": expected_old in text,
                        "semantics": "literal diagnostic, not entailment or human edit quality",
                    }
                    result["pending_review_artifact"] = propose_report_revision(
                        store,
                        "r",
                        assessment["assessment_id"],
                        "a",
                        generator=lambda _, value=value: value,
                        **auth,
                    )
                row["runs"][operation] = result
            row["report_revision_unchanged"] = (
                store.inspect("r", original["report_id"], **auth)["revision"]
                == original["revision"]
            )
            if not row["report_revision_unchanged"]:
                raise AssertionError("benchmark auto-published a report revision")
            report["cases"].append(row)
            print(
                language,
                {k: v["job"]["status"] for k, v in row["runs"].items()},
                flush=True,
            )
        finally:
            conn.close()
    report["decision"] = (
        "Defer: retain deterministic notices and explicit review. Native schema validity and authored date probes do not establish independent substantive edit quality or evidence support."
    )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--berlin-xml", type=Path)
    args = parser.parse_args()
    result = evaluate(args.berlin_xml)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
