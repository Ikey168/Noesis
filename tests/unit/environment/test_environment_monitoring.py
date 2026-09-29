"""E11: subscriptions on thresholds, unavailability, permits/releases and vintages at committed watermarks."""

import ast

import pytest

from src.kb.environment_identity import EnvironmentIdentity
from src.kb.environment_monitoring import EnvironmentMonitor
from src.kb.environment_places import EnvironmentDossiers
from src.kb.environment_store import EnvironmentStoreError, record_id
from src.kb.subscriptions import SubscriptionStore
from tests.unit.environment import harness
from tests.unit.environment.harness import NS, SCOPES

THRESHOLDS = [
    {"indicator": "NO2", "op": "gt", "value": "0.040", "unit": "mg/m³"},
    {"indicator": "no2", "op": "gt", "value": "50", "unit": "µg/m³",
     "basis": {"cited": {"document_id": "user-note:hourly-alert", "quote": "alert me above 50 µg/m³ (my own level)"}}},
]


def _commit(env, watermark):
    # Stands in for the maintenance orchestrator / source run committing an ingestion watermark.
    SubscriptionStore(env.conn).commit_watermark(NS, watermark, kind="ingestion", detail={"generation": watermark})


@pytest.fixture
def monitored():
    env = harness.Env()
    env.install_berlin(run=False)
    assert all(r["ok"] for r in env.acquire_all())
    place = env.place()
    monitor = EnvironmentMonitor(env.conn, now=env.now)
    created = monitor.create(NS, "alex", place_id=place["place_id"], principal_id="alice", scopes=SCOPES,
                             thresholds=THRESHOLDS)
    return env, monitor, created, place


def test_monitoring_evaluates_only_committed_watermarks(monitored):
    env, monitor, created, _ = monitored
    with pytest.raises(EnvironmentStoreError) as error:
        monitor.run(created["subscription_id"], principal_id="alice", scopes=SCOPES)
    assert error.value.code == "watermark_uncommitted"
    assert "maintenance orchestrator" in created["refresh"]
    source = (harness.ROOT / "src/kb/environment_monitoring.py").read_text()
    imported = {n.names[0].name for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Import)}
    assert not imported & {"threading", "sched", "asyncio"}  # no new scheduler


def test_threshold_crossings_use_user_thresholds_and_observations_only(monitored):
    env, monitor, created, _ = monitored
    _commit(env, 1)
    result = monitor.run(created["subscription_id"], principal_id="alice", scopes=SCOPES)
    crossings = [n for n in result["notifications"] if n["kind"] == "threshold_crossed"]
    messages = " ".join(n["message"] for n in crossings)
    assert "53" in messages and "52.1" in messages and "45" in messages and "44.7" in messages
    assert all("user threshold" in n["message"] for n in crossings)
    cited = [n for n in crossings if n["cites"]["threshold_basis"] != "user-declared"]
    assert cited and all("not asserted by the pack" in n["message"] for n in cited)
    assert all(n["cites"]["vintage_id"].startswith("env-vintage:") for n in crossings)
    assert not any("open-meteo" in n["message"].lower() or "ICON" in n["message"] for n in crossings)
    kinds = {n["kind"] for n in result["notifications"]}
    assert {"new_unavailability", "facility_in_view", "series_in_view"} <= kinds
    replay = monitor.run(created["subscription_id"], 1, principal_id="alice", scopes=SCOPES)
    assert replay["status"] == "replayed" and replay["notifications"] == []


