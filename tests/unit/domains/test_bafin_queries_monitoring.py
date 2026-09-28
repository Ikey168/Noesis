"""Point-in-time BaFin notice queries, issuer dossiers and monitors (#2106, BF09, BF10)."""

from __future__ import annotations

import copy

import pytest

from src.domains.market.asof import MarketAsOfSnapshotStore
from src.domains.market.bafin_identity import BafinIdentity, organisation_key
from src.domains.market.bafin_monitoring import BafinNoticeMonitor, verify_receipt
from src.domains.market.bafin_notices import (
    CONTRACT,
    BafinError,
    BafinNoticeStore,
    end_of_day_ms,
)
from src.domains.market.bafin_queries import BafinQueries, export_dossier_bundle
from src.evidence_bundle.verifier import verify_bundle
from src.kb.subscriptions import SubscriptionStore
from tests.unit import bafin_harness as h


@pytest.fixture(scope="module")
def full():
    conn = h.connection()
    h.acquire_all(conn)
    return conn


def ids(rows):
    return [r["source"]["source_id"] for r in rows]


def test_a_notice_published_after_the_cutoff_is_invisible_even_with_an_earlier_event_date(
    full,
):
    q = BafinQueries(full)
    # VR-2026-0001: event 2026-03-02, published 2026-03-05.
    assert (
        q.holders_above_thresholds(h.NS, h.ISSUER, "2026-03-04", scopes=h.SCOPES)[
            "holders"
        ]
        == []
    )
    on = q.holders_above_thresholds(h.NS, h.ISSUER, "2026-03-05", scopes=h.SCOPES)
    assert ids(on["holders"]) == ["VR-2026-0001"]
    assert on["cutoffs"]["publicly_available_by_ms"] == end_of_day_ms("2026-03-05")
    intraday = q.holders_above_thresholds(
        h.NS, h.ISSUER, end_of_day_ms("2026-03-05") - 3_600_000, scopes=h.SCOPES
    )
    assert (
        intraday["holders"] == []
    )  # a date-only publication counts from the end of that day
    # Acquired later than the acquisition cutoff: not seen, although published.
    late = q.holders_above_thresholds(
        h.NS, h.ISSUER, "2026-03-05", scopes=h.SCOPES, acquired_by_ms=h.ms("2026-03-09")
    )
    assert late["holders"] == []


def test_holders_show_the_latest_notification_per_notifier_with_basis_chain_and_corrections(
    full,
):
    q = BafinQueries(full)
    before = q.holders_above_thresholds(h.NS, h.ISSUER, "2026-03-19", scopes=h.SCOPES)
    assert before["holders"][0]["percentages"]["s33"] == "5.12"
    after = q.holders_above_thresholds(h.NS, h.ISSUER, "2026-04-14", scopes=h.SCOPES)
    by_name = {r["notifier"]["name"]: r for r in after["holders"]}
    fiktiva = by_name["Fiktiva Holding SE"]
    assert (
        fiktiva["source"]["source_id"] == "VR-2026-0007"
        and len(fiktiva["corrections"]) == 1
    )
    assert fiktiva["thresholds_reached"] == {"s33": ["3", "5"], "s38": [], "s39": ["5"]}
    assert [m["name"] for m in fiktiva["chain"]][-1] == "Fiktiva Invest GmbH"
    nordlicht = by_name["Nordlicht Asset Management S.A."]
    assert nordlicht["thresholds_reached"] == {"s33": ["3"], "s38": [], "s39": []}
    assert (
        "sum" not in str(after["semantics"]).lower()
        or "never summed" in str(after["semantics"]).lower()
    )
    only_five = q.holders_above_thresholds(
        h.NS, h.ISSUER, "2026-04-14", scopes=h.SCOPES, threshold="5"
    )
    assert [r["notifier"]["name"] for r in only_five["holders"]] == [
        "Fiktiva Holding SE"
    ]
    stale = q.holders_above_thresholds(h.NS, h.ISSUER, "2027-06-01", scopes=h.SCOPES)
    assert all(r["stale"] and r["last_notice_date"] for r in stale["holders"])
    assert (
        len(stale["holders"]) == 2
    )  # shown with their last notice date, never dropped


