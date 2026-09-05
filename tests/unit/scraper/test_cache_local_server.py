import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import threading


def test_persistent_cache_revalidation_change_error_recovery_and_offline(tmp_path):
    state = {"body": "first", "etag": "one", "requests": 0, "status": 200}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"] += 1
            status = state["status"]
            if status == 200 and self.headers.get("If-None-Match") == state["etag"]:
                status = 304
            self.send_response(status)
            self.send_header("ETag", state["etag"])
            self.send_header("Cache-Control", "max-age=0")
            self.end_headers()
            if status != 304:
                self.wfile.write(state["body"].encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    script = Path(__file__).resolve().parents[3] / "scripts/check_scrapy_cache.py"

    def run(offline=False):
        output = tmp_path / "out.json"
        command = [
            sys.executable,
            str(script),
            "--url",
            f"http://127.0.0.1:{server.server_port}/article",
            "--cache",
            str(tmp_path / "cache"),
            "--out",
            str(output),
        ]
        if offline:
            command += ["--offline"]
        subprocess.run(command, check=True, timeout=15, capture_output=True)
        return json.loads(output.read_text())[0]

    try:
        first = run()
        checked = run()
        assert first["text"] == checked["text"] == "first"
        assert checked["provenance"]["mode"] == "live-revalidated"
        assert (
            checked["provenance"]["original_fetched_at"]
            == first["provenance"]["original_fetched_at"]
        )
        state.update(body="second", etag="two")
        changed = run()
        assert changed["text"] == "second"
        state["status"] = 503
        run()
        state.update(status=200, body="third", etag="three")
        assert run()["text"] == "third"
        requests = state["requests"]
        replay = run(True)
        assert (
            replay["text"] == "third"
            and replay["provenance"]["mode"] == "offline-replay"
        )
        assert state["requests"] == requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
