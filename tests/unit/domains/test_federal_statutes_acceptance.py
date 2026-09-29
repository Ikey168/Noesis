"""Offline acceptance: a provision to its versions, amendment acts, dossier and citing decisions (#2105, FL12).

Journey on authored fictional fixtures (statute MPHG, BGBl. 2030 I Nr. 45, two
federal decisions, a DIP dossier, Directive (EU) 2030/77), with sockets
disabled: acquisition through the real adapters -> citation parsing -> as-of
selection (source-stated, observed, conflict, gap) -> provision diff ->
decision links -> dossier and EU links -> subscription events. The production
``legal-research`` 1.3.0 sources also replay offline through the source-pack
runtime. The journey answers are identical with the ``federal-statutes``
feature on and off (the feature governs composition, not stored evidence).
"""

from __future__ import annotations

import socket

import pytest

from src.domains import registry as domain_registry
from src.kb.legal import LegalStore
from src.kb.legal_federal import FederalStatutes, feature_enabled
from src.kb.legal_statute_monitoring import StatuteMonitor
from src.kb.subscriptions import SubscriptionStore
from tests.unit import federal_statutes_harness as h


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError(
            "network access attempted during the offline acceptance run"
        )

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = (
        dict(domain_registry._REGISTRY),
        set(domain_registry._ENABLED),
        domain_registry._AUTHORITY,
    )
    yield
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])


def journey(conn) -> dict:
    """Run the whole journey on ``conn`` and return the answers the acceptance asserts on."""
    h.cellar_directive(conn)
    h.apply(conn, "gii", h.gii_pages("2030-03-01"), at="2030-03-01")
    # The fictional statute is watchable once acquired (it is not in the FL01 set).
    monitor = StatuteMonitor(conn)
    watch = monitor.create(
        h.NS,
        "accept",
        watch="provision",
        statute="MPHG",
        provision="§ 5 Abs. 2 MPHG",
        principal_id="alice",
        scopes=h.SCOPES,
    )
    subscriptions = SubscriptionStore(conn)
    h.apply(conn, "ris", h.ris_pages(("2029-06-01",)), at="2030-03-02")
    subscriptions.commit_watermark(h.NS, 1, kind="ingestion")
    baseline = monitor.run(
        watch["subscription_id"], principal_id="alice", scopes=h.SCOPES
    )
    h.apply(conn, "bgbl", h.bgbl_pages(), at="2030-03-15")
    h.apply(conn, "ris", h.ris_pages(), at="2030-04-05")
    h.apply(conn, "gii", h.gii_pages("2030-06-01"), at="2030-06-01")
    h.apply(conn, "rii", h.rii_pages(), at="2031-06-01")
    h.dip_dossier(conn)
    federal = FederalStatutes(LegalStore(conn))
    links = federal.link_amendment_dossiers(
        h.NS, scopes=h.SCOPES, principal_id="alice", dossier_namespace=h.DOSSIER_NS
    )
    subscriptions.commit_watermark(h.NS, 2, kind="ingestion")
    events = monitor.run(
        watch["subscription_id"], principal_id="alice", scopes=h.SCOPES
    )
    read = h.READ_ONLY
    return {
        "citation": federal.resolve(h.NS, "§ 5 Abs. 2 MPHG a.F.", scopes=read),
        "gap": federal.provision_as_of(
            h.NS, "MPHG", "§ 5 Abs. 2 MPHG", "2029-01-01", scopes=read
        ),
        "stated": federal.provision_as_of(
            h.NS, "MPHG", "§ 5 Abs. 2 MPHG", "2030-03-15", scopes=read
        ),
        "observed": federal.provision_as_of(
            h.NS, "MPHG", "§ 5 Abs. 2 MPHG", "2031-02-01", scopes=read
        ),
        "conflict": federal.provision_as_of(
            h.NS, "MPHG", "§1/abs2", "2030-06-01", scopes=read
        ),
        "diff": federal.compare_provision(
            h.NS, "MPHG", "§5/abs2", "2030-03-15", "2031-02-01", scopes=read
        ),
        "acts": federal.list_amendment_acts(
            h.NS, scopes=read, statute="MPHG", provision="§5/abs2"
        ),
        "decisions": federal.decisions_citing(h.NS, "MPHG", "§5/abs2", scopes=read),
        "links": links,
        "baseline": baseline,
        "events": events,
    }


def strip_ids(value):
    """Answers without run-specific subscription and event identifiers (for on/off comparison)."""
    if isinstance(value, dict):
        return {
            k: strip_ids(v)
            for k, v in value.items()
            if k not in {"subscription_id", "event_id", "notification_id", "delivery"}
        }
    if isinstance(value, list):
        return [strip_ids(v) for v in value]
    return value