def test_managers_transactions_in_a_window_for_an_issuer_or_a_person(full):
    q = BafinQueries(full)
    april = q.managers_transactions(
        h.NS,
        scopes=h.SCOPES,
        isin=h.ISSUER,
        date_from="2026-03-01",
        date_to="2026-04-30",
    )
    assert ids(april["transactions"]) == ["DD-2026-0101", "DD-2026-0102"]
    aggregated = april["transactions"][0]
    assert (
        len(aggregated["trades"]) == 2 and aggregated["aggregate"]["volume"] == "1500"
    )
    person = q.managers_transactions(
        h.NS,
        scopes=h.SCOPES,
        person="Max Mustermann",
        date_from="2026-01-01",
        date_to="2026-06-30",
    )
    assert ids(person["transactions"]) == ["DD-2026-0102"]
    # The removed transaction's manager is withdrawn and never matches a person query.
    erika = q.managers_transactions(
        h.NS,
        scopes=h.SCOPES,
        person="Erika Musterfrau",
        date_from="2026-01-01",
        date_to="2026-06-30",
    )
    assert ids(erika["transactions"]) == ["DD-2026-0110"]
    with pytest.raises(BafinError):
        q.managers_transactions(
            h.NS, scopes=h.SCOPES, date_from="2026-01-01", date_to="2026-06-30"
        )


def test_short_positions_that_end_read_below_threshold_or_closed_never_zero(full):
    q = BafinQueries(full)
    open_ = q.net_short_positions(h.NS, h.ISSUER, "2026-04-30", scopes=h.SCOPES)
    assert [(p["holder"]["name"], p["position_pct"]) for p in open_["positions"]] == [
        ("Kurzfrist Capital LLP", "0.55"),
        ("Zeitwert Partners Ltd", "0.71"),
    ]
    assert open_["sum_of_published_positions"]["value"] == "1.26"
    assert "not total short interest" in open_["sum_of_published_positions"]["label"]
    later = q.net_short_positions(h.NS, h.ISSUER, "2026-06-01", scopes=h.SCOPES)
    assert later["positions"] == []
    assert {p["status"] for p in later["no_longer_published"]} == {
        "below publication threshold or closed"
    }
    assert all("position_pct" not in p for p in later["no_longer_published"])
    assert later["sum_of_published_positions"]["value"] is None


def test_warnings_are_never_attributed_without_review_and_authorisation_is_as_of(full):
    q = BafinQueries(full)
    hits = q.warnings_for_entity(
        h.NS, scopes=h.SCOPES, name="Fiktiva Invest GmbH", as_of="2026-06-01"
    )
    assert hits["matched_by_review"] == [] and len(hits["name_equal_unreviewed"]) == 1
    assert "not attributed" in hits["name_equal_unreviewed"][0]["match"]
    assert (
        q.warnings_for_entity(
            h.NS, scopes=h.SCOPES, name="Fiktiva Invest GmbH", as_of="2026-05-03"
        )["name_equal_unreviewed"]
        == []
    )
    june = q.authorisation_status(
        h.NS, scopes=h.SCOPES, as_of="2026-06-15", bafin_id="0123456"
    )
    assert june["status"] == "listed"
    july = q.authorisation_status(
        h.NS, scopes=h.SCOPES, as_of="2026-07-02", bafin_id="123456"
    )
    assert {
        lic["type"]: lic["as_of_state"] for lic in july["entities"][0]["licences"]
    } == {
        "Anlageberatung": "ended",
        "Finanzportfolioverwaltung": "active as published",
    }
    early = q.authorisation_status(
        h.NS, scopes=h.SCOPES, as_of="2026-04-09", bafin_id="123456"
    )
    assert early["status"] == "not in the acquired company database as of the date"


