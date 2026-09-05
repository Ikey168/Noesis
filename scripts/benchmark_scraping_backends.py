"""Bounded local fixture benchmark for repaired and optional scraping backends."""

import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import resource
import signal
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.scraper.backend_evaluation import fetch_backend
from src.ingestion.extract import extract_article

TEXT = "A precisely attributed source explains the evidence and its limitations. " * 8
PAGES = {
    "/static": "<article><h1>Fixture</h1><p>" + TEXT + "</p></article>",
    "/delayed": "<html><body><script>setTimeout(()=>{document.body.innerHTML="
    + json.dumps("<article><h1>Fixture</h1><p>" + TEXT + "</p></article>")
    + "},100)</script></body></html>",
    "/missing": "<html><body>No article is available.</body></html>",
}


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
        start = time.monotonic()
        try:
            html = fetch_backend(args.url, args.backend)
            if len(html) > 2_000_000:
                raise ValueError("page budget exceeded")
            result = extract_article(html, url=args.url)
            output = {
                "status": "completed",
                "text": result.text if result else "",
                "snapshot_sha256": hashlib.sha256(html.encode()).hexdigest(),
                "acquisition_metadata": getattr(html, "acquisition_metadata", None),
                "extraction_metadata": result.metadata if result else None,
            }
        except Exception as exc:
            output = {
                "status": "failed",
                "failure_type": type(exc).__name__,
                "failure": str(exc)[:200],
            }
        output.update(
            elapsed_seconds=time.monotonic() - start,
            peak_parent_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        )
        args.out.write_text(json.dumps(output))
        return

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            content = (
                "User-agent: *\nAllow: /"
                if self.path == "/robots.txt"
                else PAGES.get(self.path, "")
            )
            self.send_response(200 if content else 404)
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
            for route, html in PAGES.items():
                with tempfile.TemporaryDirectory(prefix="noesis-crawl-eval-") as temp:
                    target = Path(temp) / "result.json"
                    command = [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--backend",
                        backend,
                        "--url",
                        f"http://127.0.0.1:{server.server_port}{route}",
                        "--out",
                        str(target),
                    ]
                    process = subprocess.Popen(
                        command,
                        cwd=temp,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                    try:
                        process.wait(timeout=45)
                        result = (
                            json.loads(target.read_text())
                            if target.exists() and target.stat().st_size < 5_000_000
                            else {
                                "status": "failed",
                                "failure_type": "missing_or_oversized_output",
                            }
                        )
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                        result = {
                            "status": "failed",
                            "failure_type": "whole_process_timeout",
                        }
                text = result.pop("text", "")
                results.append(
                    {
                        "backend": backend,
                        "fixture": route,
                        "fixture_sha256": hashlib.sha256(html.encode()).hexdigest(),
                        **result,
                        "expected_body_recall": sum(
                            word in text.split() for word in TEXT.split()
                        )
                        / len(TEXT.split())
                        if route != "/missing"
                        else None,
                        "false_body_on_empty": bool(text)
                        if route == "/missing"
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
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(
            {
                "corpus": "authored local static/delayed/missing-article fixtures; not production anti-bot or independent fidelity evaluation",
                "versions": versions,
                "runs": results,
                "decision": "retain repaired production stack; defer optional backend adoption pending representative corpus, restart/deadline/resource checks and credentialed cost measurements",
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
