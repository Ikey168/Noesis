"""Memento TimeGate/TimeMap resolution across archives (#2250 WA03, #2261 WA05, #2275 WA06)."""

from __future__ import annotations

import copy

import pytest

from src.ingestion.memento import (
    ARCHIVES,
    BOUNDED_COVERAGE,
    LIVE_VERIFICATION,
    SAVE_PAGE_NOW,
    MementoError,
    archive_for,
    parse_link_format,
    parse_timemap,
)
from src.kb.citation_preservation import READ_SCOPE
from tests.unit import web_archive_harness as h


def test_archive_configuration_matches_the_wa01_audit():
    audit = (h.ROOT / "docs/roadmaps/platform-web-archives-source-audit.md").read_text()
    for archive_id, spec in ARCHIVES.items():
        assert f"`{archive_id}`" in audit, archive_id
        assert spec["access_decision"] in {"in_scope", "deferred", "excluded"}
        for field in ("kind", "formats", "digest", "rate_limit", "terms"):
            assert field in spec, (archive_id, field)
    assert ARCHIVES["archive-today"]["access_decision"] == "excluded"
    assert {ARCHIVES[a]["access_decision"] for a in ("library-of-congress", "vefsafn-is")} == {"deferred"}
    assert ARCHIVES["uk-web-archive"]["access_decision"] == "in_scope"
    assert ARCHIVES["common-crawl"]["kind"] == "crawl-corpus"
    assert SAVE_PAGE_NOW["operation"] == "write" and SAVE_PAGE_NOW["scope"] == "knowledge:citation:archive-request"
    assert SAVE_PAGE_NOW["feature_default"] is False
    assert BOUNDED_COVERAGE["max_mementos_per_timemap"] == 5000
    assert all(v["status"] == "unverified-live" for v in LIVE_VERIFICATION.values())
    for url in BOUNDED_COVERAGE["verification_urls"]:
        assert url in audit


def test_link_format_and_json_timemaps_parse_per_rfc_7089():
    links = parse_link_format(h.text("ia_timemap.link"))
    assert {"original"} == links[0]["rel"] and links[3]["datetime"] == "Fri, 15 Dec 2023 12:00:00 GMT"
    link_map = parse_timemap(h.text("ia_timemap.link"), "application/link-format")
    assert link_map["original"] == h.URL and len(link_map["mementos"]) == 3
    json_map = parse_timemap(h.text("timetravel_timemap.json"), "application/json")
    assert len(json_map["mementos"]) == 6 and json_map["timegate"].startswith("https://timetravel")
    assert archive_for(json_map["mementos"][0]["uri_m"]) == "internet-archive"
    assert archive_for("https://archive.ph/2024/x") == "archive-today"
    assert archive_for("https://wayback.archive-it.org/1/2024/x") == "unaudited:wayback.archive-it.org"


def test_aggregator_resolution_records_the_holding_archive_and_reports_every_archive():
    conn = h.connect()
    transport = h.FakeArchives()
    client = h.client(conn, transport)
    result = client.resolve(h.URL, request_id="r1")
    archives = result["archives"]
    assert archives["internet-archive"]["outcome"] == "captures"
    assert len(archives["internet-archive"]["capture_ids"]) == 3
    assert archives["uk-web-archive"]["outcome"] == "captures"
    # Not listed by the aggregator is not the same as no capture on record.
    assert archives["arquivo-pt"]["outcome"] == "not_reported_by_aggregator"
    assert archives["archive-today"]["outcome"] == "excluded_by_access_decision"
    assert archives["archive-today"]["withheld_mementos"] == 1
    assert archives["library-of-congress"]["outcome"] == "deferred_by_access_decision"
    assert archives["bnf"]["outcome"] == "excluded_by_access_decision"
    assert archives["common-crawl"] == {"outcome": "not_queried", "archive_kind": "crawl-corpus"}
    assert result["withheld"] == {"archive-today": 1, "unaudited:wayback.archive-it.org": 1}
    assert result["resolver"] == "timetravel"
    captures = client.store.captures_for_url(h.NS, h.URL, scopes={READ_SCOPE})
    assert {c["archive_id"] for c in captures} == {"internet-archive", "uk-web-archive"}
    assert {c["resolver"] for c in captures} == {"timetravel"}
    assert "timetravel" not in {c["archive_id"] for c in captures}
    assert all(c["receipt"]["evidence_origin"] == "fixture" for c in captures)
    assert transport.hosts() == ["timetravel.mementoweb.org"]
    # The request id is a durable receipt: a replay makes no request.
    replay = client.resolve(h.URL, request_id="r1")
    assert replay["replayed"] is True and len(transport.calls) == 1
    with pytest.raises(MementoError) as conflict:
        client.resolve(h.URL, request_id="r1", via_aggregator=False)
    assert conflict.value.code == "request_id_conflict"
    # Re-reading an unchanged TimeMap adds no snapshot.
    before = conn.execute("SELECT count(*) FROM web_archive_timemaps").fetchone()[0]
    client.resolve(h.URL, request_id="r2")
    assert conn.execute("SELECT count(*) FROM web_archive_timemaps").fetchone()[0] == before