def test_a_reviewed_match_attributes_a_warning():
    conn = h.connection()
    h.acquire_all(conn)
    identity = BafinIdentity(conn)
    identity.propose(h.NS, principal_id=h.PRINCIPAL, scopes=h.SCOPES)
    party = organisation_key("Fiktiva Invest GmbH")
    link = identity.propose_link(
        h.NS,
        "bafin:named:fiktiva invest",
        target_key=party,
        target_entity=None,
        evidence={
            "kind": "name",
            "value": "Fiktiva Invest GmbH",
            "target_source": "the warning text names the authorised firm",
            "attributes": [{"kind": "context", "value": "identity theft"}],
        },
        principal_id="reviewer",
        scopes=h.SCOPES,
    )
    identity.service.review(
        h.NS,
        link["candidate_id"],
        "accept",
        "the warning concerns this firm's identity",
        principal_id="reviewer",
        scopes=h.SCOPES,
    )
    hits = BafinQueries(conn).warnings_for_entity(
        h.NS, scopes=h.SCOPES, party=party, as_of="2026-06-01"
    )
    assert len(hits["matched_by_review"]) == 1 and hits["name_equal_unreviewed"] == []


def test_the_dossier_combines_everything_cites_revisions_and_exports_a_verifiable_bundle(
    full,
):
    q = BafinQueries(full)
    dossier = q.dossier(
        h.NS, h.ISSUER, "2026-07-02", scopes=h.SCOPES, principal_id=h.PRINCIPAL
    )
    assert dossier["issuer_name"] == "Musterwerke AG"
    assert {r["name"] for r in dossier["related_entities"]} >= {
        "Fiktiva Invest GmbH",
        "Fiktiva Holding SE",
        "Musterwerke AG",
        "Zeitwert Partners Ltd",
    }
    assert [w["entity"] for w in dossier["warnings_and_measures"]] == [
        "Fiktiva Invest GmbH"
    ]
    assert dossier["authorisations"][0]["match"] == "name-equal, unreviewed"
    assert len(dossier["managers_transactions"]["transactions"]) == 3
    assert all(
        n.startswith("bafin-notice:") and r.startswith("bafin-rev:")
        for n, r in dossier["pins"].items()
    )
    earlier = q.dossier(
        h.NS, h.ISSUER, "2026-03-19", scopes=h.SCOPES, principal_id=h.PRINCIPAL
    )
    assert (
        earlier["warnings_and_measures"] == []
        and earlier["net_short_positions"]["positions"] == []
    )
    assert earlier["dossier_hash"] != dossier["dossier_hash"]
    bundle = export_dossier_bundle(full, dossier, created_at_ms=1)
    verified = verify_bundle(bundle)
    assert verified.valid, verified.errors
    evidence = [o for o in bundle["objects"] if o["type"] == "evidence"]
    assert len(evidence) == len(dossier["pins"])
    forged = copy.deepcopy(dossier)
    forged["pins"]["bafin-notice:x"] = "bafin-rev:does-not-exist"
    with pytest.raises(BafinError):
        export_dossier_bundle(full, forged, created_at_ms=1)


def test_market_asof_snapshots_pin_only_published_notice_revisions(full):
    snapshots = MarketAsOfSnapshotStore(full, now=lambda: 1)
    scopes = h.SCOPES | {"operator"}

    def capture(day, key):
        return snapshots.create_snapshot(
            "market:asof-test",
            key,
            effective_at_ms=end_of_day_ms(day),
            publicly_available_by_ms=end_of_day_ms(day),
            acquired_by_ms=h.ms("2026-12-31"),
            principal_id=h.PRINCIPAL,
            scopes=scopes,
            selection={
                "bafin_notices": [
                    {
                        "namespace": h.NS,
                        "issuer_isin": h.ISSUER,
                        "kinds": ["voting_rights_notification"],
                    }
                ]
            },
        )

    early = capture("2026-03-04", "early")
    assert (
        early["inputs"] == []
        and early["gaps"][0]["reason"] == "no_notices_published_at_cutoffs"
    )
    late = capture("2026-03-20", "late")
    assert {i["source_id"] for i in late["inputs"]} == {"VR-2026-0001", "VR-2026-0007"}
    assert all(i["public_at_ms"] <= end_of_day_ms("2026-03-20") for i in late["inputs"])
    assert capture("2026-03-20", "late")["idempotent"] is True
    with pytest.raises(Exception):
        snapshots.create_snapshot(
            "market:asof-test",
            "bad",
            effective_at_ms=1,
            publicly_available_by_ms=1,
            acquired_by_ms=1,
            principal_id=h.PRINCIPAL,
            scopes=scopes,
            selection={"bafin_notices": [{"issuer_isin": h.ISSUER}]},
        )


