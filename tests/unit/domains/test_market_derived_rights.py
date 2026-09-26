"""Current-rights rechecks and retention for stored derived receipts (#1664).

Stored screener, quantitative, specialized, alert and research receipts hold
values computed from licensed sources, so they behave as caches. These checks
show that a revocation after the run withholds the cached values on read and
export, that caller-only inputs are labeled unverified rather than
authorized, and that retention removes receipts whose sources were purged.
"""

from __future__ import annotations

import json

from jsonschema import Draft7Validator

from src.domains.market.entitlements import (
    MarketEntitlementStore,
    collect_receipt_provenance,
    recheck_stored_receipt_rights,
)
from src.domains.market.quantitative import MarketQuantitativeStore
from src.domains.market.screeners import MarketScreenerStore
from tests.unit.domains.market_entitlement_fixtures import FIXTURE_CAPABILITIES
from tests.unit.domains.test_market_dashboard import (
    ACQUIRED_CUTOFF,
    NAMESPACE,
    PRINCIPAL,
    PUBLIC_CUTOFF,
    SCOPES,
    T0,
)
from tests.unit.domains.test_market_screeners import market  # noqa: F401 - fixture

PURGE_SCHEMA = json.loads(
    open(
        "contracts/schemas/jsonschema/noesis-market-entitlement-purge-report-v1.json",
        encoding="utf-8",
    ).read()
)


