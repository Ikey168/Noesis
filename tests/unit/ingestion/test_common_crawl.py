import base64
import gzip
import hashlib
import io
import json

import duckdb
import pytest

pytest.importorskip("warcio")
from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import WARCWriter

from src.ingestion.common_crawl import CommonCrawlCollection


def capture(day):
    body = "Berliner Forschungsförderung und öffentliche Daten.".encode()
    out = io.BytesIO()
    writer = WARCWriter(out, gzip=False)
    record = writer.create_warc_record(
        "https://berlin.example/report",
        "response",
        payload=io.BytesIO(body),
        http_headers=StatusAndHeaders(
            "200 OK", [("Content-Type", "text/html")], protocol="HTTP/1.1"
        ),
        warc_headers_dict={"WARC-Date": f"2025-01-{day:02}T00:00:00Z"},
    )
    writer.write_record(record)
    compressed = gzip.compress(out.getvalue())
    entry = {
        "url": "https://berlin.example/report",
        "timestamp": f"202501{day:02}000000",
        "digest": base64.b32encode(hashlib.sha1(body).digest()).decode(),
        "filename": "crawl-data/CC-MAIN-2025-05/segments/fixture/warc/capture.warc.gz",
        "length": str(len(compressed)),
        "offset": str(day * 10000),
    }
    return entry, compressed


def options():
    return {
        "crawl": "CC-MAIN-2025-05",
        "host": "berlin.example",
        "from_timestamp": "20250101000000",
        "to_timestamp": "20250131235959",
    }


def test_pages_resume_dedup_and_replay(tmp_path):
    fixtures = [capture(2), capture(3)]
    calls = []

    def fetch(**kw):
        calls.append(kw)
        if "showNumPages" in kw["params"]:
            return {"content": '{"pages":2}'}
        if "page" in kw["params"]:
            return {"content": json.dumps(fixtures[kw["params"]["page"]][0])}
        entry, data = next(
            pair
            for pair in fixtures
            if kw["headers"]["Range"].startswith("bytes=" + pair[0]["offset"] + "-")
        )
        return {
            "status": 206,
            "content": data,
            "headers": {
                "Content-Range": f"bytes {entry['offset']}-{int(entry['offset']) + len(data) - 1}/100000"
            },
        }

    path = str(tmp_path / "crawl.duckdb")
    conn = duckdb.connect(path)
    collection = CommonCrawlCollection(conn, "berlin", transport=fetch, **options())
    assert len(collection.step()["completed"]) == 1
    conn.close()
    conn = duckdb.connect(path)
    try:
        collection = CommonCrawlCollection(conn, "berlin", transport=fetch, **options())
        assert len(collection.step()["completed"]) == 2
        assert collection.step()["status"] == "complete"
        count = len(calls)
        collection.step()
        assert len(calls) == count == 5
        assert conn.execute("SELECT count(*) FROM source_binary_blobs").fetchone() == (
            1,
        )
        assert conn.execute(
            "SELECT count(*) FROM source_binary_observations"
        ).fetchone() == (2,)
        with pytest.raises(ValueError, match="configuration changed"):
            CommonCrawlCollection(
                conn, "berlin", transport=fetch, **{**options(), "max_pages": 1}
            )
    finally:
        conn.close()


@pytest.mark.parametrize("problem", ["wrong-range", "corrupt", "digest", "large"])
def test_invalid_captures_do_not_commit(problem):
    entry, data = capture(2)
    if problem == "digest":
        entry["digest"] = "A" * 32
    if problem == "large":
        entry["length"] = "9999999"

    def fetch(**kw):
        if "showNumPages" in kw["params"]:
            return {"content": '{"pages":1}'}
        if "page" in kw["params"]:
            return {"content": json.dumps(entry)}
        return {
            "status": 200 if problem == "wrong-range" else 206,
            "content": b"x" * len(data) if problem == "corrupt" else data,
            "headers": {
                "Content-Range": f"bytes {entry['offset']}-{int(entry['offset']) + len(data) - 1}/100000"
            },
        }

    conn = duckdb.connect()
    try:
        collection = CommonCrawlCollection(
            conn, "invalid", transport=fetch, **options()
        )
        with pytest.raises((ValueError, OSError)):
            collection.step()
        assert not collection.inspect()["completed"]
    finally:
        conn.close()


def test_absent_captures_and_reserved_budget():
    conn = duckdb.connect()
    try:
        collection = CommonCrawlCollection(
            conn, "empty", transport=lambda **_: {"content": '{"pages":0}'}, **options()
        )
        assert collection.step()["status"] == "complete"

        def unavailable(**_):
            raise TimeoutError("offline")

        collection = CommonCrawlCollection(
            conn, "limited", max_requests=1, transport=unavailable, **options()
        )
        with pytest.raises(TimeoutError):
            collection.step()
        with pytest.raises(ValueError, match="budget exhausted"):
            collection.step()
        assert collection.inspect()["requests_reserved"] == 1
    finally:
        conn.close()


def test_identical_payload_across_crawls_preserves_observations():
    conn = duckdb.connect()
    try:
        for crawl, day in [("CC-MAIN-2025-05", 2), ("CC-MAIN-2025-08", 3)]:
            entry, data = capture(day)
            entry["filename"] = entry["filename"].replace("CC-MAIN-2025-05", crawl)

            def fetch(entry=entry, data=data, **kw):
                if "showNumPages" in kw["params"]:
                    return {"content": '{"pages":1}'}
                if "page" in kw["params"]:
                    return {"content": json.dumps(entry)}
                return {
                    "status": 206,
                    "content": data,
                    "headers": {
                        "Content-Range": f"bytes {entry['offset']}-{int(entry['offset']) + len(data) - 1}/100000"
                    },
                }

            result = CommonCrawlCollection(
                conn, crawl, transport=fetch, **{**options(), "crawl": crawl}
            ).step()
            assert result["completed"][0]["crawl"] == crawl
        assert conn.execute("SELECT count(*) FROM source_binary_blobs").fetchone() == (
            1,
        )
        assert conn.execute(
            "SELECT count(*) FROM source_binary_observations"
        ).fetchone() == (2,)
    finally:
        conn.close()