# ------------------------------------------------------------------ monitors


def monitor_world():
    conn = h.connection()
    h.acquire(conn, "voting", "2026-03-10")
    h.acquire(conn, "shorts", "2026-04-10")
    h.acquire(conn, "company", "2026-04-10")
    return conn


def run(monitor, subscription_id, watermark):
    SubscriptionStore(monitor.conn).commit_watermark(h.NS, watermark, kind="ingestion")
    return monitor.run(subscription_id, principal_id=h.PRINCIPAL, scopes=h.SCOPES)


def test_the_first_evaluation_is_a_baseline_and_replays_add_nothing():
    conn = monitor_world()
    monitor = BafinNoticeMonitor(conn)
    issuer = monitor.create(
        h.NS,
        "issuer",
        watch="issuer",
        isin=h.ISSUER,
        principal_id=h.PRINCIPAL,
        scopes=h.SCOPES,
    )
    first = run(monitor, issuer["subscription_id"], 1)
    assert first["baseline"] and first["evaluation"] == 1
    assert {n["kind"] for n in first["notifications"]} == {"in_view"}
    assert (
        monitor.run(
            issuer["subscription_id"], 1, principal_id=h.PRINCIPAL, scopes=h.SCOPES
        )["status"]
        == "replayed"
    )
    unchanged = run(monitor, issuer["subscription_id"], 2)
    assert unchanged["notifications"] == [] and unchanged["evaluation"] == 2


def test_alerts_for_corrections_short_position_ends_warnings_and_authorisation_changes():
    conn = monitor_world()
    monitor = BafinNoticeMonitor(conn)
    issuer = monitor.create(
        h.NS,
        "issuer",
        watch="issuer",
        isin=h.ISSUER,
        principal_id=h.PRINCIPAL,
        scopes=h.SCOPES,
    )
    holder = monitor.create(
        h.NS,
        "holder",
        watch="holder",
        name="Fiktiva Invest GmbH",
        principal_id=h.PRINCIPAL,
        scopes=h.SCOPES,
    )
    manager = monitor.create(
        h.NS,
        "manager",
        watch="manager",
        isin=h.ISSUER,
        person="Erika Musterfrau",
        principal_id=h.PRINCIPAL,
        scopes=h.SCOPES,
    )
    warnings = monitor.create(
        h.NS,
        "warnings",
        watch="warning-list",
        principal_id=h.PRINCIPAL,
        scopes=h.SCOPES,
    )
    for subscription in (issuer, holder, manager, warnings):
        run(monitor, subscription["subscription_id"], 1)
    h.acquire(conn, "voting", "2026-04-20")
    h.acquire(conn, "shorts", "2026-05-01")
    h.acquire(conn, "dealings", "2026-04-10")
    h.acquire(conn, "warnings", "2026-05-15")
    h.acquire(conn, "company", "2026-07-01")
    kinds = {
        name: [
            n["kind"] for n in run(monitor, s["subscription_id"], 2)["notifications"]
        ]
        for name, s in {
            "issuer": issuer,
            "holder": holder,
            "manager": manager,
            "warnings": warnings,
        }.items()
    }
    assert sorted(kinds["issuer"]) == sorted(
        [
            "corrected_notification",
            "new_notification",
            "short_position_ended",
            "no_longer_listed",
            "new_managers_transaction",
            "new_managers_transaction",
        ]
    )
    # The holder is a chain member of the corrected notification and of the one no longer listed.
    assert sorted(kinds["holder"]) == sorted(
        [
            "corrected_notification",
            "new_warning",
            "authorisation_change",
            "no_longer_listed",
        ]
    )
    assert kinds["manager"] == ["new_managers_transaction"]
    assert sorted(kinds["warnings"]) == [
        "new_measure",
        "new_warning",
        "new_warning",
        "new_warning",
    ]
    polled = monitor.poll(
        issuer["subscription_id"], principal_id=h.PRINCIPAL, scopes=h.SCOPES
    )
    assert polled["events"]


