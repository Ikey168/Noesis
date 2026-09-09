"""Bounded, resumable Common Crawl index and WARC range acquisition."""

import base64
import gzip
import hashlib
import io
import json
import re
from datetime import datetime
from urllib.parse import urlsplit

from src.ingestion.source_pack_runtime import HTTPSPageAdapter
from src.ingestion.warc_io import import_archive


class CommonCrawlCollection:
    """Single-writer collection pinned to one crawl, host and capture window.

    Each step stores its index page before fetching captures. Network reservations
    are durable and conservative: a failed/crashed request consumes its ceiling.
    Completed records replay through the existing WARC receipts and binary blobs.
    """

    def __init__(
        self,
        conn,
        collection_id,
        *,
        crawl,
        host,
        from_timestamp,
        to_timestamp,
        max_pages=5,
        max_records=100,
        max_requests=110,
        max_network_bytes=50_000_000,
        max_record_bytes=2_000_000,
        max_index_bytes=2_000_000,
        timeout_s=15,
        transport=None,
    ):
        if (
            not collection_id
            or not re.fullmatch(r"CC-MAIN-20\d{2}-\d{2}", crawl)
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host)
            or "." not in host
            or ".." in host
            or not 1 <= max_pages <= 100
            or not 1 <= max_records <= 10000
            or not 1 <= max_requests <= 10200
            or not 1 <= max_network_bytes <= 1_000_000_000
            or not 1 <= max_record_bytes <= 20_000_000
            or not 1 <= max_index_bytes <= 20_000_000
            or not 0 < timeout_s <= 60
        ):
            raise ValueError("invalid Common Crawl collection bounds")
        for stamp in (from_timestamp, to_timestamp):
            datetime.strptime(stamp + "+0000", "%Y%m%d%H%M%S%z")
        if (
            len(from_timestamp) != 14
            or len(to_timestamp) != 14
            or from_timestamp > to_timestamp
        ):
            raise ValueError("invalid Common Crawl capture window")
        self.conn, self.id = conn, collection_id
        self.transport = transport or HTTPSPageAdapter._request
        self.config = {
            "crawl": crawl,
            "host": host,
            "from": from_timestamp,
            "to": to_timestamp,
            "max_pages": max_pages,
            "max_records": max_records,
            "max_requests": max_requests,
            "max_network_bytes": max_network_bytes,
            "max_record_bytes": max_record_bytes,
            "max_index_bytes": max_index_bytes,
            "timeout_s": timeout_s,
        }
        conn.execute(
            "CREATE TABLE IF NOT EXISTS common_crawl_collections "
            "(collection_id TEXT PRIMARY KEY, config TEXT, state TEXT)"
        )
        old = conn.execute(
            "SELECT config FROM common_crawl_collections WHERE collection_id=?",
            [collection_id],
        ).fetchone()
        serial = json.dumps(self.config, sort_keys=True)
        if old and old[0] != serial:
            raise ValueError("Common Crawl collection configuration changed")
        if not old:
            state = {
                "page": 0,
                "pages": None,
                "pending": [],
                "completed": [],
                "requests_reserved": 0,
                "bytes_reserved": 0,
                "status": "pending",
            }
            conn.execute(
                "INSERT INTO common_crawl_collections VALUES (?,?,?)",
                [collection_id, serial, json.dumps(state)],
            )

    def inspect(self):
        return json.loads(
            self.conn.execute(
                "SELECT state FROM common_crawl_collections WHERE collection_id=?",
                [self.id],
            ).fetchone()[0]
        )

    def _save(self, state):
        self.conn.execute(
            "UPDATE common_crawl_collections SET state=? WHERE collection_id=?",
            [json.dumps(state), self.id],
        )

    def _get(self, state, url, params, *, max_bytes, headers=None):
        if (
            state["requests_reserved"] >= self.config["max_requests"]
            or state["bytes_reserved"] + max_bytes > self.config["max_network_bytes"]
        ):
            raise ValueError("Common Crawl network budget exhausted")
        state["requests_reserved"] += 1
        state["bytes_reserved"] += max_bytes
        self._save(state)
        response = self.transport(
            url=url,
            params=params,
            headers={
                "User-Agent": "Noesis/0.1 (https://github.com/Ikey168/Noesis)",
                **(headers or {}),
            },
            timeout=self.config["timeout_s"],
            max_bytes=max_bytes,
        )
        raw = response.get("content", b"")
        raw = raw.encode() if isinstance(raw, str) else bytes(raw)
        if len(raw) > max_bytes:
            raise ValueError("Common Crawl response exceeds byte budget")
        if (
            response.get("final_url")
            and urlsplit(response["final_url"]).hostname != urlsplit(url).hostname
        ):
            raise ValueError("Common Crawl response redirected outside provider")
        return response, raw

    def _entry(self, item):
        url = urlsplit(item["url"])
        if (
            url.scheme not in {"http", "https"}
            or url.hostname != self.config["host"]
            or url.username
            or url.password
            or not self.config["from"] <= item["timestamp"] <= self.config["to"]
            or not re.fullmatch(r"\d{14}", item["timestamp"])
        ):
            raise ValueError("Common Crawl index result is outside collection scope")
        filename = item["filename"]
        if (
            not filename.startswith("crawl-data/" + self.config["crawl"] + "/")
            or not re.fullmatch(r"[A-Za-z0-9/._-]+\.warc\.gz", filename)
            or ".." in filename.split("/")
        ):
            raise ValueError("invalid Common Crawl WARC path")
        offset, length = int(item["offset"]), int(item["length"])
        if offset < 0 or not 1 <= length <= self.config["max_record_bytes"]:
            raise ValueError("Common Crawl range exceeds record budget")
        if not re.fullmatch(r"[A-Z2-7]{32}", item["digest"]):
            raise ValueError("Common Crawl index lacks a SHA-1 payload digest")
        return offset, length

    def step(self):
        """Advance by one captured record or one empty index page; return state."""
        from warcio.archiveiterator import ArchiveIterator

        state = self.inspect()
        if state["status"] in {"complete", "bounded"}:
            return state
        index_url = "https://index.commoncrawl.org/" + self.config["crawl"] + "-index"
        query = {
            "url": self.config["host"],
            "matchType": "host",
            "from": self.config["from"],
            "to": self.config["to"],
            "output": "json",
            "pageSize": 1,
            "filter": "=status:200",
        }
        if state["pages"] is None:
            response, raw = self._get(
                state, index_url, {**query, "showNumPages": "true"}, max_bytes=100_000
            )
            if int(response.get("status", 200)) != 200:
                raise ValueError("Common Crawl index is unavailable")
            pages = json.loads(raw).get("pages")
            if type(pages) is not int or not 0 <= pages <= 1_000_000:
                raise ValueError("invalid Common Crawl index page count")
            state["pages"] = pages
            self._save(state)
        if not state["pending"]:
            if state["page"] >= min(state["pages"], self.config["max_pages"]):
                state["status"] = (
                    "complete" if state["page"] >= state["pages"] else "bounded"
                )
                self._save(state)
                return state
            response, raw = self._get(
                state,
                index_url,
                {**query, "page": state["page"]},
                max_bytes=self.config["max_index_bytes"],
            )
            if int(response.get("status", 200)) != 200:
                raise ValueError("Common Crawl index page is unavailable")
            entries = [json.loads(line) for line in raw.splitlines() if line.strip()]
            for entry in entries:
                self._entry(entry)
            state["pending"] = entries
            state["page"] += 1
            self._save(state)
            if not entries:
                return state
        if len(state["completed"]) >= self.config["max_records"]:
            state["status"] = "bounded"
            self._save(state)
            return state
        item = state["pending"][0]
        offset, length = self._entry(item)
        response, compressed = self._get(
            state,
            "https://data.commoncrawl.org/" + item["filename"],
            {},
            max_bytes=length,
            headers={"Range": f"bytes={offset}-{offset + length - 1}"},
        )
        headers = {k.lower(): v for k, v in response.get("headers", {}).items()}
        expected_range = f"bytes {offset}-{offset + length - 1}/"
        if (
            int(response.get("status", 200)) != 206
            or len(compressed) != length
            or not headers.get("content-range", "").startswith(expected_range)
        ):
            raise ValueError("Common Crawl server did not honor the requested range")
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
            expanded = stream.read(self.config["max_record_bytes"] + 1)
        if len(expanded) > self.config["max_record_bytes"]:
            raise ValueError("Common Crawl decompressed record exceeds budget")
        records = ArchiveIterator(io.BytesIO(expanded), check_digests="raise")
        record = next(records)
        payload = record.raw_stream.read(self.config["max_record_bytes"] + 1)
        stamp = datetime.fromisoformat(
            record.rec_headers.get_header("WARC-Date")
        ).strftime("%Y%m%d%H%M%S")
        if (
            record.rec_type != "response"
            or record.rec_headers.get_header("WARC-Target-URI") != item["url"]
            or stamp != item["timestamp"]
            or base64.b32encode(hashlib.sha1(payload).digest()).decode()
            != item["digest"]
            or next(records, None) is not None
        ):
            raise ValueError("Common Crawl index and WARC capture disagree")
        archive_id = (
            "common-crawl:"
            + hashlib.sha256(
                json.dumps([self.config["crawl"], item], sort_keys=True).encode()
            ).hexdigest()
        )
        receipt = import_archive(
            self.conn,
            io.BytesIO(compressed),
            archive_id=archive_id,
            max_records=1,
            max_record_bytes=self.config["max_record_bytes"],
            max_archive_bytes=self.config["max_record_bytes"],
        )
        state["completed"].append(
            {"crawl": self.config["crawl"], "index_entry": item, "archive": receipt}
        )
        state["pending"].pop(0)
        state["status"] = "running"
        self._save(state)
        return state
