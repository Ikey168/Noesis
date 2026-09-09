"""Optional bounded WARC/ARC exchange over existing binary snapshot identities."""

import base64
import gzip
import hashlib
import io
import json
from datetime import datetime
from urllib.parse import urlsplit

from src.ingestion.snapshots import SnapshotStore


class ArchiveLimitError(ValueError):
    pass


class _BoundedReader:
    def __init__(self, stream, limit):
        self.stream, self.remaining = stream, limit
        self.hasher = hashlib.sha256()

    def read(self, size=-1):
        size = min(size if size >= 0 else self.remaining + 1, self.remaining + 1)
        data = self.stream.read(size)
        self.remaining -= len(data)
        if self.remaining < 0:
            raise ArchiveLimitError("archive exceeds byte budget")
        self.hasher.update(data)
        return data


_DDL = """CREATE TABLE IF NOT EXISTS warc_observations (
    record_id TEXT PRIMARY KEY, record_hash TEXT NOT NULL, digest TEXT NOT NULL,
    metadata TEXT NOT NULL)
"""


def import_archive(
    conn,
    stream,
    *,
    archive_id,
    max_records=100,
    max_record_bytes=2_000_000,
    max_archive_bytes=20_000_000,
):
    """Read plain or gzip WARC/ARC, validate fully, then atomically store captures.

    HTTP entity bytes remain encoded exactly as captured, with original headers.
    Gzip archive compression is decoded under the aggregate byte budget. Revisit
    records require an earlier imported WARC-Refers-To; missing bases fail.
    """
    from warcio.archiveiterator import ArchiveIterator

    if (
        not isinstance(archive_id, str)
        or not archive_id.strip()
        or not 1 <= max_records <= 10000
        or not 1 <= max_record_bytes <= 20_000_000
        or not 1 <= max_archive_bytes <= 100_000_000
    ):
        raise ValueError("invalid archive bounds")
    # Bounded spooling detects gzip without requiring a seekable caller stream.
    raw = _BoundedReader(stream, max_archive_bytes)
    prefix = raw.read(2)

    class Prefixed:
        def read(self, size=-1):
            nonlocal prefix
            if size == 0:
                return b""
            first, prefix = (
                prefix[:size] if size >= 0 else prefix,
                prefix[size:] if size >= 0 else b"",
            )
            return first + raw.read(size - len(first) if size >= 0 else -1)

    framed = Prefixed()
    decoded = gzip.GzipFile(fileobj=framed) if prefix == b"\x1f\x8b" else framed
    bounded = _BoundedReader(decoded, max_archive_bytes)
    captures, total = [], 0
    iterator = ArchiveIterator(bounded, arc2warc=False, check_digests="raise")
    for index, record in enumerate(iterator):
        if index >= max_records:
            raise ArchiveLimitError("archive exceeds record budget")
        if (
            record.length is None
            or record.length < 0
            or record.length + record.rec_headers.total_len > max_record_bytes
        ):
            raise ArchiveLimitError("archive record exceeds byte budget")
        payload = record.raw_stream.read(max_record_bytes + 1)
        if len(payload) > max_record_bytes:
            raise ArchiveLimitError("archive payload exceeds byte budget")
        # warcio's length-limited stream does not itself reject premature EOF.
        if getattr(record.raw_stream, "limit", 0) > 0:
            raise ValueError("truncated archive record")
        total += len(payload)
        if total > max_archive_bytes:
            raise ArchiveLimitError("archive payload total exceeds byte budget")
        if record.rec_type not in {"response", "revisit"}:
            continue
        headers = record.rec_headers
        if record.format == "arc":
            url = headers.get_header("uri")
            stamp = datetime.strptime(
                headers.get_header("archive-date") + "+0000", "%Y%m%d%H%M%S%z"
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            url, stamp = (
                headers.get_header("WARC-Target-URI"),
                headers.get_header("WARC-Date"),
            )
        parsed = urlsplit(url or "")
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("archive response has invalid source URL")
        captured = datetime.fromisoformat(stamp or "")
        if captured.tzinfo is None:
            raise ValueError("archive capture date lacks timezone")
        if record.http_headers is None:
            raise ValueError("archive response lacks HTTP headers")
        record_id = headers.get_header("WARC-Record-ID")
        # Converted ARC IDs may be generated; derive identity from capture data.
        if record.format == "arc" or not record_id:
            record_id = (
                "arc:" + hashlib.sha256((url + stamp).encode() + payload).hexdigest()
            )
        metadata = {
            "record_id": record_id,
            "url": url,
            "captured_at": stamp,
            "captured_at_ms": int(captured.timestamp() * 1000),
            "record_type": record.rec_type,
            "refers_to": headers.get_header("WARC-Refers-To"),
            "http_status": record.http_headers.statusline,
            "http_protocol": record.http_headers.protocol,
            "http_headers": record.http_headers.headers,
            "warc_headers": headers.headers,
        }
        captures.append((metadata, payload))
    # ArchiveIterator treats EOFError as end of iteration. Force gzip trailer
    # validation outside that handler so truncated compressed input cannot pass.
    while bounded.read(16384):
        pass
    conn.execute(_DDL)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS warc_import_receipts "
        "(archive_id TEXT PRIMARY KEY, archive_hash TEXT, receipt TEXT)"
    )
    archive_hash = raw.hasher.hexdigest()
    prior = conn.execute(
        "SELECT archive_hash, receipt FROM warc_import_receipts WHERE archive_id=?",
        [archive_id],
    ).fetchone()
    if prior:
        if prior[0] != archive_hash:
            raise ValueError("archive ID already used with different bytes")
        return json.loads(prior[1])
    snapshots = SnapshotStore(conn)
    results = []
    conn.execute("BEGIN TRANSACTION")
    try:
        for metadata, payload in captures:
            if metadata["record_type"] == "revisit":
                if payload:
                    raise ValueError("revisit unexpectedly contains a payload")
                base = conn.execute(
                    "SELECT digest FROM warc_observations WHERE record_id=?",
                    [metadata["refers_to"]],
                ).fetchone()
                if not base:
                    raise ValueError("revisit base is unavailable")
                payload = bytes(
                    conn.execute(
                        "SELECT payload FROM source_binary_blobs WHERE digest=?",
                        [base[0]],
                    ).fetchone()[0]
                )
                warc = {k.lower(): v for k, v in metadata["warc_headers"]}
                if not warc.get("warc-profile", "").endswith(
                    "/identical-payload-digest"
                ):
                    raise ValueError("unsupported revisit profile")
                expected = warc.get("warc-payload-digest")
                if expected:
                    algorithm, encoded = expected.split(":", 1)
                    if algorithm not in {"sha1", "sha256"}:
                        raise ValueError("unsupported revisit digest")
                    actual = (
                        base64.b32encode(hashlib.new(algorithm, payload).digest())
                        .decode()
                        .rstrip("=")
                    )
                    if actual.casefold() != encoded.rstrip("=").casefold():
                        raise ValueError("revisit digest does not match base")
            digest = hashlib.sha256(payload).hexdigest()
            serial = json.dumps(metadata, sort_keys=True, ensure_ascii=False)
            record_hash = hashlib.sha256((serial + digest).encode()).hexdigest()
            old = conn.execute(
                "SELECT record_hash FROM warc_observations WHERE record_id=?",
                [metadata["record_id"]],
            ).fetchone()
            if old and old[0] != record_hash:
                raise ValueError("archive record ID conflicts with prior capture")
            content_type = next(
                (v for k, v in metadata["http_headers"] if k.lower() == "content-type"),
                "application/octet-stream",
            )
            snapshots.snapshot_bytes(
                metadata["url"],
                payload,
                metadata["captured_at_ms"],
                content_type=content_type,
                final_url=metadata["url"],
            )
            conn.execute(
                "INSERT INTO warc_observations VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
                [metadata["record_id"], record_hash, digest, serial],
            )
            results.append(
                {
                    "record_id": metadata["record_id"],
                    "digest": digest,
                    "url": metadata["url"],
                    "captured_at": metadata["captured_at"],
                }
            )
        receipt = {
            "archive_id": archive_id,
            "archive_sha256": archive_hash,
            "captures": results,
            "payload_bytes": total,
            "adapter_version": "warcio-1.8.1/noesis-v1",
        }
        conn.execute(
            "INSERT INTO warc_import_receipts VALUES (?,?,?)",
            [archive_id, archive_hash, json.dumps(receipt)],
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return receipt


def export_archive(conn, record_ids, *, max_records=100, max_bytes=20_000_000):
    """Return a bounded WARC; selected revisits are materialized as responses."""
    from itertools import islice

    from warcio.statusandheaders import StatusAndHeaders
    from warcio.warcwriter import WARCWriter

    if not 1 <= max_records <= 10000 or not 1 <= max_bytes <= 100_000_000:
        raise ValueError("invalid archive export bounds")
    ids = list(islice(record_ids, max_records + 1))
    if len(ids) > max_records:
        raise ArchiveLimitError("archive export exceeds record budget")
    output = io.BytesIO()

    class Writer:
        def write(self, data):
            if output.tell() + len(data) > max_bytes:
                raise ArchiveLimitError("archive export exceeds byte budget")
            return output.write(data)

        def flush(self):
            output.flush()

    writer = WARCWriter(Writer(), gzip=False, warc_version="1.1")
    for record_id in ids:
        row = conn.execute(
            "SELECT w.metadata, b.payload FROM warc_observations w "
            "JOIN source_binary_blobs b ON w.digest=b.digest WHERE w.record_id=?",
            [record_id],
        ).fetchone()
        if not row:
            raise ValueError("archive capture is unavailable")
        meta, payload = json.loads(row[0]), bytes(row[1])
        http = StatusAndHeaders(
            meta["http_status"], meta["http_headers"], protocol=meta["http_protocol"]
        )
        headers = {"WARC-Date": meta["captured_at"]}
        if meta["record_type"] == "response" and record_id.startswith("<urn:"):
            headers["WARC-Record-ID"] = record_id
        record = writer.create_warc_record(
            meta["url"],
            "response",
            payload=io.BytesIO(payload),
            http_headers=http,
            warc_headers_dict=headers,
        )
        writer.write_record(record)
    return output.getvalue()