def test_a_threshold_crossing_compares_with_the_notifiers_previous_notification():
    conn = h.connection()
    h.acquire(conn, "voting", "2026-03-10")
    monitor = BafinNoticeMonitor(conn)
    issuer = monitor.create(
        h.NS,
        "issuer",
        watch="issuer",
        isin=h.ISSUER,
        principal_id=h.PRINCIPAL,
        scopes=h.SCOPES,
    )
    run(monitor, issuer["subscription_id"], 1)
    later = {
        "contract": CONTRACT,
        "kind": "voting_rights_notification",
        "source": {
            "provider": "bafin-voting-rights",
            "source_id": "VR-2026-0100",
            "source_id_basis": "stated",
        },
        "issuer": {"name": "Musterwerke AG", "isin": h.ISSUER},
        "notifier": {"name": "Fiktiva Holding SE", "kind": "legal_person"},
        "chain": [],
        "thresholds": ["10"],
        "percentages": {"s33": "10.4", "s39": "10.4"},
        "event_date": "2026-06-01",
        "publication_date": "2026-06-03",
    }
    BafinNoticeStore(conn).apply(
        h.NS, [later], run_id="later", observed_at_ms=h.ms("2026-06-04")
    )
    notes = run(monitor, issuer["subscription_id"], 2)["notifications"]
    crossing = next(n for n in notes if n["kind"] == "threshold_crossing")
    assert crossing["thresholds"]["before"]["s39"] == ["5"] and crossing["thresholds"][
        "after"
    ]["s39"] == ["5", "10"]
    # A late-arriving older notification never produces a crossing or a correction event.
    older = {
        **later,
        "source": {**later["source"], "source_id": "VR-2025-0001"},
        "percentages": {"s33": "3.1"},
        "event_date": "2025-06-01",
        "publication_date": "2025-06-03",
    }
    BafinNoticeStore(conn).apply(
        h.NS, [older], run_id="older", observed_at_ms=h.ms("2026-06-05")
    )
    notes = run(monitor, issuer["subscription_id"], 3)["notifications"]
    assert [n["kind"] for n in notes] == ["new_notification"]


def test_receipts_cite_the_notice_revision_and_are_checked_against_the_store():
    conn = monitor_world()
    monitor = BafinNoticeMonitor(conn)
    issuer = monitor.create(
        h.NS,
        "issuer",
        watch="issuer",
        isin=h.ISSUER,
        principal_id=h.PRINCIPAL,
        scopes=h.SCOPES,
    )
    run(monitor, issuer["subscription_id"], 1)
    h.acquire(conn, "voting", "2026-04-20")
    note = next(
        n
        for n in run(monitor, issuer["subscription_id"], 2)["notifications"]
        if n["kind"] == "corrected_notification"
    )
    receipt = note["receipt"]
    assert receipt["evaluation"] == 2 and receipt["revision_id"].startswith(
        "bafin-rev:"
    )
    assert verify_receipt(conn, receipt)["valid"] is True
    forged = {**receipt, "record_hash": "0" * 64}
    assert verify_receipt(conn, forged)["valid"] is False
    rehashed = {k: v for k, v in forged.items() if k != "receipt_hash"}
    from src.domains.market.bafin_notices import digest

    rehashed["receipt_hash"] = digest(rehashed)
    assert verify_receipt(conn, rehashed) == {
        "valid": False,
        "reason": "the cited revision differs from the stored one",
    }


