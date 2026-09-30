"""Save Page Now behind an explicit write scope (#2311, WA11). Fixture transport only, never the network."""

from __future__ import annotations

import pytest

from src.ingestion import wayback
from src.ingestion.wayback import (
    SavePageNowError,
    check_save_page_now,
    request_save_page_now,
    save_page_now_enabled,
    save_page_now_requests,
)
from src.kb.citation_preservation import CitationPreservationStore
from tests.unit import web_archive_harness as h

CREDS = {"access": "fixture-access", "secret": "fixture-secret"}


class FakeSPN:
    def __init__(self, submit, *statuses):
        self.submit, self.statuses, self.calls = submit, list(statuses), []

    def __call__(self, *, url, params, headers, timeout, max_bytes, method="GET", data=None):
        self.calls.append({"url": url, "method": method, "data": data, "headers": dict(headers)})
        if method == "POST":
            return self.submit
        return self.statuses.pop(0)


def _json(value, status=200):
    import json

    return {"status": status, "headers": {"Content-Type": "application/json"}, "content": json.dumps(value).encode()}


SUBMITTED = _json({"url": h.URL, "job_id": "spn2-fixture-1"})
SUCCESS = _json({"status": "success", "job_id": "spn2-fixture-1", "timestamp": "20260930101500",
                 "original_url": h.URL})


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def refuse(**_):
        raise AssertionError("the live Save Page Now transport must never run in tests")

    monkeypatch.setattr(wayback, "_spn_request", refuse)


def _request(conn, transport, request_id="spn-1", scopes=None, **extra):
    return request_save_page_now(conn, h.URL, namespace=h.NS, request_id=request_id, principal_id="alice",
                                 scopes=scopes or h.ARCHIVE_WRITE, feature_enabled=True, transport=transport,
                                 credentials=CREDS, citation_id="cite:1", evidence_origin="fixture",
                                 now=h.clock(), **extra)


def test_read_only_and_ordinary_writers_are_refused_before_any_request():
    transport = FakeSPN(SUBMITTED, SUCCESS)
    for scopes in (h.READ_ONLY, h.SCOPES):
        with pytest.raises(SavePageNowError) as refused:
            _request(h.connect(), transport, scopes=scopes)
        assert refused.value.code == "unauthorized"
    with pytest.raises(SavePageNowError) as off:
        request_save_page_now(h.connect(), h.URL, namespace=h.NS, request_id="x", principal_id="a",
                              scopes=h.ARCHIVE_WRITE, feature_enabled=False, transport=transport, credentials=CREDS)
    assert off.value.code == "feature_disabled"
    assert transport.calls == [] and save_page_now_enabled(h.connect()) is False


def test_a_successful_request_records_job_status_and_a_pinnable_capture():
    conn = h.connect()
    transport = FakeSPN(SUBMITTED, SUCCESS)
    receipt = _request(conn, transport)
    assert receipt["status"] == "success" and receipt["job_id"] == "spn2-fixture-1"
    assert receipt["requester"] == "alice" and receipt["url"] == h.URL and receipt["requested_at_ms"]
    assert receipt["uri_m"] == "https://web.archive.org/web/20260930101500/https://example.org/report"
    assert "fixture-secret" not in str(receipt)
    assert [c["method"] for c in transport.calls] == ["POST", "GET"]
    assert transport.calls[0]["data"] == {"url": h.URL}
    store = CitationPreservationStore(conn)
    capture = store.capture_record(h.NS, receipt["capture_id"], scopes=h.SCOPES)
    assert capture["resolver"] == "save-page-now" and capture["archive_id"] == "internet-archive"
    pin = store.pin_citation(h.NS, "cite:1", capture["capture_id"], h.URL, principal_id="alice", scopes=h.SCOPES)
    assert pin["capture"]["uri_m"] == receipt["uri_m"]
    assert _request(conn, transport)["replayed"] is True and len(transport.calls) == 2
    assert save_page_now_requests(conn, h.NS, scopes=h.READ_ONLY)[0]["job_id"] == "spn2-fixture-1"


def test_robots_excluded_urls_are_recorded_as_refused_and_never_retried():
    conn = h.connect()
    transport = FakeSPN(_json({"status": "error", "status_ext": "error:robots-txt",
                               "message": "This URL is excluded"}))
    receipt = _request(conn, transport)
    assert receipt["status"] == "refused" and receipt["status_ext"] == "error:robots-txt"
    assert receipt["retry_elsewhere"] is False and len(transport.calls) == 1


def test_pending_jobs_are_polled_once_per_call_and_the_daily_budget_is_enforced():
    conn = h.connect()
    transport = FakeSPN(SUBMITTED, _json({"status": "pending", "job_id": "spn2-fixture-1"}), SUCCESS)
    assert _request(conn, transport)["status"] == "pending"
    checked = check_save_page_now(conn, "spn-1", namespace=h.NS, scopes=h.ARCHIVE_WRITE, transport=transport,
                                  credentials=CREDS, now=h.clock())
    assert checked["status"] == "success" and checked["polled"] is True
    for n in range(2, 6):
        _request(conn, FakeSPN(SUBMITTED, SUCCESS), request_id=f"spn-{n}")
    with pytest.raises(SavePageNowError) as spent:
        _request(conn, FakeSPN(SUBMITTED, SUCCESS), request_id="spn-6")
    assert spent.value.code == "budget_exhausted"


def test_the_live_transport_is_restricted_to_the_verification_set(monkeypatch):
    monkeypatch.delenv("NOESIS_IA_S3_ACCESS_KEY", raising=False)
    monkeypatch.delenv("NOESIS_IA_S3_SECRET_KEY", raising=False)
    with pytest.raises(SavePageNowError) as outside:
        request_save_page_now(h.connect(), h.URL, namespace=h.NS, request_id="live", principal_id="a",
                              scopes=h.ARCHIVE_WRITE, feature_enabled=True)
    assert outside.value.code == "not_in_verification_set"
    with pytest.raises(SavePageNowError) as no_keys:
        request_save_page_now(h.connect(), "https://example.com/", namespace=h.NS, request_id="live",
                              principal_id="a", scopes=h.ARCHIVE_WRITE, feature_enabled=True)
    assert no_keys.value.code == "credentials_missing"