def test_unavailable_aggregator_is_reported_per_archive_not_hidden():
    conn = h.connect()
    transport = h.FakeArchives({"https://timetravel.mementoweb.org/timemap/json/":
                                {"status": 503, "headers": {}, "content": b""}})
    result = h.client(conn, transport).resolve(h.URL, request_id="down")
    assert result["archives"]["internet-archive"]["outcome"] == "not_resolved"
    assert result["archives"]["archive-today"]["outcome"] == "excluded_by_access_decision"
    (snap,) = [t for t in h.client(conn).store.timemaps_for_url(h.NS, h.URL, scopes={READ_SCOPE})
               if t["archive_id"] == "timetravel"]
    assert snap["outcome"] == "archive_unavailable"


def test_timegate_negotiation_sends_accept_datetime_and_records_the_holding_archive():
    conn = h.connect()
    transport = h.FakeArchives()
    answer = h.client(conn, transport).timegate(h.URL, "2024-03-01T00:00:00Z", request_id="tg")
    assert transport.calls[0]["headers"]["Accept-Datetime"] == "Fri, 01 Mar 2024 00:00:00 GMT"
    assert answer["archive_id"] == "internet-archive" and answer["resolver"] == "timetravel"
    assert answer["memento_datetime"] == "2024-02-15T09:30:00Z" and answer["uri_m"] == h.IA_MEMENTO


def test_direct_national_archive_reads_record_reading_room_captures_without_fetching_them():
    conn = h.connect()
    transport = h.FakeArchives()
    client = h.client(conn, transport)
    result = client.resolve(h.URL, request_id="direct", via_aggregator=False,
                            archives=["uk-web-archive", "arquivo-pt", "archive-today"])
    ukwa = result["archives"]["uk-web-archive"]
    assert ukwa["outcome"] == "captures" and ukwa["reading_room_only"] == 1
    conditions = {c["uri_m"].split("/wayback/")[1][:3]: c["access_condition"]
                  for c in client.store.captures_for_url(h.NS, h.URL, scopes={READ_SCOPE})}
    assert conditions == {"arc": "open", "ld/": "reading-room-only"}
    assert result["archives"]["arquivo-pt"]["outcome"] == "no_capture_on_record"
    assert result["archives"]["archive-today"]["outcome"] == "excluded_by_access_decision"
    assert "archive.ph" not in transport.hosts()
    # Only TimeMaps were read: no capture body was fetched from any archive.
    assert all("/timemap/" in c["url"] for c in transport.calls)


def test_per_archive_failures_are_isolated():
    conn = h.connect()
    transport = h.FakeArchives({"https://www.webarchive.org.uk/wayback/archive/timemap/link/":
                                ConnectionError("reset")})
    result = h.client(conn, transport).resolve(h.URL, request_id="iso", via_aggregator=False,
                                               archives=["uk-web-archive", "arquivo-pt"])
    assert result["archives"]["uk-web-archive"]["outcome"] == "archive_unavailable"
    assert result["archives"]["arquivo-pt"]["outcome"] == "no_capture_on_record"
    oversized = h.FakeArchives({"https://arquivo.pt/wayback/timemap/link/":
                                {"status": 200, "headers": {}, "content": b"x" * 2_000_001}})
    big = h.client(h.connect(), oversized).resolve_archive(h.URL, "arquivo-pt", request_id="big")
    assert big["outcome"] == "archive_unavailable" and big["detail"]["failure"] == "response_too_large"


def test_archive_today_when_permitted_maps_mementos_and_stops_on_captcha():
    permitted = copy.deepcopy(ARCHIVES)
    permitted["archive-today"]["access_decision"] = "in_scope"
    conn = h.connect()
    client = h.client(conn, h.FakeArchives(), archives=permitted)
    result = client.resolve_archive(h.URL, "archive-today", request_id="at")
    assert result["outcome"] == "captures"
    (capture,) = client.store.captures_for_url(h.NS, h.URL, scopes={READ_SCOPE})
    assert capture["archive_id"] == "archive-today" and capture["memento_datetime"] == "2024-02-18T08:00:00Z"
    captcha = h.FakeArchives({"https://archive.ph/timemap/": {
        "status": 200, "headers": {}, "content": b"<html>Please complete the CAPTCHA</html>"}})
    blocked = h.client(h.connect(), captcha, archives=permitted).resolve_archive(h.URL, "archive-today",
                                                                                  request_id="cap")
    assert blocked["outcome"] == "blocked_by_archive" and len(captcha.calls) == 1
    assert blocked["detail"]["retry_elsewhere"] is False
    limited = h.FakeArchives({"https://archive.ph/timemap/": {"status": 429, "headers": {}, "content": b""}})
    assert h.client(h.connect(), limited, archives=permitted).resolve_archive(
        h.URL, "archive-today", request_id="rl")["outcome"] == "blocked_by_archive"
