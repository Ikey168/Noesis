"""New statistics, reports, loss-estimate revisions and identity changes through subscriptions (#2230, IN10)."""

from __future__ import annotations

import copy

import tests.unit.insurance_harness as h
from src.domains.market.insurance import CONTRACT, InsuranceStore
from src.domains.market.insurance_identity import InsuranceIdentity
from src.domains.market.insurance_monitoring import InsuranceMonitor, verify_receipt
from src.kb.subscriptions import SubscriptionStore

FORBIDDEN = ("solvent", "insolvent", "rating", "downgrade", "trend", "worse", "better", "risk")


def commit(conn, watermark: int) -> None:
    SubscriptionStore(conn).commit_watermark(h.NS, watermark)


def kinds(result):
    return [n["kind"] for n in result["notifications"]]


def estimate(value: str, published: str) -> dict:
    return {
        "contract": CONTRACT, "kind": "catastrophe_loss_estimate",
        "source": {"provider": "florida-oir-claims", "source_id": "fiktiva-2|insured",
                   "url": "https://floir.com/fixture/claims.csv"},
        "publication_date": published, "publisher": "Florida Office of Insurance Regulation",
        "event": {"name": "Hurricane Fiktiva", "identifiers": {"nhc_storm_id": "AL992024"}},
        "estimate_type": "insured", "measure": "estimated insured losses", "value": value, "unit": "USD",
        "currency": "USD",
    }


def test_each_event_kind_cites_its_publication_and_unchanged_runs_emit_nothing():
    conn = h.connection()
    h.acquire(conn, "eiopa", "2025-07-01")
    h.acquire(conn, "sfcr", "2025-05-02")
    InsuranceStore(conn, initialize=False).apply(h.NS, [estimate("1", "2024-10-15")], run_id="e1",
                                                 observed_at_ms=h.ms("2024-10-16"))
    monitor = InsuranceMonitor(conn, now=lambda: h.ms("2026-01-01"))
    market = monitor.create(h.NS, "m", watch="market", target="DE", principal_id=h.PRINCIPAL, scopes=h.SCOPES)
    event = monitor.create(h.NS, "e", watch="event", target="AL992024", principal_id=h.PRINCIPAL, scopes=h.SCOPES)
    insurer = monitor.create(h.NS, "i", watch="insurer", target=h.SOLO_LEI, principal_id=h.PRINCIPAL,
                             scopes=h.SCOPES)
    commit(conn, 1)
    for sub in (market, event, insurer):
        first = monitor.run(sub["subscription_id"], principal_id=h.PRINCIPAL, scopes=h.SCOPES)
        assert first["baseline"] and set(kinds(first)) == {"in_view"}
    # Unchanged state at a new watermark: nothing.
    commit(conn, 2)
    assert kinds(monitor.run(market["subscription_id"], principal_id=h.PRINCIPAL, scopes=h.SCOPES)) == []
    # A new release revises one figure and is itself a new vintage.
    h.acquire(conn, "eiopa", "2026-01-05")
    InsuranceStore(conn, initialize=False).apply(h.NS, [estimate("2", "2024-11-15")], run_id="e2",
                                                 observed_at_ms=h.ms("2024-11-16"))
    h.ownership_entities(conn)
    InsuranceIdentity(conn).propose(h.NS, principal_id=h.PRINCIPAL, scopes=h.SCOPES, ownership_namespace=h.OWN_NS)
    commit(conn, 3)
    market_run = monitor.run(market["subscription_id"], principal_id=h.PRINCIPAL, scopes=h.SCOPES)
    assert kinds(market_run).count("new_vintage") == 2  # the release and the revised motor premium
    event_run = monitor.run(event["subscription_id"], principal_id=h.PRINCIPAL, scopes=h.SCOPES)
    assert kinds(event_run) == ["new_loss_estimate_revision"]
    insurer_run = monitor.run(insurer["subscription_id"], principal_id=h.PRINCIPAL, scopes=h.SCOPES)
    assert set(kinds(insurer_run)) == {"identity_match_change"}
    # A corrected group SFCR notifies the group's watchers.
    group = monitor.create(h.NS, "g", watch="insurer", target=h.GROUP_LEI, principal_id=h.PRINCIPAL,
                           scopes=h.SCOPES)
    monitor.run(group["subscription_id"], principal_id=h.PRINCIPAL, scopes=h.SCOPES)
    h.acquire(conn, "sfcr", "2025-06-12")
    commit(conn, 4)
    corrected = monitor.run(group["subscription_id"], principal_id=h.PRINCIPAL, scopes=h.SCOPES)
    assert kinds(corrected) == ["corrected_insurer_report"]
    for result in (market_run, event_run, insurer_run, corrected):
        for note in result["notifications"]:
            assert not any(word in note["message"].casefold() for word in FORBIDDEN)
            assert verify_receipt(conn, note["receipt"])["valid"]
    note = event_run["notifications"][0]
    assert note["receipt"]["url"] == "https://floir.com/fixture/claims.csv"
    assert note["receipt"]["publication_date"] == "2024-11-15"
    forged = copy.deepcopy(note["receipt"])
    forged["record_hash"] = "0" * 64
    assert verify_receipt(conn, forged)["valid"] is False
    # Replaying a watermark changes nothing.
    assert monitor.run(event["subscription_id"], 3, principal_id=h.PRINCIPAL, scopes=h.SCOPES)["status"] in {
        "ignored", "replayed"}
    assert monitor.poll(event["subscription_id"], principal_id=h.PRINCIPAL, scopes=h.SCOPES)["events"]