def _run_screen(conn):
    store = MarketScreenerStore(conn, now=lambda: ACQUIRED_CUTOFF)
    store.save_query(
        NAMESPACE,
        "screen:rights",
        {
            "filters": [{"field": "price.close", "operator": "gte", "value": 100}],
            "ranking": {"field": "price.close", "direction": "desc"},
            "missing_policy": "exclude",
            "limit": 10,
        },
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    run = store.run(
        NAMESPACE,
        universe_id="universe:tech",
        as_of_ms=T0 + 10 * 86_400_000,
        start_ms=T0 - 86_400_000,
        end_ms=T0 + 20 * 86_400_000,
        acquired_by_ms=ACQUIRED_CUTOFF,
        publicly_available_by_ms=PUBLIC_CUTOFF,
        principal_id=PRINCIPAL,
        scopes=SCOPES,
        query_id="screen:rights",
    )
    return store, run


def _set_policy(conn, *, capabilities, status="active", expected_revision):
    return MarketEntitlementStore(conn, now=lambda: ACQUIRED_CUTOFF).put_entitlement(
        NAMESPACE,
        "entitlement:fixture",
        provider="fixture-only",
        license_id="fixture-only",
        capabilities=capabilities,
        evidence_ref="synthetic:rights-review:v2",
        decision_ref="synthetic:reviewer:v2",
        principal_id="fixture-reviewer",
        scopes={"operator"},
        status=status,
        effective_at_ms=0,
        expected_revision=expected_revision,
    )


def test_stored_screen_is_authorized_while_rights_are_current(market):  # noqa: F811
    store, run = _run_screen(market)

    inspected = store.inspect_run(NAMESPACE, run["run_id"], principal_id=PRINCIPAL, scopes=SCOPES)
    exported = store.export_run(NAMESPACE, run["run_id"], principal_id=PRINCIPAL, scopes=SCOPES)

    assert inspected["current_rights"]["state"] == "authorized"
    assert inspected["current_rights"]["source_revision_count"] > 0
    assert exported["current_rights"]["operation"] == "export"
    assert len(exported["results"]) == 1


def test_revocation_after_run_withholds_cached_screen_values(market):  # noqa: F811
    store, run = _run_screen(market)
    stored_hash = store.inspect_run(
        NAMESPACE, run["run_id"], principal_id=PRINCIPAL, scopes=SCOPES
    )["record_hash"]
    _set_policy(market, capabilities=FIXTURE_CAPABILITIES, status="revoked", expected_revision=1)

    inspected = store.inspect_run(NAMESPACE, run["run_id"], principal_id=PRINCIPAL, scopes=SCOPES)
    exported = store.export_run(NAMESPACE, run["run_id"], principal_id=PRINCIPAL, scopes=SCOPES)

    for receipt in (inspected, exported):
        assert receipt["withheld"] is True
        assert receipt["rights"]["reason_codes"] == ["entitlement_revoked"]
        assert "results" not in receipt or receipt["results"] == []
        assert "input_snapshot" not in receipt
        assert receipt["record_hash"] == stored_hash


def test_export_restriction_withholds_export_but_not_internal_derive(market):  # noqa: F811
    store, run = _run_screen(market)
    _set_policy(
        market,
        capabilities=[item for item in FIXTURE_CAPABILITIES if item not in {"export", "redistribute"}],
        expected_revision=1,
    )

    inspected = store.inspect_run(NAMESPACE, run["run_id"], principal_id=PRINCIPAL, scopes=SCOPES)
    exported = store.export_run(NAMESPACE, run["run_id"], principal_id=PRINCIPAL, scopes=SCOPES)

    assert inspected["current_rights"]["state"] == "authorized"
    assert exported["withheld"] is True
    assert exported["rights"]["reason_codes"] == ["operation_restricted"]


def test_caller_supplied_inputs_are_unverified_not_authorized(market):  # noqa: F811
    receipt = {"rows": [{"source_revision_ids": ["caller:bar:1"], "value": 1}]}

    rights = recheck_stored_receipt_rights(
        market, NAMESPACE, receipt, operation="derive",
        principal_id=PRINCIPAL, scopes=SCOPES, now_ms=ACQUIRED_CUTOFF,
    )

    assert rights["state"] == "unverified"
    assert rights["unknown_revision_count"] == 1


def test_quantitative_receipt_from_stored_bars_is_withheld_after_revocation(market):  # noqa: F811
    _store, run = _run_screen(market)
    revision_ids = run["input_snapshot"]["input_revision_ids"]
    quant = MarketQuantitativeStore(market, now=lambda: ACQUIRED_CUTOFF)
    stored = quant._persist(
        NAMESPACE, "event-study", PRINCIPAL,
        {"source_revision_ids": revision_ids},
        {"contract": "noesis-market-event-study-v1", "kind": "event-study", "source_revision_ids": revision_ids, "formula_version": "fixture:1"},
    )
    assert quant.inspect_run(NAMESPACE, stored["run_id"], principal_id=PRINCIPAL, scopes=SCOPES)["current_rights"]["state"] == "authorized"

    _set_policy(market, capabilities=FIXTURE_CAPABILITIES, status="revoked", expected_revision=1)

    exported = quant.export_run(NAMESPACE, stored["run_id"], principal_id=PRINCIPAL, scopes=SCOPES)
    assert exported["withheld"] is True and exported["artifact"] is None


def test_retention_run_purges_sources_then_dependent_derived_receipts(market):  # noqa: F811
    store, run = _run_screen(market)
    # A later policy drops retention; the stored sources and the cached run
    # that depends on them must both go, leaving payload-free tombstones.
    _set_policy(
        market,
        capabilities=[item for item in FIXTURE_CAPABILITIES if item != "retain"],
        expected_revision=1,
    )

    report = MarketEntitlementStore(market, now=lambda: ACQUIRED_CUTOFF).run_retention(
        NAMESPACE, principal_id="fixture-reviewer", scopes={"operator"}
    )

    assert report["complete"] is True
    assert report["source_revisions"]["purged_count"] > 0
    derived = report["derived_receipts"]
    assert {item["object_kind"] for item in derived["purged"]} == {"derived:screener_run"}
    assert derived["purged"][0]["object_id"] == run["run_id"]
    for part in (report["source_revisions"], derived):
        Draft7Validator(PURGE_SCHEMA).validate(part)
    assert market.execute(
        "SELECT count(*) FROM market_screener_run_snapshots WHERE run_id=?", [run["run_id"]]
    ).fetchone()[0] == 0
    tombstone = market.execute(
        "SELECT object_kind,reason_code FROM market_entitlement_purge_events WHERE object_id=?",
        [run["run_id"]],
    ).fetchone()
    assert tombstone[0] == "derived:screener_run"
    # The saved query definition is not provider data and is untouched.
    assert market.execute(
        "SELECT count(*) FROM market_screener_query_revisions"
    ).fetchone()[0] == 1


def test_provenance_walk_ignores_decision_summaries_without_provider():
    revision_ids, refs = collect_receipt_provenance(
        {
            "source_entitlements": [{"entitlement_id": "e", "license_id": "l", "allowed": True}],
            "source_refs": [{"entitlement_id": "e", "license_id": "l", "provider": "p"}],
            "nested": [{"input_revision_ids": ["r1", "r2"]}, {"revision_id": "r3"}],
        }
    )
    assert revision_ids == {"r1", "r2", "r3"}
    assert refs == [{"entitlement_id": "e", "license_id": "l", "provider": "p"}]


def test_retention_schedule_runs_due_namespaces_once_per_interval(market):  # noqa: F811
    from src.domains.market.entitlements import MarketRetentionSchedule

    clock = {"now": ACQUIRED_CUTOFF}
    _run_screen(market)
    _set_policy(
        market,
        capabilities=[item for item in FIXTURE_CAPABILITIES if item != "retain"],
        expected_revision=1,
    )
    schedule = MarketRetentionSchedule(
        market,
        {"enabled": True, "namespaces": [NAMESPACE], "interval_s": 3600},
        now=lambda: clock["now"],
    )

    first = schedule.tick("worker:test")
    second = schedule.tick("worker:test")
    clock["now"] += 3_600_000
    third = schedule.tick("worker:test")

    assert first["runs"][0]["status"] == "complete"
    assert first["runs"][0]["purged_derived_receipts"] == 1
    assert second["runs"] == []
    assert third["runs"][0]["purged_source_revisions"] == 0
    assert MarketRetentionSchedule(market, None).tick("worker:test") == {
        "contract": "noesis-market-retention-tick-v1", "enabled": False, "runs": []
    }


def _report_citing(revision_id):
    return {
        "title": "Peer price note",
        "snapshot": {"id": "snapshot:1", "generations": {NAMESPACE: 1}},
        "sections": [{"id": "s", "title": "Prices", "assertions": [{
            "id": "a1", "text": "The subject closed above 100.", "kind": "sourced",
            "citations": ["bar"],
            "dependencies": [{"kind": "source", "id": revision_id, "revision": revision_id,
                              "namespace": NAMESPACE, "locator": {"revision_id": revision_id}}],
        }]}],
        "bibliography": [{"id": "bar", "text": "Fixture provider daily bar."}],
        "limitations": ["Fixture data."],
    }


def test_authored_report_export_rechecks_cited_market_evidence(market):  # noqa: F811
    from src.kb.authored_reports import AuthoredReportStore, ReportError

    _store, run = _run_screen(market)
    revision_id = run["input_snapshot"]["input_revision_ids"][0]
    auth = {
        "principal_id": PRINCIPAL,
        "scopes": SCOPES | {"knowledge:reports:read", "knowledge:reports:write", f"namespace:{NAMESPACE}:write"},
    }
    reports = AuthoredReportStore(market, now=lambda: ACQUIRED_CUTOFF)
    report = reports.create(NAMESPACE, "market-note", _report_citing(revision_id), **auth)

    exported = reports.export(NAMESPACE, report["report_id"], **auth)
    assert exported["market_rights"]["state"] == "authorized"
    assert exported["market_rights"]["market_dependencies"] == 1

    _set_policy(
        market,
        capabilities=[item for item in FIXTURE_CAPABILITIES if item not in {"export", "redistribute"}],
        expected_revision=1,
    )
    try:
        reports.export(NAMESPACE, report["report_id"], **auth)
    except ReportError as error:
        assert error.code == "market_rights_withheld"
        assert "operation_restricted" in str(error)
    else:  # pragma: no cover - the export must fail closed
        raise AssertionError("export succeeded without current market export rights")


def test_authored_report_without_market_evidence_is_unaffected(market):  # noqa: F811
    from src.kb.authored_reports import AuthoredReportStore

    auth = {
        "principal_id": PRINCIPAL,
        "scopes": SCOPES | {"knowledge:reports:read", "knowledge:reports:write", f"namespace:{NAMESPACE}:write"},
    }
    reports = AuthoredReportStore(market, now=lambda: ACQUIRED_CUTOFF)
    report = reports.create(NAMESPACE, "plain-note", _report_citing("document-revision:not-market"), **auth)

    assert "market_rights" not in reports.export(NAMESPACE, report["report_id"], **auth)