def check(answers) -> None:
    citation = answers["citation"]["citations"][0]
    assert (
        citation["statute"],
        citation["provisions"][0]["path"],
        citation["version_hint"],
    ) == ("MPHG", "§5/abs2", "a.F.")
    assert answers["gap"]["status"] == "no_version_on_record"
    stated = answers["stated"]
    assert (
        stated["status"] == "source_stated"
        and stated["selected"]["validity_basis"] == "source_stated"
    )
    assert (
        stated["selected"]["passages"][0]["text"]
        == "(2) Die Meldung ist innerhalb von zehn Handelstagen abzugeben."
    )
    observed = answers["observed"]
    assert observed["label"] == "observed on 2030-06-01, validity not stated"
    assert (
        observed["selected"]["passages"][0]["text"]
        == "(2) Die Meldung ist innerhalb von fünf Handelstagen abzugeben."
    )
    assert [c["validity_basis"] for c in answers["conflict"]["candidates"]] == [
        "source_stated",
        "observed",
    ]
    (act,) = answers["acts"]["acts"]
    assert (
        act["bgbl_citation"] == "BGBl. 2030 I Nr. 45"
        and act["promulgation_date"] == "2030-03-14"
    )
    assert (
        act["entry_into_force"][0]["text"]
        == "Dieses Gesetz tritt am 1. April 2030 in Kraft."
    )
    assert {i["provision"] for i in act["instructions"]} == {"§5", "§5/abs2"}
    assert (
        act["dossier_links"][0]["relation"] == "enacted_as"
        and act["implements"][0]["celex"] == "32030L0077"
    )
    diff = answers["diff"]
    assert [c["change"] for c in diff["changes"]] == ["changed"]
    assert (
        diff["amendment_acts_stated_by_the_source"][0]["bgbl_key"]
        == "bgbl-1/2030/nr-45"
    )
    selections = sorted(
        (c["raw"], c["selection"]["status"])
        for d in answers["decisions"]["decisions"]
        for c in d["citations"]
    )
    assert selections == [
        ("§ 5 Abs 2 MPHG", "observed"),
        ("§ 5 Abs. 2 MPHG", "observed"),
        ("§ 5 Abs. 2 MPHG a.F.", "a_f_earlier_version"),
    ]
    assert answers["links"]["linked"][0]["bgbl_key"] == "bgbl-1/2030/nr-45"
    assert answers["baseline"]["baseline"] is True
    assert sorted(n["kind"] for n in answers["events"]["notifications"]) == [
        "new_amendment_act",
        "new_citing_decision",
        "new_citing_decision",
        "new_citing_decision",
        "new_observed_version",
        "new_source_stated_version",
    ]
    for name in ("stated", "observed", "conflict", "gap"):
        assert "in_force" not in answers[name] and "is_current_law" not in answers[name]


def test_offline_journey_with_the_feature_on_and_off():
    from tests.unit.composition.test_migration import _migrated

    results = {}
    for selected in ([], ["federal-statutes"]):
        conn, coordinator, bundles, _ = _migrated()
        coordinator.select("legal", bundles["legal"]["version"], features=selected)
        assert coordinator.activate(f"legal-{len(selected)}")["status"] == "published"
        assert feature_enabled(conn) is bool(selected)
        answers = journey(conn)
        check(answers)
        results[bool(selected)] = strip_ids(answers)
    assert results[True] == results[False]


def test_reacquisition_is_idempotent_and_restart_keeps_answers(tmp_path):
    import duckdb

    path = str(tmp_path / "federal.duckdb")
    conn = duckdb.connect(path)
    answers = journey(conn)
    counts = {
        t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        for t in (
            "legal_works",
            "legal_versions",
            "legal_passages",
            "legal_statute_versions",
            "legal_amendments",
            "legal_provision_citations",
            "legal_implementation_links",
        )
    }
    for key, pages in (
        ("gii", h.gii_pages("2030-06-01")),
        ("ris", h.ris_pages()),
        ("bgbl", h.bgbl_pages()),
        ("rii", h.rii_pages()),
    ):
        h.apply(conn, key, pages, at="2031-06-02")
    assert {
        t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in counts
    } == counts
    conn.close()
    reopened = duckdb.connect(path, read_only=True)
    again = FederalStatutes(LegalStore(reopened, initialize=False)).provision_as_of(
        h.NS, "MPHG", "§ 5 Abs. 2 MPHG", "2031-02-01", scopes=h.READ_ONLY
    )
    assert (
        again["status"] == "observed"
        and again["selected"]["version_id"]
        == answers["observed"]["selected"]["version_id"]
    )


def test_production_sources_replay_offline_through_the_runtime():
    from tests.unit.domains.test_legal_pack import install, run

    conn = h.connection()
    value, runtime = install(conn)
    receipt = run(runtime, value, "federal-offline")
    sources = {s["source_id"]: s for s in receipt["sources"]}
    for source_id in (
        "gii-federal-statutes",
        "ris-federal-statute-versions",
        "bgbl-federal-promulgations",
    ):
        assert sources[source_id]["status"] == "complete", sources[source_id]
    outcomes = {
        (o["source_id"], o["outcome"])
        for o in LegalStore(conn).selection_outcomes(h.NS, receipt["run_id"])
    }
    assert ("gii-federal-statutes", "not_found") in outcomes
    assert ("bgbl-federal-promulgations", "seen_not_acquired") in outcomes
    assert (
        conn.execute("SELECT count(*) FROM legal_statute_versions").fetchone()[0] == 0
    )