def test_monitors_refuse_bad_watches_and_poll_is_not_ready_before_any_monitor():
    conn = monitor_world()
    monitor = BafinNoticeMonitor(conn)
    with pytest.raises(BafinError):
        monitor.create(
            h.NS,
            "x",
            watch="manager",
            person="Max",
            principal_id=h.PRINCIPAL,
            scopes=h.SCOPES,
        )
    with pytest.raises(BafinError):
        monitor.create(
            h.NS, "x", watch="everything", principal_id=h.PRINCIPAL, scopes=h.SCOPES
        )
    with pytest.raises(BafinError) as caught:
        BafinNoticeMonitor(h.connection(), initialize=False).poll(
            "subscription:none", principal_id=h.PRINCIPAL, scopes=h.SCOPES
        )
    assert caught.value.code == "not_ready"


def test_an_older_export_acquired_after_the_cutoff_never_changes_an_earlier_snapshot():
    conn = h.connection()
    store = BafinNoticeStore(conn)
    later = {
        "contract": CONTRACT,
        "kind": "voting_rights_notification",
        "source": {
            "provider": "bafin-voting-rights",
            "source_id": "VR-9",
            "source_id_basis": "stated",
        },
        "issuer": {"name": "Musterwerke AG", "isin": h.ISSUER},
        "notifier": {"name": "Fiktiva Holding SE", "kind": "legal_person"},
        "chain": [],
        "thresholds": ["5"],
        "percentages": {"s33": "5.4", "s39": "5.4"},
        "event_date": "2026-03-02",
        "publication_date": None,
    }
    store.apply(
        h.NS,
        [later],
        run_id="first",
        observed_at_ms=h.ms("2026-04-01"),
        source_as_of="2026-04-01",
    )
    snapshots = MarketAsOfSnapshotStore(conn, now=lambda: 1)
    scopes = h.SCOPES | {"operator"}
    snapshot = snapshots.create_snapshot(
        "market:asof-test",
        "before-older",
        effective_at_ms=end_of_day_ms("2026-04-10"),
        publicly_available_by_ms=end_of_day_ms("2026-04-10"),
        acquired_by_ms=end_of_day_ms("2026-04-10"),
        principal_id=h.PRINCIPAL,
        scopes=scopes,
        selection={"bafin_notices": [{"namespace": h.NS, "issuer_isin": h.ISSUER}]},
    )
    before = BafinNoticeStore(conn).visible(
        h.NS,
        public_cutoff_ms=end_of_day_ms("2026-04-10"),
        acquired_by_ms=end_of_day_ms("2026-04-10"),
    )["notices"][0]
    assert before["publication_basis"] == "first-observed" and before[
        "public_at_ms"
    ] == h.ms("2026-04-01")
    # An older export (stating the publication date) is acquired after the snapshot's acquisition cutoff.
    older = {
        **later,
        "percentages": {"s33": "5.1", "s39": "5.1"},
        "publication_date": "2026-03-05",
    }
    counts = store.apply(
        h.NS,
        [older],
        run_id="older",
        observed_at_ms=h.ms("2026-05-01"),
        source_as_of="2026-03-06",
    )
    assert counts["history"] == 1
    after = BafinNoticeStore(conn).visible(
        h.NS,
        public_cutoff_ms=end_of_day_ms("2026-04-10"),
        acquired_by_ms=end_of_day_ms("2026-04-10"),
    )["notices"][0]
    assert (
        after["revision_id"],
        after["public_at_ms"],
        after["publication_basis"],
    ) == (before["revision_id"], before["public_at_ms"], before["publication_basis"])
    inspected = snapshots.inspect_snapshot(
        "market:asof-test",
        snapshot["snapshot_id"],
        principal_id=h.PRINCIPAL,
        scopes=scopes,
    )
    assert inspected["input_hash"] == snapshot["input_hash"]
    again = snapshots.create_snapshot(
        "market:asof-test",
        "before-older",
        effective_at_ms=end_of_day_ms("2026-04-10"),
        publicly_available_by_ms=end_of_day_ms("2026-04-10"),
        acquired_by_ms=end_of_day_ms("2026-04-10"),
        principal_id=h.PRINCIPAL,
        scopes=scopes,
        selection={"bafin_notices": [{"namespace": h.NS, "issuer_isin": h.ISSUER}]},
    )
    assert again["idempotent"] is True  # re-verification recaptures the same inputs
