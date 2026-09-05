"""Two-process persisted Crawlee queue probe with bounded browser concurrency."""

import argparse
import asyncio
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.metadata
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading


async def worker(storage, urls, limit):
    from crawlee.configuration import Configuration
    from crawlee.storages import RequestQueue
    from crawlee.crawlers import PlaywrightCrawler
    from crawlee import ConcurrencySettings

    config = Configuration(storage_dir=str(storage), purge_on_start=False)
    queue = await RequestQueue.open(name="noesis-resume", configuration=config)
    crawler = PlaywrightCrawler(
        configuration=config,
        request_manager=queue,
        max_requests_per_crawl=limit,
        max_request_retries=0,
        concurrency_settings=ConcurrencySettings(
            min_concurrency=1, desired_concurrency=1, max_concurrency=1
        ),
        request_handler_timeout=timedelta(seconds=10),
    )
    completed = []

    @crawler.router.default_handler
    async def handle(context):
        await context.page.wait_for_selector("article", timeout=2000)
        completed.append(context.request.url)

    await crawler.run(urls, purge_request_queue=False)
    return completed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--storage", type=Path)
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--urls", nargs="*", default=[])
    args = parser.parse_args()
    if args.storage:
        args.out.write_text(
            json.dumps(asyncio.run(worker(args.storage, args.urls, args.limit)))
        )
        return
    counts = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            counts[self.path] = counts.get(self.path, 0) + 1
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<article>Static evidence</article>"
                if self.path == "/first"
                else b'<script>setTimeout(()=>{document.body.innerHTML="<article>Delayed evidence</article>"},100)</script>'
            )

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    runs = []
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(3):
                out = root / f"{index}.json"
                command = [
                    sys.executable,
                    __file__,
                    "--storage",
                    str(root / "queue"),
                    "--out",
                    str(out),
                    "--limit",
                    str(index + 1),
                ]
                if index == 0:
                    command += ["--urls", base + "/first", base + "/second"]
                subprocess.run(
                    command, check=True, timeout=45, stdout=subprocess.DEVNULL
                )
                runs.append(json.loads(out.read_text()))
        assert [len(run) for run in runs] == [1, 1, 0], runs
        assert counts.get("/first") == counts.get("/second") == 1, counts
        args.out.write_text(
            json.dumps(
                {
                    "backend": "crawlee",
                    "version": importlib.metadata.version("crawlee"),
                    "status": "passed",
                    "completed_per_process": [len(run) for run in runs],
                    "page_fetch_counts": counts,
                    "concurrency_ceiling": 1,
                    "assessment": "Named queue survives a bounded stop and process restart without refetching completed pages. Delayed DOM readiness passes. The persisted request counter requires increasing the cumulative request ceiling on resume. Adaptive concurrency and crash-during-handler recovery are not established.",
                },
                indent=2,
            )
            + "\n"
        )
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
