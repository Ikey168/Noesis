"""Shared free-tier quota ledger (FA02): windows, waits, deferral, 429 blocks, metering."""
import json
import urllib.error
from datetime import datetime, timezone

import pytest

from src.ingestion import quota
from src.ingestion.quota import Limit, QuotaDeferred, QuotaLedger, retry_after_seconds


class Clock:
    def __init__(self, t):
        self.t = t
        self.slept = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def ts(text):
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()


def ledger(tmp_path, hosts, clock):
    return QuotaLedger(tmp_path / "q.sqlite", {"hosts": hosts}, clock=clock, sleep=clock.sleep)


def test_window_bounds():
    now = ts("2026-10-08T15:30:12")
    assert Limit("day").bounds(now) == (ts("2026-10-08T00:00:00"), ts("2026-10-09T00:00:00"))
    assert Limit("week").bounds(now) == (ts("2026-10-05T00:00:00"), ts("2026-10-12T00:00:00"))  # Monday
    assert Limit("month").bounds(ts("2026-12-31T23:00:00")) == (ts("2026-12-01T00:00:00"), ts("2027-01-01T00:00:00"))
    assert Limit("300s").bounds(ts("2026-10-08T15:31:00"))[1] - Limit("300s").bounds(ts("2026-10-08T15:31:00"))[0] == 300


def test_daily_limit_defers_with_retry_time(tmp_path):
    clock = Clock(ts("2026-10-08T10:00:00"))
    lg = ledger(tmp_path, {"api.example.org": {"limits": [{"window": "day", "requests": 2}]}}, clock)
    lg.acquire("https://api.example.org/a")
    lg.acquire("https://api.example.org/b")
    with pytest.raises(QuotaDeferred) as err:
        lg.acquire("https://api.example.org/c")
    assert err.value.retry_at == ts("2026-10-09T00:00:00")
    assert err.value.as_dict()["status"] == "deferred"
    clock.t = ts("2026-10-09T00:00:01")
    lg.acquire("https://api.example.org/c")  # new window


def test_short_waits_are_slept(tmp_path):
    clock = Clock(ts("2026-10-08T10:00:00.0".split(".")[0]) + 0.5)
    lg = ledger(tmp_path, {"api.example.org": {"limits": [{"window": "second", "requests": 1}]}}, clock)
    lg.acquire("api.example.org")
    lg.acquire("api.example.org")
    assert clock.slept and clock.slept[0] == pytest.approx(0.5)


def test_byte_limit_and_status(tmp_path):
    clock = Clock(ts("2026-10-08T10:00:00"))
    lg = ledger(tmp_path, {"ops.example.org": {"limits": [{"window": "week", "bytes": 1000}]}}, clock)
    lg.record_bytes("https://ops.example.org/x", 900)
    with pytest.raises(QuotaDeferred):
        lg.acquire("https://ops.example.org/y", nbytes=200)
    status = lg.status("ops.example.org")["hosts"][0]
    assert status["windows"][0]["used_bytes"] == 900 and status["windows"][0]["resets_at"] == "2026-10-12T00:00:00Z"


def test_block_from_retry_after(tmp_path):
    clock = Clock(ts("2026-10-08T10:00:00"))
    lg = ledger(tmp_path, {}, clock)  # even unlisted hosts honour provider blocks
    lg.block("https://api.unlisted.org/x", 120)
    with pytest.raises(QuotaDeferred) as err:
        lg.acquire("https://api.unlisted.org/y")
    assert "provider asked to retry later" in err.value.reason
    assert lg.status("api.unlisted.org")["hosts"][0]["blocked_until"] == "2026-10-08T10:02:00Z"
    assert retry_after_seconds("30") == 30
    assert retry_after_seconds("Wed, 08 Oct 2026 10:01:00 GMT", now=ts("2026-10-08T10:00:00")) == 60


def test_unlisted_hosts_are_not_counted(tmp_path):
    clock = Clock(ts("2026-10-08T10:00:00"))
    lg = ledger(tmp_path, {}, clock)
    for _ in range(50):
        lg.acquire("https://free.example.org/")
    assert lg.status()["hosts"] == []


def test_metered_get_counts_and_blocks_on_429(tmp_path, monkeypatch):
    clock = Clock(ts("2026-10-08T10:00:00"))
    lg = ledger(tmp_path, {"api.example.org": {"limits": [{"window": "day", "requests": 5}]}}, clock)
    monkeypatch.setattr(quota, "_DEFAULT", lg)
    monkeypatch.delenv("NOESIS_QUOTA_DISABLED", raising=False)

    def ok(url, headers):
        return b"{}"

    def limited(url, headers):
        raise urllib.error.HTTPError(url, 429, "Too Many", {"Retry-After": "600"}, None)

    quota.metered_get(ok)("https://api.example.org/a", {})
    assert lg.status("api.example.org")["hosts"][0]["windows"][0]["used_requests"] == 1
    with pytest.raises(urllib.error.HTTPError):
        quota.metered_get(limited)("https://api.example.org/b", {})
    with pytest.raises(QuotaDeferred):
        quota.metered_get(ok)("https://api.example.org/c", {})


def test_repository_config_parses():
    cfg = json.loads(quota.DEFAULT_CONFIG.read_text())
    parsed = quota._parse_limits(cfg)
    assert parsed["www.courtlistener.com"][2] == Limit("day", 125)
    assert parsed["ops.epo.org"][0].bytes == 4_000_000_000


def test_connector_harvest_reraises_deferral(tmp_path, monkeypatch):
    from src.ingestion.connectors.scholarly.sources import ScopusConnector
    clock = Clock(ts("2026-10-08T10:00:00"))
    lg = ledger(tmp_path, {"api.elsevier.com": {"limits": [{"window": "week", "requests": 0}]}}, clock)
    monkeypatch.setattr(quota, "_DEFAULT", lg)
    monkeypatch.delenv("NOESIS_QUOTA_DISABLED", raising=False)
    conn = ScopusConnector(http_get=lambda u, h: b"{}", dns_resolver=lambda h: ["93.184.216.34"], api_key="k")
    with pytest.raises(QuotaDeferred):
        list(conn.harvest({"topic": "x"}))
