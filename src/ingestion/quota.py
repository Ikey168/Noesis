"""Per-source free-tier quotas shared across processes (FA02).

Many free tiers are capped per time window (OpenAlex per day, EPO OPS bytes per
week, Semantic Scholar per second, UN Comtrade per day …). Per-run budgets in
source packs cannot see what earlier runs or other processes already spent, so
this ledger keeps the usage per **API host** in a small SQLite file shared by
every caller in the same environment (connectors, source packs, the abstract
backfill, bulk downloads).

Limits are declared in ``config/source_quotas.json``::

    {"hosts": {"api.openalex.org": {"limits": [{"window": "day", "requests": 1000}]},
               "ops.epo.org": {"limits": [{"window": "week", "bytes": 4000000000}]},
               "api.company-information.service.gov.uk":
                   {"limits": [{"seconds": 300, "requests": 600}]}}}

``window`` is ``second|minute|hour|day|week|month`` (UTC calendar windows; weeks
start on Monday) or ``seconds`` gives a fixed window of that length. A request
that would exceed a limit waits when the window ends within ``max_wait_s`` and
otherwise raises :class:`QuotaDeferred` with the time it may be retried. An
HTTP 429 ``Retry-After`` blocks the host until then (:meth:`QuotaLedger.block`).

Ledger path: ``NOESIS_QUOTA_DB`` or ``~/.local/state/noesis/quota.sqlite``;
config path: ``NOESIS_QUOTA_CONFIG`` or the repository default. Hosts without a
declared limit are not counted.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

CONTRACT = "noesis-source-quota-v1"
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "config" / "source_quotas.json"
WINDOWS = ("second", "minute", "hour", "day", "week", "month")
MAX_WAIT_S = 5.0


class QuotaDeferred(Exception):
    """A request would exceed a declared free-tier limit; retry at ``retry_at`` (epoch seconds)."""

    def __init__(self, host: str, reason: str, retry_at: float):
        self.host, self.reason, self.retry_at = host, reason, retry_at
        when = datetime.fromtimestamp(retry_at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        super().__init__(f"quota for {host} exhausted ({reason}); retry after {when}")

    def as_dict(self) -> Dict[str, Any]:
        return {"status": "deferred", "host": self.host, "reason": self.reason,
                "retry_at": datetime.fromtimestamp(self.retry_at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


@dataclass(frozen=True)
class Limit:
    window: str          # named window or "<n>s"
    requests: Optional[int] = None
    bytes: Optional[int] = None

    def bounds(self, now: float) -> tuple[float, float]:
        if self.window.endswith("s") and self.window[:-1].isdigit():
            length = int(self.window[:-1])
            start = now - (now % length)
            return start, start + length
        moment = datetime.fromtimestamp(now, timezone.utc)
        if self.window == "second":
            start = moment.replace(microsecond=0); end = start + timedelta(seconds=1)
        elif self.window == "minute":
            start = moment.replace(second=0, microsecond=0); end = start + timedelta(minutes=1)
        elif self.window == "hour":
            start = moment.replace(minute=0, second=0, microsecond=0); end = start + timedelta(hours=1)
        elif self.window == "day":
            start = moment.replace(hour=0, minute=0, second=0, microsecond=0); end = start + timedelta(days=1)
        elif self.window == "week":
            day = moment.replace(hour=0, minute=0, second=0, microsecond=0)
            start = day - timedelta(days=day.weekday()); end = start + timedelta(days=7)
        else:  # month
            start = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            end = (start.replace(year=start.year + 1, month=1) if start.month == 12
                   else start.replace(month=start.month + 1))
        return start.timestamp(), end.timestamp()


def _parse_limits(raw: Mapping[str, Any]) -> Dict[str, List[Limit]]:
    hosts: Dict[str, List[Limit]] = {}
    for host, spec in (raw.get("hosts") or {}).items():
        limits = []
        for item in spec.get("limits") or []:
            if "seconds" in item:
                window = f"{int(item['seconds'])}s"
            elif item.get("window") in WINDOWS:
                window = item["window"]
            else:
                raise ValueError(f"{host}: limit needs window in {WINDOWS} or seconds")
            if item.get("requests") is None and item.get("bytes") is None:
                raise ValueError(f"{host}: limit needs requests or bytes")
            limits.append(Limit(window, item.get("requests"), item.get("bytes")))
        hosts[host.lower()] = limits
    return hosts


def retry_after_seconds(value: Optional[str], now: Optional[float] = None) -> Optional[float]:
    """Seconds to wait from a ``Retry-After`` header (delta seconds or HTTP date)."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        return max(0.0, parsedate_to_datetime(value).timestamp() - (now or time.time()))
    except (TypeError, ValueError):
        return None


