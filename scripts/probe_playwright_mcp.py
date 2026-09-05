"""Live local Playwright MCP session probe; authored German dynamic page."""

import argparse
import json
import os
import signal
import socket
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from src.integrations.mcp import federation_adapter


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            '<html lang="de"><title>Berliner Forschung</title><h1>Berliner Forschung</h1><p id="status">Laden</p><script>setTimeout(()=>document.querySelector("p").textContent="Förderung geprüft",100)</script></html>'.encode()
        )

    def log_message(self, *a):
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--browser-path", required=True)
    args = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    try:
        with (
            tempfile.TemporaryDirectory(prefix="noesis-mcp-probe-") as folder,
            open(folder + "/server.log", "w") as log,
        ):
            process = subprocess.Popen(
                [
                    "npx",
                    "--yes",
                    "@playwright/mcp@0.0.80",
                    "--port",
                    str(port),
                    "--host",
                    "127.0.0.1",
                    "--allowed-hosts",
                    f"127.0.0.1:{port},localhost:{port}",
                    "--headless",
                    "--isolated",
                    "--executable-path",
                    args.browser_path,
                    "--output-dir",
                    folder,
                    "--block-service-workers",
                ],
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
            try:
                for _ in range(100):
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                            break
                    except OSError:
                        time.sleep(0.1)
                adapter = federation_adapter(
                    "playwright",
                    endpoint=f"http://127.0.0.1:{port}/mcp",
                    navigation_origins=[f"http://127.0.0.1:{server.server_port}"],
                )
                try:
                    first = adapter.query(
                        {
                            "kind": "tool",
                            "name": "browser_navigate",
                            "arguments": {
                                "url": f"http://127.0.0.1:{server.server_port}/berlin"
                            },
                        },
                        scopes={"operator"},
                    )
                    adapter.query(
                        {
                            "kind": "tool",
                            "name": "browser_wait_for",
                            "arguments": {"text": "Förderung geprüft"},
                        },
                        scopes={"operator"},
                    )
                    second = adapter.query(
                        {"kind": "tool", "name": "browser_snapshot", "arguments": {}},
                        scopes={"operator"},
                    )
                    assert "Förderung geprüft" in json.dumps(
                        second, ensure_ascii=False
                    ), second
                    output = {
                        "fixture_kind": "authored local German dynamic HTML; real MCP/browser execution, not production site evaluation",
                        "npm_package": "@playwright/mcp@0.0.80",
                        "server_version": adapter.client.version,
                        "navigation": first,
                        "snapshot": second,
                        "captured_files": {
                            p.name: p.read_text()
                            for p in Path(folder).glob("*.yml")
                            if p.stat().st_size < 1000000
                        },
                        "limitations": [
                            "Navigation allowlist constrains requested destinations; it is not a browser network sandbox or redirect policy.",
                            "Independent comparison against existing bulk browser acquisition remains outstanding.",
                        ],
                    }
                    args.out.parent.mkdir(parents=True, exist_ok=True)
                    args.out.write_text(
                        json.dumps(output, ensure_ascii=False, indent=2) + "\n"
                    )
                    print("Navigation, dynamic wait and same-session snapshot passed")
                finally:
                    adapter.client.close()
            finally:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                if process.returncode is None:
                    raise RuntimeError("MCP process cleanup failed")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == "__main__":
    main()
