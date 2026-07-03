"""M2.1: the lighter data-plane response encoding. encode_payload emits compact
JSON and gzip-compresses large payloads when the client accepts gzip, shrinking
the cold-path transfer while round-tripping exactly."""

import gzip
import json

from src.genui.dataplane import COMPRESS_MIN_BYTES, encode_payload


def _big_body():
    # A payload comfortably over the compression threshold.
    rows = [{"id": f"row-{i}", "text": "grid resilience under peak load " * 4} for i in range(200)]
    return {"server": "pipeline_mcp", "tool": "articles_data", "data": {"count": len(rows), "rows": rows}}


def test_small_payload_ships_raw_even_when_gzip_accepted():
    body = {"server": "s", "tool": "t", "data": {"ok": True}}
    data, encoding = encode_payload(body, "gzip, deflate")
    assert encoding is None
    assert json.loads(data.decode("utf-8")) == body


def test_compact_separators_drop_whitespace():
    data, _ = encode_payload({"a": 1, "b": [1, 2]}, "")
    text = data.decode("utf-8")
    assert ", " not in text and '": ' not in text  # no inter-token spaces


def test_large_payload_gzips_when_accepted_and_round_trips():
    body = _big_body()
    data, encoding = encode_payload(body, "gzip")
    assert encoding == "gzip"
    # Smaller than the raw compact JSON, and decompresses back to the body.
    raw = json.dumps(body, separators=(",", ":"), default=str).encode("utf-8")
    assert len(data) < len(raw)
    assert json.loads(gzip.decompress(data).decode("utf-8")) == body


def test_large_payload_stays_raw_without_gzip_accept():
    body = _big_body()
    data, encoding = encode_payload(body, "identity")
    assert encoding is None
    assert len(data) >= COMPRESS_MIN_BYTES
    assert json.loads(data.decode("utf-8")) == body
