"""Unattended scheduled brief delivery over a configured webhook (#1678)."""

from __future__ import annotations

import duckdb
import pytest

from src.domains.market.delivery import MarketBriefDeliveryWorker
from src.domains.market.research import MarketResearchError, MarketResearchStore
from tests.unit.domains.test_market_research import NS, SCOPES

REQUEST = {
    "version": 1,
    "title": "Daily peer brief",
    "sections": [{"heading": "Summary", "body": "Fixture observation."}],
    "cutoff_ms": 900,
    "formula_versions": ["formula:1"],
    "assumptions": [],
    "source_locators": [],
}


class Recorder:
    def __init__(self, failures=0):
        self.failures = failures
        self.calls = []

    def __call__(self, payload, *, idempotency_key):
        self.calls.append((payload, idempotency_key))
        if self.failures:
            self.failures -= 1
            raise ConnectionError("receiver unavailable")


def _setup(transport, *, external=False):
    conn = duckdb.connect(":memory:")
    clock = {"now": 1000}
    store = MarketResearchStore(conn, now=lambda: clock["now"])
    store.schedule_brief(
        NS, schedule_id="schedule:daily", report_id="brief:daily", cadence="daily",
        next_due_ms=900, request=REQUEST, owner="alice", principal_id="alice", scopes=SCOPES,
    )
    worker = MarketBriefDeliveryWorker(
        conn,
        {
            "enabled": True,
            "principal_id": "alice",
            "scopes": sorted(SCOPES),
            "namespaces": [NS],
            "destinations": [{"kind": "webhook", "ref": "team", "url_env": "NOESIS_TEST_BRIEF_URL"}],
            "external": external,
            "retry_delay_s": 60,
        },
        transports={"webhook:team": transport},
        now=lambda: clock["now"],
    )
    return conn, clock, worker


def test_due_schedule_is_generated_exported_and_delivered_once():
    transport = Recorder()
    _conn, _clock, worker = _setup(transport)

    first = worker.tick("worker:test")
    second = worker.tick("worker:test")

    assert first["generated"] == 1 and first["delivered"] == 1
    payload, key = transport.calls[0]
    assert payload["contract"] == "noesis-market-brief-delivery-payload-v1"
    assert payload["delivery_key"] == key == first["deliveries"][0]["delivery_key"]
    assert "# Daily peer brief" in payload["export"]["payload"]
    assert payload["export"]["rights"]["status"] == "not_provided"  # no source locators
    assert second["generated"] == 0 and second["delivered"] == 0
    assert len(transport.calls) == 1


def test_failed_delivery_is_retried_with_the_same_idempotency_key():
    transport = Recorder(failures=1)
    _conn, clock, worker = _setup(transport)

    failed = worker.tick("worker:test")
    early = worker.tick("worker:test")
    clock["now"] += 60_000
    retried = worker.tick("worker:test")

    assert failed["retrying"] == 1
    assert failed["deliveries"][0]["error"] == "ConnectionError"
    assert len(transport.calls) == 2  # not resent before the retry is due
    assert early["deliveries"] == [] or early["deliveries"][0]["deduplicated"]
    assert retried["delivered"] == 1
    assert retried["deliveries"][0]["attempts"] == 2
    assert transport.calls[0][1] == transport.calls[1][1]


def test_external_delivery_without_verified_rights_is_withheld_and_not_sent():
    transport = Recorder()
    conn, _clock, worker = _setup(transport, external=True)

    result = worker.tick("worker:test")

    assert result["withheld"] == 1
    assert transport.calls == []
    assert conn.execute("SELECT status FROM market_research_deliveries").fetchone()[0] == "withheld"
    assert worker.tick("worker:test")["deliveries"] == []


@pytest.mark.parametrize(
    ("config", "code"),
    [
        ({"enabled": False}, "delivery_disabled"),
        ({"enabled": True, "principal_id": "alice", "namespaces": [NS],
          "destinations": [{"kind": "webhook", "ref": "team", "url_env": "HOME"}]}, "invalid_destinations"),
        ({"enabled": True, "principal_id": "alice", "namespaces": [NS],
          "destinations": [{"kind": "email", "ref": "team", "url_env": "NOESIS_X"}]}, "invalid_destinations"),
        ({"enabled": True, "principal_id": "alice", "namespaces": [NS],
          "destinations": [{"kind": "webhook", "ref": "team", "url_env": "NOESIS_MISSING_URL"}]}, "destination_unavailable"),
    ],
)
def test_worker_requires_explicit_configuration(config, code):
    with pytest.raises(MarketResearchError) as error:
        MarketBriefDeliveryWorker(duckdb.connect(":memory:"), config, environ={})
    assert error.value.code == code


def test_delivered_brief_carries_structural_accessibility_result():
    transport = Recorder()
    _conn, _clock, worker = _setup(transport)

    worker.tick("worker:test")

    accessibility = transport.calls[0][0]["accessibility"]
    assert accessibility["status"] == "passed"
    assert accessibility["human_review"] == "not_performed"


def test_accessibility_check_flags_structural_problems():
    from src.domains.market.research import check_market_brief_accessibility

    markdown = "\n".join([
        "# Title", "", "#### Deep heading", "", "![](chart.png)", "",
        "| a | b |", "| 1 | 2 |", "", "## Charts", "", "### Revenue", "", "## Sources",
    ])
    issues = {
        item["code"]
        for item in check_market_brief_accessibility({"format": "markdown", "payload": markdown})["issues"]
    }
    assert issues == {
        "skipped_heading_level",
        "image_without_alt_text",
        "table_without_header_row",
        "chart_without_text_alternative",
    }
    assert check_market_brief_accessibility({"format": "pdf", "payload": {"bytes_b64": ""}})["status"] == "not_applicable"
    assert check_market_brief_accessibility({"format": "json", "payload": None})["status"] == "not_applicable"
