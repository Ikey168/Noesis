"""Bounded local fixture benchmark for repaired and optional scraping backends."""

import argparse
import hashlib
import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TEXT = "A precisely attributed source explains the evidence and its limitations. " * 8
PAGES = {
    "/static": "<article><h1>Fixture</h1><p>" + TEXT + "</p></article>",
    "/delayed": "<html><body><script>setTimeout(()=>{document.body.innerHTML="
    + json.dumps("<article><h1>Fixture</h1><p>" + TEXT + "</p></article>")
    + "},100)</script></body></html>",
    "/pagination-1": "<article><h1>Page one</h1><p>"
    + TEXT
    + "</p><a rel='next' href='/pagination-2'>Next</a></article>",
    "/pagination-2": "<article><h1>Page two</h1><p>" + TEXT + "</p></article>",
    "/lazy": "<html><body><script>setTimeout(()=>{document.body.innerHTML="
    + json.dumps("<article><h1>Lazy</h1><p>" + TEXT + "</p></article>")
    + "},250)</script></body></html>",
    "/edited-before": "<article><h1>Edit fixture</h1><p>"
    + TEXT
    + "version before.</p></article>",
    "/edited-after": "<article><h1>Edit fixture</h1><p>"
    + TEXT
    + "version after.</p></article>",
    "/http-error": "<html><body>Temporary server failure</body></html>",
    "/lazy-scroll": "<html><body><div style='height:2400px'></div><div id='sentinel'></div><script>new IntersectionObserver(entries=>{if(entries.some(e=>e.isIntersecting)){document.getElementById('sentinel').innerHTML="
    + json.dumps("<article><p>" + TEXT + "</p></article>")
    + ";}}).observe(document.getElementById('sentinel'))</script></body></html>",
    "/missing": "<html><body>No article is available.</body></html>",
}