class QuotaLedger:
    """Shared usage ledger. ``clock``/``sleep`` are injectable for tests."""

    def __init__(self, path: Optional[os.PathLike] = None, config: Optional[Mapping[str, Any]] = None,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 max_wait_s: float = MAX_WAIT_S):
        self.path = Path(path or os.getenv("NOESIS_QUOTA_DB")
                         or Path.home() / ".local" / "state" / "noesis" / "quota.sqlite")
        if config is None:
            config_path = Path(os.getenv("NOESIS_QUOTA_CONFIG") or DEFAULT_CONFIG)
            config = json.loads(config_path.read_text()) if config_path.exists() else {}
        self.limits = _parse_limits(config)
        self.clock, self.sleep, self.max_wait_s = clock, sleep, max_wait_s
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS usage(host TEXT, window TEXT, start REAL, "
                       "requests INTEGER, bytes INTEGER, PRIMARY KEY(host, window, start))")
            db.execute("CREATE TABLE IF NOT EXISTS blocks(host TEXT PRIMARY KEY, until REAL, reason TEXT)")

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.execute("PRAGMA busy_timeout=30000")
        return db

    @staticmethod
    def host_of(url_or_host: str) -> str:
        parsed = urllib.parse.urlparse(url_or_host if "://" in url_or_host else "https://" + url_or_host)
        return (parsed.hostname or "").lower()

    def acquire(self, url_or_host: str, requests: int = 1, nbytes: int = 0) -> None:
        """Reserve ``requests`` (and ``nbytes``) for the host, waiting briefly or raising QuotaDeferred."""
        host = self.host_of(url_or_host)
        limits = self.limits.get(host)
        while True:
            now = self.clock()
            wait_until = None
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                try:
                    block = db.execute("SELECT until, reason FROM blocks WHERE host=?", [host]).fetchone()
                    if block and block[0] > now:
                        wait_until, reason = block[0], f"provider asked to retry later ({block[1]})"
                    elif limits:
                        for limit in limits:
                            start, end = limit.bounds(now)
                            row = db.execute("SELECT requests, bytes FROM usage WHERE host=? AND window=? AND start=?",
                                             [host, limit.window, start]).fetchone() or (0, 0)
                            if limit.requests is not None and row[0] + requests > limit.requests:
                                wait_until, reason = end, f"{limit.requests} requests per {limit.window}"
                                break
                            if limit.bytes is not None and nbytes and row[1] + nbytes > limit.bytes:
                                wait_until, reason = end, f"{limit.bytes} bytes per {limit.window}"
                                break
                    if wait_until is None:
                        for limit in limits or []:
                            start, _ = limit.bounds(now)
                            db.execute("INSERT INTO usage VALUES(?,?,?,?,?) ON CONFLICT(host, window, start) "
                                       "DO UPDATE SET requests=requests+excluded.requests, bytes=bytes+excluded.bytes",
                                       [host, limit.window, start, requests, nbytes])
                    db.execute("COMMIT")
                except BaseException:
                    db.execute("ROLLBACK")
                    raise
            if wait_until is None:
                return
            if wait_until - now <= self.max_wait_s:
                self.sleep(max(0.0, wait_until - now))
                continue
            raise QuotaDeferred(host, reason, wait_until)

    def record_bytes(self, url_or_host: str, nbytes: int) -> None:
        """Add transferred bytes after a response (for byte-limited hosts)."""
        host = self.host_of(url_or_host)
        if not nbytes or not self.limits.get(host):
            return
        now = self.clock()
        with self._db() as db:
            for limit in self.limits[host]:
                if limit.bytes is None:
                    continue
                start, _ = limit.bounds(now)
                db.execute("INSERT INTO usage VALUES(?,?,?,0,?) ON CONFLICT(host, window, start) "
                           "DO UPDATE SET bytes=bytes+excluded.bytes", [host, limit.window, start, nbytes])

    def block(self, url_or_host: str, seconds: float, reason: str = "HTTP 429") -> None:
        """Block the host for ``seconds`` (e.g. from a 429 ``Retry-After``)."""
        host = self.host_of(url_or_host)
        until = self.clock() + max(0.0, seconds)
        with self._db() as db:
            db.execute("INSERT INTO blocks VALUES(?,?,?) ON CONFLICT(host) DO UPDATE SET "
                       "until=max(until, excluded.until), reason=excluded.reason", [host, until, reason])

    def status(self, host: Optional[str] = None) -> Dict[str, Any]:
        """Configured limits, current-window usage and blocks, per host."""
        now = self.clock()
        hosts = [self.host_of(host)] if host else sorted(self.limits)
        out = []
        with self._db() as db:
            for h in hosts:
                windows = []
                for limit in self.limits.get(h, []):
                    start, end = limit.bounds(now)
                    row = db.execute("SELECT requests, bytes FROM usage WHERE host=? AND window=? AND start=?",
                                     [h, limit.window, start]).fetchone() or (0, 0)
                    windows.append({"window": limit.window, "limit_requests": limit.requests,
                                    "limit_bytes": limit.bytes, "used_requests": row[0], "used_bytes": row[1],
                                    "resets_at": _iso(end)})
                block = db.execute("SELECT until, reason FROM blocks WHERE host=?", [h]).fetchone()
                out.append({"host": h, "windows": windows,
                            "blocked_until": _iso(block[0]) if block and block[0] > now else None,
                            "blocked_reason": block[1] if block and block[0] > now else None})
        return {"contract": CONTRACT, "ledger": str(self.path), "hosts": out}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_DEFAULT: Optional[QuotaLedger] = None