def test_updates_fire_with_revisions_and_invalidate_dossiers_and_evidence(monitored):
    env, monitor, created, place = monitored
    dossiers = EnvironmentDossiers(env.conn, now=env.now)
    dossier = dossiers.build(NS, "before", principal_id="alice", scopes=SCOPES, place_id=place["place_id"])
    evidence = EnvironmentIdentity(env.conn).attach_evidence(
        NS, subject_id="policy:subject", assertion_key="x", facility_record=record_id(NS, "facility", "eea-industry",
                                                                                         "DE.UBA.PRTR/000000901.FACILITY"),
        series_record=record_id(NS, "observation_series", "eea-industry", "DE.UBA.PRTR/000000901.FACILITY:CO2:AIR"),
        period_start="2024-01-01", principal_id="alice", scopes=SCOPES) if _obligation(env) else None
    _commit(env, 1)
    monitor.run(created["subscription_id"], principal_id="alice", scopes=SCOPES)
    env.tick(3600)
    env.web.overrides.update({"entsoe_a80_unplanned.xml": "entsoe_a80_unplanned_rev2.xml",
                              "uba_measures_282_5.json": "uba_measures_282_5_validated.json"})
    assert env.acquire("entsoe")["ok"]
    assert env.acquire("uba", {**harness.selections()["uba"], "data_status": "validated"})["ok"]
    eea = (harness.fixture_builder.RAW / "eea_industry_berlin_2024.json").read_text().replace("612000000", "611500000")
    env.web.overrides["eea_industry_berlin_2024.json"] = eea.encode()
    assert env.acquire("eea-industry")["ok"]
    _commit(env, 2)
    result = monitor.run(created["subscription_id"], principal_id="alice", scopes=SCOPES)
    kinds = {n["kind"]: n for n in result["notifications"]}
    assert "revision 2" in kinds["updated_unavailability"]["message"]
    assert kinds["updated_unavailability"]["cites"]["revision_id"].startswith("env-rev:")
    assert "validated" in kinds["new_vintage"]["message"]
    assert kinds["releases_changed"]["cites"]["vintages"]
    assert result["stale_dossiers"] and result["stale_dossiers"][0]["dossier_id"] == dossier["dossier_id"]
    assert dossiers.inspect(NS, dossier["dossier_id"], scopes=SCOPES, principal_id="alice")["stale"]
    assert result["stale_obligation_evidence"][0]["evidence_id"] == evidence["evidence_id"]
    # A newly reported permit changes the facility item.
    permits = eea.replace("permits/hkw-mitte.pdf", "permits/hkw-mitte-2026-amendment.pdf")
    env.web.overrides["eea_industry_berlin_2024.json"] = permits.encode()
    assert env.acquire("eea-industry")["ok"]
    _commit(env, 3)
    later = monitor.run(created["subscription_id"], principal_id="alice", scopes=SCOPES, watermark=3)
    permit = next(n for n in later["notifications"] if n["kind"] == "permit_changed")
    assert permit["cites"]["revision_id"].startswith("env-rev:")


def _obligation(env):
    from src.kb.assertions import record_assertions

    record_assertions(env.conn, "policy:subject", {"x": {"value": "100000", "unit": "t"}}, effective_at_ms=1,
                      document_id="policy:doc", visibility="public")
    return True


def test_thresholds_need_a_known_unit_and_cited_sources_need_a_quote(monitored):
    env, monitor, _, place = monitored
    with pytest.raises(EnvironmentStoreError):
        monitor.create(NS, "bad-unit", place_id=place["place_id"], principal_id="alice", scopes=SCOPES,
                       thresholds=[{"indicator": "no2", "op": "gt", "value": "40", "unit": "bananas"}])
    with pytest.raises(EnvironmentStoreError):
        monitor.create(NS, "bad-cite", place_id=place["place_id"], principal_id="alice", scopes=SCOPES,
                       thresholds=[{"indicator": "no2", "op": "gt", "value": "40", "unit": "µg/m³",
                                    "basis": {"cited": {"source_url": "https://example.test"}}}])
    other = EnvironmentMonitor(env.conn, now=env.now)
    created = other.create(NS, "vintages-only", place_id=place["place_id"], principal_id="alice", scopes=SCOPES,
                           watch=["vintages"])
    with pytest.raises(EnvironmentStoreError):
        other.run(created["subscription_id"], principal_id="mallory", scopes=SCOPES)
    _commit(env, 1)
    polled = other.run(created["subscription_id"], principal_id="alice", scopes=SCOPES)
    assert {n["kind"] for n in polled["notifications"]} == {"series_in_view"}
    events = other.poll(created["subscription_id"], principal_id="alice", scopes=SCOPES)
    assert events["events"]