from src.scraper.benchmark_worker import benchmark_one


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--backend")
    parser.add_argument(
        "--backends",
        nargs="+",
        default=[
            "scrapy",
            "playwright",
            "crawl4ai",
            "crawlee",
            "crawlee-adaptive",
            "zyte",
            "firecrawl",
        ],
    )
    parser.add_argument("--url")
    args = parser.parse_args()
    if args.backend:
        benchmark_one(args.url, args.backend, args.out)
        return

    served_edit = {"html": PAGES["/edited-before"]}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            content = (
                "User-agent: *\nAllow: /"
                if self.path == "/robots.txt"
                else served_edit["html"]
                if self.path == "/edited"
                else PAGES.get(self.path, "")
            )
            self.send_response(
                503 if self.path == "/http-error" else 200 if content else 404
            )
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(content.encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    results = []
    try:
        for backend in args.backends:
            if backend in ("zyte", "firecrawl"):
                results.append(
                    {
                        "backend": backend,
                        "status": "not_run",
                        "reason": "remote services cannot access local corpus; credentialed public-corpus evaluation and explicit price ceiling required",
                    }
                )
                continue
            for route, html in [*PAGES.items(), ("/restart-static", PAGES["/static"])]:
                with tempfile.TemporaryDirectory(prefix="noesis-crawl-eval-"):
                    source_route = route
                    if route.startswith("/edited-"):
                        source_route = "/edited"
                        served_edit["html"] = html
                    elif route == "/restart-static":
                        source_route = "/static"
                    url = f"http://127.0.0.1:{server.server_port}{source_route}"
                    from src.evaluation.runtime_jobs import execute_job

                    job = execute_job(
                        "scrape-fixture",
                        {"url": url, "backend": backend},
                        timeout_s=45,
                        max_rss_bytes=1024**3,
                        max_output_bytes=8_000_000,
                    )
                    result = job.get(
                        "result",
                        {
                            "status": job["status"],
                            "failure_code": job.get("failure_code"),
                        },
                    )
                    result["worker_receipt"] = {
                        key: value for key, value in job.items() if key != "result"
                    }
                    result["elapsed_seconds"] = job.get(
                        "elapsed_seconds", result.get("elapsed_seconds")
                    )
                    result["peak_process_tree_rss_bytes"] = job.get(
                        "peak_process_tree_rss_bytes"
                    )
                text = result.get("text", "")
                expected_title = {
                    "/static": "Fixture",
                    "/restart-static": "Fixture",
                    "/delayed": "Fixture",
                    "/pagination-1": "Page one",
                    "/pagination-2": "Page two",
                    "/lazy": "Lazy",
                    "/edited-before": "Edit fixture",
                    "/edited-after": "Edit fixture",
                }.get(route)
                metadata = result.get("extraction_metadata") or {}
                title = metadata.get("selected", {}).get("title", {})
                result["title_matches_expected"] = (
                    title.get("value") == expected_title if expected_title else None
                )
                result["metadata_bound_to_snapshot"] = (
                    metadata.get("snapshot_sha256") == result.get("snapshot_sha256")
                    if metadata
                    else False
                )
                result["title_locator_present"] = bool(title.get("locator"))
                from collections import Counter

                overlap = sum((Counter(TEXT.split()) & Counter(text.split())).values())
                results.append(
                    {
                        "backend": backend,
                        "fixture": route,
                        "fixture_sha256": hashlib.sha256(html.encode()).hexdigest(),
                        **result,
                        "expected_body_recall": overlap / len(TEXT.split())
                        if route not in {"/missing", "/http-error"}
                        and result["status"] == "completed"
                        else None,
                        "false_body_on_empty": bool(text)
                        if route in {"/missing", "/http-error"}
                        and result["status"] == "completed"
                        else None,
                    }
                )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    import importlib.metadata

    versions = {}
    for package in ["scrapy", "playwright", "crawl4ai", "crawlee", "trafilatura"]:
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    summaries = {}
    for backend in args.backends:
        values = [
            run for run in results if run["backend"] == backend and run.get("fixture")
        ]
        completed = [run for run in values if run["status"] == "completed"]
        latencies = sorted(run["elapsed_seconds"] for run in completed)

        def percentile(values, fraction):
            if not values:
                return None
            return values[
                min(len(values) - 1, max(0, round((len(values) - 1) * fraction)))
            ]

        content_runs = [
            run for run in values if run["fixture"] not in {"/missing", "/http-error"}
        ]
        by_route = {run["fixture"]: run for run in values}
        original, restarted = (
            by_route.get("/static", {}),
            by_route.get("/restart-static", {}),
        )
        restart_complete = all(
            run.get("status") == "completed" for run in (original, restarted)
        )
        before, after = (
            by_route.get("/edited-before", {}),
            by_route.get("/edited-after", {}),
        )
        edits_complete = all(
            run.get("status") == "completed" for run in (before, after)
        )
        summaries[backend] = {
            "usable_document_rate": sum(
                run.get("expected_body_recall") == 1.0 for run in content_runs
            )
            / len(content_runs)
            if content_runs
            else None,
            "locator_metadata_coverage": sum(
                bool((run.get("extraction_metadata") or {}).get("selected"))
                for run in content_runs
            )
            / len(content_runs)
            if content_runs
            else None,
            "restart": {
                "contract": "fresh-worker repeat of the same static URL; no persisted queue claimed",
                "completed": restart_complete,
                "same_text": original.get("text") == restarted.get("text")
                if restart_complete
                else None,
                "same_snapshot": original.get("snapshot_sha256")
                == restarted.get("snapshot_sha256")
                if restart_complete
                else None,
            },
            "same_url_source_edit": {
                "completed": edits_complete,
                "changed_text": before.get("text") != after.get("text")
                if edits_complete
                else None,
                "changed_snapshot": before.get("snapshot_sha256")
                != after.get("snapshot_sha256")
                if edits_complete
                else None,
            },
            "fixtures": len(values),
            "completed": len(completed),
            "completion_rate": len(completed) / len(values) if values else None,
            "p50_elapsed_seconds": median(latencies) if latencies else None,
            "p95_elapsed_seconds": percentile(latencies, 0.95),
            "max_peak_parent_rss_kib": max(
                (run["peak_parent_rss_kib"] for run in completed), default=None
            ),
            "max_peak_process_tree_rss_bytes": max(
                (run["peak_process_tree_rss_bytes"] or 0 for run in values),
                default=None,
            ),
            "live_cost": "unavailable" if backend in {"zyte", "firecrawl"} else 0,
        }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(
            {
                "corpus": "authored local static/JS/pagination/lazy/error/source-edit fixtures; not production anti-bot or independent fidelity evaluation",
                "cost_accounting": "local backends are recorded as zero provider cost; hosted backend cost remains unavailable until an authorized credentialed run with an explicit ceiling",
                "versions": versions,
                "runs": results,
                "summaries": summaries,
                "decision": "retain repaired production stack; defer optional backend adoption pending representative corpus, restart/deadline/resource checks and credentialed cost measurements",
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
