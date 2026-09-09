import gzip
import io
import json

import duckdb
import pytest

pytest.importorskip("warcio")
from warcio.exceptions import ArchiveLoadFailed
from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import WARCWriter

from src.ingestion.warc_io import ArchiveLimitError, export_archive, import_archive

PAYLOAD = "<article>Berliner Forschung: öffentliche Förderung.</article>".encode()
IDENTITY = "<urn:uuid:12345678-1234-1234-1234-123456789abc>"


def archive(*, payload=PAYLOAD, revisit=False):
    out = io.BytesIO()
    writer = WARCWriter(out, gzip=False, warc_version="1.1")
    headers = {"WARC-Date": "2025-01-02T03:04:05Z", "WARC-Record-ID": IDENTITY}
    response = writer.create_warc_record(
        "https://berlin.example/report",
        "response",
        payload=io.BytesIO(payload),
        warc_headers_dict=headers,
        http_headers=StatusAndHeaders(
            "200 OK",
            [
                ("Content-Type", "text/html; charset=utf-8"),
                ("X-Test", "one"),
                ("X-Test", "two"),
            ],
            protocol="HTTP/1.1",
        ),
    )
    writer.write_record(response)
    if revisit:
        second = writer.create_warc_record(
            "https://berlin.example/report",
            "revisit",
            payload=io.BytesIO(b""),
            http_headers=StatusAndHeaders(
                "200 OK", [("Content-Type", "text/html")], protocol="HTTP/1.1"
            ),
            warc_headers_dict={
                "WARC-Date": "2025-02-02T03:04:05Z",
                "WARC-Refers-To": IDENTITY,
                "WARC-Profile": "http://netpreserve.org/warc/1.1/revisit/identical-payload-digest",
            },
        )
        writer.write_record(second)
    return out.getvalue()


@pytest.mark.parametrize("compressed", [False, True])
def test_roundtrip_replay_and_headers(compressed):
    conn = duckdb.connect()
    data = archive()
    if compressed:
        data = gzip.compress(data)
    first = import_archive(conn, io.BytesIO(data), archive_id="fixture")
    assert import_archive(conn, io.BytesIO(data), archive_id="fixture") == first
    assert (
        conn.execute("SELECT payload FROM source_binary_blobs").fetchone()[0] == PAYLOAD
    )
    encoded = export_archive(conn, [IDENTITY])
    other = duckdb.connect()
    second = import_archive(other, io.BytesIO(encoded), archive_id="roundtrip")
    assert first["captures"] == second["captures"]
    m1 = json.loads(
        conn.execute("SELECT metadata FROM warc_observations").fetchone()[0]
    )
    m2 = json.loads(
        other.execute("SELECT metadata FROM warc_observations").fetchone()[0]
    )
    assert m1["http_headers"] == m2["http_headers"]
    conn.close()
    other.close()


def test_revisit_shares_blob_and_preserves_observation():
    conn = duckdb.connect()
    result = import_archive(
        conn, io.BytesIO(archive(revisit=True)), archive_id="revisits"
    )
    assert len(result["captures"]) == 2
    assert len({r["digest"] for r in result["captures"]}) == 1
    assert conn.execute("SELECT count(*) FROM source_binary_blobs").fetchone() == (1,)
    assert conn.execute(
        "SELECT count(*) FROM source_binary_observations"
    ).fetchone() == (2,)
    conn.close()


@pytest.mark.parametrize(
    "data", [b"not a WARC", archive()[:-30], gzip.compress(archive())[:-10]]
)
def test_corrupt_and_truncated_archives(data):
    conn = duckdb.connect()
    with pytest.raises((ArchiveLoadFailed, ValueError, EOFError, OSError)):
        import_archive(conn, io.BytesIO(data), archive_id="bad")
    conn.close()


def test_limits_and_record_identity_conflicts():
    conn = duckdb.connect()
    with pytest.raises(ArchiveLimitError):
        import_archive(
            conn, io.BytesIO(archive()), archive_id="large", max_record_bytes=20
        )
    with pytest.raises(ArchiveLimitError):
        import_archive(
            conn,
            io.BytesIO(gzip.compress(archive(payload=b"x" * 10000))),
            archive_id="bomb",
            max_archive_bytes=2000,
        )
    import_archive(conn, io.BytesIO(archive()), archive_id="good")
    with pytest.raises(ValueError, match="conflicts"):
        import_archive(
            conn, io.BytesIO(archive(payload=b"changed")), archive_id="conflict"
        )
    assert (
        conn.execute("SELECT payload FROM source_binary_blobs").fetchone()[0] == PAYLOAD
    )
    with pytest.raises(ArchiveLimitError):
        export_archive(conn, [IDENTITY], max_bytes=20)
    conn.close()


def test_arc_import_has_stable_identity_on_replay():
    body = b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n" + PAYLOAD
    data = (
        f"https://berlin.example/report 8.8.8.8 20250102030405 text/html {len(body)}\n".encode()
        + body
        + b"\n"
    )
    conn = duckdb.connect()
    first = import_archive(conn, io.BytesIO(data), archive_id="arc")
    assert import_archive(conn, io.BytesIO(data), archive_id="arc") == first
    assert conn.execute("SELECT count(*) FROM warc_observations").fetchone() == (1,)
    assert first["captures"][0]["record_id"].startswith("arc:")
    conn.close()


def test_missing_revisit_base_rolls_back_prior_captures():
    data = archive(revisit=True).replace(
        ("WARC-Refers-To: " + IDENTITY).encode(),
        b"WARC-Refers-To: <urn:uuid:00000000-0000-0000-0000-000000000000>",
    )
    conn = duckdb.connect()
    try:
        with pytest.raises(ValueError, match="base is unavailable"):
            import_archive(conn, io.BytesIO(data), archive_id="missing-base")
        assert conn.execute("SELECT count(*) FROM warc_observations").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM warc_import_receipts").fetchone() == (
            0,
        )
    finally:
        conn.close()


def test_archive_record_and_aggregate_bounds():
    conn = duckdb.connect()
    try:
        with pytest.raises(ArchiveLimitError, match="record budget"):
            import_archive(
                conn,
                io.BytesIO(archive(revisit=True)),
                archive_id="too-many",
                max_records=1,
            )
        with pytest.raises(ArchiveLimitError, match="byte budget"):
            import_archive(
                conn,
                io.BytesIO(archive()),
                archive_id="too-large",
                max_archive_bytes=100,
            )
    finally:
        conn.close()
