import json

import duckdb
import pytest

from src.ingestion.wayback import acquire_wayback

URL = "https://berlin.example/removed-report"


def transport_for(stamp="20240102030405", *, available=True, status="200", fail=False):
    calls = []

    def fetch(**kw):
        calls.append(kw["url"])
        if kw["params"]:
            return {
                "content": json.dumps(
                    {
                        "archived_snapshots": {
                            "closest": {
                                "available": True,
                                "url": f"http://web.archive.org/web/{stamp}/{URL}",
                                "timestamp": stamp,
                                "status": status,
                            }
                        }
                        if available
                        else {}
                    }
                )
            }
        if fail:
            return {"status": 503}
        return {
            "content": "<article><h1>Berliner Bericht</h1><p>"
            + (
                "Öffentliche Forschung in Berlin und ihre dokumentierten Ergebnisse. "
                * 8
            )
            + "</p></article>"
        }

    return fetch, calls


def test_multiple_captures_restart_replay_and_dates(tmp_path):
    path = str(tmp_path / "history.duckdb")
    conn = duckdb.connect(path)
    fetch, calls = transport_for()
    options = {
        "timestamp": "2024",
        "request_id": "first",
        "language": "de",
        "transport": fetch,
    }
    first = acquire_wayback(conn, URL, **options)
    assert first["status"] == "acquired"
    assert first["captured_at_ms"] < first["retrieved_at_ms"]
    assert conn.execute("SELECT created_at FROM documents").fetchone() == (None,)
    conn.close()
    conn = duckdb.connect(path)
    try:
        assert acquire_wayback(conn, URL, **options) == first
        assert len(calls) == 2
        fetch2, _ = transport_for("20250102030405")
        second = acquire_wayback(
            conn,
            URL,
            timestamp="2025",
            request_id="second",
            language="de",
            transport=fetch2,
        )
        assert (
            second["status"] == "acquired"
            and second["document_id"] != first["document_id"]
        )
        assert first["snapshot_digest"] == second["snapshot_digest"]
        assert conn.execute("SELECT count(*) FROM source_binary_blobs").fetchone() == (
            1,
        )
        assert conn.execute("SELECT count(*) FROM documents").fetchone() == (2,)
        with pytest.raises(ValueError, match="different inputs"):
            acquire_wayback(conn, URL + "other", **options)
    finally:
        conn.close()


@pytest.mark.parametrize(
    "controls,expected,requests",
    [
        ({"available": False}, "unavailable", 1),
        ({"status": "302"}, "redirected_capture", 1),
        ({"fail": True}, "failed", 2),
    ],
)
def test_unavailable_redirected_and_failed(controls, expected, requests):
    conn = duckdb.connect()
    fetch, calls = transport_for(**controls)
    result = acquire_wayback(
        conn, URL, timestamp="2024", request_id="test", language="de", transport=fetch
    )
    assert result["status"] == expected and len(calls) == requests
    conn.close()