def default_ledger() -> Optional[QuotaLedger]:
    """Process-wide ledger, or ``None`` when disabled with ``NOESIS_QUOTA_DISABLED=1``."""
    global _DEFAULT
    if os.getenv("NOESIS_QUOTA_DISABLED") == "1":
        return None
    if _DEFAULT is None:
        _DEFAULT = QuotaLedger()
    return _DEFAULT


def acquire(url: str, requests: int = 1, nbytes: int = 0) -> None:
    ledger = default_ledger()
    if ledger is not None:
        ledger.acquire(url, requests, nbytes)


def note_response(url: str, status: int, headers: Optional[Mapping[str, str]] = None, nbytes: int = 0) -> None:
    """Record transferred bytes, and block the host on 429/503 with ``Retry-After``."""
    ledger = default_ledger()
    if ledger is None:
        return
    if nbytes:
        ledger.record_bytes(url, nbytes)
    if status in (429, 503):
        lower = {k.lower(): v for k, v in (headers or {}).items()}
        wait = retry_after_seconds(lower.get("retry-after"))
        ledger.block(url, wait if wait is not None else 60.0, f"HTTP {status}")


def metered_get(get: Callable[[str, Mapping[str, str]], bytes]) -> Callable[[str, Mapping[str, str]], bytes]:
    """Wrap an ``http_get(url, headers)`` so every call is acquired and its response noted."""
    if getattr(get, "_noesis_metered", False):
        return get

    def wrapped(url: str, headers: Mapping[str, str]) -> bytes:
        import urllib.error
        acquire(url)
        try:
            payload = get(url, headers)
        except urllib.error.HTTPError as exc:
            note_response(url, exc.code, dict(exc.headers or {}))
            raise
        note_response(url, 200, None, len(payload or b""))
        return payload

    wrapped._noesis_metered = True  # type: ignore[attr-defined]
    return wrapped


def metered_post(post: Callable[[str, Mapping[str, str], bytes], bytes]) -> Callable[[str, Mapping[str, str], bytes], bytes]:
    """Same as :func:`metered_get` for ``http_post(url, headers, body)``."""
    if getattr(post, "_noesis_metered", False):
        return post

    def wrapped(url: str, headers: Mapping[str, str], body: bytes) -> bytes:
        import urllib.error
        acquire(url)
        try:
            payload = post(url, headers, body)
        except urllib.error.HTTPError as exc:
            note_response(url, exc.code, dict(exc.headers or {}))
            raise
        note_response(url, 200, None, len(payload or b""))
        return payload

    wrapped._noesis_metered = True  # type: ignore[attr-defined]
    return wrapped
